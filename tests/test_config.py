"""Tests for the one configuration seam: precedence, override, observability switch."""

import os
from unittest.mock import patch

import pytest

from medical_nudging import config


def _write_settings(path, text):
    path.write_text(text)
    config._CONFIG_FILE = path
    config.load_config.cache_clear()


def test_get_value_precedence_env_then_yaml_then_default(tmp_path, monkeypatch):
    _write_settings(tmp_path / "settings.yaml", "agent:\n  search_backend: ripgrep\n")

    assert config.get_value("agent.search_backend", "SEARCH_BACKEND", "auto") == "ripgrep"
    monkeypatch.setenv("SEARCH_BACKEND", "opensearch")
    assert config.get_value("agent.search_backend", "SEARCH_BACKEND", "auto") == "opensearch"
    assert config.get_value("agent.missing", default="fallback") == "fallback"


def test_override_merges_sections_and_is_visible_to_accessors(tmp_path):
    _write_settings(tmp_path / "settings.yaml", "model:\n  model_id: base\n  effort: low\n")

    config.override({"model": {"model_id": "experiment"}, "opensearch_index": "corpus-v3"})

    model = config.get_model_config()
    assert model["model_id"] == "experiment"
    assert model["effort"] == "low"
    assert config.get_opensearch_config()["opensearch_index"] == "corpus-v3"


def test_override_replace_drops_unlisted_keys(tmp_path):
    _write_settings(tmp_path / "settings.yaml", "model:\n  model_id: base\n  effort: low\n")

    config.override({"model": {"model_id": "experiment"}}, replace=True)

    assert config.load_config()["model"] == {"model_id": "experiment"}


def test_observability_enabled_parses_one_switch():
    with patch.dict(os.environ, {}, clear=True):
        assert config.observability_enabled() is False
    with patch.dict(os.environ, {"OBSERVABILITY_ENABLED": " TRUE "}, clear=True):
        assert config.observability_enabled() is True
    with patch.dict(os.environ, {"OBSERVABILITY_ENABLED": "false"}, clear=True):
        assert config.observability_enabled() is False


def test_load_experiment_config_defaults_name_to_stem(tmp_path):
    path = tmp_path / "quick_check.yaml"
    path.write_text("model:\n  model_id: exp-model\ninference:\n  samples: 1\n")

    experiment = config.load_experiment_config(path)

    assert experiment["experiment"]["name"] == "quick_check"
    assert (
        config.experiment_output_dir(experiment) == config.EXPERIMENT_RESULTS_ROOT / "quick_check"
    )


def test_load_experiment_config_rejects_non_mapping(tmp_path):
    path = tmp_path / "list.yaml"
    path.write_text("- not\n- a mapping\n")

    with pytest.raises(ValueError, match="mapping"):
        config.load_experiment_config(path)


def test_load_experiment_config_reports_malformed_yaml_as_value_error(tmp_path):
    path = tmp_path / "broken.yaml"
    path.write_text("model: [unclosed\n")

    with pytest.raises(ValueError, match="invalid YAML"):
        config.load_experiment_config(path)


def test_apply_experiment_config_routes_settings_and_keeps_configured_endpoint(tmp_path):
    _write_settings(
        tmp_path / "settings.yaml",
        "model:\n  model_id: base\n  effort: low\n"
        "fhir_api:\n  enabled: false\n  datastore_endpoint: https://configured.example/r4/\n",
    )
    experiment = {
        "experiment": {"name": "arm"},
        "model": {"model_id": "exp-model"},
        "agent": {"max_nudges": 3},
        "opensearch_index": "corpus-v3",
        "inference": {"samples": 2, "fhir_api": {"datastore_endpoint": "", "samples": 2}},
    }

    config.apply_experiment_config(experiment)

    assert config.get_model_config()["model_id"] == "exp-model"
    assert config.get_model_config()["effort"] == "low"
    assert config.get_agent_config()["max_nudges"] == 3
    assert config.get_opensearch_config()["opensearch_index"] == "corpus-v3"
    fhir = config.get_fhir_config()
    assert fhir["enabled"] is True
    assert fhir["datastore_endpoint"] == "https://configured.example/r4/"


def test_apply_experiment_config_explicit_endpoint_wins(tmp_path):
    _write_settings(tmp_path / "settings.yaml", "fhir_api:\n  enabled: false\n")

    config.apply_experiment_config(
        {"inference": {"fhir_api": {"datastore_endpoint": "https://arm.example/r4/"}}}
    )

    assert config.get_fhir_config()["datastore_endpoint"] == "https://arm.example/r4/"


@pytest.mark.parametrize(
    ("endpoint", "region"),
    [
        ("https://healthlake.us-east-1.amazonaws.com/datastore/abc123/r4/", "us-east-1"),
        ("https://abc123.us-east-1.aoss.amazonaws.com", "us-east-1"),
        ("https://healthlake.us-gov-west-1.amazonaws.com/datastore/x/r4/", "us-gov-west-1"),
        ("https://fhir.example.org/r4", None),
        ("", None),
        (None, None),
    ],
)
def test_aws_region_from_endpoint(endpoint, region):
    assert config.aws_region_from_endpoint(endpoint) == region


def test_fhir_region_comes_from_the_endpoint_not_the_shell(tmp_path, monkeypatch):
    _write_settings(
        tmp_path / "settings.yaml",
        "aws_region: us-east-1\n"
        "fhir_api:\n  enabled: true\n"
        "  datastore_endpoint: https://healthlake.us-east-1.amazonaws.com/datastore/abc/r4/\n",
    )
    monkeypatch.setenv("AWS_REGION", "us-west-2")

    assert config.get_fhir_config()["region"] == "us-east-1"


def test_fhir_region_falls_back_to_env_for_endpoints_without_a_region(tmp_path, monkeypatch):
    _write_settings(
        tmp_path / "settings.yaml",
        "fhir_api:\n  enabled: true\n  datastore_endpoint: https://fhir.example.org/r4\n",
    )
    monkeypatch.setenv("AWS_REGION", "eu-west-1")

    assert config.get_fhir_config()["region"] == "eu-west-1"
