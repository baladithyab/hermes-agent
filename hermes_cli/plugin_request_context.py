"""Safe authenticated identity projection for hosted dashboard plugins.

Only the server may construct this metadata. It travels on the private host
channel, not in HTTP headers, and deliberately contains no bearer credentials.
Selection authorization is separate; this projection alone cannot authorize a
selection-sensitive mutation.
"""
import hashlib
import secrets
import re
from dataclasses import asdict

from hermes_cli.plugin_selection_authority import SelectionAuthority

from fastapi import HTTPException

from hermes_constants import get_hermes_home

_PROCESS_INCARNATION = secrets.token_hex(16)
_AUTHORITY = SelectionAuthority()


def bind_selection(request, context, *, select_current=False, deselect=False):
    """Host authenticates and resolves home first; HTTP cannot supply either."""
    client = request.headers.get('X-Hermes-Plugin-Client', '')
    if not re.fullmatch(r'[a-zA-Z0-9_-]{16,128}', client):
        raise HTTPException(status_code=409, detail='STALE_SCOPE')
    try:
        if deselect:
            selected = _AUTHORITY.deselect(context['principal'], client,
                request.headers.get('X-Hermes-Plugin-Scope'),
                request.headers.get('X-Hermes-Plugin-Epoch'))
        elif select_current:
            selected = _AUTHORITY.select(context['principal'], client,
                                         context['host_incarnation'], context['home'],
                                         expected_epoch=request.headers.get('X-Hermes-Plugin-Epoch', '0'))
        else:
            selected = _AUTHORITY.resolve(context['principal'], client,
                request.headers.get('X-Hermes-Plugin-Scope'),
                request.headers.get('X-Hermes-Plugin-Epoch'))
            if selected.home != context['home'] or selected.connection != context['host_incarnation']:
                raise ValueError('STALE_SCOPE')
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None
    return {**context, 'selection': asdict(selected)}



def authenticated_context(request):
    from hermes_cli.web_server import _has_valid_session_token

    native = getattr(request.state, 'native_http_principal', None)
    principal = getattr(request.state, 'token_principal', None)
    session = getattr(request.state, 'session', None)
    if isinstance(native, dict) and isinstance(native.get('subject'), str) and native['subject']:
        # The private gateway grant was redeemed by the HTTP boundary; the
        # active home was scoped there, never selected by these headers.
        identity = ('native-http', native['subject'])
    elif getattr(request.state, 'token_authenticated', False) and principal is not None:
        identity = (principal.provider, principal.principal)
    elif session is not None:
        identity = (session.provider, session.user_id)
    elif _has_valid_session_token(request):
        identity = ('local-session', _PROCESS_INCARNATION)
    else:
        raise HTTPException(status_code=401, detail='Unauthorized')
    home = str(get_hermes_home().resolve())
    digest = hashlib.sha256(('\0'.join(identity)).encode()).hexdigest()
    return {'version': 1, 'principal': digest, 'home': home,
            'host_incarnation': _PROCESS_INCARNATION}
