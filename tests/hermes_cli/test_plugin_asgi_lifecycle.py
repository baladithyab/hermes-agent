"""Real child-process dashboard lifecycle admission and deadline regressions."""
import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

from hermes_cli.plugins import PluginManager
from tests.hermes_cli.plugin_host_readiness import warm_host_infrastructure


API = '''
import asyncio
from pathlib import Path
from contextlib import asynccontextmanager
from fastapi import APIRouter
root = Path(__file__).parent
@asynccontextmanager
async def lifespan(app):
    with (root / 'events').open('a') as out: out.write('start\\n')
    try:
        while not (root / 'release-start').exists():
            await asyncio.sleep(.01)
        yield
    finally:
        with (root / 'events').open('a') as out: out.write('stop\\n')
router = APIRouter(lifespan=lifespan)
@router.get('/ping')
async def ping():
    return {'ok': True}
'''


def wait_file(path):
    deadline = time.monotonic() + 5
    while not path.exists() and time.monotonic() < deadline:
        threading.Event().wait(.01)
    assert path.exists()


@pytest.fixture
def hosted(tmp_path, monkeypatch):
    monkeypatch.setenv('HOME', str(tmp_path))
    monkeypatch.setenv('HERMES_HOME', str(tmp_path / 'home'))
    dashboard = tmp_path / 'dashboard'
    dashboard.mkdir()
    (dashboard / 'plugin_api.py').write_text(API)
    host = PluginManager()._plugin_host()
    def ping():
        result = host.asgi_request('lifecycle', str(dashboard), 'plugin_api.py',
                                   'GET', '/ping', '', [], b'')
        return json.loads(result['body'])
    try:
        warm_host_infrastructure(host, tmp_path / 'host-infrastructure-ready')
        assert not (dashboard / 'events').exists()
        assert not (dashboard / 'entered').exists()
        yield host, dashboard, ping
    finally:
        (dashboard / 'release-start').touch()
        host.shutdown()
        assert not host.alive


def test_simultaneous_first_requests_enter_lifespan_once(hosted):
    host, directory, ping = hosted
    with ThreadPoolExecutor(max_workers=8) as pool:
        first = pool.submit(ping)
        wait_file(directory / 'events')
        others = [pool.submit(ping) for _ in range(7)]
        # A separate plugin request is a loop/transport barrier while startup waits.
        assert host._call('hello', {})['pid'] > 0
        (directory / 'release-start').touch()
        assert first.result(timeout=10) == {'ok': True}
        assert [f.result(timeout=10) for f in others] == [{'ok': True}] * 7
    host._unload('lifecycle')
    assert (directory / 'events').read_text() == 'start\nstop\n'


def test_startup_timeout_cleans_up_and_can_retry(hosted):
    host, directory, ping = hosted
    started = time.monotonic()
    with pytest.raises(Exception, match='Timeout|timeout|timed out'):
        ping()
    assert time.monotonic() - started < 8
    wait_file(directory / 'events')
    assert (directory / 'events').read_text() == 'start\nstop\n'
    (directory / 'release-start').touch()
    assert ping() == {'ok': True}
    host._unload('lifecycle')
    assert (directory / 'events').read_text() == 'start\nstop\nstart\nstop\n'


def test_unload_during_startup_drains_then_reloads(hosted):
    host, directory, ping = hosted
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(ping)
        wait_file(directory / 'events')
        # Keep startup gated until unload actually completes its RPC. Startup's
        # bounded cancellation must permit unload without publishing a live app.
        unloading = pool.submit(host._unload, 'lifecycle')
        with pytest.raises(Exception, match='timed out'):
            first.result(timeout=8)
        unloading.result(timeout=8)
    assert (directory / 'events').read_text() == 'start\nstop\n'
    (directory / 'release-start').touch()
    assert ping() == {'ok': True}
    host._unload('lifecycle')
    assert (directory / 'events').read_text() == 'start\nstop\nstart\nstop\n'


def test_shutdown_timeout_reports_error_and_allows_other_plugin(hosted):
    host, directory, ping = hosted
    (directory / 'plugin_api.py').write_text(API.replace(
        "with (root / 'events').open('a') as out: out.write('stop\\n')",
        "await asyncio.sleep(60)"))
    (directory / 'release-start').touch()
    assert ping() == {'ok': True}
    started = time.monotonic()
    result = host._call('unload', {'plugin_key': 'lifecycle'}, timeout=8)
    assert time.monotonic() - started < 8
    assert len(result['errors']) == 1
    assert result['errors'][0].startswith('TimeoutError:')
    assert host._call('hello', {})['pid'] > 0
    with pytest.raises(Exception, match='host restart required'):
        ping()
    # Failed cleanup must not create another authority in the same plugin;
    # unrelated plugins in this host remain available.
    other = directory.parent / 'other'
    other.mkdir()
    (other / 'plugin_api.py').write_text(API)
    (other / 'release-start').touch()
    response = host.asgi_request('other', str(other), 'plugin_api.py',
                                 'GET', '/ping', '', [], b'')
    assert json.loads(response['body']) == {'ok': True}


def test_queued_request_is_invalidated_by_unload(tmp_path):
    import asyncio
    from hermes_cli.plugin_host_asgi import DashboardLifetimes

    (tmp_path / 'plugin_api.py').write_text(API)
    (tmp_path / 'release-start').touch()

    async def exercise():
        lifetimes = DashboardLifetimes()
        entered = asyncio.Event()
        release = asyncio.Event()
        queued = asyncio.Event()
        async def first():
            async with lifetimes.request('probe', tmp_path / 'plugin_api.py'):
                entered.set()
                await release.wait()
        async def stale():
            queued.set()
            async with lifetimes.request('probe', tmp_path / 'plugin_api.py'):
                pytest.fail('queued request survived unload')
        active = asyncio.create_task(first())
        await entered.wait()
        pending = asyncio.create_task(stale())
        await queued.wait()
        closing = asyncio.create_task(lifetimes.unload('probe'))
        # Yield once so unload invalidates admission before releasing the lock.
        await asyncio.sleep(0)
        release.set()
        await active
        with pytest.raises(RuntimeError, match='stale dashboard request'):
            await pending
        assert await closing == []
        assert (tmp_path / 'events').read_text() == 'start\nstop\n'
        async with lifetimes.request('probe', tmp_path / 'plugin_api.py'):
            pass
        assert await lifetimes.unload('probe') == []
    asyncio.run(exercise())
