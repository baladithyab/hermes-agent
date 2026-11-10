"""Explicit route policy through real plugin discovery and core credential routing."""
import copy
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import shlex
import shutil
import sys
import threading
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
    registry = {'synthetic': {'id': 'synthetic', 'models': {
        'seed-chat': {'id': 'seed-chat', 'tool_call': True}}}}
    (home / 'models_dev_cache.json').write_text(json.dumps(registry), encoding='utf-8')
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            requests.append((self.path, self.headers.get('Authorization')))
            value = registry if self.path == '/models-dev.json' else {
                'object': 'list', 'data': [{'id': 'shared-model'}, {'id': 'a-only'}]}
            body = json.dumps(value).encode()
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    origin = f'http://127.0.0.1:{server.server_port}'
    cfg = {
        'models_dev': {'url': origin + '/models-dev.json'},
        'plugins': {'isolation': 'host', 'enabled': ['explicit-routing']},
        'providers': {
            'route-a': {'name': 'Route A', 'base_url': origin + '/a/v1', 'api_mode': 'chat_completions',
                        'api_key': 'generated-route-a', 'models': {}},
            'route-b': {'name': 'Route B', 'base_url': origin + '/b/v1', 'api_mode': 'chat_completions',
                        'api_key': 'generated-route-b', 'models': {}},
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

    yield SimpleNamespace(home=home, cfg=cfg, save=save, load=load, origin=origin, requests=requests)
    for manager in managers:
        manager.unload()
        manager._plugin_host().shutdown()
    server.shutdown()
    server.server_close()
    thread.join(timeout=5)


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
    assert not any(route.error for route in routes)
    assert [route.creds['provider'] for route in routes] == ['custom:route-a', 'custom:route-b']
    assert [route.creds['base_url'] for route in routes] == [environment.origin + '/a/v1', environment.origin + '/b/v1']
    assert [route.creds['api_key'] for route in routes] == ['generated-route-a', 'generated-route-b']
    assert all(route.creds['request_overrides'] == {} for route in routes)
    probes = [(path, key) for path, key in environment.requests if path.endswith('/models')]
    assert ('/a/v1/models', 'Bearer generated-route-a') in probes
    assert ('/b/v1/models', 'Bearer generated-route-b') in probes
    assert not any(key == 'Bearer generated-wrong-parent-key' or '/wrong' in path for path, key in probes)
    assert json.dumps(schema_extension(cfg), sort_keys=True) == before
    assert environment.cfg['plugins']['isolation'] == 'host'
    assert not any(loaded.error for loaded in manager._plugins.values())
    assert _routes(environment, [{'goal': 'no internal selector input', 'model_selection': True}])[0].error


@pytest.mark.parametrize('selection', [
    {'provider': 'custom:route-b', 'model': 'a-only'},
    {'provider': 'custom:missing', 'model': 'shared-model'},
    {**_A, 'api_key': 'generated-injection'}, {**_A, 'key_env': 'OTHER_ROUTE_KEY'},
    {**_A, 'base_url': 'http://127.0.0.1:9/injected'}, {**_A, 'model_selection': True},
    {**_A, 'profile': 'another'}, {**_A, 'request_overrides': {}},
    {**_A, 'fallback_providers': []}, {**_A, 'reasoning_effort': 'super'},
    {**_A, 'reasoning_effort': {'effort': 'high'}}, {**_A, 'reasoning_effort': False},
    {**_A, 'reasoning_effort': 'ultra'}, {**_A, 'model': ''}, {**_A, 'model': ' '},
    {**_A, 'model': 2}, {**_A, 'provider': ''}, {'provider': 'custom:route-a'},
    [], '', 4, False,
])
def test_non_null_invalid_or_unauthorized_selection_refuses_only_that_task(environment, selection):
    environment.load()
    refused, good = _routes(environment, [{'goal': 'refused', 'route': selection}, {'goal': 'valid', 'route': _B}])
    assert refused.error
    assert not good.error and good.creds['provider'] == _B['provider']


@pytest.mark.parametrize('model', ['unpublished-model', 'My Custom Model', 'x' * 300])
@pytest.mark.parametrize('placement', ['bundled', 'host'])
def test_absent_extra_allowlist_preserves_unlisted_custom_models(environment, model, placement):
    environment.cfg['delegation']['backends'] = {}
    environment.load(placement)
    route, = _routes(environment, [{'goal': 'typed custom model', 'route': {**_A, 'model': model}}])
    assert not route.error and route.creds['model'] == model
    assert environment.cfg['providers']['route-a']['models'] == {}


@pytest.mark.parametrize('policy', [[], None, {}, 'invalid', [None], [{**_A, 'base_url': 'forbidden'}]])
def test_configured_empty_or_malformed_allowlist_refuses(environment, policy):
    environment.cfg['delegation']['backends']['explicit-routing']['allowed'] = policy
    environment.load()
    assert _routes(environment, [{'goal': 'refuse policy', 'route': _A}])[0].error


@pytest.mark.parametrize('backends', [None, [], {'explicit-routing': None}, {'explicit-routing': []}])
def test_malformed_backend_policy_block_refuses(environment, backends):
    environment.cfg['delegation']['backends'] = backends
    environment.load()
    assert _routes(environment, [{'goal': 'refuse malformed settings', 'route': _A}])[0].error


@pytest.mark.parametrize('change', ['disabled', 'unknown-provider', 'non-chat'])
def test_existing_provider_and_canonical_model_gates_remain(environment, change):
    environment.cfg['delegation']['backends'] = {}
    if change == 'disabled':
        environment.cfg['providers']['route-a']['enabled'] = False
    environment.load()
    selection = {'provider': 'custom:missing', 'model': 'shared-model'} if change == 'unknown-provider' else _A
    if change == 'non-chat':
        selection = {**_A, 'model': 'synthetic-image-model'}
    assert _routes(environment, [{'goal': 'must refuse', 'route': selection}])[0].error
    if change == 'disabled':
        assert not any(path.startswith('/a/') for path, _ in environment.requests)


@pytest.mark.parametrize('fallback', [
    [_B], [], [{**_B, 'reasoning_effort': 'low'}],
    [{**_B, 'api_key': 'generated-operator-key', 'base_url': 'http://127.0.0.1:9/operator/v1'}],
    [{**_B, 'key_env': 'ROUTE_B_KEY', 'request_overrides': {'extra_body': {'operator': True}}}], _B,
])
def test_trusted_configured_fallback_chain_is_preserved(environment, fallback):
    from hermes_cli.fallback_config import get_fallback_chain
    from tools.delegate_tool_config import _resolve_child_fallback_chain
    environment.cfg['delegation']['fallback_providers'] = fallback
    environment.load()
    route, = _routes(environment, [{'goal': 'allowed fallback', 'route': _A}])
    expected = get_fallback_chain({'fallback_providers': fallback}) or []
    assert not route.error and route.routing_cfg['fallback_providers'] == expected
    assert _resolve_child_fallback_chain(_parent(), route.routing_cfg, pinned=True) == (expected or None)


@pytest.mark.parametrize('fallback', [
    [{'provider': 'custom:route-b', 'model': 'not-allowed'}],
    [{**_B, 'reasoning_effort': 'super'}], [{'provider': _B['provider']}], [None],
    {'provider': _B['provider']}, 'invalid',
])
def test_unsafe_configured_fallback_policy_is_refused_without_silent_dropping(environment, fallback):
    environment.cfg['delegation']['fallback_providers'] = fallback
    environment.load()
    assert _routes(environment, [{'goal': 'unsafe fallback', 'route': _A}])[0].error


def test_disabled_fallback_is_refused_without_an_extra_allowlist(environment):
    environment.cfg['delegation']['backends'] = {}
    environment.cfg['delegation']['fallback_providers'] = [_B]
    environment.cfg['providers']['route-b']['enabled'] = False
    environment.load()
    assert _routes(environment, [{'goal': 'disabled fallback', 'route': _A}])[0].error


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


@pytest.mark.parametrize('placement', ['bundled', 'host'])
def test_schema_never_mints_and_core_retains_positive_ttl_callable(environment, placement):
    from tools.delegate_tool_routes import schema_extension
    marker = environment.home / 'mint-count'
    helper = environment.home / 'synthetic-token.py'
    helper.write_text(
        'import json\nfrom pathlib import Path\n'
        f'p = Path({str(marker)!r})\n'
        'p.write_text(str(int(p.read_text()) + 1) if p.exists() else "1")\n'
        'print(json.dumps({"access_token": "generated-route-a", "expires_in": 3600}))\n', encoding='utf-8')
    provider = environment.cfg['providers']['route-a']
    provider.pop('api_key')
    provider['key_cmd'] = f'{shlex.quote(sys.executable)} {shlex.quote(str(helper))}'
    environment.load(placement)
    before = schema_extension(environment.cfg['delegation'])
    assert not marker.exists()
    route, = _routes(environment, [{'goal': 'callable credential', 'route': _A}])
    assert not route.error and callable(route.creds['api_key'])
    assert route.creds['base_url'] == provider['base_url']
    assert marker.read_text() == '1'
    assert route.creds['api_key']() == 'generated-route-a'
    assert marker.read_text() == '1'  # validation's positive-TTL source survives into the child bundle
    assert schema_extension(environment.cfg['delegation']) == before


def test_canonical_alias_endpoint_and_credential_survive_core_selection(environment):
    environment.cfg['delegation']['backends'] = {}
    environment.cfg['model_aliases'] = {'alternate': {
        'model': 'shared-model', 'provider': _A['provider'],
        'base_url': environment.origin + '/alias/v1', 'api_key': 'generated-alias-key'}}
    environment.load('host')
    route, = _routes(environment, [{'goal': 'canonical alias', 'route': {**_A, 'model': 'alternate'}}])
    assert not route.error and route.creds['model'] == 'shared-model'
    assert route.creds['base_url'] == environment.origin + '/alias/v1'
    assert route.creds['api_key'] == 'generated-alias-key'


@pytest.mark.parametrize('allow_canonical', [False, True])
def test_configured_policy_also_gates_canonical_alias_model(environment, allow_canonical):
    alias = {**_A, 'model': 'alternate'}
    environment.cfg['delegation']['backends']['explicit-routing']['allowed'] = [alias]
    if allow_canonical:
        environment.cfg['delegation']['backends']['explicit-routing']['allowed'].append(_A)
    environment.cfg['model_aliases'] = {'alternate': {'model': 'shared-model', 'provider': _A['provider']}}
    environment.load('host')
    route, = _routes(environment, [{'goal': 'canonical policy', 'route': alias}])
    if allow_canonical:
        assert not route.error and route.creds['model'] == 'shared-model'
    else:
        assert route.error


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
