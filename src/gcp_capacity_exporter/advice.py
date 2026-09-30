"""Minimal client for the Compute Engine beta advice APIs.

- advice/capacity: Spot obtainability and estimated uptime for one machine type
  at a requested size, for a target distribution shape or one pinned zone.
- advice/capacityHistory: Spot PRICE (regional) and PREEMPTION (regional or
  zonal) history for one machine type.

Pinned to compute/beta, whose scores are `obtainability` and `estimatedUptime`
(the alpha `uptimeScore` no longer exists). Every request is authenticated;
there is no unauthenticated fallback.
"""

from __future__ import annotations

import math
import random
import re
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Callable, Protocol

import google.auth
import requests
from google.auth.exceptions import GoogleAuthError
from google.auth.transport.requests import Request as GoogleAuthRequest
from requests.adapters import HTTPAdapter

DEFAULT_BASE_URL = "https://compute.googleapis.com/compute/beta"
SCOPES = ("https://www.googleapis.com/auth/cloud-platform",)
API_CAPACITY = "capacity"
API_HISTORY = "capacity_history"
RETRYABLE_STATUS = frozenset({429, 500, 502, 503, 504})
MAX_RETRY_AFTER_S = 60.0

# transport(url, body, headers, timeout_s) -> decoded JSON object; raises AdviceError
Transport = Callable[[str, dict, dict, float], dict]

_DURATION_RE = re.compile(r"(\d+(?:\.\d+)?)s?")
_FRACTION_RE = re.compile(r"(\.\d{6})\d+")  # datetime parses at most microseconds
_NANOS = Decimal(1_000_000_000)
_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)
_PREEMPTION_KEYS = ("preemptionRate", "rate", "value")
_RATE_LIMIT_REASONS = frozenset({"rateLimitExceeded", "userRateLimitExceeded"})


class AdviceError(Exception):
    """A failed advice API call; `reason` becomes the errors_total label."""

    def __init__(
        self,
        message: str,
        *,
        reason: str = "other",
        retryable: bool = False,
        status: int | None = None,
        retry_after_s: float | None = None,
    ):
        super().__init__(message)
        self.reason = reason
        self.retryable = retryable
        self.status = status
        self.retry_after_s = retry_after_s


class AuthError(AdviceError):
    def __init__(self, message: str):
        super().__init__(message, reason="auth", retryable=True)


class Cancelled(Exception):
    """The exporter is shutting down; the call was abandoned before it was sent."""


@dataclass(frozen=True)
class CapacityScores:
    obtainability: float | None  # 0.0-1.0
    estimated_uptime_s: float | None  # documented values: 60, 600, 3600


@dataclass(frozen=True)
class HistorySignals:
    hourly_price_usd: Decimal | None
    preemption_rate: float | None  # 0.0-1.0


class TokenSource(Protocol):
    def headers(self) -> dict[str, str]: ...

    def invalidate(self) -> None: ...


class GoogleTokenSource:
    """Authorization headers from Application Default Credentials (thread-safe)."""

    def __init__(self, credentials: Any):
        self._credentials = credentials
        self._lock = threading.Lock()
        self._request = GoogleAuthRequest()
        self._force_refresh = False

    @classmethod
    def from_adc(cls) -> GoogleTokenSource:
        try:
            credentials, _ = google.auth.default(scopes=list(SCOPES))
        except GoogleAuthError as exc:
            raise AuthError(f"no usable Application Default Credentials: {exc}") from exc
        return cls(credentials)

    def headers(self) -> dict[str, str]:
        with self._lock:
            if self._force_refresh or not self._credentials.valid:
                try:
                    self._credentials.refresh(self._request)
                except GoogleAuthError as exc:
                    raise AuthError(f"could not refresh GCP credentials: {exc}") from exc
                self._force_refresh = False
            headers: dict[str, str] = {}
            self._credentials.apply(headers)  # bearer token, plus the quota project if set
            return headers

    def invalidate(self) -> None:
        with self._lock:
            self._force_refresh = True


class RequestsTransport:
    """JSON POST over one shared requests.Session sized for the worker pool."""

    def __init__(self, pool_size: int = 10):
        self._session = requests.Session()
        adapter = HTTPAdapter(pool_connections=1, pool_maxsize=pool_size)
        self._session.mount("https://", adapter)
        self._session.mount("http://", adapter)

    def __call__(self, url: str, body: dict, headers: dict, timeout_s: float) -> dict:
        try:
            response = self._session.post(url, json=body, headers=headers, timeout=timeout_s)
        except requests.Timeout as exc:
            raise AdviceError(
                f"POST {url} timed out after {timeout_s:g}s", reason="timeout", retryable=True
            ) from exc
        except requests.RequestException as exc:
            raise AdviceError(
                f"POST {url} failed: {exc}", reason="connection", retryable=True
            ) from exc
        if response.status_code >= 400:
            detail, rate_limited = _describe_error(response)
            raise AdviceError(
                f"HTTP {response.status_code} from {url}: {detail}",
                reason=f"http_{response.status_code}",
                retryable=response.status_code in RETRYABLE_STATUS or rate_limited,
                status=response.status_code,
                retry_after_s=_retry_after_s(response.headers.get("Retry-After")),
            )
        try:
            doc = response.json()
        except ValueError as exc:
            raise AdviceError(
                f"invalid JSON from {url}: {exc}", reason="invalid_response", retryable=True
            ) from exc
        if not isinstance(doc, dict):
            raise AdviceError(
                f"unexpected response from {url}: not a JSON object", reason="invalid_response"
            )
        return doc

    def close(self) -> None:
        self._session.close()


class AdviceClient:
    """advice/capacity and advice/capacityHistory calls with auth and retries.

    Every HTTP attempt, retries included, is reported through `on_request` so
    request counters match quota consumption. 429/5xx, rate-limit 403s,
    timeouts and connection errors are retried with exponential backoff and
    jitter (honouring Retry-After); a 401 refreshes the token once.
    """

    def __init__(
        self,
        project_id: str,
        tokens: TokenSource,
        *,
        transport: Transport | None = None,
        base_url: str = DEFAULT_BASE_URL,
        timeout_s: float = 30.0,
        max_attempts: int = 4,
        on_request: Callable[[str], None] = lambda api: None,
        sleep: Callable[[float], Any] = time.sleep,
        should_stop: Callable[[], bool] = lambda: False,
        now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ):
        self._project_id = project_id
        self._tokens = tokens
        self._transport = transport or RequestsTransport()
        self._base_url = base_url.rstrip("/")
        self._timeout_s = timeout_s
        self._max_attempts = max(1, max_attempts)
        self._on_request = on_request
        self._sleep = sleep
        self._should_stop = should_stop
        self._now = now

    def capacity(
        self, region: str, machine_type: str, size: int, shape: str = "ANY", zone: str | None = None
    ) -> CapacityScores:
        """Scores for `size` Spot VMs of one machine type.

        One type per call: multi-type requests return a single blended
        recommendation, which says nothing about the individual types.
        """
        distribution: dict[str, Any] = {"targetShape": shape}
        if zone:
            distribution["zones"] = [{"zone": f"zones/{zone}"}]
        body = {
            "instanceProperties": {"scheduling": {"provisioningModel": "SPOT"}},
            "instanceFlexibilityPolicy": {
                "instanceSelections": {"selection-1": {"machineTypes": [machine_type]}}
            },
            "distributionPolicy": distribution,
            "size": size,
        }
        doc = self._post(API_CAPACITY, f"/regions/{region}/advice/capacity", body)
        recommendations = doc.get("recommendations")
        if not isinstance(recommendations, list) or not recommendations:
            raise AdviceError("advice/capacity returned no recommendation", reason="no_data")
        first = recommendations[0] if isinstance(recommendations[0], dict) else {}
        scores = first.get("scores") if isinstance(first.get("scores"), dict) else {}
        return CapacityScores(
            obtainability=parse_float(scores.get("obtainability")),
            estimated_uptime_s=parse_duration_s(scores.get("estimatedUptime")),
        )

    def history(
        self,
        region: str,
        machine_type: str,
        *,
        price: bool,
        preemption: bool,
        zone: str | None = None,
    ) -> HistorySignals:
        """Current hourly Spot price and/or latest daily preemption rate."""
        if not (price or preemption):
            raise ValueError("history: request price, preemption, or both")
        if price and zone:
            raise ValueError("history: Spot prices are regional; request them without a zone")
        body: dict[str, Any] = {
            "types": (["PRICE"] if price else []) + (["PREEMPTION"] if preemption else []),
            "instanceProperties": {
                "machineType": machine_type,
                "scheduling": {"provisioningModel": "SPOT"},
            },
        }
        if zone:
            body["locationPolicy"] = {"location": f"zones/{zone}"}
        doc = self._post(API_HISTORY, f"/regions/{region}/advice/capacityHistory", body)
        return HistorySignals(
            hourly_price_usd=(
                active_hourly_price(doc.get("priceHistory"), self._now()) if price else None
            ),
            preemption_rate=(
                latest_preemption_rate(doc.get("preemptionHistory")) if preemption else None
            ),
        )

    def _post(self, api: str, path: str, body: dict) -> dict:
        url = f"{self._base_url}/projects/{self._project_id}{path}"
        attempt = 0
        refreshed = False
        while True:
            if self._should_stop():
                raise Cancelled(url)
            attempt += 1
            try:
                headers = {"Content-Type": "application/json", **self._tokens.headers()}
                self._on_request(api)
                return self._transport(url, body, headers, self._timeout_s)
            except AdviceError as exc:
                if exc.status == 401 and not refreshed:
                    # expired or revoked token: refresh once without spending an attempt
                    refreshed = True
                    attempt -= 1
                    self._tokens.invalidate()
                    continue
                if not exc.retryable or attempt >= self._max_attempts:
                    if attempt > 1:
                        raise AdviceError(
                            f"{exc} (gave up after {attempt} attempts)",
                            reason=exc.reason,
                            status=exc.status,
                        ) from exc
                    raise
                self._sleep(_backoff_s(attempt, exc.retry_after_s))


# -- response parsing -----------------------------------------------------------


def parse_float(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def parse_duration_s(value: Any) -> float | None:
    """Duration proto JSON ("600s", "3.5s") or {"seconds", "nanos"} -> seconds."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return parse_float(value)
    if isinstance(value, dict):
        seconds = parse_float(value.get("seconds") or 0)
        nanos = parse_float(value.get("nanos") or 0)
        if seconds is None or nanos is None:
            return None
        return seconds + nanos / 1e9
    match = _DURATION_RE.fullmatch(str(value).strip())
    return float(match.group(1)) if match else None


def parse_money(money: Any) -> Decimal | None:
    """google.type.Money JSON ({"units": "0", "nanos": 478720000}) -> Decimal.

    proto3 JSON omits zero fields, so either part may be missing (or be a
    string); a document with neither carries no price at all.
    """
    if not isinstance(money, dict) or ("units" not in money and "nanos" not in money):
        return None
    try:
        units = Decimal(str(money.get("units") or 0))
        nanos = Decimal(str(money.get("nanos") or 0))
    except InvalidOperation:
        return None
    value = units + nanos / _NANOS
    return value if value.is_finite() else None


def parse_timestamp(value: Any) -> datetime | None:
    """RFC 3339 timestamp (nanosecond precision allowed) -> aware datetime."""
    if not isinstance(value, str) or not value.strip():
        return None
    text = _FRACTION_RE.sub(r"\1", value.strip()).replace("Z", "+00:00")
    try:
        ts = datetime.fromisoformat(text)
    except ValueError:
        return None
    return ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)


def active_hourly_price(history: Any, now: datetime) -> Decimal | None:
    """USD/hour of the priceHistory interval active at `now`.

    Falls back to the most recently started interval when none covers `now`
    (the history has gaps when data is unavailable). Non-USD, future-dated and
    non-positive entries are ignored.
    """
    active: tuple[datetime, Decimal] | None = None
    latest: tuple[datetime, Decimal] | None = None
    for item in history if isinstance(history, list) else ():
        if not isinstance(item, dict):
            continue
        money = item.get("listPrice")
        if not isinstance(money, dict) or (money.get("currencyCode") or "USD") != "USD":
            continue
        price = parse_money(money)
        interval = item.get("interval") if isinstance(item.get("interval"), dict) else {}
        start = parse_timestamp(interval.get("startTime"))
        if price is None or price <= 0 or start is None or start > now:
            continue
        end = parse_timestamp(interval.get("endTime"))
        if (end is None or end > now) and (active is None or start > active[0]):
            active = (start, price)
        if latest is None or start > latest[0]:
            latest = (start, price)
    chosen = active or latest
    return chosen[1] if chosen else None


def latest_preemption_rate(history: Any) -> float | None:
    """Rate of the most recent daily preemptionHistory record.

    The current day's record is provisional (it changes during the day) but is
    the freshest signal. Accepts the same rate keys as ccc-costopt's parser.
    """
    best: tuple[tuple[datetime, int], float] | None = None
    for index, item in enumerate(history if isinstance(history, list) else ()):
        if not isinstance(item, dict):
            continue
        key = next((k for k in _PREEMPTION_KEYS if k in item), None)
        rate = parse_float(item[key]) if key else None
        if rate is None:
            continue
        interval = item.get("interval") if isinstance(item.get("interval"), dict) else {}
        order = (parse_timestamp(interval.get("startTime")) or _EPOCH, index)
        if best is None or order > best[0]:
            best = (order, rate)
    return best[1] if best else None


# -- helpers ----------------------------------------------------------------------


def _backoff_s(attempt: int, retry_after_s: float | None) -> float:
    delay = 0.5 * 2 ** (attempt - 1) + random.uniform(0, 0.25)
    if retry_after_s is not None:
        delay = max(delay, min(retry_after_s, MAX_RETRY_AFTER_S))
    return delay


def _retry_after_s(value: str | None) -> float | None:
    if not value:
        return None
    try:
        seconds = float(value)
    except ValueError:
        return None  # the HTTP-date form is not worth parsing here
    return seconds if seconds >= 0 else None


def _describe_error(response: requests.Response) -> tuple[str, bool]:
    """(detail, rate_limited) from a Google API error body, else the raw text."""
    try:
        error = response.json().get("error")
    except (ValueError, AttributeError):
        error = None
    if not isinstance(error, dict):
        return (response.text or response.reason or "").strip()[:300], False
    reasons = {e.get("reason") for e in error.get("errors") or [] if isinstance(e, dict)}
    rate_limited = error.get("status") == "RESOURCE_EXHAUSTED" or bool(
        reasons & _RATE_LIMIT_REASONS
    )
    status, message = error.get("status"), str(error.get("message") or "").strip()
    detail = f"{status}: {message}" if status and message else message or str(status or "")
    return detail[:300], rate_limited
