"""Telegram and WhatsApp Cloud keep multi-select answers on the numbered-text path."""

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from gateway.config import PlatformConfig
from tools import clarify_gateway as cm


@pytest.fixture(autouse=True)
def _isolated_clarify_state():
    with cm._lock:
        cm._entries.clear()
        cm._session_index.clear()
        cm._notify_cbs.clear()
    yield
    with cm._lock:
        cm._entries.clear()
        cm._session_index.clear()
        cm._notify_cbs.clear()


def _make_adapter(platform):
    if platform == "telegram":
        from plugins.platforms.telegram.adapter import TelegramAdapter

        adapter = TelegramAdapter(PlatformConfig(enabled=True, token="test-token", extra={}))
        adapter._bot = MagicMock()
        adapter._bot.send_message = AsyncMock(return_value=MagicMock(message_id=123))
        adapter._bot.send_chat_action = AsyncMock()
        return adapter, adapter._bot.send_message

    from gateway.platforms.whatsapp_cloud import WhatsAppCloudAdapter

    adapter = WhatsAppCloudAdapter(PlatformConfig(enabled=True, extra={
        "phone_number_id": "test-phone", "access_token": "test-token",
    }))
    response = SimpleNamespace(status_code=200, json=lambda: {"messages": [{"id": "wamid.test"}]})
    adapter._http_client = MagicMock()
    adapter._http_client.post = AsyncMock(return_value=response)
    return adapter, adapter._http_client.post


@pytest.mark.parametrize("platform", ["telegram", "whatsapp_cloud"])
@pytest.mark.parametrize("typed_reply", ["1, 3", "1 3"])
@pytest.mark.asyncio
async def test_multi_select_sends_numbered_text_and_accepts_several_choices(platform, typed_reply):
    adapter, transport = _make_adapter(platform)
    entry = cm.register("cid-multi", "sk-multi", "Pick colors", ["red", "green", "blue"], multi_select=True)

    result = await adapter.send_clarify(
        chat_id="12345", question=entry.question, choices=entry.choices,
        clarify_id=entry.clarify_id, session_key=entry.session_key, metadata={"thread_id": "77"},
    )

    assert result.success
    transport.assert_awaited_once()
    kwargs = transport.call_args.kwargs
    if platform == "telegram":
        text = kwargs["text"]
        assert kwargs.get("reply_markup") is None
        assert kwargs["message_thread_id"] == 77
    else:
        payload = kwargs["json"]
        assert payload["type"] == "text"
        text = payload["text"]["body"]
    assert "Pick colors" in text
    assert all(choice in text for choice in entry.choices)
    assert "Multiple selections allowed" in text
    assert entry.awaiting_text
    assert entry.clarify_id not in adapter._clarify_state
    assert cm.resolve_text_response_for_session(entry.session_key, typed_reply)
    assert json.loads(cm.wait_for_response(entry.clarify_id, timeout=0)) == ["red", "blue"]


@pytest.mark.parametrize("platform", ["telegram", "whatsapp_cloud"])
@pytest.mark.asyncio
async def test_single_select_keeps_native_controls(platform):
    adapter, transport = _make_adapter(platform)
    entry = cm.register("cid-single", "sk-single", "Pick one color", ["red", "green"])

    result = await adapter.send_clarify(
        chat_id="12345", question=entry.question, choices=entry.choices,
        clarify_id=entry.clarify_id, session_key=entry.session_key,
    )

    assert result.success
    transport.assert_awaited_once()
    kwargs = transport.call_args.kwargs
    if platform == "telegram":
        assert kwargs["reply_markup"] is not None
    else:
        assert kwargs["json"]["type"] == "interactive"
        assert kwargs["json"]["interactive"]["type"] == "button"
    assert not entry.awaiting_text
    assert adapter._clarify_state[entry.clarify_id] == entry.session_key
