"""Backend model selection stays in core and retains the canonical selector's whole route."""

from types import SimpleNamespace

import pytest

from tests.tools.test_delegation_route_fields import LOCAL, _spawn


def test_selector_retains_alias_endpoint_and_the_same_callable_credential(monkeypatch):
    calls = []

    def token():
        calls.append('mint')
        return 'generated-scoped-token'

    selected = SimpleNamespace(
        success=True, new_model='canonical-model', target_provider='custom:chosen',
        base_url='http://127.0.0.1:9032/alias', api_key=token, api_mode='anthropic_messages',
        request_overrides={'extra_body': {'alias_setting': True}})
    monkeypatch.setattr('hermes_cli.model_switch.switch_model', lambda **kwargs: selected)
    monkeypatch.setattr('hermes_cli.config.load_config_readonly', dict)
    monkeypatch.setattr('tools.delegate_tool._resolve_delegation_credentials', lambda route, parent: {
        **LOCAL, 'request_overrides': {'extra_body': {'provider_setting': True}}})
    result, (child,), _ = _spawn([
        {'provider': 'custom:chosen', 'model': 'alias', 'model_selection': True},
    ])
    assert result == {'status': 'built'}
    assert child['model'] == 'canonical-model' and child['provider'] == 'custom:chosen'
    assert child['base_url'] == selected.base_url and child['api_mode'] == selected.api_mode
    assert child['api_key'] is token and calls == []
    assert child['request_overrides']['extra_body'] == {'provider_setting': True, 'alias_setting': True}


@pytest.mark.parametrize('route', [
    {'provider': 'custom:chosen', 'model': 'selected', 'model_selection': 'true'},
    {'provider': 'custom:chosen', 'model': 'selected', 'model_selection': {}},
    {'provider': 'custom:chosen', 'model': 'selected', 'model_selection': {'allowed': None}},
    {'model': 'selected', 'model_selection': True},
    {'provider': 'custom:chosen', 'model': 'selected', 'model_selection': True, 'api_key': 'generated-injection'},
    {'provider': 'custom:chosen', 'model': 'selected', 'model_selection': True, 'base_url': 'http://127.0.0.1:9'},
])
def test_invalid_selection_route_is_refused_before_the_selector(monkeypatch, route):
    def unexpected(**kwargs):
        raise AssertionError('invalid route reached the selector')

    monkeypatch.setattr('hermes_cli.model_switch.switch_model', unexpected)
    result, children, _ = _spawn([route])
    assert result.get('error') and children == []


def test_selector_denial_never_builds_a_child_or_resolves_another_route(monkeypatch):
    monkeypatch.setattr('hermes_cli.config.load_config_readonly', dict)
    monkeypatch.setattr('hermes_cli.model_switch.switch_model', lambda **kwargs: SimpleNamespace(
        success=False, error_message='selected model is denied by its provider'))
    result, children, _ = _spawn([
        {'provider': 'custom:chosen', 'model': 'denied', 'model_selection': True},
    ])
    assert 'denied by its provider' in result['error'] and children == []


def test_alias_expansion_cannot_bypass_an_exact_selection_policy(monkeypatch):
    monkeypatch.setattr('hermes_cli.config.load_config_readonly', dict)
    monkeypatch.setattr('hermes_cli.model_switch.switch_model', lambda **kwargs: SimpleNamespace(
        success=True, target_provider='custom:chosen', new_model='disallowed-canonical-model'))
    result, children, _ = _spawn([{
        'provider': 'custom:chosen', 'model': 'allowed-alias',
        'model_selection': {'allowed': [{'provider': 'custom:chosen', 'model': 'allowed-alias'}]},
    }])
    assert 'canonical provider/model pair' in result['error'] and children == []
