"""Private operation transport authority, including expired request capabilities."""
from dataclasses import asdict
import pytest

from hermes_cli.plugin_operation_authority import RequestOperations
from hermes_cli.plugin_selection_authority import SelectionAuthority


def test_expired_request_cannot_accept_and_committed_work_stays_pinned():
    authority = SelectionAuthority()
    ops = RequestOperations(authority)
    selected = authority.select('p', 'window', 'host', '/A')
    enabled = [True]
    token = ops.open({'selection': asdict(selected)}, 'plugin', 'PUT', '/config', b'one', lambda: enabled[0])
    params = {'verb': 'accept', 'capability': token, 'operation_id': 'op', 'revision': 'a' * 64}
    accepted = ops.call(params)
    assert ops.call(params) == accepted
    ops.close(token)
    with pytest.raises(ValueError, match='STALE_SCOPE'):
        ops.call(params)
    deselected = authority.deselect('p', 'window', selected.scope_id, selected.epoch)
    check = {'verb': 'revalidate', 'capability': accepted['capability']}
    assert ops.call(check)
    assert accepted['record']['selection']['home'] == '/A'
    restored = authority.select('p', 'window', 'host', '/A', expected_epoch=deselected.epoch)
    assert restored.epoch > selected.epoch
    enabled[0] = False
    assert not ops.call(check)
    enabled[0] = True
    assert not ops.call(check)
    assert not ops.call({'verb': 'revalidate', 'capability': 'forged'})


def test_delayed_unaccepted_request_fails_after_switch_and_payload_collision():
    authority = SelectionAuthority()
    ops = RequestOperations(authority)
    selected = authority.select('p', 'window', 'host', '/A')
    context = {'selection': asdict(selected)}
    first = ops.open(context, 'plugin', 'PUT', '/config', b'one', lambda: True)
    other = ops.open(context, 'plugin', 'PUT', '/config', b'two', lambda: True)
    params = {'verb': 'accept', 'capability': first, 'operation_id': 'op', 'revision': 'a' * 64}
    ops.call(params)
    with pytest.raises(ValueError, match='CONFLICT'):
        ops.call({**params, 'capability': other})
    authority.select('p', 'window', 'host', '/B')
    with pytest.raises(ValueError, match='STALE_SCOPE'):
        ops.call({**params, 'operation_id': 'queued'})
