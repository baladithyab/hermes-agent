"""Discord clarify view timeouts must unblock the waiting agent."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from plugins.platforms.discord.adapter import (
    ClarifyChoiceView,
    ClarifyMultiChoiceView,
    discord,
)


@pytest.fixture(autouse=True)
def _isolated_clarify_state():
    from tools import clarify_gateway as cm

    with cm._lock:
        cm._entries.clear()
        cm._session_index.clear()
        cm._notify_cbs.clear()
    yield
    with cm._lock:
        cm._entries.clear()
        cm._session_index.clear()
        cm._notify_cbs.clear()


@pytest.mark.parametrize("view_cls", [ClarifyChoiceView, ClarifyMultiChoiceView])
@pytest.mark.parametrize("message_state", ["editable", "failed_edit", "missing"])
@pytest.mark.asyncio
async def test_on_timeout_unblocks_before_embed_edit(view_cls, message_state):
    from tools import clarify_gateway as cm

    entry = cm.register(
        "cid-timeout", "sk-timeout", "Pick", ["one", "two"],
        multi_select=view_cls is ClarifyMultiChoiceView,
    )
    view = view_cls(choices=entry.choices, clarify_id=entry.clarify_id, allowed_user_ids={"42"})
    original_children = list(view.children)
    observed = []
    embed = MagicMock()
    embed.set_footer.side_effect = lambda **_kwargs: observed.append(("footer", entry.event.is_set(), entry.response))

    async def edit(**_kwargs):
        observed.append(("edit", entry.event.is_set(), entry.response))
        if message_state == "failed_edit":
            raise RuntimeError("Message cannot be edited")

    message = SimpleNamespace(embeds=[embed], edit=AsyncMock(side_effect=edit))
    view._message = message if message_state != "missing" else None

    await view.on_timeout()

    assert entry.event.is_set()
    assert entry.response == ""
    assert cm.wait_for_response(entry.clarify_id, timeout=0) == ""
    assert not cm.has_pending(entry.session_key)
    assert all(child.disabled for child in original_children)
    assert view.children == []
    if message_state == "missing":
        assert observed == []
        message.edit.assert_not_awaited()
    else:
        assert observed == [("footer", True, ""), ("edit", True, "")]
        message.edit.assert_awaited_once_with(embed=embed, view=view)
        assert embed.color == discord.Color.greyple()
        embed.set_footer.assert_called_once_with(text="⏱ Prompt expired — no action taken")


@pytest.mark.parametrize("view_cls", [ClarifyChoiceView, ClarifyMultiChoiceView])
@pytest.mark.asyncio
async def test_late_timeout_after_resolution_is_noop(view_cls, monkeypatch):
    from tools import clarify_gateway as cm

    resolve = MagicMock()
    monkeypatch.setattr(cm, "resolve_gateway_clarify", resolve)
    view = view_cls(choices=["one", "two"], clarify_id="cid-late", allowed_user_ids={"42"})
    view.resolved = True
    view._message = SimpleNamespace(embeds=[MagicMock()], edit=AsyncMock())

    await view.on_timeout()

    resolve.assert_not_called()
    view._message.edit.assert_not_awaited()
