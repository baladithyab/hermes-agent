"""Named provider denial and credential fallback preserve the owning route authority."""

import json
from types import SimpleNamespace

import pytest

from hermes_cli import runtime_provider as rp


@pytest.fixture
def named_provider_config(tmp_path, monkeypatch):
    """Use the real config loader with a disposable named provider and fallback."""
    from hermes_cli import config as config_mod

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    config_mod._LOAD_CONFIG_CACHE.clear()
    config_mod._RAW_CONFIG_CACHE.clear()
    config = {
        "model": {"provider": "custom:cpa", "default": "primary-model"},
        "providers": {
            "cpa": {"name": "CPA Gateway", "api": "http://127.0.0.1:19181/v1",
                    "api_key": "generated-primary-key", "enabled": False},
        },
        "fallback_providers": [
            {"provider": "custom", "model": "fallback-model",
             "base_url": "http://127.0.0.1:19182/v1", "api_key": "generated-fallback-key"},
        ],
    }
    yield config, tmp_path / "config.yaml"
    config_mod._LOAD_CONFIG_CACHE.clear()
    config_mod._RAW_CONFIG_CACHE.clear()


@pytest.mark.parametrize("requested", [None, "cpa", " CUSTOM:CPA ", "CPA Gateway", "custom:cpa-gateway"])
@pytest.mark.parametrize("explicit_overrides", [False, True])
def test_disabled_named_provider_never_enters_fallback(named_provider_config, requested, explicit_overrides):
    config, config_path = named_provider_config
    config_path.write_text(json.dumps(config), encoding="utf-8")
    overrides = (
        {"explicit_base_url": "http://127.0.0.1:19183/v1", "explicit_api_key": "generated-override-key"}
        if explicit_overrides else {}
    )

    with pytest.raises(ValueError, match="disabled"):
        rp.resolve_runtime_with_fallback(config, requested=requested, **overrides)


@pytest.mark.parametrize("requested", [None, "custom:cpa", "cpa", "misspelled-provider"])
@pytest.mark.parametrize("explicit_overrides", [False, True])
def test_removed_or_unknown_named_provider_never_enters_fallback(named_provider_config, requested, explicit_overrides):
    config, config_path = named_provider_config
    config["providers"].clear()
    config_path.write_text(json.dumps(config), encoding="utf-8")
    overrides = (
        {"explicit_base_url": "http://127.0.0.1:19183/v1", "explicit_api_key": "generated-override-key"}
        if explicit_overrides else {}
    )

    with pytest.raises(rp.AuthError) as error:
        rp.resolve_runtime_with_fallback(config, requested=requested, **overrides)

    assert error.value.code == "invalid_provider"


def test_named_provider_without_endpoint_never_enters_fallback(named_provider_config):
    config, config_path = named_provider_config
    config["providers"]["cpa"] = {"enabled": True}
    config_path.write_text(json.dumps(config), encoding="utf-8")

    with pytest.raises(rp.AuthError) as error:
        rp.resolve_runtime_with_fallback(config, requested="custom:cpa")

    assert error.value.code == "invalid_provider"


@pytest.mark.parametrize("code", ["missing_api_key", "token_expired"])
def test_credential_failure_still_resolves_configured_fallback(named_provider_config, monkeypatch, code):
    config, config_path = named_provider_config
    config["model"]["provider"] = "deepseek"
    config_path.write_text(json.dumps(config), encoding="utf-8")
    monkeypatch.setattr(rp, "load_pool", lambda _provider: SimpleNamespace(has_credentials=lambda: False))

    def missing_credentials(_provider):
        raise rp.AuthError("Generated credential failure", provider="deepseek", code=code)

    monkeypatch.setattr(rp, "resolve_api_key_provider_credentials", missing_credentials)

    runtime, fallback = rp.resolve_runtime_with_fallback(config, requested="deepseek")

    assert fallback == config["fallback_providers"][0]
    assert runtime["base_url"] == fallback["base_url"]
    assert runtime["api_key"] == fallback["api_key"]


@pytest.mark.parametrize("requested", [None, "cpa", "custom:cpa", "custom:cpa-gateway"])
def test_enabled_named_command_provider_keeps_its_identity(named_provider_config, requested):
    from agent.command_token_source import CommandTokenSource

    config, config_path = named_provider_config
    config["providers"]["cpa"]["enabled"] = True
    config["providers"]["cpa"]["key_cmd"] = "generated-test-command"
    config_path.write_text(json.dumps(config), encoding="utf-8")

    runtime, fallback = rp.resolve_runtime_with_fallback(config, requested=requested)

    assert fallback is None
    assert runtime["provider"] == "custom"
    assert runtime["requested_provider"] == (requested or "custom:cpa")
    assert runtime["base_url"] == config["providers"]["cpa"]["api"]
    assert isinstance(runtime["api_key"], CommandTokenSource)


def test_disabled_builtin_provider_never_enters_fallback(named_provider_config):
    config, config_path = named_provider_config
    config["providers"]["deepseek"] = {"enabled": False}
    config_path.write_text(json.dumps(config), encoding="utf-8")

    with pytest.raises(ValueError, match="disabled"):
        rp.resolve_runtime_with_fallback(config, requested="deepseek")


