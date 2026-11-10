"""Owned loopback model, real clarify tool, native card, two authenticated sockets."""
import asyncio
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import threading
import time
import sys
import traceback


STARTED_AT = time.monotonic()
LAST_PHASE = None


def phase(name, **fields):
    global LAST_PHASE
    LAST_PHASE = name
    print('hermes_clarify_phase ' + json.dumps({'phase': name, 'pid': os.getpid(),
          'elapsed': round(time.monotonic() - STARTED_AT, 3), **fields}), file=sys.stderr, flush=True)


class OwnedModelServer(ThreadingHTTPServer):
    """Capture each fixture handler so cleanup can positively join it."""
    daemon_threads = True
    block_on_close = False

    def __init__(self, *args):
        self.handler_threads = []
        self.handler_lock = threading.Lock()
        super().__init__(*args)

    def process_request(self, request, client_address):
        thread = threading.Thread(target=self.process_request_thread,
                                  args=(request, client_address), daemon=True)
        with self.handler_lock:
            self.handler_threads.append(thread)
        thread.start()


class ModelPeer(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b'{"data": []}')

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
        messages = body.get('messages', [])
        self.server.requests.append(messages)
        asking = bool(messages) and not any(m['role'] == 'tool' for m in messages)
        message = {'role': 'assistant', 'content': 'CLARIFY_FINISHED'}
        if asking:
            message = {'role': 'assistant', 'content': None, 'tool_calls': [{
                'index': 0, 'id': 'owned-clarify', 'type': 'function', 'function': {
                    'name': 'clarify', 'arguments': json.dumps({'questions': [
                        {'question': 'Pick a color', 'choices': ['blue', 'green']}]})}}]}
        self.send_response(200)
        self.send_header('Content-Type', 'text/event-stream' if body.get('stream') else 'application/json')
        self.end_headers()
        if body.get('stream'):
            for delta, finish in [(message, None), ({}, 'tool_calls' if asking else 'stop')]:
                frame = {'id': 'local', 'object': 'chat.completion.chunk', 'created': 1,
                         'model': 'local', 'choices': [{'index': 0, 'delta': delta, 'finish_reason': finish}]}
                self.wfile.write(('data: ' + json.dumps(frame) + '\n\n').encode())
            self.wfile.write(b'data: [DONE]\n\n')
        else:
            self.wfile.write(json.dumps({'id': 'local', 'choices': [{'index': 0, 'message': message,
                'finish_reason': 'tool_calls' if asking else 'stop'}]}).encode())
        self.wfile.flush()


async def probe(peer):
    phase('imports_started')
    import websockets
    from gateway.config import Platform, PlatformConfig
    from gateway.platforms.base import BasePlatformAdapter, SendResult
    from gateway.run import GatewayRunner
    from gateway.run_api import start_gateway_api, stop_gateway_api
    from gateway.session import SessionSource
    from gateway.session_authority import initialize_session_authority
    from hermes_cli import web_server
    from tools import clarify_gateway
    phase('imports_complete')

    class Adapter(BasePlatformAdapter):
        async def connect(self, **kwargs):
            return True
        async def disconnect(self):
            pass
        async def send(self, chat_id, content, **kwargs):
            return SendResult(success=True, message_id='owned')
        async def send_typing(self, *args, **kwargs):
            pass
        async def get_chat_info(self, chat_id):
            return {'id': chat_id}
        async def send_clarify(self, **kwargs):
            return SendResult(success=True, message_id='owned-card')

    runner = GatewayRunner()
    authority = await initialize_session_authority(runner, profile_id='fixture', instance_id='clarify')
    runner.adapters[Platform.TELEGRAM] = Adapter(PlatformConfig(enabled=True, token='fixture'), Platform.TELEGRAM)
    ref = authority.register(SessionSource(platform=Platform.TELEGRAM, chat_id='clarify-chat',
                                           chat_type='dm', user_id='fixture-user'))
    api = await start_gateway_api(runner)
    phase('api_ready')
    url = api.api_origin.replace('http:', 'ws:') + '/api/ws?token=' + web_server._SESSION_TOKEN

    async def rpc(ws, rid, method, **params):
        await ws.send(json.dumps({'jsonrpc': '2.0', 'id': rid, 'method': method,
                                 'params': {'session_id': ref.session_id, **params}}))
        async with asyncio.timeout(10):
            while True:
                frame = json.loads(await ws.recv())
                if frame.get('id') == rid:
                    return frame

    a = b = None
    try:
        a, b = await websockets.connect(url), await websockets.connect(url)
        phase('sockets_attached')
        await rpc(a, 1, 'session.resume')
        await rpc(b, 1, 'session.resume')
        assert 'result' in await rpc(a, 2, 'prompt.submit', input_id='clarify-first', text='Ask my color')
        phase('prompt_accepted')
        deadline = time.monotonic() + 15
        while True:
            snapshot = await rpc(b, 3, 'session.resume')
            prompts = snapshot['result']['prompts']
            if prompts:
                break
            if time.monotonic() >= deadline:
                raise AssertionError('real clarify waiter missing from shared snapshot')
            await asyncio.sleep(.05)
        prompt = prompts[0]
        assert prompt['kind'] == 'clarify', prompt
        assert clarify_gateway.has_pending(authority.sessions[ref.session_id].route)
        phase('clarify_observed')
        await a.close()
        a = None
        assert (await rpc(b, 4, 'session.resume'))['result']['prompts'][0]['prompt_id'] == prompt['prompt_id']
        identity = {'prompt_id': prompt['prompt_id'], 'execution_generation': prompt['execution_generation']}
        stale = await rpc(b, 5, 'clarify.respond', **{**identity, 'execution_generation': -1}, answer='green')
        assert stale['error']['data']['reason'] == 'stale_generation'
        response = await rpc(b, 6, 'clarify.respond', **identity, answer='green')
        assert response['result']['status'] == 'resolved', response
        phase('response_accepted')
        duplicate = await rpc(b, 7, 'clarify.respond', **identity, answer='SHOULD_NOT_REPLACE')
        assert duplicate.get('result', {}).get('status') == 'already_resolved' or duplicate.get('error', {}).get('data', {}).get('reason') == 'stale_generation', duplicate
        await asyncio.wait_for(authority.sessions[ref.session_id].task, 15)
        phase('turn_settled')
        tools = [m for messages in peer.requests for m in messages if m['role'] == 'tool']
        assert any('green' in json.dumps(m) for m in tools), tools
        assert not any('SHOULD_NOT_REPLACE' in json.dumps(m) for m in tools)
        assert (await rpc(b, 8, 'session.resume'))['result']['prompts'] == []
        Path(os.environ['HERMES_HOME'], 'clarify-passed').write_text('reply reached real model; stale reply rejected')
    finally:
        phase('async_cleanup_started')
        async with asyncio.timeout(45):
            try:
                clarify_gateway.clear_session(authority.sessions[ref.session_id].route)
                task = authority.sessions[ref.session_id].task
                if task is not None:
                    await asyncio.wait_for(task, 15)
            finally:
                try:
                    for ws in (a, b):
                        if ws is not None:
                            await ws.close()
                finally:
                    await asyncio.wait_for(stop_gateway_api(api), 10)
        phase('async_transport_cleanup_complete')


def main():
    phase('peer_started')
    peer = OwnedModelServer(('127.0.0.1', 0), ModelPeer)
    peer.requests = []
    server_thread = threading.Thread(target=peer.serve_forever, daemon=True)
    server_thread.start()
    url = f'http://127.0.0.1:{peer.server_port}/v1'
    os.environ.update(OPENAI_API_KEY='loopback-only', OPENAI_BASE_URL=url, TELEGRAM_ALLOWED_USERS='fixture-user')
    Path(os.environ['HERMES_HOME'], 'config.yaml').write_text(
        f'model:\n  default: local\n  provider: custom\n  base_url: {url}\n  context_length: 128000\n'
        'agent:\n  clarify_timeout: 25\nauxiliary:\n  title_generation:\n    enabled: false\n')
    try:
        asyncio.run(probe(peer))
    finally:
        phase('model_cleanup_started')
        deadline = time.monotonic() + 10
        stopping = threading.Thread(target=peer.shutdown, daemon=True)
        stopping.start()
        try:
            for owned in (stopping, server_thread):
                owned.join(timeout=max(0, deadline - time.monotonic()))
                assert not owned.is_alive(), 'owned model listener has no terminal receipt'
            with peer.handler_lock:
                handlers = list(peer.handler_threads)
            for owned in handlers:
                owned.join(timeout=max(0, deadline - time.monotonic()))
                assert not owned.is_alive(), 'owned model handler has no terminal receipt'
        finally:
            peer.server_close()
        phase('model_threads_terminal')


if __name__ == '__main__':
    status = 0
    try:
        main()
        phase('peer_exit_ready')
    except BaseException as exc:
        # Code locations and fixed phases diagnose failures without exception
        # messages, model payloads, config, headers or credential values.
        failures = []
        current = exc
        while current is not None and len(failures) < 4:
            failures.append({'error_type': type(current).__name__, 'frames': [
                {'file': Path(frame.f_code.co_filename).name, 'line': line,
                 'function': frame.f_code.co_name}
                for frame, line in traceback.walk_tb(current.__traceback__)]})
            current = current.__context__
        phase('peer_failed', error_type=type(exc).__name__, failed_at=LAST_PHASE,
              failure_locations=failures)
        status = 1
    os._exit(status)
