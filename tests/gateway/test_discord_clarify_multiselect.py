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


@pytest.fixture(autouse=True)
def _isolated_clarify_and_auth(monkeypatch):
    for name in ("DISCORD_ALLOW_ALL_USERS", "GATEWAY_ALLOW_ALL_USERS", "GATEWAY_ALLOWED_USERS"):
        monkeypatch.delenv(name, raising=False)
    store = SimpleNamespace(is_approved=lambda *_: False)
    monkeypatch.setattr("gateway.pairing.PairingStore", lambda: store)
    _clear_clarify_state()
    yield
    _clear_clarify_state()


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

    @pytest.mark.asyncio
    async def test_constructor_caps_choices_at_discord_limit(self):
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

    @pytest.mark.parametrize("values,expected", [
        (["0", "2"], ["red, blue (Recommended)", "雪"]),
        (["1"], ["green"]),
        ([], []),
    ])
    @pytest.mark.asyncio
    async def test_select_callback_resolves_json_array(self, values, expected):
        from tools import clarify_gateway as cm

        cm.register(
            "cid-resolve",
            "sk-resolve",
            "Pick any",
            ["red, blue (Recommended)", "green", "雪"],
            multi_select=True,
        )
        view = ClarifyMultiChoiceView(
            choices=["red, blue (Recommended)", "green", "雪"],
            clarify_id="cid-resolve",
            allowed_user_ids={"42"},
        )

        interaction = _make_interaction(values=values)
        await view.children[0].callback(interaction)

        with cm._lock:
            entry = cm._entries["cid-resolve"]
        assert json.loads(entry.response) == expected
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

        await view.children[1].callback(_make_interaction())

        with cm._lock:
            assert cm._entries["cid-other"].awaiting_text is True
        assert cm.resolve_text_response_for_session("sk-other", "purple, yellow")
        assert cm.wait_for_response("cid-other", timeout=0) == "purple, yellow"

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
        assert "Select all that apply" in kwargs["content"]
        assert kwargs["content"].count("Pick colors") == 1
        assert not kwargs["embed"].description
        assert not kwargs["embed"].fields

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

    @pytest.mark.parametrize("multi_select", [False, True], ids=["single", "multi"])
    @pytest.mark.parametrize("control", ["answer", "other"])
    @pytest.mark.asyncio
    async def test_native_controls_follow_live_authorization(self, multi_select, control):
        from tools import clarify_gateway as cm

        entry = cm.register("cid-live", "sk-live", "Pick colors", ["red", "green"], multi_select=multi_select)
        adapter = _make_adapter(allowed_users={"42"})
        grants = {"42": True}
        adapter.set_authorization_check(lambda user_id, chat_type, chat_id: grants.get(user_id, False))
        channel = SimpleNamespace(send=AsyncMock(return_value=SimpleNamespace(id=789)))
        adapter._client.get_channel = MagicMock(return_value=channel)

        result = await adapter.send_clarify(
            chat_id="9001", question=entry.question, choices=entry.choices,
            clarify_id=entry.clarify_id, session_key=entry.session_key,
        )
        assert result.success
        view = channel.send.call_args.kwargs["view"]
        child = view.children[-1 if control == "other" else 0]

        # A revoke after sending the card must override the adapter's connect-time snapshot.
        grants["42"] = False
        rejected = _make_interaction(values=["0"])
        await child.callback(rejected)
        rejected.response.send_message.assert_awaited_once()
        rejected.response.edit_message.assert_not_awaited()
        assert not view.resolved
        assert not entry.awaiting_text
        assert not entry.event.is_set()
        assert entry.response is None

        grants["42"] = True
        accepted = _make_interaction(values=["0"])
        await child.callback(accepted)
        accepted.response.edit_message.assert_awaited_once()
        assert view.resolved
        if control == "other":
            assert entry.awaiting_text
            assert not entry.event.is_set()
        else:
            assert entry.event.is_set()
            response = cm.wait_for_response(entry.clarify_id, timeout=0)
            assert (json.loads(response) if multi_select else response) == (["red"] if multi_select else "red")
