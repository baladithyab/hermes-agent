"""Real pipe backpressure must not consume the RPC deadline indefinitely."""
import os
import subprocess
import sys
import threading
import time
from types import SimpleNamespace

import pytest

from hermes_cli.plugin_host_wire import Channel, PluginHostDeadline


def test_request_deadline_includes_blocked_pipe_write():
    read_fd, write_fd = os.pipe()
    reader = os.fdopen(read_fd, 'rb', buffering=0)
    writer = os.fdopen(write_fd, 'wb', buffering=0)
    channel = Channel(reader, writer, lambda *args: None, name='blocked')
    result = []
    finished = threading.Event()

    def request():
        started = time.monotonic()
        try:
            channel.call('large', {'body': 'x' * 1024 * 1024}, timeout=.1)
        except BaseException as exc:
            result.append((time.monotonic() - started, exc))
        finally:
            finished.set()

    thread = threading.Thread(target=request, daemon=True)
    thread.start()
    try:
        assert finished.wait(2), 'RPC timeout did not cover blocked pipe write'
        assert result and result[0][0] < 2
        assert isinstance(result[0][1], PluginHostDeadline)
        assert not channel._pending
    finally:
        reader.close()
        thread.join(2)
        channel.close()


def test_oversized_frame_rejected_before_dispatch():
    read_fd, write_fd = os.pipe()
    reader = os.fdopen(read_fd, 'rb', buffering=0)
    writer = os.fdopen(write_fd, 'wb', buffering=0)
    channel = Channel(reader, writer, lambda *args: None, name='oversized')
    try:
        with pytest.raises(ValueError, match='frame'):
            channel.call('large', {'body': 'x' * (9 * 1024 * 1024)}, timeout=.1)
        assert not channel._pending
    finally:
        reader.close()
        channel.close()


@pytest.mark.platforms('posix')
def test_partial_write_deadline_reaps_captured_process_and_refuses_replay(tmp_path, monkeypatch):
    from hermes_cli.plugin_host import PluginHost
    from hermes_cli.plugin_host_wire import PluginHostUnavailable

    process = subprocess.Popen(
        [sys.executable, '-c', "import os,threading; os.write(1,b'ready\\n'); threading.Event().wait(60)"],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, bufsize=0,
    )
    host = PluginHost(SimpleNamespace(home_path=tmp_path))
    try:
        assert process.stdout.readline() == b'ready\n'
        host._proc = process
        host._generation = 1
        host._channel = Channel(
            process.stdout, process.stdin, lambda *args: None, name='captured-blocked',
            on_close=lambda reason: host._on_channel_close(1, reason),
        ).start()
        spawns = []
        monkeypatch.setattr(host, '_argv', lambda: spawns.append('unexpected replacement'))
        with pytest.raises(PluginHostDeadline):
            host._call('large', {'body': 'x' * 1024 * 1024}, timeout=.1)
        assert process.returncode is not None
        assert process.wait(timeout=0) == process.returncode
        assert host._generation == 2 and not host.alive
        assert not host._channel._pending
        with pytest.raises(PluginHostUnavailable, match='deadline'):
            host.ensure_started()
        with pytest.raises(PluginHostUnavailable, match='deadline'):
            host._call('large', {'body': 'must not be replayed'}, timeout=.1)
        assert host._proc is process and spawns == []
    finally:
        if process.poll() is None:
            process.kill()
        process.wait(timeout=3)
        host.shutdown()
