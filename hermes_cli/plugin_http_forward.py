"""Shared admission and authority for every profile's hosted plugin HTTP path."""
from __future__ import annotations

import asyncio
import json

from fastapi.responses import JSONResponse


async def forward_hosted_plugin(request, plugin, target, path, resolver, *, query=None, headers=None):
    """Preserve route-local authentication and bind authority before reading a body.

    The caller has resolved the requesting profile and checked plugin consent.
    Secondary profiles use this same path, retaining their credential scrubbing.
    """
    from hermes_cli.plugin_api_limits import load_plugin_api_limits
    from hermes_cli.plugin_host_http_auth import admitted_for_sensitive_route
    from hermes_cli.plugin_host_wire import PluginHostBusy
    from hermes_cli.plugin_http_admission import buffers, receipt_buffers
    from hermes_cli.plugin_request_context import authenticated_context, bind_selection

    name = plugin['name']
    _host, dashboard_dir, api_file = target
    context = authenticated_context(request) if plugin.get('_trusted_context') else None
    authenticated = admitted_for_sensitive_route(request)
    if context is not None and path in ('_host/selection', '_host/deselection') and request.method == 'POST':
        selected = bind_selection(request, context, select_current=True,
                                  deselect=path == '_host/deselection')['selection']
        return JSONResponse({'scope_id': selected['scope_id'], 'epoch': selected['epoch']},
                            headers={'Cache-Control': 'no-store'})
    if context is not None and request.headers.get('X-Hermes-Plugin-Client'):
        context = bind_selection(request, context)
    if path in ('_host/resource/open', '_host/resource/close'):
        if request.method != 'POST':
            return JSONResponse({'error': 'RESOURCE_METHOD_DENIED'}, status_code=405)
        from hermes_cli.plugin_resource_presentation import registration
        return await registration(request, name, target, context, resolver, path)

    limits = (await asyncio.to_thread(load_plugin_api_limits, dashboard_dir, api_file, context)).for_path('/' + path)
    lease = None
    try:
        if limits.frames.extended:
            admission = receipt_buffers if path == 'console/result' else buffers
            lease = await admission.acquire(limits.request_bytes + limits.response_bytes)
        response = await _forward_body(request, path, target, name, context, limits, authenticated,
                                       query=query, headers=headers)
        if lease is not None:
            from hermes_cli.plugin_buffered_response import AdmittedPluginResponse
            lease.shrink(len(response.body))
            result = AdmittedPluginResponse(response, lease.release)
            lease = None
            return result
        return response
    except PluginHostBusy:
        # Only local admission before invocation may promise no effect occurred.
        return JSONResponse({'error': 'PLUGIN_NOT_FORWARDED_BUSY', 'forwarded': False},
                            status_code=429, headers={'Cache-Control': 'no-store'})
    finally:
        if lease is not None:
            lease.release()


async def _forward_body(request, path, target, name, context, limits, authenticated, *, query, headers):
    from hermes_cli.plugin_http_admission import plugin_io
    from hermes_cli.web_server_plugin_profiles import (
        _GATEWAY_CREDENTIAL_HEADERS, hosted_response, run_hosted_request,
    )

    body = bytearray()
    try:
        async with asyncio.timeout(15):
            async for chunk in request.stream():
                if len(body) + len(chunk) > limits.request_bytes:
                    return JSONResponse({'detail': 'Plugin request too large'}, status_code=413)
                body.extend(chunk)
    except TimeoutError:
        return JSONResponse({'detail': 'Plugin request body deadline'}, status_code=408)
    host, dashboard_dir, api_file = target
    query = request.url.query if query is None else query
    headers = ([(k, v) for k, v in request.headers.items() if k.lower() not in _GATEWAY_CREDENTIAL_HEADERS]
               if headers is None else headers)
    args = (name, dashboard_dir, api_file, request.method, '/' + path, query, headers, bytes(body))
    if limits.frames.extended:
        raw = await plugin_io(host.asgi_request, *args, trusted_context=context,
                              authenticated=authenticated, receipt=path == 'console/result')
        response = hosted_response(raw)
    else:
        # Keep stable's per-profile executor, copied ContextVars and total deadline.
        response = await run_hosted_request(host.home, name, lambda timeout: host.asgi_request(
            *args, timeout=timeout, authenticated=authenticated, trusted_context=context))
    if response.status_code == 429:
        try:
            failure = json.loads(response.body)
        except ValueError:
            failure = None
        if (isinstance(failure, dict) and failure.get('error') == 'PLUGIN_NOT_FORWARDED_BUSY'
                and failure.get('forwarded') is False):
            return JSONResponse({'error': 'PLUGIN_REQUEST_FAILED'}, status_code=429)
    return response
