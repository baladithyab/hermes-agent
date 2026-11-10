"""A real child callback cannot hold parent authority locks across policy I/O."""
import threading
import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
from dataclasses import asdict

import pytest

from hermes_cli.plugin_selection_authority import SelectionAuthority
from tests.hermes_cli.test_plugin_asgi_lifecycle import hosted


API = '''
from fastapi import APIRouter, Request
router = APIRouter()
@router.post('/work')
def work(request: Request):
    ops = request.state.hermes_operations
    accepted = ops.accept('generated-operation', 'a' * 64)
    return {'valid': ops.revalidate(accepted)}
'''


@pytest.mark.parametrize('blocked_call', [1, 2])
def test_blocked_parent_policy_does_not_stall_containment(hosted, blocked_call):
    host, directory, _ = hosted
    (directory / 'plugin_api.py').write_text(API)
    # Import FastAPI before measuring callback containment; cold import is a
    # separate deadline case and can consume the entire one-second RPC budget.
    host.asgi_request('callback', str(directory), 'plugin_api.py',
                      'GET', '/warmup', '', [], b'')
    authority = SelectionAuthority()
    operations = host._request_operations
    operations.authority = authority
    selected = authority.select('principal', 'window', 'connection', str(directory))
    entered, release, returned = threading.Event(), threading.Event(), threading.Event()
    calls = []

    def policy():
        calls.append(1)
        if len(calls) == blocked_call:
            entered.set()
            release.wait(20)
            returned.set()
        return True

    context = {'selection': asdict(selected)}
    token = operations.open(context, 'callback', 'POST', '/work', b'', policy)
    process = host._proc
    pool = ThreadPoolExecutor(max_workers=1)
    future = pool.submit(host._call, 'asgi', {
        'plugin': 'callback', 'dashboard_dir': str(directory), 'api_file': 'plugin_api.py',
        'method': 'POST', 'path': '/work', 'query': '', 'headers': [],
        'body': '', 'trusted_context': context, 'request_capability': token,
    }, timeout=1)
    try:
        assert entered.wait(10), 'real child did not enter parent policy callback'
        # The RPC must finish while policy is STILL blocked, not after release.
        try:
            future.result(timeout=7)
        except FutureTimeout:
            assert future.done(), 'parent callback lock stalled deadline containment'
        else:
            pytest.fail('blocked callback unexpectedly succeeded')
        assert process.poll() is not None
        assert host._deadline_failed and not host.alive
        assert not returned.is_set()
        assert not operations.requests and not operations.operations
        assert not authority._accepted
        # Shared authority must remain usable by an unrelated scope.
        other = authority.select('other', 'window', 'connection', '/owned/other')
        assert authority.current(other)
    finally:
        release.set()
        pool.shutdown(wait=True)
        if process.poll() is None:
            process.kill()
        process.wait(timeout=3)
    assert returned.wait(2)
    # Drain the callback thread before testing that its late completion did not
    # republish an accepted record after shutdown.
    host._channel._thread.join(2)
    deadline = time.monotonic() + 2
    while host._channel._incoming._value != 64 and time.monotonic() < deadline:
        threading.Event().wait(.01)
    assert host._channel._incoming._value == 64
    assert not authority._accepted
    assert not operations.operations


@pytest.mark.parametrize('blocked_call', [1, 2])
def test_child_callback_timeout_retires_abandoned_authority(hosted, monkeypatch, blocked_call):
    host, directory, _ = hosted
    (directory / 'plugin_api.py').write_text(API)
    # Import FastAPI before measuring callback containment; cold import is a
    # separate deadline case and can consume the entire one-second RPC budget.
    host.asgi_request('callback', str(directory), 'plugin_api.py',
                      'GET', '/warmup', '', [], b'')
    authority = SelectionAuthority()
    operations = host._request_operations
    operations.authority = authority
    selected = authority.select('principal', 'window', 'connection', str(directory))
    entered, release = threading.Event(), threading.Event()
    calls = []

    def policy_incarnation(*args):
        # First read captures incarnation; subsequent reads are operation policy.
        calls.append(1)
        if len(calls) == blocked_call + 1:
            entered.set()
            release.wait(20)
        return 'generated-incarnation'

    monkeypatch.setattr('hermes_cli.plugin_policy_incarnation.policy_incarnation', policy_incarnation)
    monkeypatch.setattr('hermes_cli.web_server_dashboard._hosted_plugin_for_request',
                        lambda name: (host, str(directory), 'plugin_api.py'))
    process = host._proc
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(host.asgi_request, 'callback', str(directory), 'plugin_api.py',
                             'POST', '/work', '', [], b'', trusted_context={
                                 'selection': asdict(selected), 'home': str(directory)})
        try:
            assert entered.wait(10)
            with pytest.raises(TimeoutError, match='timed out'):
                future.result(timeout=7)
            assert future.done(), 'child deadline did not return'
            assert host.alive and host._proc is process
            assert not operations.requests
            assert not operations.operations, 'abandoned accepted capability remains usable'
            assert not authority._accepted
        finally:
            release.set()
    deadline = time.monotonic() + 3
    while host._channel._incoming._value != 64 and time.monotonic() < deadline:
        threading.Event().wait(.01)
    assert host._channel._incoming._value == 64
    assert not authority._accepted
    assert not operations.operations
    assert host._call('hello', {})['pid'] == process.pid


def test_failed_duplicate_request_preserves_original_acceptance(hosted, monkeypatch):
    host, directory, _ = hosted
    (directory / 'plugin_api.py').write_text('''
from fastapi import APIRouter, Request
router = APIRouter()
calls = 0
@router.post('/work')
def work(request: Request):
    global calls
    calls += 1
    accepted = request.state.hermes_operations.accept('same-operation', 'a' * 64)
    if calls == 2:
        raise TimeoutError('generated ambiguous duplicate response')
    return accepted
''')
    authority = SelectionAuthority()
    operations = host._request_operations
    operations.authority = authority
    selected = authority.select('principal', 'window', 'connection', str(directory))
    monkeypatch.setattr('hermes_cli.plugin_policy_incarnation.policy_incarnation',
                        lambda *args: 'generated-incarnation')
    monkeypatch.setattr('hermes_cli.web_server_dashboard._hosted_plugin_for_request',
                        lambda name: (host, str(directory), 'plugin_api.py'))

    def submit():
        return host.asgi_request('callback', str(directory), 'plugin_api.py',
                                 'POST', '/work', '', [], b'', trusted_context={
                                     'selection': asdict(selected), 'home': str(directory)})

    submit()
    capability, = operations.operations
    original = operations.operations[capability][0]
    with pytest.raises(TimeoutError, match='timed out'):
        submit()
    assert host.alive
    assert not operations.requests
    assert operations.call({'verb': 'revalidate', 'capability': capability}), (
        'failed duplicate revoked the previously accepted original operation')
    assert operations.operations[capability][0] is original
    submit()
    assert list(operations.operations) == [capability]
    assert operations.operations[capability][0] is original


def test_accepted_callback_stays_pinned_across_profile_connection_aba(hosted, monkeypatch):
    host, directory, _ = hosted
    (directory / 'plugin_api.py').write_text(API)
    # Import FastAPI before measuring callback containment; cold import is a
    # separate deadline case and can consume the entire one-second RPC budget.
    host.asgi_request('callback', str(directory), 'plugin_api.py',
                      'GET', '/warmup', '', [], b'')
    authority = SelectionAuthority()
    operations = host._request_operations
    operations.authority = authority
    selected = authority.select('principal', 'window', 'connection-A', str(directory))
    entered, release = threading.Event(), threading.Event()
    observed_homes = []

    def policy_incarnation(home, *args):
        observed_homes.append(home)
        # Initial capture, acceptance, then pre-dispatch revalidation.
        if len(observed_homes) == 3:
            entered.set()
            release.wait(10)
        return 'generated-incarnation'

    monkeypatch.setattr('hermes_cli.plugin_policy_incarnation.policy_incarnation', policy_incarnation)
    monkeypatch.setattr('hermes_cli.web_server_dashboard._hosted_plugin_for_request',
                        lambda name: (host, str(directory), 'plugin_api.py'))
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(host.asgi_request, 'callback', str(directory), 'plugin_api.py',
                             'POST', '/work', '', [], b'', trusted_context={
                                 'selection': asdict(selected), 'home': str(directory)})
        try:
            assert entered.wait(5)
            capability, = operations.operations
            original = operations.operations[capability][0]
            assert original.selection == selected
            authority.select('principal', 'window', 'connection-B', str(directory / 'B'))
            again = authority.select('principal', 'window', 'connection-A', str(directory))
            assert again.epoch > selected.epoch
        finally:
            release.set()
        import json
        assert json.loads(future.result(timeout=5)['body']) == {'valid': True}
    assert set(observed_homes) == {str(directory)}
    assert operations.operations[capability][0] is original
    assert operations.call({'verb': 'revalidate', 'capability': capability})
    # The old selection can continue accepted work but cannot accept fresh work.
    stale = operations.open({'selection': asdict(selected)}, 'callback', 'POST',
                            '/work', b'', lambda: True)
    try:
        with pytest.raises(ValueError, match='STALE_SCOPE'):
            operations.call({'verb': 'accept', 'capability': stale,
                             'operation_id': 'stale-new-work', 'revision': 'a' * 64})
    finally:
        operations.close(stale, failed=True)
    assert operations.call({'verb': 'revalidate', 'capability': capability})
