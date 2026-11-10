"""Keep a bounded plugin buffer admitted until ASGI delivery or cancellation."""
import asyncio
import weakref

from starlette.responses import Response

from hermes_cli.plugin_http_admission import DELIVERY_SECONDS


class AdmittedPluginResponse(Response):
    def __init__(self, response, release, *, current=None, delivery_timeout=DELIVERY_SECONDS):
        # Response.render keeps an existing bytes body without copying it.
        super().__init__(response.body, status_code=response.status_code,
                         media_type=response.media_type, background=response.background)
        self.raw_headers = response.raw_headers
        # Also release if a route/dependency fails before ASGI invokes us.
        self._release = weakref.finalize(self, release)
        self._current = current
        self._delivery_timeout = delivery_timeout

    async def _is_current(self):
        if self._current is None:
            return True
        try:
            # The callback includes profile/policy filesystem checks.
            return await asyncio.to_thread(self._current)
        except Exception:  # health: allow BLE001 -- arbitrary policy callback failures revoke delivery; exception text may contain credentials
            return False

    async def _send(self, send, message):
        if self._current is None:
            return await send(message)
        delivery = asyncio.ensure_future(send(message))
        try:
            while not delivery.done():
                await asyncio.wait((delivery,), timeout=0.1)
                if not delivery.done() and not await self._is_current():
                    # Headers may already be on the wire. Abort rather than
                    # send a second status or a successful final body frame.
                    raise ConnectionAbortedError('Resource unavailable')
            await delivery
        finally:
            if not delivery.done():
                delivery.cancel()
            # A blocked send must end before its buffer reservation is released.
            await asyncio.gather(delivery, return_exceptions=True)

    async def __call__(self, scope, receive, send):
        try:
            async with asyncio.timeout(self._delivery_timeout):
                if not await self._is_current():
                    return await Response('Resource unavailable', status_code=410,
                                          headers={'Cache-Control': 'no-store'})(scope, receive, send)
                await self._send(send, {'type': 'http.response.start', 'status': self.status_code,
                                        'headers': self.raw_headers})
                size = len(self.body)
                for offset in range(0, size or 1, 65536):
                    if not await self._is_current():
                        raise ConnectionAbortedError('Resource unavailable')
                    end = min(offset + 65536, size)
                    # Already accepted transport bytes cannot be recalled.
                    await self._send(send, {'type': 'http.response.body', 'body': self.body[offset:end],
                                            'more_body': end < size})
                if self.background is not None:
                    await self.background()
        finally:
            self.body = b''
            self._release()
