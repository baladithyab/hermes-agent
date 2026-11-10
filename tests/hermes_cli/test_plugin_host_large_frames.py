"""Per-call large RPC admission must not enlarge unrelated calls or log payloads."""
import io
import json
import os
import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager

import pytest

from hermes_cli.plugin_api_limits import FrameLimits, MAX_FRAME_BYTES, MIB
from hermes_cli.plugin_host_wire import Channel, PluginHostBusy, PluginHostUnavailable


@contextmanager
def channels(handler):
    a_read, b_write = os.pipe()
    b_read, a_write = os.pipe()
    a = Channel(os.fdopen(a_read, 'rb', buffering=0), os.fdopen(a_write, 'wb', buffering=0),
                lambda *args: None, name='parent').start()
    b = Channel(os.fdopen(b_read, 'rb', buffering=0), os.fdopen(b_write, 'wb', buffering=0), handler,
                name='child', incoming_limits=lambda method, params:
                FrameLimits(MAX_FRAME_BYTES, MAX_FRAME_BYTES) if method == 'large' else FrameLimits()).start()
    try:
        yield a, b
    finally:
        a.close()
        b.close()
        a._thread.join(2)
        b._thread.join(2)


def test_large_admission_does_not_block_small_reentrant_work_and_releases_on_completion():
    entered = [threading.Event(), threading.Event()]
    release = threading.Event()
    def handler(method, params, origin):
        if method == 'large' and 'index' in params:
            entered[params['index']].set()
            assert release.wait(5)
        return params
    with channels(handler) as (parent, _), ThreadPoolExecutor(max_workers=2) as pool:
        limits = FrameLimits(MAX_FRAME_BYTES, MAX_FRAME_BYTES)
        pending = [pool.submit(parent.call, 'large', {'index': index}, frame_limits=limits, timeout=5) for index in range(2)]
        try:
            assert all(event.wait(3) for event in entered)
            with pytest.raises(PluginHostBusy, match='not forwarded'):
                parent.call('large', {'body': 'third'}, frame_limits=limits, timeout=1)
            assert parent.call('ordinary', {'ok': True}, timeout=3) == {'ok': True}
        finally:
            release.set()
        assert [future.result(timeout=3) for future in pending] == [{'index': 0}, {'index': 1}]
        assert parent.call('large', {'body': 'next'}, frame_limits=limits, timeout=3) == {'body': 'next'}


def test_large_permission_is_correlated_to_the_original_response_id():
    inbound_read, peer_write = os.pipe()
    peer_read, outbound_write = os.pipe()
    peer_reader = os.fdopen(peer_read, 'rb')
    peer_writer = os.fdopen(peer_write, 'wb')
    parent = Channel(os.fdopen(inbound_read, 'rb', buffering=0), os.fdopen(outbound_write, 'wb', buffering=0),
                     lambda *args: None, name='parent').start()
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            large = pool.submit(parent.call, 'large', {}, timeout=10,
                                frame_limits=FrameLimits(MAX_FRAME_BYTES, MAX_FRAME_BYTES))
            large_request = json.loads(peer_reader.readline())
            ordinary = pool.submit(parent.call, 'ordinary', {}, timeout=10)
            ordinary_request = json.loads(peer_reader.readline())
            assert ordinary_request['id'] != large_request['id']
            # A hostile peer sends a large result for the SMALL call while the
            # large call is pending. A global elevated cap would accept it.
            payload = json.dumps({'id': ordinary_request['id'], 'result': 'x' * (9 * MIB)}).encode() + b'\n'
            peer_writer.write(payload)
            peer_writer.flush()
            with pytest.raises(PluginHostUnavailable):
                ordinary.result(timeout=5)
            with pytest.raises(PluginHostUnavailable):
                large.result(timeout=5)
    finally:
        peer_writer.close()
        peer_reader.close()
        parent.close()
        parent._thread.join(2)


def test_undeclared_incoming_request_cannot_name_its_own_limit():
    dispatched = []
    request = {'id': 1, 'method': 'ordinary', 'params': {
        'body': 'x' * (9 * MIB), 'frame_limit': MAX_FRAME_BYTES}}
    channel = Channel(io.BytesIO(json.dumps(request).encode() + b'\n'), io.BytesIO(),
                      lambda *args: dispatched.append(args), name='untrusted')
    channel.start()
    channel._thread.join(3)
    assert channel.closed_reason is not None
    assert dispatched == []
    channel.close()


def test_physical_limit_rejects_an_unterminated_frame_without_dispatch_or_secret_logs(caplog):
    dispatched = []
    data = b'generated-provider-secret' + b'x' * MAX_FRAME_BYTES
    channel = Channel(io.BytesIO(data), io.BytesIO(), lambda *args: dispatched.append(args), name='physical')
    channel.start()
    channel._thread.join(3)
    assert channel.closed_reason is not None
    assert dispatched == []
    assert 'generated-provider-secret' not in caplog.text
    channel.close()


def test_malformed_frame_is_not_logged_verbatim(caplog):
    channel = Channel(io.BytesIO(b'{generated-provider-secret\n'), io.BytesIO(), lambda *args: None, name='malformed')
    channel.start()
    channel._thread.join(3)
    assert channel.closed_reason is not None
    assert 'generated-provider-secret' not in caplog.text
    assert 'malformed frame' in caplog.text
    channel.close()


def test_reply_publication_does_not_leave_large_invocation_admission_held(monkeypatch):
    release = threading.Event()
    with channels(lambda method, params, origin: params) as (parent, child):
        send = child._send
        publications = 0
        def delayed_cleanup(message, **kwargs):
            nonlocal publications
            send(message, **kwargs)
            publications += 1
            if publications <= 2:
                assert release.wait(5)
        monkeypatch.setattr(child, '_send', delayed_cleanup)
        limits = FrameLimits(MAX_FRAME_BYTES, MAX_FRAME_BYTES)
        try:
            for index in range(3):
                assert parent.call('large', {'index': index}, frame_limits=limits, timeout=3) == {'index': index}
            assert parent.closed_reason is None
            assert child.closed_reason is None
        finally:
            release.set()
