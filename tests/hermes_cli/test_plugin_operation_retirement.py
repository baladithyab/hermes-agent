"""Retired capabilities cannot regain authority, even if an ID is reused."""
from dataclasses import asdict

from hermes_cli.plugin_operation_authority import RequestOperations
from hermes_cli.plugin_selection_authority import SelectionAuthority


def test_retirement_reclaims_all_parent_slots_and_invalidates_old_capability():
    authority = SelectionAuthority()
    selected = authority.select('principal', 'window', 'connection', '/owned/A')
    operations = RequestOperations(authority)
    old = None
    for index in range(105):
        request = operations.open({'selection': asdict(selected)}, 'plugin', 'PUT', '/config', b'body', lambda: True)
        accepted = operations.call({'verb': 'accept', 'capability': request,
                                    'operation_id': 'same-id', 'revision': 'a' * 64})
        operations.close(request)
        assert operations.call({'verb': 'revalidate', 'capability': accepted['capability']})
        if old:
            assert not operations.call({'verb': 'revalidate', 'capability': old['capability']})
        assert operations.call({'verb': 'retire', 'capability': accepted['capability']})
        assert not operations.call({'verb': 'revalidate', 'capability': accepted['capability']})
        old = accepted
    assert not operations.operations
    assert not authority._accepted
    assert not authority._revoked


def test_shutdown_revokes_only_its_own_capabilities():
    authority = SelectionAuthority()
    selected = authority.select('principal', 'window', 'connection', '/owned/A')
    left, right = RequestOperations(authority), RequestOperations(authority)
    def accept(ops, ident):
        request = ops.open({'selection': asdict(selected)}, 'plugin', 'PUT', '/config', b'body', lambda: True)
        return ops.call({'verb': 'accept', 'capability': request,
                         'operation_id': ident, 'revision': 'a' * 64})
    a, b = accept(left, 'a'), accept(right, 'b')
    left.shutdown()
    assert not left.call({'verb': 'revalidate', 'capability': a['capability']})
    assert right.call({'verb': 'revalidate', 'capability': b['capability']})
    assert not left.requests
    right.shutdown()
    assert not authority._accepted
