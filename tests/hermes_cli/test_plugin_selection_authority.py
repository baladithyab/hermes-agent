"""Selection ABA and accepted-work semantics, without credentials or a fake host."""
import pytest

from hermes_cli.plugin_selection_authority import SelectionAuthority


def test_switch_rejects_stale_acceptance_but_not_committed_work():
    authority = SelectionAuthority()
    first = authority.select('principal', 'window', 'connection', '/owned/A')
    accepted = authority.accept(first, 'operation', 'a' * 64, lambda: True, action='apply', payload_digest='b' * 64)
    authority.select('principal', 'window', 'connection', '/owned/B')
    again = authority.select('principal', 'window', 'connection', '/owned/A')
    assert again.epoch > first.epoch
    with pytest.raises(ValueError, match='STALE_SCOPE'):
        authority.accept(first, 'stale', 'a' * 64, lambda: True, action='apply', payload_digest='b' * 64)
    assert accepted.selection == first
    assert authority.revalidate(accepted, lambda: True)
    assert not authority.revalidate(accepted, lambda: False)
    # A restored policy cannot resurrect explicitly revoked accepted work.
    assert not authority.revalidate(accepted, lambda: True)


def test_principal_connection_and_revision_are_immutable():
    authority = SelectionAuthority()
    a = authority.select('principal-a', 'window', 'connection-a', '/owned/A')
    b = authority.select('principal-b', 'window', 'connection-b', '/owned/B')
    op = authority.accept(a, 'operation', 'a' * 64, lambda: True, action='apply', payload_digest='b' * 64)
    assert authority.current(a)
    assert authority.current(b)
    with pytest.raises(ValueError, match='STALE_REVISION'):
        authority.accept(a, 'bad', '', lambda: True, action='apply', payload_digest='b' * 64)
    with pytest.raises(ValueError, match='CONFLICT'):
        authority.accept(a, 'operation', 'b' * 64, lambda: True, action='apply', payload_digest='b' * 64)
    assert authority.accept(a, 'operation', 'a' * 64, lambda: True, action='apply', payload_digest='b' * 64) == op
    with pytest.raises(ValueError, match='POLICY_LOCKED'):
        authority.accept(a, 'revoked', 'a' * 64, lambda: False, action='apply', payload_digest='b' * 64)


def test_revalidation_error_fails_closed():
    authority = SelectionAuthority()
    selection = authority.select('principal', 'window', 'connection', '/owned/A')
    op = authority.accept(selection, 'operation', 'a' * 64, lambda: True, action='apply', payload_digest='b' * 64)
    def unavailable():
        raise OSError('unavailable policy')
    assert not authority.revalidate(op, unavailable)
    assert not authority.revalidate(op, lambda: True)
    with pytest.raises(ValueError, match='POLICY_LOCKED'):
        authority.accept(selection, 'operation', 'a' * 64, lambda: True, action='apply', payload_digest='b' * 64)


def test_delayed_selection_cannot_retarget_after_switch():
    authority = SelectionAuthority()
    a = authority.select('p', 'client', 'connection', '/A', expected_epoch=0)
    b = authority.select('p', 'client', 'connection', '/B', expected_epoch=a.epoch)
    with pytest.raises(ValueError, match='STALE_SCOPE'):
        authority.select('p', 'client', 'connection', '/A', expected_epoch=a.epoch)
    assert authority.current(b)
    with pytest.raises(ValueError, match='STALE_SCOPE'):
        authority.resolve('other-principal', 'client', b.scope_id, b.epoch)
    again = authority.select('p', 'client', 'connection', '/A', expected_epoch=b.epoch)
    with pytest.raises(ValueError, match='STALE_SCOPE'):
        authority.resolve('p', 'client', a.scope_id, a.epoch)
    assert authority.resolve('p', 'client', again.scope_id, again.epoch) == again
