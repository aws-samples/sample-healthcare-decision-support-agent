"""Isolate local deployment configuration and evidence capture in offline tests."""

import pytest


@pytest.fixture(autouse=True)
def isolated_runtime_configuration(monkeypatch, tmp_path):
    from medical_nudging import config
    from medical_nudging.tools.raw_resource_channel import reset_default_channel

    monkeypatch.setattr(config, "_CONFIG_FILE", tmp_path / "unconfigured-settings.yaml")
    config.load_config.cache_clear()
    reset_default_channel()
    yield
    reset_default_channel()
    config.load_config.cache_clear()
