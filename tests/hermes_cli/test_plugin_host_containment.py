"""Hard RPC deadlines contain real non-cooperative plugin subprocesses."""
import time

import pytest

from hermes_cli.plugin_host_wire import PluginHostUnavailable
from tests.hermes_cli import test_plugin_asgi_lifecycle as lifecycle

hosted = lifecycle.hosted


FAULT_API = '''
import asyncio
import time
from pathlib import Path
from contextlib import asynccontextmanager
from fastapi import APIRouter
root = Path(__file__).parent
MODE = {mode!r}
def entered():
    with (root / 'entered').open('a') as out: out.write('once\\n')
async def stuck():
    entered()
    while True:
        try:
            await asyncio.sleep(60)
        except asyncio.CancelledError:
            pass
if MODE == 'import':
    entered()
    time.sleep(60)
@asynccontextmanager
async def lifespan(app):
    if MODE == 'startup':
        await stuck()
    yield
    if MODE == 'shutdown':
        await stuck()
router = APIRouter(lifespan=lifespan)
@router.get('/ping')
async def ping():
    if MODE == 'request':
        await stuck()
    return {{'ok': True}}
'''


@pytest.mark.parametrize('mode', ['import', 'startup', 'request', 'shutdown'])
def test_deadline_reaps_only_captured_host_and_never_replays(hosted, mode):
    host, directory, ping = hosted
    (directory / 'plugin_api.py').write_text(FAULT_API.format(mode=mode))
    process = host._proc
    if mode == 'shutdown':
        assert ping() == {'ok': True}
    started = time.monotonic()
    try:
        with pytest.raises(TimeoutError):
            if mode == 'shutdown':
                host._unload('lifecycle')
            else:
                ping()
        assert time.monotonic() - started < 14
        assert process.poll() is not None, 'deadline left non-cooperative child running'
        assert not host.alive
        assert (directory / 'entered').read_text() == 'once\n'
        # A timeout is ambiguous, not permission to respawn and replay a write.
        with pytest.raises(PluginHostUnavailable, match='deadline'):
            ping()
        assert host.pid == process.pid
        assert (directory / 'entered').read_text() == 'once\n'
    finally:
        # Even the red run must not leave the deliberately resistant child alive.
        if process.poll() is None:
            process.kill()
        process.wait(timeout=3)


def test_late_deadline_cannot_kill_replacement(hosted):
    from hermes_cli.plugin_host_containment import contain_deadline
    host, directory, ping = hosted
    (directory / 'release-start').touch()
    old = host._channel
    host.shutdown()
    host.ensure_started()
    replacement = host._proc
    contain_deadline(host, old)
    assert host._proc is replacement and host.alive
    assert ping() == {'ok': True}


def test_remote_timeout_is_not_transport_failure(hosted):
    host, directory, ping = hosted
    (directory / 'plugin_api.py').write_text('''
from fastapi import APIRouter
router = APIRouter()
@router.get('/ping')
async def ping():
    raise TimeoutError('upstream read timed out')
''')
    process = host._proc
    with pytest.raises(TimeoutError):
        ping()
    assert host.alive and host._proc is process
    assert host._call('hello', {})['pid'] == process.pid
