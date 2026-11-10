"""Clarification response survives viewer detach through the real gateway worker."""
import os
from pathlib import Path
import subprocess
import sys
import json


# 90s cold setup +8x10s RPC +25s poll +30s socket work +15s settlement
# +45s async cleanup +10s model-thread cleanup +5s margin. Inner limits stay fixed.
PEER_ENVELOPE = 300


def _phases(output):
    return [json.loads(line.removeprefix('hermes_clarify_phase ')) for line in output.splitlines()
            if line.startswith('hermes_clarify_phase ')]


def test_real_shared_clarify_survives_detach(tmp_path):
    home, state = tmp_path / 'home', tmp_path / 'state'
    home.mkdir()
    state.mkdir()
    repo = Path(__file__).resolve().parents[2]
    env = {key: os.environ[key] for key in ('PATH', 'LANG', 'TZ') if key in os.environ}
    env.update(HOME=str(home), USERPROFILE=str(home), HERMES_HOME=str(state), PYTHONPATH=str(repo),
               PYTHONUNBUFFERED='1')
    output, errors = tmp_path / 'clarify-peer-stdout.log', tmp_path / 'clarify-peer-stderr.log'
    # File-backed diagnostics cannot strand communicate reader threads when a
    # worker inherits stdio. Completion is the captured peer's native wait.
    with output.open('wb') as stdout, errors.open('wb') as stderr:
        peer = subprocess.Popen(
            [sys.executable, '-u', str(Path(__file__).parent / 'fixtures' / 'authority_clarify_peer.py')],
            cwd=repo, env=env, stdin=subprocess.DEVNULL, stdout=stdout, stderr=stderr)
        try:
            code = peer.wait(timeout=PEER_ENVELOPE)
        except subprocess.TimeoutExpired:
            # Only the captured owned peer; a forced terminal result is never acceptance.
            peer.kill()
            code = peer.wait(timeout=15)
            raise AssertionError({'peer_timeout': True, 'positive_wait': True, 'returncode': code,
                                  'phases': _phases(errors.read_text(errors='replace'))}) from None
    receipts = _phases(errors.read_text(errors='replace'))
    assert code == 0, {'returncode': code, 'phases': receipts}
    assert receipts and receipts[-1]['phase'] == 'peer_exit_ready', receipts
    assert {'async_transport_cleanup_complete', 'model_threads_terminal'} <= {r['phase'] for r in receipts}, receipts
    assert (state / 'clarify-passed').read_text() == 'reply reached real model; stale reply rejected'
    print(json.dumps({'clarify_peer_terminal': True, 'pid': peer.pid, 'positive_wait': True,
                      'returncode': code, 'cleanup_phases_verified': True}), flush=True)


def test_expired_clarify_disappears_before_turn_settlement():
    from gateway.session_events import SessionEvents
    from gateway.session_pending_controls import PendingControls
    from tools import clarify_gateway

    events = SessionEvents()
    controls = PendingControls(events)
    entry = clarify_gateway.register('owned-expiring-question', 'owned-expiring-route', 'Choose', ['a', 'b'])
    try:
        controls.register_clarify('owned-session', 1, entry)
        assert controls.snapshot('owned-session', 1)
        assert clarify_gateway.wait_for_response(entry.clarify_id, timeout=.01) is None
        assert controls.snapshot('owned-session', 1) == ()
        assert controls.respond('owned-session', 1, entry.clarify_id, {'answer': 'a'}, kind='clarify')['status'] == 'already_resolved'
    finally:
        clarify_gateway.clear_session(entry.session_key)
