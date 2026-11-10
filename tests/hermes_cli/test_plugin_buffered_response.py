import asyncio
import threading

import pytest
from starlette.background import BackgroundTask
from starlette.responses import Response

from hermes_cli.plugin_buffered_response import AdmittedPluginResponse


@pytest.mark.parametrize('guarded', [False, True])
def test_admission_is_held_until_delivery_and_released_on_disconnect(guarded):
    async def run():
        slot = threading.BoundedSemaphore(1)
        assert slot.acquire(blocking=False)
        releases = []
        def release():
            releases.append(True)
            slot.release()
        response = AdmittedPluginResponse(Response(b'bounded provider bytes'), release,
                                          current=(lambda: True) if guarded else None)
        started = asyncio.Event()
        blocked = asyncio.Event()
        async def send(message):
            if message['type'] == 'http.response.body':
                started.set()
                await blocked.wait()
        async def receive():
            return {'type': 'http.disconnect'}
        task = asyncio.create_task(response({'type': 'http'}, receive, send))
        await started.wait()
        assert not slot.acquire(blocking=False)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert releases == [True]
        assert slot.acquire(blocking=False)
    asyncio.run(run())


def test_revalidation_prevents_stale_bytes_before_response_start():
    async def run():
        sent = []
        releases = []
        response = AdmittedPluginResponse(Response(b'provider-secret'), lambda: releases.append(True), current=lambda: False)
        async def send(message):
            sent.append(message)
        async def receive():
            return {'type': 'http.request', 'body': b'', 'more_body': False}
        await response({'type': 'http'}, receive, send)
        assert sent[0]['status'] == 410
        assert all(b'provider-secret' not in message.get('body', b'') for message in sent)
        assert releases == [True]
    asyncio.run(run())


def test_byte_admission_cancellation_and_queue_bound_do_not_occupy_executor():
    from hermes_cli.plugin_http_admission import BufferAdmission
    from hermes_cli.plugin_host_wire import PluginHostBusy

    async def run():
        admission = BufferAdmission(capacity=10, max_waiters=1)
        first = await admission.acquire(10)
        started = asyncio.Event()
        async def waiting():
            started.set()
            return await admission.acquire(10)
        pending = asyncio.create_task(waiting())
        await started.wait()
        with pytest.raises(PluginHostBusy):
            await admission.acquire(1)
        pending.cancel()
        with pytest.raises(asyncio.CancelledError):
            await pending
        assert admission.used == 10
        first.shrink(4)
        second = await admission.acquire(6)
        assert admission.used == 10
        first.release()
        second.release()
        assert admission.used == 0
    asyncio.run(run())


def test_unread_delivery_times_out_and_releases_buffer():
    async def run():
        released = []
        response = AdmittedPluginResponse(Response(b'provider-secret'), lambda: released.append(True), delivery_timeout=0.01)
        async def send(message):
            await asyncio.Event().wait()
        async def receive():
            return {'type': 'http.disconnect'}
        with pytest.raises(TimeoutError):
            await response({'type': 'http'}, receive, send)
        assert released == [True]
        assert response.body == b''
    asyncio.run(run())


def test_listener_accepts_native_query_budget_without_unbounded_headers(monkeypatch):
    import h11
    from hermes_cli import web_server

    monkeypatch.setattr(web_server.app.state, 'auth_required', False, raising=False)
    configuration, _server = web_server._build_uvicorn_server('127.0.0.1', 8642)
    parser = h11.Connection(h11.SERVER, max_incomplete_event_size=configuration.h11_max_incomplete_event_size)
    data = b'GET /v0/resource/plugins/demo/index.html?value=' + b'x' * (65536 - 6) + b' HTTP/1.1\r\nHost: p-resource.localhost\r\n\r\n'
    event = None
    for offset in range(0, len(data), 1024):
        parser.receive_data(data[offset:offset + 1024])
        event = parser.next_event()
    assert isinstance(event, h11.Request)
    assert len(event.target.split(b'?', 1)[1]) == 65536
    oversized = h11.Connection(h11.SERVER, max_incomplete_event_size=configuration.h11_max_incomplete_event_size)
    oversized.receive_data(b'GET / HTTP/1.1\r\nX-Padding: ' + b'x' * (configuration.h11_max_incomplete_event_size + 1))
    with pytest.raises(h11.RemoteProtocolError):
        oversized.next_event()


@pytest.mark.parametrize('guarded', [False, True])
@pytest.mark.parametrize('size', [0, 3 * 65536 + 17])
def test_large_response_uses_bounded_chunks_and_preserves_exact_bytes(guarded, size):
    async def run():
        body = b'x' * size
        messages = []
        released = []
        completed = []
        async def background():
            assert not released
            completed.append(True)
        original = Response(body, background=BackgroundTask(background))
        response = AdmittedPluginResponse(original, lambda: released.append(True),
                                          current=(lambda: True) if guarded else None)
        async def send(message):
            assert not released
            messages.append(message)
        async def receive():
            return {'type': 'http.disconnect'}
        await response({'type': 'http'}, receive, send)
        chunks = [message for message in messages if message['type'] == 'http.response.body']
        assert all(len(message['body']) <= 65536 for message in chunks)
        assert b''.join(message['body'] for message in chunks) == body
        assert all(message['more_body'] for message in chunks[:-1])
        assert chunks[-1]['more_body'] is False
        assert released == [True]
        assert completed == [True]
        assert response.body == b''
    asyncio.run(run())
