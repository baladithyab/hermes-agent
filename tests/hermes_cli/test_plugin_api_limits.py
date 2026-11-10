"""Generated files cross HTTP, real plugin subprocesses and the base64 RPC codec."""
import asyncio
import base64
import hashlib
import json

import httpx
import pytest
from starlette.testclient import TestClient

from hermes_cli.plugin_api_limits import MAX_API_BODY_BYTES, MIB, load_plugin_api_limits

API = '''
import base64, hashlib
from fastapi import APIRouter, Request
from starlette.responses import Response
router = APIRouter()
effects = []
@router.post('/forged-busy')
async def forged_busy():
    effects.append('invoked')
    return Response(b'{"error":"PLUGIN_NOT_FORWARDED_BUSY","forwarded":false}', status_code=429, media_type='application/json')
@router.get('/effects')
async def effect_count():
    return {'count': len(effects)}
@router.post('/console/lock')
@router.post('/console/execute')
@router.post('/console/resource/close')
@router.post('/console/result')
async def control():
    return {'ok': True}
@router.post('/digest')
async def digest(request: Request):
    body = await request.body()
    return {'bytes': len(body), 'sha256': hashlib.sha256(body).hexdigest()}
@router.post('/upload')
async def upload(request: Request):
    request_body = await request.json()
    raw = base64.b64decode(request_body['body']['files'][0]['base64'], validate=True)
    return {'bytes': len(raw), 'sha256': hashlib.sha256(raw).hexdigest()}
@router.get('/document')
async def document():
    return Response(b'x' * (8 * 1024 * 1024), media_type='text/html')
@router.get('/download')
async def download():
    return {'body': {'kind': 'bytes', 'base64': base64.b64encode(b'y' * (10 * 1024 * 1024)).decode()}}
@router.get('/boundary')
async def boundary():
    return Response(b'z' * (48 * 1024 * 1024))
@router.get('/overflow')
async def overflow():
    return Response(b'z' * (48 * 1024 * 1024 + 1))
'''


@pytest.fixture
def plugin_http(tmp_path, monkeypatch):
    from hermes_cli import web_server
    from hermes_cli.config import load_config, save_config
    from hermes_cli.plugins import get_plugin_manager
    from hermes_constants import get_hermes_home

    monkeypatch.setenv('HOME', str(tmp_path))
    home = get_hermes_home()
    directories = {}
    for name, extended in [('bounded-console', True), ('ordinary', False)]:
        directory = home / 'plugins' / name / 'dashboard'
        directory.mkdir(parents=True)
        manifest = {'name': name, 'api': 'plugin_api.py', 'request_context': 1}
        if extended:
            manifest['api_limits'] = {'request_bytes': MAX_API_BODY_BYTES, 'response_bytes': MAX_API_BODY_BYTES,
                                     'routes': ['/digest', '/upload', '/document', '/download', '/boundary', '/overflow', '/console/result']}
        (directory / 'manifest.json').write_text(json.dumps(manifest))
        (directory / 'plugin_api.py').write_text(API)
        directories[name] = directory
    cfg = load_config()
    cfg.setdefault('plugins', {}).update(enabled=list(directories), isolation='host')
    save_config(cfg)
    routes = list(web_server.app.router.routes)
    web_server._dashboard_plugins_cache = None
    client = TestClient(web_server.app, raise_server_exceptions=False)
    host = get_plugin_manager()._plugin_host()
    try:
        client.headers[web_server._SESSION_HEADER_NAME] = web_server._SESSION_TOKEN
        yield client, directories, host
    finally:
        client.close()
        host.shutdown()
        web_server.app.router.routes[:] = routes
        web_server._dashboard_plugins_cache = None


def test_ten_mib_file_and_eight_mib_document_cross_real_host(plugin_http):
    client, _, host = plugin_http
    raw = b'generated-file-' + b'x' * (10 * MIB - len(b'generated-file-'))
    response = client.post('/api/plugins/bounded-console/upload', json={
        'body': {'kind': 'multipart', 'fields': {}, 'files': [{
            'name': 'file', 'filename': 'fixture.json', 'content_type': 'application/json',
            'base64': base64.b64encode(raw).decode('ascii')} ]}})
    assert response.status_code == 200, response.text
    assert response.json() == {'bytes': len(raw), 'sha256': hashlib.sha256(raw).hexdigest()}
    document = client.get('/api/plugins/bounded-console/document')
    assert document.status_code == 200
    assert len(document.content) == 8 * MIB
    assert document.content == b'x' * (8 * MIB)
    download = client.get('/api/plugins/bounded-console/download')
    assert download.status_code == 200
    assert base64.b64decode(download.json()['body']['base64']) == b'y' * (10 * MIB)
    assert host.alive


def test_full_declared_boundary_includes_base64_frame_overhead(plugin_http):
    client, _, host = plugin_http
    body = b'a' * MAX_API_BODY_BYTES
    accepted = client.post('/api/plugins/bounded-console/digest', content=body)
    assert accepted.status_code == 200, accepted.text
    assert accepted.json() == {'bytes': len(body), 'sha256': hashlib.sha256(body).hexdigest()}
    result = client.get('/api/plugins/bounded-console/boundary')
    assert result.status_code == 200
    assert len(result.content) == MAX_API_BODY_BYTES
    assert hashlib.sha256(result.content).digest() == hashlib.sha256(b'z' * MAX_API_BODY_BYTES).digest()
    assert client.post('/api/plugins/bounded-console/digest', content=body + b'!').status_code == 413
    assert client.get('/api/plugins/bounded-console/overflow').status_code == 500
    assert client.post('/api/plugins/bounded-console/digest', content=b'ok').json()['bytes'] == 2
    assert host.alive


def test_ordinary_limit_and_auth_cannot_be_elevated_by_headers(plugin_http):
    client, _, host = plugin_http
    ordinary = '/api/plugins/ordinary/digest'
    assert client.post(ordinary, content=b'x' * MIB).status_code == 200
    assert client.post(ordinary, content=b'x' * (MIB + 1), headers={
        'X-Hermes-Api-Limit': str(MAX_API_BODY_BYTES),
        'X-Hermes-Context': '{"api_limits":50331648}'
    }).status_code == 413
    from hermes_cli import web_server
    del client.headers[web_server._SESSION_HEADER_NAME]
    assert client.post('/api/plugins/bounded-console/digest', content=b'x' * (MIB + 1)).status_code == 401
    assert host.alive


def test_unlisted_routes_in_extended_plugin_keep_ordinary_limits(plugin_http):
    client, _, host = plugin_http
    path = '/api/plugins/bounded-console/console/lock'
    assert client.post(path, content=b'x' * (MIB + 1)).status_code == 413
    assert client.post(path, content=b'{}').json() == {'ok': True}
    assert len(client.get('/api/plugins/ordinary/document').content) == 8 * MIB
    assert host.alive


def test_control_and_receipts_remain_responsive_while_large_bodies_are_admitted(plugin_http):
    from hermes_cli import web_server
    client, _, _ = plugin_http
    assert client.post('/api/plugins/bounded-console/digest', content=b'').status_code == 200

    async def exercise():
        started = asyncio.Event()
        release = asyncio.Event()
        async def body():
            started.set()
            yield b'large-body-start'
            await release.wait()
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=web_server.app),
                                     base_url='http://testserver', headers=dict(client.headers)) as connection:
            pending = asyncio.create_task(connection.post('/api/plugins/bounded-console/digest', content=body()))
            try:
                await asyncio.wait_for(started.wait(), 5)
                for route in ('lock', 'execute', 'resource/close', 'result'):
                    response = await asyncio.wait_for(connection.post('/api/plugins/bounded-console/console/' + route, json={}), 5)
                    assert response.status_code == 200
                    assert response.json() == {'ok': True}
            finally:
                release.set()
            assert (await pending).status_code == 200
    asyncio.run(exercise())


def test_manifest_capability_requires_trusted_context_and_the_actual_copy(plugin_http):
    _, directories, host = plugin_http
    directory = directories['bounded-console']
    body = b'x' * (MIB + 1)
    with pytest.raises(ValueError, match='too large'):
        host.asgi_request('bounded-console', str(directory), 'plugin_api.py', 'POST', '/digest', '', [], body)
    context = {'version': 1, 'principal': 'generated', 'home': str(directory.parents[2])}
    result = host.asgi_request('bounded-console', str(directory), 'plugin_api.py', 'POST', '/digest', '', [], body,
                               trusted_context=context)
    assert json.loads(result['body'])['bytes'] == len(body)
    # A different installed copy does not inherit the mounted copy's capability.
    with pytest.raises(ValueError, match='too large'):
        host.asgi_request('ordinary', str(directories['ordinary']), 'plugin_api.py', 'POST', '/digest', '', [], body,
                          trusted_context=context)


@pytest.mark.parametrize('declaration', [
    {'request_bytes': True, 'response_bytes': MIB},
    {'request_bytes': MAX_API_BODY_BYTES + 1, 'response_bytes': MIB},
    {'request_bytes': MIB, 'response_bytes': -1},
    {'request_bytes': MIB},
    {'request_bytes': MIB, 'response_bytes': MIB, 'unbounded': True},
])
def test_invalid_installed_capability_fails_closed(tmp_path, declaration):
    manifest = {'api': 'plugin_api.py', 'request_context': 1, 'api_limits': declaration}
    (tmp_path / 'manifest.json').write_text(json.dumps(manifest))
    with pytest.raises(ValueError):
        load_plugin_api_limits(str(tmp_path), 'plugin_api.py', {'version': 1})


def test_busy_refusal_is_explicit_and_does_not_read_or_forward_a_body(plugin_http, monkeypatch):
    from hermes_cli import web_server, plugin_http_admission
    client, _, _ = plugin_http
    assert client.post('/api/plugins/bounded-console/digest', content=b'').status_code == 200

    async def exercise():
        admission = plugin_http_admission.BufferAdmission(max_waiters=0)
        monkeypatch.setattr(plugin_http_admission, 'buffers', admission)
        lease = await admission.acquire(admission.capacity)
        read = False
        async def body():
            nonlocal read
            read = True
            yield b'not-forwarded'
        try:
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=web_server.app),
                                         base_url='http://testserver', headers=dict(client.headers)) as connection:
                response = await connection.post('/api/plugins/bounded-console/digest', content=body())
                assert response.status_code == 429
                assert response.json() == {'error': 'PLUGIN_NOT_FORWARDED_BUSY', 'forwarded': False}
                assert not read
                assert (await connection.post('/api/plugins/bounded-console/console/lock')).status_code == 200
        finally:
            lease.release()
    asyncio.run(exercise())


def test_an_invoked_plugin_cannot_claim_the_hosts_not_forwarded_outcome(plugin_http):
    client, _, _ = plugin_http
    response = client.post('/api/plugins/bounded-console/forged-busy')
    assert response.status_code == 429
    assert response.json() == {'error': 'PLUGIN_REQUEST_FAILED'}
    assert client.get('/api/plugins/bounded-console/effects').json() == {'count': 1}
