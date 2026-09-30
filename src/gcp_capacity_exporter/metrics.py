"""Metric families, and the buffered snapshot behind the gauges.

Gauge samples of one scrape cycle are gathered in a SeriesBuffer and published
with a single swap: /metrics never shows a half-finished cycle, and series that
were not refreshed (failed calls, removed targets) drop out instead of
repeating a stale value. Counters are live.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import Iterable, Iterator, Mapping

from prometheus_client import (
    CollectorRegistry,
    Counter,
    Gauge,
    GCCollector,
    PlatformCollector,
    ProcessCollector,
)
from prometheus_client.core import GaugeMetricFamily
from prometheus_client.registry import Collector

from .advice import API_CAPACITY, API_HISTORY

CAPACITY_LABELS = ("region", "zone", "machine_type", "size", "target_distribution_shape")
PRICE_LABELS = ("region", "machine_type", "purchase_option")
PREEMPTION_LABELS = ("region", "zone", "machine_type")
ERROR_LABELS = (
    "api",
    "region",
    "zone",
    "machine_type",
    "size",
    "target_distribution_shape",
    "reason",
)


@dataclass(frozen=True)
class Family:
    name: str  # without the metric prefix
    signal: str  # the config signal that enables it
    labels: tuple[str, ...]
    help: str


FAMILIES = (
    Family(
        "spot_hourly_price",
        "price",
        PRICE_LABELS,
        "Spot VM list price in USD per hour: the advice/capacityHistory price interval "
        "active now.",
    ),
    Family(
        "spot_monthly_price",
        "price",
        PRICE_LABELS,
        "Spot VM list price in USD per month: hourly price x hours_per_month.",
    ),
    Family(
        "obtainability_score",
        "obtainability",
        CAPACITY_LABELS,
        "advice/capacity obtainability (0-1): likelihood of obtaining `size` Spot VMs of the "
        "machine type.",
    ),
    Family(
        "spot_preemption_rate",
        "preemption_rate",
        PREEMPTION_LABELS,
        "Latest daily Spot preemption rate (0-1) from advice/capacityHistory; the current "
        "day is provisional.",
    ),
    Family(
        "estimated_uptime_seconds",
        "estimated_uptime",
        CAPACITY_LABELS,
        "advice/capacity estimated run time of most of the requested Spot VMs before "
        "preemption.",
    ),
)
_BY_NAME = {family.name: family for family in FAMILIES}

Snapshot = dict[str, dict[tuple[str, ...], float]]


class SeriesBuffer:
    """Gauge samples of one scrape cycle, by family and label values."""

    def __init__(self) -> None:
        self._series: Snapshot = {family.name: {} for family in FAMILIES}

    def add(self, family: str, labels: Mapping[str, str], value: float) -> None:
        spec = _BY_NAME[family]
        if labels.keys() != set(spec.labels):
            raise ValueError(f"{family}: expected labels {spec.labels}, got {tuple(labels)}")
        self._series[family][tuple(labels[name] for name in spec.labels)] = float(value)

    def __len__(self) -> int:
        return sum(len(series) for series in self._series.values())

    def freeze(self) -> Snapshot:
        return {name: dict(series) for name, series in self._series.items()}


class ExporterMetrics:
    """The exporter's registry: snapshot gauges, API counters, cycle duration."""

    def __init__(
        self,
        prefix: str,
        signals: Iterable[str],
        *,
        registry: CollectorRegistry | None = None,
        runtime_metrics: bool = True,
    ):
        self.registry = CollectorRegistry() if registry is None else registry
        enabled = set(signals)
        self._prefix = prefix
        self._families = tuple(family for family in FAMILIES if family.signal in enabled)
        self._snapshot: Snapshot = {}
        self._lock = threading.Lock()

        self._requests = Counter(
            f"{prefix}_requests_total",
            "Advice API HTTP requests sent, retries included.",
            ["api"],
            registry=self.registry,
        )
        for api in (API_CAPACITY, API_HISTORY):
            self._requests.labels(api=api)  # export zeros so rate() works from the start
        self._errors = Counter(
            f"{prefix}_errors_total",
            "Advice API calls that failed after retries, or returned no usable value for an "
            "enabled signal.",
            list(ERROR_LABELS),
            registry=self.registry,
        )
        self.scrape_duration = Gauge(
            f"{prefix}_scrape_duration_seconds",
            "Duration of the last completed scrape cycle.",
            registry=self.registry,
        )
        self.registry.register(_SnapshotCollector(self))
        if runtime_metrics:
            ProcessCollector(registry=self.registry)
            PlatformCollector(registry=self.registry)
            GCCollector(registry=self.registry)

    def count_request(self, api: str) -> None:
        self._requests.labels(api=api).inc()

    def count_error(
        self,
        *,
        api: str,
        region: str,
        zone: str,
        machine_type: str,
        size: str,
        shape: str,
        reason: str,
    ) -> None:
        self._errors.labels(api, region, zone, machine_type, size, shape, reason).inc()

    def publish(self, buffer: SeriesBuffer) -> None:
        """Replace every gauge series with the buffer's contents in one step."""
        frozen = buffer.freeze()
        with self._lock:
            self._snapshot = frozen

    def gauge_families(self, *, empty: bool = False) -> Iterator[GaugeMetricFamily]:
        with self._lock:
            snapshot = self._snapshot
        for family in self._families:
            metric = GaugeMetricFamily(
                f"{self._prefix}_{family.name}", family.help, labels=family.labels
            )
            if not empty:
                for label_values, value in sorted(snapshot.get(family.name, {}).items()):
                    metric.add_metric(label_values, value)
            yield metric


class _SnapshotCollector(Collector):
    def __init__(self, metrics: ExporterMetrics):
        self._metrics = metrics

    def describe(self) -> Iterator[GaugeMetricFamily]:
        return self._metrics.gauge_families(empty=True)

    def collect(self) -> Iterator[GaugeMetricFamily]:
        return self._metrics.gauge_families()
