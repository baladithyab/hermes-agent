"""Acceptance binds the entire authorized intent, not just a revision."""
import pytest
from hermes_cli.plugin_selection_authority import SelectionAuthority


def test_accepted_intent_cannot_be_reused_for_another_action_or_payload():
    authority = SelectionAuthority()
    selected = authority.select('p', 'window', 'host', '/owned/A')
    args = (selected, 'op', 'a' * 64, lambda: True)
    accepted = authority.accept(*args, action='apply', payload_digest='b' * 64)
    assert accepted.action == 'apply'
    assert accepted.payload_digest == 'b' * 64
    for action, digest in [('stop', 'b' * 64), ('apply', 'c' * 64)]:
        with pytest.raises(ValueError, match='CONFLICT'):
            authority.accept(*args, action=action, payload_digest=digest)
    authority.select('p', 'window', 'host', '/owned/B')
    assert authority.revalidate(accepted, lambda: True)


@pytest.mark.parametrize('action,digest', [('', 'b' * 64), ('apply', ''), ('apply', True)])
def test_invalid_intent_cannot_be_accepted(action, digest):
    authority = SelectionAuthority()
    selected = authority.select('p', 'window', 'host', '/owned/A')
    with pytest.raises(ValueError, match='INVALID_INPUT'):
        authority.accept(selected, 'op', 'a' * 64, lambda: True,
                         action=action, payload_digest=digest)
