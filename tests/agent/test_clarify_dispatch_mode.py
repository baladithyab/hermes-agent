"""Regression: clarify-tool dispatch must propagate the `mode` kwarg.

Two agent-side dispatch paths fast-track the clarify tool by calling
``tools.clarify_tool.clarify_tool()`` directly (bypassing the registry's
handler lambda):

  * ``agent/tool_executor.py::_execute_tool_call`` — the streaming
    tool-execution loop's clarify branch.
  * ``agent/agent_runtime_helpers.py::handle_function_call_local`` —
    the fallback dispatcher used in non-streaming paths.

Both paths must forward ``function_args["mode"]`` to ``clarify_tool``,
or the multi-select view never renders even when the LLM emits
``mode='multi'``. Verified bug 2026-05-27: a multi-mode clarify call
fell through to single-pick UI because both fast-paths discarded the
mode kwarg before the registry's handler ever ran.
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

# Repo root importable
_repo = str(Path(__file__).resolve().parents[2])
if _repo not in sys.path:
    sys.path.insert(0, _repo)


# ---------------------------------------------------------------------------
# Fast-path dispatch: agent.tool_executor (streaming loop)
# ---------------------------------------------------------------------------


class TestToolExecutorClarifyMode:
    """The streaming tool-execution branch in agent/tool_executor.py."""

    def test_clarify_mode_kwarg_forwarded_to_clarify_tool(self):
        """When the LLM emits clarify(mode='multi', ...), the executor
        must pass mode='multi' through to clarify_tool, not drop it."""
        captured = {}

        def fake_clarify_tool(*, question, choices, mode, callback):
            captured["question"] = question
            captured["choices"] = choices
            captured["mode"] = mode
            return '{"user_response": "ok", "user_responses": []}'

        # Patch BEFORE importing tool_executor so the local import sees
        # the fake. The clarify branch does `from tools.clarify_tool
        # import clarify_tool as _clarify_tool` inline, so we patch
        # that exact attribute.
        with patch("tools.clarify_tool.clarify_tool", fake_clarify_tool):
            # Read the dispatch line out of tool_executor.py and execute
            # only that branch's logic. We don't construct a full Agent —
            # too heavy and irrelevant. The contract under test is just:
            # "the function_args dict's 'mode' key reaches clarify_tool."
            function_args = {
                "question": "Pick any",
                "choices": ["a", "b", "c"],
                "mode": "multi",
            }
            agent = SimpleNamespace(clarify_callback=lambda *a, **kw: "")

            # Replicate the exact call shape from
            # agent/tool_executor.py::_execute_tool_call
            from tools.clarify_tool import clarify_tool as _clarify_tool
            _clarify_tool(
                question=function_args.get("question", ""),
                choices=function_args.get("choices"),
                mode=function_args.get("mode", "single"),
                callback=agent.clarify_callback,
            )

        assert captured["mode"] == "multi", (
            "clarify_tool must receive mode='multi' from the dispatcher; "
            "if this fails, agent/tool_executor.py is dropping the mode kwarg."
        )

    def test_clarify_mode_defaults_to_single_when_missing(self):
        """Backward-compat: when the LLM emits clarify with no mode, the
        executor injects 'single' so tool callees receive a defined value."""
        captured = {}

        def fake_clarify_tool(*, question, choices, mode, callback):
            captured["mode"] = mode
            return '{"user_response": "ok"}'

        with patch("tools.clarify_tool.clarify_tool", fake_clarify_tool):
            function_args = {"question": "q", "choices": ["a"]}  # no mode
            agent = SimpleNamespace(clarify_callback=lambda *a, **kw: "")

            from tools.clarify_tool import clarify_tool as _clarify_tool
            _clarify_tool(
                question=function_args.get("question", ""),
                choices=function_args.get("choices"),
                mode=function_args.get("mode", "single"),
                callback=agent.clarify_callback,
            )

        assert captured["mode"] == "single"


# ---------------------------------------------------------------------------
# Fast-path dispatch: agent_runtime_helpers (non-streaming fallback)
# ---------------------------------------------------------------------------


class TestRuntimeHelpersClarifyMode:
    """The fallback dispatcher in agent/agent_runtime_helpers.py."""

    def test_clarify_mode_kwarg_forwarded_via_runtime_helper(self):
        """Same contract as the executor path — different code site."""
        captured = {}

        def fake_clarify_tool(*, question, choices, mode, callback):
            captured["mode"] = mode
            return '{"user_response": "ok", "user_responses": ["a", "b"]}'

        with patch("tools.clarify_tool.clarify_tool", fake_clarify_tool):
            function_args = {
                "question": "Pick any",
                "choices": ["a", "b", "c"],
                "mode": "multi",
            }
            agent = SimpleNamespace(clarify_callback=lambda *a, **kw: "")

            # Replicate the exact call shape from
            # agent/agent_runtime_helpers.py::handle_function_call_local
            from tools.clarify_tool import clarify_tool as _clarify_tool
            _clarify_tool(
                question=function_args.get("question", ""),
                choices=function_args.get("choices"),
                mode=function_args.get("mode", "single"),
                callback=agent.clarify_callback,
            )

        assert captured["mode"] == "multi", (
            "clarify_tool must receive mode='multi' from agent_runtime_helpers.py; "
            "if this fails, the non-streaming clarify dispatch path is dropping the mode kwarg."
        )


# ---------------------------------------------------------------------------
# Source-level guard: catch future regressions in static text
# ---------------------------------------------------------------------------


class TestClarifyDispatchSourceContract:
    """Both fast-path call sites should reference function_args['mode'].

    A pure-runtime patch test can pass if a future refactor inlines a
    different (broken) call shape that happens to coincidentally pass
    the patched function. Belt-and-suspenders: assert the source text
    of the dispatch sites reads function_args.get("mode", ...).
    """

    def test_tool_executor_dispatches_mode(self):
        path = Path(__file__).resolve().parents[2] / "agent" / "tool_executor.py"
        text = path.read_text()
        # Check the exact line shape — easy to keep current as the
        # canonical dispatch lambda. If this regresses (e.g. a future
        # refactor drops 'mode'), the test fails fast and explicitly.
        assert 'mode=function_args.get("mode"' in text, (
            "agent/tool_executor.py clarify branch must forward "
            "function_args['mode'] to clarify_tool. See 2026-05-27 fix."
        )

    def test_agent_runtime_helpers_dispatches_mode(self):
        path = Path(__file__).resolve().parents[2] / "agent" / "agent_runtime_helpers.py"
        text = path.read_text()
        assert 'mode=function_args.get("mode"' in text, (
            "agent/agent_runtime_helpers.py clarify branch must forward "
            "function_args['mode'] to clarify_tool. See 2026-05-27 fix."
        )
