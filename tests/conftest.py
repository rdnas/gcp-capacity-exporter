"""Shared test doubles: a routing advice-API transport and a static token source."""

from __future__ import annotations

import threading
from datetime import datetime, timedelta, timezone
from typing import Callable

from gcp_capacity_exporter.advice import AdviceClient

NOW = datetime(2026, 9, 30, 12, 0, tzinfo=timezone.utc)


def money(usd: str, currency: str = "USD") -> dict:
    """'0.05' -> {"currencyCode": "USD", "units": "0", "nanos": 50000000}."""
    units, _, fraction = usd.partition(".")
    return {"currencyCode": currency, "units": units, "nanos": int((fraction + "0" * 9)[:9])}


def price_interval(start: str, end: str | None, usd: str, currency: str = "USD") -> dict:
    interval = {"startTime": start}
    if end is not None:
        interval["endTime"] = end
    return {"interval": interval, "listPrice": money(usd, currency)}


def preemption_day(day: str, rate: float) -> dict:
    """One daily record; days start at midnight Pacific time (07:00 UTC)."""
    start = datetime.fromisoformat(f"{day}T07:00:00+00:00")
    return {
        "interval": {"startTime": _rfc3339(start), "endTime": _rfc3339(start + timedelta(days=1))},
        "preemptionRate": rate,
    }


def _rfc3339(ts: datetime) -> str:
    return ts.strftime("%Y-%m-%dT%H:%M:%SZ")


def capacity_doc(obtainability: float | None = 0.8, uptime: str | None = "3600s") -> dict:
    scores = {}
    if obtainability is not None:
        scores["obtainability"] = obtainability
    if uptime is not None:
        scores["estimatedUptime"] = uptime
    return {"recommendations": [{"scores": scores, "shards": []}]}


def history_doc(call: dict, usd: str = "0.05", rate: float = 0.03) -> dict:
    doc: dict = {"machineType": call["machine_type"]}
    if "PRICE" in call["types"]:
        doc["priceHistory"] = [price_interval("2026-09-01T07:00:00Z", None, usd)]
    if "PREEMPTION" in call["types"]:
        doc["preemptionHistory"] = [
            preemption_day("2026-09-29", 0.5),
            preemption_day("2026-09-30", rate),
        ]
    return doc


class StaticTokens:
    def __init__(self) -> None:
        self.invalidations = 0

    def headers(self) -> dict[str, str]:
        return {"authorization": "Bearer test-token"}

    def invalidate(self) -> None:
        self.invalidations += 1


class FakeAPI:
    """Transport double: decodes each advice call, records it, and routes it to a handler.

    Handlers receive the decoded call dict and return a JSON document or raise
    AdviceError, exactly like the real transport.
    """

    def __init__(
        self,
        capacity: Callable[[dict], dict] | None = None,
        history: Callable[[dict], dict] | None = None,
    ):
        self.calls: list[dict] = []
        self._lock = threading.Lock()
        self._capacity = capacity or (lambda call: capacity_doc())
        self._history = history or history_doc

    def __call__(self, url: str, body: dict, headers: dict, timeout_s: float) -> dict:
        region = url.split("/regions/", 1)[1].split("/", 1)[0]
        if url.endswith("/advice/capacity"):
            policy = body["distributionPolicy"]
            selection = body["instanceFlexibilityPolicy"]["instanceSelections"]["selection-1"]
            zones = policy.get("zones") or []
            call = {
                "api": "capacity",
                "region": region,
                "machine_type": selection["machineTypes"][0],
                "size": body["size"],
                "shape": policy["targetShape"],
                "zone": zones[0]["zone"].removeprefix("zones/") if zones else "",
            }
            handler = self._capacity
        elif url.endswith("/advice/capacityHistory"):
            location = (body.get("locationPolicy") or {}).get("location", "")
            call = {
                "api": "capacity_history",
                "region": region,
                "machine_type": body["instanceProperties"]["machineType"],
                "types": tuple(body["types"]),
                "zone": location.removeprefix("zones/"),
            }
            handler = self._history
        else:
            raise AssertionError(f"unexpected URL {url}")
        call.update(url=url, body=body, headers=headers)
        with self._lock:
            self.calls.append(call)
        return handler(call)

    def count(self, **match) -> int:
        return sum(all(call.get(k) == v for k, v in match.items()) for call in self.calls)


def make_client(transport, **kwargs) -> AdviceClient:
    kwargs.setdefault("sleep", lambda seconds: None)
    kwargs.setdefault("now", lambda: NOW)
    tokens = kwargs.pop("tokens", None) or StaticTokens()
    return AdviceClient("my-project", tokens, transport=transport, **kwargs)
