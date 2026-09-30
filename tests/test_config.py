"""Config schema: defaults, validation, shape handling, and the shipped examples."""

from pathlib import Path

import pytest
import yaml

from gcp_capacity_exporter.config import (
    ConfigError,
    load_config,
    parse_config,
    parse_duration_s,
    parse_listen,
)

ROOT = Path(__file__).resolve().parents[1]


def target(**fields) -> dict:
    base = {"region": "us-east4", "machine_types": ["t2d-standard-8"], "sizes": [10]}
    base.update(fields)
    return base


def config(**fields) -> dict:
    base = {"project_id": "my-project", "targets": [target()]}
    base.update(fields)
    return base


def test_defaults():
    cfg = parse_config(config())
    assert cfg.project_id == "my-project"
    assert (cfg.listen_host, cfg.listen_port) == ("0.0.0.0", 9469)
    assert cfg.metric_prefix == "gce_capacity"
    assert (cfg.scrape.interval_s, cfg.scrape.timeout_s) == (300, 30)
    assert (cfg.scrape.workers, cfg.scrape.max_attempts) == (4, 4)
    assert cfg.hours_per_month == 730
    assert cfg.signals == ("price", "obtainability", "preemption_rate", "estimated_uptime")
    (only,) = cfg.targets
    assert only.target_distribution_shapes == ("ANY",)
    assert only.zones == ()


def test_shapes_are_validated_normalized_and_deduplicated():
    shapes = ["BALANCED", "any", "BALANCED", "ANY_SINGLE_ZONE"]
    cfg = parse_config(config(targets=[target(target_distribution_shapes=shapes)]))
    assert cfg.targets[0].target_distribution_shapes == ("BALANCED", "ANY", "ANY_SINGLE_ZONE")


@pytest.mark.parametrize(
    "shapes, message",
    [
        (["SINGLE_ZONE"], "unknown target distribution shape 'SINGLE_ZONE'"),
        ([], "must not be empty"),
        ("ANY", "expected a list"),
    ],
)
def test_invalid_shapes(shapes, message):
    with pytest.raises(ConfigError, match=message):
        parse_config(config(targets=[target(target_distribution_shapes=shapes)]))


def test_old_target_shapes_key_is_rejected_with_a_hint():
    with pytest.raises(ConfigError, match="did you mean 'target_distribution_shapes'"):
        parse_config(config(targets=[target(target_shapes=["ANY"])]))


def test_unknown_top_level_and_scrape_keys_are_rejected():
    with pytest.raises(ConfigError, match="unknown key 'intervall'"):
        parse_config(config(intervall="5m"))
    with pytest.raises(ConfigError, match="scrape: unknown key 'retries'"):
        parse_config(config(scrape={"retries": 3}))


@pytest.mark.parametrize("value", [0, -1, "730", 730.5, True])
def test_hours_per_month_must_be_an_integer_of_at_least_one(value):
    with pytest.raises(ConfigError, match="hours_per_month"):
        parse_config(config(hours_per_month=value))


def test_custom_hours_per_month():
    assert parse_config(config(hours_per_month=720)).hours_per_month == 720
    assert parse_config(config(hours_per_month=1)).hours_per_month == 1


def test_machine_types_sizes_and_zones_are_deduplicated():
    cfg = parse_config(
        config(
            targets=[
                target(
                    machine_types=["t2d-standard-8", "n2d-standard-8", "t2d-standard-8"],
                    sizes=[50, 10, 50],
                    zones=["us-east4-c", "us-east4-a", "us-east4-c"],
                )
            ]
        )
    )
    (only,) = cfg.targets
    assert only.machine_types == ("t2d-standard-8", "n2d-standard-8")
    assert only.sizes == (50, 10)
    assert only.zones == ("us-east4-c", "us-east4-a")


@pytest.mark.parametrize(
    "fields, message",
    [
        ({"region": None}, r"targets\[0\]\.region: required"),
        ({"region": "US-EAST4"}, "lowercase GCE name"),
        ({"machine_types": []}, r"targets\[0\]\.machine_types: must not be empty"),
        ({"machine_types": ["N2D standard"]}, "lowercase GCE name"),
        ({"sizes": [0]}, r"sizes\[0\]: must be >= 1"),
        ({"sizes": ["10"]}, r"sizes\[0\]: expected an integer"),
        ({"sizes": None}, r"targets\[0\]\.sizes: required"),
        ({"zones": ["us-central1-a"]}, "zone 'us-central1-a' is not in region 'us-east4'"),
        ({"zones": ["us-east4-"]}, "is not in region"),
    ],
)
def test_invalid_targets(fields, message):
    with pytest.raises(ConfigError, match=message):
        parse_config(config(targets=[target(**fields)]))


@pytest.mark.parametrize("targets", [None, [], "us-east4", ["us-east4"]])
def test_targets_must_be_a_non_empty_list_of_mappings(targets):
    with pytest.raises(ConfigError, match="targets"):
        parse_config(config(targets=targets))


@pytest.mark.parametrize("project_id", [None, "", "  ", "my project", "projects/x"])
def test_project_id_is_required_and_plain(project_id):
    with pytest.raises(ConfigError, match="project_id"):
        parse_config(config(project_id=project_id))


def test_sizes_are_optional_without_capacity_signals():
    cfg = parse_config(
        config(signals=["price", "preemption_rate"], targets=[target(sizes=None)])
    )
    assert cfg.targets[0].sizes == ()
    assert cfg.signals == ("price", "preemption_rate")


@pytest.mark.parametrize(
    "signals, message",
    [(["bogus"], "unknown signal 'bogus'"), ([], "must not be empty"), ("price", "expected a list")],
)
def test_invalid_signals(signals, message):
    with pytest.raises(ConfigError, match=message):
        parse_config(config(signals=signals))


def test_signals_are_deduplicated_in_canonical_order():
    cfg = parse_config(config(signals=["estimated_uptime", "price", "price"]))
    assert cfg.signals == ("price", "estimated_uptime")


def test_scrape_durations():
    cfg = parse_config(config(scrape={"interval": "1m30s", "timeout": "10s", "workers": 8}))
    assert (cfg.scrape.interval_s, cfg.scrape.timeout_s, cfg.scrape.workers) == (90, 10, 8)
    assert parse_duration_s("1d", "x") == 86400
    assert parse_duration_s(" 2h ", "x") == 7200


@pytest.mark.parametrize(
    "scrape, message",
    [
        ({"interval": 300}, "expected a duration string"),
        ({"interval": "5x"}, "invalid duration"),
        ({"interval": "5 m"}, "invalid duration"),
        ({"timeout": "0s"}, "greater than zero"),
        ({"workers": 0}, "scrape.workers: must be >= 1"),
        ({"max_attempts": 0}, "scrape.max_attempts: must be >= 1"),
        ("5m", "scrape: expected a mapping"),
    ],
)
def test_invalid_scrape(scrape, message):
    with pytest.raises(ConfigError, match=message):
        parse_config(config(scrape=scrape))


@pytest.mark.parametrize(
    "value, expected",
    [
        ("0.0.0.0:9469", ("0.0.0.0", 9469)),
        (":9469", ("0.0.0.0", 9469)),
        ("127.0.0.1:8080", ("127.0.0.1", 8080)),
        ("[::]:9469", ("::", 9469)),
    ],
)
def test_parse_listen(value, expected):
    assert parse_listen(value) == expected


@pytest.mark.parametrize("value", ["9469", "localhost", "host:", "host:0", "host:70000", 9469])
def test_parse_listen_rejects(value):
    with pytest.raises(ConfigError, match="listen"):
        parse_listen(value)


def test_metric_prefix():
    assert parse_config(config(metric_prefix="capacity_advisor_")).metric_prefix == "capacity_advisor"
    for bad in ("my-prefix", "1abc", "_"):
        with pytest.raises(ConfigError, match="metric_prefix"):
            parse_config(config(metric_prefix=bad))


def test_load_config_errors(tmp_path):
    with pytest.raises(ConfigError, match="cannot read"):
        load_config(tmp_path / "missing.yaml")
    bad_yaml = tmp_path / "bad.yaml"
    bad_yaml.write_text("targets: [unclosed\n")
    with pytest.raises(ConfigError, match="invalid YAML"):
        load_config(bad_yaml)
    not_a_mapping = tmp_path / "list.yaml"
    not_a_mapping.write_text("- project_id: x\n")
    with pytest.raises(ConfigError, match="mapping at the top level"):
        load_config(not_a_mapping)


def test_example_config_is_valid():
    cfg = load_config(ROOT / "config.example.yaml")
    (only,) = cfg.targets
    assert only.target_distribution_shapes == ("ANY", "BALANCED")
    assert only.sizes == (10, 50)


def test_chart_default_config_is_valid_once_project_and_targets_are_set():
    values = yaml.safe_load((ROOT / "chart" / "values.yaml").read_text())["config"]
    example = yaml.safe_load((ROOT / "chart" / "ci" / "example-values.yaml").read_text())["config"]
    cfg = parse_config({**values, **example})
    assert cfg.metric_prefix == "gce_capacity"
    assert cfg.targets
