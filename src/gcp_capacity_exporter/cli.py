"""Command line entry point: gcp-capacity-exporter --config config.yaml"""

from __future__ import annotations

import argparse
import logging
import os
import signal
import sys
import threading
from dataclasses import replace
from typing import Sequence

from prometheus_client import disable_created_metrics, generate_latest

from . import __version__
from .advice import AdviceClient, AuthError, GoogleTokenSource, RequestsTransport
from .collector import CapacityJob, Collector, HistoryJob, plan_jobs
from .config import Config, ConfigError, load_config, parse_listen
from .metrics import ExporterMetrics
from .server import MetricsServer

log = logging.getLogger("gcp_capacity_exporter")

CONFIG_ENV = "GCP_CAPACITY_EXPORTER_CONFIG"
LOG_LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR")


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    logging.basicConfig(
        level=args.log_level, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    try:
        cfg = load_config(args.config)
        if args.listen:
            host, port = parse_listen(args.listen, "--listen")
            cfg = replace(cfg, listen_host=host, listen_port=port)
    except ConfigError as exc:
        log.error("invalid configuration: %s", exc)
        return 2

    capacity_jobs, history_jobs = plan_jobs(cfg)
    if args.check_config:
        print(describe_plan(cfg, capacity_jobs, history_jobs))
        return 0

    try:
        tokens = GoogleTokenSource.from_adc()
        tokens.headers()  # fail at startup, not on the first API call
    except AuthError as exc:
        log.error("%s", exc)
        return 1

    disable_created_metrics()
    stop = threading.Event()
    metrics = ExporterMetrics(cfg.metric_prefix, cfg.signals, runtime_metrics=not args.once)
    transport = RequestsTransport(pool_size=cfg.scrape.workers)
    client = AdviceClient(
        cfg.project_id,
        tokens,
        transport=transport,
        timeout_s=cfg.scrape.timeout_s,
        max_attempts=cfg.scrape.max_attempts,
        on_request=metrics.count_request,
        sleep=stop.wait,
        should_stop=stop.is_set,
    )
    collector = Collector(cfg, client, metrics, stop_event=stop)

    if args.once:
        stats = collector.run_cycle()
        transport.close()
        sys.stdout.write(generate_latest(metrics.registry).decode("utf-8"))
        return 0 if stats is not None and stats.failed == 0 else 1

    try:
        server = MetricsServer(cfg.listen_host, cfg.listen_port, metrics.registry, collector.health)
    except OSError as exc:
        log.error("cannot listen on %s:%d: %s", cfg.listen_host, cfg.listen_port, exc)
        return 1

    def shutdown(signum: int, _frame: object) -> None:
        log.info("received %s, shutting down", signal.Signals(signum).name)
        stop.set()

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)
    server.start()
    log.info(
        "serving http://%s:%d/metrics; %d advice/capacity + %d advice/capacityHistory calls "
        "every %s",
        cfg.listen_host,
        server.port,
        len(capacity_jobs),
        len(history_jobs),
        _duration(cfg.scrape.interval_s),
    )
    try:
        collector.run_forever()
    finally:
        server.stop()
        transport.close()
    return 0


def describe_plan(
    cfg: Config, capacity_jobs: Sequence[CapacityJob], history_jobs: Sequence[HistoryJob]
) -> str:
    """Human-readable per-cycle API volume, for --check-config."""
    calls = len(capacity_jobs) + len(history_jobs)
    lines = [
        f"config OK: project {cfg.project_id}, {len(cfg.targets)} target(s), "
        f"signals: {', '.join(cfg.signals)}",
        f"per cycle: {len(capacity_jobs)} advice/capacity + {len(history_jobs)} "
        f"advice/capacityHistory calls (before retries), every {_duration(cfg.scrape.interval_s)}"
        f" = ~{calls * 3600 / cfg.scrape.interval_s:.0f} calls/hour",
    ]
    regions = dict.fromkeys(job.region for job in (*capacity_jobs, *history_jobs))
    for region in regions:
        regional = [j for j in capacity_jobs if j.region == region and not j.zone]
        zonal = sum(1 for j in capacity_jobs if j.region == region and j.zone)
        shapes = ", ".join(dict.fromkeys(j.shape for j in regional)) or "-"
        history = sum(1 for j in history_jobs if j.region == region)
        lines.append(
            f"  {region}: {len(regional)} regional capacity (shapes: {shapes}), "
            f"{zonal} zonal capacity, {history} history"
        )
    return "\n".join(lines)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="gcp-capacity-exporter",
        description="Prometheus exporter for GCE Capacity Advisor Spot signals.",
    )
    parser.add_argument(
        "-c",
        "--config",
        default=os.environ.get(CONFIG_ENV, "config.yaml"),
        help=f"YAML config file (default: ${CONFIG_ENV} or ./config.yaml)",
    )
    parser.add_argument("--listen", help="override the config's listen address (host:port)")
    env_level = os.environ.get("LOG_LEVEL", "").upper()
    parser.add_argument(
        "--log-level",
        type=str.upper,
        choices=LOG_LEVELS,
        default=env_level if env_level in LOG_LEVELS else "INFO",
        help="default: $LOG_LEVEL or INFO",
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--check-config",
        action="store_true",
        help="validate the config, print the per-cycle API call plan, and exit",
    )
    mode.add_argument(
        "--once",
        action="store_true",
        help="run one scrape cycle, print the metrics, and exit (non-zero if any call failed)",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    return parser


def _duration(seconds: int) -> str:
    for unit, size in (("h", 3600), ("m", 60)):
        if seconds % size == 0:
            return f"{seconds // size}{unit}"
    return f"{seconds}s"
