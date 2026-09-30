"""HTTP endpoints: /metrics exposition end to end, /healthz, and routing."""

import json
import urllib.error
import urllib.request

import pytest
from prometheus_client import CollectorRegistry
from prometheus_client.parser import text_string_to_metric_families

from conftest import FakeAPI, capacity_doc, make_client
from gcp_capacity_exporter.collector import Collector
from gcp_capacity_exporter.config import parse_config
from gcp_capacity_exporter.metrics import ExporterMetrics
from gcp_capacity_exporter.server import MetricsServer


def fetch(url: str) -> tuple[int, dict, bytes]:
    try:
        with urllib.request.urlopen(url, timeout=5) as response:
            return response.status, dict(response.headers), response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, dict(exc.headers), exc.read()


@pytest.fixture
def serve():
    servers = []

    def start(registry, health=lambda: (True, {})):
        server = MetricsServer("127.0.0.1", 0, registry, health).start()
        servers.append(server)
        return f"http://127.0.0.1:{server.port}"

    yield start
    for server in servers:
        server.stop()


def test_metrics_endpoint_renders_a_scrape_cycle(serve):
    cfg = parse_config(
        {
            "project_id": "my-project",
            "targets": [
                {
                    "region": "us-east4",
                    "machine_types": ["t2d-standard-8"],
                    "sizes": [10],
                    "target_distribution_shapes": ["ANY", "BALANCED"],
                }
            ],
        }
    )
    metrics = ExporterMetrics(cfg.metric_prefix, cfg.signals)
    api = FakeAPI(capacity=lambda call: capacity_doc(0.9 if call["shape"] == "ANY" else 0.6))
    Collector(cfg, make_client(api, on_request=metrics.count_request), metrics).run_cycle()

    status, headers, body = fetch(serve(metrics.registry) + "/metrics")

    assert status == 200
    assert headers["Content-Type"].startswith("text/plain")
    text = body.decode()
    assert "# TYPE gce_capacity_obtainability_score gauge" in text
    families = {family.name: family for family in text_string_to_metric_families(text)}
    samples = {
        (sample.name, tuple(sorted(sample.labels.items()))): sample.value
        for family in families.values()
        for sample in family.samples
    }

    def value(name, **labels):
        return samples.get((name, tuple(sorted(labels.items()))))

    capacity = dict(region="us-east4", zone="", machine_type="t2d-standard-8", size="10")
    assert value("gce_capacity_obtainability_score", **capacity, target_distribution_shape="ANY") == 0.9
    assert value("gce_capacity_obtainability_score", **capacity, target_distribution_shape="BALANCED") == 0.6
    assert value("gce_capacity_estimated_uptime_seconds", **capacity, target_distribution_shape="ANY") == 3600
    price = dict(region="us-east4", machine_type="t2d-standard-8", purchase_option="spot")
    assert value("gce_capacity_spot_hourly_price", **price) == pytest.approx(0.05)
    assert value("gce_capacity_spot_monthly_price", **price) == pytest.approx(36.5)
    preemption = dict(region="us-east4", zone="", machine_type="t2d-standard-8")
    assert value("gce_capacity_spot_preemption_rate", **preemption) == pytest.approx(0.03)
    assert value("gce_capacity_requests_total", api="capacity") == 2
    assert value("gce_capacity_requests_total", api="capacity_history") == 1
    assert families["gce_capacity_requests"].type == "counter"
    assert families["gce_capacity_errors"].type == "counter"
    assert "gce_capacity_scrape_duration_seconds" in families


def test_healthz_reflects_the_health_check(serve):
    state = {"ok": True}

    def health():
        return (True, {"cycles_completed": 3}) if state["ok"] else (False, {"reason": "stuck"})

    base = serve(CollectorRegistry(), health)
    status, headers, body = fetch(base + "/healthz")
    assert status == 200 and headers["Content-Type"] == "application/json"
    assert json.loads(body) == {"status": "ok", "cycles_completed": 3}

    state["ok"] = False
    status, _, body = fetch(base + "/healthz")
    assert status == 503
    assert json.loads(body) == {"status": "unhealthy", "reason": "stuck"}


def test_index_and_unknown_paths(serve):
    base = serve(CollectorRegistry())
    status, _, body = fetch(base + "/")
    assert status == 200 and b'href="/metrics"' in body
    status, _, _ = fetch(base + "/nope")
    assert status == 404
