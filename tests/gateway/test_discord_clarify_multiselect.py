"""Tests for the Discord ClarifyMultiChoiceView (multi-select clarify).

Mirrors test_discord_clarify_buttons.py shape but covers the multi-select
path added to support `clarify(mode='multi')`. The View should:

  · render N toggle buttons + Submit + Other (cap N at 23 per Discord limit)
  · toggle selection state when a numeric button is clicked (no resolve)
  · finalize on Submit, joining selected canonical choices with the
    \\x1f separator and resolving the gateway clarify entry
  · obey the same auth + already-resolved gating as the single-pick view
  · support the Other escape-hatch (mark_awaiting_text + disable buttons)
  · disable all buttons after Submit/Other
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
                      include_message=True):
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
    return SimpleNamespace(user=user, response=response, message=message)


# ===========================================================================
# Construction
# ===========================================================================

class TestClarifyMultiChoiceViewConstruction:
    """View renders one toggle per choice + Submit + Other."""

    def test_renders_n_toggles_plus_submit_plus_other(self):
        view = ClarifyMultiChoiceView(
            choices=["apple", "banana", "cherry"],
            clarify_id="m1",
            allowed_user_ids={"42"},
        )
        # 3 numeric + Submit + Other
        assert len(view.children) == 5
        labels = [b.label for b in view.children]
        assert labels[0].startswith("☐ 1. apple")
        assert labels[1].startswith("☐ 2. banana")
        assert labels[2].startswith("☐ 3. cherry")
        assert "Submit" in labels[3]
        assert "Other" in labels[4]
        # custom_ids
        ids = [b.custom_id for b in view.children]
        assert ids[0] == "clarify_multi:m1:0"
        assert ids[1] == "clarify_multi:m1:1"
        assert ids[2] == "clarify_multi:m1:2"
        assert ids[3] == "clarify_multi:m1:submit"
        assert ids[4] == "clarify_multi:m1:other"
        # Initial selection state — all unselected
        assert view._selected == [False, False, False]

    def test_caps_at_23_choices_plus_submit_plus_other(self):
        choices = [f"choice-{i}" for i in range(50)]
        view = ClarifyMultiChoiceView(
            choices=choices,
            clarify_id="m_big",
            allowed_user_ids=set(),
        )
        # Discord limit is 25 components; we cap choices at 23 + Submit + Other = 25
        assert len(view.children) == 25
        # Last two are Submit + Other
        assert "Submit" in view.children[-2].label
        assert "Other" in view.children[-1].label

    def test_truncates_long_choice_label(self):
        long_choice = "x" * 200
        view = ClarifyMultiChoiceView(
            choices=[long_choice],
            clarify_id="m_long",
            allowed_user_ids=set(),
        )
        first_label = view.children[0].label
        assert first_label.startswith("☐ 1. ")
        assert first_label.endswith("...")
        # Final label total <= 80 (Discord cap on button labels)
        assert len(first_label) <= 80


# ===========================================================================
# Toggle behavior
# ===========================================================================

class TestClarifyMultiChoiceToggle:
    """Clicking a numeric button toggles selection without resolving."""

    def setup_method(self):
        _clear_clarify_state()

    @pytest.mark.asyncio
    async def test_toggle_flips_state_and_does_not_resolve(self):
        from tools import clarify_gateway as cm
        cm.register("m_t1", "sk-T", "Pick many", ["red", "green", "blue"], multi=True)

        view = ClarifyMultiChoiceView(
            choices=["red", "green", "blue"],
            clarify_id="m_t1",
            allowed_user_ids={"42"},
        )

        interaction = _make_interaction(user_id="42")
        await view._toggle(interaction, index=1)

        assert view._selected == [False, True, False]
        # Button label shows selected mark
        assert view.children[1].label.startswith("☑ 2. green")
        # Resolve was NOT called
        with cm._lock:
            entry = cm._entries.get("m_t1")
        assert entry is not None
        assert not entry.event.is_set()
        assert entry.response is None
        # View not finalized
        assert view.resolved is False
        assert all(not b.disabled for b in view.children)

    @pytest.mark.asyncio
    async def test_toggle_again_unselects(self):
        from tools import clarify_gateway as cm
        cm.register("m_t2", "sk-T", "Pick", ["a", "b"], multi=True)

        view = ClarifyMultiChoiceView(
            choices=["a", "b"],
            clarify_id="m_t2",
            allowed_user_ids={"42"},
        )
        interaction = _make_interaction(user_id="42")

        await view._toggle(interaction, index=0)
        assert view._selected == [True, False]
        await view._toggle(interaction, index=0)
        assert view._selected == [False, False]
        # Label back to unchecked
        assert view.children[0].label.startswith("☐ 1. a")

    @pytest.mark.asyncio
    async def test_unauthorized_toggle_rejected(self):
        from tools import clarify_gateway as cm
        cm.register("m_t3", "sk-T", "Pick", ["x"], multi=True)
        view = ClarifyMultiChoiceView(
            choices=["x"],
            clarify_id="m_t3",
            allowed_user_ids={"99999"},  # not 42
        )
        interaction = _make_interaction(user_id="42")
        await view._toggle(interaction, index=0)

        # Selection unchanged, ephemeral message sent
        assert view._selected == [False]
        interaction.response.send_message.assert_called_once()
        assert interaction.response.send_message.call_args.kwargs.get("ephemeral") is True


# ===========================================================================
# Submit → resolve_gateway_clarify with joined string
# ===========================================================================

class TestClarifyMultiChoiceSubmit:
    """Submit finalizes with the canonical-choices payload."""

    def setup_method(self):
        _clear_clarify_state()

    @pytest.mark.asyncio
    async def test_submit_joins_selected_with_unit_separator(self):
        from tools import clarify_gateway as cm
        cm.register("m_s1", "sk-S", "Pick", ["red", "green", "blue"], multi=True)

        view = ClarifyMultiChoiceView(
            choices=["red", "green", "blue"],
            clarify_id="m_s1",
            allowed_user_ids={"42"},
        )
        interaction = _make_interaction(user_id="42")

        # Pick red and blue (skip green)
        await view._toggle(interaction, index=0)
        await view._toggle(interaction, index=2)
        await view._on_submit(interaction)

        with cm._lock:
            entry = cm._entries.get("m_s1")
        assert entry is not None
        assert entry.event.is_set()
        # Joined with U+001F (ASCII Unit Separator)
        assert entry.response == "red\x1fblue"
        # All buttons disabled, view resolved
        assert view.resolved is True
        assert all(b.disabled for b in view.children)
        # Final submit edit_message was called with the embed kwarg —
        # toggle calls earlier in this test pass view-only, so the call
        # with embed=... is the submit one.
        edit_calls = interaction.response.edit_message.call_args_list
        embed_calls = [c for c in edit_calls if "embed" in c.kwargs]
        assert len(embed_calls) == 1

    @pytest.mark.asyncio
    async def test_submit_with_no_selection_resolves_empty_string(self):
        """Empty-selection submit yields response='' so the agent-side tool
        can split into an empty list — matches the documented contract."""
        from tools import clarify_gateway as cm
        cm.register("m_s2", "sk-S", "Pick", ["x", "y"], multi=True)

        view = ClarifyMultiChoiceView(
            choices=["x", "y"],
            clarify_id="m_s2",
            allowed_user_ids=set(),
        )
        interaction = _make_interaction()
        await view._on_submit(interaction)

        with cm._lock:
            entry = cm._entries.get("m_s2")
        assert entry is not None
        assert entry.event.is_set()
        # Empty selection → empty string payload
        assert entry.response == ""

    @pytest.mark.asyncio
    async def test_submit_uses_canonical_choices_from_entry(self):
        """If the View was constructed with stale labels but the gateway
        entry carries the canonical strings, those are what get resolved."""
        from tools import clarify_gateway as cm
        cm.register("m_s3", "sk-S", "Pick", ["CANONICAL-A", "CANONICAL-B"], multi=True)

        view = ClarifyMultiChoiceView(
            choices=["stale-a", "stale-b"],
            clarify_id="m_s3",
            allowed_user_ids=set(),
        )
        interaction = _make_interaction()
        await view._toggle(interaction, index=0)
        await view._on_submit(interaction)

        with cm._lock:
            entry = cm._entries.get("m_s3")
        assert entry is not None
        # Canonical, not "stale-a"
        assert entry.response == "CANONICAL-A"

    @pytest.mark.asyncio
    async def test_submit_unauthorized_rejected(self):
        from tools import clarify_gateway as cm
        cm.register("m_s4", "sk-S", "Pick", ["x"], multi=True)
        view = ClarifyMultiChoiceView(
            choices=["x"],
            clarify_id="m_s4",
            allowed_user_ids={"99999"},
        )
        # Bypass auth check on toggle by manually flipping state — we want
        # to verify Submit's auth gate even when state looks "selected".
        view._selected = [True]
        interaction = _make_interaction(user_id="42")
        await view._on_submit(interaction)

        with cm._lock:
            entry = cm._entries.get("m_s4")
        assert entry is not None
        assert not entry.event.is_set()
        assert entry.response is None
        interaction.response.send_message.assert_called_once()
        assert interaction.response.send_message.call_args.kwargs.get("ephemeral") is True

    @pytest.mark.asyncio
    async def test_already_resolved_submit_sends_ephemeral_reply(self):
        view = ClarifyMultiChoiceView(
            choices=["a", "b"],
            clarify_id="m_s5",
            allowed_user_ids=set(),
        )
        view.resolved = True
        interaction = _make_interaction()
        await view._on_submit(interaction)

        interaction.response.send_message.assert_called_once()
        assert interaction.response.send_message.call_args.kwargs.get("ephemeral") is True
        interaction.response.edit_message.assert_not_called()


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
        assert all(b.disabled for b in view.children)
