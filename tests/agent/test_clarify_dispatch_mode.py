"""Clarify fast paths must forward the multi_select argument."""

import json
from pathlib import Path
from types import SimpleNamespace


def _make_agent():
    class _Guardrails:
        def before_call(self, *_args, **_kwargs):
            return SimpleNamespace(allows_execution=True)

    return SimpleNamespace(
        _interrupt_requested=False,
        _tool_guardrails=_Guardrails(),
        quiet_mode=True,
        verbose_logging=False,
        tool_progress_mode="off",
        tool_progress_callback=None,
        tool_start_callback=None,
        tool_complete_callback=None,
        _checkpoint_mgr=SimpleNamespace(enabled=False),
        _should_emit_quiet_tool_messages=lambda: False,
        _vprint=lambda *_args, **_kwargs: None,
        clarify_callback=lambda *_args, **_kwargs: "callback-result",
        _current_tool=None,
        _touch_activity=lambda *_args, **_kwargs: None,
        _context_engine_tool_names=set(),
        _memory_manager=None,
        session_id="session-1",
        valid_tool_names=set(),
        enabled_toolsets=None,
        disabled_toolsets=None,
        _current_turn_id="turn-1",
        _current_api_request_id="request-1",
        _append_guardrail_observation=lambda _name, _args, result, failed=False: result,
        _record_file_mutation_result=lambda *_args, **_kwargs: None,
        _subdirectory_hints=SimpleNamespace(
            check_tool_call=lambda *_args, **_kwargs: ""
        ),
        _tool_result_content_for_active_model=lambda _name, result: result,
        _flush_messages_to_session_db=lambda _messages: None,
        _apply_pending_steer_to_tool_results=lambda *_args, **_kwargs: None,
        tool_delay=0,
        log_prefix="",
        log_prefix_chars=200,
    )


def test_runtime_fast_path_forwards_multi_select(monkeypatch):
    from agent.agent_runtime_helpers import invoke_tool
    import hermes_cli.plugins as plugins
    import tools.clarify_tool as clarify_mod

    monkeypatch.setattr(
        plugins,
        "get_pre_tool_call_block_message",
        lambda *_args, **_kwargs: None,
    )
    captured = {}

    def fake_clarify_tool(
        question="",
        choices=None,
        multi_select=False,
        questions=None,
        callback=None,
    ):
        captured.update(
            question=question,
            choices=choices,
            multi_select=multi_select,
            questions=questions,
            callback=callback,
        )
        return json.dumps({"ok": True})

    monkeypatch.setattr(clarify_mod, "clarify_tool", fake_clarify_tool)
    agent = _make_agent()

    invoke_tool(
        agent,
        "clarify",
        {"question": "Pick", "choices": ["a", "b"], "multi_select": True},
        effective_task_id="task-1",
        tool_call_id="tool-1",
    )

    assert captured["multi_select"] is True
    assert captured["callback"] is agent.clarify_callback


def test_both_fast_paths_have_source_guard():
    root = Path(__file__).resolve().parents[2]
    for relative in (
        "agent/tool_executor.py",
        "agent/agent_runtime_helpers.py",
    ):
        source = (root / relative).read_text(encoding="utf-8")
        assert 'multi_select=next_args.get("multi_select", False)' in source
