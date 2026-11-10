"""Durable per-plugin revocation epochs for Hermes-mediated config writes.

Epoch publication precedes config publication. A failed write may revoke an old
owner conservatively; it cannot restore authority. External writers that bypass
these APIs do not provide an unobserved disable/re-enable history.
"""
from collections.abc import Mapping
from contextlib import contextmanager
import json
import os
from pathlib import Path
import re
import secrets
import stat

from pm.filesystem import durable_write_bytes, lock_fd


_LIMIT = 1024 * 1024
_NAME = '.plugin-policy-epochs.json'
_TOKEN = re.compile(r'[a-f0-9]{64}')


def _home(home):
    path = Path(home)
    if not path.is_absolute() or path.resolve() != path:
        raise ValueError('Invalid plugin policy home')
    return path


def _read(home):
    path = home / _NAME
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0))
    except FileNotFoundError:
        return {}
    with os.fdopen(fd, 'rb') as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_size > _LIMIT or path.is_symlink():
            raise ValueError('Invalid plugin policy epoch file')
        if os.name != 'nt':
            getuid = getattr(os, 'getuid', None)
            if getuid is None or info.st_uid != getuid() or info.st_mode & 0o077:
                raise ValueError('Plugin policy epochs must be private')
        raw = stream.read(_LIMIT + 1)
    if len(raw) > _LIMIT:
        raise ValueError('Plugin policy epoch file is too large')
    record = json.loads(raw)
    epochs = record.get('epochs') if isinstance(record, dict) else None
    if (not isinstance(record, dict) or record.get('version') != 1 or
            not isinstance(epochs, dict) or
            any(not isinstance(key, str) or not isinstance(token, str) or
                not _TOKEN.fullmatch(token) for key, token in epochs.items())):
        raise ValueError('Invalid plugin policy epochs')
    return epochs


def read_policy_epoch(home, plugin_key):
    """Return an opaque epoch without creating or repairing policy state."""
    try:
        return _read(_home(home)).get(plugin_key)
    except (OSError, ValueError, TypeError):
        return None


def _section(config):
    if not isinstance(config, Mapping):
        raise ValueError('Plugin policy config must be a mapping')
    plugins = config.get('plugins') or {}
    if not isinstance(plugins, Mapping):
        raise ValueError('Plugin policy must be a mapping')
    enabled, disabled = plugins.get('enabled', []), plugins.get('disabled', [])
    entries = plugins.get('entries') or {}
    if (not isinstance(enabled, list) or not isinstance(disabled, list) or
            not isinstance(entries, Mapping) or
            any(not isinstance(key, str) for key in [*enabled, *disabled, *entries])):
        raise ValueError('Invalid plugin policy selection')
    return plugins, set(enabled), set(disabled), entries


def _projection(section, key):
    plugins, enabled, disabled, entries = section
    return (key in enabled, key in disabled, key in entries, entries.get(key),
            plugins.get('isolation'), plugins.get('host'))


@contextmanager
def _locked(home):
    path = home / '.plugin-policy-epochs.lock'
    fd = os.open(path, os.O_CREAT | os.O_RDWR | getattr(os, 'O_NOFOLLOW', 0), 0o600)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or path.is_symlink():
            raise ValueError('Invalid plugin policy lock')
        if os.name != 'nt':
            getuid = getattr(os, 'getuid', None)
            if getuid is None or info.st_uid != getuid() or info.st_mode & 0o077:
                raise ValueError('Plugin policy lock must be private')
        if info.st_size == 0:
            os.write(fd, b'\0')
        if not lock_fd(fd, wait=True, timeout=2):
            raise TimeoutError('Plugin policy epoch publication timed out')
        yield
    finally:
        os.close(fd)


def record_policy_write(home, previous, proposed):
    """Initialize epochs and revoke only plugins whose policy changes."""
    home = _home(home)
    before, after = _section(previous), _section(proposed)
    keys = before[1] | before[2] | set(before[3]) | after[1] | after[2] | set(after[3])
    if not keys and not (home / _NAME).exists():
        return
    home.mkdir(parents=True, exist_ok=True)
    with _locked(home):
        epochs = _read(home)
        changed = False
        for key in keys | set(epochs):
            if key not in epochs or _projection(before, key) != _projection(after, key):
                epochs[key] = secrets.token_hex(32)
                changed = True
        if changed:
            raw = (json.dumps({'version': 1, 'epochs': epochs}, sort_keys=True) + '\n').encode()
            if len(raw) > _LIMIT:
                raise ValueError('Plugin policy epoch file is too large')
            durable_write_bytes(home / _NAME, raw)


def record_policy_bytes(home, previous, proposed):
    """Apply the same write contract to PM's serialized config publication."""
    from hermes_yaml import safe_load
    before = safe_load(previous.decode('utf-8-sig')) if previous else {}
    after = safe_load(proposed.decode('utf-8-sig')) if proposed else {}
    record_policy_write(home, before or {}, after or {})
