"""A consumed RPC reply retires admission; active invocations remain bounded.

Independent G2 regression: the 64 ordinary invocation permits have a different
lifetime from the two extended-frame permits. Exercise real Channel read/write
loops over paired OS pipes, including the peer-visible publication boundary.
"""
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import os
import threading

from hermes_cli.plugin_host_wire import Channel


CAPACITY = 64
TIMEOUT = 15


@contextmanager
def paired_channels(handler):
    """Real framing/reader threads, with positive joins for every serving thread."""
    parent_read, child_write = os.pipe()
    child_read, parent_write = os.pipe()
    parent = Channel(os.fdopen(parent_read, 'rb', buffering=0),
                     os.fdopen(parent_write, 'wb', buffering=0),
                     lambda *_args: None, name='saturation-parent')
    child = Channel(os.fdopen(child_read, 'rb', buffering=0),
                    os.fdopen(child_write, 'wb', buffering=0),
                    handler, name='saturation-child')
    workers = []
    workers_lock = threading.Lock()
    serve = child._serve_bounded

    def track_worker(message, limits):
        with workers_lock:
            workers.append(threading.current_thread())
        serve(message, limits)

    child._serve_bounded = track_worker
    parent.start()
    child.start()
    try:
        yield parent, child
    finally:
        parent.close()
        child.close()
        parent._thread.join(TIMEOUT)
        child._thread.join(TIMEOUT)
        assert not parent._thread.is_alive()
        assert not child._thread.is_alive()
        with workers_lock:
            captured = list(workers)
        for worker in captured:
            worker.join(TIMEOUT)
        assert all(not worker.is_alive() for worker in captured)


def test_consumed_replies_retire_all_ordinary_slots_before_worker_cleanup(monkeypatch):
    """Request65 is legitimate after64 replies, even if old workers cannot clean up."""
    release_cleanup = threading.Event()
    all_published = threading.Event()
    published_lock = threading.Lock()
    published = 0
    cleanup_timeouts = []
    dispatched = []
    dispatch_lock = threading.Lock()
    next_reply_or_close = threading.Event()

    def handler(_method, params, _origin):
        with dispatch_lock:
            dispatched.append(params['index'])
        return params

    with paired_channels(handler) as (parent, child):
        send = child._send

        def hold_after_publication(message, **kwargs):
            nonlocal published
            # The complete real frame is already in the OS pipe. In particular,
            # this hook holds no Channel write lock and does not delay the peer.
            send(message, **kwargs)
            result = message.get('result')
            if isinstance(result, dict) and result.get('hold_cleanup'):
                with published_lock:
                    published += 1
                    if published == CAPACITY:
                        all_published.set()
                if not release_cleanup.wait(TIMEOUT * 3):
                    with published_lock:
                        cleanup_timeouts.append(result['index'])

        monkeypatch.setattr(child, '_send', hold_after_publication)
        child._on_close = lambda _reason: next_reply_or_close.set()
        pool = ThreadPoolExecutor(max_workers=1)
        try:
            # Sequential calls deliberately keep peer pending admission below
            # capacity while accumulating exactly64 completed server workers.
            for index in range(CAPACITY):
                params = {'index': index, 'hold_cleanup': True}
                assert parent.call('ordinary', params, timeout=TIMEOUT) == params
            assert all_published.wait(TIMEOUT), 'not all64 real replies reached publication'
            assert not release_cleanup.is_set()

            # The peer has consumed all64 replies. No outstanding invocation
            # justifies closing it as busy; repeat to catch duplicate releases.
            params = {'index': CAPACITY, 'hold_cleanup': False}
            next_call = pool.submit(parent.call, 'ordinary', params, timeout=TIMEOUT)
            next_call.add_done_callback(lambda _future: next_reply_or_close.set())
            assert next_reply_or_close.wait(TIMEOUT), 'neither the next reply nor channel closure arrived'
            assert child.closed_reason is None, 'completed invocations incorrectly closed the peer as busy'
            assert next_call.result(timeout=TIMEOUT) == params
            for index in range(CAPACITY + 1, CAPACITY + 4):
                params = {'index': index, 'hold_cleanup': False}
                assert parent.call('ordinary', params, timeout=TIMEOUT) == params
            assert parent.closed_reason is None
            assert child.closed_reason is None
            assert Counter(dispatched) == Counter(range(CAPACITY + 4))
        finally:
            release_cleanup.set()
            # A failing receiver can close logically without closing its pipe.
            # Wake only this test's pending caller before joining its executor.
            parent.close()
            pool.shutdown(wait=True)
    assert cleanup_timeouts == []


def test_sixty_four_active_invocations_still_refuse_an_unadmitted_peer_request():
    """Do not fix completed-work accounting by raising or bypassing the active bound."""
    entered = threading.Event()
    release = threading.Event()
    closed = threading.Event()
    lock = threading.Lock()
    dispatched = []
    handler_timeouts = []

    def handler(_method, params, _origin):
        with lock:
            dispatched.append(params['index'])
            if len(dispatched) == CAPACITY:
                entered.set()
        if not release.wait(TIMEOUT):
            with lock:
                handler_timeouts.append(params['index'])
        return params

    with paired_channels(handler) as (parent, child):
        child._on_close = lambda _reason: closed.set()
        try:
            # Send deliberately untracked peer frames to exercise the receiving
            # bound itself, rather than the caller's separate pending64 guard.
            for index in range(CAPACITY):
                parent._send({'id': index + 1, 'method': 'ordinary',
                              'params': {'index': index}, 'origin': None})
            assert entered.wait(TIMEOUT)
            assert child.closed_reason is None
            parent._send({'id': CAPACITY + 1, 'method': 'ordinary',
                          'params': {'index': CAPACITY}, 'origin': None})
            assert closed.wait(TIMEOUT), 'the active invocation bound was bypassed'
            assert child.closed_reason == 'plugin channel busy'
            with lock:
                assert Counter(dispatched) == Counter(range(CAPACITY))
        finally:
            release.set()
    assert handler_timeouts == []


def test_reply_writers_waiting_for_publication_still_hold_ordinary_admission(monkeypatch):
    """Moving release before write-lock ownership would create an unbounded writer queue."""
    writers_queued = threading.Event()
    closed = threading.Event()
    lock = threading.Lock()
    dispatched = []
    queued = 0

    def handler(_method, params, _origin):
        with lock:
            dispatched.append(params['index'])
        return params

    with paired_channels(handler) as (parent, child):
        child._on_close = lambda _reason: closed.set()
        send = child._send

        def observe_queued_reply(message, **kwargs):
            nonlocal queued
            if 'result' in message:
                with lock:
                    queued += 1
                    if queued == CAPACITY:
                        writers_queued.set()
            return send(message, **kwargs)

        monkeypatch.setattr(child, '_send', observe_queued_reply)
        assert child._write_lock.acquire(timeout=TIMEOUT)
        try:
            for index in range(CAPACITY):
                parent._send({'id': index + 1, 'method': 'ordinary',
                              'params': {'index': index}, 'origin': None})
            assert writers_queued.wait(TIMEOUT), 'all64 handlers must actually reach reply sending'
            assert child.closed_reason is None
            parent._send({'id': CAPACITY + 1, 'method': 'ordinary',
                          'params': {'index': CAPACITY}, 'origin': None})
            assert closed.wait(TIMEOUT), 'reply writers lost their bounded admission before owning publication'
            assert child.closed_reason == 'plugin channel busy'
            with lock:
                assert queued == CAPACITY
                assert Counter(dispatched) == Counter(range(CAPACITY))
        finally:
            child._write_lock.release()


def test_repeated_sixty_four_peer_roundtrips_do_not_close_duplicate_or_replay():
    """Four full concurrent rounds reuse the same real channel and request identities."""
    rounds = 4
    start_round = threading.Barrier(CAPACITY)
    handlers_entered = threading.Barrier(CAPACITY)
    lock = threading.Lock()
    dispatched = []
    active = 0
    peak_active = 0

    def handler(_method, params, _origin):
        nonlocal active, peak_active
        with lock:
            active += 1
            peak_active = max(peak_active, active)
            dispatched.append((params['round'], params['peer']))
        try:
            # All64 handlers really enter; merely scheduling64 fast callers
            # could otherwise exercise a much smaller concurrency by accident.
            handlers_entered.wait(timeout=TIMEOUT)
            return params
        finally:
            with lock:
                active -= 1

    with paired_channels(handler) as (parent, child), ThreadPoolExecutor(max_workers=CAPACITY) as pool:
        def peer_roundtrips(peer):
            results = []
            for round_index in range(rounds):
                start_round.wait(timeout=TIMEOUT)
                params = {'round': round_index, 'peer': peer}
                results.append(parent.call('ordinary', params, timeout=TIMEOUT))
            return results

        pending = [pool.submit(peer_roundtrips, peer) for peer in range(CAPACITY)]
        try:
            for peer, future in enumerate(pending):
                assert future.result(timeout=TIMEOUT * rounds) == [
                    {'round': round_index, 'peer': peer} for round_index in range(rounds)
                ]
            assert parent.closed_reason is None
            assert child.closed_reason is None
            assert parent._pending == {}
            assert parent._contexts == {}
            assert peak_active == CAPACITY
            assert active == 0
            assert Counter(dispatched) == Counter(
                (round_index, peer) for round_index in range(rounds) for peer in range(CAPACITY)
            )
        finally:
            start_round.abort()
            handlers_entered.abort()
