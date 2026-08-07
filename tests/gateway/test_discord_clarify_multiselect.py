"""Discord multi-select clarify rendering and callback behavior."""

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from gateway.config import PlatformConfig
from plugins.platforms.discord.adapter import (
    ClarifyChoiceView,
    ClarifyMultiChoiceView,
    DiscordAdapter,
)


def _make_adapter(*, allowed_users=None, allowed_roles=None):
    config = PlatformConfig(enabled=True, token="test-token", extra={})
    adapter = DiscordAdapter(config)
    adapter._client = MagicMock()
    adapter._allowed_user_ids = set(allowed_users or [])
    adapter._allowed_role_ids = set(allowed_roles or [])
    return adapter


def _clear_clarify_state():
    from tools import clarify_gateway as cm

    with cm._lock:
        cm._entries.clear()
        cm._session_index.clear()
        cm._notify_cbs.clear()


def _make_interaction(*, values=None, user_id="42", display_name="Tester", roles=None):
    user = SimpleNamespace(
        id=user_id,
        display_name=display_name,
        roles=[SimpleNamespace(id=r) for r in (roles or [])],
    )
    embed = MagicMock()
    embed.color = None
    embed.set_footer = MagicMock()
    return SimpleNamespace(
        user=user,
        data={"values": values or []},
        message=SimpleNamespace(embeds=[embed]),
        response=SimpleNamespace(
            edit_message=AsyncMock(),
            send_message=AsyncMock(),
            defer=AsyncMock(),
        ),
    )


class TestClarifyMultiChoiceView:
    def setup_method(self):
        _clear_clarify_state()

    def test_constructor_caps_choices_at_discord_limit(self):
        choices = [f"choice-{i}" for i in range(30)]
        view = ClarifyMultiChoiceView(
            choices=choices,
            clarify_id="cid-cap",
            allowed_user_ids={"42"},
        )

        select, other = view.children
        assert len(view.choices) == 25
        assert len(select.options) == 25
        assert select.min_values == 0
        assert select.max_values == 25
        assert "Other" in other.label

    @pytest.mark.asyncio
    async def test_select_callback_resolves_json_array(self):
        from tools import clarify_gateway as cm

        cm.register(
            "cid-resolve",
            "sk-resolve",
            "Pick any",
            ["red", "green", "blue"],
            multi_select=True,
        )
        view = ClarifyMultiChoiceView(
            choices=["red", "green", "blue"],
            clarify_id="cid-resolve",
            allowed_user_ids={"42"},
        )

        interaction = _make_interaction(values=["0", "2"])
        await view._on_select(interaction)

        with cm._lock:
            entry = cm._entries["cid-resolve"]
        assert json.loads(entry.response) == ["red", "blue"]
        assert entry.event.is_set()
        interaction.response.edit_message.assert_called_once()

    @pytest.mark.asyncio
    async def test_other_button_marks_awaiting_text(self):
        from tools import clarify_gateway as cm

        cm.register(
            "cid-other",
            "sk-other",
            "Pick any",
            ["red", "blue"],
            multi_select=True,
        )
        view = ClarifyMultiChoiceView(
            choices=["red", "blue"],
            clarify_id="cid-other",
            allowed_user_ids={"42"},
        )

        await view._on_other(_make_interaction())

        with cm._lock:
            assert cm._entries["cid-other"].awaiting_text is True

    @pytest.mark.asyncio
    async def test_auth_gating_rejects_unauthorized_user(self):
        from tools import clarify_gateway as cm

        cm.register(
            "cid-auth",
            "sk-auth",
            "Pick any",
            ["red", "blue"],
            multi_select=True,
        )
        view = ClarifyMultiChoiceView(
            choices=["red", "blue"],
            clarify_id="cid-auth",
            allowed_user_ids={"999"},
        )
        interaction = _make_interaction(values=["0"], user_id="42")

        await view._on_select(interaction)

        interaction.response.send_message.assert_called_once()
        interaction.response.edit_message.assert_not_called()
        with cm._lock:
            entry = cm._entries["cid-auth"]
        assert entry.response is None
        assert not entry.event.is_set()


class TestDiscordSendClarifyMultiSelect:
    def setup_method(self):
        _clear_clarify_state()

    @pytest.mark.asyncio
    async def test_registered_multi_select_uses_dropdown_view(self):
        from tools import clarify_gateway as cm

        cm.register(
            "cid-send-multi",
            "sk-send-multi",
            "Pick colors",
            ["red", "green"],
            multi_select=True,
        )
        adapter = _make_adapter(allowed_users={"42"})
        channel = MagicMock()
        channel.send = AsyncMock(return_value=SimpleNamespace(id=123))
        adapter._client.get_channel = MagicMock(return_value=channel)

        result = await adapter.send_clarify(
            chat_id="9001",
            question="Pick colors",
            choices=["red", "green"],
            clarify_id="cid-send-multi",
            session_key="sk-send-multi",
        )

        assert result.success is True
        kwargs = channel.send.call_args.kwargs
        assert isinstance(kwargs["view"], ClarifyMultiChoiceView)
        assert kwargs["embed"].fields[0]["value"].startswith(
            "Select all that apply"
        )

    @pytest.mark.asyncio
    async def test_single_select_still_uses_button_view(self):
        adapter = _make_adapter(allowed_users={"42"})
        channel = MagicMock()
        channel.send = AsyncMock(return_value=SimpleNamespace(id=456))
        adapter._client.get_channel = MagicMock(return_value=channel)

        result = await adapter.send_clarify(
            chat_id="9001",
            question="Pick one color",
            choices=["red", "green"],
            clarify_id="cid-send-single",
            session_key="sk-send-single",
        )

        assert result.success is True
        assert isinstance(channel.send.call_args.kwargs["view"], ClarifyChoiceView)
