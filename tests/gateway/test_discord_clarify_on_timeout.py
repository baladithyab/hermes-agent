"""Regression tests for clarify View on_timeout — both single and multi-select.

Background
----------
Both ClarifyChoiceView (single-pick) and ClarifyMultiChoiceView (multi)
have an on_timeout handler that fires when the user doesn't interact
within the View's configured timeout. Pre-fix the handler did the bare
minimum (set self.resolved=True, disable children) and left the gateway
clarify entry unresolved, causing the agent thread to keep blocking on
its own ``wait_for_response`` until that timeout fired.

Worse: a user clicking the now-disabled buttons saw nothing happen — the
embed wasn't updated to indicate timeout, so the UI lied about state.

This test module locks in the fixed behavior:

  · on_timeout MUST call ``resolve_gateway_clarify(id, "")`` so the agent
    unblocks immediately on the empty-string sentinel
  · on_timeout MUST update the embed (when the message handle is
    available via ``view.message``) so the user sees the timeout
  · on_timeout MUST be a no-op if the view was already resolved by a
    button/Select/Other interaction (idempotent late-fire is safe)
  · The View timeout is sourced from ``agent.clarify_timeout`` config
    via ``_clarify_view_timeout()``, not hardcoded 300s

Plus one integration check on send_clarify itself: it must stash the
sent message on the View as ``view.message`` so on_timeout has a handle
to edit later (discord.py only auto-tracks messages for views sent via
``interaction.followup.send`` — channel.send views need manual stash).
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# Repo root importable
_repo = str(Path(__file__).resolve().parents[2])
if _repo not in sys.path:
    sys.path.insert(0, _repo)

# Triggers the shared discord mock from tests/gateway/conftest.py before
# importing the production module.
from plugins.platforms.discord.adapter import (  # noqa: E402
    ClarifyChoiceView,
    ClarifyMultiChoiceView,
    DiscordAdapter,
    _clarify_view_timeout,
)
from gateway.config import PlatformConfig  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _clear_clarify_state():
    from tools import clarify_gateway as cm
    with cm._lock:
        cm._entries.clear()
        cm._session_index.clear()
        cm._notify_cbs.clear()


def _make_adapter(*, allowed_users=None, allowed_roles=None):
    config = PlatformConfig(enabled=True, token="test-token", extra={})
    adapter = DiscordAdapter(config)
    adapter._client = MagicMock()
    adapter._allowed_user_ids = set(allowed_users or [])
    adapter._allowed_role_ids = set(allowed_roles or [])
    return adapter


def _make_message_with_embed():
    """Return a SimpleNamespace-shaped message with an editable embed."""
    embed = MagicMock()
    embed.color = None
    embed.set_footer = MagicMock()
    msg = SimpleNamespace(
        id=999,
        embeds=[embed],
        edit=AsyncMock(),
    )
    return msg


# ===========================================================================
# _clarify_view_timeout — config-driven, with safety margin
# ===========================================================================

class TestClarifyViewTimeout:
    """The View timeout must follow ``agent.clarify_timeout`` config."""

    def test_default_is_agent_timeout_plus_margin(self):
        # get_clarify_timeout default is 600. We add a 10s margin so the
        # agent-side wait expires first under normal conditions.
        with patch(
            "plugins.platforms.discord.adapter.get_clarify_timeout",
            create=True,
        ):
            # Direct override path — patch through clarify_gateway itself
            # since the function imports lazily.
            with patch("tools.clarify_gateway.get_clarify_timeout", return_value=600):
                assert _clarify_view_timeout() == 610.0

    def test_honors_custom_clarify_timeout(self):
        with patch("tools.clarify_gateway.get_clarify_timeout", return_value=120):
            assert _clarify_view_timeout() == 130.0

    def test_falls_back_when_import_fails(self):
        # If the import raises (test isolation, broken state), the helper
        # should not blow up — return a sane default.
        with patch(
            "tools.clarify_gateway.get_clarify_timeout",
            side_effect=RuntimeError("config unreachable"),
        ):
            assert _clarify_view_timeout() == 610.0  # 600 fallback + 10s margin


# ===========================================================================
# ClarifyChoiceView (single-pick) on_timeout
# ===========================================================================

class TestClarifyChoiceViewOnTimeout:
    def setup_method(self):
        _clear_clarify_state()

    @pytest.mark.asyncio
    async def test_on_timeout_resolves_gateway_with_empty_sentinel(self):
        """Agent thread must unblock immediately when the View times out."""
        from tools import clarify_gateway as cm
        cm.register("cid_t1", "sk-T1", "Pick one", ["a", "b"], multi=False)

        view = ClarifyChoiceView(
            choices=["a", "b"], clarify_id="cid_t1", allowed_user_ids=set(),
        )
        view.message = _make_message_with_embed()

        await view.on_timeout()

        # Empty-string sentinel — same one clarify_gateway.clear_session uses.
        with cm._lock:
            entry = cm._entries.get("cid_t1")
        # After resolve_gateway_clarify the entry is still present (the agent
        # thread's wait_for_response is what pops it on return). But the
        # event must be set with an empty response.
        assert entry is not None
        assert entry.event.is_set()
        assert entry.response == ""

    @pytest.mark.asyncio
    async def test_on_timeout_disables_all_components(self):
        view = ClarifyChoiceView(
            choices=["x", "y"], clarify_id="cid_t2", allowed_user_ids=set(),
        )
        view.message = _make_message_with_embed()
        # No registry entry — exercising the resolve-failure-tolerance path.

        await view.on_timeout()

        assert view.resolved is True
        assert all(c.disabled for c in view.children)

    @pytest.mark.asyncio
    async def test_on_timeout_edits_embed_to_indicate_timeout(self):
        view = ClarifyChoiceView(
            choices=["a"], clarify_id="cid_t3", allowed_user_ids=set(),
        )
        msg = _make_message_with_embed()
        view.message = msg

        await view.on_timeout()

        # Message edit was called with the modified embed and the disabled
        # view (so disabled state propagates to the rendered UI).
        msg.edit.assert_awaited_once()
        kwargs = msg.edit.await_args.kwargs
        assert "embed" in kwargs
        assert kwargs["view"] is view
        # Footer was updated to indicate timeout.
        msg.embeds[0].set_footer.assert_called_once()
        footer_text = msg.embeds[0].set_footer.call_args.kwargs.get(
            "text"
        ) or (msg.embeds[0].set_footer.call_args.args[0] if msg.embeds[0].set_footer.call_args.args else "")
        assert "imed out" in footer_text  # "Timed out"

    @pytest.mark.asyncio
    async def test_on_timeout_idempotent_when_already_resolved(self):
        """If a button click already resolved the view, on_timeout is a no-op."""
        from tools import clarify_gateway as cm
        cm.register("cid_t4", "sk-T4", "Pick", ["a"], multi=False)
        # Simulate prior resolution.
        cm.resolve_gateway_clarify("cid_t4", "a")

        view = ClarifyChoiceView(
            choices=["a"], clarify_id="cid_t4", allowed_user_ids=set(),
        )
        view.resolved = True  # button-click already flipped this
        msg = _make_message_with_embed()
        view.message = msg

        await view.on_timeout()

        # Idempotent: must not edit the message again or re-resolve.
        msg.edit.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_on_timeout_survives_missing_message(self):
        """on_timeout must not raise when view.message is None."""
        view = ClarifyChoiceView(
            choices=["a"], clarify_id="cid_t5", allowed_user_ids=set(),
        )
        # view.message left as None — discord.py default; happens when
        # send_clarify failed to stash or the view was constructed for tests.

        # Should complete cleanly, no AttributeError, no edit call.
        await view.on_timeout()

        assert view.resolved is True
        assert all(c.disabled for c in view.children)

    @pytest.mark.asyncio
    async def test_on_timeout_survives_message_edit_exception(self):
        """If channel.permissions changed or msg was deleted, edit may raise."""
        from tools import clarify_gateway as cm
        cm.register("cid_t6", "sk-T6", "Pick", ["a"], multi=False)

        view = ClarifyChoiceView(
            choices=["a"], clarify_id="cid_t6", allowed_user_ids=set(),
        )
        msg = _make_message_with_embed()
        msg.edit = AsyncMock(side_effect=RuntimeError("Forbidden"))
        view.message = msg

        # Should swallow the edit failure but still resolve the gateway.
        await view.on_timeout()

        with cm._lock:
            entry = cm._entries.get("cid_t6")
        assert entry is not None
        assert entry.event.is_set()
        assert entry.response == ""


# ===========================================================================
# ClarifyMultiChoiceView on_timeout (mirrors single-pick contract)
# ===========================================================================

class TestClarifyMultiChoiceViewOnTimeout:
    def setup_method(self):
        _clear_clarify_state()

    @pytest.mark.asyncio
    async def test_multi_on_timeout_resolves_gateway_with_empty_sentinel(self):
        from tools import clarify_gateway as cm
        cm.register("cid_mt1", "sk-MT1", "Pick any", ["a", "b"], multi=True)

        view = ClarifyMultiChoiceView(
            choices=["a", "b"], clarify_id="cid_mt1", allowed_user_ids=set(),
        )
        view.message = _make_message_with_embed()

        await view.on_timeout()

        with cm._lock:
            entry = cm._entries.get("cid_mt1")
        assert entry is not None
        assert entry.event.is_set()
        # Empty string → clarify_tool short-circuits to user_responses=[].
        assert entry.response == ""

    @pytest.mark.asyncio
    async def test_multi_on_timeout_disables_all_components(self):
        view = ClarifyMultiChoiceView(
            choices=["x", "y", "z"], clarify_id="cid_mt2", allowed_user_ids=set(),
        )
        view.message = _make_message_with_embed()

        await view.on_timeout()

        assert view.resolved is True
        assert all(c.disabled for c in view.children)

    @pytest.mark.asyncio
    async def test_multi_on_timeout_edits_embed(self):
        view = ClarifyMultiChoiceView(
            choices=["a"], clarify_id="cid_mt3", allowed_user_ids=set(),
        )
        msg = _make_message_with_embed()
        view.message = msg

        await view.on_timeout()

        msg.edit.assert_awaited_once()
        msg.embeds[0].set_footer.assert_called_once()

    @pytest.mark.asyncio
    async def test_multi_on_timeout_idempotent_when_resolved(self):
        from tools import clarify_gateway as cm
        cm.register("cid_mt4", "sk-MT4", "Pick", ["a"], multi=True)
        cm.resolve_gateway_clarify("cid_mt4", "a")

        view = ClarifyMultiChoiceView(
            choices=["a"], clarify_id="cid_mt4", allowed_user_ids=set(),
        )
        view.resolved = True
        msg = _make_message_with_embed()
        view.message = msg

        await view.on_timeout()
        msg.edit.assert_not_awaited()


# ===========================================================================
# send_clarify must stash the sent message on the view (so on_timeout works)
# ===========================================================================

class TestSendClarifyStashesMessage:
    """Without this, on_timeout has no Message handle to edit."""

    def setup_method(self):
        _clear_clarify_state()

    @pytest.mark.asyncio
    async def test_single_pick_send_clarify_stashes_message_on_view(self):
        adapter = _make_adapter()

        # Mock the channel + sent message + register the gateway entry.
        sent_msg = SimpleNamespace(id=12345)
        channel = SimpleNamespace(send=AsyncMock(return_value=sent_msg))
        adapter._client.get_channel = MagicMock(return_value=channel)

        from tools import clarify_gateway as cm
        cm.register("cidS1", "sk-S1", "Pick", ["one", "two"], multi=False)

        result = await adapter.send_clarify(
            chat_id="123", question="Pick", choices=["one", "two"],
            clarify_id="cidS1", session_key="sk-S1", multi=False,
        )

        assert result.success is True
        # The View constructed inside send_clarify must have view.message
        # set to the sent message, so on_timeout can edit it later.
        # We verify by inspecting the view passed to channel.send().
        view = channel.send.await_args.kwargs["view"]
        assert view is not None
        assert view.message is sent_msg

    @pytest.mark.asyncio
    async def test_multi_send_clarify_stashes_message_on_view(self):
        adapter = _make_adapter()
        sent_msg = SimpleNamespace(id=67890)
        channel = SimpleNamespace(send=AsyncMock(return_value=sent_msg))
        adapter._client.get_channel = MagicMock(return_value=channel)

        from tools import clarify_gateway as cm
        cm.register("cidS2", "sk-S2", "Pick any", ["a", "b"], multi=True)

        result = await adapter.send_clarify(
            chat_id="123", question="Pick any", choices=["a", "b"],
            clarify_id="cidS2", session_key="sk-S2", multi=True,
        )

        assert result.success is True
        view = channel.send.await_args.kwargs["view"]
        assert view is not None
        assert view.message is sent_msg


# ===========================================================================
# Integration: View timeout passes through to discord.py super().__init__
# ===========================================================================

class TestViewTimeoutIsConfigDriven:
    """Both views must pull their timeout from ``_clarify_view_timeout()``."""

    def test_single_pick_view_timeout_matches_helper(self):
        with patch("tools.clarify_gateway.get_clarify_timeout", return_value=120):
            view = ClarifyChoiceView(
                choices=["a"], clarify_id="cidV1", allowed_user_ids=set(),
            )
            assert view.timeout == 130.0

    def test_multi_view_timeout_matches_helper(self):
        with patch("tools.clarify_gateway.get_clarify_timeout", return_value=120):
            view = ClarifyMultiChoiceView(
                choices=["a"], clarify_id="cidV2", allowed_user_ids=set(),
            )
            assert view.timeout == 130.0
