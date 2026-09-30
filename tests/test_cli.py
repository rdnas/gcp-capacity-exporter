"""CLI wiring: --check-config, config and credential failures, --once."""

import google.auth
import pytest
import yaml
from google.auth.exceptions import DefaultCredentialsError

from conftest import FakeAPI, StaticTokens
from gcp_capacity_exporter import cli

CONFIG = {
    "project_id": "my-project",
    "scrape": {"interval": "10m"},
    "targets": [
        {
            "region": "us-east4",
            "machine_types": ["t2d-standard-8", "n2d-standard-8"],
            "sizes": [10, 50],
            "target_distribution_shapes": ["ANY", "BALANCED"],
            "zones": ["us-east4-a"],
        }
    ],
}


@pytest.fixture
def config_file(tmp_path):
    def write(data=CONFIG):
        path = tmp_path / "config.yaml"
        path.write_text(yaml.safe_dump(data))
        return str(path)

    return write


def test_check_config_prints_the_call_plan(config_file, capsys):
    assert cli.main(["--config", config_file(), "--check-config"]) == 0
    out = capsys.readouterr().out
    assert "config OK: project my-project" in out
    assert "12 advice/capacity + 4 advice/capacityHistory calls" in out
    assert "every 10m = ~96 calls/hour" in out
    assert "us-east4: 8 regional capacity (shapes: ANY, BALANCED), 4 zonal capacity, 4 history" in out


def test_invalid_config_exits_2(config_file, caplog):
    target = {**CONFIG["targets"][0], "target_shapes": ["ANY"]}
    assert cli.main(["--config", config_file({**CONFIG, "targets": [target]})]) == 2
    assert "did you mean 'target_distribution_shapes'" in caplog.text


def test_invalid_listen_override_exits_2(config_file):
    assert cli.main(["--config", config_file(), "--listen", "nope", "--check-config"]) == 2


def test_missing_credentials_are_fatal_at_startup(config_file, monkeypatch, caplog):
    def no_credentials(**kwargs):
        raise DefaultCredentialsError("Your default credentials were not found.")

    monkeypatch.setattr(google.auth, "default", no_credentials)
    assert cli.main(["--config", config_file(), "--once"]) == 1
    assert "no usable Application Default Credentials" in caplog.text


def test_once_runs_one_cycle_and_prints_the_metrics(config_file, monkeypatch, capsys):
    api = FakeAPI()
    api.close = lambda: None
    monkeypatch.setattr(cli.GoogleTokenSource, "from_adc", classmethod(lambda cls: StaticTokens()))
    monkeypatch.setattr(cli, "RequestsTransport", lambda pool_size: api)

    assert cli.main(["--config", config_file(), "--once"]) == 0
    out = capsys.readouterr().out
    assert len(api.calls) == 12 + 4
    assert (
        'gce_capacity_obtainability_score{machine_type="t2d-standard-8",region="us-east4",'
        'size="10",target_distribution_shape="ANY",zone=""} 0.8\n'
    ) in out
    assert 'gce_capacity_requests_total{api="capacity"} 12.0' in out
