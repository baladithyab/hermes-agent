"""Per-task routing through real config, /model, runtime credentials and child assembly.

Only the child agent/runner is replaced: no inference is sent. A loopback model
catalog exercises the real validation path without an external provider.
"""

import json
import os
import threading
import unittest
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from jsonschema import Draft7Validator

from tests.tools.test_delegate import _make_mock_parent
from tools.delegate_tool import DELEGATE_TASK_SCHEMA, delegate_task
from tools.delegate_tool_dispatch import _dispatch_unit
from tools.registry import registry


_MODELS = ["parent-model", "config-model", "top-model", "task-model", "owner-model"]


class _Catalog(BaseHTTPRequestHandler):
    def do_GET(self):
        body = json.dumps({"data": [{"id": model} for model in _MODELS]}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):  # noqa: A002
        pass


@pytest.fixture
def routing_home():
    server = HTTPServer(("127.0.0.1", 0), _Catalog)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    urls = {name: f"http://127.0.0.1:{server.server_port}/{name}/v1"
            for name in ("parent", "config", "top", "worker", "direct")}
    # Two named providers may use the same gateway but different credentials.
    urls["worker"] = urls["top"]

    def configure(kind):
        route = {
            "inherit": {},
            "config": {"model": "config-model", "provider": "custom:config-route"},
            "direct": {"model": "config-model", "provider": "descriptive-local", "base_url": urls["direct"],
                       "api_key": "direct-test-key", "api_mode": "anthropic_messages"},
            "broken": {"model": "config-model", "provider": "missing-delegation-provider"},
            "owner": {"model": "config-model", "provider": "custom:config-route"},
        }[kind]
        config = {
            "model": {"default": "parent-model", "provider": "custom:parent-route"},
            "providers": {
                f"{name}-route": {"base_url": url, "api_key": f"{name}-test-key",
                                 "api_mode": "chat_completions", "models": _MODELS,
                                 "extra_body": {"route": name}}
                for name, url in urls.items() if name != "direct"
            },
            "delegation": {"max_concurrent_children": 10, "max_spawn_depth": 2, "max_iterations": 5,
                           "request_overrides": {"extra_body": {"delegated": True}}, **route},
        }
        path = Path(os.environ["HERMES_HOME"]) / "config.yaml"
        path.write_text(json.dumps(config), encoding="utf-8")
        return config

    try:
        yield urls, configure
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def _parent(urls):
    parent = _make_mock_parent()
    parent.model = "parent-model"
    parent.provider = "custom"
    parent.requested_provider = "custom:parent-route"
    parent.base_url = urls["parent"]
    parent.api_key = "parent-test-key"
    parent._client_kwargs = {"base_url": urls["parent"], "api_key": parent.api_key}
    parent.client = None
    parent._credential_pool = None
    parent.enabled_toolsets = ["file"]
    parent.disabled_toolsets = []
    parent.capabilities = {"openai_native_compaction": True}
    parent.providers_allowed = ["parent-only-filter"]
    parent.request_overrides = {"extra_body": {"parent_only": True}}
    parent.acp_command = "parent-only-command"
    parent.acp_args = ["--parent"]
    parent._fallback_chain = [{"provider": "custom:parent-route", "model": "parent-model"}]
    return parent


@pytest.mark.parametrize(
    "config_kind,call,task_routes,expected",
    [
        ("inherit", {}, [{}], [("parent-model", "parent")]),
        ("config", {}, [{}], [("config-model", "config")]),
        ("config", {"model": "top-model"}, [{}], [("top-model", "config")]),
        ("inherit", {}, [{"model": "task-model"}], [("task-model", "parent")]),
        ("config", {"provider": "custom:top-route"}, [{}], [("config-model", "top")]),
        ("inherit", {"provider": "custom:worker-route"}, [{}], [("parent-model", "worker")]),
        ("config", {"model": "--provider custom:top-route"}, [{}], [("config-model", "top")]),
        ("config", {"model": "top-model", "provider": "custom:top-route"},
         [{"model": "task-model"}, {"provider": "custom:worker-route"}, {}],
         [("task-model", "top"), ("top-model", "worker"), ("top-model", "top")]),
        ("config", {"model": "top-model", "provider": "custom:top-route"},
         [{"model": "task-model", "provider": "custom:worker-route"}, {"model": " ", "provider": " "}],
         [("task-model", "worker"), ("top-model", "top")]),
        ("direct", {"model": "top-model"}, [{}], [("top-model", "direct")]),
        ("direct", {"provider": "custom:worker-route"}, [{}], [("config-model", "worker")]),
        ("broken", {"model": "top-model", "provider": "custom:top-route"}, [{}], [("top-model", "top")]),
        ("config", {"model": "top-model --provider custom:top-route"}, [{}], [("top-model", "top")]),
        ("config", {}, [{"model": "task-model --provider custom:worker-route"}], [("task-model", "worker")]),
        ("inherit", {"goal": "Inspect legacy single-task routing", "model": "top-model",
                     "provider": "custom:top-route"}, None, [("top-model", "top")]),
        ("owner", {"model": "top-model"}, [{}], [("top-model", "worker")]),
    ],
)
def test_routes_reach_children_with_isolated_credentials(routing_home, config_kind, call, task_routes, expected):
    urls, configure = routing_home
    configure(config_kind)
    parent = _parent(urls)
    # Surface state can lag the live client; the inherited URL and key travel together.
    if config_kind == "inherit":
        parent.base_url, parent.api_key = "https://stale.example/v1", "stale-test-key"
    args = dict(call)
    if task_routes is not None:
        args["tasks"] = [{"goal": f"Inspect routing contract for child {i}", **route}
                         for i, route in enumerate(task_routes)]
    validator = Draft7Validator(DELEGATE_TASK_SCHEMA["parameters"])
    for field in ("model", "provider"):
        assert list(validator.iter_errors({"tasks": [{"goal": "Inspect routing"}], field: 42}))
        assert list(validator.iter_errors({"tasks": [{"goal": "Inspect routing", field: 42}]}))
    validator.validate(args)
    built, batches = [], []

    def construct(**kwargs):
        built.append(kwargs)
        child = MagicMock(model=kwargs["model"], session_id=f"child-{len(built)}")
        child._session_init_model_config = {}
        return child

    def capture_batch(batch, background):
        batches.append(batch)
        return json.dumps({"status": "dispatched"})

    owner = {"model": "owner-model", "provider": "custom:worker-route",
             "fallback_providers": [{"provider": "custom:worker-route", "model": "owner-model"}]}
    with patch("run_agent.AIAgent", side_effect=construct), patch("tools.delegate_tool._run_batch", capture_batch):
        if config_kind == "owner":
            result = json.loads(delegate_task(**args, credentials_cfg=owner, parent_agent=parent))
        else:
            result = json.loads(registry.get_entry("delegate_task").handler(args, parent_agent=parent))
    assert "error" not in result, result
    assert len(built) == len(expected)
    for kwargs, (model, route) in zip(built, expected):
        assert kwargs["model"] == model
        assert kwargs["base_url"] == urls[route]
        assert kwargs["api_key"] == f"{route}-test-key"
        assert kwargs["requested_provider"] == ("custom" if route == "direct" else f"custom:{route}-route")
        assert kwargs["api_mode"] == ("anthropic_messages" if route == "direct" else "chat_completions")
        if config_kind != "owner":
            assert kwargs["request_overrides"]["extra_body"]["delegated"] is True
        inherited = config_kind == "inherit" and not call and task_routes == [{}]
        if inherited:
            assert kwargs["fallback_model"] == parent._fallback_chain
            assert kwargs["capabilities"] == parent.capabilities
            assert kwargs["providers_allowed"] == parent.providers_allowed
            assert kwargs["acp_command"] == parent.acp_command
        else:
            assert kwargs["fallback_model"] == (owner["fallback_providers"] if config_kind == "owner" else None)
            assert kwargs["capabilities"] is None
            assert kwargs["providers_allowed"] is None
            assert kwargs["acp_command"] is None

    # A mixed completion never claims every child used one model. A single-child
    # completion unit still reports its own model when the call contains others.
    batch = batches[0]
    with patch("tools.async_delegation.dispatch_async_delegation_batch", return_value={}) as dispatch:
        _dispatch_unit(batch, None, None, {})
        models = {model for model, _ in expected}
        assert dispatch.call_args.kwargs["model"] == (next(iter(models)) if len(models) == 1 else None)
        for item, (model, _) in zip(batch.children, expected):
            _dispatch_unit(replace(batch, children=[item]), None, None, {})
            assert dispatch.call_args.kwargs["model"] == model


@pytest.mark.parametrize("failure", ["task_conflict", "top_conflict", "unknown_provider", "missing_key", "missing_command", "runtime_error"])
def test_invalid_task_route_rejects_batch_before_child_construction(routing_home, failure):
    from hermes_cli.runtime_provider import resolve_runtime_provider

    urls, configure = routing_home
    config = configure("config")
    if failure == "missing_key":
        config["providers"]["worker-route"]["api_key"] = ""
        (Path(os.environ["HERMES_HOME"]) / "config.yaml").write_text(json.dumps(config), encoding="utf-8")
    parent = _parent(urls)
    args = {"tasks": [
        {"goal": "Inspect the valid first task"},
        {"goal": "Inspect the invalid second task", "model": "task-model", "provider": "custom:worker-route"},
    ]}
    invalid = args["tasks"][1]
    if failure in {"task_conflict", "top_conflict"}:
        invalid["model"] += " --provider custom:worker-route"
        if failure == "top_conflict":
            args["provider"] = invalid.pop("provider")
        message = "Cannot specify both 'provider' parameter and inline '--provider'"
    elif failure == "unknown_provider":
        invalid["provider"] = "missing-task-provider"
        message = "Unknown provider"
    else:
        message = {"missing_key": "has no API key", "missing_command": "not found on PATH",
                   "runtime_error": "test runtime unavailable"}[failure]

    def resolve(**kwargs):
        runtime = resolve_runtime_provider(**kwargs)
        if kwargs["requested"] == "custom:worker-route":
            if failure == "missing_key":
                runtime["api_key"] = ""
            elif failure == "missing_command":
                runtime["command"] = str(Path(os.environ["HERMES_HOME"]) / "missing-provider-command")
            elif failure == "runtime_error":
                raise ValueError("test runtime unavailable")
        return runtime

    with (
        patch("hermes_cli.runtime_provider.resolve_runtime_provider", side_effect=resolve),
        patch("run_agent.AIAgent") as construct,
        patch("tools.delegate_tool._run_batch") as run,
    ):
        result = json.loads(registry.get_entry("delegate_task").handler(args, parent_agent=parent))
    assert message in result["error"]
    construct.assert_not_called()
    run.assert_not_called()
    assert parent._active_children == []


class TestDispatchDelegateTask(unittest.TestCase):
    """Tests for the _dispatch_delegate_task helper and full param forwarding."""

    def test_routes_forwarded_but_acp_args_remain_internal(self):
        """Both model dispatch paths keep routes and strip trusted-only ACP fields."""
        import run_agent
        from tools.registry import registry

        captured = {}

        def fake_delegate_task(**kwargs):
            captured.update(kwargs)
            return "{}"

        parent = _make_mock_parent(depth=0)
        args = {
            "goal": "test",
            "model": "top-model",
            "provider": "top-provider",
            "acp_command": "claude",
            "acp_args": ["--acp", "--stdio"],
            "tasks": [{
                "goal": "nested",
                "model": "task-model",
                "provider": "task-provider",
                "acp_command": "codex",
                "acp_args": ["--acp"],
            }],
        }
        for dispatch in (
            lambda: run_agent.AIAgent._dispatch_delegate_task(parent, args),
            lambda: registry.get_entry("delegate_task").handler(args, parent_agent=parent),
        ):
            captured.clear()
            with patch("tools.delegate_tool.delegate_task", fake_delegate_task):
                dispatch()
            self.assertEqual(captured["model"], args["model"])
            self.assertEqual(captured["provider"], args["provider"])
            self.assertEqual(captured["tasks"][0]["model"], args["tasks"][0]["model"])
            self.assertEqual(captured["tasks"][0]["provider"], args["tasks"][0]["provider"])
            self.assertNotIn("acp_command", captured)
            self.assertNotIn("acp_args", captured)
            self.assertNotIn("acp_command", captured["tasks"][0])
            self.assertNotIn("acp_args", captured["tasks"][0])
            self.assertTrue(captured["background"])
        # Filtering must not mutate the original tool call stored in the transcript.
        self.assertIn("acp_command", args["tasks"][0])
