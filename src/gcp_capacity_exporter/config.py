"""Exporter configuration: YAML schema, defaults and validation.

The whole file is checked at startup, unknown keys included, so a typo or an
impossible target fails fast instead of surfacing later as API errors.
"""

from __future__ import annotations

import difflib
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, TypeVar

import yaml

SHAPES = ("ANY", "ANY_SINGLE_ZONE", "BALANCED")
SIGNALS = ("price", "obtainability", "preemption_rate", "estimated_uptime")
CAPACITY_SIGNALS = frozenset({"obtainability", "estimated_uptime"})  # advice/capacity
HISTORY_SIGNALS = frozenset({"price", "preemption_rate"})  # advice/capacityHistory

DEFAULT_LISTEN = "0.0.0.0:9469"
DEFAULT_METRIC_PREFIX = "gce_capacity"

_TOP_KEYS = frozenset(
    {"project_id", "listen", "metric_prefix", "scrape", "hours_per_month", "signals", "targets"}
)
_SCRAPE_KEYS = frozenset({"interval", "timeout", "workers", "max_attempts"})
_TARGET_KEYS = frozenset(
    {"region", "machine_types", "sizes", "target_distribution_shapes", "zones"}
)

_DURATION_UNITS = {"s": 1, "m": 60, "h": 3600, "d": 86400}
_DURATION_RE = re.compile(r"(\d+)([smhd])")
_PREFIX_RE = re.compile(r"[a-zA-Z_:][a-zA-Z0-9_:]*")
_NAME_RE = re.compile(r"[a-z][a-z0-9-]*")  # regions, zones, machine types

T = TypeVar("T")


class ConfigError(ValueError):
    """The exporter configuration is invalid."""


@dataclass(frozen=True)
class ScrapeConfig:
    interval_s: int = 300
    timeout_s: int = 30  # per HTTP request
    workers: int = 4  # concurrent API calls
    max_attempts: int = 4  # per API call, first try included


@dataclass(frozen=True)
class Target:
    region: str
    machine_types: tuple[str, ...]
    sizes: tuple[int, ...]
    target_distribution_shapes: tuple[str, ...] = ("ANY",)
    zones: tuple[str, ...] = ()  # optional per-zone series, always ANY_SINGLE_ZONE


@dataclass(frozen=True)
class Config:
    project_id: str
    targets: tuple[Target, ...]
    listen_host: str = "0.0.0.0"
    listen_port: int = 9469
    metric_prefix: str = DEFAULT_METRIC_PREFIX
    scrape: ScrapeConfig = field(default_factory=ScrapeConfig)
    hours_per_month: int = 730  # hourly -> monthly price normalization
    signals: tuple[str, ...] = SIGNALS

    def has(self, signal: str) -> bool:
        return signal in self.signals


def load_config(path: str | Path) -> Config:
    path = Path(path)
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ConfigError(f"cannot read {path}: {exc.strerror or exc}") from exc
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise ConfigError(f"{path}: invalid YAML: {exc}") from exc
    return parse_config(data)


def parse_config(data: Any) -> Config:
    if not isinstance(data, dict):
        raise ConfigError("expected a mapping at the top level")
    _check_keys(data, _TOP_KEYS, "config")
    project_id = _project_id(data.get("project_id"))
    signals = _signals(_get(data, "signals", list(SIGNALS)))
    needs_sizes = not CAPACITY_SIGNALS.isdisjoint(signals)
    targets = tuple(
        _target(raw, f"targets[{i}]", needs_sizes)
        for i, raw in enumerate(_list(data.get("targets"), "targets", required=True))
    )
    listen_host, listen_port = parse_listen(_get(data, "listen", DEFAULT_LISTEN))
    return Config(
        project_id=project_id,
        targets=targets,
        listen_host=listen_host,
        listen_port=listen_port,
        metric_prefix=_metric_prefix(_get(data, "metric_prefix", DEFAULT_METRIC_PREFIX)),
        scrape=_scrape(_get(data, "scrape", {})),
        hours_per_month=_int(_get(data, "hours_per_month", 730), "hours_per_month", minimum=1),
        signals=signals,
    )


def parse_listen(value: Any, where: str = "listen") -> tuple[str, int]:
    """'host:port', ':port' or '[ipv6]:port' -> (host, port); no host = all interfaces."""
    text = value.strip() if isinstance(value, str) else ""
    host, sep, port = text.rpartition(":")
    if not sep or not port.isdigit() or not 1 <= int(port) <= 65535:
        raise ConfigError(f"{where}: expected 'host:port' such as '0.0.0.0:9469', got {value!r}")
    if host.startswith("[") and host.endswith("]"):
        host = host[1:-1]
    return host or "0.0.0.0", int(port)


def parse_duration_s(value: Any, where: str) -> int:
    """Duration string ('30s', '5m', '1h30m') -> seconds."""
    if not isinstance(value, str):
        raise ConfigError(
            f"{where}: expected a duration string such as '30s' or '5m', got {value!r}"
        )
    text = value.strip()
    parts = _DURATION_RE.findall(text)
    if not parts or "".join(n + u for n, u in parts) != text:
        raise ConfigError(f"{where}: invalid duration {value!r} (use e.g. '30s', '5m', '1h30m')")
    seconds = sum(int(n) * _DURATION_UNITS[u] for n, u in parts)
    if seconds <= 0:
        raise ConfigError(f"{where}: must be greater than zero")
    return seconds


# -- sections ------------------------------------------------------------------


def _scrape(value: Any) -> ScrapeConfig:
    if not isinstance(value, dict):
        raise ConfigError("scrape: expected a mapping")
    _check_keys(value, _SCRAPE_KEYS, "scrape")
    defaults = ScrapeConfig()
    interval, timeout = value.get("interval"), value.get("timeout")
    return ScrapeConfig(
        interval_s=(
            defaults.interval_s
            if interval is None
            else parse_duration_s(interval, "scrape.interval")
        ),
        timeout_s=(
            defaults.timeout_s if timeout is None else parse_duration_s(timeout, "scrape.timeout")
        ),
        workers=_int(_get(value, "workers", defaults.workers), "scrape.workers", minimum=1),
        max_attempts=_int(
            _get(value, "max_attempts", defaults.max_attempts), "scrape.max_attempts", minimum=1
        ),
    )


def _target(raw: Any, where: str, needs_sizes: bool) -> Target:
    if not isinstance(raw, dict):
        raise ConfigError(f"{where}: expected a mapping")
    _check_keys(raw, _TARGET_KEYS, where)
    region = _name(raw.get("region"), f"{where}.region")
    machine_types = _unique(
        _name(mt, f"{where}.machine_types[{i}]")
        for i, mt in enumerate(
            _list(raw.get("machine_types"), f"{where}.machine_types", required=True)
        )
    )
    sizes = _unique(
        _int(size, f"{where}.sizes[{i}]", minimum=1)
        for i, size in enumerate(_list(raw.get("sizes"), f"{where}.sizes", required=needs_sizes))
    )
    shapes_raw = raw.get("target_distribution_shapes")
    shapes = (
        ("ANY",)
        if shapes_raw is None
        else _unique(
            _shape(shape, f"{where}.target_distribution_shapes[{i}]")
            for i, shape in enumerate(
                _list(shapes_raw, f"{where}.target_distribution_shapes", required=True)
            )
        )
    )
    zones = _unique(
        _zone(zone, region, f"{where}.zones[{i}]")
        for i, zone in enumerate(_list(raw.get("zones"), f"{where}.zones"))
    )
    return Target(region, machine_types, sizes, shapes, zones)


# -- scalars -------------------------------------------------------------------


def _project_id(value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ConfigError("project_id: required (the project billed for the advice API calls)")
    project_id = value.strip()
    if any(ch.isspace() or ch == "/" for ch in project_id):
        raise ConfigError(f"project_id: invalid project id {value!r}")
    return project_id


def _signals(value: Any) -> tuple[str, ...]:
    chosen = set()
    for i, signal in enumerate(_list(value, "signals", required=True)):
        if signal not in SIGNALS:
            raise ConfigError(
                f"signals[{i}]: unknown signal {signal!r} (allowed: {', '.join(SIGNALS)})"
            )
        chosen.add(signal)
    return tuple(s for s in SIGNALS if s in chosen)


def _metric_prefix(value: Any) -> str:
    prefix = value.strip().rstrip("_") if isinstance(value, str) else ""
    if not _PREFIX_RE.fullmatch(prefix):
        raise ConfigError(f"metric_prefix: not a valid Prometheus metric name prefix: {value!r}")
    return prefix


def _shape(value: Any, where: str) -> str:
    shape = value.strip().upper() if isinstance(value, str) else None
    if shape not in SHAPES:
        raise ConfigError(
            f"{where}: unknown target distribution shape {value!r} "
            f"(allowed: {', '.join(SHAPES)})"
        )
    return shape


def _zone(value: Any, region: str, where: str) -> str:
    zone = _name(value, where)
    if not zone.startswith(f"{region}-") or zone == f"{region}-":
        raise ConfigError(f"{where}: zone {zone!r} is not in region {region!r}")
    return zone


def _name(value: Any, where: str) -> str:
    if value is None:
        raise ConfigError(f"{where}: required")
    if not isinstance(value, str) or not _NAME_RE.fullmatch(value.strip()):
        raise ConfigError(
            f"{where}: expected a lowercase GCE name such as 'us-east4' or "
            f"'n2d-standard-8', got {value!r}"
        )
    return value.strip()


def _int(value: Any, where: str, *, minimum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ConfigError(f"{where}: expected an integer, got {value!r}")
    if value < minimum:
        raise ConfigError(f"{where}: must be >= {minimum}, got {value}")
    return value


# -- helpers -------------------------------------------------------------------


def _get(data: dict, key: str, default: Any) -> Any:
    value = data.get(key)
    return default if value is None else value


def _list(value: Any, where: str, *, required: bool = False) -> list:
    if value is None:
        if required:
            raise ConfigError(f"{where}: required")
        return []
    if not isinstance(value, list):
        raise ConfigError(f"{where}: expected a list, got {type(value).__name__}")
    if required and not value:
        raise ConfigError(f"{where}: must not be empty")
    return value


def _unique(items: Iterable[T]) -> tuple[T, ...]:
    return tuple(dict.fromkeys(items))


def _check_keys(data: dict, allowed: frozenset[str], where: str) -> None:
    for key in data:
        if key in allowed:
            continue
        close = difflib.get_close_matches(str(key), sorted(allowed), n=1)
        hint = f"; did you mean {close[0]!r}?" if close else ""
        raise ConfigError(
            f"{where}: unknown key {key!r}{hint} (allowed: {', '.join(sorted(allowed))})"
        )
