#!/usr/bin/env python3
"""
Clarify Tool Module - Interactive Clarifying Questions

Allows the agent to present structured multiple-choice questions or open-ended
prompts to the user. In CLI mode, choices are navigable with arrow keys. On
messaging platforms, choices are rendered as a numbered list.

The actual user-interaction logic lives in the platform layer (cli.py for CLI,
gateway/run.py for messaging). This module defines the schema, validation, and
a thin dispatcher that delegates to a platform-provided callback.
"""

import json
from typing import List, Optional, Callable


# Maximum number of predefined choices the agent can offer.
# A 5th "Other (type your answer)" option is always appended by the UI.
MAX_CHOICES = 4

# Sentinel used by multi-select Discord views to join the user's selected
# choices into a single string for transit through the gateway clarify
# primitive (which deals in opaque str responses). Picked to be the same
# byte the Discord adapter emits — keep them in sync.
_MULTI_SEP = "\x1f"


def clarify_tool(
    question: str,
    choices: Optional[List[str]] = None,
    mode: str = "single",
    callback: Optional[Callable] = None,
) -> str:
    """
    Ask the user a question, optionally with multiple-choice options.

    Args:
        question: The question text to present.
        choices:  Up to 4 predefined answer choices. When omitted the
                  question is purely open-ended.
        mode:     ``"single"`` (default) or ``"multi"``. Multi-select
                  prompts the user to pick zero or more choices and is
                  currently supported on Discord only — other platforms
                  fall back to single-pick. Ignored when ``choices`` is
                  omitted (open-ended).
        callback: Platform-provided function that handles the actual UI
                  interaction. Signature:
                  ``callback(question, choices, *, mode='single') -> str``.
                  Older callbacks without the ``mode`` kwarg still work —
                  this dispatcher detects and falls back. Injected by the
                  agent runner (cli.py / gateway).

    Returns:
        JSON string with the user's response. Single-pick:
        ``{"user_response": "<choice>"}``. Multi-select adds
        ``"user_responses": [<choice>, ...]`` and sets ``user_response``
        to the joined-with-comma form for legacy callers.
    """
    if not question or not question.strip():
        return tool_error("Question text is required.")

    question = question.strip()

    # Validate mode — quietly normalize unknown modes to "single" rather
    # than failing the tool call (matches existing tolerant behavior).
    mode = (mode or "single").strip().lower()
    if mode not in ("single", "multi"):
        mode = "single"

    # Validate and trim choices
    if choices is not None:
        if not isinstance(choices, list):
            return tool_error("choices must be a list of strings.")
        choices = [str(c).strip() for c in choices if str(c).strip()]
        if len(choices) > MAX_CHOICES:
            choices = choices[:MAX_CHOICES]
        if not choices:
            choices = None  # empty list → open-ended

    # Multi mode requires choices — open-ended multi makes no sense.
    if mode == "multi" and not choices:
        mode = "single"

    if callback is None:
        return json.dumps(
            {"error": "Clarify tool is not available in this execution context."},
            ensure_ascii=False,
        )

    try:
        # Forward-compat: try modern signature with ``mode`` kwarg, fall
        # back to legacy positional signature for callbacks that haven't
        # been updated yet (CLI, older custom embeddings).
        try:
            user_response = callback(question, choices, mode=mode)
        except TypeError:
            user_response = callback(question, choices)
    except Exception as exc:
        return json.dumps(
            {"error": f"Failed to get user input: {exc}"},
            ensure_ascii=False,
        )

    raw = str(user_response if user_response is not None else "").strip()
    payload = {
        "question": question,
        "choices_offered": choices,
        "user_response": raw,
    }
    if mode == "multi":
        # Split on the multi-select separator. Empty selections are
        # represented as an empty list (user clicked Submit without
        # picking anything).
        parts = [p.strip() for p in raw.split(_MULTI_SEP) if p.strip()] if raw else []
        payload["user_responses"] = parts
        # Keep ``user_response`` populated for callers that haven't been
        # updated to read ``user_responses`` — comma-joined for human
        # readability.
        payload["user_response"] = ", ".join(parts)
    return json.dumps(payload, ensure_ascii=False)


def check_clarify_requirements() -> bool:
    """Clarify tool has no external requirements -- always available."""
    return True


# =============================================================================
# OpenAI Function-Calling Schema
# =============================================================================

CLARIFY_SCHEMA = {
    "name": "clarify",
    "description": (
        "Ask the user a question when you need clarification, feedback, or a "
        "decision before proceeding. Supports two modes:\n\n"
        "1. **Multiple choice** — provide up to 4 choices. The user picks one "
        "or types their own answer via a 5th 'Other' option.\n"
        "2. **Open-ended** — omit choices entirely. The user types a free-form "
        "response.\n\n"
        "By default the user picks ONE choice. Set `mode: \"multi\"` for "
        "multi-select where the user can pick zero or more (Discord only; "
        "other platforms fall back to single-pick). Returns "
        "`user_responses: [<choice>, ...]` alongside the legacy "
        "`user_response` (joined with commas) when in multi mode.\n\n"
        "Use this tool when:\n"
        "- The task is ambiguous and you need the user to choose an approach\n"
        "- You want post-task feedback ('How did that work out?')\n"
        "- You want to offer to save a skill or update memory\n"
        "- A decision has meaningful trade-offs the user should weigh in on\n\n"
        "Do NOT use this tool for simple yes/no confirmation of dangerous "
        "commands (the terminal tool handles that). Prefer making a reasonable "
        "default choice yourself when the decision is low-stakes."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "question": {
                "type": "string",
                "description": "The question to present to the user.",
            },
            "choices": {
                "type": "array",
                "items": {"type": "string"},
                "maxItems": MAX_CHOICES,
                "description": (
                    "Up to 4 answer choices. Omit this parameter entirely to "
                    "ask an open-ended question. When provided, the UI "
                    "automatically appends an 'Other (type your answer)' option."
                ),
            },
            "mode": {
                "type": "string",
                "enum": ["single", "multi"],
                "description": (
                    "Selection mode. 'single' (default) — user picks exactly "
                    "one choice. 'multi' — user picks zero or more, then "
                    "presses Submit. Multi mode is currently rendered "
                    "natively only on Discord; other platforms fall back to "
                    "single-pick UI. Ignored when `choices` is omitted."
                ),
            },
        },
        "required": ["question"],
    },
}


# --- Registry ---
from tools.registry import registry, tool_error

registry.register(
    name="clarify",
    toolset="clarify",
    schema=CLARIFY_SCHEMA,
    handler=lambda args, **kw: clarify_tool(
        question=args.get("question", ""),
        choices=args.get("choices"),
        mode=args.get("mode", "single"),
        callback=kw.get("callback")),
    check_fn=check_clarify_requirements,
    emoji="❓",
)
