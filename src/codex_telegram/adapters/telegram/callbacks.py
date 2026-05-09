"""Telegram callback-query routing helpers."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Coroutine
from typing import Any, Protocol

from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message

from codex_telegram.adapters.telegram.routing import ChatContext, build_chat_key
from codex_telegram.adapters.telegram.rendering import (
    COMMAND_FAILURE_PREFIX,
    COMMAND_SUCCESS_PREFIX,
    WARNING_PREFIX,
    _user_input_complete,
    render_codex_thread_attached,
    render_project_state,
)
from codex_telegram.application.models import CallbackToken
from codex_telegram.application.ports import StateRepository
from codex_telegram.application.service import BotService, ThreadSelectionResult
from codex_telegram.domain import PendingApproval, PendingUserInput
from codex_telegram.observability import (
    clear_log_context,
    get_logger,
    log_context,
    log_exception,
)

LOGGER = get_logger(__name__)


class CallbackTokenRepository(Protocol):
    async def consume_callback_token(
        self,
        token: str,
        *,
        chat_key: str,
        topic_id: int | None,
    ) -> CallbackToken | None: ...


class CallbackActionHandler(Protocol):
    async def __call__(
        self,
        context: ChatContext,
        token: CallbackToken,
        *,
        message_id: int | None = None,
    ) -> None: ...


class CallbackActionHost(Protocol):
    _service: BotService
    _repository: StateRepository

    async def _send_text(
        self,
        context: ChatContext,
        text: str,
        *,
        reply_markup: InlineKeyboardMarkup | None = None,
        parse_mode: str | None = None,
    ) -> Message: ...

    async def _send_focus_final_messages(
        self,
        context: ChatContext,
        thread_id: str,
    ) -> None: ...

    async def _send_reply_prompt(
        self,
        context: ChatContext,
        thread_id: str,
    ) -> None: ...

    async def _focus_bridge(
        self,
        context: ChatContext,
        selector: str,
    ) -> ThreadSelectionResult: ...

    async def _run_plan_implementation(
        self,
        context: ChatContext,
        thread_id: str,
    ) -> None: ...

    async def _sync_thread_status_card(self, context: ChatContext) -> None: ...

    async def _send_codex_threads_project_picker(
        self,
        context: ChatContext,
        *,
        connection_id: str | None,
        connection_label: str,
        include_all: bool,
    ) -> None: ...

    async def _send_recent_codex_threads(self, context: ChatContext) -> None: ...

    async def _send_recent_new_projects(self, context: ChatContext) -> None: ...

    async def _send_codex_threads_thread_picker(
        self,
        context: ChatContext,
        *,
        connection_id: str | None,
        include_all: bool,
        project_id: str | None,
    ) -> None: ...

    async def _send_codex_threads_connection_picker(
        self,
        context: ChatContext,
    ) -> None: ...

    async def _new_conversation_notice(
        self,
        context: ChatContext,
        thread: Any,
    ) -> str: ...

    async def _send_new_project_picker(
        self,
        context: ChatContext,
        *,
        connection_id: str,
        connection_label: str,
    ) -> None: ...

    async def _callback_markup(
        self,
        context: ChatContext,
        rows: list[list[tuple[str, str, dict[str, object]]]],
    ) -> InlineKeyboardMarkup | None: ...

    def _watch_turn_after_approval(
        self,
        context: ChatContext,
        pending: PendingApproval,
    ) -> Coroutine[object, object, None]: ...

    def _spawn_background(self, coro: Coroutine[object, object, None]) -> None: ...

    async def _submit_user_input(
        self,
        context: ChatContext,
        pending: PendingUserInput,
    ) -> None: ...

    async def _refresh_user_input_message(
        self,
        context: ChatContext,
        pending: PendingUserInput,
        *,
        message_id: int | None,
    ) -> None: ...


class TelegramCallbackActionExecutor:
    """Execute decoded callback actions against Telegram shell operations."""

    def __init__(self, host: CallbackActionHost) -> None:
        self._host = host

    async def handle(
        self,
        context: ChatContext,
        token: CallbackToken,
        *,
        message_id: int | None = None,
    ) -> None:
        host = self._host
        if token.action == "focus_conversation":
            selector = _payload_str_or_none(token.payload, "selector")
            if selector is None:
                selector = _payload_str(token.payload, "anchor_id")
            result = await host._focus_bridge(context, selector)
            await host._send_text(context, result.message)
            if result.success and result.thread is not None:
                await host._send_focus_final_messages(
                    context,
                    result.thread.thread_id,
                )
            await host._sync_thread_status_card(context)
            return
        if token.action == "reply_conversation":
            await host._send_reply_prompt(
                context,
                _payload_str(token.payload, "thread_id"),
            )
            return
        if token.action == "implement_plan":
            await host._run_plan_implementation(
                context,
                _payload_str(token.payload, "thread_id"),
            )
            return
        if token.action == "codex_threads_connection":
            await host._send_codex_threads_project_picker(
                context,
                connection_id=_payload_str_or_none(token.payload, "connection_id"),
                connection_label=_payload_str(token.payload, "connection_label"),
                include_all=_payload_bool(token.payload, "include_all"),
            )
            return
        if token.action == "codex_threads_recent":
            await host._send_recent_codex_threads(context)
            return
        if token.action == "codex_threads_recent_projects":
            await host._send_codex_threads_project_picker(
                context,
                connection_id=None,
                connection_label="all connections",
                include_all=True,
            )
            return
        if token.action == "new_recent_projects":
            await host._send_recent_new_projects(context)
            return
        if token.action == "codex_threads_project":
            await host._send_codex_threads_thread_picker(
                context,
                connection_id=_payload_str_or_none(token.payload, "connection_id"),
                include_all=_payload_bool(token.payload, "include_all"),
                project_id=_payload_str_or_none(token.payload, "project_id"),
            )
            return
        if token.action == "show_threads":
            await host._send_codex_threads_connection_picker(context)
            return
        if token.action == "show_projects":
            project_state = await host._service.show_project_state(context.chat_key)
            await host._send_text(context, render_project_state(project_state))
            return
        if token.action == "new_default_project":
            thread = await host._service.new_thread_in_default_project(
                context.chat_key,
            )
            await host._send_text(
                context, await host._new_conversation_notice(context, thread)
            )
            await host._sync_thread_status_card(context)
            return
        if token.action == "new_connection":
            await host._send_new_project_picker(
                context,
                connection_id=_payload_str(token.payload, "connection_id"),
                connection_label=_payload_str(token.payload, "connection_label"),
            )
            return
        if token.action == "new_project":
            thread = await host._service.new_thread_in_project(
                context.chat_key,
                _payload_str(token.payload, "project_id"),
            )
            await host._send_text(
                context, await host._new_conversation_notice(context, thread)
            )
            await host._sync_thread_status_card(context)
            return
        if token.action == "attach_codex":
            codex_thread_id = _payload_str(token.payload, "codex_thread_id")
            codex_backend_id = _payload_str_or_none(token.payload, "codex_backend_id")
            connection = await host._service.attach_codex_thread(
                context.chat_key,
                codex_thread_id,
                backend_id=codex_backend_id,
            )
            await host._send_text(context, render_codex_thread_attached(connection))
            await host._send_focus_final_messages(
                context,
                connection.bridge.bridge_id,
            )
            await host._sync_thread_status_card(context)
            return
        if token.action == "resolve_approval":
            request_id = _payload_int(token.payload, "request_id")
            decision = _payload_str(token.payload, "decision")
            pending = await host._service.pending_request_for_chat(context.chat_key)
            if pending is None or pending.request_id != request_id:
                await host._send_text(
                    context,
                    "That approval request is no longer pending.",
                )
                return
            response = await host._service.resolve_pending_request(
                request_id,
                decision,
            )
            await host._send_text(context, response)
            await host._sync_thread_status_card(context)
            host._spawn_background(host._watch_turn_after_approval(context, pending))
            return
        if token.action == "revoke_webhook":
            webhook_id = _payload_str(token.payload, "webhook_id")
            await host._send_text(
                context,
                WARNING_PREFIX + f"Confirm webhook revoke: {webhook_id}",
                reply_markup=await host._callback_markup(
                    context,
                    [
                        [
                            (
                                "Confirm revoke",
                                "confirm_revoke_webhook",
                                {"webhook_id": webhook_id},
                            )
                        ]
                    ],
                ),
            )
            return

        if token.action == "confirm_revoke_webhook":
            webhook_id = _payload_str(token.payload, "webhook_id")
            revoked = await host._service.revoke_webhook_subscription(
                webhook_id,
                chat_key=context.chat_key,
            )
            message = (
                COMMAND_SUCCESS_PREFIX + f"Revoked webhook {webhook_id}."
                if revoked
                else COMMAND_FAILURE_PREFIX + f"Unknown webhook {webhook_id}."
            )
            await host._send_text(context, message)
            return
        if token.action == "user_input_select":
            await self._handle_user_input_select(
                context,
                token,
                message_id=message_id,
            )
            return
        if token.action == "user_input_other":
            await self._handle_user_input_other(
                context,
                token,
                message_id=message_id,
            )
            return
        if token.action == "user_input_submit":
            await self._handle_user_input_submit(context, token)
            return
        raise ValueError(f"Unsupported callback action: {token.action}")

    async def _handle_user_input_select(
        self,
        context: ChatContext,
        token: CallbackToken,
        *,
        message_id: int | None,
    ) -> None:
        request_id = _payload_int(token.payload, "request_id")
        question_id = _payload_str(token.payload, "question_id")
        answer = _payload_str(token.payload, "answer")
        pending = await self._host._repository.get_pending_user_input(context.chat_key)
        if pending is None or pending.request_id != request_id:
            await self._host._send_text(context, "This question is no longer pending.")
            return
        await self._host._repository.update_pending_user_input_selection(
            request_id,
            question_id=question_id,
            answers=(answer,),
            awaiting_free_text=False,
        )
        updated = await self._host._repository.get_pending_user_input(context.chat_key)
        if updated is None:
            return
        if len(updated.questions) == 1 and _user_input_complete(updated):
            await self._host._submit_user_input(context, updated)
            return
        await self._host._refresh_user_input_message(
            context,
            updated,
            message_id=message_id,
        )

    async def _handle_user_input_other(
        self,
        context: ChatContext,
        token: CallbackToken,
        *,
        message_id: int | None,
    ) -> None:
        request_id = _payload_int(token.payload, "request_id")
        question_id = _payload_str(token.payload, "question_id")
        pending = await self._host._repository.get_pending_user_input(context.chat_key)
        if pending is None or pending.request_id != request_id:
            await self._host._send_text(context, "This question is no longer pending.")
            return
        await self._host._repository.update_pending_user_input_selection(
            request_id,
            question_id=question_id,
            answers=(),
            awaiting_free_text=True,
        )
        updated = await self._host._repository.get_pending_user_input(context.chat_key)
        if updated is None:
            return
        await self._host._refresh_user_input_message(
            context,
            updated,
            message_id=message_id,
        )
        await self._host._send_text(context, "Reply with the custom answer text.")

    async def _handle_user_input_submit(
        self,
        context: ChatContext,
        token: CallbackToken,
    ) -> None:
        request_id = _payload_int(token.payload, "request_id")
        pending = await self._host._repository.get_pending_user_input(context.chat_key)
        if pending is None or pending.request_id != request_id:
            await self._host._send_text(context, "This question is no longer pending.")
            return
        if not _user_input_complete(pending):
            await self._host._send_text(context, "Please answer every question first.")
            return
        await self._host._submit_user_input(context, pending)


class TelegramCallbackQueryRouter:
    """Consume callback tokens and route valid callback actions."""

    def __init__(
        self,
        repository: CallbackTokenRepository,
        *,
        enable_topic_sessions: bool,
        allowed: Callable[[int], bool],
        handle_action: CallbackActionHandler,
        send_runtime_error: Callable[[ChatContext, Exception], Awaitable[object]],
    ) -> None:
        self._repository = repository
        self._enable_topic_sessions = enable_topic_sessions
        self._allowed = allowed
        self._handle_action = handle_action
        self._send_runtime_error = send_runtime_error

    async def handle(self, callback: CallbackQuery) -> None:
        data = callback.data or ""
        if not data.startswith("ct:"):
            await callback.answer()
            return
        message = callback.message
        if message is None:
            await callback.answer(
                "This action is no longer available.", show_alert=True
            )
            return
        chat = getattr(message, "chat", None)
        chat_id = getattr(chat, "id", None)
        if not isinstance(chat_id, int) or not self._allowed(chat_id):
            await callback.answer("This action is not allowed.", show_alert=True)
            return
        topic_id = getattr(message, "message_thread_id", None)
        context = ChatContext(
            chat_key=build_chat_key(
                chat_id,
                topic_id,
                enable_topic_sessions=self._enable_topic_sessions,
            ),
            chat_id=chat_id,
            topic_id=topic_id,
        )
        clear_log_context()
        with log_context(
            chat_key=context.chat_key,
            chat_id=context.chat_id,
            topic_id=context.topic_id,
        ):
            try:
                token = await self._repository.consume_callback_token(
                    data.removeprefix("ct:"),
                    chat_key=context.chat_key,
                    topic_id=context.topic_id,
                )
                if token is None:
                    await callback.answer(
                        "This action expired. Run the listing again.",
                        show_alert=True,
                    )
                    return
                await self._handle_action(
                    context,
                    token,
                    message_id=getattr(message, "message_id", None),
                )
                await callback.answer()
            except Exception as err:
                log_exception(LOGGER, "telegram_callback_failed", err=err)
                await callback.answer("Action failed.", show_alert=True)
                await self._send_runtime_error(context, err)
        clear_log_context()


def _payload_str(payload: dict[str, object], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"Callback payload missing {key}.")
    return value.strip()


def _payload_int(payload: dict[str, object], key: str) -> int:
    value = payload.get(key)
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip().isdigit():
        return int(value.strip())
    raise ValueError(f"Callback payload missing {key}.")


def _payload_str_or_none(payload: dict[str, object], key: str) -> str | None:
    value = payload.get(key)
    if not isinstance(value, str) or not value.strip():
        return None
    return value.strip()


def _payload_bool(payload: dict[str, object], key: str) -> bool:
    return payload.get(key) is True
