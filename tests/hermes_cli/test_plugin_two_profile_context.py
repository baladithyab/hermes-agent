"""Two generated profile homes through authenticated HTTP and real plugin hosts."""
import json
import os
from pathlib import Path

from tests.hermes_cli.test_plugin_host_request_context import API, network_client
from tests.hermes_cli.plugin_host_readiness import warm_host_infrastructure


def test_two_profile_selection_aba_preserves_pinned_acceptance(tmp_path, monkeypatch):
    from hermes_cli import web_server
    from hermes_cli.config import load_config, save_config
    from hermes_cli.plugins import get_plugin_manager
    from hermes_cli.web_server_profiles import _config_profile_scope

    monkeypatch.setattr(Path, 'home', lambda: tmp_path)
    monkeypatch.setenv('HOME', str(tmp_path))
    home = tmp_path / '.hermes'
    monkeypatch.setenv('HERMES_HOME', str(home))
    homes = {'current': home, 'beta': home / 'profiles/beta'}
    for name, directory in homes.items():
        dashboard = directory / 'plugins/context-probe/dashboard'
        dashboard.mkdir(parents=True)
        (dashboard / 'manifest.json').write_text(json.dumps({
            'name': 'context-probe', 'api': 'plugin_api.py', 'request_context': 1}))
        (dashboard / 'plugin_api.py').write_text(API)
        # The named-profile identity must exist before resolving its scope.
        (directory / 'profile.yaml').write_text('name: ' + name + '\n')
        with _config_profile_scope(name):
            cfg = load_config()
            cfg.setdefault('plugins', {}).update(enabled=['context-probe'], isolation='host')
            save_config(cfg)
    snapshots = {name: (directory / 'config.yaml').read_bytes()
                 for name, directory in homes.items()}
    routes = list(web_server.app.router.routes)
    web_server._dashboard_plugins_cache = None
    hosts = []
    readiness = {}
    base = '/api/plugins/context-probe'
    try:
        with network_client(web_server.app) as client:
            client.headers[web_server._SESSION_HEADER_NAME] = web_server._SESSION_TOKEN
            client.headers['X-Hermes-Plugin-Client'] = 'generated-two-profile-window'

            def request(method, path, profile):
                return client.request(method, base + path, params={'profile': profile})

            def select(profile):
                response = request('POST', '/_host/selection', profile)
                assert response.status_code == 200, response.text
                value = response.json()
                client.headers['X-Hermes-Plugin-Scope'] = value['scope_id']
                client.headers['X-Hermes-Plugin-Epoch'] = str(value['epoch'])
                if profile not in readiness:
                    with _config_profile_scope(profile):
                        host = get_plugin_manager()._plugin_host()
                        assert host._home == homes[profile].resolve()
                        readiness[profile] = warm_host_infrastructure(
                            host, homes[profile] / 'host-infrastructure-ready')
                return value

            first = select('current')
            accepted = request('POST', '/accept', 'current')
            assert accepted.status_code == 200, accepted.text
            assert accepted.json()['home'] == str(home)
            first_scope = request('GET', '/scope', 'current').json()
            assert first_scope['pid'] == readiness['current']['pid'] != os.getpid()
            # A comparison token cannot authorize the other profile's handler.
            assert request('GET', '/scope', 'beta').status_code == 409
            second = select('beta')
            assert second['epoch'] > first['epoch']
            other_scope = request('GET', '/scope', 'beta')
            assert other_scope.status_code == 200, other_scope.text
            other_scope = other_scope.json()
            assert other_scope['context']['home'] == str(homes['beta'])
            assert other_scope['pid'] == readiness['beta']['pid']
            assert other_scope['pid'] not in (os.getpid(), first_scope['pid'])
            assert request('GET', '/scope', 'current').status_code == 409
            # Original accepted operation remains in A's actual subprocess and
            # uses its captured A policy, even while B is selected.
            with _config_profile_scope('current'):
                host = get_plugin_manager()._plugin_host()
                result = host.asgi_request('context-probe',
                    str(home / 'plugins/context-probe/dashboard'), 'plugin_api.py',
                    'GET', '/revalidate', '', [], b'')
                assert json.loads(result['body']) == {'valid': True}
            again = select('current')
            assert again['epoch'] > second['epoch']
            assert request('GET', '/revalidate', 'current').json() == {'valid': True}
            current_headers = dict(client.headers)
            client.headers['X-Hermes-Plugin-Scope'] = first['scope_id']
            client.headers['X-Hermes-Plugin-Epoch'] = str(first['epoch'])
            assert request('POST', '/accept', 'current').status_code == 409
            client.headers.update(current_headers)
            # A separate native client has an independent selection, not a
            # process-global current profile shared with this window.
            client.headers['X-Hermes-Plugin-Client'] = 'generated-independent-window'
            client.headers.pop('X-Hermes-Plugin-Scope')
            client.headers.pop('X-Hermes-Plugin-Epoch')
            select('beta')
            assert request('GET', '/scope', 'beta').status_code == 200
            client.headers.update(current_headers)
            assert request('GET', '/revalidate', 'current').json() == {'valid': True}
            assert request('GET', '/scope', 'current').json()['pid'] == first_scope['pid']
        assert all((homes[name] / 'config.yaml').read_bytes() == data
                   for name, data in snapshots.items())
        # Explicit policy revocation is distinct from selection: revoking B
        # must not revoke A, while A disable/re-enable must latch against ABA.
        def revalidate_original():
            with _config_profile_scope('current'):
                original_host = get_plugin_manager()._plugin_host()
                result = original_host.asgi_request('context-probe',
                    str(home / 'plugins/context-probe/dashboard'), 'plugin_api.py',
                    'GET', '/revalidate', '', [], b'')
                return json.loads(result['body'])['valid']

        for profile in ('beta', 'current'):
            with _config_profile_scope(profile):
                cfg = load_config()
                cfg['plugins']['disabled'] = ['context-probe']
                save_config(cfg)
                cfg['plugins']['disabled'] = []
                save_config(cfg)
            assert revalidate_original() is (profile == 'beta')
    finally:
        for name in homes:
            with _config_profile_scope(name):
                host = get_plugin_manager()._plugin_host()
                hosts.append(host)
                host.shutdown()
        web_server.app.router.routes[:] = routes
        web_server._dashboard_plugins_cache = None
    assert all(not host.alive for host in hosts)
