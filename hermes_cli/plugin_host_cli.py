"""Native CLI argv and text streams; third-party parsers stay in their host."""
from __future__ import annotations

import argparse
import contextvars
from concurrent.futures import Future
import os
import queue
import re
import secrets
import signal
import sys
import threading

from hermes_cli.plugin_host_wire import PluginHostUnsupported, encode

_OUTPUT = contextvars.ContextVar("plugin_cli_output", default=None)
_MAX_WRITE = 1024 * 1024


class NativeCliPump:
    """Run parsers/handlers on the host's main thread, including signal lifetimes."""
    def __init__(self, stopped):
        self.stopped = stopped
        self.jobs = queue.Queue(maxsize=16)
        self.running = False

    def call(self, callback):
        if self.stopped.is_set():
            raise PluginHostUnsupported("Plugin host is stopping")
        if threading.current_thread() is threading.main_thread():
            return callback()
        future = Future()
        self.jobs.put_nowait((future, contextvars.copy_context(), callback))
        return future.result()

    def serve(self):
        while not self.stopped.is_set():
            try:
                future, context, callback = self.jobs.get(timeout=.1)
            except queue.Empty:
                continue
            self.running = True
            try:
                future.set_result(context.run(callback))
            except BaseException as exc:
                future.set_exception(exc)
            finally:
                self.running = False

    def close(self, _reason):
        self.stopped.set()
        if self.running:
            os.kill(os.getpid(), signal.SIGINT)


class _RemoteOutput:
    def __init__(self, runtime, plugin, capability, stream):
        self.runtime, self.plugin, self.capability, self.stream = runtime, plugin, capability, stream

    def write(self, text):
        if len(text.encode("utf-8")) > _MAX_WRITE:
            raise PluginHostUnsupported("Native plugin CLI writes are limited to 1 MiB")
        self.runtime._send_call("cli_output", self.plugin, {
            "capability": self.capability, "stream": self.stream, "text": text,
        }, allow_objects=False)
        return len(text)

    def flush(self):
        self.runtime._send_call("cli_output", self.plugin, {
            "capability": self.capability, "stream": self.stream, "text": "", "flush": True,
        }, allow_objects=False)


class _ContextStream:
    def __init__(self, fallback, index):
        self.fallback, self.index = fallback, index

    def write(self, text):
        outputs = _OUTPUT.get()
        return (outputs[self.index] if outputs else self.fallback).write(text)

    def flush(self):
        outputs = _OUTPUT.get()
        return (outputs[self.index] if outputs else self.fallback).flush()

    def __getattr__(self, name):
        return getattr(self.fallback, name)


def route_native_output():
    sys.stdout = _ContextStream(sys.stdout, 0)
    sys.stderr = _ContextStream(sys.stderr, 1)


def native_executor(runtime, plugin, name, description, setup_fn, handler_fn):
    if not callable(setup_fn) or (handler_fn is not None and not callable(handler_fn)):
        raise TypeError("Native CLI setup and handler must be callable")

    def execute_on_main(argv, common, capability):
        token = _OUTPUT.set(tuple(_RemoteOutput(runtime, plugin, capability, stream) for stream in (0, 1)))
        try:
            parser = argparse.ArgumentParser(
                prog=f"hermes {name}", description=description,
                formatter_class=argparse.RawDescriptionHelpFormatter,
            )
            setup_fn(parser)
            args = parser.parse_args(argv, namespace=argparse.Namespace(**common))
            handler = handler_fn if handler_fn is not None else getattr(args, "func", None)
            if not callable(handler):
                parser.error("this command has no handler")
            result = runtime._run(handler(args))
            return result if type(result) is int else 0
        except SystemExit as exc:
            if type(exc.code) is int:
                return exc.code
            if exc.code is not None:
                sys.stderr.write(str(exc.code) + "\n")
                return 1
            return 0
        finally:
            _OUTPUT.reset(token)

    def execute(argv, common, capability):
        return runtime.native_cli.call(lambda: execute_on_main(argv, common, capability))

    return execute


def register_native_cli(host, params):
    ctx = host._plugin_ctx(params)
    name, help_text, description = params.get("name"), params.get("help"), params.get("description", "")
    if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]{0,63}", name):
        raise PluginHostUnsupported("Invalid hosted native CLI command name")
    if not isinstance(help_text, str) or len(help_text) > 16384 or not isinstance(description, str) or len(description) > 65536:
        raise PluginHostUnsupported("Invalid hosted native CLI help")
    refs = params.get("args")
    if not isinstance(refs, list) or len(refs) != 1 or not isinstance(refs[0], dict):
        raise PluginHostUnsupported("Hosted native CLI executor is missing")
    ref = refs[0].get("__callable__")
    if type(ref) is not int or ref <= 0:
        raise PluginHostUnsupported("Invalid hosted native CLI executor")
    generation = host._generation

    def setup(parser):
        parser.add_argument("_plugin_argv", nargs=argparse.REMAINDER)

    def run(args):
        common = {key: value for key, value in vars(args).items() if key not in {"func", "_plugin_argv"}}
        capability = secrets.token_urlsafe(32)
        host._native_cli_outputs[capability] = (ctx.plugin_id, generation, (sys.stdout, sys.stderr))
        try:
            result = host._call("invoke", {
                "ref": ref, "args": encode([args._plugin_argv, common, capability]), "kwargs": {},
            }, generation=generation)
        finally:
            host._native_cli_outputs.pop(capability, None)
        if type(result) is not int:
            raise PluginHostUnsupported("Invalid hosted native CLI result; mutation outcome may be unknown")
        return result

    handle = ctx.register_cli_command(name, help_text, setup, run, description)
    entry = ctx._manager._cli_commands.get(name)
    if entry is not None and entry.get("plugin_key") == ctx.plugin_id:
        entry["raw_argv"] = True
    return host._encode_for_host(handle)


def write_native_output(host, params):
    slot = host._native_cli_outputs.get(params.get("capability"))
    stream, text = params.get("stream"), params.get("text")
    if slot is None or slot[:2] != (params.get("plugin"), host._generation):
        raise PluginHostUnsupported("Native CLI output capability is not active for this plugin")
    if type(stream) is not int or stream not in (0, 1) or not isinstance(text, str) or len(text.encode("utf-8")) > _MAX_WRITE:
        raise PluginHostUnsupported("Invalid native CLI output")
    destination = slot[2][stream]
    destination.write(text)
    if params.get("flush") is True:
        destination.flush()
