"""Wire format for the plugin host: framed JSON-RPC in both directions plus a value codec.

Either side can call the other while a call is in flight (a host tool handler calling
``ctx.dispatch_tool`` re-enters the parent), so each request is served on its own thread, with a bounded admission limit that
fails closed rather than queuing recursive work into a deadlocked pool. Every outgoing request made
while serving one names it as ``origin``. The origin is a ContextVar, so it follows the work onto
event-loop tasks and ``to_thread`` hops. The parent uses that to run a
nested request in the caller's contextvars (profile home, secret scope, session) instead of the
reader thread's bare context.

Values cross as JSON. Callables and provider objects travel as references the other side turns
back into proxies; dataclasses become :class:`Record` (dict + attribute access); anything else
becomes an :class:`Opaque` placeholder naming its type, so a plugin that touches a live handle it
cannot have fails with a readable error rather than a pickle surprise. Where a core ABC declares
the type (``SecretSource.fetch -> FetchResult``, ``home_path: Path``), :func:`restore` rebuilds it.
"""

from __future__ import annotations

import base64
import contextvars
import dataclasses
import enum
import functools
import inspect
import itertools
import json
import logging
import os
import select
import time
import threading
import types
import typing
from collections import abc
from concurrent.futures import Future
from pathlib import PurePath
from typing import Any, BinaryIO, Callable, Dict, Optional

from hermes_cli.plugin_api_limits import DEFAULT_FRAME_BYTES, MAX_FRAME_BYTES, FrameLimits

logger = logging.getLogger("hermes_cli.plugins")

PROTOCOL_VERSION = 1


class PluginHostError(RuntimeError):
    """The host (or the parent) raised while serving a request; ``type_name`` is the remote class."""

    def __init__(self, type_name: str, message: str):
        super().__init__(f"{type_name}: {message}" if type_name else message)
        self.type_name = type_name
        self.remote_message = message


class PluginHostUnavailable(RuntimeError):
    """The channel is closed: the host process exited or was never started."""


class PluginHostBusy(RuntimeError):
    """Local admission refused before any request bytes were sent."""


class PluginHostDeadline(TimeoutError):
    """Local transport deadline, distinct from a remote plugin's TimeoutError."""


class PluginHostUnsupported(RuntimeError):
    """A ctx method that cannot cross the plugin-host boundary."""


_REMOTE_EXCEPTIONS = {
    cls.__name__: cls for cls in (ValueError, TypeError, KeyError, FileNotFoundError, PermissionError,
                                  NotImplementedError, TimeoutError, LookupError, RuntimeError)
}
_REMOTE_EXCEPTIONS["PluginHostUnsupported"] = PluginHostUnsupported


def error_payload(exc: BaseException) -> dict[str, Any]:
    """The wire form of ``exc``: its class name, plus the nearest class in ``_REMOTE_EXCEPTIONS``
    (so a ``json.JSONDecodeError`` is still caught by ``except ValueError`` on the other side).
    ``RuntimeError`` is never a base: ``PluginHostError`` already is one and keeps the class name."""
    payload: dict[str, Any] = {"type": type(exc).__name__, "message": str(exc)}
    base = next((c.__name__ for c in type(exc).__mro__
                 if c.__name__ in _REMOTE_EXCEPTIONS and c is not RuntimeError), None)
    if base and base != payload["type"]:
        payload["base"] = base
    return payload


def raise_remote(error: dict[str, Any]) -> None:
    """Re-raise a remote error as the builtin type plugins commonly catch; a subclass of one comes
    back as that base, its own class name kept in the message."""
    type_name = str(error.get("type") or "")
    message = str(error.get("message") or "")
    cls = _REMOTE_EXCEPTIONS.get(type_name)
    if cls is not None:
        raise cls(message)
    base = _REMOTE_EXCEPTIONS.get(str(error.get("base") or ""))
    if base is not None:
        raise base(f"{type_name}: {message}" if type_name else message)
    raise PluginHostError(type_name, message)


class Record(dict):
    """A dataclass that crossed the boundary: item AND attribute access, like the original fields."""

    def __getattr__(self, name: str) -> Any:
        try:
            return self[name]
        except KeyError:
            raise AttributeError(name) from None


class Opaque:
    """Placeholder for a live object the other process owns (a gateway runner, an SDK client)."""

    def __init__(self, type_name: str, text: str = ""):
        self.type_name, self.text = type_name, text

    def __getattr__(self, name: str) -> Any:
        raise AttributeError(f"{self.type_name!r} is a live object in the Hermes process and is not "
                             f"available inside the plugin host (attribute {name!r})")

    def __bool__(self) -> bool:
        return True

    def __repr__(self) -> str:
        return f"<opaque {self.type_name}>"


def encode(value: Any, refs: Optional[Callable[[Any], Optional[dict]]] = None, _depth: int = 0) -> Any:
    """JSON-safe form of ``value``; ``refs`` may turn callables/objects into reference dicts."""
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if _depth > 64:
        return {"__opaque__": type(value).__name__, "repr": "<too deep>"}
    if isinstance(value, enum.Enum):
        return encode(value.value, refs, _depth + 1)
    if isinstance(value, PurePath):
        return str(value)
    if isinstance(value, (bytes, bytearray)):
        return {"__bytes__": base64.b64encode(bytes(value)).decode("ascii")}
    if isinstance(value, dict):
        return {str(k): encode(v, refs, _depth + 1) for k, v in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [encode(v, refs, _depth + 1) for v in value]
    if isinstance(value, Opaque):
        return {"__opaque__": value.type_name, "repr": value.text}
    if refs is not None:
        ref = refs(value)
        if ref is not None:
            return ref
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        fields = {f.name: getattr(value, f.name, None) for f in dataclasses.fields(value)}
        return {"__record__": type(value).__name__, "fields": encode(fields, refs, _depth + 1)}
    dump = getattr(value, "model_dump", None)  # pydantic models
    if callable(dump) and not isinstance(value, type):
        try:
            return {"__record__": type(value).__name__, "fields": encode(dump(), refs, _depth + 1)}
        except Exception:
            pass
    try:
        text = repr(value)[:200]
    except Exception:
        text = ""
    return {"__opaque__": type(value).__name__, "repr": text}


def decode(value: Any, resolve: Optional[Callable[[dict], Any]] = None) -> Any:
    """Inverse of :func:`encode`; ``resolve`` turns ``__callable__``/``__object__`` refs into proxies."""
    if isinstance(value, list):
        return [decode(v, resolve) for v in value]
    if not isinstance(value, dict):
        return value
    if "__record__" in value:
        return Record(decode(value.get("fields") or {}, resolve))
    if "__bytes__" in value:
        return base64.b64decode(value["__bytes__"])
    if "__opaque__" in value:
        return Opaque(str(value["__opaque__"]), str(value.get("repr") or ""))
    if ("__callable__" in value or "__object__" in value or "__handle__" in value) and resolve is not None:
        return resolve(value)
    return {k: decode(v, resolve) for k, v in value.items()}


def restore(value: Any, hint: Any) -> Any:
    """``value`` as decoded, rebuilt as the type ``hint`` declares: JSON carries dicts, lists and
    scalars, so a ``FetchResult`` arrives as a :class:`Record`, a ``Path`` as ``str``, an enum as its
    value, a ``frozenset`` as a list. A value that does not fit stays as decoded, so the consumer's
    own type check still names it."""
    if value is None or hint is None or hint is Any:
        return value
    origin = typing.get_origin(hint)
    if origin in (typing.Union, types.UnionType):
        arms = [arm for arm in typing.get_args(hint) if arm is not type(None)]
        return restore(value, arms[0]) if len(arms) == 1 else value
    if origin is not None:
        return _restore_container(value, origin, typing.get_args(hint))
    if not isinstance(hint, type) or isinstance(value, hint):
        return value
    try:
        if dataclasses.is_dataclass(hint) and isinstance(value, Record):
            hints = typing.get_type_hints(hint)
            return hint(**{f.name: restore(value[f.name], hints.get(f.name))
                           for f in dataclasses.fields(hint) if f.init and f.name in value})
        if issubclass(hint, (enum.Enum, PurePath)):
            return hint(value)
    except (TypeError, ValueError, NameError):
        pass
    return value


_SEQUENCES: dict[Any, type] = {list: list, tuple: tuple, set: set, frozenset: frozenset,
                               abc.Sequence: list, abc.Set: frozenset, abc.Iterable: list}


def _restore_container(value: Any, origin: Any, args: tuple) -> Any:
    if origin in (dict, abc.Mapping) and isinstance(value, dict):
        key_hint, item_hint = args if len(args) == 2 else (None, None)
        return {restore(k, key_hint): restore(v, item_hint) for k, v in value.items()}
    kind = _SEQUENCES.get(origin)
    if kind is None or not isinstance(value, list):
        return value
    if kind is tuple and args and args[-1] is not Ellipsis:  # tuple[str, str]: one hint per slot
        return tuple(restore(v, a) for v, a in zip(value, args)) if len(args) == len(value) else tuple(value)
    return kind(restore(v, args[0] if args else None) for v in value)


def declared_member(cls: type, name: str) -> Optional[Callable[..., Any]]:
    """The function ``cls`` declares for method or property ``name`` (its annotations are the contract)."""
    raw = inspect.getattr_static(cls, name, None)
    if isinstance(raw, property):
        raw = raw.fget
    return raw if inspect.isfunction(raw) else None


@functools.lru_cache(maxsize=512)
def declared_return(cls: type, name: str) -> Any:
    """Return type ``cls`` declares for method/property ``name``; None when undeclared or unresolvable."""
    fn = declared_member(cls, name)
    try:
        return typing.get_type_hints(fn).get("return") if fn is not None else None
    except (NameError, TypeError, AttributeError):
        return None


def restore_arguments(fn: Optional[Callable[..., Any]], args: list, kwargs: dict) -> tuple[list, dict]:
    """``args``/``kwargs`` for a call to the unbound method ``fn``, each rebuilt as its declared
    parameter type. Positional stays positional and keyword stays keyword: the plugin's override may
    take ``**kwargs`` where the ABC names the parameter."""
    if fn is None:
        return args, kwargs
    try:
        hints = typing.get_type_hints(fn)
        params = list(inspect.signature(fn).parameters.values())[1:]  # drop ``self``
    except (NameError, TypeError, ValueError, AttributeError):
        return args, kwargs
    by_position = [p.name for p in params
                   if p.kind in (inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD)]
    restored = [restore(value, hints.get(name)) for value, name in zip(args, by_position)]
    restored += list(args[len(restored):])
    by_name = {p.name for p in params
               if p.kind in (inspect.Parameter.POSITIONAL_OR_KEYWORD, inspect.Parameter.KEYWORD_ONLY)}
    return restored, {k: restore(v, hints.get(k)) if k in by_name else v for k, v in kwargs.items()}


def is_async_callable(fn: Any) -> bool:
    target = getattr(fn, "__func__", fn)
    return inspect.iscoroutinefunction(target) or inspect.iscoroutinefunction(getattr(fn, "__call__", None))  # noqa: B004 -- __call__ feeds iscoroutinefunction (async instances), not a callability test


def describe_signature(fn: Any) -> Optional[list]:
    """``[[name, kind, has_default], ...]`` so the peer's proxy presents the same parameters (hook
    dispatch passes only the kwargs a callback declares)."""
    try:
        sig = inspect.signature(fn)
    except (TypeError, ValueError):
        return None
    return [[p.name, int(p.kind), p.default is not inspect.Parameter.empty] for p in sig.parameters.values()]


def signature_from(spec: Optional[list]):
    if not spec:
        return None
    try:
        return inspect.Signature([
            inspect.Parameter(name, inspect._ParameterKind(kind),
                              default=None if has_default else inspect.Parameter.empty)
            for name, kind, has_default in spec
        ])
    except (TypeError, ValueError):
        return None


_SERVING: contextvars.ContextVar[Optional[int]] = contextvars.ContextVar("plugin_host_serving", default=None)


def serving_request() -> Optional[int]:
    """The peer request this code is running for (the ``origin`` of any call it makes), if any."""
    return _SERVING.get()


def bind_serving_request(request_id: Optional[int]) -> None:
    """Mark the current context (an event-loop task) as working for peer request ``request_id``."""
    _SERVING.set(request_id)


class Channel:
    """Bidirectional request/response over a pair of byte streams (one JSON document per line)."""

    def __init__(self, reader: BinaryIO, writer: BinaryIO,
                 handler: Callable[[str, dict[str, Any], Optional[int]], Any], *, name: str,
                 on_close: Optional[Callable[[str], None]] = None,
                 context_for_origin: Optional[Callable[[Optional[int]], contextvars.Context]] = None,
                 incoming_limits: Optional[Callable[[str, dict[str, Any]], FrameLimits]] = None):
        self._reader, self._writer = reader, writer
        self._handler, self._name, self._on_close = handler, name, on_close
        self._context_for_origin = context_for_origin
        self._incoming_limits = incoming_limits
        # Large calls retain their slot through reply consumption, not merely
        # through the pipe write. Normal re-entrant callbacks keep their slots.
        self._large_outgoing = threading.BoundedSemaphore(2)
        self._large_incoming = threading.BoundedSemaphore(2)
        self._write_lock = threading.Lock()
        self._incoming = threading.BoundedSemaphore(64)
        self._writer_fd = None
        if os.name == 'posix':
            try:
                self._writer_fd = writer.fileno()
                os.set_blocking(self._writer_fd, False)
            except (AttributeError, OSError, ValueError):
                self._writer_fd = None
        self._ids = itertools.count(1)
        self._pending: dict[int, Future] = {}
        self._pending_limits: dict[int, FrameLimits] = {}
        self._pending_lock = threading.Lock()
        self._contexts: dict[int, contextvars.Context] = {}
        self._closed_reason: Optional[str] = None
        self._thread = threading.Thread(target=self._read_loop, name=f"{name}-reader", daemon=True)

    def start(self) -> Channel:
        self._thread.start()
        return self

    @property
    def closed_reason(self) -> Optional[str]:
        return self._closed_reason

    def context_of(self, request_id: Optional[int]) -> Optional[contextvars.Context]:
        """Caller context recorded when THIS side sent ``request_id`` (still in flight)."""
        return self._contexts.get(request_id) if request_id is not None else None

    def request(self, method: str, params: dict[str, Any], *, timeout: Optional[float] = None,
                frame_limits: FrameLimits = FrameLimits()) -> Any:
        if self._closed_reason is not None:
            raise PluginHostUnavailable(self._closed_reason)
        if not isinstance(frame_limits, FrameLimits):
            raise ValueError('invalid plugin frame limits')
        large = frame_limits.extended
        if large and not self._large_outgoing.acquire(blocking=False):
            raise PluginHostBusy('plugin request not forwarded')
        deadline = time.monotonic() + (30.0 if timeout is None else timeout)
        request_id = next(self._ids)
        future: Future = Future()
        try:
            with self._pending_lock:
                if len(self._pending) >= 64:
                    raise PluginHostBusy('plugin request not forwarded')
                self._pending[request_id] = future
                self._pending_limits[request_id] = frame_limits
            self._contexts[request_id] = contextvars.copy_context()
            message = {"id": request_id, "method": method, "params": params,
                       "origin": _SERVING.get()}
            self._send(message, deadline=deadline, max_bytes=frame_limits.request_bytes)
            return future.result(timeout=None if timeout is None else max(0, deadline - time.monotonic()))
        except TimeoutError as exc:
            raise PluginHostDeadline('plugin RPC deadline; outcome unknown') from exc
        finally:
            with self._pending_lock:
                self._pending.pop(request_id, None)
                self._pending_limits.pop(request_id, None)
            self._contexts.pop(request_id, None)
            if large:
                self._large_outgoing.release()

    def _send(self, message: dict[str, Any], *, deadline=None,
              max_bytes: int = DEFAULT_FRAME_BYTES) -> None:
        data = (json.dumps(message, separators=(",", ":"), allow_nan=True) + "\n").encode("utf-8")
        if len(data) > min(max_bytes, MAX_FRAME_BYTES):
            raise ValueError('plugin frame too large')
        deadline = time.monotonic() + 30 if deadline is None else deadline
        if not self._write_lock.acquire(timeout=max(0, deadline - time.monotonic())):
            raise TimeoutError('plugin channel write deadline')
        try:
            if self._closed_reason is not None:
                raise PluginHostUnavailable(self._closed_reason)
            if self._writer_fd is None:
                self._writer.write(data)
                self._writer.flush()
                return
            view = memoryview(data)
            while view:
                remaining = deadline - time.monotonic()
                if remaining <= 0 or not select.select([], [self._writer_fd], [], remaining)[1]:
                    # A partial JSON frame cannot be safely reused or replayed.
                    self._close('plugin channel write deadline')
                    raise TimeoutError('plugin channel write deadline')
                try:
                    sent = os.write(self._writer_fd, view)
                except BlockingIOError:
                    continue
                view = view[sent:]
        except TimeoutError:
            raise
        except (BrokenPipeError, OSError, ValueError) as exc:
            self._close(f"{self._name} channel write failed: {type(exc).__name__}")
            raise PluginHostUnavailable(self._closed_reason) from exc
        finally:
            self._write_lock.release()

    def _read_frames(self):
        # Popen uses unbuffered pipes. FileIO.readline() performs one read per
        # byte; read chunks instead without introducing a buffered-close lock.
        read = getattr(self._reader, 'read1', self._reader.read)
        frame = bytearray()
        while chunk := read(64 * 1024):
            parts = chunk.split(b'\n')
            for index, part in enumerate(parts):
                if len(frame) + len(part) + 1 > MAX_FRAME_BYTES:
                    raise ValueError('plugin frame too large')
                frame.extend(part)
                if index < len(parts) - 1:
                    yield bytes(frame) + b'\n'
                    frame.clear()
        if frame:
            raise ValueError('incomplete plugin frame')

    def _read_loop(self) -> None:
        reason = f"{self._name} channel closed"
        try:
            for line in self._read_frames():
                if not line.strip():
                    continue
                try:
                    message = json.loads(line)
                except ValueError:
                    logger.warning("%s: dropped a malformed frame", self._name)
                    continue
                if not isinstance(message, dict):
                    reason = 'invalid plugin frame'
                    break
                if "method" in message:
                    method, params = message["method"], message.get("params") or {}
                    if not isinstance(method, str) or not isinstance(params, dict):
                        reason = 'invalid plugin request'
                        break
                    limits = self._incoming_limits(method, params) if self._incoming_limits else FrameLimits()
                    if len(line) > limits.request_bytes:
                        reason = 'plugin frame too large'
                        break
                    large = limits.extended
                    if large and not self._large_incoming.acquire(blocking=False):
                        self._send({"id": message.get("id"), "error": {
                            "type": "PluginHostBusy", "message": "plugin request not forwarded"}})
                        continue
                    if not self._incoming.acquire(blocking=False):
                        if large:
                            self._large_incoming.release()
                        reason = 'plugin channel busy'
                        break
                    threading.Thread(target=self._serve_bounded, args=(message, limits), daemon=True,
                                     name=f"{self._name}-rpc").start()
                    continue
                request_id = message.get("id")
                if type(request_id) is not int:
                    reason = 'invalid plugin response'
                    break
                with self._pending_lock:
                    future = self._pending.get(request_id)
                    limits = self._pending_limits.get(request_id, FrameLimits())
                if len(line) > limits.response_bytes:
                    reason = 'plugin frame too large'
                    break
                if future is not None and not future.done():
                    future.set_result(message)
        except (OSError, ValueError, TypeError) as exc:
            reason = f"{self._name} channel read failed: {type(exc).__name__}"
        self._close(reason)

    def _serve_bounded(self, message, limits):
        released = False

        def release():
            nonlocal released
            if not released:
                released = True
                if limits.extended:
                    self._large_incoming.release()
        try:
            self._serve(message, limits, release)
        finally:
            release()
            self._incoming.release()

    def _serve(self, message: dict[str, Any], limits: FrameLimits, release=lambda: None) -> None:
        request_id = message.get("id")
        origin = message.get("origin")
        context = self._context_for_origin(origin) if self._context_for_origin else None

        def run() -> dict[str, Any]:
            token = _SERVING.set(request_id)
            try:
                result = self._handler(str(message.get("method")), message.get("params") or {}, origin)
                return {"id": request_id, "result": result}
            except BaseException as exc:  # a plugin may raise anything, SystemExit included
                if not isinstance(exc, Exception):
                    logger.warning("%s: request %s raised %s", self._name, message.get("method"),
                                   type(exc).__name__)
                return {"id": request_id, "error": error_payload(exc)}
            finally:
                _SERVING.reset(token)

        reply = (context or contextvars.copy_context()).run(run)
        # Retire invocation admission before publishing the reply. The peer can
        # submit its next request as soon as it reads the terminating newline.
        release()
        if request_id is None:
            return
        try:
            self._send(reply, max_bytes=limits.response_bytes)
        except PluginHostUnavailable:
            pass
        except (TypeError, ValueError) as exc:  # unserializable result slipped past encode()
            self._send({"id": request_id, "error": {"type": "TypeError", "message": str(exc)}})

    def result_of(self, reply: dict[str, Any]) -> Any:
        if "error" in reply:
            raise_remote(reply["error"])
        return reply.get("result")

    def call(self, method: str, params: dict[str, Any], *, timeout: Optional[float] = None,
             frame_limits: FrameLimits = FrameLimits()) -> Any:
        return self.result_of(self.request(method, params, timeout=timeout, frame_limits=frame_limits))

    def _close(self, reason: str) -> None:
        with self._pending_lock:
            if self._closed_reason is not None:
                return
            self._closed_reason = reason
            pending = list(self._pending.values())
        for future in pending:
            if not future.done():
                future.set_exception(PluginHostUnavailable(reason))
        if self._on_close is not None:
            try:
                self._on_close(reason)
            except Exception:
                logger.debug("%s: on_close raised", self._name, exc_info=True)

    def close(self, reason: str = "closed") -> None:
        self._close(reason)
        for stream in (self._writer, self._reader):
            try:
                stream.close()
            except OSError:
                pass
