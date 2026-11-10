import asyncio
import base64
import json
from urllib.parse import urlsplit

import pytest
from starlette.requests import Request

from hermes_cli.plugin_api_limits import MIB, parse_plugin_api_limits
from hermes_cli.plugin_resource_presentation import (
    MAX_BODY, PluginResourceMiddleware, ResourcePresentations, WRAPPER_PATH,
    decode_resource_response,
)

ENTRY = '/v0/resource/plugins/demo/index.html'
DOCUMENT = 'cdce3827-ef75-4770-b081-491a58125eb2'
GRANT = 'G' * 43


def request_scope(host='localhost:8642', *, scheme='http', path='/', query=b''):
    return {'type': 'http', 'http_version': '1.1', 'scheme': scheme, 'method': 'GET',
            'path': path, 'raw_path': path.encode(), 'query_string': query,
            'server': ('127.0.0.1', 8642), 'client': ('127.0.0.1', 1234),
            'headers': [(b'host', host.encode()), (b'cookie', b'session=DO_NOT_FORWARD'),
                        (b'authorization', b'Bearer SECRET'), (b'accept', b'text/html')]}


def native(body=b'<script type="module" src="./module.js"></script>', **kwargs):
    return {'ok': True, 'status': 200, 'headers': [('content-type', 'text/html')],
            'body_base64': base64.b64encode(body).decode(), **kwargs}


@pytest.fixture
def harness():
    state = {'now': 1000, 'current': True}
    registry = ResourcePresentations(clock=lambda: state['now'])
    calls = []

    def invoke(body, *, close=False):
        calls.append((body, close))
        return native()

    opened = registry.open(Request(request_scope()), {'document_id': DOCUMENT, 'grant_id': GRANT,
                           'entry_path': ENTRY, 'expires_at': 1300}, identity=('captured-profile',),
                           invoke=invoke, current=lambda: state['current'])
    return registry, state, calls, opened


def fetch(harness, path=ENTRY, query='', method='GET'):
    registry, _, _, opened = harness
    parsed = urlsplit(opened['url'])
    return asyncio.run(registry.fetch(parsed.netloc, parsed.scheme, method, path, query, 'text/html'))


def test_native_bytes_query_and_decoded_once_paths(harness):
    response = fetch(harness, '/v0/resource/plugins/demo/%E4%B8%AD%20%252e.js', 'profile=other&code=secret&x=&x=2')
    assert response.body == b'<script type="module" src="./module.js"></script>'
    body, close = harness[2][0]
    assert not close
    assert body == {'document_id': DOCUMENT, 'grant_id': GRANT,
                    'path': '/v0/resource/plugins/demo/中 %2e.js',
                    'query': [('profile', 'other'), ('code', 'secret'), ('x', ''), ('x', '2')], 'accept': 'text/html'}
    assert response.headers['access-control-allow-origin'] == '*'
    assert 'access-control-allow-credentials' not in response.headers
    assert response.headers['referrer-policy'] == 'no-referrer'
    assert 'sandbox allow-scripts allow-forms' in response.headers['content-security-policy']
    assert 'allow-same-origin' not in response.headers['content-security-policy']


def test_ancestor_wrapper_preserves_native_entry_and_opaque_sandbox(harness):
    response = fetch(harness, WRAPPER_PATH)
    origin = harness[3]['url'].removesuffix(WRAPPER_PATH)
    assert b'sandbox="allow-scripts allow-forms"' in response.body
    assert (origin + ENTRY).encode() in response.body
    assert b'allow-same-origin' not in response.body
    assert b'MessagePort' not in response.body
    assert 'frame-src ' + origin in response.headers['content-security-policy']
    assert "'self'" not in response.headers['content-security-policy']
    assert not harness[2]


@pytest.mark.parametrize('method', ['HEAD', 'POST', 'PUT', 'DELETE'])
def test_no_native_invocation_for_unsupported_methods(harness, method):
    assert fetch(harness, method=method).status_code == 405
    assert not harness[2]


def test_preflight_does_not_invoke_native_get(harness):
    assert fetch(harness, method='OPTIONS').status_code == 204
    assert not harness[2]


def test_expiry_selection_change_and_close_revoke_before_invocation(harness):
    harness[1]['current'] = False
    assert fetch(harness).status_code == 410
    harness[1]['current'] = True
    harness[1]['now'] = 1300
    assert fetch(harness).status_code == 410
    harness[1]['now'] = 1000
    harness[0].close(harness[3]['handle_id'], ('captured-profile',))
    assert fetch(harness).status_code == 410
    assert harness[2] == [({'document_id': DOCUMENT, 'grant_id': GRANT}, True)]


def test_wrong_owner_cannot_close_and_scope_change_withholds_completed_bytes(harness):
    with pytest.raises(ValueError, match='RESOURCE_REVOKED'):
        harness[0].close(harness[3]['handle_id'], ('other-profile',))
    record = harness[0]._records[harness[3]['handle_id']]
    calls = []

    def effect_then_revoke(body, *, close=False):
        calls.append(body)
        harness[1]['current'] = False
        return native(b'provider-secret')

    record.invoke = effect_then_revoke
    with pytest.raises(ValueError, match='RESOURCE_REVOKED'):
        fetch(harness)
    assert len(calls) == 1  # never replay the effect-capable GET


def test_response_allowlist_redirects_and_size_bounds():
    origin = 'http://p-' + 'a' * 48 + '.localhost:8642'
    response = decode_resource_response(native(b'unchanged', headers=[
        ['Set-Cookie', 'secret=1'], ['Authorization', 'secret'], ['Content-Security-Policy', '*'],
        ['Content-Encoding', 'gzip'], ['Location', '/v0/resource/plugins/demo/a//other.js?code=secret#section']]), origin, ENTRY)
    assert response.body == b'unchanged'
    assert response.headers['content-encoding'] == 'gzip'
    assert response.headers['location'] == '/v0/resource/plugins/demo/a//other.js?code=secret#section'
    assert 'set-cookie' not in response.headers and 'authorization' not in response.headers
    for target in ['./other.js', 'https://evil.test/', '//evil.test/', '/api/config', '/v0/resource/plugins/other/x']:
        with pytest.raises(ValueError):
            decode_resource_response(native(headers=[['Location', target]]), origin, ENTRY)
    for value in ['not-base64!', 'A' * (4 * ((MAX_BODY + 2) // 3) + 4)]:
        with pytest.raises(ValueError, match='RESOURCE_RESPONSE_INVALID'):
            decode_resource_response(native(body_base64=value), origin, ENTRY)
    exact = b'x' * MAX_BODY
    assert decode_resource_response(native(exact), origin, ENTRY).body == exact
    with pytest.raises(ValueError):
        decode_resource_response(native(exact + b'x'), origin, ENTRY)


def test_remote_origin_requires_operator_configuration_not_host_or_forwarded_headers():
    registry = ResourcePresentations(clock=lambda: 1000)
    scope = request_scope('evil.test', scheme='https')
    scope['headers'].append((b'x-forwarded-host', b'resources.example.test'))
    kwargs = {'identity': (), 'invoke': lambda *_args, **_kwargs: native(), 'current': lambda: True}
    data = {'document_id': DOCUMENT, 'grant_id': GRANT, 'entry_path': ENTRY, 'expires_at': 1200}
    with pytest.raises(ValueError, match='RESOURCE_ORIGIN_UNAVAILABLE'):
        registry.open(Request(scope), data, **kwargs)
    opened = registry.open(Request(scope), data, template='https://p-{token}.resources.example.test', **kwargs)
    assert urlsplit(opened['url']).hostname.endswith('.resources.example.test')
    assert 'evil.test' not in opened['url']
    with pytest.raises(ValueError):
        registry.open(Request(scope), {**data, 'entry_path': WRAPPER_PATH}, **kwargs)


def test_outer_middleware_never_falls_through_and_redacts_original_access_log_scope(harness):
    registry, _, calls, opened = harness
    reached = []

    async def inner(scope, receive, send):
        reached.append(scope)

    app = PluginResourceMiddleware(inner, registry=registry)
    host = urlsplit(opened['url']).netloc

    async def run(scope):
        sent = []
        async def receive():
            return {'type': 'http.request', 'body': b'', 'more_body': False}
        async def send(message):
            sent.append(message)
        await app(scope, receive, send)
        return sent

    scope = request_scope(host, path=ENTRY, query=b'code=never-log&profile=other')
    sent = asyncio.run(run(scope))
    assert sent[0]['status'] == 200
    assert not reached
    assert scope['path'] == '/__plugin_resource__' and scope['query_string'] == b''
    assert scope['headers'] == [(b'host', b'plugin-resource.invalid')]
    assert 'secret' not in json.dumps(calls).lower()
    for path in ['/api/config', '/login', '/files/private']:
        assert asyncio.run(run(request_scope(host, path=path)))[0]['status'] == 410
    unknown = 'p-' + 'b' * 48 + '.resources.example.test'
    assert asyncio.run(run(request_scope(unknown)))[0]['status'] == 410
    assert not reached


def test_declared_resource_capability_preserves_ordinary_limits():
    ordinary = parse_plugin_api_limits({})
    assert ordinary.request_bytes == MIB and not ordinary.resource_presentation
    resource = parse_plugin_api_limits({'request_context': 1, 'resource_presentation': 1})
    assert resource.resource_presentation and resource.request_bytes == MIB
    for manifest in [{'resource_presentation': 1}, {'request_context': 1, 'resource_presentation': True}]:
        with pytest.raises(ValueError):
            parse_plugin_api_limits(manifest)


def test_registration_captures_authenticated_profile_and_host_incarnation(tmp_path, monkeypatch):
    import contextvars
    from dataclasses import asdict
    from fastapi import FastAPI
    from hermes_cli import web_server
    from hermes_cli import plugin_resource_presentation as module
    from hermes_cli import plugin_request_context, plugin_policy_incarnation
    from hermes_cli.dashboard_auth import prefix
    from hermes_cli.plugin_selection_authority import SelectionAuthority

    active_home = contextvars.ContextVar('resource-test-home', default='alpha')
    registry = ResourcePresentations(clock=lambda: 1000)
    monkeypatch.setattr(module, 'presentations', registry)
    monkeypatch.setattr(prefix, '_load_dashboard_section', dict)
    authority = SelectionAuthority()
    monkeypatch.setattr(plugin_request_context, '_AUTHORITY', authority)
    monkeypatch.setattr(plugin_policy_incarnation, 'policy_incarnation', lambda *_args: 'incarnation')
    selected = authority.select('principal', 'client-' + 'a' * 20, 'connection', 'alpha')
    context = {'version': 1, 'principal': 'principal', 'home': 'alpha',
               'host_incarnation': 'connection', 'selection': asdict(selected)}
    (tmp_path / 'manifest.json').write_text(json.dumps({'api': 'api.py', 'request_context': 1, 'resource_presentation': 1}))
    observed = []

    class Host:
        _generation = 1
        alive = True

        def asgi_request(self, name, directory, api, method, path, query, headers, body, **kwargs):
            observed.append((active_home.get(), method, path, query, headers, json.loads(body), kwargs))
            return {'status': 200, 'headers': [], 'body': json.dumps({'api_version': 1, 'data': native()}).encode()}

    host = Host()
    target = (host, str(tmp_path), 'api.py')
    def resolver(name):
        return target if name == 'demo' and active_home.get() == 'alpha' else None
    payload = {'document_id': DOCUMENT, 'grant_id': GRANT, 'entry_path': ENTRY, 'expires_at': 1300}
    scope = request_scope()
    scope['method'] = 'POST'
    scope['app'] = FastAPI()
    scope['headers'].append((web_server._SESSION_HEADER_NAME.lower().encode(), web_server._SESSION_TOKEN.encode()))
    async def receive():
        return {'type': 'http.request', 'body': json.dumps(payload).encode(), 'more_body': False}
    registered = asyncio.run(module.registration(Request(scope, receive), 'demo', target, context, resolver, '_host/resource/open'))
    assert registered.status_code == 200
    opened = json.loads(registered.body)
    active_home.set('beta')
    presentation = urlsplit(opened['url'])
    assert asyncio.run(registry.fetch(presentation.netloc, 'http', 'GET', ENTRY, 'profile=beta', '')).status_code == 200
    assert observed[0][:5] == ('alpha', 'POST', '/console/resource', '', [('content-type', 'application/json')])
    assert observed[0][5]['query'] == [['profile', 'beta']]
    assert observed[0][6]['trusted_context'] == context
    assert observed[0][6]['authenticated'] is True
    host._generation = 2
    assert asyncio.run(registry.fetch(presentation.netloc, 'http', 'GET', ENTRY, '', '')).status_code == 410
    assert len(observed) == 1


def test_native_query_budget_preserves_controls_large_values_and_256_pairs(harness):
    query = 'value=' + 'x' * 9000 + '&empty=&control=%00%0A%0D&' + '&'.join('x=' for _ in range(253))
    assert fetch(harness, query=query).status_code == 200
    pairs = harness[2][0][0]['query']
    assert len(pairs) == 256
    assert pairs[2] == ('control', '\x00\n\r')
    with pytest.raises(ValueError):
        fetch(harness, query='x=' + 'a' * 65535)


def test_local_port_remapping_and_configured_tls_termination():
    registry = ResourcePresentations(clock=lambda: 1000)
    data = {'document_id': DOCUMENT, 'grant_id': GRANT, 'entry_path': ENTRY, 'expires_at': 1200}
    options = {'identity': (), 'invoke': lambda *_args, **_kwargs: native(), 'current': lambda: True}
    local = registry.open(Request(request_scope('localhost:9200')), data, **options)
    assert urlsplit(local['url']).port == 9200
    remote = registry.open(Request(request_scope('dashboard.example.test')), data,
                           template='https://p-{token}.resources.example.test:9443', **options)
    host = urlsplit(remote['url']).netloc
    assert asyncio.run(registry.fetch(host, 'http', 'GET', ENTRY, '', '')).status_code == 200
    assert asyncio.run(registry.fetch(host.replace(':9443', ':9444'), 'http', 'GET', ENTRY, '', '')).status_code == 410
    assert asyncio.run(registry.fetch(host.replace('resources.', 'other.'), 'http', 'GET', ENTRY, '', '')).status_code == 410


def test_native_slot_ends_before_unread_delivery_and_close_bypasses_native_wait(harness):
    async def exercise():
        registry, _, calls, opened = harness
        host = urlsplit(opened['url']).netloc
        first = await registry.fetch(host, 'http', 'GET', ENTRY, '', '')
        second = await asyncio.wait_for(registry.fetch(host, 'http', 'GET', ENTRY, '', ''), 5)
        assert len(calls) == 2
        await asyncio.wait_for(asyncio.to_thread(registry.close, opened['handle_id'], ('captured-profile',)), 5)
        assert calls[-1][1] is True
        sent = []
        async def send(message):
            sent.append(message)
        async def receive():
            return {'type': 'http.disconnect'}
        await first({'type': 'http'}, receive, send)
        await second({'type': 'http'}, receive, send)
        assert [message['status'] for message in sent if message['type'] == 'http.response.start'] == [410, 410]
    asyncio.run(exercise())


@pytest.mark.parametrize('revocation', ['close', 'expiry', 'selection', 'check-error'])
def test_revocation_cancels_unread_delivery_without_completing_or_replaying(harness, monkeypatch, revocation):
    from hermes_cli import plugin_resource_presentation as module
    from hermes_cli.plugin_http_admission import BufferAdmission, BufferLease

    admission = BufferAdmission(capacity=module.MAX_WIRE + MAX_BODY)
    monkeypatch.setattr(module, 'buffers', admission)
    released = []
    original_release = BufferLease.release
    def release(lease):
        if lease.admission is admission:
            released.append(lease.size)
        original_release(lease)
    monkeypatch.setattr(BufferLease, 'release', release)

    async def exercise():
        registry, state, calls, opened = harness
        body = b'x' * (3 * 65536 + 17)
        record = registry._records[opened['handle_id']]
        def invoke(payload, *, close=False):
            calls.append((payload, close))
            return native(body)
        record.invoke = invoke
        host = urlsplit(opened['url']).netloc
        reached = []
        async def inner(scope, receive, send):
            reached.append(scope)
        app = PluginResourceMiddleware(inner, registry=registry)
        paused, resume, cancelled = asyncio.Event(), asyncio.Event(), asyncio.Event()
        sent = []
        async def send(message):
            # The first chunk is already accepted by the transport. The next
            # send has not been accepted and must be cancelled under backpressure.
            if message['type'] == 'http.response.body' and any(
                    frame['type'] == 'http.response.body' for frame in sent):
                paused.set()
                try:
                    await resume.wait()
                except asyncio.CancelledError:
                    cancelled.set()
                    raise
            sent.append(message)
        async def receive():
            return {'type': 'http.request', 'body': b'', 'more_body': False}
        pending = asyncio.create_task(app(request_scope(host, path=ENTRY), receive, send))
        try:
            await asyncio.wait_for(paused.wait(), 5)
            assert admission.used == len(body)
            assert not released
            if revocation == 'close':
                await asyncio.wait_for(asyncio.to_thread(
                    registry.close, opened['handle_id'], ('captured-profile',)), 5)
            elif revocation == 'expiry':
                state['now'] = 1300
            elif revocation == 'selection':
                state['current'] = False
            else:
                def failed_check():
                    raise RuntimeError('private policy failure')
                record.current = failed_check
            with pytest.raises(ConnectionAbortedError, match='^Resource unavailable$'):
                await asyncio.wait_for(pending, 5)
            assert cancelled.is_set()
            assert admission.used == 0
            assert released == [len(body)]
            resume.set()
            await asyncio.sleep(0)
            assert [frame['status'] for frame in sent if frame['type'] == 'http.response.start'] == [200]
            chunks = [frame for frame in sent if frame['type'] == 'http.response.body']
            assert b''.join(frame['body'] for frame in chunks) == body[:65536]
            assert all(frame['more_body'] for frame in chunks)
            assert len([call for call in calls if not call[1]]) == 1
            assert len([call for call in calls if call[1]]) == (1 if revocation == 'close' else 0)
            assert not reached
        finally:
            resume.set()
            pending.cancel()
            await asyncio.gather(pending, return_exceptions=True)
        assert released == [len(body)]
    asyncio.run(exercise())


def test_native_wait_does_not_starve_single_default_worker_or_revoke(harness):
    import threading
    from concurrent.futures import ThreadPoolExecutor

    async def exercise():
        loop = asyncio.get_running_loop()
        loop.set_default_executor(ThreadPoolExecutor(max_workers=1))
        started = asyncio.Event()
        finish = threading.Event()
        registry, _, calls, opened = harness
        record = registry._records[opened['handle_id']]
        def invoke(body, *, close=False):
            calls.append((body, close))
            if not close:
                loop.call_soon_threadsafe(started.set)
                assert finish.wait(5)
            return native()
        record.invoke = invoke
        host = urlsplit(opened['url']).netloc
        pending = asyncio.create_task(registry.fetch(host, 'http', 'GET', ENTRY, '', ''))
        try:
            await asyncio.wait_for(started.wait(), 5)
            await asyncio.wait_for(asyncio.to_thread(registry.close, opened['handle_id'], ('captured-profile',)), 5)
            assert calls[-1][1] is True
        finally:
            finish.set()
        with pytest.raises(ValueError, match='RESOURCE_REVOKED'):
            await pending
        assert len([call for call in calls if not call[1]]) == 1
    asyncio.run(exercise())
