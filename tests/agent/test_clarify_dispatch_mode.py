"""Both agent dispatch paths preserve per-question multi-select and the injected callback."""

import json
from types import SimpleNamespace

import pytest


def _runtime_dispatch(agent, args):
    from agent.agent_runtime_helpers import invoke_tool

    return invoke_tool(
        agent, "clarify", args, effective_task_id="task-1", tool_call_id="tool-1",
        pre_tool_block_checked=True, skip_tool_request_middleware=True,
        skip_tool_execution_middleware=True,
    )


def _sequential_dispatch(agent, args):
    from agent.tool_executor import _ToolCallRef, _resolve_sequential_dispatch

    ref = _ToolCallRef(name="clarify", args=args, task_id="task-1", call_id="tool-1", trace=[])
    return _resolve_sequential_dispatch(agent, ref, messages=[]).execute(args)


@pytest.mark.parametrize("dispatch", [_runtime_dispatch, _sequential_dispatch], ids=["runtime", "sequential"])
def test_dispatch_forwards_question_modes_and_agent_callback(dispatch):
    seen = []

    def callback(questions):
        seen.append(questions)
        return {"answers": {"q0": '["red", "blue"]', "q1": "green"}, "outcome": "submitted"}

    agent = SimpleNamespace(
        clarify_callback=callback, _memory_manager=None, session_id="session-1",
        _current_turn_id="turn-1", _current_api_request_id="request-1",
    )
    result = json.loads(dispatch(agent, {"questions": [
        {"question": "Pick colors", "choices": ["red", "blue"], "multi_select": True},
        {"question": "Pick one", "choices": ["green", "yellow"]},
    ]}))

    assert len(seen) == 1
    assert [question["multi_select"] for question in seen[0]] == [True, False]
    assert result["outcome"] == "submitted"
    assert [response["user_response"] for response in result["responses"]] == [["red", "blue"], "green"]
