"""Authenticated request metadata must survive the real plugin subprocess boundary."""
import json
import os
from contextlib import contextmanager, nullcontext
import socket
import threading
from concurrent.futures import ThreadPoolExecutor

import httpx
import pytest
import uvicorn

from hermes_cli.plugins import PluginManager
from tests.hermes_cli.plugin_host_readiness import warm_host_infrastructure
from tests.waiting import wait_until


API = '''
import asyncio, os
from fastapi import APIRouter, Request
router = APIRouter()
accepted = {}
@router.post('/accept')
def accept(request: Request):
    ops = request.state.hermes_operations
    result = ops.accept('generated-op-' + request.state.hermes_context['selection']['client'], 'a' * 64)
    accepted['op'] = (ops, result)
    return {'valid': ops.revalidate(result), 'action': result['record']['action'],
            'home': result['record']['selection']['home']}
@router.get('/revalidate')
def revalidate():
    ops, result = accepted['op']
    return {'valid': ops.revalidate(result)}
@router.get('/scope')
async def scope(request: Request):
    await asyncio.sleep(.02)
    return {'context': getattr(request.state, 'hermes_context', None),
            'pid': os.getpid(), 'authorization': request.headers.get('authorization')}
'''


def test_host_context_is_request_local_and_not_a_client_header(tmp_path, monkeypatch):
    home = tmp_path / 'home'
    home.mkdir()
    monkeypatch.setenv('HOME', str(tmp_path))
    monkeypatch.setenv('HERMES_HOME', str(home))
    dashboard = home / 'dashboard'
    dashboard.mkdir()
    (dashboard / 'plugin_api.py').write_text(API)
    manager = PluginManager()
    host = manager._plugin_host()
    try:
        warm_host_infrastructure(host, tmp_path / 'host-infrastructure-ready')
        def call(epoch):
            context = {'version': 1, 'principal': 'generated-principal',
                       'home': str(home), 'selection_epoch': epoch}
            result = host.asgi_request('probe', str(dashboard), 'plugin_api.py',
                'GET', '/scope', '', [('X-Hermes-Context', 'forged'),
                ('Authorization', 'Bearer generated-do-not-forward')], b'',
                trusted_context=context)
            body = json.loads(result['body'])
            assert body['context'] == context
            assert body['pid'] != os.getpid()
            assert body['authorization'] is None
        with ThreadPoolExecutor(max_workers=2) as pool:
            list(pool.map(call, [1, 2]))
        result = host.asgi_request('probe', str(dashboard), 'plugin_api.py',
            'GET', '/scope', '', [('X-Hermes-Context', 'forged')], b'')
        assert json.loads(result['body'])['context'] is None
    finally:
        host.shutdown()
    assert not host.alive


def test_dashboard_lifespan_unloads_only_matching_plugin_and_reloads(tmp_path, monkeypatch):
    monkeypatch.setenv('HOME', str(tmp_path))
    monkeypatch.setenv('HERMES_HOME', str(tmp_path / 'home'))
    manager = PluginManager()
    host = manager._plugin_host()
    api = '''
from contextlib import asynccontextmanager
from pathlib import Path
from fastapi import APIRouter
log = Path(__file__).with_suffix('.events')
@asynccontextmanager
async def lifespan(app):
    with log.open('a') as stream: stream.write('start\\n')
    try:
        yield
    finally:
        with log.open('a') as stream: stream.write('stop\\n')
router = APIRouter(lifespan=lifespan)
@router.get('/ping')
def ping(): return {'ok': True}
'''
    dashboards = {}
    for name in ('one', 'two'):
        directory = tmp_path / name
        directory.mkdir()
        (directory / 'plugin_api.py').write_text(api)
        dashboards[name] = directory
    def ping(name):
        result = host.asgi_request(name, str(dashboards[name]), 'plugin_api.py',
                                   'GET', '/ping', '', [], b'')
        assert json.loads(result['body']) == {'ok': True}
    try:
        warm_host_infrastructure(host, tmp_path / 'host-infrastructure-ready')
        ping('one')
        ping('two')
        ping('one')
        host._unload('one')
        assert (dashboards['one'] / 'plugin_api.events').read_text() == 'start\nstop\n'
        assert (dashboards['two'] / 'plugin_api.events').read_text() == 'start\n'
        ping('one')
        assert (dashboards['one'] / 'plugin_api.events').read_text() == 'start\nstop\nstart\n'
    finally:
        host.shutdown()
    assert not host.alive
    for name, expected in [('one', 'start\nstop\nstart\nstop\n'), ('two', 'start\nstop\n')]:
        assert (dashboards[name] / 'plugin_api.events').read_text() == expected


@contextmanager
def network_client(app):
    sock = socket.socket()
    sock.bind(('127.0.0.1', 0))
    sock.listen(32)
    server = uvicorn.Server(uvicorn.Config(app, log_level='critical', lifespan='off',
                                         timeout_graceful_shutdown=65))
    thread = threading.Thread(target=server.run, kwargs={'sockets': [sock]}, daemon=True)
    thread.start()
    try:
        wait_until(lambda: server.started or not thread.is_alive(), timeout=5,
                   what='fixture HTTP server startup')
        assert server.started
        # The client must observe the real server's 60s response budget before
        # timing out, and teardown must permit that in-flight work to finish.
        with httpx.Client(base_url=f'http://127.0.0.1:{sock.getsockname()[1]}',
                          timeout=httpx.Timeout(70, connect=5)) as client:
            yield client
    finally:
        server.should_exit = True
        thread.join(75)
        sock.close()
        assert not thread.is_alive()


@pytest.mark.parametrize('network', [False, True])
def test_authenticated_http_enters_real_host_with_no_credentials(tmp_path, monkeypatch, network):
    from starlette.testclient import TestClient
    from hermes_cli import web_server
    from hermes_cli.config import load_config, save_config
    from hermes_cli.plugins import get_plugin_manager
    from hermes_cli.web_server_profiles import _config_profile_scope
    from hermes_constants import get_hermes_home

    monkeypatch.setenv('HOME', str(tmp_path))
    home = get_hermes_home()
    dashboard = home / 'plugins' / 'context-probe' / 'dashboard'
    dashboard.mkdir(parents=True)
    (dashboard / 'manifest.json').write_text(json.dumps(
        {'name': 'context-probe', 'api': 'plugin_api.py', 'request_context': 1}))
    (dashboard / 'plugin_api.py').write_text(API)
    cfg = load_config()
    cfg.setdefault('plugins', {}).update(enabled=['context-probe'], isolation='host')
    save_config(cfg)
    routes = list(web_server.app.router.routes)
    web_server._dashboard_plugins_cache = None
    transport = network_client(web_server.app) if network else nullcontext(TestClient(web_server.app))
    client = transport.__enter__()
    url = '/api/plugins/context-probe/scope'
    try:
        assert client.get(url).status_code == 401
        client.headers[web_server._SESSION_HEADER_NAME] = web_server._SESSION_TOKEN
        admitted = client.get('/api/dashboard/plugins')
        assert admitted.status_code == 200
        assert 'context-probe' in {plugin['name'] for plugin in admitted.json()}
        with _config_profile_scope('current'):
            host = get_plugin_manager()._plugin_host()
            assert host._home == home.resolve()
            readiness = warm_host_infrastructure(host, home / 'host-infrastructure-ready')
        response = client.get(url, headers={'X-Hermes-Context': 'forged'})
        assert response.status_code == 200, response.text
        data = response.json()
        assert data['pid'] == readiness['pid'] != os.getpid()
        assert data['context']['home'] == str(home.resolve())
        assert len(data['context']['principal']) == 64
        assert data['authorization'] is None
        if network:
            import subprocess
            from pathlib import Path
            env = {key: os.environ[key] for key in ('PATH', 'HOME', 'HERMES_HOME') if key in os.environ}
            env.update(TEST_PLUGIN_URL=str(client.base_url).rstrip('/') + '/api/plugins/context-probe',
                       TEST_PLUGIN_AUTH_HEADER=web_server._SESSION_HEADER_NAME,
                       TEST_PLUGIN_AUTH=web_server._SESSION_TOKEN)
            probe = subprocess.run(['node', str(Path(__file__).with_name('plugin_selection_network.mts'))],
                                   env=env, capture_output=True, text=True, timeout=20, check=False)
            assert probe.returncode == 0, probe.stdout + probe.stderr
        client.headers['X-Hermes-Plugin-Client'] = 'generated-window-' + str(network)
        assert client.get(url).status_code == 409
        selection = client.post('/api/plugins/context-probe/_host/selection')
        assert selection.status_code == 200
        selected = selection.json()
        assert set(selected) == {'scope_id', 'epoch'}
        client.headers['X-Hermes-Plugin-Scope'] = selected['scope_id']
        client.headers['X-Hermes-Plugin-Epoch'] = str(selected['epoch'])
        scoped = client.get(url)
        assert scoped.status_code == 200
        assert scoped.json()['context']['selection']['home'] == str(home.resolve())
        assert scoped.json()['context']['selection']['epoch'] == selected['epoch']
        accepted = client.post('/api/plugins/context-probe/accept', json={'retry': 4})
        assert accepted.status_code == 200, accepted.text
        assert accepted.json() == {'valid': True, 'action': 'POST:/accept', 'home': str(home.resolve())}
        assert client.get('/api/plugins/context-probe/revalidate').json() == {'valid': True}
        # Selection changes do not revoke immutable accepted work. Exercise the
        # real private transport after A->B->A, and reject the old HTTP token.
        from hermes_cli.plugin_request_context import _AUTHORITY
        original = scoped.json()['context']['selection']
        deselected = client.post('/api/plugins/context-probe/_host/deselection')
        assert deselected.status_code == 200
        assert client.get(url).status_code == 409
        assert client.post('/api/plugins/context-probe/_host/selection').status_code == 409
        _AUTHORITY.select(original['principal'], original['client'], original['connection'], str(home / 'B'),
                          expected_epoch=deselected.json()['epoch'])
        again = _AUTHORITY.select(original['principal'], original['client'], original['connection'], str(home.resolve()))
        assert client.get(url).status_code == 409
        client.headers['X-Hermes-Plugin-Scope'] = again.scope_id
        client.headers['X-Hermes-Plugin-Epoch'] = str(again.epoch)
        assert client.get('/api/plugins/context-probe/revalidate').json() == {'valid': True}
        client.headers['X-Hermes-Plugin-Epoch'] = str(again.epoch + 1)
        assert client.get(url).status_code == 409
        # Revocation and re-enable entirely between checks must not revive an
        # accepted capability (sampling only the final boolean loses this ABA).
        cfg['plugins']['disabled'] = ['context-probe']
        save_config(cfg)
        cfg['plugins']['disabled'] = []
        save_config(cfg)
        host = get_plugin_manager()._plugin_host()
        result = host.asgi_request('context-probe', str(dashboard), 'plugin_api.py',
            'GET', '/revalidate', '', [], b'')
        assert json.loads(result['body']) == {'valid': False}
        cfg['plugins']['disabled'] = ['context-probe']
        save_config(cfg)
        assert client.get(url).status_code == 404
        # Direct child continuation simulates already accepted background work:
        # unlike a new HTTP request it remains callable, but host policy denies it.
        host = get_plugin_manager()._plugin_host()
        result = host.asgi_request('context-probe', str(dashboard), 'plugin_api.py',
            'GET', '/revalidate', '', [], b'')
        assert json.loads(result['body']) == {'valid': False}
        cfg['plugins']['disabled'] = []
        save_config(cfg)
        result = host.asgi_request('context-probe', str(dashboard), 'plugin_api.py',
            'GET', '/revalidate', '', [], b'')
        assert json.loads(result['body']) == {'valid': False}
    finally:
        try:
            transport.__exit__(None, None, None)
        finally:
            get_plugin_manager()._plugin_host().shutdown()
            web_server.app.router.routes[:] = routes
            web_server._dashboard_plugins_cache = None
