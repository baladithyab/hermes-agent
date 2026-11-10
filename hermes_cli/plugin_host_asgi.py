"""Loop-owned dashboard lifecycle admission for the isolated plugin host."""
import asyncio
import importlib.util
import sys
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class _Slot:
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    epoch: int = 0
    unloading: int = 0
    failed: bool = False
    apps: dict[str, tuple[Any, Any]] = field(default_factory=dict)


class DashboardLifetimes:
    """All methods run on the child's single asyncio loop, never under RPC locks."""

    def __init__(self):
        self.slots: dict[str, _Slot] = {}

    @asynccontextmanager
    async def request(self, plugin: str, api_path: Path):
        slot = self.slots.setdefault(plugin, _Slot())
        epoch = slot.epoch
        if slot.failed:
            raise RuntimeError('dashboard shutdown failed; host restart required')
        if slot.unloading:
            raise RuntimeError('dashboard unloading')
        async with slot.lock:
            if slot.unloading or slot.epoch != epoch:
                raise RuntimeError('stale dashboard request')
            key = str(api_path)
            if key not in slot.apps:
                from fastapi import FastAPI
                module_name = f'hermes_dashboard_plugin_{plugin}'
                spec = importlib.util.spec_from_file_location(module_name, api_path)
                if spec is None or spec.loader is None:
                    raise ImportError(f'cannot load dashboard api {api_path}')
                module = importlib.util.module_from_spec(spec)
                sys.modules[module_name] = module
                try:
                    spec.loader.exec_module(module)
                    router = getattr(module, 'router', None)
                    if router is None:
                        raise AttributeError(f'dashboard api {api_path.name} has no router')
                    app = FastAPI()
                    app.include_router(router)
                    lifespan = app.router.lifespan_context(app)
                    await asyncio.wait_for(lifespan.__aenter__(), timeout=3)
                except BaseException:
                    if sys.modules.get(module_name) is module:
                        sys.modules.pop(module_name, None)
                    raise
                slot.apps[key] = (app, lifespan)
            yield slot.apps[key][0]

    async def unload(self, plugin: str) -> list[str]:
        slot = self.slots.get(plugin)
        if slot is None:
            return []
        # Invalidate queued requests before waiting for the current request. No
        # re-entrant parent callback needs this loop-owned admission lock.
        slot.epoch += 1
        slot.unloading += 1
        errors = []
        try:
            async with slot.lock:
                apps, slot.apps = slot.apps, {}
                for _, lifespan in apps.values():
                    try:
                        await asyncio.wait_for(lifespan.__aexit__(None, None, None), timeout=3)
                    except Exception as exc:
                        slot.failed = True
                        errors.append(f'{type(exc).__name__}: {exc}')
                sys.modules.pop(f'hermes_dashboard_plugin_{plugin}', None)
        finally:
            slot.unloading -= 1
        return errors
