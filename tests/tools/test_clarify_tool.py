"""Tests for tools/clarify_tool.py - Interactive clarifying questions."""

import json
from typing import List, Optional


from tools.clarify_tool import (
    clarify_tool,
    check_clarify_requirements,
    MAX_CHOICES,
    CLARIFY_SCHEMA,
)


class TestClarifyToolBasics:
    """Basic functionality tests for clarify_tool."""

    def test_simple_question_with_callback(self):
        """Should return user response for simple question."""
        def mock_callback(question: str, choices: Optional[List[str]]) -> str:
            assert question == "What color?"
            assert choices is None
            return "blue"

        result = json.loads(clarify_tool("What color?", callback=mock_callback))
        assert result["question"] == "What color?"
        assert result["choices_offered"] is None
        assert result["user_response"] == "blue"

    def test_question_with_choices(self):
        """Should pass choices to callback and return response."""
        def mock_callback(question: str, choices: Optional[List[str]]) -> str:
            assert question == "Pick a number"
            assert choices == ["1", "2", "3"]
            return "2"

        result = json.loads(clarify_tool(
            "Pick a number",
            choices=["1", "2", "3"],
            callback=mock_callback
        ))
        assert result["question"] == "Pick a number"
        assert result["choices_offered"] == ["1", "2", "3"]
        assert result["user_response"] == "2"

    def test_empty_question_returns_error(self):
        """Should return error for empty question."""
        result = json.loads(clarify_tool("", callback=lambda q, c: "ignored"))
        assert "error" in result
        assert "required" in result["error"].lower()

    def test_whitespace_only_question_returns_error(self):
        """Should return error for whitespace-only question."""
        result = json.loads(clarify_tool("   \n\t  ", callback=lambda q, c: "ignored"))
        assert "error" in result

    def test_no_callback_returns_error(self):
        """Should return error when no callback is provided."""
        result = json.loads(clarify_tool("What do you want?"))
        assert "error" in result
        assert "not available" in result["error"].lower()


class TestClarifyToolChoicesValidation:
    """Tests for choices parameter validation."""

    def test_choices_trimmed_to_max(self):
        """Should trim choices to MAX_CHOICES."""
        choices_passed = []

        def mock_callback(question: str, choices: Optional[List[str]]) -> str:
            choices_passed.extend(choices or [])
            return "picked"

        many_choices = ["a", "b", "c", "d", "e", "f", "g"]
        clarify_tool("Pick one", choices=many_choices, callback=mock_callback)

        assert len(choices_passed) == MAX_CHOICES

    def test_empty_choices_become_none(self):
        """Empty choices list should become None (open-ended)."""
        choices_received = ["marker"]

        def mock_callback(question: str, choices: Optional[List[str]]) -> str:
            choices_received.clear()
            if choices is not None:
                choices_received.extend(choices)
            return "answer"

        clarify_tool("Open question?", choices=[], callback=mock_callback)
        assert choices_received == []  # Was cleared, nothing added

    def test_choices_with_only_whitespace_stripped(self):
        """Whitespace-only choices should be stripped out."""
        choices_received = []

        def mock_callback(question: str, choices: Optional[List[str]]) -> str:
            choices_received.extend(choices or [])
            return "answer"

        clarify_tool("Pick", choices=["valid", "  ", "", "also valid"], callback=mock_callback)
        assert choices_received == ["valid", "also valid"]

    def test_invalid_choices_type_returns_error(self):
        """Non-list choices should return error."""
        result = json.loads(clarify_tool(
            "Question?",
            choices="not a list",  # type: ignore
            callback=lambda q, c: "ignored"
        ))
        assert "error" in result
        assert "list" in result["error"].lower()

    def test_choices_converted_to_strings(self):
        """Non-string choices should be converted to strings."""
        choices_received = []

        def mock_callback(question: str, choices: Optional[List[str]]) -> str:
            choices_received.extend(choices or [])
            return "answer"

        clarify_tool("Pick", choices=[1, 2, 3], callback=mock_callback)  # type: ignore
        assert choices_received == ["1", "2", "3"]


class TestClarifyToolCallbackHandling:
    """Tests for callback error handling."""

    def test_callback_exception_returns_error(self):
        """Should return error if callback raises exception."""
        def failing_callback(question: str, choices: Optional[List[str]]) -> str:
            raise RuntimeError("User cancelled")

        result = json.loads(clarify_tool("Question?", callback=failing_callback))
        assert "error" in result
        assert "Failed to get user input" in result["error"]
        assert "User cancelled" in result["error"]

    def test_callback_receives_stripped_question(self):
        """Callback should receive trimmed question."""
        received_question = []

        def mock_callback(question: str, choices: Optional[List[str]]) -> str:
            received_question.append(question)
            return "answer"

        clarify_tool("  Question with spaces  \n", callback=mock_callback)
        assert received_question[0] == "Question with spaces"

    def test_user_response_stripped(self):
        """User response should be stripped of whitespace."""
        def mock_callback(question: str, choices: Optional[List[str]]) -> str:
            return "  response with spaces  \n"

        result = json.loads(clarify_tool("Q?", callback=mock_callback))
        assert result["user_response"] == "response with spaces"


class TestCheckClarifyRequirements:
    """Tests for the requirements check function."""

    def test_always_returns_true(self):
        """clarify tool has no external requirements."""
        assert check_clarify_requirements() is True


class TestClarifySchema:
    """Tests for the OpenAI function-calling schema."""

    def test_schema_name(self):
        """Schema should have correct name."""
        assert CLARIFY_SCHEMA["name"] == "clarify"

    def test_schema_has_description(self):
        """Schema should have a description."""
        assert "description" in CLARIFY_SCHEMA
        assert len(CLARIFY_SCHEMA["description"]) > 50

    def test_schema_question_required(self):
        """Question parameter should be required."""
        assert "question" in CLARIFY_SCHEMA["parameters"]["required"]

    def test_schema_choices_optional(self):
        """Choices parameter should be optional."""
        assert "choices" not in CLARIFY_SCHEMA["parameters"]["required"]

    def test_schema_choices_max_items(self):
        """Schema should specify max items for choices."""
        choices_spec = CLARIFY_SCHEMA["parameters"]["properties"]["choices"]
        assert choices_spec.get("maxItems") == MAX_CHOICES

    def test_max_choices_is_four(self):
        """MAX_CHOICES constant should be 4."""
        assert MAX_CHOICES == 4

    def test_schema_advertises_mode_parameter(self):
        """Schema should expose `mode` with the single|multi enum."""
        mode_spec = CLARIFY_SCHEMA["parameters"]["properties"].get("mode")
        assert mode_spec is not None, "schema must advertise the new mode parameter"
        assert mode_spec.get("enum") == ["single", "multi"]
        # Mode is optional — single is the default
        assert "mode" not in CLARIFY_SCHEMA["parameters"]["required"]


class TestClarifyToolMultiMode:
    """Multi-select mode (mode='multi') splits the joined response."""

    def test_multi_mode_splits_unit_separator(self):
        """When the callback returns choices joined by U+001F, the tool
        should expose them as `user_responses: list[str]` and also
        populate the legacy `user_response` field with a comma-joined
        form so callers that haven't been updated still get something
        readable."""
        def mock_callback(question, choices, *, mode="single"):
            assert mode == "multi"
            return "red\x1fblue"

        result = json.loads(clarify_tool(
            "Pick all that apply",
            choices=["red", "green", "blue"],
            mode="multi",
            callback=mock_callback,
        ))
        assert result["user_responses"] == ["red", "blue"]
        assert result["user_response"] == "red, blue"

    def test_multi_mode_empty_response_yields_empty_list(self):
        """User clicked Submit with no selections."""
        def mock_callback(question, choices, *, mode="single"):
            assert mode == "multi"
            return ""

        result = json.loads(clarify_tool(
            "Pick all that apply",
            choices=["a", "b"],
            mode="multi",
            callback=mock_callback,
        ))
        assert result["user_responses"] == []
        assert result["user_response"] == ""

    def test_multi_mode_falls_back_to_legacy_callback_signature(self):
        """Older callbacks without `mode` kwarg still work — the tool
        catches TypeError and retries the call positionally."""
        captured = {}

        def legacy_callback(question, choices):
            captured["question"] = question
            captured["choices"] = choices
            return "x\x1fy"

        result = json.loads(clarify_tool(
            "legacy mixer",
            choices=["x", "y", "z"],
            mode="multi",
            callback=legacy_callback,
        ))
        assert captured["choices"] == ["x", "y", "z"]
        assert result["user_responses"] == ["x", "y"]

    def test_multi_mode_without_choices_falls_back_to_single(self):
        """Open-ended multi makes no sense — the tool quietly downgrades
        to single mode so the response shape stays single."""
        def mock_callback(question, choices, *, mode="single"):
            assert mode == "single"  # downgraded
            assert choices is None
            return "free-form text"

        result = json.loads(clarify_tool(
            "open ended",
            choices=None,
            mode="multi",
            callback=mock_callback,
        ))
        # user_responses key is multi-only — should not appear in single
        assert "user_responses" not in result
        assert result["user_response"] == "free-form text"

    def test_unknown_mode_normalized_to_single(self):
        """Tolerant: unknown modes do NOT fail the call, they normalize
        to single (matches the rest of the tool's tolerant validation)."""
        def mock_callback(question, choices, *, mode="single"):
            assert mode == "single"
            return "picked"

        result = json.loads(clarify_tool(
            "q",
            choices=["picked"],
            mode="not_a_mode",
            callback=mock_callback,
        ))
        assert "user_responses" not in result
        assert result["user_response"] == "picked"
