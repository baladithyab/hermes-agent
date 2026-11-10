"""Earned native readiness for cold, owned gateway fixture processes."""
import json
from pathlib import Path
import time

from tests.waiting import wait_until

# Fixture envelope: 90s cold imports +60s owned/profile initialization +20s
# normal startup warmup +10s publication margin. No production bound changes.
# An in-flight control read keeps its existing two-second transport bound.
NATIVE_BOOT_BUDGET = 180


def wait_native_ready(process, home, control, *, spawned_at):
    instance = None
    last_state = None
    last_error = None
    last_phase = 'spawned'

    def phase(name, **fields):
        nonlocal last_phase
        last_phase = name
        print(json.dumps({'native_fixture_phase': name, 'pid': process.pid,
                          'elapsed': round(time.monotonic() - spawned_at, 3), **fields}), flush=True)

    phase('spawned')
    from gateway.status import get_process_start_time

    code = process.poll()
    if code is not None:
        phase('readiness_held', returncode=code)
        raise AssertionError('owned gateway exited before identity capture')
    expected_start = get_process_start_time(process.pid)
    if expected_start is None:
        phase('readiness_held', last_error_type='missing_start_fingerprint')
        raise AssertionError('owned gateway process has no start fingerprint')

    def identify_ready():
        nonlocal instance, last_state, last_error
        code = process.poll()
        assert code is None, f'owned gateway exited before READY: returncode={code}; phase={last_phase}'
        try:
            descriptor = control(home, 'identify')
        except (PermissionError, AssertionError, ValueError) as exc:
            phase('control_refused_or_invalid', error_type=type(exc).__name__)
            raise AssertionError('native control denied or malformed; no readiness retry') from None
        except OSError as exc:
            if last_error != type(exc).__name__:
                last_error = type(exc).__name__
                phase('control_unavailable', error_type=last_error)
            return None
        assert isinstance(descriptor, dict), 'native identify payload is not an object'
        assert descriptor.get('protocol') == 1 and descriptor.get('runtime_protocol') == 1, 'native protocol mismatch'
        assert descriptor.get('kind') == 'hermes-gateway', 'native owner kind mismatch'
        assert descriptor.get('pid') == process.pid, 'native reply belongs to another process'
        assert descriptor.get('start_time') == expected_start == get_process_start_time(process.pid), 'native owner start mismatch'
        reported_home = descriptor.get('hermes_home')
        assert isinstance(reported_home, str) and reported_home, 'native owner home missing'
        assert Path(reported_home).resolve() == home.resolve(), 'native owner home mismatch'
        reported_instance = descriptor.get('instance_id')
        assert isinstance(reported_instance, str) and reported_instance, 'native instance identity missing'
        last_error = None
        if instance is None:
            instance = reported_instance
            phase('first_valid_identify')
        assert reported_instance == instance, 'native instance changed while waiting'
        state = descriptor.get('state')
        assert state in {'starting', 'ready'}, 'native owner entered a non-serving state'
        if state != last_state:
            last_state = state
            phase('descriptor_state', state=state)
        if state == 'ready':
            assert process.poll() is None, 'owned gateway exited at READY'
            phase('ready')
            return descriptor
        return None

    remaining = max(0, NATIVE_BOOT_BUDGET - (time.monotonic() - spawned_at))
    try:
        return wait_until(identify_ready, timeout=remaining, interval=.1, what='owned native gateway READY')
    except AssertionError:
        phase('readiness_held', last_state=last_state, last_error_type=last_error)
        raise
