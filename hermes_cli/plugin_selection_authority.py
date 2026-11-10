"""Host-owned selection and immutable acceptance records.

Caller authenticates the principal and resolves home before calling select.
Policy callbacks must be local bounded reads, not remote network calls.
"""
from dataclasses import dataclass
import re
import secrets
import threading


@dataclass(frozen=True)
class Selection:
    principal: str
    client: str
    connection: str
    home: str
    epoch: int
    scope_id: str


@dataclass(frozen=True)
class AcceptedOperation:
    operation_id: str
    selection: Selection
    revision: str
    action: str
    payload_digest: str


class SelectionAuthority:
    def __init__(self):
        self._lock = threading.RLock()
        self._selections = {}
        self._accepted = {}
        self._revoked = set()
        self._epoch = 0

    def select(self, principal, client, connection, home, *, expected_epoch=None):
        with self._lock:
            key = principal, client
            if key not in self._selections and len(self._selections) >= 256:
                raise ValueError('BUSY')
            previous = self._selections.get(key)
            if expected_epoch is not None and str(previous.epoch if previous else 0) != str(expected_epoch):
                raise ValueError('STALE_SCOPE')
            if previous and (previous.connection, previous.home) == (connection, home):
                return previous
            self._epoch += 1
            selected = Selection(principal, client, connection, home,
                                 self._epoch, secrets.token_hex(16))
            self._selections[key] = selected
            return selected

    def resolve(self, principal, client, scope_id, epoch):
        """Resolve a client assertion without allowing it to choose a home."""
        with self._lock:
            selected = self._selections.get((principal, client))
            if selected is None or selected.scope_id != scope_id or str(selected.epoch) != str(epoch):
                raise ValueError('STALE_SCOPE')
            return selected

    def deselect(self, principal, client, scope_id, epoch):
        with self._lock:
            previous = self.resolve(principal, client, scope_id, epoch)
            self._epoch += 1
            # Retain a tombstone epoch for CAS; accepted records are untouched.
            selected = Selection(principal, client, previous.connection, '',
                                 self._epoch, secrets.token_hex(16))
            self._selections[(principal, client)] = selected
            return selected

    def current(self, selection):
        with self._lock:
            return self._selections.get((selection.principal, selection.client)) == selection

    def accept(self, selection, operation_id, revision, policy, *, action, payload_digest):
        with self._lock:
            if (not isinstance(operation_id, str) or not re.fullmatch(r'[a-zA-Z0-9_-]{1,128}', operation_id)
                    or not isinstance(action, str) or not re.fullmatch(r'[a-zA-Z0-9_./:-]{1,256}', action)
                    or not isinstance(payload_digest, str) or not re.fullmatch('[a-f0-9]{64}', payload_digest)):
                raise ValueError('INVALID_INPUT')
            if not self.current(selection):
                raise ValueError('STALE_SCOPE')
            if not isinstance(revision, str) or not re.fullmatch('[a-f0-9]{64}', revision):
                raise ValueError('STALE_REVISION')
            if not self._allowed(policy):
                raise ValueError('POLICY_LOCKED')
            key = selection.principal, operation_id
            if key in self._revoked:
                raise ValueError('POLICY_LOCKED')
            record = AcceptedOperation(operation_id, selection, revision, action, payload_digest)
            previous = self._accepted.get(key)
            if previous is not None:
                if previous != record:
                    raise ValueError('CONFLICT')
                return previous
            if len(self._accepted) >= 100:
                raise ValueError('BUSY')
            self._accepted[key] = record
            return record

    def retire(self, operation):
        """Release this exact acceptance; an old handle cannot retire a reuse."""
        with self._lock:
            key = operation.selection.principal, operation.operation_id
            if self._accepted.get(key) is not operation:
                return False
            del self._accepted[key]
            self._revoked.discard(key)
            return True

    @staticmethod
    def _allowed(policy):
        try:
            return policy() is True
        except Exception:
            return False

    def revalidate(self, operation, policy):
        with self._lock:
            key = operation.selection.principal, operation.operation_id
            if self._accepted.get(key) is not operation or key in self._revoked:
                return False
            if not self._allowed(policy):
                self._revoked.add(key)
                return False
            return True
