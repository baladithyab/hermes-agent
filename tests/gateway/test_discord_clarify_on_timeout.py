"""Discord clarify view timeouts must unblock the waiting agent."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from plugins.platforms.discord.adapter import (
    ClarifyChoiceView,
    ClarifyMultiChoiceView,
    discord,
)


def _message_with_embed():
    embed = MagicMock()
    embed.color = None
    embed.set_footer = MagicMock()
    return SimpleNamespace(embeds=[embed], edit=AsyncMock())


@pytest.mark.parametrize("view_cls", [ClarifyChoiceView, ClarifyMultiChoiceView])
@pytest.mark.asyncio
async def test_on_timeout_unblocks_and_expires_view(view_cls, monkeypatch):
    from tools import clarify_gateway as cm

    calls = []
    monkeypatch.setattr(
        cm,
        "resolve_gateway_clarify",
        lambda clarify_id, response: calls.append((clarify_id, response)) or True,
    )
    view = view_cls(
        choices=["one", "two"],
        clarify_id="cid-timeout",
        allowed_user_ids={"42"},
    )
    original_children = list(view.children)
    message = _message_with_embed()
    view._message = message

    await view.on_timeout()

    assert calls == [("cid-timeout", "")]
    assert view.resolved is True
    assert all(child.disabled for child in original_children)
    assert view.children == []
    message.edit.assert_awaited_once()
    kwargs = message.edit.call_args.kwargs
    assert kwargs["view"] is view
    assert kwargs["embed"].color == discord.Color.greyple()
    kwargs["embed"].set_footer.assert_called_once_with(
        text="⏱ Prompt expired — no action taken"
    )


@pytest.mark.parametrize("view_cls", [ClarifyChoiceView, ClarifyMultiChoiceView])
@pytest.mark.asyncio
async def test_late_timeout_after_resolution_is_noop(view_cls, monkeypatch):
    from tools import clarify_gateway as cm

    resolve = MagicMock()
    monkeypatch.setattr(cm, "resolve_gateway_clarify", resolve)
    view = view_cls(
        choices=["one", "two"],
        clarify_id="cid-late",
        allowed_user_ids={"42"},
    )
    view.resolved = True
    view._message = _message_with_embed()

    await view.on_timeout()

    resolve.assert_not_called()
    view._message.edit.assert_not_awaited()
