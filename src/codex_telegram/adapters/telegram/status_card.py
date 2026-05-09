"""Telegram pinned status-card synchronization."""

from __future__ import annotations

from typing import Protocol

from aiogram import Bot
from aiogram.types import InlineKeyboardMarkup, Message

from codex_telegram.adapters.telegram.errors import _telegram_message_not_modified
from codex_telegram.adapters.telegram.rendering import (
    _public_error_summary,
    _status_card_is_stale,
    render_overview,
)
from codex_telegram.adapters.telegram.routing import ChatContext
from codex_telegram.application.models import CodexRuntimeState
from codex_telegram.application.ports import ProgressMessageStore
from codex_telegram.application.service import BotService
from codex_telegram.domain import BridgeThread, ConversationAnchor, LogicalThread
from codex_telegram.observability import get_logger, log_warning

LOGGER = get_logger(__name__)

STATUS_CARD_PARSE_MODE = "HTML"

CallbackRows = list[list[tuple[str, str, dict[str, object]]]]


def _unanchored_bridge_anchor(thread: LogicalThread) -> ConversationAnchor:
    return ConversationAnchor(
        anchor_id=f"bridge:{thread.thread_id}",
        chat_key=thread.chat_key,
        codex_backend_id=thread.codex_backend_id,
        codex_thread_id=thread.codex_thread_id or thread.thread_id,
        title=thread.title,
        alias=None,
        project_id=None,
        latest_bridge_id=thread.thread_id,
        created_at=thread.created_at,
        updated_at=thread.updated_at,
        latest_bridge_pending_turn_id=thread.pending_turn_id,
        latest_bridge_awaiting_reply=thread.awaiting_reply,
        latest_bridge_expires_at=thread.expires_at,
        latest_bridge_closed_at=thread.closed_at,
    )


class SendText(Protocol):
    async def __call__(
        self,
        context: ChatContext,
        text: str,
        *,
        reply_markup: InlineKeyboardMarkup | None = None,
        parse_mode: str | None = None,
    ) -> Message: ...


class EditText(Protocol):
    async def __call__(
        self,
        context: ChatContext,
        *,
        message_id: int,
        text: str,
        reply_markup: InlineKeyboardMarkup | None = None,
        parse_mode: str | None = None,
    ) -> None: ...


class CallbackMarkup(Protocol):
    async def __call__(
        self,
        context: ChatContext,
        rows: CallbackRows,
    ) -> InlineKeyboardMarkup | None: ...


class ConversationFocusTextShortcuts(Protocol):
    async def __call__(
        self,
        context: ChatContext,
        anchors: list[ConversationAnchor],
        focused_bridge_id: str | None,
        *,
        prefix: str = "cf",
    ) -> dict[str, str]: ...


class TelegramStatusCardSynchronizer:
    """Create, edit, pin, and replace the per-chat Telegram status card."""

    def __init__(
        self,
        bot: Bot,
        service: BotService,
        progress_store: ProgressMessageStore,
        send_text: SendText,
        edit_text: EditText,
        callback_markup: CallbackMarkup,
        conversation_focus_text_shortcuts: ConversationFocusTextShortcuts,
    ) -> None:
        self._bot = bot
        self._service = service
        self._progress_store = progress_store
        self._send_text = send_text
        self._edit_text = edit_text
        self._callback_markup = callback_markup
        self._conversation_focus_text_shortcuts = conversation_focus_text_shortcuts

    async def sync_thread(self, context: ChatContext) -> None:
        await self.refresh(
            context,
            create_if_missing=True,
            reconcile_pinned=True,
            refresh_pinned_preview=True,
        )

    async def show(self, context: ChatContext) -> None:
        await self.refresh(
            context,
            create_if_missing=True,
            reconcile_pinned=True,
            refresh_pinned_preview=True,
        )

    async def refresh(
        self,
        context: ChatContext,
        *,
        create_if_missing: bool = False,
        reconcile_pinned: bool = False,
        refresh_pinned_preview: bool = False,
    ) -> None:
        conversations = await self._service.list_conversations(context.chat_key)
        if not isinstance(conversations, list):
            return
        focused = await self._service.ensure_focused_bridge(context.chat_key)
        runtime = await self._service.runtime_state_for_thread(focused)
        if not isinstance(runtime, CodexRuntimeState):
            runtime = CodexRuntimeState()
        text = await self._render_text(context, conversations, focused, runtime)
        reply_markup = await self._status_card_markup(context)
        card = await self._progress_store.get_status_card(context.chat_key)
        if card is not None and create_if_missing and _status_card_is_stale(card):
            await self._unpin(context, card.message_id)
            await self._send_new(context, text, reply_markup)
            return
        pinned_message_id = (
            await self._current_pinned_message_id(context) if reconcile_pinned else None
        )
        if pinned_message_id is not None:
            should_refresh_preview = (
                card is None or _preview_line_changed(card.rendered_text, text)
            )
            if (
                card is not None
                and card.message_id == pinned_message_id
                and card.rendered_text == text
            ):
                return
            replaced = await self._replace_message(
                context,
                message_id=pinned_message_id,
                text=text,
                reply_markup=reply_markup,
            )
            if replaced and refresh_pinned_preview and should_refresh_preview:
                await self._repin(context, pinned_message_id)
            return
        if card is None:
            if not create_if_missing:
                return
            await self._send_new(context, text, reply_markup)
            return
        if card.rendered_text == text:
            return
        try:
            await self._edit_text(
                context,
                message_id=card.message_id,
                text=text,
                reply_markup=reply_markup,
                parse_mode=STATUS_CARD_PARSE_MODE,
            )
        except Exception as err:
            if _telegram_message_not_modified(err):
                return
            log_warning(
                LOGGER,
                "telegram_status_card_edit_failed",
                v={"message_id": card.message_id, "error": _public_error_summary(err)},
            )
            if not create_if_missing:
                return
            await self._unpin(context, card.message_id)
            await self._send_new(context, text, reply_markup)
            return
        await self._progress_store.save_status_card(
            context.chat_key,
            chat_id=context.chat_id,
            topic_id=context.topic_id,
            message_id=card.message_id,
            rendered_text=text,
        )
        if reconcile_pinned and refresh_pinned_preview:
            await self._pin(context, card.message_id)

    async def _render_text(
        self,
        context: ChatContext,
        conversations: list[ConversationAnchor],
        focused: BridgeThread | LogicalThread,
        runtime: CodexRuntimeState,
    ) -> str:
        overview_conversations = await self._overview_conversations(
            context,
            conversations,
            focused.bridge_id,
        )
        focus_commands = await self._conversation_focus_text_shortcuts(
            context,
            overview_conversations,
            focused.bridge_id,
            prefix="ct",
        )
        return render_overview(
            overview_conversations,
            focused.bridge_id,
            focused_title=getattr(focused, "title", None),
            focused_codex_backend_id=getattr(focused, "codex_backend_id", None),
            focused_codex_thread_id=getattr(focused, "codex_thread_id", None),
            runtime=runtime,
            focus_commands=focus_commands,
        )

    async def _overview_conversations(
        self,
        context: ChatContext,
        conversations: list[ConversationAnchor],
        focused_bridge_id: str | None,
    ) -> list[ConversationAnchor]:
        known_bridge_ids = {
            anchor.latest_bridge_id
            for anchor in conversations
            if anchor.latest_bridge_id is not None
        }
        threads = await self._service.list_threads(context.chat_key)
        if not isinstance(threads, list):
            return conversations
        unanchored = [
            _unanchored_bridge_anchor(thread)
            for thread in threads
            if thread.anchor_id is None
            and thread.thread_id != focused_bridge_id
            and thread.thread_id not in known_bridge_ids
            and thread.closed_at is None
        ]
        return [*conversations, *unanchored]

    async def _status_card_markup(
        self,
        context: ChatContext,
    ) -> InlineKeyboardMarkup | None:
        return await self._callback_markup(
            context,
            [
                [
                    ("Threads", "show_threads", {}),
                    ("Projects", "show_projects", {}),
                ]
            ],
        )

    async def _send_new(
        self,
        context: ChatContext,
        text: str,
        reply_markup: InlineKeyboardMarkup | None,
    ) -> None:
        sent = await self._send_text(
            context,
            text,
            reply_markup=reply_markup,
            parse_mode=STATUS_CARD_PARSE_MODE,
        )
        await self._progress_store.save_status_card(
            context.chat_key,
            chat_id=context.chat_id,
            topic_id=context.topic_id,
            message_id=sent.message_id,
            rendered_text=text,
        )
        await self._pin(context, sent.message_id)

    async def _replace_message(
        self,
        context: ChatContext,
        *,
        message_id: int,
        text: str,
        reply_markup: InlineKeyboardMarkup | None,
    ) -> bool:
        try:
            await self._edit_text(
                context,
                message_id=message_id,
                text=text,
                reply_markup=reply_markup,
                parse_mode=STATUS_CARD_PARSE_MODE,
            )
        except Exception as err:
            if _telegram_message_not_modified(err):
                return False
            else:
                log_warning(
                    LOGGER,
                    "telegram_status_card_pinned_edit_failed",
                    v={
                        "message_id": message_id,
                        "error": _public_error_summary(err),
                    },
                )
                return False
        await self._progress_store.save_status_card(
            context.chat_key,
            chat_id=context.chat_id,
            topic_id=context.topic_id,
            message_id=message_id,
            rendered_text=text,
        )
        return True

    async def _current_pinned_message_id(self, context: ChatContext) -> int | None:
        try:
            chat = await self._bot.get_chat(context.chat_id)
        except Exception as err:
            log_warning(
                LOGGER,
                "telegram_status_card_get_chat_failed",
                v={"error": _public_error_summary(err)},
            )
            return None
        pinned = getattr(chat, "pinned_message", None)
        message_id = getattr(pinned, "message_id", None)
        return message_id if isinstance(message_id, int) else None

    async def _pin(self, context: ChatContext, message_id: int) -> None:
        try:
            await self._bot.pin_chat_message(
                chat_id=context.chat_id,
                message_id=message_id,
                disable_notification=True,
            )
        except Exception as err:
            log_warning(
                LOGGER,
                "telegram_status_card_pin_failed",
                v={"message_id": message_id, "error": _public_error_summary(err)},
            )

    async def _repin(self, context: ChatContext, message_id: int) -> None:
        await self._unpin(context, message_id)
        await self._pin(context, message_id)

    async def _unpin(self, context: ChatContext, message_id: int) -> None:
        try:
            await self._bot.unpin_chat_message(
                chat_id=context.chat_id,
                message_id=message_id,
            )
        except Exception as err:
            log_warning(
                LOGGER,
                "telegram_status_card_unpin_failed",
                v={"message_id": message_id, "error": _public_error_summary(err)},
            )


def _preview_line_changed(old_text: str, new_text: str) -> bool:
    return _preview_line(old_text) != _preview_line(new_text)


def _preview_line(text: str) -> str:
    return text.partition("\n")[0]
