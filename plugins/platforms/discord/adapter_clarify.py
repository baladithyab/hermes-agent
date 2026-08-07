"""Discord clarify views, built only when the adapter has loaded discord.py."""

from __future__ import annotations

import json
from types import ModuleType


def define_clarify_view_classes(base_view: type, adapter: ModuleType) -> tuple[type, type]:
    """Use the owning adapter's shared view base, live globals, and lazy-loaded discord.py."""

    class _ClarifyView(base_view):
        """Shared text-capture and expiry behavior for single- and multi-select prompts."""

        def __init__(self, clarify_id: str, allowed_user_ids: set, allowed_role_ids: set | None):
            super().__init__(allowed_user_ids, allowed_role_ids, timeout=adapter._read_discord_prompt_timeout())
            self.clarify_id = clarify_id

        async def _finish(self, interaction, color, footer: str, *, log_edit_failure: bool) -> None:
            """Disable the controls and stamp the embed; fall back to a bare defer."""
            self.resolved = True
            self._disable_all()
            embed = self._first_embed(interaction.message) if interaction.message else None
            if embed:
                embed.color = color
                embed.set_footer(text=footer)
            try:
                await interaction.response.edit_message(embed=embed, view=self)
            except Exception:
                if log_edit_failure:
                    adapter.logger.debug("Discord clarify edit_message failed for %s", self.clarify_id, exc_info=True)
                try:
                    await interaction.response.defer()
                except Exception:
                    adapter.logger.debug("Discord clarify defer failed (id=%s)", self.clarify_id, exc_info=True)

        async def _on_other(self, interaction) -> None:
            """Flip the clarify entry into text-capture mode."""
            if not await self._gate(
                interaction, resolved_msg=adapter.t("platform.discord.prompt.clarify_already_answered"),
                unauth_msg=adapter._unauthorized(),
            ):
                return
            # Don't pop: the gateway text-intercept needs the entry until the user types.
            try:
                from tools.clarify_gateway import mark_awaiting_text
                mark_awaiting_text(self.clarify_id)
            except Exception as exc:
                adapter.logger.warning(
                    "Discord clarify mark_awaiting_text failed (id=%s): %s", self.clarify_id, exc, exc_info=True)
            display_name = getattr(getattr(interaction, "user", None), "display_name", "user")
            await self._finish(
                interaction, adapter.discord.Color.blue(),
                adapter.t("platform.discord.prompt.awaiting_typed", user=display_name), log_edit_failure=False)

        async def on_timeout(self):
            if self.resolved:
                return
            self.resolved = True
            self._disable_all()
            self.clear_items()
            # Component expiry must release the worker before any best-effort embed edit.
            try:
                from tools.clarify_gateway import resolve_gateway_clarify
                resolve_gateway_clarify(self.clarify_id, "")
            except Exception:
                adapter.logger.debug("Discord clarify timeout resolve failed (id=%s)", self.clarify_id, exc_info=True)
            await self._expire_embed(adapter.t("platform.discord.prompt.expired_footer"))

    class ClarifyChoiceView(_ClarifyView):
        """One button per clarify choice (max 24) plus ``✏️ Other``. A numeric click resolves the
        gateway clarify entry immediately; ``Other`` flips to text-capture (next message answers).
        Single-use: after the first valid click all buttons disable."""

        def __init__(self, choices: list[str], clarify_id: str, allowed_user_ids: set, allowed_role_ids: set | None = None):
            super().__init__(clarify_id, allowed_user_ids, allowed_role_ids)
            self.choices = list(choices)[:24]
            for index, choice in enumerate(self.choices):
                button = adapter.discord.ui.Button(
                    label=self._button_label(index, choice), style=adapter.discord.ButtonStyle.primary,
                    custom_id=f"clarify:{clarify_id}:{index}",
                )
                button.callback = self._make_choice_callback(index, choice)
                self.add_item(button)
            other_btn = adapter.discord.ui.Button(
                label=adapter._t_discord("platform.discord.prompt.other", adapter._DISCORD_BUTTON_LABEL_LIMIT),
                style=adapter.discord.ButtonStyle.secondary, custom_id=f"clarify:{clarify_id}:other",
            )
            other_btn.callback = self._on_other
            self.add_item(other_btn)

        @staticmethod
        def _button_label(index: int, choice: str) -> str:
            """``"N. <choice>"`` within Discord's 80-char (UTF-16) label cap.
            Mobile wraps early, so long choices cut at a word boundary in the trailing half, else a
            soft boundary (``- , . )``, inclusive), else hard."""
            prefix = f"{index + 1}. "
            budget = adapter._DISCORD_BUTTON_LABEL_LIMIT - adapter.utf16_len(prefix)
            if adapter.utf16_len(choice) <= budget:
                return f"{prefix}{choice}"
            truncated = adapter._prefix_within_utf16_limit(
                choice, max(0, budget - adapter.utf16_len(adapter._DISCORD_ELLIPSIS))).rstrip()
            cut_at = -1
            space = truncated.rfind(" ")
            if space >= len(truncated) // 2:
                cut_at = space
            if cut_at < 0:
                latest_soft = max((truncated.rfind(s) for s in ("-", ",", ".", ")")), default=-1)
                if latest_soft >= len(truncated) // 2:
                    cut_at = latest_soft + 1
            if cut_at > 0:
                truncated = truncated[:cut_at]
            return f"{prefix}{truncated.rstrip() + adapter._DISCORD_ELLIPSIS}"

        def _make_choice_callback(self, index: int, choice: str):
            async def _callback(interaction):
                await self._resolve_choice(interaction, index, choice)
            return _callback

        async def _resolve_choice(self, interaction, index: int, choice: str) -> None:
            """Resolve the clarify with a chosen option."""
            if not await self._gate(
                interaction, resolved_msg=adapter.t("platform.discord.prompt.clarify_already_answered"),
                unauth_msg=adapter._unauthorized(),
            ):
                return
            display_name = getattr(getattr(interaction, "user", None), "display_name", "user")
            await self._finish(
                interaction, adapter.discord.Color.green(),
                adapter.t("platform.discord.prompt.answered_by", user=display_name, choice=choice), log_edit_failure=True)
            # Round-trip the canonical choice text from the entry, not the button label.
            from tools.clarify_gateway import _entries as _clarify_entries
            entry = _clarify_entries.get(self.clarify_id)
            resolved_text = choice
            if entry and entry.choices and 0 <= index < len(entry.choices):
                resolved_text = entry.choices[index]
            try:
                from tools.clarify_gateway import resolve_gateway_clarify
                resolved = resolve_gateway_clarify(self.clarify_id, resolved_text)
                adapter.logger.info(
                    "Discord clarify button resolved (id=%s, choice=%r, user=%s, ok=%s)",
                    self.clarify_id, resolved_text,
                    getattr(getattr(interaction, "user", None), "display_name", "?"), resolved,
                )
            except Exception as exc:
                adapter.logger.error(
                    "Discord clarify resolve_gateway_clarify failed (id=%s): %s", self.clarify_id, exc, exc_info=True)

    class ClarifyMultiChoiceView(_ClarifyView):
        """Select-menu view for clarify prompts that accept many choices."""

        def __init__(self, choices: list[str], clarify_id: str, allowed_user_ids: set, allowed_role_ids: set | None = None):
            super().__init__(clarify_id, allowed_user_ids, allowed_role_ids)
            self.choices = list(choices)[:adapter._DISCORD_SELECT_MAX_OPTIONS]
            options = [
                adapter.discord.SelectOption(
                    label=adapter._truncate_discord_component_text(str(choice), adapter._DISCORD_SELECT_FIELD_LIMIT),
                    value=str(index),
                )
                for index, choice in enumerate(self.choices)
            ]
            select = adapter.discord.ui.Select(
                placeholder=adapter._t_discord(
                    "platform.discord.prompt.clarify_multi_placeholder", adapter._DISCORD_SELECT_PLACEHOLDER_LIMIT),
                min_values=0, max_values=len(options), options=options,
                custom_id=f"clarify:{clarify_id}:multi", row=0,
            )
            select.callback = self._on_select
            self.add_item(select)
            other_btn = adapter.discord.ui.Button(
                label=adapter._t_discord("platform.discord.prompt.other", adapter._DISCORD_BUTTON_LABEL_LIMIT),
                style=adapter.discord.ButtonStyle.secondary, custom_id=f"clarify:{clarify_id}:other", row=1,
            )
            other_btn.callback = self._on_other
            self.add_item(other_btn)

        async def _on_select(self, interaction) -> None:
            if not await self._gate(
                interaction, resolved_msg=adapter.t("platform.discord.prompt.clarify_already_answered"),
                unauth_msg=adapter._unauthorized(),
            ):
                return
            selected_values = []
            for raw_value in (getattr(interaction, "data", {}) or {}).get("values", []):
                try:
                    index = int(raw_value)
                except (TypeError, ValueError):
                    continue
                if 0 <= index < len(self.choices):
                    selected_values.append(self.choices[index])
            display_name = getattr(getattr(interaction, "user", None), "display_name", "user")
            selected_text = ", ".join(selected_values) or adapter.t("platform.discord.prompt.clarify_none_selected")
            await self._finish(
                interaction, adapter.discord.Color.green(),
                adapter.t("platform.discord.prompt.answered_by", user=display_name, choice=selected_text), log_edit_failure=True)
            try:
                from tools.clarify_gateway import resolve_gateway_clarify
                resolve_gateway_clarify(self.clarify_id, json.dumps(selected_values, ensure_ascii=False))
            except Exception:
                adapter.logger.error("Discord clarify multi-select resolution failed (id=%s)", self.clarify_id, exc_info=True)

    return ClarifyChoiceView, ClarifyMultiChoiceView
