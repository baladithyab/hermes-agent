"""Real pipe backpressure must not consume the RPC deadline indefinitely."""
import os
import threading
import time

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
