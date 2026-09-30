"""Scrape cycle: expand targets into advice API jobs, run them on a worker pool,
and publish every result as one metrics snapshot."""

from __future__ import annotations

import logging
import math
import threading
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Callable

from .advice import API_CAPACITY, API_HISTORY, AdviceClient, AdviceError, Cancelled
from .config import CAPACITY_SIGNALS, Config
from .metrics import ExporterMetrics, SeriesBuffer

log = logging.getLogger(__name__)

ZONAL_SHAPE = "ANY_SINGLE_ZONE"  # a zone pin is only meaningful with this shape
PURCHASE_OPTION = "spot"  # matches ccc-costopt's PrometheusPriceProvider filter


@dataclass(frozen=True)
class CapacityJob:
    region: str
    machine_type: str
    size: int
    shape: str
    zone: str = ""  # "" = region-level


@dataclass(frozen=True)
class HistoryJob:
    region: str
    machine_type: str
    price: bool
    preemption: bool
    zone: str = ""  # zonal history is PREEMPTION only; prices are regional


@dataclass(frozen=True)
class CycleStats:
    calls: int
    failed: int
    series: int
    duration_s: float
    failures: dict[str, int] = field(default_factory=dict)  # errors_total reason -> count


@dataclass(frozen=True)
class _Outcome:
    samples: tuple[tuple[str, dict[str, str], float], ...] = ()
    failure: str | None = None  # errors_total reason
    cancelled: bool = False


def plan_jobs(cfg: Config) -> tuple[list[CapacityJob], list[HistoryJob]]:
    """The deduplicated API calls of one cycle, in config order.

    Region-level capacity calls are machine_types x sizes x shapes, and `zones`
    add one ANY_SINGLE_ZONE call per zone x size. History does not depend on
    shape or size: one call per (region, machine type), plus one zonal
    PREEMPTION call per zone when preemption_rate is enabled.
    """
    capacity: dict[CapacityJob, None] = {}
    history: dict[HistoryJob, None] = {}
    wants_capacity = not CAPACITY_SIGNALS.isdisjoint(cfg.signals)
    price, preemption = cfg.has("price"), cfg.has("preemption_rate")
    for target in cfg.targets:
        for mt in target.machine_types:
            if wants_capacity:
                for size in target.sizes:
                    for shape in target.target_distribution_shapes:
                        capacity[CapacityJob(target.region, mt, size, shape)] = None
                    for zone in target.zones:
                        capacity[CapacityJob(target.region, mt, size, ZONAL_SHAPE, zone)] = None
            if price or preemption:
                history[HistoryJob(target.region, mt, price, preemption)] = None
            if preemption:
                for zone in target.zones:
                    history[HistoryJob(target.region, mt, False, True, zone)] = None
    return list(capacity), list(history)


class Collector:
    def __init__(
        self,
        cfg: Config,
        client: AdviceClient,
        metrics: ExporterMetrics,
        *,
        stop_event: threading.Event | None = None,
        clock: Callable[[], float] = time.monotonic,
    ):
        self._cfg = cfg
        self._client = client
        self._metrics = metrics
        self._stop = stop_event or threading.Event()
        self._clock = clock
        self._hours_per_month = Decimal(cfg.hours_per_month)
        self.capacity_jobs, self.history_jobs = plan_jobs(cfg)
        scrape = cfg.scrape
        # Between cycles the loop idles for up to one interval; within a cycle
        # some job completes at least every max_attempts request timeouts.
        self.stale_after_s = 2 * scrape.interval_s + scrape.max_attempts * scrape.timeout_s + 60
        self._last_progress = clock()
        self._cycles = 0
        self._last_stats: CycleStats | None = None

    def run_forever(self) -> None:
        """Scrape right away, then on a fixed interval grid until stopped."""
        interval = self._cfg.scrape.interval_s
        next_start = self._clock()
        while not self._stop.is_set():
            try:
                self.run_cycle()
            except Exception:  # one broken cycle must not end the exporter
                log.exception("scrape cycle crashed")
            now = self._clock()
            ticks = max(1, math.ceil((now - next_start) / interval))
            if ticks > 1:
                log.warning(
                    "scrape cycle overran the %ss interval; skipping %d tick(s)",
                    interval,
                    ticks - 1,
                )
            next_start += ticks * interval
            self._stop.wait(max(0.0, next_start - now))

    def run_cycle(self) -> CycleStats | None:
        """Run every job once and publish the results; None if shutdown interrupted it."""
        started = self._clock()
        self._last_progress = started
        buffer = SeriesBuffer()
        failures: Counter[str] = Counter()
        jobs: list[tuple[Callable[[Any], _Outcome], Any]] = [
            *((self._capacity, job) for job in self.capacity_jobs),
            *((self._history, job) for job in self.history_jobs),
        ]
        with ThreadPoolExecutor(
            max_workers=self._cfg.scrape.workers, thread_name_prefix="scrape"
        ) as pool:
            futures = [pool.submit(run, job) for run, job in jobs]
            abandoned = False
            for future in as_completed(futures):
                self._last_progress = self._clock()
                outcome = future.result()
                if outcome.cancelled or self._stop.is_set():
                    abandoned = True
                    break
                for family, labels, value in outcome.samples:
                    buffer.add(family, labels, value)
                if outcome.failure:
                    failures[outcome.failure] += 1
            if abandoned:
                for future in futures:
                    future.cancel()
                log.info("scrape cycle abandoned: shutting down")
                return None

        self._metrics.publish(buffer)
        duration = self._clock() - started
        self._metrics.scrape_duration.set(duration)
        stats = CycleStats(
            calls=len(jobs),
            failed=sum(failures.values()),
            series=len(buffer),
            duration_s=duration,
            failures=dict(failures),
        )
        self._cycles += 1
        self._last_stats = stats
        self._last_progress = self._clock()
        log.info(
            "scrape cycle: %d calls, %d failed%s, %d series in %.1fs",
            stats.calls,
            stats.failed,
            f" ({', '.join(f'{r} x{n}' for r, n in sorted(failures.items()))})" if failures else "",
            stats.series,
            duration,
        )
        return stats

    def health(self) -> tuple[bool, dict[str, Any]]:
        """(healthy, detail) for /healthz: the loop must keep making progress."""
        idle = self._clock() - self._last_progress
        detail: dict[str, Any] = {
            "cycles_completed": self._cycles,
            "seconds_since_progress": round(idle, 1),
        }
        if self._last_stats is not None:
            stats = self._last_stats
            detail["last_cycle"] = {
                "calls": stats.calls,
                "failed": stats.failed,
                "series": stats.series,
                "duration_seconds": round(stats.duration_s, 3),
            }
        if idle > self.stale_after_s:
            detail["reason"] = (
                f"no scrape progress for {idle:.0f}s (limit {self.stale_after_s}s)"
            )
            return False, detail
        return True, detail

    # -- jobs (worker threads) ---------------------------------------------------

    def _capacity(self, job: CapacityJob) -> _Outcome:
        where = (
            f"advice/capacity {_location(job.region, job.zone)} {job.machine_type} "
            f"size={job.size} shape={job.shape}"
        )

        def fail(reason: str, message: str) -> str:
            return self._fail(
                reason,
                f"{where}: {message}",
                api=API_CAPACITY,
                region=job.region,
                zone=job.zone,
                machine_type=job.machine_type,
                size=str(job.size),
                shape=job.shape,
            )

        try:
            scores = self._client.capacity(
                job.region, job.machine_type, job.size, job.shape, zone=job.zone or None
            )
        except Cancelled:
            return _Outcome(cancelled=True)
        except AdviceError as exc:
            return _Outcome(failure=fail(exc.reason, str(exc)))
        except Exception as exc:  # a parsing bug must not take the cycle down
            log.exception("%s: unexpected error", where)
            return _Outcome(failure=fail("other", repr(exc)))

        labels = {
            "region": job.region,
            "zone": job.zone,
            "machine_type": job.machine_type,
            "size": str(job.size),
            "target_distribution_shape": job.shape,
        }
        samples, missing = [], []
        for signal, family, value in (
            ("obtainability", "obtainability_score", scores.obtainability),
            ("estimated_uptime", "estimated_uptime_seconds", scores.estimated_uptime_s),
        ):
            if not self._cfg.has(signal):
                continue
            if value is None:
                missing.append(signal)
            else:
                samples.append((family, labels, value))
        failure = fail("no_data", f"no {' or '.join(missing)} in the response") if missing else None
        return _Outcome(tuple(samples), failure)

    def _history(self, job: HistoryJob) -> _Outcome:
        where = f"advice/capacityHistory {_location(job.region, job.zone)} {job.machine_type}"

        def fail(reason: str, message: str) -> str:
            return self._fail(
                reason,
                f"{where}: {message}",
                api=API_HISTORY,
                region=job.region,
                zone=job.zone,
                machine_type=job.machine_type,
                size="",
                shape="",
            )

        try:
            signals = self._client.history(
                job.region,
                job.machine_type,
                price=job.price,
                preemption=job.preemption,
                zone=job.zone or None,
            )
        except Cancelled:
            return _Outcome(cancelled=True)
        except AdviceError as exc:
            return _Outcome(failure=fail(exc.reason, str(exc)))
        except Exception as exc:
            log.exception("%s: unexpected error", where)
            return _Outcome(failure=fail("other", repr(exc)))

        samples, missing = [], []
        if job.price:
            hourly = signals.hourly_price_usd
            if hourly is None:
                missing.append("current USD price")
            else:
                labels = {
                    "region": job.region,
                    "machine_type": job.machine_type,
                    "purchase_option": PURCHASE_OPTION,
                }
                samples.append(("spot_hourly_price", labels, float(hourly)))
                samples.append(
                    ("spot_monthly_price", labels, float(hourly * self._hours_per_month))
                )
        if job.preemption:
            if signals.preemption_rate is None:
                missing.append("preemption rate")
            else:
                labels = {"region": job.region, "zone": job.zone, "machine_type": job.machine_type}
                samples.append(("spot_preemption_rate", labels, signals.preemption_rate))
        failure = fail("no_data", f"no {' or '.join(missing)} in the history") if missing else None
        return _Outcome(tuple(samples), failure)

    def _fail(self, reason: str, message: str, **labels: str) -> str:
        log.warning("%s", message)
        self._metrics.count_error(reason=reason, **labels)
        return reason


def _location(region: str, zone: str) -> str:
    return f"{region}/{zone}" if zone else region
