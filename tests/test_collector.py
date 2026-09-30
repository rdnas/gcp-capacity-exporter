"""Job planning and full scrape cycles against a fake advice API."""

import threading
import time

import pytest
from prometheus_client import generate_latest

from conftest import FakeAPI, capacity_doc, history_doc, make_client
from gcp_capacity_exporter.advice import AdviceError
from gcp_capacity_exporter.collector import CapacityJob, Collector, HistoryJob, plan_jobs
from gcp_capacity_exporter.config import parse_config
from gcp_capacity_exporter.metrics import ExporterMetrics

REGION = "us-east4"
T2D, N2D = "t2d-standard-8", "n2d-standard-8"


def config(*targets: dict, **fields):
    return parse_config({"project_id": "my-project", "targets": list(targets), **fields})


def target(**fields) -> dict:
    base = {"region": REGION, "machine_types": [T2D, N2D], "sizes": [10, 50]}
    base.update(fields)
    return base


def build(cfg, api, **collector_kwargs):
    metrics = ExporterMetrics(cfg.metric_prefix, cfg.signals, runtime_metrics=False)
    client = make_client(
        api,
        on_request=metrics.count_request,
        max_attempts=cfg.scrape.max_attempts,
        should_stop=collector_kwargs.get("stop_event", threading.Event()).is_set,
    )
    return Collector(cfg, client, metrics, **collector_kwargs), metrics


def sample(metrics, name, **labels):
    return metrics.registry.get_sample_value(f"gce_capacity_{name}", labels)


def capacity_labels(machine_type, size, shape, zone=""):
    return dict(
        region=REGION, zone=zone, machine_type=machine_type, size=str(size),
        target_distribution_shape=shape,
    )


def price_labels(machine_type):
    return dict(region=REGION, machine_type=machine_type, purchase_option="spot")


def error_labels(api, machine_type, reason, zone="", size="", shape=""):
    return dict(
        api=api, region=REGION, zone=zone, machine_type=machine_type, size=size,
        target_distribution_shape=shape, reason=reason,
    )


# -- planning -----------------------------------------------------------------------


def test_plan_is_types_x_sizes_x_shapes_with_one_history_call_per_type():
    capacity, history = plan_jobs(config(target(target_distribution_shapes=["ANY", "BALANCED"])))
    assert len(capacity) == 2 * 2 * 2
    assert {(j.machine_type, j.size, j.shape) for j in capacity} == {
        (mt, size, shape) for mt in (T2D, N2D) for size in (10, 50) for shape in ("ANY", "BALANCED")
    }
    assert all(j.zone == "" for j in capacity)
    assert history == [HistoryJob(REGION, T2D, True, True), HistoryJob(REGION, N2D, True, True)]


def test_zones_add_single_zone_capacity_and_zonal_preemption_jobs():
    capacity, history = plan_jobs(config(target(zones=["us-east4-a", "us-east4-c"])))
    zonal = [j for j in capacity if j.zone]
    assert len(capacity) == 2 * 2 * 1 + 2 * 2 * 2
    assert len(zonal) == 8 and {j.shape for j in zonal} == {"ANY_SINGLE_ZONE"}
    assert CapacityJob(REGION, T2D, 50, "ANY_SINGLE_ZONE", "us-east4-c") in zonal
    assert [j for j in history if j.zone] == [
        HistoryJob(REGION, mt, False, True, zone)
        for mt in (T2D, N2D)
        for zone in ("us-east4-a", "us-east4-c")
    ]


def test_overlapping_targets_are_deduplicated():
    capacity, history = plan_jobs(
        config(
            target(),
            target(machine_types=[N2D, "c3d-standard-8"], sizes=[50, 100]),
        )
    )
    keys = [(j.machine_type, j.size) for j in capacity]
    assert len(keys) == len(set(keys)) == 4 + 3
    assert [j.machine_type for j in history] == [T2D, N2D, "c3d-standard-8"]


@pytest.mark.parametrize(
    "signals, capacity_calls, history_jobs",
    [
        (["price"], 0, [HistoryJob(REGION, T2D, True, False), HistoryJob(REGION, N2D, True, False)]),
        (["obtainability"], 2 * 2 + 2 * 2, []),
        (
            ["preemption_rate"],
            0,
            [
                HistoryJob(REGION, T2D, False, True),
                HistoryJob(REGION, T2D, False, True, "us-east4-a"),
                HistoryJob(REGION, N2D, False, True),
                HistoryJob(REGION, N2D, False, True, "us-east4-a"),
            ],
        ),
    ],
)
def test_signals_decide_which_apis_are_called(signals, capacity_calls, history_jobs):
    capacity, history = plan_jobs(config(target(zones=["us-east4-a"]), signals=signals))
    assert len(capacity) == capacity_calls
    assert history == history_jobs


# -- cycles ----------------------------------------------------------------------------

OBTAINABILITY = {"ANY": 0.9, "BALANCED": 0.6, "ANY_SINGLE_ZONE": 0.4}
HOURLY = {T2D: "0.05", N2D: "0.08"}


def scored(call):
    size_penalty = 0.1 if call["size"] == 50 else 0.0
    uptime = "600s" if call["zone"] else "3600s"
    return capacity_doc(OBTAINABILITY[call["shape"]] - size_penalty, uptime)


def priced(call):
    return history_doc(call, usd=HOURLY[call["machine_type"]], rate=0.03)


def full_config(**fields):
    return config(
        target(target_distribution_shapes=["ANY", "BALANCED"], zones=["us-east4-a"]),
        hours_per_month=720,
        **fields,
    )


def test_cycle_publishes_every_family_in_one_flush():
    api = FakeAPI(capacity=scored, history=priced)
    collector, metrics = build(full_config(), api)
    assert sample(metrics, "obtainability_score", **capacity_labels(T2D, 10, "ANY")) is None

    stats = collector.run_cycle()

    # 2 types x 2 sizes x (2 shapes + 1 zone) capacity calls; history: 2 regional + 2 zonal
    assert (stats.calls, stats.failed) == (12 + 4, 0)
    assert stats.series == 12 * 2 + 2 * 2 + 2 * 2
    for mt in (T2D, N2D):
        for size, penalty in ((10, 0.0), (50, 0.1)):
            for shape in ("ANY", "BALANCED"):
                labels = capacity_labels(mt, size, shape)
                assert sample(metrics, "obtainability_score", **labels) == pytest.approx(
                    OBTAINABILITY[shape] - penalty
                )
                assert sample(metrics, "estimated_uptime_seconds", **labels) == 3600
            zonal = capacity_labels(mt, size, "ANY_SINGLE_ZONE", zone="us-east4-a")
            assert sample(metrics, "obtainability_score", **zonal) == pytest.approx(0.4 - penalty)
            assert sample(metrics, "estimated_uptime_seconds", **zonal) == 600

    assert sample(metrics, "spot_hourly_price", **price_labels(T2D)) == pytest.approx(0.05)
    assert sample(metrics, "spot_monthly_price", **price_labels(T2D)) == pytest.approx(36.0)
    assert sample(metrics, "spot_monthly_price", **price_labels(N2D)) == pytest.approx(57.6)
    for zone in ("", "us-east4-a"):
        labels = dict(region=REGION, zone=zone, machine_type=T2D)
        assert sample(metrics, "spot_preemption_rate", **labels) == pytest.approx(0.03)

    # prices are shape-independent: one PRICE call per type, never zonal
    assert api.count(api="capacity_history", types=("PRICE", "PREEMPTION"), zone="") == 2
    assert api.count(api="capacity_history", types=("PREEMPTION",), zone="us-east4-a") == 2
    assert sample(metrics, "requests_total", api="capacity") == 12
    assert sample(metrics, "requests_total", api="capacity_history") == 4
    assert sample(metrics, "scrape_duration_seconds") >= 0


def test_failed_calls_are_counted_and_their_stale_series_dropped():
    broken = threading.Event()

    def capacity(call):
        key = (call["machine_type"], call["size"], call["shape"])
        if broken.is_set() and key == (N2D, 50, "BALANCED"):
            raise AdviceError("HTTP 503", reason="http_503", retryable=True, status=503)
        return scored(call)

    def history(call):
        if broken.is_set() and call["zone"] and call["machine_type"] == T2D:
            raise AdviceError("HTTP 403", reason="http_403", status=403)
        return priced(call)

    collector, metrics = build(
        full_config(scrape={"max_attempts": 2}), FakeAPI(capacity=capacity, history=history)
    )
    collector.run_cycle()
    zonal_preemption = dict(region=REGION, zone="us-east4-a", machine_type=T2D)
    assert sample(metrics, "obtainability_score", **capacity_labels(N2D, 50, "BALANCED")) is not None
    assert sample(metrics, "spot_preemption_rate", **zonal_preemption) is not None

    broken.set()
    stats = collector.run_cycle()

    failed = capacity_labels(N2D, 50, "BALANCED")
    assert stats.failed == 2 and stats.failures == {"http_503": 1, "http_403": 1}
    assert sample(metrics, "obtainability_score", **failed) is None
    assert sample(metrics, "estimated_uptime_seconds", **failed) is None
    assert sample(metrics, "obtainability_score", **capacity_labels(N2D, 50, "ANY")) is not None
    assert sample(metrics, "spot_preemption_rate", **zonal_preemption) is None
    capacity_error = error_labels("capacity", N2D, "http_503", size="50", shape="BALANCED")
    history_error = error_labels("capacity_history", T2D, "http_403", zone="us-east4-a")
    assert sample(metrics, "errors_total", **capacity_error) == 1
    assert sample(metrics, "errors_total", **history_error) == 1
    # every attempt is a request: 12 + 12 capacity calls plus one retry of the 503
    assert sample(metrics, "requests_total", api="capacity") == 25
    assert sample(metrics, "requests_total", api="capacity_history") == 8


def test_missing_values_are_counted_as_no_data_but_partial_results_kept():
    def no_price(call):
        return {**history_doc(call), "priceHistory": []}

    api = FakeAPI(capacity=lambda call: capacity_doc(0.7, uptime=None), history=no_price)
    collector, metrics = build(config(target(machine_types=[T2D], sizes=[10])), api)
    stats = collector.run_cycle()
    assert stats.failures == {"no_data": 2}
    assert sample(metrics, "obtainability_score", **capacity_labels(T2D, 10, "ANY")) == 0.7
    assert sample(metrics, "estimated_uptime_seconds", **capacity_labels(T2D, 10, "ANY")) is None
    assert sample(metrics, "spot_hourly_price", **price_labels(T2D)) is None
    assert sample(metrics, "spot_preemption_rate", region=REGION, zone="", machine_type=T2D) == 0.03
    assert sample(
        metrics, "errors_total", **error_labels("capacity", T2D, "no_data", size="10", shape="ANY")
    ) == 1
    assert sample(metrics, "errors_total", **error_labels("capacity_history", T2D, "no_data")) == 1


def test_disabled_signals_are_neither_fetched_nor_exported():
    api = FakeAPI()
    collector, metrics = build(config(target(), signals=["obtainability"]), api)
    collector.run_cycle()
    text = generate_latest(metrics.registry).decode()
    assert api.count(api="capacity_history") == 0
    assert "gce_capacity_obtainability_score{" in text
    absent = ("estimated_uptime_seconds", "spot_hourly_price", "spot_monthly_price",
              "spot_preemption_rate")
    for name in absent:
        assert f"gce_capacity_{name}" not in text


def test_shutdown_abandons_the_cycle_without_publishing():
    stop = threading.Event()

    def stop_on_first_call(call):
        stop.set()
        return scored(call)

    collector, metrics = build(full_config(), FakeAPI(capacity=stop_on_first_call), stop_event=stop)
    assert collector.run_cycle() is None
    assert sample(metrics, "obtainability_score", **capacity_labels(T2D, 10, "ANY")) is None
    assert sample(metrics, "requests_total", api="capacity") < 12


def test_health_turns_unhealthy_without_progress():
    now = [1000.0]
    collector, _ = build(config(target()), FakeAPI(), clock=lambda: now[0])
    assert collector.stale_after_s == 2 * 300 + 4 * 30 + 60
    ok, detail = collector.health()
    assert ok and detail["cycles_completed"] == 0

    now[0] += collector.stale_after_s + 1
    ok, detail = collector.health()
    assert not ok and "no scrape progress" in detail["reason"]

    collector.run_cycle()
    ok, detail = collector.health()
    assert ok and detail["cycles_completed"] == 1
    assert detail["last_cycle"]["calls"] == 4 + 2 and detail["last_cycle"]["failed"] == 0


def test_run_forever_scrapes_immediately_then_stops_promptly():
    stop = threading.Event()
    api = FakeAPI()
    collector, _ = build(config(target(), scrape={"interval": "1h"}), api, stop_event=stop)
    loop = threading.Thread(target=collector.run_forever)
    loop.start()
    deadline = time.monotonic() + 5
    while collector.health()[1]["cycles_completed"] == 0 and time.monotonic() < deadline:
        time.sleep(0.01)
    stop.set()
    loop.join(timeout=5)
    assert not loop.is_alive()
    assert len(api.calls) == 4 + 2  # exactly one cycle: the next one is an hour away
