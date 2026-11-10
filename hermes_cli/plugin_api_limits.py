"""Installed dashboard manifest capability for bounded large HTTP requests.

``request_context: 1`` plus ``api_limits: {request_bytes: N, response_bytes: N,
routes: ["/exact/path"]}`` opts only those installed routes into large forwarding.
Each bound is an integer in 1..48 MiB. HTTP headers never select these limits.
Undeclared HTTP routes retain 1 MiB requests and 8 MiB responses.
``resource_presentation: 1`` separately opts into isolated resource origins and
bounded pre-body admission for their effect-capable GET proxy. It requires
``request_context: 1`` and does not itself increase either body/frame bound.
"""
from __future__ import annotations

import dataclasses
import json
from pathlib import Path
from typing import Any

MIB = 1024 * 1024
DEFAULT_FRAME_BYTES = 8 * MIB
ORDINARY_RESPONSE_FRAME_BYTES = ((8 * MIB + 2) // 3) * 4 + MIB
MAX_API_BODY_BYTES = 48 * MIB
# Base64 of the entire HTTP body, plus a bounded envelope/header allowance.
MAX_FRAME_BYTES = ((MAX_API_BODY_BYTES + 2) // 3) * 4 + MIB


@dataclasses.dataclass(frozen=True)
class FrameLimits:
    request_bytes: int = DEFAULT_FRAME_BYTES
    response_bytes: int = DEFAULT_FRAME_BYTES

    def __post_init__(self):
        for value in (self.request_bytes, self.response_bytes):
            if type(value) is not int or not 1 <= value <= MAX_FRAME_BYTES:
                raise ValueError('invalid plugin frame limit')

    @property
    def extended(self) -> bool:
        return self.request_bytes > DEFAULT_FRAME_BYTES or self.response_bytes > ORDINARY_RESPONSE_FRAME_BYTES


@dataclasses.dataclass(frozen=True)
class PluginApiLimits:
    request_bytes: int = MIB
    response_bytes: int = 8 * MIB
    declared: bool = False
    resource_presentation: bool = False
    routes: tuple[str, ...] = ()

    def for_path(self, path: str) -> PluginApiLimits:
        if path in self.routes:
            if path == '/console/result':
                return dataclasses.replace(self, request_bytes=min(self.request_bytes, MIB))
            return self
        return PluginApiLimits(resource_presentation=self.resource_presentation)

    @property
    def frames(self) -> FrameLimits:
        return FrameLimits(*(
            max(DEFAULT_FRAME_BYTES, ((size + 2) // 3) * 4 + MIB)
            for size in (self.request_bytes, self.response_bytes)
        ))


def parse_plugin_api_limits(manifest: dict[str, Any]) -> PluginApiLimits:
    resource = manifest.get('resource_presentation')
    if resource is not None and (type(resource) is not int or resource != 1
                                or type(manifest.get('request_context')) is not int
                                or manifest['request_context'] != 1):
        raise ValueError('resource_presentation requires version 1 and request_context 1')
    raw = manifest.get('api_limits')
    if raw is None:
        return PluginApiLimits(resource_presentation=resource == 1)
    if (type(manifest.get('request_context')) is not int or manifest['request_context'] != 1
            or not isinstance(raw, dict) or set(raw) != {'request_bytes', 'response_bytes', 'routes'}):
        raise ValueError('api_limits requires request_context 1, body bounds and exact routes')
    for value in (raw['request_bytes'], raw['response_bytes']):
        if type(value) is not int or not 1 <= value <= MAX_API_BODY_BYTES:
            raise ValueError('plugin API body limit must be an integer in 1..48 MiB')
    routes = raw['routes']
    if (not isinstance(routes, list) or not 1 <= len(routes) <= 64
            or any(not isinstance(path, str) or not path.startswith('/') or len(path) > 256
                   or any(char in path for char in '%?#\\*') or '//' in path
                   or any(part in ('.', '..') for part in path.split('/')) for path in routes)
            or len(set(routes)) != len(routes)):
        raise ValueError('api_limits requires unique exact route paths')
    return PluginApiLimits(raw['request_bytes'], raw['response_bytes'], True, resource == 1, tuple(routes))


def load_plugin_api_limits(dashboard_dir: str, api_file: str, trusted_context: Any) -> PluginApiLimits:
    if not isinstance(trusted_context, dict):
        return PluginApiLimits()
    manifest_path = Path(dashboard_dir) / 'manifest.json'
    try:
        with manifest_path.open('rb') as stream:
            raw = stream.read(64 * 1024 + 1)
    except FileNotFoundError:
        return PluginApiLimits()
    if len(raw) > 64 * 1024:
        raise ValueError('dashboard manifest too large')
    manifest = json.loads(raw.decode('utf-8-sig'))
    if not isinstance(manifest, dict) or manifest.get('api') != api_file:
        raise ValueError('dashboard manifest API changed')
    return parse_plugin_api_limits(manifest)


def asgi_frame_limits(method: str, params: dict[str, Any]) -> FrameLimits:
    """Child-side admission. Only the parent's typed ASGI call gets an allowance."""
    if method != 'asgi':
        return FrameLimits()
    return load_plugin_api_limits(str(params.get('dashboard_dir', '')),
                                 str(params.get('api_file', '')),
                                 params.get('trusted_context')).for_path(str(params.get('path', ''))).frames
