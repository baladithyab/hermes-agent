"""Bound large plugin buffers without parking the shared default executor."""
from __future__ import annotations

import asyncio
from collections import deque
from concurrent.futures import ThreadPoolExecutor
import contextvars
from functools import partial
import threading

from hermes_cli.plugin_api_limits import MIB
from hermes_cli.plugin_host_wire import PluginHostBusy

BUFFER_BYTES = 192 * MIB
DELIVERY_SECONDS = 15
_LARGE_IO = ThreadPoolExecutor(max_workers=2, thread_name_prefix='plugin-http')
_RECEIPT_IO = ThreadPoolExecutor(max_workers=1, thread_name_prefix='plugin-receipt')


class BufferLease:
    def __init__(self, admission, size):
        self.admission, self.size = admission, size

    def shrink(self, size):
        with self.admission.lock:
            if not 0 <= size <= self.size:
                raise ValueError('invalid plugin buffer size')
            self.admission.used -= self.size - size
            self.size = size
            self.admission.wake()

    def release(self):
        self.shrink(0)


class BufferAdmission:
    """FIFO bounded byte reservations, including slow-client delivery buffers.

    Waiters hold no body and consume no executor threads. A tiny lock only
    protects counters and futures, never filesystem access or native calls.
    """
    def __init__(self, capacity=BUFFER_BYTES, max_waiters=16):
        self.capacity, self.max_waiters = capacity, max_waiters
        self.used = 0
        self.lock = threading.Lock()
        self.waiters = deque()

    def wake(self):
        while self.waiters:
            size, loop, future = self.waiters[0]
            if self.used + size > self.capacity:
                break
            self.waiters.popleft()
            self.used += size
            lease = BufferLease(self, size)
            def deliver(future=future, lease=lease):
                if future.done():
                    lease.release()
                else:
                    future.set_result(lease)
            loop.call_soon_threadsafe(deliver)

    async def acquire(self, size, timeout=10):
        if not 0 < size <= self.capacity:
            raise ValueError('invalid plugin buffer reservation')
        loop = asyncio.get_running_loop()
        future = loop.create_future()
        waiter = (size, loop, future)
        with self.lock:
            if not self.waiters and self.used + size <= self.capacity:
                self.used += size
                return BufferLease(self, size)
            if len(self.waiters) >= self.max_waiters:
                raise PluginHostBusy('plugin request not forwarded')
            self.waiters.append(waiter)
        try:
            return await asyncio.wait_for(asyncio.shield(future), timeout)
        except BaseException as error:
            with self.lock:
                if waiter in self.waiters:
                    self.waiters.remove(waiter)
                self.wake()
            if future.done() and not future.cancelled():
                future.result().release()
            else:
                future.cancel()
            if isinstance(error, TimeoutError):
                raise PluginHostBusy('plugin request not forwarded') from None
            raise


# A retained resource/export cannot consume the receipt lane. The two pools
# share a fixed 192 MiB reservation ceiling, not an unbounded priority bypass.
receipt_buffers = BufferAdmission(capacity=49 * MIB)
buffers = BufferAdmission(capacity=BUFFER_BYTES - 49 * MIB)


async def plugin_io(call, *args, receipt=False, **kwargs):
    """Admitted work never waits for native admission in the default pool."""
    context = contextvars.copy_context()
    executor = _RECEIPT_IO if receipt else _LARGE_IO
    future = asyncio.get_running_loop().run_in_executor(executor, context.run, partial(call, *args, **kwargs))
    try:
        return await asyncio.shield(future)
    except asyncio.CancelledError:
        # The thread still owns its input/output reservation until invocation
        # ends. Cancellation must not turn it into an unaccounted native call.
        while not future.done():
            try:
                await asyncio.shield(future)
            except asyncio.CancelledError:
                continue
            except Exception:
                break
        if future.done() and not future.cancelled():
            future.exception()
        raise
