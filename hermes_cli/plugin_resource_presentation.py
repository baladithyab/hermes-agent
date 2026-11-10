"""Isolated, expiring presentation origins for explicitly declared plugin resources.

The owner authorizes native GETs (which can have effects). This layer only fixes
transport context and browser confinement; it neither follows nor retries GETs.
"""
from __future__ import annotations

import asyncio
import base64
import binascii
import contextvars
from dataclasses import dataclass
import html
import ipaddress
import json
from pathlib import Path
import re
import secrets
import threading
import time
from urllib.parse import parse_qsl, unquote, urlsplit

from starlette.responses import JSONResponse, Response

from hermes_cli.plugin_http_admission import BufferAdmission, buffers, plugin_io
from hermes_cli.plugin_host_wire import PluginHostBusy

MAX_BODY = 32 * 1024 * 1024
MAX_WIRE = 48 * 1024 * 1024
WRAPPER_PATH = '/__hermes_plugin_resource__'
TOKEN = re.compile(r'p-[a-f0-9]{48}')
NATIVE_PATH = re.compile(r'/v0/resource/plugins/[A-Za-z0-9_-]+/')
SAFE_HEADERS = frozenset(('content-type', 'content-encoding', 'content-disposition',
                          'content-language', 'etag', 'last-modified'))


def resource_path(path):
    if (not isinstance(path, str) or len(path) > 4096 or not NATIVE_PATH.match(path)
            or any(ord(char) < 32 for char in path) or any(char in path for char in '\\?#')):
        raise ValueError('RESOURCE_PATH_DENIED')
    decoded = unquote(path, errors='strict')
    if '\\' in decoded or any(ord(char) < 32 for char in decoded) or any(part in ('.', '..') for part in decoded.split('/')):
        raise ValueError('RESOURCE_PATH_DENIED')
    return path


def presentation_headers(origin, *, wrapper=False):
    frame_source = origin if wrapper else "'none'"
    policy = ("sandbox allow-scripts allow-forms; default-src 'none'; base-uri 'none'; object-src 'none'; worker-src 'none'; "
              f"script-src 'unsafe-inline' {origin}; style-src 'unsafe-inline' {origin}; "
              f"img-src data: blob: {origin}; font-src {origin}; media-src {origin}; "
              f"connect-src {origin}; form-action {origin}; "
              f"frame-src {frame_source}")
    return {'Content-Security-Policy': policy, 'Cache-Control': 'no-store',
            'Referrer-Policy': 'no-referrer', 'X-Content-Type-Options': 'nosniff',
            'Access-Control-Allow-Origin': '*', 'Access-Control-Allow-Methods': 'GET, OPTIONS',
            'Access-Control-Allow-Headers': 'Accept', 'Vary': 'Origin'}


def wrapper_document(origin, entry):
    source = html.escape(origin + entry, quote=True)
    return (f'<!doctype html><html><head><meta charset="utf-8"><meta name="referrer" content="no-referrer">'
            '<style>html,body,iframe{margin:0;width:100%;height:100%;border:0;overflow:hidden}</style>'
            '</head><body><iframe title="Plugin resource" sandbox="allow-scripts allow-forms" '
            f'referrerpolicy="no-referrer" src="{source}"></iframe></body></html>').encode()


def decode_resource_response(raw, origin, path):
    if not isinstance(raw, dict) or raw.get('ok') is not True:
        raise ValueError('RESOURCE_GONE')
    status, headers, encoded = raw.get('status'), raw.get('headers'), raw.get('body_base64')
    if (type(status) is not int or not 200 <= status <= 599 or not isinstance(encoded, str)
            or len(encoded) > 4 * ((MAX_BODY + 2) // 3)):
        raise ValueError('RESOURCE_RESPONSE_INVALID')
    try:
        body = base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError):
        raise ValueError('RESOURCE_RESPONSE_INVALID') from None
    if len(body) > MAX_BODY or (status in (204, 205, 304) and body):
        raise ValueError('RESOURCE_RESPONSE_INVALID')
    if isinstance(headers, dict):
        headers = list(headers.items())
    if not isinstance(headers, list) or len(headers) > 64:
        raise ValueError('RESOURCE_RESPONSE_INVALID')
    output = presentation_headers(origin)
    budget = 0
    for pair in headers:
        if not isinstance(pair, (list, tuple)) or len(pair) != 2 or not all(isinstance(x, str) for x in pair):
            raise ValueError('RESOURCE_RESPONSE_INVALID')
        name, value = pair
        budget += len(name) + len(value)
        if budget > 128 * 1024 or any(ord(char) < 32 or ord(char) == 127 for char in name + value):
            raise ValueError('RESOURCE_RESPONSE_INVALID')
        key = name.lower()
        if key in SAFE_HEADERS:
            output[key] = value
        elif key == 'location':
            # The owner has already checked its exact grant-approved redirect
            # set. Independently reject a response that leaves this plugin.
            destination = urlsplit(value)
            if (not value.startswith('/') or value.startswith('//') or destination.scheme or destination.netloc
                    or not resource_path(destination.path).startswith(NATIVE_PATH.match(path).group())):
                raise ValueError('RESOURCE_REDIRECT_DENIED')
            output['Location'] = value
    return Response(body, status_code=status, headers=output)


@dataclass
class Presentation:
    origin: str
    document_id: str
    configured_origin: bool
    grant_id: str
    entry_path: str
    expires_at: float
    identity: tuple
    invoke: object
    current: object


class ResourcePresentations:
    def __init__(self, *, clock=time.time):
        self.clock = clock
        self._records = {}
        self._lock = threading.Lock()
        self._native_slot = BufferAdmission(capacity=1, max_waiters=16)
        self._configured_namespaces = frozenset()

    def reserve_template(self, template):
        if not isinstance(template, str) or template.count('{token}') != 1:
            return
        try:
            name = urlsplit(template.replace('{token}', '0' * 48)).hostname or ''
        except ValueError:
            return
        if name.startswith('p-' + '0' * 48 + '.'):
            with self._lock:
                self._configured_namespaces = self._configured_namespaces | {name.split('.', 1)[1].lower()}

    def _origin(self, request, token, template):
        if template:
            if not isinstance(template, str) or template.count('{token}') != 1:
                raise ValueError('RESOURCE_ORIGIN_CONFIG_INVALID')
            origin = template.replace('{token}', token)
            parsed = urlsplit(origin)
            if (parsed.scheme not in ('http', 'https') or parsed.username or parsed.password
                    or parsed.path not in ('', '/') or parsed.query or parsed.fragment
                    or not parsed.hostname or parsed.hostname.split('.')[0] != 'p-' + token
                    or '.' not in parsed.hostname):
                raise ValueError('RESOURCE_ORIGIN_CONFIG_INVALID')
            if (any(ord(char) <= 32 for char in origin) or len(parsed.hostname) > 253
                    or any(not re.fullmatch(r'[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?', label)
                           for label in parsed.hostname.split('.'))
                    or (parsed.port is not None and not 1 <= parsed.port <= 65535)):
                raise ValueError('RESOURCE_ORIGIN_CONFIG_INVALID')
            from hermes_cli.dashboard_auth.prefix import resolve_public_url
            public = urlsplit(resolve_public_url())
            if public.hostname and public.hostname.lower() == parsed.hostname.lower():
                raise ValueError('RESOURCE_ORIGIN_CONFIG_INVALID')
            port = parsed.port
            suffix = f':{port}' if port is not None and port != (443 if parsed.scheme == 'https' else 80) else ''
            origin = f'{parsed.scheme}://{parsed.hostname}{suffix}'
            with self._lock:
                self._configured_namespaces = self._configured_namespaces | {parsed.hostname.split('.', 1)[1].lower()}
            return origin
        # Never build a trusted remote domain from forwarded headers or Host.
        browser_host = urlsplit('http://' + request.headers.get('host', ''))
        client = request.scope.get('client')
        try:
            local_client = bool(client and ipaddress.ip_address(client[0]).is_loopback)
        except ValueError:
            local_client = False
        port = browser_host.port if browser_host.port is not None else 80
        if (request.url.scheme != 'http' or browser_host.hostname not in ('localhost', '127.0.0.1', '::1')
                or browser_host.username or browser_host.password or browser_host.path or browser_host.query
                or browser_host.fragment or not local_client or not 1 <= port <= 65535):
            raise ValueError('RESOURCE_ORIGIN_UNAVAILABLE')
        suffix = f':{port}' if port != 80 else ''
        return f'http://p-{token}.localhost{suffix}'

    def recognizes(self, host):
        name = host.split(':', 1)[0].lower()
        # Reserve capability-shaped hosts even before first registration (or
        # after restart); unknown grants must not fall into the normal router.
        if TOKEN.fullmatch(name.split('.', 1)[0]):
            return True
        if name.endswith('.localhost') and name.startswith('p-'):
            return True
        return name.startswith('p-') and any(name.endswith('.' + suffix) for suffix in self._configured_namespaces)

    def open(self, request, data, *, identity, invoke, current, template=''):
        if not isinstance(data, dict) or set(data) != {'document_id', 'grant_id', 'entry_path', 'expires_at'}:
            raise ValueError('RESOURCE_REQUEST_INVALID')
        for key in ('document_id', 'grant_id'):
            if not isinstance(data[key], str) or not re.fullmatch(r'[A-Za-z0-9_-]{16,256}', data[key]):
                raise ValueError('RESOURCE_REQUEST_INVALID')
        entry = resource_path(data['entry_path'])
        expiry = data['expires_at']
        if type(expiry) not in (int, float) or not self.clock() < expiry <= self.clock() + 300:
            raise ValueError('RESOURCE_REQUEST_INVALID')
        if not current():
            raise ValueError('RESOURCE_REVOKED')
        token = secrets.token_hex(24)
        origin = self._origin(request, token, template)
        record = Presentation(origin=origin, configured_origin=bool(template), document_id=data['document_id'],
                              grant_id=data['grant_id'], entry_path=entry, expires_at=expiry,
                              identity=identity, invoke=invoke, current=current)
        with self._lock:
            candidates = tuple(self._records.items())
        retired = [(key, value) for key, value in candidates
                   if value.expires_at <= self.clock() or not value.current()]
        with self._lock:
            for key, value in retired:
                if self._records.get(key) is value:
                    del self._records[key]
            if len(self._records) >= 32:
                raise ValueError('RESOURCE_BUSY')
            self._records['p-' + token] = record
        return {'url': origin + WRAPPER_PATH, 'handle_id': 'p-' + token, 'expires_at': expiry}

    def close(self, handle, identity):
        with self._lock:
            record = self._records.get(handle)
            if record is None:
                return
            if record.identity != identity:
                raise ValueError('RESOURCE_REVOKED')
            del self._records[handle]
        # Revocation is effective locally before potentially failing owner I/O.
        record.invoke({'document_id': record.document_id, 'grant_id': record.grant_id}, close=True)

    def _live(self, key, record):
        with self._lock:
            same = self._records.get(key) is record
        return same and record.expires_at > self.clock() and record.current()

    async def fetch(self, host, scheme, method, path, query, accept):
        key = host.split(':', 1)[0].split('.', 1)[0]
        with self._lock:
            record = self._records.get(key)
        # Configured HTTPS is the public origin even behind a plain HTTP TLS
        # terminator. Host AND effective port must match trusted configuration;
        # no forwarded header can select a resource origin.
        expected = urlsplit(record.origin) if record else None
        actual = urlsplit(f'{expected.scheme if expected else scheme}://{host}')
        matches = (expected is not None and actual.hostname == expected.hostname
                   and (actual.port if actual.port is not None else (443 if expected.scheme == 'https' else 80))
                   == (expected.port if expected.port is not None else (443 if expected.scheme == 'https' else 80))
                   and not actual.username and not actual.password
                   and not actual.path and not actual.query and not actual.fragment
                   and (record.configured_origin or scheme == expected.scheme))
        if not matches or not await asyncio.to_thread(self._live, key, record):
            return Response('Resource unavailable', status_code=410, headers={'Cache-Control': 'no-store'})
        if method not in ('GET', 'OPTIONS'):
            return Response(status_code=405, headers=presentation_headers(record.origin))
        if method == 'OPTIONS':
            return Response(status_code=204, headers=presentation_headers(record.origin))
        if path == WRAPPER_PATH and not query:
            return Response(wrapper_document(record.origin, record.entry_path), media_type='text/html',
                            headers=presentation_headers(record.origin, wrapper=True))
        resource_path(path)
        if not path.startswith(NATIVE_PATH.match(record.entry_path).group()) or len(query) > 65536 or len(accept) > 1024:
            raise ValueError('RESOURCE_PATH_DENIED')
        slot = lease = None
        try:
            slot = await self._native_slot.acquire(1)
            lease = await buffers.acquire(MAX_WIRE + MAX_BODY)
            if not await asyncio.to_thread(self._live, key, record):
                raise ValueError('RESOURCE_REVOKED')
            try:
                response = await plugin_io(record.invoke, {
                    'document_id': record.document_id, 'grant_id': record.grant_id,
                    'path': unquote(path, errors='strict'),
                    'query': parse_qsl(query, keep_blank_values=True, max_num_fields=256, errors='strict'),
                    'accept': accept})
            finally:
                slot.release()
                slot = None
            if not await asyncio.to_thread(self._live, key, record):
                raise ValueError('RESOURCE_REVOKED')
            from hermes_cli.plugin_buffered_response import AdmittedPluginResponse
            decoded = await plugin_io(decode_resource_response, response, record.origin, path)
            lease.shrink(len(decoded.body))
            result = AdmittedPluginResponse(decoded, lease.release, current=lambda: self._live(key, record))
            lease = None
            return result
        except PluginHostBusy:
            return JSONResponse({'error': 'PLUGIN_NOT_FORWARDED_BUSY', 'forwarded': False},
                                status_code=429, headers=presentation_headers(record.origin))
        finally:
            if slot is not None:
                slot.release()
            if lease is not None:
                lease.release()


presentations = ResourcePresentations()


class PluginResourceMiddleware:
    """Outermost: a resource hostname can never reach Hermes authentication/SPA."""
    def __init__(self, app, *, registry=presentations):
        self.app = app
        self.registry = registry
        from hermes_cli.dashboard_auth.prefix import _load_dashboard_section
        self.registry.reserve_template(_load_dashboard_section().get('resource_origin_template', ''))

    async def __call__(self, scope, receive, send):
        if scope['type'] not in ('http', 'websocket'):
            return await self.app(scope, receive, send)
        headers = dict(scope.get('headers', []))
        host = headers.get(b'host', b'').decode('latin-1').lower()
        if not self.registry.recognizes(host):
            return await self.app(scope, receive, send)
        raw_path = scope.get('raw_path', scope.get('path', '').encode())
        query = scope.get('query_string', b'')
        method, scheme = scope.get('method', ''), scope.get('scheme', '')
        accept = headers.get(b'accept', b'').decode('latin-1')
        # Uvicorn retains this original scope for access logs, including query.
        # No resource credential, query value, Cookie or bearer reaches inner middleware.
        scope.update(path='/__plugin_resource__', raw_path=b'/__plugin_resource__', query_string=b'',
                     headers=[(b'host', b'plugin-resource.invalid')])
        if scope['type'] == 'websocket':
            return await send({'type': 'websocket.close', 'code': 1008})
        try:
            raw_path = raw_path.decode('ascii', errors='strict')
            query = query.decode('ascii', errors='strict')
            response = await self.registry.fetch(host, scheme, method, raw_path, query, accept)
        except Exception:  # health: allow BLE001 -- resource boundary redacts arbitrary backend exceptions that may contain grants or credentials
            # Never put a grant, native query or backend exception on the wire/log.
            response = Response('Resource unavailable', status_code=410, headers={'Cache-Control': 'no-store'})
        await response(scope, receive, send)


async def registration(request, name, target, context, resolver, path):
    """Run only after the normal authenticated/profile-scoped plugin route gates."""
    if not isinstance(context, dict) or not isinstance(context.get('selection'), dict):
        return JSONResponse({'error': 'RESOURCE_SELECTION_REQUIRED'}, status_code=403)
    host, dashboard_dir, api_file = target
    from hermes_cli.plugin_host_http_auth import admitted_for_sensitive_route
    authenticated = admitted_for_sensitive_route(request)
    from hermes_cli.plugin_policy_incarnation import policy_incarnation
    from hermes_cli.plugin_request_context import _AUTHORITY
    from hermes_cli.plugin_selection_authority import Selection
    from hermes_cli.dashboard_auth.prefix import _load_dashboard_section
    try:
        def capture():
            manifest_path = Path(dashboard_dir) / 'manifest.json'
            if manifest_path.stat().st_size > 65536:
                raise ValueError('RESOURCE_UNSUPPORTED')
            manifest = json.loads(manifest_path.read_text(encoding='utf-8-sig'))
            if (type(manifest.get('resource_presentation')) is not int or manifest['resource_presentation'] != 1
                    or type(manifest.get('request_context')) is not int or manifest['request_context'] != 1):
                raise ValueError('RESOURCE_UNSUPPORTED')
            return (policy_incarnation(context['home'], dashboard_dir, api_file),
                    host._generation, _load_dashboard_section().get('resource_origin_template', ''))
        incarnation, generation, template = await asyncio.to_thread(capture)
        selected = Selection(**context['selection'])
        identity = (name, selected.principal, selected.client, selected.scope_id, selected.epoch,
                    selected.connection, selected.home)
        captured = contextvars.copy_context()

        def current():
            def check():
                return (_AUTHORITY.current(selected) and host.alive and host._generation == generation
                        and resolver(name) == target
                        and policy_incarnation(context['home'], dashboard_dir, api_file) == incarnation)
            return captured.copy().run(check)

        def invoke(body, *, close=False):
            def call():
                if not close and not current():
                    raise ValueError('RESOURCE_REVOKED')
                result = host.asgi_request(name, dashboard_dir, api_file, 'POST',
                                           '/console/resource/close' if close else '/console/resource', '',
                                           [('content-type', 'application/json')],
                                           json.dumps(body, separators=(',', ':')).encode(),
                                           trusted_context=context, authenticated=authenticated)
                raw = result['body']
                if not isinstance(raw, bytes) or len(raw) > MAX_WIRE or not 200 <= result['status'] < 300:
                    raise ValueError('RESOURCE_UNAVAILABLE')
                envelope = json.loads(raw)
                if not isinstance(envelope, dict) or envelope.get('api_version') != 1:
                    raise ValueError('RESOURCE_RESPONSE_INVALID')
                return envelope.get('data')
            return captured.copy().run(call)

        body = bytearray()
        async for chunk in request.stream():
            if len(body) + len(chunk) > 8192:
                raise ValueError('RESOURCE_REQUEST_INVALID')
            body.extend(chunk)
        data = json.loads(body)
        if path == '_host/resource/close':
            if not isinstance(data, dict) or set(data) != {'handle_id'} or not isinstance(data['handle_id'], str):
                raise ValueError('RESOURCE_REQUEST_INVALID')
            await asyncio.to_thread(presentations.close, data['handle_id'], identity)
            return JSONResponse({'closed': True}, headers={'Cache-Control': 'no-store'})
        result = await asyncio.to_thread(presentations.open, request, data, identity=identity, invoke=invoke,
                                        current=current, template=template)
        return JSONResponse(result, headers={'Cache-Control': 'no-store'})
    except Exception as error:  # health: allow BLE001 -- arbitrary plugin failures become a bounded public error code, never a raw exception
        code = str(error)
        if not re.fullmatch(r'RESOURCE_[A-Z_]+', code):
            code = 'RESOURCE_UNAVAILABLE'
        return JSONResponse({'error': code}, status_code=400, headers={'Cache-Control': 'no-store'})
