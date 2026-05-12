"""Telegram long-polling bot adapter."""

from __future__ import annotations

import asyncio
import base64
import binascii
from collections.abc import AsyncIterator, Coroutine, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from html import escape
import json
from pathlib import Path
import re
from typing import Any, Literal, cast

from aiogram import Bot, Dispatcher
from aiogram.enums import ChatAction
from aiogram.types import (
    BufferedInputFile,
    CallbackQuery,
    ForceReply,
    FSInputFile,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    URLInputFile,
)

from codex_telegram.adapters.speech_to_text import SpeechToTextClient
from codex_telegram.adapters.telegram.attachments import (
    TelegramAttachmentDelivery,
    _attachment_is_photo,
    _validate_attachment_path,
)
from codex_telegram.adapters.telegram.callbacks import (
    TelegramCallbackActionExecutor,
    TelegramCallbackQueryRouter,
)
from codex_telegram.adapters.telegram.commands import (
    CodexThreadsCommand,
    CommandOptions,
    TelegramCommandExecutor,
    _parse_codex_threads_argument,
    _parse_command_options,
    _realtime_started_text,
    _split_project_command,
    _split_webhook_command,
    split_command,
)
from codex_telegram.adapters.telegram.errors import (
    _telegram_message_not_modified,
    _telegram_retry_after_seconds,
)
from codex_telegram.adapters.telegram.media_input import TelegramMediaInputResolver
from codex_telegram.adapters.telegram.rendering import (
    COMMAND_FAILURE_PREFIX,
    COMMAND_SUCCESS_PREFIX,
    TELEGRAM_MESSAGE_TEXT_LIMIT,
    TELEGRAM_TRUNCATION_NOTICE,
    WARNING_PREFIX,
    WRAPPED_CONVERSATION_NOTICE,
    _anchor_display_name,
    _anchor_is_running,
    _append_wrapped_notice,
    _coalesce_progress_text,
    _codex_thread_key,
    _count_phrase,
    _logical_thread_name,
    _public_error_summary,
    _short_line,
    _telegram_delivery_text,
    _user_input_complete,
    build_codex_thread_listing,
    build_recent_codex_thread_listing,
    overview_action_anchors,
    render_approval_request,
    render_codex_thread_attached,
    render_codex_threads,
    render_conversations,
    render_current_thread,
    render_directory_state,
    render_goal_status,
    render_help,
    render_history,
    render_mcp_servers,
    render_overview,
    render_project_state,
    render_settings,
    render_single_setting,
    render_skills,
    render_status,
    render_usage,
    render_user_input_request,
    render_webhook_created,
    render_webhooks,
    telegram_bot_commands,
)
from codex_telegram.adapters.telegram.routing import (
    ChatContext,
    build_chat_key,
    chat_context_from_key,
)
from codex_telegram.adapters.telegram.status_card import TelegramStatusCardSynchronizer
from codex_telegram.application.models import (
    BackendConnection,
    CodexRuntimeState,
    CodexThreadBackendFailure,
    ConversationAttachment,
    CodexThreadGroup,
    CodexThreadListResult,
    CurrentThreadState,
    DirectoryState,
    EffectiveSettings,
    ProjectState,
    ThreadHistory,
)
from codex_telegram.application.ports import ProgressMessageStore, StateRepository
from codex_telegram.application.service import (
    BotService,
    ThreadSelectionResult,
    TurnRunResult,
)
from codex_telegram.domain import (
    BridgeControlJob,
    CodexGoal,
    CodexThread,
    ConversationAnchor,
    LogicalThread,
    PendingApproval,
    PendingUserInput,
    RealtimeEvent,
    Project,
    RealtimeSession,
    TurnResult,
    TurnUpdate,
    ThreadMessage,
    UserInputQuestion,
    UserTurnInput,
    TurnResultImage,
    WebhookSubscription,
)
from codex_telegram.observability import (
    clear_log_context,
    get_logger,
    log_context,
    log_debug,
    log_error,
    log_exception,
    log_info,
    log_warning,
)

LOGGER = get_logger(__name__)

THOUGHT_BALLOON_PREFIX = "💭 "
FINAL_MESSAGE_PREFIX = "✅ "
CODEX_THREADS_FULL_LIMIT = 50
RECENT_PICKER_LIMIT = 5
EXPANDED_PICKER_LIMIT = 20
IDLE_WRAP_POLL_SECONDS = 30.0
RESUMING_PREVIOUS_CONVERSATION_NOTICE = "Resuming previous conversation..."


@dataclass(frozen=True, slots=True)
class _DeliveredFinal:
    message_id: int | None
    rendered_text: str


def _telegram_command_value(value: str) -> bool:
    return bool(value) and all(
        ("0" <= char <= "9")
        or ("A" <= char <= "Z")
        or ("a" <= char <= "z")
        or char == "_"
        for char in value
    )


class TelegramBotRunner:
    """Run the Telegram bot with direct Codex integration."""

    def __init__(
        self,
        bot: Bot,
        dispatcher: Dispatcher,
        service: BotService,
        repository: StateRepository,
        progress_store: ProgressMessageStore,
        allow_from: set[int] | None,
        enable_topic_sessions: bool,
        speech_client: SpeechToTextClient | None = None,
        webhook_public_base_url: str | None = None,
        webhook_local_base_url: str | None = None,
        attachment_roots: tuple[Path, ...] = (),
    ) -> None:
        self._bot = bot
        self._dispatcher = dispatcher
        self._service = service
        self._repository = repository
        self._progress_store = progress_store
        self._allow_from = allow_from
        self._enable_topic_sessions = enable_topic_sessions
        self._speech_client = speech_client
        self._webhook_public_base_url = webhook_public_base_url
        self._webhook_local_base_url = webhook_local_base_url
        self._background_tasks: set[asyncio.Task[None]] = set()
        self._delivery_backoff_until: dict[str, float] = {}
        self._attachment_delivery = TelegramAttachmentDelivery(
            self._bot,
            self._repository,
            self._send_text,
            allowed_roots=attachment_roots,
        )
        self._media_input = TelegramMediaInputResolver(
            self._bot,
            self._speech_client,
            self._send_text,
        )
        self._command_executor = TelegramCommandExecutor(self)
        self._status_cards = TelegramStatusCardSynchronizer(
            self._bot,
            self._service,
            self._progress_store,
            self._send_text,
            self._edit_text,
            self._callback_markup,
            self._conversation_focus_text_shortcuts,
        )
        self._callback_actions = TelegramCallbackActionExecutor(self)
        self._callback_router = TelegramCallbackQueryRouter(
            self._repository,
            enable_topic_sessions=self._enable_topic_sessions,
            allowed=self._allowed,
            handle_action=self._callback_actions.handle,
            send_runtime_error=self._send_runtime_error,
        )
        self._dispatcher.message()(self._on_message)
        self._dispatcher.callback_query()(self._on_callback_query)

    async def run(self) -> None:
        """Start long polling."""
        await self._register_commands()
        await self._notify_interrupted_threads()
        self._spawn_background(self._attachment_delivery.run_loop())
        self._spawn_background(self._bridge_control_loop())
        self._spawn_background(self._bridge_expiry_loop())
        await self._dispatcher.start_polling(self._bot)

    async def close(self) -> None:
        """Close background tasks and the bot transport."""
        for task in list(self._background_tasks):
            task.cancel()
        if self._background_tasks:
            await asyncio.gather(*self._background_tasks, return_exceptions=True)
        await self._bot.session.close()

    async def _on_message(self, message: Message) -> None:
        if not self._allowed(message.chat.id):
            return

        context = ChatContext(
            chat_key=build_chat_key(
                message.chat.id,
                message.message_thread_id,
                enable_topic_sessions=self._enable_topic_sessions,
            ),
            chat_id=message.chat.id,
            topic_id=message.message_thread_id,
        )
        clear_log_context()
        with log_context(
            chat_key=context.chat_key,
            chat_id=context.chat_id,
            topic_id=context.topic_id,
        ):
            try:
                turn_input = await self._resolve_message_input(message, context)
                if turn_input is None:
                    return
                display_text = turn_input.display_text()
                log_info(
                    LOGGER,
                    "telegram_message_received",
                    v={
                        "text_length": len(display_text),
                        "image_count": len(turn_input.images),
                    },
                )
                pending_user_input = await self._pending_user_input_for_chat(
                    context.chat_key
                )
                if pending_user_input is not None:
                    if pending_user_input.awaiting_free_text_question_id is not None:
                        await self._handle_user_input_free_text(
                            context,
                            pending_user_input,
                            turn_input.text,
                        )
                        return
                    await self._send_text(
                        context,
                        "Please answer the pending Codex question using the buttons.",
                    )
                    return

                reply_target = await self._reply_target_thread(context, message)
                turn_thread_id: str | None = None
                thread = await self._service.ensure_active_thread(context.chat_key)
                if reply_target is not None:
                    selected = await self._service.resolve_bridge(
                        context.chat_key,
                        reply_target,
                        focus=False,
                    )
                    if selected.success:
                        thread = (
                            selected.thread
                            if selected.thread is not None
                            else await self._service.ensure_active_thread(
                                context.chat_key
                            )
                        )
                        turn_thread_id = thread.thread_id
                        await self._send_text(
                            context,
                            RESUMING_PREVIOUS_CONVERSATION_NOTICE,
                        )
                    else:
                        log_warning(
                            LOGGER,
                            "telegram_reply_resume_target_missing",
                            v={"thread_id": reply_target},
                        )

                one_shot_reply_target = await self._one_shot_reply_target_thread(
                    context, message
                )
                if one_shot_reply_target is not None:
                    selected = await self._service.resolve_bridge(
                        context.chat_key,
                        one_shot_reply_target,
                        focus=False,
                    )
                    if selected.success and selected.thread is not None:
                        thread = selected.thread
                        turn_thread_id = thread.thread_id
                    else:
                        await self._send_text(context, selected.message)
                        return

                interrupted = await self._service.take_interrupted_notice(
                    thread.thread_id
                )
                if interrupted:
                    await self._send_text(
                        context,
                        WARNING_PREFIX
                        + "The previous Codex request was interrupted by a restart.",
                    )

                command = split_command(turn_input.text)
                if command is not None:
                    handled = await self._handle_command(
                        context, thread.thread_id, command, turn_input.text
                    )
                    if handled:
                        log_info(
                            LOGGER,
                            "telegram_command_handled",
                            v={"command": command[0]},
                        )
                        return

                try:
                    routed_realtime = await self._service.route_realtime_input(
                        context.chat_key,
                        turn_input,
                    )
                    if routed_realtime is True:
                        return
                except ValueError as exc:
                    await self._send_text(context, COMMAND_FAILURE_PREFIX + str(exc))
                    return

                if turn_input.text:
                    await self._service.apply_implementation_trigger_if_needed(
                        context.chat_key,
                        turn_input.text,
                    )

                async with self._typing_loop(context, thread_id=thread.thread_id):
                    run_result = await self._service.run_turn(
                        context.chat_key,
                        turn_input,
                        thread_id=turn_thread_id,
                        on_update=lambda update: self._handle_turn_update(
                            context, update
                        ),
                        on_wait_notice=lambda: self._handle_wait_notice(context),
                        on_state_change=lambda: self._handle_turn_state_change(context),
                    )
                await self._finish_run_result(context, run_result)
            except Exception as err:
                log_exception(LOGGER, "telegram_message_handling_failed", err=err)
                await self._send_runtime_error(context, err)

        clear_log_context()

    async def _on_callback_query(self, callback: CallbackQuery) -> None:
        await self._callback_router.handle(callback)

    async def _handle_command(
        self,
        context: ChatContext,
        thread_id: str,
        command: tuple[str, str],
        raw_text: str,
    ) -> bool:
        return await self._command_executor.handle(
            context,
            thread_id,
            command,
            raw_text,
        )

    async def _watch_turn_after_approval(
        self, context: ChatContext, pending: PendingApproval
    ) -> None:
        await self._progress_store.clear_progress(pending.logical_thread_id)
        with log_context(
            chat_key=context.chat_key,
            chat_id=context.chat_id,
            topic_id=context.topic_id,
            request_id=pending.request_id,
            turn_id=pending.turn_id,
        ):
            try:
                async with self._typing_loop(
                    context,
                    thread_id=pending.logical_thread_id,
                ):
                    result = await self._service.continue_turn(
                        context.chat_key,
                        pending.logical_thread_id,
                        pending.turn_id or "",
                        on_update=lambda update: self._handle_turn_update(
                            context, update
                        ),
                        on_wait_notice=lambda: self._handle_wait_notice(context),
                        on_state_change=lambda: self._handle_turn_state_change(context),
                    )
                if result.status == "approvalRequired":
                    newer = await self._service.pending_request_for_chat(
                        context.chat_key
                    )
                    if newer is not None:
                        await self._send_approval_request(context, newer)
                    return
                if result.status == "userInputRequired":
                    newer_input = await self._pending_user_input_for_chat(
                        context.chat_key
                    )
                    if newer_input is not None:
                        await self._send_user_input_request(context, newer_input)
                    return
                await self._send_terminal_result(context, result)
            except Exception as err:
                log_exception(LOGGER, "telegram_approved_turn_failed", err=err)
                await self._send_runtime_error(context, err)

    async def _watch_turn_after_user_input(
        self, context: ChatContext, pending: PendingUserInput
    ) -> None:
        await self._progress_store.clear_progress(pending.logical_thread_id)
        with log_context(
            chat_key=context.chat_key,
            chat_id=context.chat_id,
            topic_id=context.topic_id,
            request_id=pending.request_id,
            turn_id=pending.turn_id,
        ):
            try:
                async with self._typing_loop(
                    context,
                    thread_id=pending.logical_thread_id,
                ):
                    result = await self._service.continue_turn(
                        context.chat_key,
                        pending.logical_thread_id,
                        pending.turn_id or "",
                        on_update=lambda update: self._handle_turn_update(
                            context, update
                        ),
                        on_wait_notice=lambda: self._handle_wait_notice(context),
                        on_state_change=lambda: self._handle_turn_state_change(context),
                    )
                if result.status == "approvalRequired":
                    newer_approval = await self._service.pending_request_for_chat(
                        context.chat_key
                    )
                    if newer_approval is not None:
                        await self._send_approval_request(context, newer_approval)
                    return
                if result.status == "userInputRequired":
                    newer_input = await self._pending_user_input_for_chat(
                        context.chat_key
                    )
                    if newer_input is not None:
                        await self._send_user_input_request(context, newer_input)
                    return
                await self._send_terminal_result(context, result)
            except Exception as err:
                log_exception(LOGGER, "telegram_user_input_turn_failed", err=err)
                await self._send_runtime_error(context, err)

    async def run_webhook_event(
        self,
        subscription: WebhookSubscription,
        prompt: str,
    ) -> None:
        """Run one externally triggered event through the normal turn flow."""
        chat_key = subscription.chat_key
        context = chat_context_from_key(chat_key)
        try:
            run_result = await self._service.run_webhook_turn(
                subscription,
                prompt,
                on_update=lambda update: self._handle_turn_update(context, update),
                on_wait_notice=lambda: self._handle_wait_notice(context),
                on_state_change=lambda: self._handle_turn_state_change(context),
            )
            await self._finish_run_result(context, run_result)
        except Exception as err:
            log_exception(
                LOGGER,
                "telegram_webhook_turn_failed",
                err=err,
                chat_key=chat_key,
                anchor_id=subscription.anchor_id,
                webhook_id=subscription.webhook_id,
            )
            await self._send_runtime_error(context, err)

    async def run_bridge_command(self, thread_id: str, text: str) -> dict[str, object]:
        """Execute a bridge API command through the Telegram command path."""
        thread = await self._repository.get_thread(thread_id)
        if thread is None:
            raise ValueError(f"Unknown bridge: {thread_id}")
        snapshot = await self._service.bridge_snapshot(thread_id)
        if not snapshot.active:
            raise ValueError(
                "Bridge is expired. Use anchor_id for durable flows, or refresh/focus "
                "the conversation from Telegram before bridge control."
            )
        context = chat_context_from_key(thread.chat_key)
        stripped = text.strip()
        command = split_command(stripped)
        if command is not None:
            name, argument = command
            if _bridge_command_requires_telegram_picker(name, argument):
                raise ValueError(
                    f"/{name} requires Telegram picker UI unless fully specified."
                )
            await self._handle_command(context, thread.thread_id, command, stripped)
            return {"accepted": True, "thread_id": thread.thread_id}
        async with self._typing_loop(context, thread_id=thread.thread_id):
            run_result = await self._service.run_turn(
                context.chat_key,
                stripped,
                thread_id=thread.thread_id,
                on_update=lambda update: self._handle_turn_update(context, update),
                on_wait_notice=lambda: self._handle_wait_notice(context),
                on_state_change=lambda: self._handle_turn_state_change(context),
            )
        await self._finish_run_result(context, run_result)
        return {"accepted": True, "thread_id": thread.thread_id}

    async def enqueue_bridge_control(
        self,
        thread_id: str,
        action: str,
        payload: dict[str, object],
    ) -> dict[str, object]:
        """Queue a non-command Telegram bridge side effect."""
        job = await self._service.enqueue_bridge_control_job(
            thread_id,
            action,
            payload,
        )
        return {
            "accepted": True,
            "job_id": job.job_id,
            "status": job.status,
            "thread_id": job.logical_thread_id,
        }

    async def _handle_user_input_free_text(
        self,
        context: ChatContext,
        pending: PendingUserInput,
        text: str,
    ) -> None:
        answer = text.strip()
        if not answer:
            await self._send_text(context, "Please reply with text for this answer.")
            return
        question_id = pending.awaiting_free_text_question_id
        if question_id is None:
            return
        await self._repository.update_pending_user_input_selection(
            pending.request_id,
            question_id=question_id,
            answers=(answer,),
            awaiting_free_text=False,
        )
        updated = await self._repository.get_pending_user_input(context.chat_key)
        if updated is None:
            return
        if _user_input_complete(updated):
            await self._submit_user_input(context, updated)
            return
        await self._send_user_input_request(context, updated)

    async def _submit_user_input(
        self,
        context: ChatContext,
        pending: PendingUserInput,
    ) -> None:
        response = await self._service.resolve_pending_user_input(
            pending.request_id,
            pending.selected_answers,
        )
        await self._send_text(context, response)
        self._spawn_background(self._watch_turn_after_user_input(context, pending))

    async def _refresh_user_input_message(
        self,
        context: ChatContext,
        pending: PendingUserInput,
        *,
        message_id: int | None,
    ) -> None:
        text = render_user_input_request(pending)
        markup = await self._user_input_markup(context, pending)
        if message_id is None:
            await self._send_text(context, text, reply_markup=markup, parse_mode="HTML")
            return
        try:
            await self._edit_text(
                context,
                message_id=message_id,
                text=text,
                reply_markup=markup,
                parse_mode="HTML",
            )
        except Exception as err:
            if not _telegram_message_not_modified(err):
                raise

    async def _pending_user_input_for_chat(
        self, chat_key: str
    ) -> PendingUserInput | None:
        pending = await self._service.pending_user_input_for_chat(chat_key)
        return pending if isinstance(pending, PendingUserInput) else None

    async def _handle_text_shortcut(self, context: ChatContext, token: str) -> None:
        callback_token = await self._repository.consume_callback_token(
            token,
            chat_key=context.chat_key,
            topic_id=context.topic_id,
        )
        if callback_token is None:
            result = await self._focus_bridge(context, token)
            if result.success and result.thread is not None:
                await self._send_text(context, result.message)
                await self._send_focus_final_messages(
                    context,
                    result.thread.thread_id,
                )
                await self._sync_thread_status_card(context)
                return
            await self._send_text(
                context,
                COMMAND_FAILURE_PREFIX
                + "This thread shortcut expired. Use /threads again.",
            )
            return
        await self._callback_actions.handle(context, callback_token)

    async def _focus_bridge(
        self,
        context: ChatContext,
        selector: str,
    ) -> ThreadSelectionResult:
        previous = await self._service.ensure_focused_bridge(context.chat_key)
        previous_thread_id = _presentation_thread_id(previous)
        result = await self._service.focus_bridge(context.chat_key, selector)
        next_thread_id = (
            _presentation_thread_id(result.thread)
            if result.success and result.thread is not None
            else None
        )
        if (
            result.success
            and previous_thread_id is not None
            and previous_thread_id != next_thread_id
        ):
            await self._add_background_actions_to_final_message(
                context,
                previous_thread_id,
            )
        return result

    async def _add_background_actions_to_final_message(
        self,
        context: ChatContext,
        thread_id: str,
    ) -> None:
        final = await self._progress_store.get_final_message(thread_id)
        if final is None or not isinstance(final.message_id, int):
            return
        markup = await self._background_final_markup(context, thread_id)
        try:
            await self._edit_message_reply_markup_with_retry(
                context,
                message_id=final.message_id,
                reply_markup=markup,
                logical_thread_id=thread_id,
                event_name="telegram_background_actions_edit_throttled",
            )
        except Exception as err:
            if not _telegram_message_not_modified(err):
                raise

    async def _handle_turn_update(
        self, context: ChatContext, update: TurnUpdate
    ) -> None:
        if not update.visible:
            await self._sync_thread_status_card(context)
            return
        if not await self._thread_is_focused(context, update.logical_thread_id):
            await self._sync_thread_status_card(context)
            return
        progress = await self._progress_store.get_progress(update.logical_thread_id)
        rendered_text = _coalesce_progress_text(
            update.text,
            None if progress is None else progress.rendered_text,
        )
        if rendered_text is None:
            return
        prefixed = _rich_progress_text(rendered_text)
        log_debug(
            LOGGER,
            "telegram_turn_update_rendered",
            turn_id=update.turn_id,
            logical_thread_id=update.logical_thread_id,
            v={
                "source": update.source,
                "text_length": len(rendered_text),
            },
        )
        if self._delivery_backoff_active(context):
            return
        if progress is None or progress.message_id is None:
            await self._sync_thread_status_card(context)
            sent = await self._send_noncritical_text(
                context,
                prefixed,
                parse_mode="HTML",
                turn_id=update.turn_id,
                logical_thread_id=update.logical_thread_id,
                event_name="telegram_progress_update_throttled",
            )
            if sent is None:
                return
            await self._progress_store.save_progress(
                update.logical_thread_id,
                message_id=sent.message_id,
                rendered_text=rendered_text,
            )
            return
        if progress.rendered_text == rendered_text:
            return
        try:
            await self._edit_text(
                context,
                message_id=progress.message_id,
                text=prefixed,
                parse_mode="HTML",
            )
        except Exception as err:
            if _telegram_message_not_modified(err):
                await self._progress_store.save_progress(
                    update.logical_thread_id,
                    rendered_text=rendered_text,
                )
                return
            if await self._note_delivery_throttle(
                context,
                err,
                turn_id=update.turn_id,
                logical_thread_id=update.logical_thread_id,
                event_name="telegram_progress_update_throttled",
            ):
                return
            sent = await self._send_noncritical_text(
                context,
                prefixed,
                parse_mode="HTML",
                turn_id=update.turn_id,
                logical_thread_id=update.logical_thread_id,
                event_name="telegram_progress_update_throttled",
            )
            if sent is None:
                return
            await self._progress_store.save_progress(
                update.logical_thread_id,
                message_id=sent.message_id,
                rendered_text=rendered_text,
            )
            return
        self._clear_delivery_backoff(context)
        await self._progress_store.save_progress(
            update.logical_thread_id,
            rendered_text=rendered_text,
        )

    async def _handle_turn_state_change(self, context: ChatContext) -> None:
        await self._sync_thread_status_card(context)

    async def _handle_wait_notice(self, context: ChatContext) -> None:
        log_info(LOGGER, "telegram_turn_wait_notice")
        await self._sync_thread_status_card(context)
        await self._send_noncritical_text(
            context,
            THOUGHT_BALLOON_PREFIX + await self._wait_notice_text(context),
            event_name="telegram_wait_notice_throttled",
        )

    async def _new_conversation_notice(
        self,
        context: ChatContext,
        thread: LogicalThread,
    ) -> str:
        project_state = await self._service.show_project_state(context.chat_key)
        active_project = getattr(project_state, "active", None)
        lines = [
            f"Started new conversation {_logical_thread_name(thread)}.",
            f"Connection: {thread.codex_backend_id}",
        ]
        if isinstance(active_project, Project):
            lines.append(
                f"Project: {active_project.label} ({active_project.root_path})"
            )
        else:
            lines.append("Project: (none)")
        return "\n".join(lines)

    async def _wait_notice_text(self, context: ChatContext) -> str:
        conversations = await self._service.list_conversations(context.chat_key)
        focused = await self._service.ensure_focused_bridge(context.chat_key)
        if not isinstance(conversations, list):
            return "Codex is still working. Running conversations: unknown."
        focused_running: set[str] = set()
        background_running: set[str] = set()
        for anchor in conversations:
            bridge_id = anchor.latest_bridge_id
            if bridge_id is None or not _anchor_is_running(anchor):
                continue
            if bridge_id == focused.bridge_id:
                focused_running.add(bridge_id)
            else:
                background_running.add(bridge_id)
        parts: list[str] = []
        if focused_running:
            parts.append(_count_phrase(len(focused_running), "focused"))
        if background_running:
            parts.append(_count_phrase(len(background_running), "background"))
        if not parts:
            parts.append("none visible")
        return f"Codex is still working. Running conversations: {', '.join(parts)}."

    async def _consume_realtime_events(
        self,
        context: ChatContext,
        logical_thread_id: str,
        *,
        announce_started: bool = False,
    ) -> None:
        while True:
            try:
                event = await self._service.wait_for_realtime_event(
                    logical_thread_id,
                    timeout=30.0,
                )
            except TimeoutError:
                continue
            except ValueError:
                return
            except Exception as err:
                log_exception(LOGGER, "telegram_realtime_event_loop_failed", err=err)
                await self._send_runtime_error(context, err)
                return
            if not isinstance(event, RealtimeEvent):
                log_warning(
                    LOGGER,
                    "telegram_realtime_event_invalid",
                    logical_thread_id=logical_thread_id,
                    v={"event_type": type(event).__name__},
                )
                return
            if event.event_type == "started":
                if announce_started:
                    await self._send_text(context, _realtime_started_text())
                    announce_started = False
                continue
            if event.event_type in {"transcript_delta", "transcript_done"}:
                await self._handle_turn_update(
                    context,
                    TurnUpdate(
                        turn_id=f"realtime:{event.codex_thread_id}",
                        chat_key=event.chat_key,
                        logical_thread_id=event.logical_thread_id,
                        codex_thread_id=event.codex_thread_id,
                        codex_backend_id=event.codex_backend_id,
                        status="inProgress",
                        source=f"thread/realtime/{event.event_type}",
                        text=event.text,
                        visible=True,
                    ),
                )
                continue
            if event.event_type == "error":
                await self._send_text(
                    context,
                    COMMAND_FAILURE_PREFIX
                    + "Realtime failed: "
                    + _short_line(event.reason or event.text or "unknown error"),
                )
                return
            if event.event_type == "closed":
                await self._send_noncritical_text(
                    context,
                    THOUGHT_BALLOON_PREFIX + "Realtime mode stopped.",
                    event_name="telegram_realtime_closed_throttled",
                )
                return

    async def _finish_run_result(
        self,
        context: ChatContext,
        run_result: TurnRunResult,
    ) -> None:
        if run_result.remap_warning:
            await self._send_text(context, WARNING_PREFIX + run_result.remap_warning)
        if run_result.active_turn_notice:
            await self._send_text(
                context,
                THOUGHT_BALLOON_PREFIX + run_result.active_turn_notice,
            )
        if run_result.active_turn_continues or run_result.result is None:
            return
        if run_result.result.status == "approvalRequired":
            pending = await self._service.pending_request_for_chat(context.chat_key)
            if pending is not None:
                await self._send_approval_request(context, pending)
            return
        if run_result.result.status == "userInputRequired":
            pending_input = await self._pending_user_input_for_chat(context.chat_key)
            if pending_input is not None:
                await self._send_user_input_request(context, pending_input)
            return
        await self._send_terminal_result(context, run_result.result)

    async def _send_terminal_result(
        self,
        context: ChatContext,
        result: TurnResult,
    ) -> None:
        plan_body = await self._plan_proposal_body(context, result)
        if plan_body is not None:
            await self._send_plan_proposal(context, result, plan_body)
            return
        await self._send_final(context, result)

    async def _plan_proposal_body(
        self,
        context: ChatContext,
        result: TurnResult,
    ) -> str | None:
        if result.status != "completed" or result.error:
            return None
        settings = await self._service.get_settings(
            result.logical_thread_id,
            context.chat_key,
        )
        if not (
            isinstance(settings, EffectiveSettings)
            and settings.collaboration_mode == "plan"
        ):
            return None
        thread = await self._repository.get_thread(result.logical_thread_id)
        if thread is None:
            thread = LogicalThread(
                thread_id=result.logical_thread_id,
                chat_key=result.chat_key,
                title="",
                codex_thread_id=result.codex_thread_id,
                created_at="",
                updated_at="",
                turn_count=0,
                awaiting_reply=False,
                interrupted_notice=False,
                pending_turn_id=None,
                codex_backend_id=result.codex_backend_id,
            )
        runtime = await self._service.runtime_state_for_thread(thread)
        if isinstance(runtime, CodexRuntimeState) and runtime.plan_items:
            return _plan_items_text(runtime)
        if result.final_text and _looks_like_plan_text(result.final_text):
            return result.final_text
        return None

    async def _result_is_plan_proposal(
        self,
        context: ChatContext,
        result: TurnResult,
    ) -> bool:
        return await self._plan_proposal_body(context, result) is not None

    async def _send_plan_proposal(
        self,
        context: ChatContext,
        result: TurnResult,
        plan_body: str,
    ) -> None:
        chunks = _rich_final_chunks(
            "Proposed plan",
            plan_body,
            html_header=True,
        )
        markup = await self._plan_proposal_markup(context, result.logical_thread_id)
        progress = await self._progress_store.get_progress(result.logical_thread_id)
        message_id: int | None
        if progress is not None and isinstance(progress.message_id, int) and chunks:
            remaining = list(chunks)
            first = remaining.pop(0)
            await self._edit_message_text_with_retry(
                context,
                message_id=progress.message_id,
                text=first,
                parse_mode="HTML",
                reply_markup=markup if not remaining else None,
                turn_id=result.turn_id,
                logical_thread_id=result.logical_thread_id,
                event_name="telegram_plan_proposal_throttled",
            )
            message_id = progress.message_id
            if remaining:
                sent_message_id = await self._send_chunked_text_with_retry(
                    context,
                    remaining,
                    parse_mode="HTML",
                    reply_markup=markup,
                    turn_id=result.turn_id,
                    logical_thread_id=result.logical_thread_id,
                    event_name="telegram_plan_proposal_throttled",
                )
                if isinstance(sent_message_id, int):
                    message_id = sent_message_id
        else:
            message_id = await self._send_chunked_text_with_retry(
                context,
                chunks,
                parse_mode="HTML",
                reply_markup=markup,
                turn_id=result.turn_id,
                logical_thread_id=result.logical_thread_id,
                event_name="telegram_plan_proposal_throttled",
            )
        await self._progress_store.clear_progress(result.logical_thread_id)
        if isinstance(message_id, int):
            await self._progress_store.save_final_message(
                result.logical_thread_id,
                chat_key=context.chat_key,
                message_id=message_id,
                rendered_text="\n".join(chunks),
            )
            await self._service.mark_thread_messages_delivered(
                context.chat_key,
                result.logical_thread_id,
            )
        await self._send_result_images(context, result.images)
        await self._sync_thread_status_card(context)

    async def _send_approval_request(
        self, context: ChatContext, pending: PendingApproval
    ) -> None:
        await self._progress_store.clear_progress(pending.logical_thread_id)
        await self._send_text(
            context,
            render_approval_request(pending),
            reply_markup=await self._approval_markup(context, pending),
            parse_mode="HTML",
        )
        await self._sync_thread_status_card(context)

    async def _send_user_input_request(
        self,
        context: ChatContext,
        pending: PendingUserInput,
    ) -> None:
        await self._progress_store.clear_progress(pending.logical_thread_id)
        await self._send_text(
            context,
            render_user_input_request(pending),
            reply_markup=await self._user_input_markup(context, pending),
            parse_mode="HTML",
        )
        await self._sync_thread_status_card(context)

    async def _handle_thread_wrapped(
        self,
        context: ChatContext,
        thread_id: str,
        notice: str,
    ) -> None:
        del notice
        final = await self._progress_store.get_final_message(thread_id)
        if final is None:
            log_warning(
                LOGGER,
                "telegram_thread_wrap_final_message_missing",
                logical_thread_id=thread_id,
                v={},
            )
            return
        wrapped_text = _append_wrapped_notice(final.rendered_text)
        if wrapped_text == final.rendered_text:
            return
        await self._edit_message_text_with_retry(
            context,
            message_id=final.message_id,
            text=wrapped_text,
            parse_mode="HTML",
            logical_thread_id=thread_id,
            event_name="telegram_thread_wrap_edit_throttled",
        )
        await self._progress_store.save_final_message(
            thread_id,
            chat_key=context.chat_key,
            message_id=final.message_id,
            rendered_text=wrapped_text,
        )

    async def _send_final(self, context: ChatContext, result: TurnResult) -> None:
        value: dict[str, object] = {"status": result.status}
        if result.error is not None:
            value["error"] = {"message": result.error}
        else:
            value["final_text_length"] = len(result.final_text)
        log_info(
            LOGGER,
            "telegram_final_response_sending",
            turn_id=result.turn_id,
            logical_thread_id=result.logical_thread_id,
            v=value,
        )
        if not result.error and not await self._thread_is_focused(
            context, result.logical_thread_id
        ):
            background_delivery = await self._send_background_final(context, result)
            await self._send_result_images(context, result.images)
            if isinstance(background_delivery.message_id, int):
                await self._progress_store.save_final_message(
                    result.logical_thread_id,
                    chat_key=context.chat_key,
                    message_id=background_delivery.message_id,
                    rendered_text=background_delivery.rendered_text,
                )
                await self._service.mark_thread_messages_delivered(
                    context.chat_key,
                    result.logical_thread_id,
                )
            await self._progress_store.clear_progress(result.logical_thread_id)
            await self._sync_thread_status_card(context)
            return
        progress = await self._progress_store.get_progress(result.logical_thread_id)
        final_text = COMMAND_FAILURE_PREFIX + (result.error or "Turn failed.")
        final_message_id: int | None = None
        delivered_rendered_text: str | None = None
        if result.error and progress is not None and progress.message_id is not None:
            try:
                await self._edit_message_text_with_retry(
                    context,
                    message_id=progress.message_id,
                    text=final_text,
                    turn_id=result.turn_id,
                    logical_thread_id=result.logical_thread_id,
                    event_name="telegram_final_response_throttled",
                )
                final_message_id = progress.message_id
            except Exception as err:
                if _telegram_message_not_modified(err):
                    final_message_id = progress.message_id
                    await self._progress_store.save_final_message(
                        result.logical_thread_id,
                        chat_key=context.chat_key,
                        message_id=final_message_id,
                        rendered_text=final_text,
                    )
                    await self._progress_store.clear_progress(result.logical_thread_id)
                    await self._sync_thread_status_card(context)
                    return
                sent = await self._send_text_with_retry(
                    context,
                    final_text,
                    turn_id=result.turn_id,
                    logical_thread_id=result.logical_thread_id,
                    event_name="telegram_final_response_throttled",
                )
                final_message_id = sent.message_id
        else:
            delivery = await self._send_plain_final(
                context,
                result,
                progress_message_id=(
                    progress.message_id if progress is not None else None
                ),
            )
            final_message_id = delivery.message_id
            delivered_rendered_text = delivery.rendered_text
            await self._send_result_images(context, result.images)
        if isinstance(final_message_id, int):
            await self._progress_store.save_final_message(
                result.logical_thread_id,
                chat_key=context.chat_key,
                message_id=final_message_id,
                rendered_text=(
                    final_text
                    if result.error
                    else delivered_rendered_text
                    or _rich_final_rendered_text(
                        FINAL_MESSAGE_PREFIX,
                        result.final_text or "(no reply text)",
                    )
                ),
            )
            await self._service.mark_thread_messages_delivered(
                context.chat_key,
                result.logical_thread_id,
            )
        await self._progress_store.clear_progress(result.logical_thread_id)
        await self._sync_thread_status_card(context)

    async def _send_plain_final(
        self,
        context: ChatContext,
        result: TurnResult,
        *,
        progress_message_id: int | None,
    ) -> _DeliveredFinal:
        chunks = _rich_final_chunks(
            FINAL_MESSAGE_PREFIX,
            result.final_text or "(no reply text)",
        )
        last_message_id: int | None = None
        remaining = list(chunks)
        if progress_message_id is not None and remaining:
            first = remaining.pop(0)
            await self._edit_message_text_with_retry(
                context,
                message_id=progress_message_id,
                text=first,
                turn_id=result.turn_id,
                logical_thread_id=result.logical_thread_id,
                event_name="telegram_final_response_throttled",
                parse_mode="HTML",
            )
            last_message_id = progress_message_id
        for chunk in remaining:
            sent = await self._send_text_with_retry(
                context,
                chunk,
                turn_id=result.turn_id,
                logical_thread_id=result.logical_thread_id,
                event_name="telegram_final_response_throttled",
                parse_mode="HTML",
            )
            last_message_id = sent.message_id
        return _DeliveredFinal(
            message_id=last_message_id,
            rendered_text="\n".join(chunks),
        )

    async def _send_plain_final_message(
        self,
        context: ChatContext,
        final_text: str,
    ) -> int | None:
        last_message_id: int | None = None
        for chunk in _rich_final_chunks(
            FINAL_MESSAGE_PREFIX,
            final_text or "(no reply text)",
        ):
            sent = await self._send_text_with_retry(
                context,
                chunk,
                parse_mode="HTML",
                event_name="telegram_final_response_throttled",
            )
            last_message_id = sent.message_id
        return last_message_id

    async def _send_result_images(
        self,
        context: ChatContext,
        images: tuple[TurnResultImage, ...],
    ) -> None:
        for image in images:
            photo = _telegram_image_input(image)
            await self._bot.send_photo(
                chat_id=context.chat_id,
                photo=photo,
                caption=image.caption,
                message_thread_id=context.topic_id,
            )

    async def _send_background_final(
        self,
        context: ChatContext,
        result: TurnResult,
    ) -> _DeliveredFinal:
        title = await self._thread_title(result.logical_thread_id)
        markup = await self._background_final_markup(
            context,
            result.logical_thread_id,
        )
        chunks = _rich_final_chunks(
            f"📬 From: {title}",
            result.final_text or "(no reply text)",
            html_header=True,
        )
        message_id = await self._send_chunked_text_with_retry(
            context,
            chunks,
            parse_mode="HTML",
            reply_markup=markup,
            turn_id=result.turn_id,
            logical_thread_id=result.logical_thread_id,
            event_name="telegram_final_response_throttled",
        )
        return _DeliveredFinal(
            message_id=message_id,
            rendered_text="\n".join(chunks),
        )

    async def _run_plan_implementation(
        self,
        context: ChatContext,
        thread_id: str,
    ) -> None:
        async with self._typing_loop(context, thread_id=thread_id):
            run_result = await self._service.start_plan_implementation(
                context.chat_key,
                thread_id,
                on_update=lambda update: self._handle_turn_update(context, update),
                on_wait_notice=lambda: self._handle_wait_notice(context),
                on_state_change=lambda: self._handle_turn_state_change(context),
            )
        await self._finish_run_result(context, run_result)

    async def _send_focus_repeat_final(
        self,
        context: ChatContext,
        message: ThreadMessage,
    ) -> int | None:
        title = await self._thread_title(message.thread_id)
        chunks = _rich_final_chunks(
            f"🔁 From: {title}",
            message.text,
            html_header=True,
        )
        return await self._send_chunked_text_with_retry(
            context,
            chunks,
            parse_mode="HTML",
            event_name="telegram_final_response_throttled",
        )

    async def _send_chunked_text_with_retry(
        self,
        context: ChatContext,
        chunks: list[str],
        *,
        parse_mode: str | None = None,
        reply_markup: InlineKeyboardMarkup | None = None,
        turn_id: str | None = None,
        logical_thread_id: str | None = None,
        event_name: str,
    ) -> int | None:
        last_message_id: int | None = None
        for index, chunk in enumerate(chunks):
            sent = await self._send_text_with_retry(
                context,
                chunk,
                reply_markup=reply_markup if index == len(chunks) - 1 else None,
                parse_mode=parse_mode,
                turn_id=turn_id,
                logical_thread_id=logical_thread_id,
                event_name=event_name,
            )
            last_message_id = sent.message_id
        return last_message_id

    async def _background_final_markup(
        self,
        context: ChatContext,
        thread_id: str,
    ) -> InlineKeyboardMarkup | None:
        thread = await self._repository.get_thread(thread_id)
        selector = (
            thread.anchor_id if thread is not None and thread.anchor_id else thread_id
        )
        return await self._callback_markup(
            context,
            [
                [
                    ("Reply", "reply_conversation", {"thread_id": thread_id}),
                    ("Focus", "focus_conversation", {"selector": selector}),
                ]
            ],
        )

    async def _thread_is_focused(self, context: ChatContext, thread_id: str) -> bool:
        focused = await self._service.ensure_focused_bridge(context.chat_key)
        focused_id = getattr(focused, "bridge_id", None)
        if not isinstance(focused_id, str):
            return True
        return focused_id == thread_id

    async def _thread_title(self, thread_id: str) -> str:
        thread = await self._repository.get_thread(thread_id)
        if thread is None:
            return "Conversation"
        return _logical_thread_name(thread)

    async def _send_focus_final_messages(
        self,
        context: ChatContext,
        thread_id: str,
    ) -> None:
        entries = await self._service.focus_final_messages(
            context.chat_key,
            thread_id,
        )
        if not isinstance(entries, list) or not entries:
            return
        for entry in entries:
            repeated = bool(getattr(entry, "repeated", False))
            message = getattr(entry, "message", None)
            if not isinstance(message, ThreadMessage):
                continue
            if message.kind == "final_image":
                image = _thread_message_image(message)
                if image is not None:
                    await self._send_result_images(context, (image,))
                continue
            if repeated:
                await self._send_focus_repeat_final(context, message)
            else:
                await self._send_plain_final_message(context, message.text)
        await self._service.mark_thread_messages_delivered(
            context.chat_key,
            thread_id,
        )

    async def _send_history_final_messages(
        self,
        context: ChatContext,
        limit: int,
    ) -> None:
        history = await self._service.thread_final_history(context.chat_key, limit)
        if not history.entries:
            title = _logical_thread_name(history.thread)
            await self._send_text(
                context,
                f"<b>History</b>\nNo saved final messages for {escape(title)} yet.",
                parse_mode="HTML",
            )
            return
        for message in history.entries:
            if message.kind == "final_image":
                image = _thread_message_image(message)
                if image is not None:
                    await self._send_result_images(context, (image,))
                continue
            await self._send_focus_repeat_final(context, message)

    async def _send_text(
        self,
        context: ChatContext,
        text: str,
        *,
        reply_markup: InlineKeyboardMarkup | None = None,
        parse_mode: str | None = None,
    ) -> Message:
        text, parse_mode = _telegram_delivery_text(text, parse_mode=parse_mode)
        kwargs: dict[str, Any] = {
            "chat_id": context.chat_id,
            "text": text,
            "message_thread_id": context.topic_id,
        }
        if reply_markup is not None:
            kwargs["reply_markup"] = reply_markup
        if parse_mode is not None:
            kwargs["parse_mode"] = parse_mode
        return await self._bot.send_message(**kwargs)  # type: ignore[arg-type]

    async def _edit_text(
        self,
        context: ChatContext,
        *,
        message_id: int,
        text: str,
        reply_markup: InlineKeyboardMarkup | None = None,
        parse_mode: str | None = None,
    ) -> None:
        text, parse_mode = _telegram_delivery_text(text, parse_mode=parse_mode)
        kwargs: dict[str, Any] = {
            "chat_id": context.chat_id,
            "message_id": message_id,
            "text": text,
        }
        if reply_markup is not None:
            kwargs["reply_markup"] = reply_markup
        if parse_mode is not None:
            kwargs["parse_mode"] = parse_mode
        await self._bot.edit_message_text(**kwargs)

    async def _sync_thread_status_card(self, context: ChatContext) -> None:
        await self._status_cards.sync_thread(context)

    async def _show_status_card(self, context: ChatContext) -> None:
        await self._status_cards.show(context)

    async def _anchors_markup(
        self,
        context: ChatContext,
        anchors: list[ConversationAnchor],
    ) -> InlineKeyboardMarkup | None:
        rows: list[list[tuple[str, str, dict[str, object]]]] = [
            [
                (
                    "Focus " + _short_line(_anchor_display_name(anchor), limit=32),
                    "focus_conversation",
                    {"anchor_id": anchor.anchor_id},
                )
            ]
            for anchor in anchors
        ]
        return await self._callback_markup(context, rows)

    async def _codex_threads_markup(
        self,
        context: ChatContext,
        groups: list[CodexThreadGroup],
    ) -> InlineKeyboardMarkup | None:
        rows: list[list[tuple[str, str, dict[str, object]]]] = [
            [
                (
                    "Connect "
                    + _short_line(
                        thread.title or thread.preview or "(untitled)",
                        limit=32,
                    ),
                    "attach_codex",
                    {
                        "codex_backend_id": thread.codex_backend_id,
                        "codex_thread_id": thread.thread_id,
                    },
                )
            ]
            for group in groups
            for thread in group.threads
        ]
        return await self._callback_markup(context, rows)

    async def _send_codex_threads_listing(
        self,
        context: ChatContext,
        codex_options: CodexThreadsCommand,
    ) -> None:
        async with self._typing_loop(context):
            codex_listing = await self._service.list_codex_threads(
                context.chat_key,
                backend_name=codex_options.backend_name,
                include_all=codex_options.include_all,
                search=codex_options.search,
                limit=CODEX_THREADS_FULL_LIMIT,
            )
        connect_commands = await self._codex_thread_text_shortcuts(
            context,
            codex_listing.groups,
        )
        listing = build_codex_thread_listing(
            codex_listing.groups,
            failures=codex_listing.failures,
            connect_commands=connect_commands,
            full=codex_options.full,
        )
        await self._send_text(
            context,
            listing.text,
            parse_mode="HTML",
        )

    async def _send_codex_threads_connection_picker(
        self,
        context: ChatContext,
    ) -> None:
        async with self._typing_loop(context):
            connections = await self._service.list_backend_connections()
        rows: list[list[tuple[str, str, dict[str, object]]]] = [
            [
                (
                    "Recent",
                    "codex_threads_recent",
                    {},
                )
            ],
            [
                (
                    "Recent projects",
                    "codex_threads_recent_projects",
                    {},
                )
            ],
        ]
        rows.extend(
            [
                (
                    _connection_button_label(connection),
                    "codex_threads_connection",
                    {
                        "connection_id": connection.connection_id,
                        "connection_label": _connection_label(connection),
                        "include_all": False,
                    },
                )
            ]
            for connection in connections
        )
        await self._send_text(
            context,
            "Choose a connection.",
            reply_markup=await self._callback_markup(context, rows),
        )

    async def _send_codex_threads_project_picker(
        self,
        context: ChatContext,
        *,
        connection_id: str | None,
        connection_label: str,
        include_all: bool,
        expanded: bool = False,
    ) -> None:
        limit = _recent_picker_request_limit(expanded)
        async with self._typing_loop(context):
            projects = await self._service.list_recent_projects(
                chat_key=context.chat_key,
                connection_id=connection_id,
                include_all=include_all,
                limit=limit,
            )
        payload_base: dict[str, object] = {
            "connection_label": connection_label,
            "include_all": include_all,
        }
        if connection_id is not None:
            payload_base["connection_id"] = connection_id
        visible_projects = _visible_recent_items(projects, expanded=expanded)
        rows: list[list[tuple[str, str, dict[str, object]]]] = [
            [
                (
                    project.label,
                    "codex_threads_project",
                    {
                        **payload_base,
                        "project_id": project.project_id,
                    },
                )
            ]
            for project in visible_projects
        ]
        if _has_more_recent_items(projects, expanded=expanded):
            more_action = (
                "codex_threads_recent_projects"
                if include_all and connection_id is None
                else "codex_threads_connection"
            )
            rows.append([("More", more_action, {**payload_base, "expanded": True})])
        rows.append(
            [
                (
                    "All projects",
                    "codex_threads_project",
                    payload_base,
                )
            ]
        )
        await self._send_text(
            context,
            f"Choose a project on {connection_label}.",
            reply_markup=await self._callback_markup(context, rows),
        )

    async def _send_codex_threads_thread_picker(
        self,
        context: ChatContext,
        *,
        connection_id: str | None,
        include_all: bool,
        project_id: str | None,
        expanded: bool = False,
    ) -> None:
        limit = _recent_picker_request_limit(expanded)
        async with self._typing_loop(context):
            codex_listing = await self._service.list_codex_threads(
                context.chat_key,
                backend_id=connection_id,
                include_all=include_all,
                project_id=project_id,
                limit=limit,
            )
        visible_groups = _visible_codex_thread_groups(
            codex_listing.groups,
            expanded=expanded,
        )
        connect_commands = await self._codex_thread_text_shortcuts(
            context,
            visible_groups,
        )
        listing = build_codex_thread_listing(
            visible_groups,
            failures=codex_listing.failures,
            connect_commands=connect_commands,
            full=True,
        )
        payload: dict[str, object] = {
            "include_all": include_all,
            "expanded": True,
        }
        if connection_id is not None:
            payload["connection_id"] = connection_id
        if project_id is not None:
            payload["project_id"] = project_id
        await self._send_text(
            context,
            listing.text,
            parse_mode="HTML",
            reply_markup=await self._more_markup(
                context,
                action="codex_threads_project",
                payload=payload,
                visible_groups=codex_listing.groups,
                expanded=expanded,
            ),
        )

    async def _send_recent_codex_threads(
        self,
        context: ChatContext,
        *,
        expanded: bool = False,
    ) -> None:
        limit = _recent_picker_request_limit(expanded)
        async with self._typing_loop(context):
            codex_listing = await self._service.list_recent_codex_threads(
                context.chat_key,
                limit=limit,
            )
        visible_groups = _visible_codex_thread_groups(
            codex_listing.groups,
            expanded=expanded,
        )
        connect_commands = await self._codex_thread_text_shortcuts(
            context,
            visible_groups,
        )
        listing = build_recent_codex_thread_listing(
            visible_groups,
            failures=codex_listing.failures,
            connect_commands=connect_commands,
        )
        await self._send_text(
            context,
            listing.text,
            parse_mode="HTML",
            reply_markup=await self._more_markup(
                context,
                action="codex_threads_recent",
                payload={"expanded": True},
                visible_groups=codex_listing.groups,
                expanded=expanded,
            ),
        )

    async def _send_recent_new_projects(
        self,
        context: ChatContext,
        *,
        expanded: bool = False,
    ) -> None:
        limit = _recent_picker_request_limit(expanded)
        projects = await self._service.list_recent_projects(
            chat_key=context.chat_key,
            include_all=True,
            limit=limit,
        )
        visible_projects = _visible_recent_items(projects, expanded=expanded)
        rows: list[list[tuple[str, str, dict[str, object]]]] = [
            [
                (
                    project.label,
                    "new_project",
                    {"project_id": project.project_id},
                )
            ]
            for project in visible_projects
        ]
        if _has_more_recent_items(projects, expanded=expanded):
            rows.append([("More", "new_recent_projects", {"expanded": True})])
        await self._send_text(
            context,
            "Choose a recent project.",
            reply_markup=await self._callback_markup(context, rows),
        )

    async def _send_new_connection_picker(
        self,
        context: ChatContext,
    ) -> None:
        connections = await self._service.list_backend_connections()
        rows: list[list[tuple[str, str, dict[str, object]]]] = []
        if self._service.has_default_project():
            rows.append([("Default", "new_default_project", {})])
        rows.append([("Recent projects", "new_recent_projects", {})])
        rows.extend(
            [
                (
                    _connection_button_label(connection),
                    "new_connection",
                    {
                        "connection_id": connection.connection_id,
                        "connection_label": _connection_label(connection),
                    },
                )
            ]
            for connection in connections
        )
        await self._send_text(
            context,
            "Choose a Codex connection for the new conversation.",
            reply_markup=await self._callback_markup(context, rows),
        )

    async def _send_new_project_picker(
        self,
        context: ChatContext,
        *,
        connection_id: str,
        connection_label: str,
        expanded: bool = False,
    ) -> None:
        limit = _recent_picker_request_limit(expanded)
        projects = await self._service.list_recent_projects(
            chat_key=context.chat_key,
            connection_id=connection_id,
            include_all=False,
            limit=limit,
        )
        visible_projects = _visible_recent_items(projects, expanded=expanded)
        rows: list[list[tuple[str, str, dict[str, object]]]] = [
            [
                (
                    project.label,
                    "new_project",
                    {"project_id": project.project_id},
                )
            ]
            for project in visible_projects
        ]
        if _has_more_recent_items(projects, expanded=expanded):
            rows.append(
                [
                    (
                        "More",
                        "new_connection",
                        {
                            "connection_id": connection_id,
                            "connection_label": connection_label,
                            "expanded": True,
                        },
                    )
                ]
            )
        await self._send_text(
            context,
            f"Choose a project on {connection_label}.",
            reply_markup=await self._callback_markup(context, rows),
        )

    async def _codex_thread_text_shortcuts(
        self,
        context: ChatContext,
        groups: list[CodexThreadGroup],
    ) -> dict[str, str]:
        expires_at = (datetime.now(UTC) + timedelta(minutes=30)).isoformat()
        commands: dict[str, str] = {}
        for group in groups:
            for thread in group.threads:
                token = await self._repository.create_callback_token(
                    chat_key=context.chat_key,
                    topic_id=context.topic_id,
                    action="attach_codex",
                    payload={
                        "codex_backend_id": thread.codex_backend_id,
                        "codex_thread_id": thread.thread_id,
                    },
                    expires_at=expires_at,
                )
                commands[_codex_thread_key(thread)] = f"/ct_{token}"
        return commands

    async def _conversation_focus_text_shortcuts(
        self,
        context: ChatContext,
        anchors: list[ConversationAnchor],
        focused_bridge_id: str | None,
        *,
        prefix: str = "cf",
    ) -> dict[str, str]:
        expires_at = (datetime.now(UTC) + timedelta(minutes=30)).isoformat()
        commands: dict[str, str] = {}
        for anchor in anchors:
            if anchor.latest_bridge_id == focused_bridge_id:
                continue
            if (
                prefix == "ct"
                and anchor.latest_bridge_id is not None
                and _telegram_command_value(anchor.latest_bridge_id)
            ):
                commands[anchor.anchor_id] = f"/{prefix}_{anchor.latest_bridge_id}"
                continue
            token = await self._repository.create_callback_token(
                chat_key=context.chat_key,
                topic_id=context.topic_id,
                action="focus_conversation",
                payload={"anchor_id": anchor.anchor_id},
                expires_at=expires_at,
            )
            commands[anchor.anchor_id] = f"/{prefix}_{token}"
        return commands

    async def _webhooks_markup(
        self,
        context: ChatContext,
        subscriptions: list[WebhookSubscription],
    ) -> InlineKeyboardMarkup | None:
        rows: list[list[tuple[str, str, dict[str, object]]]] = []
        for subscription in subscriptions:
            if not subscription.enabled:
                continue
            rows.append(
                [
                    (
                        "Focus conversation",
                        "focus_conversation",
                        {"anchor_id": subscription.anchor_id or ""},
                    ),
                    (
                        "Revoke " + _short_id(subscription.webhook_id),
                        "revoke_webhook",
                        {"webhook_id": subscription.webhook_id},
                    ),
                ]
            )
        return await self._callback_markup(context, rows)

    async def _user_input_markup(
        self,
        context: ChatContext,
        pending: PendingUserInput,
    ) -> InlineKeyboardMarkup | None:
        rows: list[list[tuple[str, str, dict[str, object]]]] = []
        for question in pending.questions:
            selected = pending.selected_answers.get(question.question_id, ())
            for option in question.options:
                label = (
                    f"✓ {option.label}" if option.label in selected else option.label
                )
                rows.append(
                    [
                        (
                            label,
                            "user_input_select",
                            {
                                "request_id": pending.request_id,
                                "question_id": question.question_id,
                                "answer": option.label,
                            },
                        )
                    ]
                )
            rows.append(
                [
                    (
                        "Other",
                        "user_input_other",
                        {
                            "request_id": pending.request_id,
                            "question_id": question.question_id,
                        },
                    )
                ]
            )
        if len(pending.questions) > 1 and _user_input_complete(pending):
            rows.append(
                [
                    (
                        "Submit",
                        "user_input_submit",
                        {"request_id": pending.request_id},
                    )
                ]
            )
        return await self._callback_markup(context, rows)

    async def _plan_proposal_markup(
        self,
        context: ChatContext,
        thread_id: str,
    ) -> InlineKeyboardMarkup | None:
        return await self._callback_markup(
            context,
            [
                [
                    (
                        "Implement plan",
                        "implement_plan",
                        {"thread_id": thread_id},
                    ),
                ]
            ],
        )

    async def _approval_markup(
        self,
        context: ChatContext,
        pending: PendingApproval,
    ) -> InlineKeyboardMarkup | None:
        return await self._callback_markup(
            context,
            [
                [
                    (
                        "Approve once",
                        "resolve_approval",
                        {"request_id": pending.request_id, "decision": "approve"},
                    ),
                    (
                        "Approve session",
                        "resolve_approval",
                        {
                            "request_id": pending.request_id,
                            "decision": "approve for session",
                        },
                    ),
                ],
                [
                    (
                        "Deny",
                        "resolve_approval",
                        {"request_id": pending.request_id, "decision": "deny"},
                    ),
                    (
                        "Cancel",
                        "resolve_approval",
                        {"request_id": pending.request_id, "decision": "cancel"},
                    ),
                ],
            ],
        )

    async def _more_markup(
        self,
        context: ChatContext,
        *,
        action: str,
        payload: dict[str, object],
        visible_groups: list[CodexThreadGroup],
        expanded: bool,
    ) -> InlineKeyboardMarkup | None:
        if not _has_more_codex_thread_items(visible_groups, expanded=expanded):
            return None
        return await self._callback_markup(context, [[("More", action, payload)]])

    async def _goal_control_markup(
        self,
        context: ChatContext,
        goal: CodexGoal | None,
    ) -> InlineKeyboardMarkup | None:
        if goal is None:
            return None
        rows: list[list[tuple[str, str, dict[str, object]]]]
        if goal.status == "active":
            rows = [[("Pause", "goal_pause", {}), ("Cancel", "goal_cancel", {})]]
        elif goal.status == "paused":
            rows = [[("Resume", "goal_resume", {}), ("Cancel", "goal_cancel", {})]]
        else:
            rows = [[("Cancel", "goal_cancel", {})]]
        return await self._callback_markup(context, rows)

    async def _callback_markup(
        self,
        context: ChatContext,
        rows: list[list[tuple[str, str, dict[str, object]]]],
    ) -> InlineKeyboardMarkup | None:
        if not rows:
            return None
        expires_at = (datetime.now(UTC) + timedelta(minutes=30)).isoformat()
        keyboard: list[list[InlineKeyboardButton]] = []
        for row in rows:
            buttons = []
            for label, action, payload in row:
                token = await self._repository.create_callback_token(
                    chat_key=context.chat_key,
                    topic_id=context.topic_id,
                    action=action,
                    payload=payload,
                    expires_at=expires_at,
                )
                buttons.append(
                    InlineKeyboardButton(
                        text=label,
                        callback_data=f"ct:{token}",
                    )
                )
            keyboard.append(buttons)
        return InlineKeyboardMarkup(inline_keyboard=keyboard)

    async def _send_noncritical_text(
        self,
        context: ChatContext,
        text: str,
        *,
        parse_mode: str | None = None,
        turn_id: str | None = None,
        logical_thread_id: str | None = None,
        event_name: str,
    ) -> Message | None:
        if self._delivery_backoff_active(context):
            return None
        try:
            sent = await self._send_text(context, text, parse_mode=parse_mode)
        except Exception as err:
            if await self._note_delivery_throttle(
                context,
                err,
                turn_id=turn_id,
                logical_thread_id=logical_thread_id,
                event_name=event_name,
            ):
                return None
            raise
        self._clear_delivery_backoff(context)
        return sent

    async def _send_text_with_retry(
        self,
        context: ChatContext,
        text: str,
        *,
        reply_markup: InlineKeyboardMarkup | None = None,
        parse_mode: str | None = None,
        turn_id: str | None = None,
        logical_thread_id: str | None = None,
        event_name: str,
    ) -> Message:
        attempt = 0
        while True:
            try:
                sent = await self._send_text(
                    context,
                    text,
                    reply_markup=reply_markup,
                    parse_mode=parse_mode,
                )
            except Exception as err:
                if await self._retry_after_backoff(
                    context,
                    err,
                    turn_id=turn_id,
                    logical_thread_id=logical_thread_id,
                    event_name=event_name,
                    attempt=attempt,
                ):
                    attempt += 1
                    continue
                raise
            self._clear_delivery_backoff(context)
            return sent

    async def _edit_message_text_with_retry(
        self,
        context: ChatContext,
        *,
        message_id: int,
        text: str,
        parse_mode: str | None = None,
        reply_markup: InlineKeyboardMarkup | None = None,
        turn_id: str | None = None,
        logical_thread_id: str | None = None,
        event_name: str,
    ) -> None:
        attempt = 0
        while True:
            try:
                await self._edit_text(
                    context,
                    message_id=message_id,
                    text=text,
                    reply_markup=reply_markup,
                    parse_mode=parse_mode,
                )
            except Exception as err:
                if await self._retry_after_backoff(
                    context,
                    err,
                    turn_id=turn_id,
                    logical_thread_id=logical_thread_id,
                    event_name=event_name,
                    attempt=attempt,
                ):
                    attempt += 1
                    continue
                raise
            self._clear_delivery_backoff(context)
            return

    async def _edit_message_reply_markup_with_retry(
        self,
        context: ChatContext,
        *,
        message_id: int,
        reply_markup: InlineKeyboardMarkup | None,
        logical_thread_id: str | None = None,
        event_name: str,
    ) -> None:
        attempt = 0
        while True:
            try:
                await self._bot.edit_message_reply_markup(
                    chat_id=context.chat_id,
                    message_id=message_id,
                    reply_markup=reply_markup,
                )
            except Exception as err:
                if await self._retry_after_backoff(
                    context,
                    err,
                    logical_thread_id=logical_thread_id,
                    event_name=event_name,
                    attempt=attempt,
                ):
                    attempt += 1
                    continue
                raise
            self._clear_delivery_backoff(context)
            return

    async def _send_runtime_error(
        self,
        context: ChatContext,
        err: Exception,
    ) -> None:
        summary = _public_error_summary(err)
        log_error(LOGGER, "telegram_runtime_error_sent", v={"summary": summary})
        await self._send_noncritical_text(
            context,
            WARNING_PREFIX + "<b>Message handling failed</b>\n" + escape(summary),
            event_name="telegram_runtime_error_throttled",
            parse_mode="HTML",
        )

    async def _notify_interrupted_threads(self) -> None:
        interrupted = await self._service.list_interrupted_threads()
        for thread in interrupted:
            if not await self._service.take_interrupted_notice(thread.thread_id):
                continue
            context = chat_context_from_key(thread.chat_key)
            await self._progress_store.clear_progress(thread.thread_id)
            await self._send_text(
                context,
                WARNING_PREFIX
                + "The previous Codex request was interrupted by a restart.",
            )

    async def _bridge_expiry_loop(self) -> None:
        while True:
            try:
                await self._expire_idle_bridges_once()
            except Exception as err:
                log_exception(LOGGER, "telegram_bridge_expiry_loop_failed", err=err)
            await asyncio.sleep(IDLE_WRAP_POLL_SECONDS)

    async def _expire_idle_bridges_once(self) -> None:
        expired = await self._service.expire_idle_bridges()
        if expired:
            log_debug(
                LOGGER,
                "telegram_bridge_windows_expired",
                v={"count": len(expired), "bridge_ids": expired},
            )

    @asynccontextmanager
    async def _typing_loop(
        self,
        context: ChatContext,
        *,
        thread_id: str | None = None,
    ) -> AsyncIterator[None]:
        task = asyncio.create_task(self._typing_pump(context, thread_id=thread_id))
        try:
            yield
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def _typing_pump(
        self,
        context: ChatContext,
        *,
        thread_id: str | None = None,
    ) -> None:
        while True:
            if thread_id is None or await self._typing_thread_is_focused(
                context.chat_key,
                thread_id,
            ):
                await self._bot.send_chat_action(
                    chat_id=context.chat_id,
                    action=ChatAction.TYPING,
                    message_thread_id=context.topic_id,
                )
            await asyncio.sleep(4.0)

    async def _typing_thread_is_focused(self, chat_key: str, thread_id: str) -> bool:
        focused = await self._service.ensure_focused_bridge(chat_key)
        return focused.bridge_id == thread_id

    def _spawn_background(self, coro: Coroutine[object, object, None]) -> None:
        task: asyncio.Task[None] = asyncio.create_task(coro)
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)

    def _delivery_backoff_active(self, context: ChatContext) -> bool:
        until = self._delivery_backoff_until.get(context.chat_key)
        if until is None:
            return False
        now = asyncio.get_running_loop().time()
        if until <= now:
            self._delivery_backoff_until.pop(context.chat_key, None)
            return False
        return True

    def _clear_delivery_backoff(self, context: ChatContext) -> None:
        self._delivery_backoff_until.pop(context.chat_key, None)

    async def _note_delivery_throttle(
        self,
        context: ChatContext,
        err: Exception,
        *,
        turn_id: str | None = None,
        logical_thread_id: str | None = None,
        event_name: str,
    ) -> bool:
        retry_after = _telegram_retry_after_seconds(err)
        if retry_after is None:
            return False
        now = asyncio.get_running_loop().time()
        self._delivery_backoff_until[context.chat_key] = max(
            self._delivery_backoff_until.get(context.chat_key, 0.0),
            now + retry_after,
        )
        log_warning(
            LOGGER,
            event_name,
            turn_id=turn_id,
            logical_thread_id=logical_thread_id,
            v={"retry_after_seconds": retry_after},
        )
        return True

    async def _retry_after_backoff(
        self,
        context: ChatContext,
        err: Exception,
        *,
        turn_id: str | None = None,
        logical_thread_id: str | None = None,
        event_name: str,
        attempt: int,
    ) -> bool:
        if not await self._note_delivery_throttle(
            context,
            err,
            turn_id=turn_id,
            logical_thread_id=logical_thread_id,
            event_name=event_name,
        ):
            return False
        if attempt >= 1:
            return False
        retry_after = _telegram_retry_after_seconds(err)
        if retry_after is None:
            return False
        await asyncio.sleep(retry_after)
        return True

    def _allowed(self, chat_id: int) -> bool:
        return self._allow_from is None or chat_id in self._allow_from

    def _webhook_event_url(self, webhook_id: str) -> str:
        base_url = self._webhook_public_base_url or self._webhook_local_base_url
        if not base_url:
            return f"/events/{webhook_id}"
        return f"{base_url.rstrip('/')}/events/{webhook_id}"

    async def _reply_target_thread(
        self,
        context: ChatContext,
        message: Message,
    ) -> str | None:
        reply_to = getattr(message, "reply_to_message", None)
        message_id = getattr(reply_to, "message_id", None)
        if not isinstance(message_id, int):
            return None
        final = await self._progress_store.get_final_message_by_reply(
            context.chat_key,
            message_id,
        )
        if final is None:
            return None
        return final.thread_id

    async def _one_shot_reply_target_thread(
        self,
        context: ChatContext,
        message: Message,
    ) -> str | None:
        reply_to = getattr(message, "reply_to_message", None)
        message_id = getattr(reply_to, "message_id", None)
        if not isinstance(message_id, int):
            return None
        target = await self._progress_store.consume_pending_reply_target(
            chat_key=context.chat_key,
            prompt_message_id=message_id,
        )
        if target is None:
            return None
        return target.target_thread_id

    async def _send_reply_prompt(
        self,
        context: ChatContext,
        thread_id: str,
    ) -> None:
        sent = await self._bot.send_message(
            chat_id=context.chat_id,
            text="Reply to this prompt to answer that conversation once.",
            message_thread_id=context.topic_id,
            reply_markup=ForceReply(selective=True),
        )
        await self._progress_store.save_pending_reply_target(
            chat_key=context.chat_key,
            prompt_message_id=sent.message_id,
            target_thread_id=thread_id,
            expires_at=(datetime.now(UTC) + timedelta(minutes=30)).isoformat(),
        )

    async def _register_commands(self) -> None:
        try:
            await self._bot.set_my_commands(telegram_bot_commands())
        except Exception as err:
            log_exception(LOGGER, "telegram_command_registration_failed", err=err)

    async def _resolve_message_input(
        self,
        message: Message,
        context: ChatContext,
    ) -> UserTurnInput | None:
        if isinstance(message.text, str):
            text = message.text.strip()
            if text:
                return UserTurnInput(text=text)
        return await self._media_input.resolve(message, context)

    async def _drain_attachment_jobs_once(self) -> None:
        await self._attachment_delivery.drain_once()

    async def _bridge_control_loop(self) -> None:
        while True:
            await self._drain_bridge_control_jobs_once()
            await asyncio.sleep(1.0)

    async def _drain_bridge_control_jobs_once(self) -> None:
        jobs = await self._repository.list_pending_bridge_control_jobs(limit=20)
        for job in jobs:
            try:
                await self._deliver_bridge_control_job(job)
            except Exception as exc:
                log_exception(
                    LOGGER,
                    "telegram_bridge_control_delivery_failed",
                    err=exc,
                    job_id=job.job_id,
                    logical_thread_id=job.logical_thread_id,
                )
                if job.job_id is not None:
                    await self._repository.mark_bridge_control_job_failed(
                        job.job_id,
                        str(exc),
                    )
            else:
                if job.job_id is not None:
                    await self._repository.mark_bridge_control_job_delivered(job.job_id)

    async def _deliver_bridge_control_job(self, job: BridgeControlJob) -> None:
        context = chat_context_from_key(job.chat_key)
        if job.kind == "notify":
            text = str(job.payload.get("text", "")).strip()
            if not text:
                raise ValueError("notify bridge-control job requires text")
            level = str(job.payload.get("level", "info"))
            prefix = WARNING_PREFIX if level == "warning" else "ℹ️ "
            await self._send_text(context, prefix + text)
            return
        if job.kind == "refresh_status_card":
            await self._sync_thread_status_card(context)
            return
        raise ValueError(f"Unsupported bridge-control job kind: {job.kind}")


def _recent_picker_request_limit(expanded: bool) -> int:
    return EXPANDED_PICKER_LIMIT if expanded else RECENT_PICKER_LIMIT + 1


def _visible_recent_items(projects: list[Project], *, expanded: bool) -> list[Project]:
    limit = EXPANDED_PICKER_LIMIT if expanded else RECENT_PICKER_LIMIT
    return projects[:limit]


def _has_more_recent_items(items: Sequence[object], *, expanded: bool) -> bool:
    return not expanded and len(items) > RECENT_PICKER_LIMIT


def _visible_codex_thread_groups(
    groups: list[CodexThreadGroup],
    *,
    expanded: bool,
) -> list[CodexThreadGroup]:
    limit = EXPANDED_PICKER_LIMIT if expanded else RECENT_PICKER_LIMIT
    return _trim_codex_thread_groups(groups, limit=limit)


def _has_more_codex_thread_items(
    groups: list[CodexThreadGroup],
    *,
    expanded: bool,
) -> bool:
    return not expanded and _codex_thread_count(groups) > RECENT_PICKER_LIMIT


def _trim_codex_thread_groups(
    groups: list[CodexThreadGroup],
    *,
    limit: int,
) -> list[CodexThreadGroup]:
    remaining = limit
    trimmed: list[CodexThreadGroup] = []
    for group in groups:
        if remaining <= 0:
            break
        selected = group.threads[:remaining]
        if selected:
            trimmed.append(replace(group, threads=selected))
            remaining -= len(selected)
    return trimmed


def _codex_thread_count(groups: list[CodexThreadGroup]) -> int:
    return sum(len(group.threads) for group in groups)


def _connection_label(connection: BackendConnection) -> str:
    return connection.label or connection.connection_id


def _bridge_command_requires_telegram_picker(name: str, argument: str) -> bool:
    return name in {"new", "threads"} and not argument.strip()


def _connection_button_label(connection: BackendConnection) -> str:
    return f"Connection: {connection.connection_id}"


def _short_id(value: str, length: int = 8) -> str:
    return value if len(value) <= length else value[:length]


def _telegram_image_input(
    image: TurnResultImage,
) -> FSInputFile | BufferedInputFile | URLInputFile:
    source = image.source
    if source.startswith(("http://", "https://")):
        return URLInputFile(source)
    if source.startswith("data:image/"):
        return _buffered_image_input(source)
    path = Path(source.removeprefix("file://"))
    return FSInputFile(str(_validate_attachment_path(str(path))))


def _presentation_thread_id(value: object) -> str | None:
    for attr in ("thread_id", "bridge_id"):
        thread_id = getattr(value, attr, None)
        if isinstance(thread_id, str) and thread_id:
            return thread_id
    return None


def _thread_message_image(message: ThreadMessage) -> TurnResultImage | None:
    if message.kind != "final_image":
        return None
    payload = json.loads(message.text)
    if not isinstance(payload, dict):
        return None
    source = payload.get("source")
    if not isinstance(source, str) or not source.strip():
        return None
    caption = payload.get("caption")
    return TurnResultImage(
        source=source.strip(),
        caption=caption if isinstance(caption, str) and caption else None,
    )


def _buffered_image_input(source: str) -> BufferedInputFile:
    header, separator, payload = source.partition(",")
    if not separator:
        raise ValueError("Image data URL is missing a payload.")
    try:
        data = base64.b64decode(payload, validate=True)
    except binascii.Error as err:
        raise ValueError("Image data URL is not valid base64.") from err
    extension = "png"
    if ";" in header:
        mime_type = header.removeprefix("data:").split(";", maxsplit=1)[0]
        if mime_type == "image/jpeg":
            extension = "jpg"
        elif mime_type.startswith("image/"):
            extension = mime_type.removeprefix("image/").replace("+", "-")
    return BufferedInputFile(data, filename=f"generated.{extension}")


def _rich_final_rendered_text(
    prefix: str,
    text: str,
    *,
    html_header: bool = False,
) -> str:
    return "\n".join(_rich_final_chunks(prefix, text, html_header=html_header))


def _rich_final_chunks(
    prefix: str,
    text: str,
    *,
    html_header: bool = False,
    text_limit: int = TELEGRAM_MESSAGE_TEXT_LIMIT,
) -> list[str]:
    rendered_prefix = f"<b>{escape(prefix)}</b>\n\n" if html_header else escape(prefix)
    builder = _TelegramHtmlChunkBuilder(rendered_prefix, text_limit=text_limit)
    for kind, language, body in _markdown_blocks(text):
        if kind == "code":
            builder.append_wrapped_text(
                body,
                *_code_block_tags(language),
            )
        else:
            _append_inline_markdown(builder, body)
    return builder.finish()


def _rich_progress_text(text: str) -> str:
    chunks = _rich_final_chunks(THOUGHT_BALLOON_PREFIX, text)
    if len(chunks) == 1:
        return chunks[0]
    notice = escape(TELEGRAM_TRUNCATION_NOTICE)
    text_limit = TELEGRAM_MESSAGE_TEXT_LIMIT - len(notice)
    chunks = _rich_final_chunks(
        THOUGHT_BALLOON_PREFIX,
        text,
        text_limit=text_limit,
    )
    return chunks[0].rstrip() + notice


MARKDOWN_HEADING_RE = re.compile(r"^(?P<indent>\s{0,3})#{1,6}\s+(?P<body>.+?)\s*$")
MARKDOWN_UNORDERED_LIST_RE = re.compile(r"^(?P<indent>\s*)[-*]\s+(?P<body>\S.*)$")
MARKDOWN_ORDERED_LIST_RE = re.compile(
    r"^(?P<indent>\s*)(?P<number>\d+)[.)]\s+(?P<body>\S.*)$"
)
MARKDOWN_CLOSING_HEADING_RE = re.compile(r"\s+#+\s*$")
PLAN_LINE_RE = re.compile(r"^\s*(?:[-*]|\d+[.)])\s+\S+", re.MULTILINE)


def _looks_like_plan_text(text: str) -> bool:
    """Return whether free-form final text has a plan-list shape."""
    return len(PLAN_LINE_RE.findall(text)) >= 2


def _plan_items_text(runtime: CodexRuntimeState) -> str:
    lines: list[str] = []
    for index, item in enumerate(runtime.plan_items, start=1):
        lines.append(f"{index}. [{_plan_status_label(item.status)}] {item.step}")
    return "\n".join(lines)


def _plan_status_label(status: str) -> str:
    normalized = status.strip().casefold()
    if normalized in {"completed", "complete", "done"}:
        return "done"
    if normalized in {"in_progress", "in-progress", "running", "started"}:
        return "doing"
    if normalized in {"pending", "todo", "queued"}:
        return "todo"
    return normalized or "todo"


def _markdown_blocks(text: str) -> list[tuple[str, str | None, str]]:
    lines = text.splitlines(keepends=True)
    blocks: list[tuple[str, str | None, str]] = []
    pending: list[str] = []
    index = 0
    while index < len(lines):
        line = lines[index]
        stripped = line.strip()
        if stripped.startswith("```"):
            if pending:
                blocks.append(("text", None, "".join(pending)))
                pending = []
            language = _code_language(stripped[3:].strip())
            index += 1
            code: list[str] = []
            while index < len(lines):
                if lines[index].strip().startswith("```"):
                    index += 1
                    break
                code.append(lines[index])
                index += 1
            blocks.append(("code", language, "".join(code)))
            continue
        pending.append(line)
        index += 1
    if pending:
        blocks.append(("text", None, "".join(pending)))
    return blocks


def _code_language(value: str) -> str | None:
    if not value:
        return None
    candidate = value.split(maxsplit=1)[0]
    if not candidate or len(candidate) > 40:
        return None
    if all(char.isalnum() or char in {"_", "-"} for char in candidate):
        return candidate
    return None


def _code_block_tags(language: str | None) -> tuple[str, str]:
    if language is None:
        return "<pre>", "</pre>"
    return (
        f'<pre><code class="language-{escape(language, quote=True)}">',
        "</code></pre>",
    )


def _append_inline_markdown(
    builder: "_TelegramHtmlChunkBuilder",
    text: str,
) -> None:
    for line in text.splitlines(keepends=True):
        body = line.rstrip("\r\n")
        newline = line[len(body) :]
        _append_inline_markdown_line(builder, body)
        if newline:
            builder.append_escaped_text(newline)


def _append_inline_markdown_line(
    builder: "_TelegramHtmlChunkBuilder",
    line: str,
) -> None:
    heading_match = MARKDOWN_HEADING_RE.match(line)
    if heading_match is not None:
        builder.append_escaped_text(heading_match.group("indent"))
        body = MARKDOWN_CLOSING_HEADING_RE.sub("", heading_match.group("body")).strip()
        builder.append_wrapped_text(body, "<b>", "</b>")
        return

    unordered_match = MARKDOWN_UNORDERED_LIST_RE.match(line)
    if unordered_match is not None:
        builder.append_escaped_text(unordered_match.group("indent") + "• ")
        _append_inline_markdown_fragment(builder, unordered_match.group("body"))
        return

    ordered_match = MARKDOWN_ORDERED_LIST_RE.match(line)
    if ordered_match is not None:
        builder.append_escaped_text(
            f"{ordered_match.group('indent')}{ordered_match.group('number')}. "
        )
        _append_inline_markdown_fragment(builder, ordered_match.group("body"))
        return

    _append_inline_markdown_fragment(builder, line)


def _append_inline_markdown_fragment(
    builder: "_TelegramHtmlChunkBuilder",
    text: str,
) -> None:
    index = 0
    while index < len(text):
        if text.startswith("`", index):
            end = text.find("`", index + 1)
            if end > index + 1:
                builder.append_wrapped_text(text[index + 1 : end], "<code>", "</code>")
                index = end + 1
                continue
        if text.startswith("**", index):
            end = text.find("**", index + 2)
            if end > index + 2:
                builder.append_wrapped_text(text[index + 2 : end], "<b>", "</b>")
                index = end + 2
                continue
        builder.append_escaped_text(text[index])
        index += 1


class _TelegramHtmlChunkBuilder:
    def __init__(
        self,
        prefix: str,
        *,
        text_limit: int = TELEGRAM_MESSAGE_TEXT_LIMIT,
    ) -> None:
        self._chunks: list[str] = []
        self._current = prefix
        self._text_limit = text_limit

    def append_escaped_text(self, text: str) -> None:
        for char in text:
            self._append_raw(escape(char))

    def append_wrapped_text(self, text: str, open_tag: str, close_tag: str) -> None:
        if len(open_tag) + len(close_tag) > self._text_limit:
            raise ValueError("Telegram HTML wrapper exceeds the message limit.")
        if len(self._current) + len(open_tag) + len(close_tag) > self._text_limit:
            self._flush()
        self._append_raw(open_tag)
        if not text:
            self._append_raw(close_tag)
            return
        for char in text:
            escaped = escape(char)
            if len(self._current) + len(escaped) + len(close_tag) > self._text_limit:
                self._append_raw(close_tag)
                self._flush()
                self._append_raw(open_tag)
            self._current += escaped
        self._append_raw(close_tag)

    def finish(self) -> list[str]:
        if self._current or not self._chunks:
            self._flush()
        return self._chunks

    def _append_raw(self, value: str) -> None:
        if len(value) > self._text_limit:
            raise ValueError("Telegram HTML atom exceeds the message limit.")
        if len(self._current) + len(value) > self._text_limit:
            self._flush()
        self._current += value

    def _flush(self) -> None:
        self._chunks.append(self._current)
        self._current = ""
