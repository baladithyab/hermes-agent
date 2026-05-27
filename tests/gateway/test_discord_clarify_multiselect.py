"""Tests for the Discord ClarifyMultiChoiceView (Select-based multi-select).

Mirrors test_discord_clarify_buttons.py shape but covers the
multi-select path added to support `clarify(mode='multi')`. The View
should:

  · render ONE ``discord.ui.Select`` with min_values=0, max_values=N
    plus an ``✏️ Other`` escape-hatch button
  · resolve when the dropdown closes — interaction.data['values']
    carries the list of selected option values (stored as str(index))
  · join the selected choices with U+001F and resolve the gateway
    clarify entry
  · obey the same auth + already-resolved gating as the single-pick
    view
  · support the Other escape-hatch (mark_awaiting_text + disable
    components)
  · disable all components after submit/Other

Note on rendering: discord.py's native multi-select is a dropdown
(``Select``) where checking multiple options + closing the dropdown
fires the callback with ``interaction.data['values']``. There is no
separate Submit button — the close action IS the submit. This matches
the ``ModelPickerView`` precedent in this same adapter and is the
canonical Discord pattern.
"""

import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

# Repo root importable
_repo = str(Path(__file__).resolve().parents[2])
if _repo not in sys.path:
    sys.path.insert(0, _repo)

# Triggers the shared discord mock from tests/gateway/conftest.py before
# importing the production module.
from plugins.platforms.discord.adapter import (  # noqa: E402
    ClarifyMultiChoiceView,
)


def _clear_clarify_state():
    from tools import clarify_gateway as cm
    with cm._lock:
        cm._entries.clear()
        cm._session_index.clear()
        cm._notify_cbs.clear()


def _make_interaction(*, user_id="42", display_name="Tester", roles=None,
                      include_message=True, selected_values=None):
    """Build a mock discord.Interaction with response.* coroutine-callable.

    ``selected_values`` populates ``interaction.data['values']`` for
    Select-resolution tests — the ClarifyMultiChoiceView reads from
    there to determine which options the user picked.
    """
    user = SimpleNamespace(
        id=user_id,
        display_name=display_name,
        roles=[SimpleNamespace(id=r) for r in (roles or [])],
    )
    response = SimpleNamespace(
        edit_message=AsyncMock(),
        send_message=AsyncMock(),
        defer=AsyncMock(),
    )
    if include_message:
        embed = MagicMock()
        embed.color = None
        embed.set_footer = MagicMock()
        message = SimpleNamespace(embeds=[embed])
    else:
        message = None
    data = {"values": list(selected_values)} if selected_values is not None else {}
    return SimpleNamespace(user=user, response=response, message=message, data=data)


# ===========================================================================
# Construction
# ===========================================================================

class TestClarifyMultiChoiceViewConstruction:
    """View renders one Select dropdown + one Other button."""

    def test_renders_one_select_plus_other(self):
        view = ClarifyMultiChoiceView(
            choices=["apple", "banana", "cherry"],
            clarify_id="m1",
            allowed_user_ids={"42"},
        )
        # Select + Other = 2 components total
        assert len(view.children) == 2

        select, other_btn = view.children[0], view.children[1]
        # Select: stable custom_id, multi-select capable
        assert select.custom_id == "clarify_multi:m1:select"
        assert select.min_values == 0
        assert select.max_values == 3  # all three choices selectable
        # SelectOption labels are 1-indexed and round-trip-friendly
        opt_labels = [o.label for o in select.options]
        opt_values = [o.value for o in select.options]
        assert opt_labels == ["1. apple", "2. banana", "3. cherry"]
        assert opt_values == ["0", "1", "2"]
        # Other button has its own custom_id namespace
        assert "Other" in other_btn.label
        assert other_btn.custom_id == "clarify_multi:m1:other"

    def test_caps_at_25_options(self):
        choices = [f"choice-{i}" for i in range(50)]
        view = ClarifyMultiChoiceView(
            choices=choices,
            clarify_id="m_big",
            allowed_user_ids=set(),
        )
        # Still just 2 components (Select + Other)
        assert len(view.children) == 2
        # The Select's options are capped at Discord's 25-option limit
        select = view.children[0]
        assert len(select.options) == 25
        assert select.max_values == 25

    def test_truncates_long_choice_label(self):
        long_choice = "x" * 200
        view = ClarifyMultiChoiceView(
            choices=[long_choice],
            clarify_id="m_long",
            allowed_user_ids=set(),
        )
        select = view.children[0]
        first_label = select.options[0].label
        assert first_label.startswith("1. ")
        assert first_label.endswith("...")
        # SelectOption label cap is 100 chars
        assert len(first_label) <= 100


# ===========================================================================
# Select callback → resolve_gateway_clarify with joined string
# ===========================================================================

class TestClarifyMultiChoiceResolve:
    """The Select's callback finalizes with the canonical-choices payload."""

    def setup_method(self):
        _clear_clarify_state()

    @pytest.mark.asyncio
    async def test_multi_pick_joins_selected_with_unit_separator(self):
        from tools import clarify_gateway as cm
        cm.register("m_s1", "sk-S", "Pick", ["red", "green", "blue"], multi=True)

        view = ClarifyMultiChoiceView(
            choices=["red", "green", "blue"],
            clarify_id="m_s1",
            allowed_user_ids={"42"},
        )
        # User picked indices 0 and 2 (red and blue) and closed the dropdown
        interaction = _make_interaction(user_id="42", selected_values=["0", "2"])
        await view._on_select_resolve(interaction)

        with cm._lock:
            entry = cm._entries.get("m_s1")
        assert entry is not None
        assert entry.event.is_set()
        # Joined with U+001F (ASCII Unit Separator)
        assert entry.response == "red\x1fblue"
        # Components disabled, view resolved
        assert view.resolved is True
        assert all(c.disabled for c in view.children)
        # Embed-edit was called
        interaction.response.edit_message.assert_called_once()

    @pytest.mark.asyncio
    async def test_no_selection_resolves_empty_string(self):
        """User opened the dropdown and closed it without picking
        anything — resolves with empty string so agent-side splits to []."""
        from tools import clarify_gateway as cm
        cm.register("m_s2", "sk-S", "Pick", ["x", "y"], multi=True)

        view = ClarifyMultiChoiceView(
            choices=["x", "y"],
            clarify_id="m_s2",
            allowed_user_ids=set(),
        )
        interaction = _make_interaction(selected_values=[])
        await view._on_select_resolve(interaction)

        with cm._lock:
            entry = cm._entries.get("m_s2")
        assert entry is not None
        assert entry.event.is_set()
        assert entry.response == ""

    @pytest.mark.asyncio
    async def test_resolve_uses_canonical_choices_from_entry(self):
        """If the View was constructed with stale labels but the gateway
        entry carries the canonical strings, those are what get resolved."""
        from tools import clarify_gateway as cm
        cm.register("m_s3", "sk-S", "Pick",
                    ["CANONICAL-A", "CANONICAL-B"], multi=True)

        view = ClarifyMultiChoiceView(
            choices=["stale-a", "stale-b"],
            clarify_id="m_s3",
            allowed_user_ids=set(),
        )
        interaction = _make_interaction(selected_values=["0"])
        await view._on_select_resolve(interaction)

        with cm._lock:
            entry = cm._entries.get("m_s3")
        assert entry is not None
        # Canonical, not "stale-a"
        assert entry.response == "CANONICAL-A"

    @pytest.mark.asyncio
    async def test_resolve_unauthorized_rejected(self):
        from tools import clarify_gateway as cm
        cm.register("m_s4", "sk-S", "Pick", ["x"], multi=True)
        view = ClarifyMultiChoiceView(
            choices=["x"],
            clarify_id="m_s4",
            allowed_user_ids={"99999"},  # not 42
        )
        interaction = _make_interaction(user_id="42", selected_values=["0"])
        await view._on_select_resolve(interaction)

        with cm._lock:
            entry = cm._entries.get("m_s4")
        assert entry is not None
        assert not entry.event.is_set()
        assert entry.response is None
        interaction.response.send_message.assert_called_once()
        assert interaction.response.send_message.call_args.kwargs.get("ephemeral") is True

    @pytest.mark.asyncio
    async def test_already_resolved_sends_ephemeral_reply(self):
        view = ClarifyMultiChoiceView(
            choices=["a", "b"],
            clarify_id="m_s5",
            allowed_user_ids=set(),
        )
        view.resolved = True
        interaction = _make_interaction(selected_values=["0"])
        await view._on_select_resolve(interaction)

        interaction.response.send_message.assert_called_once()
        assert interaction.response.send_message.call_args.kwargs.get("ephemeral") is True
        interaction.response.edit_message.assert_not_called()

    @pytest.mark.asyncio
    async def test_resolve_handles_malformed_values_gracefully(self):
        """If interaction.data is missing/malformed, treat as zero-selection
        rather than raising. Defensive against platform-mock variance."""
        from tools import clarify_gateway as cm
        cm.register("m_s6", "sk-S", "Pick", ["a", "b"], multi=True)
        view = ClarifyMultiChoiceView(
            choices=["a", "b"],
            clarify_id="m_s6",
            allowed_user_ids=set(),
        )
        # data attr present but values key missing
        interaction = _make_interaction()
        interaction.data = {}  # explicitly empty
        await view._on_select_resolve(interaction)

        with cm._lock:
            entry = cm._entries.get("m_s6")
        assert entry is not None
        assert entry.event.is_set()
        assert entry.response == ""


# ===========================================================================
# Other → mark_awaiting_text (escape-hatch)
# ===========================================================================

class TestClarifyMultiChoiceOther:
    def setup_method(self):
        _clear_clarify_state()

    @pytest.mark.asyncio
    async def test_other_marks_awaiting_text_and_disables(self):
        from tools import clarify_gateway as cm
        cm.register("m_o1", "sk-O", "Pick", ["a", "b"], multi=True)

        view = ClarifyMultiChoiceView(
            choices=["a", "b"],
            clarify_id="m_o1",
            allowed_user_ids=set(),
        )
        interaction = _make_interaction()
        await view._on_other(interaction)

        with cm._lock:
            entry = cm._entries.get("m_o1")
        assert entry is not None
        assert entry.awaiting_text is True
        # Not resolved yet — text-intercept does that
        assert not entry.event.is_set()
        assert view.resolved is True
        assert all(c.disabled for c in view.children)
