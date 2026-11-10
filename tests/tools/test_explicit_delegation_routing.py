"""Explicit route policy through real plugin discovery and core credential routing."""
import copy
import json
from pathlib import Path
import shutil
from types import SimpleNamespace

import hermes_yaml as yaml
import pytest

from hermes_cli import plugins as plugins_mod
from hermes_cli.plugins import PluginManager

pytestmark = pytest.mark.platforms('posix')
_ROOT = Path(__file__).resolve().parents[2]
_A = {'provider': 'custom:route-a', 'model': 'shared-model'}
_B = {'provider': 'custom:route-b', 'model': 'shared-model'}


@pytest.fixture
def environment(tmp_path, monkeypatch):
    home = tmp_path / '.hermes'
    home.mkdir()
    monkeypatch.setenv('HOME', str(tmp_path / 'os-home'))
    monkeypatch.setenv('HERMES_HOME', str(home))
    cfg = {
        'plugins': {'isolation': 'host', 'enabled': ['explicit-routing']},
        'providers': {
            'route-a': {'name': 'Route A', 'base_url': 'http://127.0.0.1:9011/v1',
                        'api_key': 'generated-route-a', 'models': {'shared-model': {}, 'a-only': {}}},
            'route-b': {'name': 'Route B', 'base_url': 'http://127.0.0.1:9012/v1',
                        'api_key': 'generated-route-b', 'models': {'shared-model': {}}},
        },
        'delegation': {'backend': 'explicit-routing', 'base_url': 'http://127.0.0.1:9/wrong',
                       'api_key': 'generated-wrong-delegation-key',
                       'request_overrides': {'extra_headers': {'X-Wrong-Route': 'never-inherit'}},
                       'backends': {'explicit-routing': {'allowed': [_A, _B]}}},
    }
    managers = []

    def save():
        (home / 'config.yaml').write_text(yaml.safe_dump(cfg), encoding='utf-8')

    def load(placement='bundled'):
        bundled = tmp_path / 'bundled'
        category = bundled / 'delegation'
        category.mkdir(parents=True)
        (category / 'default').symlink_to(_ROOT / 'plugins/delegation/default', target_is_directory=True)
        if placement == 'host':
            destination = home / 'plugins/explicit-routing'
            shutil.copytree(_ROOT / 'plugins/delegation/explicit-routing', destination)
        else:
            (category / 'explicit-routing').symlink_to(
                _ROOT / 'plugins/delegation/explicit-routing', target_is_directory=True)
        monkeypatch.setattr(plugins_mod, 'get_bundled_plugins_dir', lambda: bundled)
        save()
        manager = PluginManager()
        manager.discover_and_load()
        managers.append(manager)
        return manager

    yield SimpleNamespace(home=home, cfg=cfg, save=save, load=load)
    for manager in managers:
        manager.unload()
        manager._plugin_host().shutdown()


def _parent():
    from tests.tools.test_delegation_route_fields import _parent as parent
    result = parent({'extra_headers': {'X-Parent-Route': 'never-inherit'}})
    result.base_url, result.api_key = 'http://127.0.0.1:9/parent', 'generated-wrong-parent-key'
    result.session_id = 'generated-explicit-routing-session'
    return result


def _routes(environment, tasks, parent=None):
    from tools.delegate_tool_routes import resolve_task_routes
    routes, error = resolve_task_routes(tasks, environment.cfg['delegation'], parent or _parent())
    assert error is None
    return routes


@pytest.mark.parametrize('placement', ['bundled', 'host'])
def test_discovery_schema_is_static_and_explicit_routes_use_the_owning_provider(environment, placement):
    from agent.delegation_registry import get_active_provider
    from hermes_cli.plugin_host import is_hosted_object
    from tools.delegate_tool_routes import schema_extension

    manager = environment.load(placement)
    cfg = environment.cfg['delegation']
    backend = get_active_provider(cfg)
    assert backend.name == 'explicit-routing'
    assert is_hosted_object(backend) is (placement == 'host')
    before = json.dumps(schema_extension(cfg), sort_keys=True)
    params = schema_extension(cfg)[0]
    assert set(params) == {'route'}
    assert not ({'model', 'provider', 'api_key', 'base_url'} & set(params))
    assert params['route']['additionalProperties'] is False
    routes = _routes(environment, [{'goal': 'first', 'route': _A}, {'goal': 'second', 'route': _B}])
    assert [route.creds['provider'] for route in routes] == ['custom:route-a', 'custom:route-b']
    assert [route.creds['base_url'] for route in routes] == ['http://127.0.0.1:9011/v1', 'http://127.0.0.1:9012/v1']
    assert [route.creds['api_key'] for route in routes] == ['generated-route-a', 'generated-route-b']
    assert all(route.creds['request_overrides'] == {} for route in routes)
    assert json.dumps(schema_extension(cfg), sort_keys=True) == before
    assert environment.cfg['plugins']['isolation'] == 'host'
    assert not any(loaded.error for loaded in manager._plugins.values())


@pytest.mark.parametrize('selection', [
    {'provider': 'custom:route-b', 'model': 'a-only'},
    {'provider': 'custom:missing', 'model': 'shared-model'},
    {**_A, 'api_key': 'generated-injection'}, {**_A, 'base_url': 'http://127.0.0.1:9/injected'},
    {**_A, 'profile': 'another'}, {**_A, 'request_overrides': {}},
    {**_A, 'fallback_providers': []}, {**_A, 'reasoning_effort': 'super'},
    {**_A, 'reasoning_effort': {'effort': 'high'}}, {**_A, 'reasoning_effort': False},
    {**_A, 'reasoning_effort': 'ultra'}, {**_A, 'model': ''}, {**_A, 'provider': ''},
    {**_A, 'model': 'shared-model --provider route-b'}, {'provider': 'custom:route-a'},
    [], '', 4, False,
])
def test_non_null_invalid_or_unauthorized_selection_refuses_only_that_task(environment, selection):
    environment.load()
    refused, good = _routes(environment, [{'goal': 'refused', 'route': selection}, {'goal': 'valid', 'route': _B}])
    assert refused.error
    assert not good.error and good.creds['provider'] == _B['provider']


@pytest.mark.parametrize('change', ['missing-policy', 'disabled', 'unknown-model', 'unknown-provider'])
def test_policy_and_current_provider_inventory_both_gate_a_choice(environment, change):
    environment.load()
    if change == 'missing-policy':
        environment.cfg['delegation']['backends'] = {}
    elif change == 'disabled':
        environment.cfg['providers']['route-a']['enabled'] = False
    elif change == 'unknown-model':
        environment.cfg['delegation']['backends']['explicit-routing']['allowed'].append(
            {'provider': _A['provider'], 'model': 'unpublished-model'})
    else:
        environment.cfg['delegation']['backends']['explicit-routing']['allowed'].append(
            {'provider': 'custom:missing', 'model': 'shared-model'})
    environment.save()
    selection = {**_A, 'model': 'unpublished-model'} if change == 'unknown-model' else _A
    if change == 'unknown-provider':
        selection = {'provider': 'custom:missing', 'model': 'shared-model'}
    assert _routes(environment, [{'goal': 'must refuse', 'route': selection}])[0].error


@pytest.mark.parametrize('fallback', [
    [_B], [], [{**_B, 'reasoning_effort': 'low'}],
])
def test_allowed_configured_fallback_chain_is_preserved(environment, fallback):
    from tools.delegate_tool_config import _resolve_child_fallback_chain
    environment.cfg['delegation']['fallback_providers'] = fallback
    environment.load()
    route, = _routes(environment, [{'goal': 'allowed fallback', 'route': _A}])
    assert not route.error and route.routing_cfg['fallback_providers'] == fallback
    assert _resolve_child_fallback_chain(_parent(), route.routing_cfg, pinned=True) == (fallback or None)


@pytest.mark.parametrize('fallback', [
    [{'provider': 'custom:route-b', 'model': 'not-allowed'}],
    [{**_B, 'api_key': 'generated-injection'}], [{**_B, 'base_url': 'http://127.0.0.1:9/injected'}],
    [{**_B, 'key_env': 'OTHER_ROUTE_KEY'}], [{'provider': _B['provider']}], [None], {'provider': _B['provider']},
])
def test_unsafe_configured_fallback_policy_is_refused_without_silent_dropping(environment, fallback):
    environment.cfg['delegation']['fallback_providers'] = fallback
    environment.load()
    assert _routes(environment, [{'goal': 'unsafe fallback', 'route': _A}])[0].error


def test_absent_and_null_routes_keep_ordinary_delegation_and_pinned_backend(environment):
    from tools.delegate_tool_routes import schema_extension
    environment.load()
    parent = _parent()
    parent._delegation_backend = 'explicit-routing'
    before = copy.deepcopy(schema_extension(environment.cfg['delegation']))
    cfg = environment.cfg['delegation']
    cfg['backend'] = 'default'  # an already-built session keeps its selected backend
    inherited, null, explicit = _routes(environment, [
        {'goal': 'ordinary'}, {'goal': 'ordinary null', 'route': None}, {'goal': 'explicit', 'route': _A},
    ], parent)
    assert inherited.creds == null.creds
    assert inherited.creds['base_url'] == cfg['base_url']
    assert inherited.creds['api_key'] == cfg['api_key']
    assert explicit.backend == 'explicit-routing' and explicit.creds['provider'] == _A['provider']
    assert schema_extension({**cfg, 'backend': 'explicit-routing'}) == before


def test_callable_provider_credentials_remain_callable_and_schema_never_mints(environment, monkeypatch):
    from tools.delegate_tool_routes import schema_extension
    from tools import delegate_tool as dt
    from tests.tools.test_delegation_route_fields import _construct
    marker = environment.home / 'must-not-exist'
    provider = environment.cfg['providers']['route-a']
    provider.pop('api_key')
    provider['key_cmd'] = f"printf generated-token > {marker.as_posix()}"
    environment.load()
    before = schema_extension(environment.cfg['delegation'])
    route, = _routes(environment, [{'goal': 'callable credential', 'route': _A}])
    assert not route.error and callable(route.creds['api_key'])
    assert route.creds['base_url'] == provider['base_url']
    assert schema_extension(environment.cfg['delegation']) == before
    built = []

    def construct(**kwargs):
        built.append(kwargs)
        return _construct(**kwargs)

    monkeypatch.setattr('run_agent.AIAgent', construct)
    monkeypatch.setattr(dt, '_run_batch', lambda batch, background: json.dumps({'built': len(batch.children)}))
    result = json.loads(dt.delegate_task(tasks=[{'goal': 'preserve callable', 'route': _A}], parent_agent=_parent()))
    assert result == {'built': 1} and callable(built[0]['api_key'])
    assert not marker.exists()


def test_real_delegate_builder_preserves_same_model_effort_changes_and_owned_credentials(environment, monkeypatch):
    from tools import delegate_tool as dt
    from tests.tools.test_delegation_route_fields import _construct

    environment.load()
    built = []

    def construct(**kwargs):
        built.append(kwargs)
        return _construct(**kwargs)

    monkeypatch.setattr('run_agent.AIAgent', construct)
    monkeypatch.setattr(dt, '_run_batch', lambda batch, background: json.dumps({'built': len(batch.children)}))
    result = json.loads(dt.delegate_task(tasks=[
        {'goal': 'same model low', 'route': {**_A, 'reasoning_effort': 'low'}},
        {'goal': 'same model high', 'route': {**_A, 'reasoning_effort': 'high'}},
        {'goal': 'other provider', 'route': _B},
    ], parent_agent=_parent()))
    assert result == {'built': 3}
    assert [row['model'] for row in built] == ['shared-model'] * 3
    assert [row['provider'] for row in built] == [_A['provider'], _A['provider'], _B['provider']]
    assert [row['api_key'] for row in built] == ['generated-route-a', 'generated-route-a', 'generated-route-b']
    assert [row['reasoning_config']['effort'] for row in built[:2]] == ['low', 'high']
    assert all(row['base_url'] != environment.cfg['delegation']['base_url'] for row in built)
    assert all(row.get('request_overrides') == {} for row in built)
    assert all(row['fallback_model'] is None for row in built)
