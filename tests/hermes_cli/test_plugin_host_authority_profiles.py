"""Native profile tickets retain both hosted admission and selection authority."""
import json
import os

import pytest

from hermes_cli.plugin_host import PluginHost
from tests.hermes_cli import test_plugin_secondary_profile_host as secondary

daemon = secondary.daemon

_API = '''
import os
from fastapi import APIRouter, Request
from hermes_cli.web_deps import late
router = APIRouter()
@router.put('/config')
def config(request: Request):
    late('_require_token')(request)
    context = request.state.hermes_context
    return {'pid': os.getpid(), 'home': context['home'],
            'selection_home': context['selection']['home'],
            'credentials': [key for key in request.headers if key in
                {'authorization', 'cookie', 'x-hermes-session-token', 'x-hermes-gateway-ticket'}]}
'''


@pytest.mark.parametrize('profile', ['launch', 'beta'])
def test_native_grant_selection_and_sensitive_admission_share_the_profile(daemon, monkeypatch, profile):
    monkeypatch.setattr(PluginHost, 'asgi_request', secondary._REAL_ASGI_REQUEST)
    name = 'authority-probe'
    home = getattr(daemon, profile)
    secondary._install(home, name)
    secondary._plugins(home, enabled=[name], isolation='host')
    dashboard = home / 'plugins' / name / 'dashboard'
    manifest = json.loads((dashboard / 'manifest.json').read_text())
    manifest['request_context'] = 1
    (dashboard / 'manifest.json').write_text(json.dumps(manifest))
    (dashboard / 'plugin_api.py').write_text(_API)
    base = '/api/plugins/' + name
    client = 'generated-authority-client-' + profile

    def headers():
        credential = daemon.owner if profile == 'launch' else daemon.beta_ticket()
        return {**credential, 'X-Hermes-Plugin-Client': client}

    try:
        selection = daemon.client.post(base + '/_host/selection', headers=headers())
        assert selection.status_code == 200, selection.text
        selected = selection.json()
        selected_headers = {**headers(), 'X-Hermes-Plugin-Scope': selected['scope_id'],
                            'X-Hermes-Plugin-Epoch': str(selected['epoch'])}
        response = daemon.client.put(base + '/config', headers=selected_headers)
        assert response.status_code == 200, response.text
        assert response.json() == {'pid': response.json()['pid'], 'home': str(home),
                                   'selection_home': str(home), 'credentials': []}
        assert response.json()['pid'] != os.getpid()
        stale = {**headers(), 'X-Hermes-Plugin-Scope': selected['scope_id'],
                 'X-Hermes-Plugin-Epoch': str(selected['epoch'] + 1)}
        assert daemon.client.put(base + '/config', headers=stale).status_code == 409
        assert daemon.client.put(base + '/config').status_code == 401
    finally:
        secondary._shutdown_hosts()
