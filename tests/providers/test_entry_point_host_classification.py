"""Shared-group general plugins are classified from metadata before host-isolated discovery imports."""

from __future__ import annotations

from importlib import metadata
import json
import logging

import pytest

import providers


def _installed_entry_point(tmp_path, *, manifest=None, editable=False):
    site = tmp_path / "site"
    dist_dir = site / "sample_plugin-1.0.dist-info"
    dist_dir.mkdir(parents=True)
    (dist_dir / "METADATA").write_text("Name: sample-plugin\nVersion: 1.0\n", encoding="utf-8")
    (dist_dir / "entry_points.txt").write_text(
        "[hermes_agent.plugins]\nsample-plugin = sample_plugin:register\n", encoding="utf-8",
    )
    records = []
    if manifest is not None:
        root = tmp_path / "editable project" if editable else site / "sample_plugin"
        root.mkdir()
        (root / "plugin.yaml").write_text(manifest, encoding="utf-8")
        if editable:
            (dist_dir / "direct_url.json").write_text(json.dumps({
                "url": root.as_uri(), "dir_info": {"editable": True},
            }), encoding="utf-8")
        else:
            records.append("sample_plugin/plugin.yaml,,\n")
    (dist_dir / "RECORD").write_text("".join(records), encoding="utf-8")
    return metadata.PathDistribution(dist_dir).entry_points[0]


@pytest.fixture
def isolated_discovery(monkeypatch):
    from hermes_cli import plugin_isolation, plugins

    monkeypatch.setattr(plugins, "_get_enabled_plugins", lambda: {"sample-plugin"})
    monkeypatch.setattr(plugins, "_get_disabled_plugins", lambda: set())
    monkeypatch.setattr(plugin_isolation, "isolation_mode", lambda config=None: "host")
    attempts = []

    def forbidden_import(ep):
        attempts.append(ep.value)
        raise AssertionError("host discovery imported third-party code")

    monkeypatch.setattr(metadata.EntryPoint, "load", forbidden_import)
    return attempts


@pytest.mark.parametrize("editable", [False, True], ids=["installed-wheel", "editable"])
def test_declared_general_target_is_left_to_plugin_manager_without_import(
    tmp_path, monkeypatch, caplog, isolated_discovery, editable,
):
    ep = _installed_entry_point(tmp_path, editable=editable, manifest=(
        "name: sample-plugin\nentry_point: sample_plugin:register\n"
    ))
    monkeypatch.setattr(metadata, "entry_points", lambda: metadata.EntryPoints([ep]))

    with caplog.at_level(logging.DEBUG, logger="providers"):
        providers._discover_entry_point_providers()

    assert isolated_discovery == []
    assert "owned by PluginManager" in caplog.text
    assert not [record for record in caplog.records if record.name == "providers" and record.levelno >= logging.WARNING]


@pytest.mark.parametrize("manifest", [
    None,
    "name: sample-plugin\nkind: model-provider\nentry_point: sample_plugin:register\n",
    "name: other-plugin\nentry_point: sample_plugin:register\n",
    "name: sample-plugin\nentry_point: other_plugin:register\n",
    "name: sample-plugin\nkind: unknown-kind\nentry_point: sample_plugin:register\n",
], ids=["unknown", "model-provider", "different-name", "different-target", "unknown-kind"])
def test_provider_or_unidentified_target_retains_host_refusal(
    tmp_path, monkeypatch, caplog, isolated_discovery, manifest,
):
    ep = _installed_entry_point(tmp_path, manifest=manifest)
    monkeypatch.setattr(metadata, "entry_points", lambda: metadata.EntryPoints([ep]))

    with caplog.at_level(logging.WARNING, logger="providers"):
        providers._discover_entry_point_providers()

    assert isolated_discovery == []
    assert "sample-plugin" in caplog.text
    assert "plugins.isolation is 'host'" in caplog.text


@pytest.mark.parametrize("enabled,disabled", [
    ({"sample-plugin"}, {"sample-plugin"}),
    ({"some-other-plugin"}, set()),
    (set(), set()),
], ids=["disabled-wins", "not-enabled", "nothing-enabled"])
def test_enable_disable_gate_precedes_classification_and_import(
    tmp_path, monkeypatch, caplog, isolated_discovery, enabled, disabled,
):
    from hermes_cli import plugins

    ep = _installed_entry_point(tmp_path)
    monkeypatch.setattr(metadata, "entry_points", lambda: metadata.EntryPoints([ep]))
    monkeypatch.setattr(plugins, "_get_enabled_plugins", lambda: enabled)
    monkeypatch.setattr(plugins, "_get_disabled_plugins", lambda: disabled)

    def forbidden_metadata(_dist):
        raise AssertionError("disabled entry point was classified")

    monkeypatch.setattr(metadata.PathDistribution, "files", property(forbidden_metadata))
    with caplog.at_level(logging.WARNING, logger="providers"):
        providers._discover_entry_point_providers()

    assert isolated_discovery == []
    assert not [record for record in caplog.records if record.name == "providers" and record.levelno >= logging.WARNING]


def test_in_process_mode_retains_legacy_load_and_signature_dispatch(
    tmp_path, monkeypatch, isolated_discovery,
):
    from hermes_cli import plugin_isolation

    ep = _installed_entry_point(tmp_path, manifest=(
        "name: sample-plugin\nentry_point: sample_plugin:register\n"
    ))
    monkeypatch.setattr(metadata, "entry_points", lambda: metadata.EntryPoints([ep]))
    monkeypatch.setattr(plugin_isolation, "isolation_mode", lambda config=None: "in_process")
    calls = []
    monkeypatch.setattr(metadata.EntryPoint, "load", lambda _ep: lambda: calls.append("registered"))

    providers._discover_entry_point_providers()

    assert calls == ["registered"]
