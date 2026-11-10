"""A zero-TTL command grant can authorize only one physical request."""

from types import SimpleNamespace

import pytest

from agent.command_token_source import CommandTokenSource, _mint


def test_zero_ttl_is_not_reused_between_requests(monkeypatch):
    grants = iter([("grant-one", 0.0), ("grant-two", 0.0)])
    monkeypatch.setattr("agent.command_token_source._mint", lambda *_: next(grants))
    source = CommandTokenSource("synthetic-helper", "synthetic-provider")
    assert source() == "grant-one"
    assert source() == "grant-two"


@pytest.mark.parametrize("ttl,expected", [(0, 0.0), (False, None), (True, None)])
def test_json_zero_is_a_grant_lifetime_and_booleans_are_not(monkeypatch, ttl, expected):
    import json

    monkeypatch.setattr("tools.environments.local.served_profile_child_env", lambda **_: {})
    monkeypatch.setattr("agent.command_token_source.subprocess.run", lambda *a, **kw: SimpleNamespace(
        returncode=0, stdout=json.dumps({"access_token": "synthetic-grant", "expires_in": ttl})))
    assert _mint("synthetic-helper", "synthetic-provider") == ("synthetic-grant", expected)
