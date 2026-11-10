"""Private hosted request capabilities; never serialize these to a renderer.

The child can request acceptance only through a capability created for the
current authenticated request. Home, principal and policy are parent-owned.
Plugin-specific operation policy remains an additional mandatory check.
"""
import hashlib
import secrets
import threading
from dataclasses import asdict

from hermes_cli.plugin_selection_authority import Selection


class RequestOperations:
    def __init__(self, authority):
        self.authority = authority
        self.lock = threading.RLock()
        self.requests = {}
        self.operations = {}
        self.operation_plugins = {}
        self.request_acceptances = {}

    def open(self, context, plugin, method, path, body, policy):
        selection = context.get('selection') if context else None
        if selection is None:
            return None
        with self.lock:
            if len(self.requests) >= 64:
                raise ValueError('BUSY')
            token = secrets.token_hex(32)
            self.requests[token] = (Selection(**selection), plugin,
                method.upper() + ':' + path, hashlib.sha256(body).hexdigest(), policy)
            return token

    def close(self, token, *, failed=False):
        with self.lock:
            self.requests.pop(token, None)
            acceptances = self.request_acceptances.pop(token, set())
            if failed:
                for cap in acceptances:
                    accepted = self.operations.pop(cap, None)
                    self.operation_plugins.pop(cap, None)
                    if accepted is not None:
                        self.authority.retire(accepted[0])

    def shutdown(self, plugin=None):
        with self.lock:
            for token, (record, _) in list(self.operations.items()):
                if plugin is None or self.operation_plugins.get(token) == plugin:
                    self.authority.retire(record)
                    del self.operations[token]
                    self.operation_plugins.pop(token, None)
            for token, request in list(self.requests.items()):
                if plugin is None or request[1] == plugin:
                    del self.requests[token]
                    self.request_acceptances.pop(token, None)

    def call(self, params):
        token = params.get('capability')
        verb = params.get('verb')
        # Policy may touch the filesystem or block. Never run it holding the
        # capability lock (or the shared selection authority lock). Shutdown
        # removes these exact entries while the callback is still in flight.
        with self.lock:
            request = self.requests.get(token) if verb == 'accept' else None
            accepted = self.operations.get(token) if verb == 'revalidate' else None
        policy = request[4] if request is not None else accepted[1] if accepted is not None else None
        allowed = self.authority._allowed(policy) if policy is not None else False
        with self.lock:
            if verb == 'accept' and (request is None or self.requests.get(token) is not request):
                raise ValueError('STALE_SCOPE')
            if verb == 'revalidate' and (accepted is None or self.operations.get(token) is not accepted):
                return False
            if params.get('verb') == 'accept':
                request = self.requests.get(token)
                if request is None:
                    raise ValueError('STALE_SCOPE')
                selection, plugin, action, digest, policy = request
                if action.split(':', 1)[0] not in {'POST', 'PUT', 'PATCH', 'DELETE'}:
                    raise ValueError('INVALID_INPUT')
                # Namespace IDs so two plugins cannot collide or query each other.
                operation_id = params.get('operation_id')
                if not isinstance(operation_id, str) or not 1 <= len(operation_id) <= 128:
                    raise ValueError('INVALID_INPUT')
                internal_id = hashlib.sha256((plugin + '\0' + operation_id).encode()).hexdigest()
                record = self.authority.accept(selection, internal_id, params.get('revision'),
                    lambda: allowed, action=action, payload_digest=digest)
                key = (record.selection.principal, record.operation_id)
                for capability, (existing, _) in self.operations.items():
                    if (existing.selection.principal, existing.operation_id) == key:
                        # An idempotent lookup does not own the original grant.
                        # Failure of this response must not cancel accepted work.
                        return {'capability': capability, 'record': asdict(existing)}
                if len(self.operations) >= 100:
                    raise ValueError('BUSY')
                capability = secrets.token_hex(32)
                self.operations[capability] = (record, policy)
                self.operation_plugins[capability] = plugin
                self.request_acceptances.setdefault(token, set()).add(capability)
                return {'capability': capability, 'record': asdict(record)}
            if params.get('verb') == 'retire':
                accepted = self.operations.pop(token, None)
                self.operation_plugins.pop(token, None)
                for req_caps in self.request_acceptances.values():
                    req_caps.discard(token)
                return bool(accepted and self.authority.retire(accepted[0]))
            if params.get('verb') == 'revalidate':
                accepted = self.operations.get(token)
                return bool(accepted and self.authority.revalidate(accepted[0], lambda: allowed))
            raise ValueError('INVALID_INPUT')
