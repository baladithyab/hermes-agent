"""Separate cold host infrastructure setup from measured plugin operations."""
import json
import os
import time
from pathlib import Path


def warm_host_infrastructure(host, directory, *, web_server=False, modules=()):
    """Warm a different fixture API in the same child; the caller owns host teardown."""
    directory = Path(directory)
    directory.mkdir()
    imports = list(dict.fromkeys(("fastapi", "httpx", *modules,
                                 *(("hermes_cli.web_server",) if web_server else ()))))
    schema = directory / "config_schema.py"
    schema.write_text(
        "import importlib, os\n"
        f"modules = {imports!r}\n"
        "for name in modules: importlib.import_module(name)\n"
        "CONFIG_SCHEMA = {'pid': os.getpid(), 'home': os.environ['HERMES_HOME'], 'modules': modules}\n",
        encoding="utf-8")
    (directory / "plugin_api.py").write_text(
        "import os\nfrom fastapi import APIRouter\nrouter = APIRouter()\n"
        "@router.get('/ready')\n"
        "def ready(): return {'ready': True, 'pid': os.getpid()}\n",
        encoding="utf-8")
    started = time.monotonic()
    host.ensure_started()
    process = host._proc
    assert process is not None and process.pid != os.getpid()
    facts = host._call("config_schema", {"path": str(schema)}, timeout=60)
    assert facts == {"pid": process.pid, "home": str(host._home), "modules": imports}
    schema_done = time.monotonic()
    plugin = "fixture-infrastructure-ready"
    try:
        reply = host.asgi_request(plugin, str(directory), "plugin_api.py",
                                  "GET", "/ready", "", [], b"")
        assert reply["status"] == 200, reply["status"]
        assert json.loads(reply["body"]) == {"ready": True, "pid": process.pid}
    finally:
        host._unload(plugin)
    assert host._proc is process and host.pid == process.pid and process.poll() is None
    facts.update(schema_seconds=schema_done - started,
                 request_seconds=time.monotonic() - schema_done)
    print("plugin-host infrastructure ready", facts, flush=True)
    return facts
