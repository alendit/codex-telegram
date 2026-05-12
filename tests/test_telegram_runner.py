from pathlib import Path
import asyncio
from collections.abc import Coroutine
import re
import sqlite3
from types import SimpleNamespace
from typing import cast
from unittest.mock import AsyncMock, Mock

import pytest
from aiogram import Dispatcher
from aiogram.enums import ChatAction
from aiogram.types import CallbackQuery, ForceReply, Message

from codex_telegram.adapters.persistence.sqlite import (
    SQLiteStateRepository,
    SQLiteTelegramProgressStore,
)
from codex_telegram.adapters.telegram import bot as telegram_bot_module
from codex_telegram.adapters.telegram.bot import (
    TELEGRAM_MESSAGE_TEXT_LIMIT,
    ChatContext,
    TelegramBotRunner,
)
from codex_telegram.application.models import (
    CallbackToken,
    CodexPlanItem,
    CurrentThreadState,
    CodexThreadBackendFailure,
    CodexThreadGroup,
    CodexThreadListResult,
    CodexRuntimeState,
    EffectiveSettings,
    RealtimeStartResult,
)
from codex_telegram.application.service import (
    BotService,
    BotServiceConfig,
    ThreadSelectionResult,
    TurnRunResult,
)
from codex_telegram.domain import (
    BridgeSnapshot,
    CodexGoal,
    CodexThread,
    ConversationAnchor,
    LogicalThread,
    PendingApproval,
    PendingUserInput,
    Project,
    RealtimeEvent,
    RealtimeSession,
    SessionOverrides,
    ThreadMessage,
    TurnResult,
    TurnResultImage,
    TurnUpdate,
    UserInputOption,
    UserInputQuestion,
)


class _RetryAfterError(Exception):
    def __init__(self, retry_after: float) -> None:
        super().__init__(f"retry after {retry_after}")
        self.retry_after = retry_after


class _LongTelegramMessageError(Exception):
    def __str__(self) -> str:
        return "Telegram server says - Bad Request: message is too long"


def _effective_settings(*, collaboration_mode: str = "default") -> EffectiveSettings:
    return EffectiveSettings(
        profile="operator",
        model="gpt-5.4",
        model_provider="openai",
        effort="medium",
        summary="concise",
        cwd="/agent",
        fast_mode=False,
        verbosity="verbose",
        command_verbosity="errors",
        followup_mode="steer",
        overrides={"collaboration_mode": collaboration_mode},
        collaboration_mode=collaboration_mode,
    )


def _button_texts(markup: object) -> list[list[str]]:
    inline_keyboard = getattr(markup, "inline_keyboard")
    return [[button.text for button in row] for row in inline_keyboard]


@pytest.mark.asyncio
async def test_typing_loop_suppresses_typing_after_thread_loses_focus(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = SQLiteStateRepository(tmp_path / "state.db")
    progress_store = SQLiteTelegramProgressStore(tmp_path / "state.db")
    await repository.initialize()
    await progress_store.initialize()
    service = AsyncMock()
    service.ensure_focused_bridge.side_effect = [
        SimpleNamespace(bridge_id="thread-1"),
        SimpleNamespace(bridge_id="thread-2"),
    ]
    bot = AsyncMock()
    runner = TelegramBotRunner(
        bot,
        Dispatcher(),
        service,
        repository,
        progress_store,
        None,
        False,
    )
    real_sleep = asyncio.sleep
    sleep_count = 0

    async def fast_sleep(seconds: float) -> None:
        nonlocal sleep_count
        del seconds
        sleep_count += 1
        if sleep_count >= 2:
            raise asyncio.CancelledError
        await real_sleep(0)

    monkeypatch.setattr(telegram_bot_module.asyncio, "sleep", fast_sleep)

    with pytest.raises(asyncio.CancelledError):
        await runner._typing_pump(
            ChatContext(chat_key="chat:1", chat_id=1, topic_id=None),
            thread_id="thread-1",
        )

    bot.send_chat_action.assert_awaited_once_with(
        chat_id=1,
        action=ChatAction.TYPING,
        message_thread_id=None,
    )


@pytest.mark.asyncio
async def test_bridge_api_command_uses_telegram_command_executor(
    tmp_path: Path,
) -> None:
    repository = SQLiteStateRepository(tmp_path / "state.db")
    progress_store = SQLiteTelegramProgressStore(tmp_path / "state.db")
    await repository.initialize()
    await progress_store.initialize()
    await repository.ensure_chat("chat:1")
    await repository.create_thread("chat:1", "thread-1", "Bridge command")
    thread = await repository.get_thread("thread-1")
    assert thread is not None

    service = AsyncMock()
    service.bridge_snapshot.return_value = BridgeSnapshot(
        logical_thread_id="thread-1",
        chat_key="chat:1",
        title="Bridge command",
        anchor_id=None,
        codex_backend_id="primary",
        codex_thread_id=None,
        active=True,
        awaiting_reply=False,
        pending_turn_id=None,
        expires_at=None,
        closed_at=None,
    )
    service.current_thread_state.return_value = CurrentThreadState(
        thread=thread,
        settings=_effective_settings(),
        pending=None,
        realtime=None,
        runtime=CodexRuntimeState(),
    )
    bot = AsyncMock()
    runner = TelegramBotRunner(
        bot,
        Dispatcher(),
        service,
        repository,
        progress_store,
        None,
        False,
    )

    result = await runner.run_bridge_command("thread-1", "/current")

    assert result == {"accepted": True, "thread_id": "thread-1"}
    service.current_thread_state.assert_awaited_once_with("chat:1")
    bot.send_message.assert_awaited_once()
    assert (
        "<b>Conversation</b> Bridge command"
        in bot.send_message.await_args.kwargs["text"]
    )
    assert bot.send_message.await_args.kwargs["parse_mode"] == "HTML"


@pytest.mark.asyncio
async def test_status_command_is_compatibility_alias_for_current_thread(
    tmp_path: Path,
) -> None:
    repository = SQLiteStateRepository(tmp_path / "state.db")
    progress_store = SQLiteTelegramProgressStore(tmp_path / "state.db")
    await repository.initialize()
    await progress_store.initialize()
    await repository.ensure_chat("chat:1")
    await repository.create_thread("chat:1", "thread-1", "Bridge command")
    thread = await repository.get_thread("thread-1")
    assert thread is not None

    service = AsyncMock()
    service.bridge_snapshot.return_value = BridgeSnapshot(
        logical_thread_id="thread-1",
        chat_key="chat:1",
        title="Bridge command",
        anchor_id=None,
        codex_backend_id="primary",
        codex_thread_id=None,
        active=True,
        awaiting_reply=False,
        pending_turn_id=None,
        expires_at=None,
        closed_at=None,
    )
    service.current_thread_state.return_value = CurrentThreadState(
        thread=thread,
        settings=_effective_settings(),
        pending=None,
        realtime=None,
        runtime=CodexRuntimeState(),
    )
    bot = AsyncMock()
    runner = TelegramBotRunner(
        bot,
        Dispatcher(),
        service,
        repository,
        progress_store,
        None,
        False,
    )

    result = await runner.run_bridge_command("thread-1", "/status")

    assert result == {"accepted": True, "thread_id": "thread-1"}
    service.current_thread_state.assert_awaited_once_with("chat:1")
    assert (
        "<b>Conversation</b> Bridge command"
        in bot.send_message.await_args.kwargs["text"]
    )
    assert bot.send_message.await_args.kwargs["parse_mode"] == "HTML"


@pytest.mark.asyncio
async def test_bridge_api_rejects_incomplete_picker_command(tmp_path: Path) -> None:
    repository = SQLiteStateRepository(tmp_path / "state.db")
    progress_store = SQLiteTelegramProgressStore(tmp_path / "state.db")
    await repository.initialize()
    await progress_store.initialize()
    await repository.ensure_chat("chat:1")
    await repository.create_thread("chat:1", "thread-1", "Bridge command")

    service = AsyncMock()
    service.bridge_snapshot.return_value = BridgeSnapshot(
        logical_thread_id="thread-1",
        chat_key="chat:1",
        title="Bridge command",
        anchor_id=None,
        codex_backend_id="primary",
        codex_thread_id=None,
        active=True,
        awaiting_reply=False,
        pending_turn_id=None,
        expires_at=None,
        closed_at=None,
    )
    runner = TelegramBotRunner(
        AsyncMock(),
        Dispatcher(),
        service,
        repository,
        progress_store,
        None,
        False,
    )

    with pytest.raises(ValueError, match="requires Telegram picker UI"):
        await runner.run_bridge_command("thread-1", "/new")


@pytest.mark.asyncio
async def test_send_text_bounds_message_before_telegram(tmp_path: Path) -> None:
    repository = SQLiteStateRepository(tmp_path / "state.db")
    progress_store = SQLiteTelegramProgressStore(tmp_path / "state.db")
    await repository.initialize()
    await progress_store.initialize()
    bot = AsyncMock()
    runner = TelegramBotRunner(
        bot,
        Dispatcher(),
        AsyncMock(),
        repository,
        progress_store,
        None,
        False,
    )

    await runner._send_text(
        ChatContext(chat_key="chat:1", chat_id=1, topic_id=None),
        "x" * (TELEGRAM_MESSAGE_TEXT_LIMIT + 500),
    )

    sent_text = bot.send_message.await_args.kwargs["text"]
    assert len(sent_text) <= TELEGRAM_MESSAGE_TEXT_LIMIT
    assert "truncated to fit Telegram" in sent_text


@pytest.mark.asyncio
async def test_send_text_preserves_html_rendering_when_truncated(
    tmp_path: Path,
) -> None:
    repository = SQLiteStateRepository(tmp_path / "state.db")
    progress_store = SQLiteTelegramProgressStore(tmp_path / "state.db")
    await repository.initialize()
    await progress_store.initialize()
    bot = AsyncMock()
    runner = TelegramBotRunner(
        bot,
        Dispatcher(),
        AsyncMock(),
        repository,
        progress_store,
        None,
        False,
    )
    objective = "no Clojure -> TypeScript dependencies; " + (
        "migrate backend from TypeScript to Clojure; " * 120
    )

    await runner._send_text(
        ChatContext(chat_key="chat:1", chat_id=1, topic_id=None),
        telegram_bot_module.render_goal_status(
            CodexGoal(
                objective=objective,
                status="active",
                token_budget=None,
                tokens_used=None,
                elapsed_seconds=None,
                created_at=None,
                updated_at=None,
            )
        ),
        parse_mode="HTML",
    )

    sent = bot.send_message.await_args.kwargs
    assert sent["parse_mode"] == "HTML"
    assert len(sent["text"]) <= TELEGRAM_MESSAGE_TEXT_LIMIT
    assert sent["text"].count("<b>") == sent["text"].count("</b>")
    assert "-&gt;" in sent["text"]
    assert "truncated to fit Telegram" in sent["text"]


@pytest.mark.asyncio
async def test_followup_mode_command_updates_followup_mode_override(
    tmp_path: Path,
) -> None:
    repository = SQLiteStateRepository(tmp_path / "state.db")
    progress_store = SQLiteTelegramProgressStore(tmp_path / "state.db")
    await repository.initialize()
    await progress_store.initialize()
    service = AsyncMock()
    service.update_override.return_value = EffectiveSettings(
        profile="operator",
        model="gpt-5.4",
        model_provider="openai",
        effort="medium",
        summary="concise",
        cwd="/agent",
        fast_mode=False,
        verbosity="verbose",
        command_verbosity="verbose",
        followup_mode="queue",
        overrides={},
    )
    bot = AsyncMock()
    runner = TelegramBotRunner(
        bot,
        Dispatcher(),
        service,
        repository,
        progress_store,
        None,
        False,
    )

    handled = await runner._handle_command(
        ChatContext("chat:1", 1, None),
        "thread-1",
        ("followup_mode", "queue"),
        "/followup_mode queue",
    )

    assert handled is True
    service.update_override.assert_awaited_once_with(
        "thread-1",
        "followup_mode",
        "queue",
    )
    bot.send_message.assert_awaited_once()
    assert bot.send_message.await_args.kwargs["text"] == "<b>followup_mode</b> queue"
    assert bot.send_message.await_args.kwargs["parse_mode"] == "HTML"


@pytest.mark.asyncio
async def test_goal_command_status_renders_current_goal(tmp_path: Path) -> None:
    repository = SQLiteStateRepository(tmp_path / "state.db")
    progress_store = SQLiteTelegramProgressStore(tmp_path / "state.db")
    await repository.initialize()
    await progress_store.initialize()
    service = AsyncMock()
    service.get_goal.return_value = CodexGoal(
        objective="Ship goal command",
        status="active",
    )
    bot = AsyncMock()
    runner = TelegramBotRunner(
        bot,
        Dispatcher(),
        service,
        repository,
        progress_store,
        None,
        False,
    )

    handled = await runner._handle_command(
        ChatContext("chat:1", 1, None),
        "thread-1",
        ("goal", "status"),
        "/goal status",
    )

    assert handled is True
    service.get_goal.assert_awaited_once_with("chat:1")
    assert (
        "<b>Objective</b> Ship goal command"
        in bot.send_message.await_args.kwargs["text"]
    )
    assert bot.send_message.await_args.kwargs["parse_mode"] == "HTML"
    assert _button_texts(bot.send_message.await_args.kwargs["reply_markup"]) == [
        ["Pause", "Cancel"]
    ]


@pytest.mark.asyncio
async def test_goal_command_status_renders_paused_goal_controls(
    tmp_path: Path,
) -> None:
    repository = SQLiteStateRepository(tmp_path / "state.db")
    progress_store = SQLiteTelegramProgressStore(tmp_path / "state.db")
    await repository.initialize()
    await progress_store.initialize()
    service = AsyncMock()
    service.get_goal.return_value = CodexGoal(
        objective="Ship goal command",
        status="paused",
    )
    bot = AsyncMock()
    runner = TelegramBotRunner(
        bot,
        Dispatcher(),
        service,
        repository,
        progress_store,
        None,
        False,
    )

    handled = await runner._handle_command(
        ChatContext("chat:1", 1, None),
        "thread-1",
        ("goal", ""),
        "/goal",
    )

    assert handled is True
    assert _button_texts(bot.send_message.await_args.kwargs["reply_markup"]) == [
        ["Resume", "Cancel"]
    ]


@pytest.mark.asyncio
async def test_goal_command_sets_objective_with_budget(tmp_path: Path) -> None:
    repository = SQLiteStateRepository(tmp_path / "state.db")
    progress_store = SQLiteTelegramProgressStore(tmp_path / "state.db")
    await repository.initialize()
    await progress_store.initialize()
    service = AsyncMock()
    service.set_goal.return_value = CodexGoal(
        objective="Ship goal command",
        status="active",
        token_budget=500,
    )
    bot = AsyncMock()
    runner = TelegramBotRunner(
        bot,
        Dispatcher(),
        service,
        repository,
        progress_store,
        None,
        False,
    )

    handled = await runner._handle_command(
        ChatContext("chat:1", 1, None),
        "thread-1",
        ("goal", "--budget 500 Ship goal command"),
        "/goal --budget 500 Ship goal command",
    )

    assert handled is True
    service.set_goal.assert_awaited_once_with(
        "chat:1",
        objective="Ship goal command",
        token_budget=500,
        status="active",
        update_token_budget=True,
    )
    assert "<b>Tokens</b> 0 / 500" in bot.send_message.await_args.kwargs["text"]
    assert bot.send_message.await_args.kwargs["parse_mode"] == "HTML"


@pytest.mark.asyncio
async def test_goal_command_updates_and_clears_budget(tmp_path: Path) -> None:
    repository = SQLiteStateRepository(tmp_path / "state.db")
    progress_store = SQLiteTelegramProgressStore(tmp_path / "state.db")
    await repository.initialize()
    await progress_store.initialize()
    service = AsyncMock()
    service.update_goal_budget.side_effect = [
        CodexGoal("Ship goal command", token_budget=250),
        CodexGoal("Ship goal command"),
    ]
    bot = AsyncMock()
    runner = TelegramBotRunner(
        bot,
        Dispatcher(),
        service,
        repository,
        progress_store,
        None,
        False,
    )
    context = ChatContext("chat:1", 1, None)

    handled = await runner._handle_command(
        context,
        "thread-1",
        ("goal", "--budget 250"),
        "/goal --budget 250",
    )
    handled_clear = await runner._handle_command(
        context,
        "thread-1",
        ("goal", "--budget unlimited"),
        "/goal --budget unlimited",
    )

    assert handled is True
    assert handled_clear is True
    assert service.update_goal_budget.await_args_list[0].args == ("chat:1", 250)
    assert service.update_goal_budget.await_args_list[1].args == ("chat:1", None)


@pytest.mark.asyncio
async def test_goal_command_pause_resume_and_clear(tmp_path: Path) -> None:
    repository = SQLiteStateRepository(tmp_path / "state.db")
    progress_store = SQLiteTelegramProgressStore(tmp_path / "state.db")
    await repository.initialize()
    await progress_store.initialize()
    service = AsyncMock()
    service.update_goal_status.side_effect = [
        CodexGoal("Ship goal command", status="paused"),
        CodexGoal("Ship goal command", status="active"),
    ]
    bot = AsyncMock()
    runner = TelegramBotRunner(
        bot,
        Dispatcher(),
        service,
        repository,
        progress_store,
        None,
        False,
    )
    context = ChatContext("chat:1", 1, None)

    assert await runner._handle_command(
        context, "thread-1", ("goal", "pause"), "/goal pause"
    )
    assert await runner._handle_command(
        context, "thread-1", ("goal", "resume"), "/goal resume"
    )
    assert await runner._handle_command(
        context, "thread-1", ("goal", "clear"), "/goal clear"
    )

    assert service.update_goal_status.await_args_list[0].args == ("chat:1", "paused")
    assert service.update_goal_status.await_args_list[1].args == ("chat:1", "active")
    service.clear_goal.assert_awaited_once_with("chat:1")


@pytest.mark.asyncio
async def test_goal_control_callback_updates_goal_status(tmp_path: Path) -> None:
    repository = SQLiteStateRepository(tmp_path / "state.db")
    progress_store = SQLiteTelegramProgressStore(tmp_path / "state.db")
    await repository.initialize()
    await progress_store.initialize()
    service = AsyncMock()
    service.update_goal_status.return_value = CodexGoal(
        "Ship goal command",
        status="paused",
    )
    bot = AsyncMock()
    runner = TelegramBotRunner(
        bot,
        Dispatcher(),
        service,
        repository,
        progress_store,
        None,
        False,
    )

    await runner._callback_actions.handle(
        ChatContext("chat:1", 1, None),
        CallbackToken(
            token="token-1",
            chat_key="chat:1",
            topic_id=None,
            action="goal_pause",
            payload={},
            expires_at="2026-05-07T00:00:00+00:00",
        ),
    )

    service.update_goal_status.assert_awaited_once_with("chat:1", "paused")
    assert "<b>Status</b> paused" in bot.send_message.await_args.kwargs["text"]
    assert _button_texts(bot.send_message.await_args.kwargs["reply_markup"]) == [
        ["Resume", "Cancel"]
    ]


@pytest.mark.asyncio
async def test_goal_cancel_callback_clears_goal(tmp_path: Path) -> None:
    repository = SQLiteStateRepository(tmp_path / "state.db")
    progress_store = SQLiteTelegramProgressStore(tmp_path / "state.db")
    await repository.initialize()
    await progress_store.initialize()
    service = AsyncMock()
    bot = AsyncMock()
    runner = TelegramBotRunner(
        bot,
        Dispatcher(),
        service,
        repository,
        progress_store,
        None,
        False,
    )

    await runner._callback_actions.handle(
        ChatContext("chat:1", 1, None),
        CallbackToken(
            token="token-1",
            chat_key="chat:1",
            topic_id=None,
            action="goal_cancel",
            payload={},
            expires_at="2026-05-07T00:00:00+00:00",
        ),
    )

    service.clear_goal.assert_awaited_once_with("chat:1")
    assert bot.send_message.await_args.kwargs["text"] == "Goal canceled."


@pytest.mark.asyncio
async def test_plan_command_sets_sticky_plan_mode(tmp_path: Path) -> None:
    repository = SQLiteStateRepository(tmp_path / "state.db")
    progress_store = SQLiteTelegramProgressStore(tmp_path / "state.db")
    await repository.initialize()
    await progress_store.initialize()
    service = AsyncMock()
    service.set_collaboration_mode.return_value = EffectiveSettings(
        profile="operator",
        model="gpt-5.4",
        model_provider="openai",
        effort="medium",
        summary="concise",
        cwd="/agent",
        fast_mode=False,
        verbosity="verbose",
        command_verbosity="errors",
        followup_mode="steer",
        overrides={"collaboration_mode": "plan"},
        collaboration_mode="plan",
    )
    bot = AsyncMock()
    runner = TelegramBotRunner(
        bot,
        Dispatcher(),
        service,
        repository,
        progress_store,
        None,
        False,
    )

    handled = await runner._handle_command(
        ChatContext("chat:1", 1, None),
        "thread-1",
        ("plan", ""),
        "/plan",
    )

    assert handled is True
    service.set_collaboration_mode.assert_awaited_once_with("chat:1", "plan")
    assert "<b>Mode</b> plan" in bot.send_message.await_args.kwargs["text"]
    assert bot.send_message.await_args.kwargs["parse_mode"] == "HTML"


@pytest.mark.asyncio
async def test_plan_prompt_sets_mode_before_turn(tmp_path: Path) -> None:
    repository = SQLiteStateRepository(tmp_path / "state.db")
    progress_store = SQLiteTelegramProgressStore(tmp_path / "state.db")
    await repository.initialize()
    await progress_store.initialize()
    service = AsyncMock()
    service.set_collaboration_mode.return_value = EffectiveSettings(
        profile="operator",
        model="gpt-5.4",
        model_provider="openai",
        effort="medium",
        summary="concise",
        cwd="/agent",
        fast_mode=False,
        verbosity="verbose",
        command_verbosity="errors",
        followup_mode="steer",
        overrides={"collaboration_mode": "plan"},
        collaboration_mode="plan",
    )
    service.run_turn.return_value = TurnRunResult(
        result=TurnResult(
            turn_id="turn-1",
            chat_key="chat:1",
            logical_thread_id="thread-1",
            codex_thread_id="codex-1",
            status="completed",
            final_text="Planned.",
        ),
        remapped=False,
        remap_warning=None,
    )
    bot = AsyncMock()
    runner = TelegramBotRunner(
        bot,
        Dispatcher(),
        service,
        repository,
        progress_store,
        None,
        False,
    )

    handled = await runner._handle_command(
        ChatContext("chat:1", 1, None),
        "thread-1",
        ("plan", "design this"),
        "/plan design this",
    )

    assert handled is True
    service.set_collaboration_mode.assert_awaited_once_with("chat:1", "plan")
    service.run_turn.assert_awaited_once()
    assert service.run_turn.await_args.args[:2] == ("chat:1", "design this")


@pytest.mark.asyncio
async def test_implement_command_switches_default_before_turn(tmp_path: Path) -> None:
    repository = SQLiteStateRepository(tmp_path / "state.db")
    progress_store = SQLiteTelegramProgressStore(tmp_path / "state.db")
    await repository.initialize()
    await progress_store.initialize()
    service = AsyncMock()
    service.run_turn.return_value = TurnRunResult(
        result=TurnResult(
            turn_id="turn-1",
            chat_key="chat:1",
            logical_thread_id="thread-1",
            codex_thread_id="codex-1",
            status="completed",
            final_text="Implemented.",
        ),
        remapped=False,
        remap_warning=None,
    )
    bot = AsyncMock()
    runner = TelegramBotRunner(
        bot,
        Dispatcher(),
        service,
        repository,
        progress_store,
        None,
        False,
    )

    handled = await runner._handle_command(
        ChatContext("chat:1", 1, None),
        "thread-1",
        ("implement", ""),
        "/implement",
    )

    assert handled is True
    service.set_collaboration_mode.assert_awaited_once_with("chat:1", "default")
    assert service.run_turn.await_args.args[:2] == (
        "chat:1",
        "Implement as planned.",
    )


@pytest.mark.asyncio
async def test_plan_result_sends_proposal_with_implement_button_only(
    tmp_path: Path,
) -> None:
    repository = SQLiteStateRepository(tmp_path / "state.db")
    progress_store = SQLiteTelegramProgressStore(tmp_path / "state.db")
    await repository.initialize()
    await progress_store.initialize()
    service = AsyncMock()
    service.get_settings.return_value = _effective_settings(collaboration_mode="plan")
    bot = AsyncMock()
    bot.send_message.return_value = SimpleNamespace(message_id=55)
    runner = TelegramBotRunner(
        bot,
        Dispatcher(),
        service,
        repository,
        progress_store,
        None,
        False,
    )

    await runner._finish_run_result(
        ChatContext("chat:1", 1, None),
        TurnRunResult(
            result=TurnResult(
                turn_id="turn-1",
                chat_key="chat:1",
                logical_thread_id="thread-1",
                codex_thread_id="codex-1",
                status="completed",
                final_text="1. Inspect\n2. Implement",
            ),
            remapped=False,
            remap_warning=None,
        ),
    )

    sent = bot.send_message.await_args.kwargs
    assert sent["text"] == "<b>Proposed plan</b>\n\n1. Inspect\n2. Implement"
    assert sent["parse_mode"] == "HTML"
    buttons = [
        button.text for row in sent["reply_markup"].inline_keyboard for button in row
    ]
    assert buttons == ["Implement plan"]


@pytest.mark.asyncio
async def test_plan_result_replaces_existing_progress_message(
    tmp_path: Path,
) -> None:
    repository = SQLiteStateRepository(tmp_path / "state.db")
    progress_store = SQLiteTelegramProgressStore(tmp_path / "state.db")
    await repository.initialize()
    await progress_store.initialize()
    await progress_store.save_progress(
        "thread-1",
        message_id=44,
        rendered_text="1. Inspect",
    )
    service = AsyncMock()
    service.get_settings.return_value = _effective_settings(collaboration_mode="plan")
    bot = AsyncMock()
    runner = TelegramBotRunner(
        bot,
        Dispatcher(),
        service,
        repository,
        progress_store,
        None,
        False,
    )

    await runner._finish_run_result(
        ChatContext("chat:1", 1, None),
        TurnRunResult(
            result=TurnResult(
                turn_id="turn-1",
                chat_key="chat:1",
                logical_thread_id="thread-1",
                codex_thread_id="codex-1",
                status="completed",
                final_text="1. Inspect\n2. Implement",
            ),
            remapped=False,
            remap_warning=None,
        ),
    )

    bot.send_message.assert_not_awaited()
    edited = bot.edit_message_text.await_args.kwargs
    assert edited["message_id"] == 44
    assert edited["text"] == "<b>Proposed plan</b>\n\n1. Inspect\n2. Implement"
    assert edited["parse_mode"] == "HTML"
    buttons = [
        button.text for row in edited["reply_markup"].inline_keyboard for button in row
    ]
    assert buttons == ["Implement plan"]
    assert await progress_store.get_progress("thread-1") is None


@pytest.mark.asyncio
async def test_plan_result_renders_structured_runtime_plan(
    tmp_path: Path,
) -> None:
    repository = SQLiteStateRepository(tmp_path / "state.db")
    progress_store = SQLiteTelegramProgressStore(tmp_path / "state.db")
    await repository.initialize()
    await progress_store.initialize()
    service = AsyncMock()
    service.get_settings.return_value = _effective_settings(collaboration_mode="plan")
    service.runtime_state_for_thread.return_value = CodexRuntimeState(
        plan_items=(
            CodexPlanItem(step="Inspect status card", status="completed"),
            CodexPlanItem(step="Render plan state", status="in_progress"),
        )
    )
    bot = AsyncMock()
    bot.send_message.return_value = SimpleNamespace(message_id=55)
    runner = TelegramBotRunner(
        bot,
        Dispatcher(),
        service,
        repository,
        progress_store,
        None,
        False,
    )

    await runner._finish_run_result(
        ChatContext("chat:1", 1, None),
        TurnRunResult(
            result=TurnResult(
                turn_id="turn-1",
                chat_key="chat:1",
                logical_thread_id="thread-1",
                codex_thread_id="codex-1",
                status="completed",
                final_text="I’m in Plan Mode now, so I can’t edit files yet.",
            ),
            remapped=False,
            remap_warning=None,
        ),
    )

    sent = bot.send_message.await_args.kwargs
    assert sent["text"] == (
        "<b>Proposed plan</b>\n\n"
        "1. [done] Inspect status card\n"
        "2. [doing] Render plan state"
    )
    assert sent["parse_mode"] == "HTML"
    buttons = [
        button.text for row in sent["reply_markup"].inline_keyboard for button in row
    ]
    assert buttons == ["Implement plan"]


@pytest.mark.asyncio
async def test_plan_result_renders_structured_runtime_plan_without_final_text(
    tmp_path: Path,
) -> None:
    repository = SQLiteStateRepository(tmp_path / "state.db")
    progress_store = SQLiteTelegramProgressStore(tmp_path / "state.db")
    await repository.initialize()
    await progress_store.initialize()
    service = AsyncMock()
    service.get_settings.return_value = _effective_settings(collaboration_mode="plan")
    service.runtime_state_for_thread.return_value = CodexRuntimeState(
        plan_items=(
            CodexPlanItem(step="Inspect runtime state", status="completed"),
            CodexPlanItem(step="Show actual plan", status="pending"),
        )
    )
    bot = AsyncMock()
    bot.send_message.return_value = SimpleNamespace(message_id=55)
    runner = TelegramBotRunner(
        bot,
        Dispatcher(),
        service,
        repository,
        progress_store,
        None,
        False,
    )

    await runner._finish_run_result(
        ChatContext("chat:1", 1, None),
        TurnRunResult(
            result=TurnResult(
                turn_id="turn-1",
                chat_key="chat:1",
                logical_thread_id="thread-1",
                codex_thread_id="codex-1",
                status="completed",
                final_text="",
            ),
            remapped=False,
            remap_warning=None,
        ),
    )

    sent = bot.send_message.await_args.kwargs
    assert sent["text"] == (
        "<b>Proposed plan</b>\n\n"
        "1. [done] Inspect runtime state\n"
        "2. [todo] Show actual plan"
    )
    assert sent["parse_mode"] == "HTML"


@pytest.mark.asyncio
async def test_plan_result_after_user_input_renders_structured_runtime_plan(
    tmp_path: Path,
) -> None:
    repository = SQLiteStateRepository(tmp_path / "state.db")
    progress_store = SQLiteTelegramProgressStore(tmp_path / "state.db")
    await repository.initialize()
    await progress_store.initialize()
    service = AsyncMock()
    service.continue_turn.return_value = TurnResult(
        turn_id="turn-1",
        chat_key="chat:1",
        logical_thread_id="thread-1",
        codex_thread_id="codex-1",
        status="completed",
        final_text="The plan is ready.",
    )
    service.get_settings.return_value = _effective_settings(collaboration_mode="plan")
    service.runtime_state_for_thread.return_value = CodexRuntimeState(
        plan_items=(
            CodexPlanItem(step="Inspect runtime state", status="completed"),
            CodexPlanItem(step="Show actual plan", status="pending"),
        )
    )
    bot = AsyncMock()
    bot.send_message.return_value = SimpleNamespace(message_id=55)
    runner = TelegramBotRunner(
        bot,
        Dispatcher(),
        service,
        repository,
        progress_store,
        None,
        False,
    )

    await runner._watch_turn_after_user_input(
        ChatContext("chat:1", 1, None),
        _pending_user_input(),
    )

    sent = bot.send_message.await_args.kwargs
    assert sent["text"] == (
        "<b>Proposed plan</b>\n\n"
        "1. [done] Inspect runtime state\n"
        "2. [todo] Show actual plan"
    )
    assert sent["parse_mode"] == "HTML"
    buttons = [
        button.text for row in sent["reply_markup"].inline_keyboard for button in row
    ]
    assert buttons == ["Implement plan"]


@pytest.mark.asyncio
async def test_plan_mode_generic_prose_sends_normal_final(
    tmp_path: Path,
) -> None:
    repository = SQLiteStateRepository(tmp_path / "state.db")
    progress_store = SQLiteTelegramProgressStore(tmp_path / "state.db")
    await repository.initialize()
    await progress_store.initialize()
    service = AsyncMock()
    service.get_settings.return_value = _effective_settings(collaboration_mode="plan")
    service.runtime_state_for_thread.return_value = CodexRuntimeState()
    bot = AsyncMock()
    bot.send_message.return_value = SimpleNamespace(message_id=55)
    runner = TelegramBotRunner(
        bot,
        Dispatcher(),
        service,
        repository,
        progress_store,
        None,
        False,
    )

    await runner._finish_run_result(
        ChatContext("chat:1", 1, None),
        TurnRunResult(
            result=TurnResult(
                turn_id="turn-1",
                chat_key="chat:1",
                logical_thread_id="thread-1",
                codex_thread_id="codex-1",
                status="completed",
                final_text="I’m in Plan Mode now, so I can’t edit files yet.",
            ),
            remapped=False,
            remap_warning=None,
        ),
    )

    sent = bot.send_message.await_args.kwargs
    assert sent["text"] == "✅ I’m in Plan Mode now, so I can’t edit files yet."
    assert "reply_markup" not in sent


@pytest.mark.asyncio
async def test_reply_to_plan_result_requests_plan_changes(
    tmp_path: Path,
) -> None:
    repository = SQLiteStateRepository(tmp_path / "state.db")
    progress_store = SQLiteTelegramProgressStore(tmp_path / "state.db")
    await repository.initialize()
    await progress_store.initialize()
    service = AsyncMock()
    service.get_settings.return_value = _effective_settings(collaboration_mode="plan")
    bot = AsyncMock()
    bot.send_message.return_value = SimpleNamespace(message_id=55)
    runner = TelegramBotRunner(
        bot,
        Dispatcher(),
        service,
        repository,
        progress_store,
        None,
        False,
    )

    await runner._finish_run_result(
        ChatContext("chat:1", 1, None),
        TurnRunResult(
            result=TurnResult(
                turn_id="turn-1",
                chat_key="chat:1",
                logical_thread_id="thread-1",
                codex_thread_id="codex-1",
                status="completed",
                final_text="1. Inspect\n2. Implement",
            ),
            remapped=False,
            remap_warning=None,
        ),
    )

    service.reset_mock()
    service.pending_user_input_for_chat.return_value = None
    service.ensure_active_thread.return_value = LogicalThread(
        thread_id="focused-thread",
        chat_key="chat:1",
        title="Focused",
        codex_thread_id="codex-focused",
        created_at="now",
        updated_at="now",
        turn_count=1,
        awaiting_reply=False,
        interrupted_notice=False,
        pending_turn_id=None,
    )
    service.resolve_bridge.return_value = ThreadSelectionResult(
        success=True,
        message="Resumed plan conversation.",
        thread=LogicalThread(
            thread_id="thread-1",
            chat_key="chat:1",
            title="Plan",
            codex_thread_id="codex-1",
            created_at="now",
            updated_at="now",
            turn_count=1,
            awaiting_reply=False,
            interrupted_notice=False,
            pending_turn_id=None,
        ),
    )
    service.take_interrupted_notice.return_value = False
    service.route_realtime_input.return_value = False
    service.run_turn.return_value = TurnRunResult(
        result=TurnResult(
            turn_id="turn-2",
            chat_key="chat:1",
            logical_thread_id="thread-1",
            codex_thread_id="codex-1",
            status="completed",
            final_text="Updated plan.",
        ),
        remapped=False,
        remap_warning=None,
    )
    message = SimpleNamespace(
        chat=SimpleNamespace(id=1),
        message_thread_id=None,
        text="Make the deploy step explicit.",
        reply_to_message=SimpleNamespace(message_id=55),
        photo=None,
        document=None,
        voice=None,
        audio=None,
    )

    await runner._on_message(cast(Message, message))

    service.resolve_bridge.assert_awaited_once_with(
        "chat:1",
        "thread-1",
        focus=False,
    )
    service.apply_implementation_trigger_if_needed.assert_awaited_once_with(
        "chat:1",
        "Make the deploy step explicit.",
    )
    service.run_turn.assert_awaited_once()
    assert service.run_turn.await_args.kwargs["thread_id"] == "thread-1"
    assert service.run_turn.await_args.args[1].text == "Make the deploy step explicit."


@pytest.mark.asyncio
async def test_implement_plan_callback_runs_target_thread_in_default_mode(
    tmp_path: Path,
) -> None:
    repository = SQLiteStateRepository(tmp_path / "state.db")
    progress_store = SQLiteTelegramProgressStore(tmp_path / "state.db")
    await repository.initialize()
    await progress_store.initialize()
    service = AsyncMock()
    service.start_plan_implementation.return_value = TurnRunResult(
        result=TurnResult(
            turn_id="turn-2",
            chat_key="chat:1",
            logical_thread_id="thread-1",
            codex_thread_id="codex-1",
            status="completed",
            final_text="Implemented.",
        ),
        remapped=False,
        remap_warning=None,
    )
    bot = AsyncMock()
    bot.send_message.return_value = SimpleNamespace(message_id=56)
    runner = TelegramBotRunner(
        bot,
        Dispatcher(),
        service,
        repository,
        progress_store,
        None,
        False,
    )

    await runner._callback_actions.handle(
        ChatContext("chat:1", 1, None),
        CallbackToken(
            token="token-1",
            chat_key="chat:1",
            topic_id=None,
            action="implement_plan",
            payload={"thread_id": "thread-1"},
            expires_at="2026-05-07T00:00:00+00:00",
        ),
    )

    service.start_plan_implementation.assert_awaited_once()
    assert service.start_plan_implementation.await_args.args[:2] == (
        "chat:1",
        "thread-1",
    )
    assert bot.send_message.await_args.kwargs["text"] == "✅ Implemented."


@pytest.mark.asyncio
async def test_edit_text_with_retry_bounds_message_before_telegram(
    tmp_path: Path,
) -> None:
    repository = SQLiteStateRepository(tmp_path / "state.db")
    progress_store = SQLiteTelegramProgressStore(tmp_path / "state.db")
    await repository.initialize()
    await progress_store.initialize()
    bot = AsyncMock()
    runner = TelegramBotRunner(
        bot,
        Dispatcher(),
        AsyncMock(),
        repository,
        progress_store,
        None,
        False,
    )

    await runner._edit_message_text_with_retry(
        ChatContext(chat_key="chat:1", chat_id=1, topic_id=None),
        message_id=77,
        text="x" * (TELEGRAM_MESSAGE_TEXT_LIMIT + 500),
        event_name="test_edit_throttled",
    )

    edited_text = bot.edit_message_text.await_args.kwargs["text"]
    assert len(edited_text) <= TELEGRAM_MESSAGE_TEXT_LIMIT
    assert "truncated to fit Telegram" in edited_text


@pytest.mark.asyncio
async def test_edit_text_with_retry_preserves_html_rendering_when_truncated(
    tmp_path: Path,
) -> None:
    repository = SQLiteStateRepository(tmp_path / "state.db")
    progress_store = SQLiteTelegramProgressStore(tmp_path / "state.db")
    await repository.initialize()
    await progress_store.initialize()
    bot = AsyncMock()
    runner = TelegramBotRunner(
        bot,
        Dispatcher(),
        AsyncMock(),
        repository,
        progress_store,
        None,
        False,
    )

    await runner._edit_message_text_with_retry(
        ChatContext(chat_key="chat:1", chat_id=1, topic_id=None),
        message_id=77,
        text="<b>Goal</b>\n<code>" + ("Clojure -> TypeScript\n" * 300) + "</code>",
        parse_mode="HTML",
        event_name="test_edit_throttled",
    )

    edited = bot.edit_message_text.await_args.kwargs
    assert edited["parse_mode"] == "HTML"
    assert len(edited["text"]) <= TELEGRAM_MESSAGE_TEXT_LIMIT
    assert edited["text"].count("<code>") == edited["text"].count("</code>")
    assert "truncated to fit Telegram" in edited["text"]


@pytest.mark.asyncio
async def test_progress_edit_bounds_message_before_telegram(tmp_path: Path) -> None:
    repository = SQLiteStateRepository(tmp_path / "state.db")
    progress_store = SQLiteTelegramProgressStore(tmp_path / "state.db")
    await repository.initialize()
    await progress_store.initialize()
    await progress_store.save_progress(
        "thread-1",
        message_id=77,
        rendered_text="old",
    )
    bot = AsyncMock()
    runner = TelegramBotRunner(
        bot,
        Dispatcher(),
        AsyncMock(),
        repository,
        progress_store,
        None,
        False,
    )

    await runner._handle_turn_update(
        ChatContext(chat_key="chat:1", chat_id=1, topic_id=None),
        TurnUpdate(
            turn_id="turn-1",
            chat_key="chat:1",
            logical_thread_id="thread-1",
            codex_thread_id="codex-1",
            status="running",
            source="agent",
            text=("x" * (TELEGRAM_MESSAGE_TEXT_LIMIT + 500)) + "\ncomplete",
        ),
    )

    edited_text = bot.edit_message_text.await_args.kwargs["text"]
    assert len(edited_text) <= TELEGRAM_MESSAGE_TEXT_LIMIT
    assert "truncated to fit Telegram" in edited_text


@pytest.mark.asyncio
async def test_wait_notice_summarizes_active_conversations_and_refreshes_status_card(
    tmp_path: Path,
) -> None:
    repository = SQLiteStateRepository(tmp_path / "state.db")
    progress_store = SQLiteTelegramProgressStore(tmp_path / "state.db")
    await repository.initialize()
    await progress_store.initialize()
    await progress_store.save_status_card(
        "chat:1",
        chat_id=1,
        topic_id=None,
        message_id=77,
        rendered_text="old",
    )

    focused = ConversationAnchor(
        anchor_id="anchor-focused",
        chat_key="chat:1",
        codex_backend_id="primary",
        codex_thread_id="codex-focused",
        title="Focused",
        alias=None,
        project_id=None,
        latest_bridge_id="bridge-focused",
        created_at="now",
        updated_at="now",
    )
    running = ConversationAnchor(
        anchor_id="anchor-running",
        chat_key="chat:1",
        codex_backend_id="primary",
        codex_thread_id="codex-running",
        title="Background work",
        alias=None,
        project_id=None,
        latest_bridge_id="bridge-running",
        created_at="now",
        updated_at="now",
        latest_bridge_pending_turn_id="turn-running",
        latest_bridge_awaiting_reply=True,
    )
    service = AsyncMock()
    service.list_conversations.return_value = [focused, running]
    service.ensure_focused_bridge.return_value = SimpleNamespace(
        bridge_id="bridge-focused",
        title="Focused",
        codex_backend_id="primary",
        codex_thread_id="codex-focused",
    )
    service.runtime_state_for_thread.return_value = CodexRuntimeState()
    bot = AsyncMock()
    bot.get_chat.return_value = SimpleNamespace(
        pinned_message=SimpleNamespace(message_id=77)
    )
    runner = TelegramBotRunner(
        bot,
        Dispatcher(),
        service,
        repository,
        progress_store,
        None,
        False,
    )

    await runner._handle_wait_notice(
        ChatContext(chat_key="chat:1", chat_id=1, topic_id=None)
    )

    wait_text = bot.send_message.await_args.kwargs["text"]
    assert wait_text == (
        "💭 Codex is still working. Running conversations: 1 background."
    )
    bot.edit_message_text.assert_awaited_once()
    status_text = bot.edit_message_text.await_args.kwargs["text"]
    assert "<b>(r)</b> /ct_" in status_text
    assert "Background work" in status_text
    assert "codex-running" not in status_text
    assert bot.edit_message_text.await_args.kwargs["parse_mode"] == "HTML"


@pytest.mark.asyncio
async def test_wait_notice_deduplicates_duplicate_focused_anchor_projection(
    tmp_path: Path,
) -> None:
    repository = SQLiteStateRepository(tmp_path / "state.db")
    progress_store = SQLiteTelegramProgressStore(tmp_path / "state.db")
    await repository.initialize()
    await progress_store.initialize()

    stale = ConversationAnchor(
        anchor_id="anchor-stale",
        chat_key="chat:1",
        codex_backend_id="primary",
        codex_thread_id="codex-stale",
        title="Stale",
        alias=None,
        project_id=None,
        latest_bridge_id="bridge-focused",
        created_at="now",
        updated_at="now",
        latest_bridge_pending_turn_id="turn-focused",
        latest_bridge_awaiting_reply=True,
    )
    current = ConversationAnchor(
        anchor_id="anchor-current",
        chat_key="chat:1",
        codex_backend_id="primary",
        codex_thread_id="codex-current",
        title="Focused",
        alias=None,
        project_id=None,
        latest_bridge_id="bridge-focused",
        created_at="now",
        updated_at="now",
        latest_bridge_pending_turn_id="turn-focused",
        latest_bridge_awaiting_reply=True,
    )
    service = AsyncMock()
    service.list_conversations.return_value = [stale, current]
    service.ensure_focused_bridge.return_value = SimpleNamespace(
        bridge_id="bridge-focused",
        title="Focused",
        codex_backend_id="primary",
        codex_thread_id="codex-current",
    )
    service.runtime_state_for_thread.return_value = CodexRuntimeState()
    bot = AsyncMock()
    runner = TelegramBotRunner(
        bot,
        Dispatcher(),
        service,
        repository,
        progress_store,
        None,
        False,
    )

    text = await runner._wait_notice_text(
        ChatContext(chat_key="chat:1", chat_id=1, topic_id=None)
    )

    assert text == "Codex is still working. Running conversations: 1 focused."


@pytest.mark.asyncio
async def test_background_status_card_shortcut_stays_stable_between_refreshes(
    tmp_path: Path,
) -> None:
    repository = SQLiteStateRepository(tmp_path / "state.db")
    progress_store = SQLiteTelegramProgressStore(tmp_path / "state.db")
    await repository.initialize()
    await progress_store.initialize()
    await progress_store.save_status_card(
        "chat:1",
        chat_id=1,
        topic_id=None,
        message_id=77,
        rendered_text="old",
    )

    focused = ConversationAnchor(
        anchor_id="anchor-focused",
        chat_key="chat:1",
        codex_backend_id="primary",
        codex_thread_id="codex-focused",
        title="Focused",
        alias=None,
        project_id=None,
        latest_bridge_id="11111111",
        created_at="now",
        updated_at="now",
    )
    running = ConversationAnchor(
        anchor_id="anchor-running",
        chat_key="chat:1",
        codex_backend_id="primary",
        codex_thread_id="codex-running",
        title="Background work",
        alias=None,
        project_id=None,
        latest_bridge_id="22222222",
        created_at="now",
        updated_at="now",
        latest_bridge_pending_turn_id="turn-running",
        latest_bridge_awaiting_reply=True,
    )
    service = AsyncMock()
    service.list_conversations.return_value = [focused, running]
    service.ensure_focused_bridge.return_value = SimpleNamespace(
        bridge_id="11111111",
        title="Focused",
        codex_backend_id="primary",
        codex_thread_id="codex-focused",
    )
    service.runtime_state_for_thread.return_value = CodexRuntimeState()
    bot = AsyncMock()
    bot.get_chat.return_value = SimpleNamespace(
        pinned_message=SimpleNamespace(message_id=77)
    )
    runner = TelegramBotRunner(
        bot,
        Dispatcher(),
        service,
        repository,
        progress_store,
        None,
        False,
    )
    context = ChatContext(chat_key="chat:1", chat_id=1, topic_id=None)

    await runner._sync_thread_status_card(context)

    first_text = bot.edit_message_text.await_args.kwargs["text"]
    assert "/ct_22222222" in first_text
    bot.edit_message_text.reset_mock()
    bot.pin_chat_message.reset_mock()
    bot.unpin_chat_message.reset_mock()

    await runner._sync_thread_status_card(context)

    bot.edit_message_text.assert_not_awaited()
    bot.pin_chat_message.assert_not_awaited()
    bot.unpin_chat_message.assert_not_awaited()
    card = await progress_store.get_status_card("chat:1")
    assert card is not None
    assert card.rendered_text == first_text

    waiting = ConversationAnchor(
        anchor_id="anchor-running",
        chat_key="chat:1",
        codex_backend_id="primary",
        codex_thread_id="codex-running",
        title="Background work",
        alias=None,
        project_id=None,
        latest_bridge_id="22222222",
        created_at="now",
        updated_at="now",
        latest_bridge_expires_at="2999-01-01T00:00:00+00:00",
    )
    service.list_conversations.return_value = [focused, waiting]

    await runner._sync_thread_status_card(context)

    bot.edit_message_text.assert_awaited_once()
    waiting_text = bot.edit_message_text.await_args.kwargs["text"]
    assert "<b>(w)</b> /ct_22222222 Background work" in waiting_text


@pytest.mark.asyncio
async def test_status_card_stable_bridge_shortcut_focuses_conversation(
    tmp_path: Path,
) -> None:
    repository = SQLiteStateRepository(tmp_path / "state.db")
    progress_store = SQLiteTelegramProgressStore(tmp_path / "state.db")
    await repository.initialize()
    await progress_store.initialize()

    service = AsyncMock()
    service.focus_bridge.return_value = SimpleNamespace(
        success=True,
        message="Focused conversation Background work.",
        thread=LogicalThread(
            thread_id="22222222",
            chat_key="chat:1",
            title="Background work",
            codex_thread_id="codex-running",
            created_at="now",
            updated_at="now",
            turn_count=1,
            awaiting_reply=False,
            interrupted_notice=False,
            pending_turn_id=None,
            codex_backend_id="primary",
        ),
    )
    service.focus_final_messages.return_value = []
    service.list_conversations.return_value = []
    service.ensure_focused_bridge.return_value = SimpleNamespace(
        bridge_id="22222222",
        title="Background work",
        codex_backend_id="primary",
        codex_thread_id="codex-running",
    )
    service.runtime_state_for_thread.return_value = CodexRuntimeState()
    bot = AsyncMock()
    bot.send_message.return_value = SimpleNamespace(message_id=88)
    runner = TelegramBotRunner(
        bot,
        Dispatcher(),
        service,
        repository,
        progress_store,
        None,
        False,
    )

    handled = await runner._handle_command(
        ChatContext(chat_key="chat:1", chat_id=1, topic_id=None),
        "focused-thread",
        ("ct_22222222", ""),
        "/ct_22222222",
    )

    assert handled is True
    service.focus_bridge.assert_awaited_once_with("chat:1", "22222222")
    bot.send_message.assert_any_await(
        chat_id=1,
        text="Focused conversation Background work.",
        message_thread_id=None,
    )


@pytest.mark.asyncio
async def test_focus_bridge_adds_background_actions_to_previous_final(
    tmp_path: Path,
) -> None:
    repository = SQLiteStateRepository(tmp_path / "state.db")
    progress_store = SQLiteTelegramProgressStore(tmp_path / "state.db")
    await repository.initialize()
    await progress_store.initialize()
    await repository.ensure_chat("chat:1")
    await repository.create_thread("chat:1", "focused-thread", "Focused")
    await progress_store.save_final_message(
        "focused-thread",
        chat_key="chat:1",
        message_id=55,
        rendered_text="✅ Previous answer",
    )
    service = AsyncMock()
    service.ensure_focused_bridge.return_value = LogicalThread(
        thread_id="focused-thread",
        chat_key="chat:1",
        title="Focused",
        codex_thread_id="codex-focused",
        created_at="now",
        updated_at="now",
        turn_count=1,
        awaiting_reply=False,
        interrupted_notice=False,
        pending_turn_id=None,
        codex_backend_id="primary",
    )
    service.focus_bridge.return_value = ThreadSelectionResult(
        success=True,
        message="Focused conversation Background work.",
        thread=LogicalThread(
            thread_id="background-thread",
            chat_key="chat:1",
            title="Background work",
            codex_thread_id="codex-background",
            created_at="now",
            updated_at="now",
            turn_count=1,
            awaiting_reply=False,
            interrupted_notice=False,
            pending_turn_id=None,
            codex_backend_id="primary",
        ),
    )
    bot = AsyncMock()
    runner = TelegramBotRunner(
        bot,
        Dispatcher(),
        service,
        repository,
        progress_store,
        None,
        False,
    )

    result = await runner._focus_bridge(
        ChatContext(chat_key="chat:1", chat_id=1, topic_id=None),
        "background-thread",
    )

    assert result.success is True
    service.focus_bridge.assert_awaited_once_with("chat:1", "background-thread")
    bot.edit_message_reply_markup.assert_awaited_once()
    edited = bot.edit_message_reply_markup.await_args.kwargs
    assert edited["chat_id"] == 1
    assert edited["message_id"] == 55
    buttons = [
        button.text for row in edited["reply_markup"].inline_keyboard for button in row
    ]
    assert buttons == ["Reply", "Focus"]


@pytest.mark.asyncio
async def test_status_card_shows_shortcut_for_unfocused_waiting_bridge(
    tmp_path: Path,
) -> None:
    repository = SQLiteStateRepository(tmp_path / "state.db")
    progress_store = SQLiteTelegramProgressStore(tmp_path / "state.db")
    await repository.initialize()
    await progress_store.initialize()
    await progress_store.save_status_card(
        "chat:1",
        chat_id=1,
        topic_id=None,
        message_id=77,
        rendered_text="old",
    )

    waiting = LogicalThread(
        thread_id="22222222",
        chat_key="chat:1",
        title="Previous focused conversation",
        codex_thread_id=None,
        created_at="now",
        updated_at="now",
        turn_count=1,
        awaiting_reply=False,
        interrupted_notice=False,
        pending_turn_id=None,
        codex_backend_id="primary",
        anchor_id=None,
        expires_at="2999-01-01T00:00:00+00:00",
    )
    focused = SimpleNamespace(
        bridge_id="11111111",
        title="New conversation",
        codex_backend_id="primary",
        codex_thread_id=None,
    )
    service = AsyncMock()
    service.list_conversations.return_value = []
    service.list_threads.return_value = [waiting]
    service.ensure_focused_bridge.return_value = focused
    service.runtime_state_for_thread.return_value = CodexRuntimeState()
    bot = AsyncMock()
    bot.get_chat.return_value = SimpleNamespace(
        pinned_message=SimpleNamespace(message_id=77)
    )
    runner = TelegramBotRunner(
        bot,
        Dispatcher(),
        service,
        repository,
        progress_store,
        None,
        False,
    )

    await runner._sync_thread_status_card(
        ChatContext(chat_key="chat:1", chat_id=1, topic_id=None)
    )

    text = bot.edit_message_text.await_args.kwargs["text"]
    assert "<b>[f]:</b> New conversation" in text
    assert "<b>(w)</b> /ct_22222222 Previous focused conversation" in text
    assert "/ct_11111111" not in text


def _pending_user_input(
    *, questions: tuple[UserInputQuestion, ...] | None = None
) -> PendingUserInput:
    return PendingUserInput(
        request_id=9,
        chat_key="chat:1",
        logical_thread_id="thread-1",
        codex_thread_id="codex-1",
        turn_id="turn-1",
        method="request_user_input",
        questions=questions
        or (
            UserInputQuestion(
                question_id="scope",
                header="Scope",
                question="Which scope?",
                options=(
                    UserInputOption(label="Native first", description="Use native."),
                    UserInputOption(label="MCP shim", description="Use MCP."),
                ),
            ),
        ),
    )


@pytest.mark.asyncio
async def test_threads_command_keeps_listing_under_telegram_limit(
    tmp_path: Path,
) -> None:
    repository = SQLiteStateRepository(tmp_path / "state.db")
    progress_store = SQLiteTelegramProgressStore(tmp_path / "state.db")
    await repository.initialize()
    await progress_store.initialize()

    service = AsyncMock()
    codex_threads = CodexThreadListResult(
        groups=[
            CodexThreadGroup(
                project="/agent/large-project",
                threads=[
                    CodexThread(
                        thread_id=f"codex-{index:02d}",
                        cwd="/agent/large-project",
                        title="Investigate Telegram thread listing overflow " * 8,
                        preview=None,
                        status="idle",
                        created_at=1710000000 + index,
                        updated_at=1710000300 + index,
                        model_provider="openai",
                    )
                    for index in range(50)
                ],
            )
        ],
        failures=[],
    )

    async def _list_codex_threads(*args: object, **kwargs: object) -> object:
        await asyncio.sleep(0)
        return codex_threads

    service.list_codex_threads.side_effect = _list_codex_threads
    bot = AsyncMock()
    runner = TelegramBotRunner(
        bot,
        Dispatcher(),
        service,
        repository,
        progress_store,
        None,
        False,
    )

    handled = await runner._handle_command(
        ChatContext(chat_key="chat:1", chat_id=1, topic_id=None),
        "thread-1",
        ("threads", "large"),
        "/threads large",
    )

    assert handled is True
    bot.send_message.assert_awaited_once()
    sent_text = bot.send_message.await_args.kwargs["text"]
    assert len(sent_text) <= 4096
    assert "more threads not shown" in sent_text
    assert "reply_markup" not in bot.send_message.await_args.kwargs
    assert "/ct_" in sent_text
    assert "/attach_thread" not in sent_text
    assert len(re.findall(r"/ct_[0-9a-f]{8}\b", sent_text)) == 3
    bot.send_chat_action.assert_awaited_once_with(
        chat_id=1,
        action=ChatAction.TYPING,
        message_thread_id=None,
    )


@pytest.mark.asyncio
async def test_threads_without_arguments_shows_connection_picker(
    tmp_path: Path,
) -> None:
    repository = SQLiteStateRepository(tmp_path / "state.db")
    progress_store = SQLiteTelegramProgressStore(tmp_path / "state.db")
    await repository.initialize()
    await progress_store.initialize()

    service = AsyncMock()
    connections = [
        SimpleNamespace(connection_id="primary", label="Home"),
        SimpleNamespace(connection_id="laptop", label="Laptop"),
    ]

    async def _list_backend_connections() -> object:
        await asyncio.sleep(0)
        return connections

    service.list_backend_connections.side_effect = _list_backend_connections
    bot = AsyncMock()
    runner = TelegramBotRunner(
        bot,
        Dispatcher(),
        service,
        repository,
        progress_store,
        None,
        False,
    )

    handled = await runner._handle_command(
        ChatContext(chat_key="chat:1", chat_id=1, topic_id=None),
        "thread-1",
        ("threads", ""),
        "/threads",
    )

    assert handled is True
    service.list_backend_connections.assert_awaited_once_with()
    sent = bot.send_message.await_args.kwargs
    assert sent["text"] == "Choose a connection."
    labels = [
        button.text for row in sent["reply_markup"].inline_keyboard for button in row
    ]
    assert labels == [
        "Recent",
        "Recent projects",
        "Connection: primary",
        "Connection: laptop",
    ]
    bot.send_chat_action.assert_awaited_once_with(
        chat_id=1,
        action=ChatAction.TYPING,
        message_thread_id=None,
    )


@pytest.mark.asyncio
async def test_codex_threads_recent_callback_lists_recent_threads(
    tmp_path: Path,
) -> None:
    repository = SQLiteStateRepository(tmp_path / "state.db")
    progress_store = SQLiteTelegramProgressStore(tmp_path / "state.db")
    await repository.initialize()
    await progress_store.initialize()

    service = AsyncMock()
    service.list_backend_connections.return_value = [
        SimpleNamespace(connection_id="primary", label="Home")
    ]
    service.list_recent_codex_threads.return_value = CodexThreadListResult(
        groups=[
            CodexThreadGroup(
                project="/agent/app",
                threads=[
                    CodexThread(
                        thread_id="codex-1",
                        cwd="/agent/app",
                        title="Recent thread",
                        preview=None,
                        status="idle",
                        created_at=1710000000,
                        updated_at=1710000300,
                        model_provider="openai",
                        codex_backend_id="mac",
                        codex_backend_name="Mac",
                    )
                ],
            )
        ],
        failures=[],
    )
    bot = AsyncMock()
    runner = TelegramBotRunner(
        bot,
        Dispatcher(),
        service,
        repository,
        progress_store,
        None,
        False,
    )

    await runner._handle_command(
        ChatContext(chat_key="chat:1", chat_id=1, topic_id=None),
        "thread-1",
        ("threads", ""),
        "/threads",
    )
    recent_callback = (
        bot.send_message.await_args.kwargs["reply_markup"]
        .inline_keyboard[0][0]
        .callback_data
    )
    callback = SimpleNamespace(
        data=recent_callback,
        message=SimpleNamespace(
            chat=SimpleNamespace(id=1),
            message_thread_id=None,
            message_id=55,
        ),
        answer=AsyncMock(),
    )
    bot.send_message.reset_mock()

    await runner._on_callback_query(cast(CallbackQuery, callback))

    service.list_recent_codex_threads.assert_awaited_once_with(
        "chat:1",
        limit=6,
    )
    sent = bot.send_message.await_args.kwargs
    assert sent["parse_mode"] == "HTML"
    assert "Recent thread" in sent["text"]
    assert "Mac" in sent["text"]
    assert "<b>Backend</b>" not in sent["text"]
    assert re.search(r"/ct_[0-9a-f]{8}\b", sent["text"]) is not None
    assert "reply_markup" not in sent


@pytest.mark.asyncio
async def test_codex_threads_recent_callback_more_expands_to_twenty(
    tmp_path: Path,
) -> None:
    repository = SQLiteStateRepository(tmp_path / "state.db")
    progress_store = SQLiteTelegramProgressStore(tmp_path / "state.db")
    await repository.initialize()
    await progress_store.initialize()
    token = await repository.create_callback_token(
        chat_key="chat:1",
        topic_id=None,
        action="codex_threads_recent",
        payload={},
        expires_at="2999-01-01T00:00:00+00:00",
    )

    def thread(index: int) -> CodexThread:
        return CodexThread(
            thread_id=f"codex-{index}",
            cwd="/agent/app",
            title=f"Thread {index}",
            preview=None,
            status="idle",
            created_at=1710000000 + index,
            updated_at=1710000300 + index,
            model_provider="openai",
            codex_backend_id="mac",
            codex_backend_name="Mac",
        )

    service = AsyncMock()
    service.list_recent_codex_threads.side_effect = [
        CodexThreadListResult(
            groups=[
                CodexThreadGroup(
                    project="/agent/app",
                    threads=[thread(index) for index in range(6)],
                )
            ],
            failures=[],
        ),
        CodexThreadListResult(
            groups=[
                CodexThreadGroup(
                    project="/agent/app",
                    threads=[thread(index) for index in range(20)],
                )
            ],
            failures=[],
        ),
    ]
    bot = AsyncMock()
    runner = TelegramBotRunner(
        bot,
        Dispatcher(),
        service,
        repository,
        progress_store,
        None,
        False,
    )
    callback = SimpleNamespace(
        data=f"ct:{token}",
        message=SimpleNamespace(
            chat=SimpleNamespace(id=1),
            message_thread_id=None,
            message_id=55,
        ),
        answer=AsyncMock(),
    )

    await runner._on_callback_query(cast(CallbackQuery, callback))

    service.list_recent_codex_threads.assert_awaited_once_with(
        "chat:1",
        limit=6,
    )
    compact = bot.send_message.await_args.kwargs
    assert "Thread 4" in compact["text"]
    assert "Thread 5" not in compact["text"]
    more_button = compact["reply_markup"].inline_keyboard[0][0]
    assert more_button.text == "More"

    callback.data = more_button.callback_data
    bot.send_message.reset_mock()
    service.list_recent_codex_threads.reset_mock()

    await runner._on_callback_query(cast(CallbackQuery, callback))

    service.list_recent_codex_threads.assert_awaited_once_with(
        "chat:1",
        limit=20,
    )
    expanded = bot.send_message.await_args.kwargs
    assert "Thread 19" in expanded["text"]
    assert "reply_markup" not in expanded


@pytest.mark.asyncio
async def test_codex_threads_connection_callback_lists_recent_projects(
    tmp_path: Path,
) -> None:
    repository = SQLiteStateRepository(tmp_path / "state.db")
    progress_store = SQLiteTelegramProgressStore(tmp_path / "state.db")
    await repository.initialize()
    await progress_store.initialize()
    token = await repository.create_callback_token(
        chat_key="chat:1",
        topic_id=None,
        action="codex_threads_connection",
        payload={"connection_id": "laptop", "connection_label": "Laptop"},
        expires_at="2999-01-01T00:00:00+00:00",
    )

    service = AsyncMock()
    service.list_recent_projects.return_value = [
        Project(
            project_id="project-1",
            connection_id="laptop",
            root_path="/agent/app",
            label="app",
            created_at="now",
            updated_at="now",
        )
    ]
    bot = AsyncMock()
    runner = TelegramBotRunner(
        bot,
        Dispatcher(),
        service,
        repository,
        progress_store,
        None,
        False,
    )
    callback = SimpleNamespace(
        data=f"ct:{token}",
        message=SimpleNamespace(
            chat=SimpleNamespace(id=1),
            message_thread_id=None,
            message_id=55,
        ),
        answer=AsyncMock(),
    )

    await runner._on_callback_query(cast(CallbackQuery, callback))

    service.list_recent_projects.assert_awaited_once_with(
        chat_key="chat:1",
        connection_id="laptop",
        include_all=False,
        limit=6,
    )
    sent = bot.send_message.await_args.kwargs
    assert sent["text"] == "Choose a project on Laptop."
    labels = [
        button.text for row in sent["reply_markup"].inline_keyboard for button in row
    ]
    assert labels == ["app", "All projects"]


@pytest.mark.asyncio
async def test_codex_threads_project_callback_lists_recent_threads(
    tmp_path: Path,
) -> None:
    repository = SQLiteStateRepository(tmp_path / "state.db")
    progress_store = SQLiteTelegramProgressStore(tmp_path / "state.db")
    await repository.initialize()
    await progress_store.initialize()
    token = await repository.create_callback_token(
        chat_key="chat:1",
        topic_id=None,
        action="codex_threads_project",
        payload={"connection_id": "laptop", "project_id": "project-1"},
        expires_at="2999-01-01T00:00:00+00:00",
    )

    service = AsyncMock()
    service.list_codex_threads.return_value = CodexThreadListResult(
        groups=[
            CodexThreadGroup(
                project="/agent/app",
                backend_id="laptop",
                backend_name="Laptop",
                threads=[
                    CodexThread(
                        thread_id=f"codex-{index}",
                        cwd="/agent/app",
                        title=f"Thread {index}",
                        preview=None,
                        status="idle",
                        created_at=1710000000 + index,
                        updated_at=1710000300 + index,
                        model_provider="openai",
                        codex_backend_id="laptop",
                        codex_backend_name="Laptop",
                    )
                    for index in range(5)
                ],
            )
        ],
        failures=[],
    )
    bot = AsyncMock()
    runner = TelegramBotRunner(
        bot,
        Dispatcher(),
        service,
        repository,
        progress_store,
        None,
        False,
    )
    callback = SimpleNamespace(
        data=f"ct:{token}",
        message=SimpleNamespace(
            chat=SimpleNamespace(id=1),
            message_thread_id=None,
            message_id=55,
        ),
        answer=AsyncMock(),
    )

    await runner._on_callback_query(cast(CallbackQuery, callback))

    service.list_codex_threads.assert_awaited_once_with(
        "chat:1",
        backend_id="laptop",
        include_all=False,
        project_id="project-1",
        limit=6,
    )
    sent = bot.send_message.await_args.kwargs
    assert sent["parse_mode"] == "HTML"
    assert "Thread 4" in sent["text"]
    assert re.search(r"/ct_[0-9a-f]{8}\b", sent["text"]) is not None
    assert "reply_markup" not in sent


@pytest.mark.asyncio
async def test_overview_command_creates_and_reuses_status_card(tmp_path: Path) -> None:
    repository = SQLiteStateRepository(tmp_path / "state.db")
    progress_store = SQLiteTelegramProgressStore(tmp_path / "state.db")
    await repository.initialize()
    await progress_store.initialize()

    focused = ConversationAnchor(
        anchor_id="anchor-a",
        chat_key="chat:1",
        codex_backend_id="primary",
        codex_thread_id="codex-a",
        title="Focused",
        alias=None,
        project_id=None,
        latest_bridge_id="bridge-a",
        created_at="now",
        updated_at="now",
    )
    service = AsyncMock()
    service.list_conversations.return_value = [focused]
    service.ensure_focused_bridge.return_value = SimpleNamespace(bridge_id="bridge-a")
    bot = AsyncMock()
    bot.send_message.return_value.message_id = 77
    runner = TelegramBotRunner(
        bot,
        Dispatcher(),
        service,
        repository,
        progress_store,
        None,
        False,
    )
    context = ChatContext(chat_key="chat:1", chat_id=1, topic_id=None)

    handled = await runner._handle_command(
        context,
        "bridge-a",
        ("overview", ""),
        "/overview",
    )

    assert handled is True
    bot.send_message.assert_awaited_once()
    labels = [
        button.text
        for row in bot.send_message.await_args.kwargs["reply_markup"].inline_keyboard
        for button in row
    ]
    assert labels == ["Threads", "Projects"]
    bot.pin_chat_message.assert_awaited_once_with(
        chat_id=1,
        message_id=77,
        disable_notification=True,
    )
    card = await progress_store.get_status_card("chat:1")
    assert card is not None
    assert card.message_id == 77

    bot.get_chat.return_value = SimpleNamespace(
        pinned_message=SimpleNamespace(message_id=77)
    )
    bot.send_message.reset_mock()
    bot.pin_chat_message.reset_mock()
    handled = await runner._handle_command(
        context,
        "bridge-a",
        ("overview", ""),
        "/overview",
    )

    assert handled is True
    bot.send_message.assert_not_awaited()
    bot.edit_message_text.assert_not_awaited()
    bot.unpin_chat_message.assert_not_awaited()
    bot.pin_chat_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_threads_command_opens_connection_picker(
    tmp_path: Path,
) -> None:
    repository = SQLiteStateRepository(tmp_path / "state.db")
    progress_store = SQLiteTelegramProgressStore(tmp_path / "state.db")
    await repository.initialize()
    await progress_store.initialize()

    service = AsyncMock()
    service.list_backend_connections.return_value = [
        SimpleNamespace(connection_id="primary", label="Home")
    ]
    bot = AsyncMock()
    runner = TelegramBotRunner(
        bot,
        Dispatcher(),
        service,
        repository,
        progress_store,
        None,
        False,
    )

    handled = await runner._handle_command(
        ChatContext(chat_key="chat:1", chat_id=1, topic_id=None),
        "bridge-focused",
        ("threads", ""),
        "/threads",
    )

    assert handled is True
    service.list_backend_connections.assert_awaited_once_with()
    sent = bot.send_message.await_args.kwargs
    assert sent["text"] == "Choose a connection."
    labels = [
        button.text for row in sent["reply_markup"].inline_keyboard for button in row
    ]
    assert labels == ["Recent", "Recent projects", "Connection: primary"]


@pytest.mark.asyncio
async def test_overview_status_card_keeps_high_level_actions_when_default_project_exists(
    tmp_path: Path,
) -> None:
    repository = SQLiteStateRepository(tmp_path / "state.db")
    progress_store = SQLiteTelegramProgressStore(tmp_path / "state.db")
    await repository.initialize()
    await progress_store.initialize()

    focused = ConversationAnchor(
        anchor_id="anchor-a",
        chat_key="chat:1",
        codex_backend_id="primary",
        codex_thread_id="codex-a",
        title="Focused",
        alias=None,
        project_id=None,
        latest_bridge_id="bridge-a",
        created_at="now",
        updated_at="now",
    )
    service = AsyncMock()
    service.has_default_project.return_value = True
    service.list_conversations.return_value = [focused]
    service.ensure_focused_bridge.return_value = SimpleNamespace(bridge_id="bridge-a")
    service.runtime_state_for_thread.return_value = CodexRuntimeState()
    bot = AsyncMock()
    bot.send_message.return_value.message_id = 77
    runner = TelegramBotRunner(
        bot,
        Dispatcher(),
        service,
        repository,
        progress_store,
        None,
        False,
    )

    handled = await runner._handle_command(
        ChatContext(chat_key="chat:1", chat_id=1, topic_id=None),
        "bridge-a",
        ("overview", ""),
        "/overview",
    )

    assert handled is True
    labels = [
        button.text
        for row in bot.send_message.await_args.kwargs["reply_markup"].inline_keyboard
        for button in row
    ]
    assert labels == ["Threads", "Projects"]


@pytest.mark.asyncio
async def test_overview_replacement_unpins_previous_status_card(tmp_path: Path) -> None:
    repository = SQLiteStateRepository(tmp_path / "state.db")
    progress_store = SQLiteTelegramProgressStore(tmp_path / "state.db")
    await repository.initialize()
    await progress_store.initialize()
    await progress_store.save_status_card(
        "chat:1",
        chat_id=1,
        topic_id=None,
        message_id=77,
        rendered_text="old",
    )

    focused = ConversationAnchor(
        anchor_id="anchor-a",
        chat_key="chat:1",
        codex_backend_id="primary",
        codex_thread_id="codex-a",
        title="Focused",
        alias=None,
        project_id=None,
        latest_bridge_id="bridge-a",
        created_at="now",
        updated_at="now",
    )
    service = AsyncMock()
    service.list_conversations.return_value = [focused]
    service.ensure_focused_bridge.return_value = SimpleNamespace(bridge_id="bridge-a")
    bot = AsyncMock()
    bot.edit_message_text.side_effect = RuntimeError("message to edit not found")
    bot.send_message.return_value.message_id = 88
    runner = TelegramBotRunner(
        bot,
        Dispatcher(),
        service,
        repository,
        progress_store,
        None,
        False,
    )

    handled = await runner._handle_command(
        ChatContext(chat_key="chat:1", chat_id=1, topic_id=None),
        "bridge-a",
        ("overview", ""),
        "/overview",
    )

    assert handled is True
    bot.unpin_chat_message.assert_awaited_once_with(chat_id=1, message_id=77)
    bot.pin_chat_message.assert_awaited_once_with(
        chat_id=1,
        message_id=88,
        disable_notification=True,
    )
    card = await progress_store.get_status_card("chat:1")
    assert card is not None
    assert card.message_id == 88


@pytest.mark.asyncio
async def test_overview_updates_current_pinned_status_card(tmp_path: Path) -> None:
    repository = SQLiteStateRepository(tmp_path / "state.db")
    progress_store = SQLiteTelegramProgressStore(tmp_path / "state.db")
    await repository.initialize()
    await progress_store.initialize()
    await progress_store.save_status_card(
        "chat:1",
        chat_id=1,
        topic_id=None,
        message_id=77,
        rendered_text="old",
    )

    focused = ConversationAnchor(
        anchor_id="anchor-a",
        chat_key="chat:1",
        codex_backend_id="primary",
        codex_thread_id="codex-a",
        title="Focused",
        alias=None,
        project_id=None,
        latest_bridge_id="bridge-a",
        created_at="now",
        updated_at="now",
    )
    service = AsyncMock()
    service.list_conversations.return_value = [focused]
    service.ensure_focused_bridge.return_value = SimpleNamespace(bridge_id="bridge-a")
    bot = AsyncMock()
    bot.get_chat.return_value = SimpleNamespace(
        pinned_message=SimpleNamespace(message_id=66)
    )
    runner = TelegramBotRunner(
        bot,
        Dispatcher(),
        service,
        repository,
        progress_store,
        None,
        False,
    )

    handled = await runner._handle_command(
        ChatContext(chat_key="chat:1", chat_id=1, topic_id=None),
        "bridge-a",
        ("overview", ""),
        "/overview",
    )

    assert handled is True
    bot.edit_message_text.assert_awaited_once()
    assert bot.edit_message_text.await_args.kwargs["message_id"] == 66
    bot.send_message.assert_not_awaited()
    bot.unpin_chat_message.assert_awaited_once_with(chat_id=1, message_id=66)
    bot.pin_chat_message.assert_awaited_once_with(
        chat_id=1,
        message_id=66,
        disable_notification=True,
    )
    card = await progress_store.get_status_card("chat:1")
    assert card is not None
    assert card.message_id == 66


@pytest.mark.asyncio
async def test_status_card_status_change_does_not_repin_current_pinned_card(
    tmp_path: Path,
) -> None:
    repository = SQLiteStateRepository(tmp_path / "state.db")
    progress_store = SQLiteTelegramProgressStore(tmp_path / "state.db")
    await repository.initialize()
    await progress_store.initialize()
    await progress_store.save_status_card(
        "chat:1",
        chat_id=1,
        topic_id=None,
        message_id=77,
        rendered_text="<b>[f]:</b> Focused\n\n<b>(r)</b> /ct_bg Background",
    )

    focused = ConversationAnchor(
        anchor_id="anchor-a",
        chat_key="chat:1",
        codex_backend_id="primary",
        codex_thread_id="codex-a",
        title="Focused",
        alias=None,
        project_id=None,
        latest_bridge_id="bridge-a",
        created_at="now",
        updated_at="now",
    )
    waiting = ConversationAnchor(
        anchor_id="anchor-b",
        chat_key="chat:1",
        codex_backend_id="primary",
        codex_thread_id="codex-b",
        title="Background",
        alias=None,
        project_id=None,
        latest_bridge_id="bridge-b",
        created_at="now",
        updated_at="now",
        latest_bridge_expires_at="2999-01-01T00:00:00+00:00",
    )
    service = AsyncMock()
    service.list_conversations.return_value = [focused, waiting]
    service.ensure_focused_bridge.return_value = SimpleNamespace(bridge_id="bridge-a")
    service.runtime_state_for_thread.return_value = CodexRuntimeState()
    bot = AsyncMock()
    bot.get_chat.return_value = SimpleNamespace(
        pinned_message=SimpleNamespace(message_id=77)
    )
    runner = TelegramBotRunner(
        bot,
        Dispatcher(),
        service,
        repository,
        progress_store,
        None,
        False,
    )

    await runner._sync_thread_status_card(
        ChatContext(chat_key="chat:1", chat_id=1, topic_id=None)
    )

    bot.edit_message_text.assert_awaited_once()
    text = bot.edit_message_text.await_args.kwargs["text"]
    assert text.startswith("<b>[f]:</b> Focused\n\n")
    assert "<b>(w)</b>" in text
    bot.unpin_chat_message.assert_not_awaited()
    bot.pin_chat_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_overview_pins_stored_status_card_when_nothing_is_pinned(
    tmp_path: Path,
) -> None:
    repository = SQLiteStateRepository(tmp_path / "state.db")
    progress_store = SQLiteTelegramProgressStore(tmp_path / "state.db")
    await repository.initialize()
    await progress_store.initialize()
    await progress_store.save_status_card(
        "chat:1",
        chat_id=1,
        topic_id=None,
        message_id=77,
        rendered_text="old",
    )

    focused = ConversationAnchor(
        anchor_id="anchor-a",
        chat_key="chat:1",
        codex_backend_id="primary",
        codex_thread_id="codex-a",
        title="Focused",
        alias=None,
        project_id=None,
        latest_bridge_id="bridge-a",
        created_at="now",
        updated_at="now",
    )
    service = AsyncMock()
    service.list_conversations.return_value = [focused]
    service.ensure_focused_bridge.return_value = SimpleNamespace(bridge_id="bridge-a")
    bot = AsyncMock()
    bot.get_chat.return_value = SimpleNamespace(pinned_message=None)
    runner = TelegramBotRunner(
        bot,
        Dispatcher(),
        service,
        repository,
        progress_store,
        None,
        False,
    )

    handled = await runner._handle_command(
        ChatContext(chat_key="chat:1", chat_id=1, topic_id=None),
        "bridge-a",
        ("overview", ""),
        "/overview",
    )

    assert handled is True
    bot.edit_message_text.assert_awaited_once()
    assert bot.edit_message_text.await_args.kwargs["message_id"] == 77
    bot.send_message.assert_not_awaited()
    bot.pin_chat_message.assert_awaited_once_with(
        chat_id=1,
        message_id=77,
        disable_notification=True,
    )


@pytest.mark.asyncio
async def test_overview_rotates_stale_pinned_status_card(tmp_path: Path) -> None:
    db_path = tmp_path / "state.db"
    repository = SQLiteStateRepository(db_path)
    progress_store = SQLiteTelegramProgressStore(db_path)
    await repository.initialize()
    await progress_store.initialize()
    await progress_store.save_status_card(
        "chat:1",
        chat_id=1,
        topic_id=None,
        message_id=77,
        rendered_text="old",
    )
    with sqlite3.connect(db_path) as db:
        db.execute("""
            UPDATE telegram_status_cards
               SET updated_at = '2026-05-05T00:00:00+00:00'
             WHERE chat_key = 'chat:1'
            """)

    focused = ConversationAnchor(
        anchor_id="anchor-a",
        chat_key="chat:1",
        codex_backend_id="primary",
        codex_thread_id="codex-a",
        title="Focused",
        alias=None,
        project_id=None,
        latest_bridge_id="bridge-a",
        created_at="now",
        updated_at="now",
    )
    service = AsyncMock()
    service.list_conversations.return_value = [focused]
    service.ensure_focused_bridge.return_value = SimpleNamespace(bridge_id="bridge-a")
    service.runtime_state_for_thread.return_value = CodexRuntimeState()
    bot = AsyncMock()
    bot.get_chat.return_value = SimpleNamespace(
        pinned_message=SimpleNamespace(message_id=77)
    )
    bot.send_message.return_value.message_id = 88
    runner = TelegramBotRunner(
        bot,
        Dispatcher(),
        service,
        repository,
        progress_store,
        None,
        False,
    )

    handled = await runner._handle_command(
        ChatContext(chat_key="chat:1", chat_id=1, topic_id=None),
        "bridge-a",
        ("overview", ""),
        "/overview",
    )

    assert handled is True
    bot.edit_message_text.assert_not_awaited()
    bot.unpin_chat_message.assert_awaited_once_with(chat_id=1, message_id=77)
    bot.pin_chat_message.assert_awaited_once_with(
        chat_id=1,
        message_id=88,
        disable_notification=True,
    )
    card = await progress_store.get_status_card("chat:1")
    assert card is not None
    assert card.message_id == 88


@pytest.mark.asyncio
async def test_final_reply_self_seeds_status_card(tmp_path: Path) -> None:
    repository = SQLiteStateRepository(tmp_path / "state.db")
    progress_store = SQLiteTelegramProgressStore(tmp_path / "state.db")
    await repository.initialize()
    await progress_store.initialize()

    focused = ConversationAnchor(
        anchor_id="anchor-a",
        chat_key="chat:1",
        codex_backend_id="primary",
        codex_thread_id="codex-a",
        title="Fix overview card",
        alias=None,
        project_id=None,
        latest_bridge_id="bridge-a",
        created_at="now",
        updated_at="now",
    )
    service = AsyncMock()
    service.list_conversations.return_value = [focused]
    service.ensure_focused_bridge.return_value = SimpleNamespace(bridge_id="bridge-a")
    bot = AsyncMock()
    bot.send_message.side_effect = [
        SimpleNamespace(message_id=88),
        SimpleNamespace(message_id=99),
    ]
    runner = TelegramBotRunner(
        bot,
        Dispatcher(),
        service,
        repository,
        progress_store,
        None,
        False,
    )

    await runner._finish_run_result(
        ChatContext(chat_key="chat:1", chat_id=1, topic_id=None),
        TurnRunResult(
            result=TurnResult(
                turn_id="turn-1",
                chat_key="chat:1",
                logical_thread_id="bridge-a",
                codex_thread_id="codex-a",
                status="completed",
                final_text="done",
            ),
            remapped=False,
            remap_warning=None,
        ),
    )

    sent_texts = [call.kwargs["text"] for call in bot.send_message.await_args_list]
    assert sent_texts[0] == "✅ done"
    assert "Fix overview card" in sent_texts[1]
    bot.pin_chat_message.assert_awaited_once_with(
        chat_id=1,
        message_id=99,
        disable_notification=True,
    )
    card = await progress_store.get_status_card("chat:1")
    assert card is not None
    assert card.message_id == 99


@pytest.mark.asyncio
async def test_first_turn_progress_refreshes_status_card_title(tmp_path: Path) -> None:
    repository = SQLiteStateRepository(tmp_path / "state.db")
    progress_store = SQLiteTelegramProgressStore(tmp_path / "state.db")
    await repository.initialize()
    await progress_store.initialize()
    await progress_store.save_status_card(
        "chat:1",
        chat_id=1,
        topic_id=None,
        message_id=77,
        rendered_text="<b>[f]:</b> New conversation",
    )

    focused = ConversationAnchor(
        anchor_id="anchor-a",
        chat_key="chat:1",
        codex_backend_id="mac",
        codex_thread_id="codex-a",
        title="What is the service status?",
        alias=None,
        project_id=None,
        latest_bridge_id="bridge-a",
        created_at="now",
        updated_at="now",
    )
    service = AsyncMock()
    service.list_conversations.return_value = [focused]
    service.ensure_focused_bridge.return_value = SimpleNamespace(bridge_id="bridge-a")
    bot = AsyncMock()
    bot.send_message.return_value = SimpleNamespace(message_id=88)
    runner = TelegramBotRunner(
        bot,
        Dispatcher(),
        service,
        repository,
        progress_store,
        None,
        False,
    )

    await runner._handle_turn_update(
        ChatContext(chat_key="chat:1", chat_id=1, topic_id=None),
        TurnUpdate(
            turn_id="turn-1",
            chat_key="chat:1",
            logical_thread_id="bridge-a",
            codex_thread_id="codex-a",
            status="running",
            text="Checking service status.\n",
            source="agent",
        ),
    )

    bot.edit_message_text.assert_awaited_once()
    assert bot.edit_message_text.await_args.kwargs["message_id"] == 77
    assert (
        "What is the service status?" in bot.edit_message_text.await_args.kwargs["text"]
    )
    bot.send_message.assert_awaited_once()


@pytest.mark.asyncio
async def test_realtime_command_starts_session(tmp_path: Path) -> None:
    repository = SQLiteStateRepository(tmp_path / "state.db")
    progress_store = SQLiteTelegramProgressStore(tmp_path / "state.db")
    await repository.initialize()
    await progress_store.initialize()

    service = AsyncMock()
    service.start_realtime.return_value = RealtimeStartResult(
        session=RealtimeSession(
            chat_key="chat:1",
            logical_thread_id="thread-1",
            codex_thread_id="codex-1",
            codex_backend_id="primary",
        )
    )
    service.wait_for_realtime_event.side_effect = [
        RealtimeEvent(
            event_type="started",
            chat_key="chat:1",
            logical_thread_id="thread-1",
            codex_thread_id="codex-1",
            codex_backend_id="primary",
            session_id="sess-1",
        ),
        ValueError("Realtime mode is not active for this conversation."),
    ]
    bot = AsyncMock()
    runner = TelegramBotRunner(
        bot,
        Dispatcher(),
        service,
        repository,
        progress_store,
        None,
        False,
    )

    handled = await runner._handle_command(
        ChatContext(chat_key="chat:1", chat_id=1, topic_id=None),
        "thread-1",
        ("realtime", ""),
        "/realtime",
    )

    assert handled is True
    service.start_realtime.assert_awaited_once_with("chat:1", thread_id="thread-1")
    assert "Realtime mode started" in bot.send_message.await_args.kwargs["text"]


@pytest.mark.asyncio
async def test_realtime_command_reports_initial_error_before_started(
    tmp_path: Path,
) -> None:
    repository = SQLiteStateRepository(tmp_path / "state.db")
    progress_store = SQLiteTelegramProgressStore(tmp_path / "state.db")
    await repository.initialize()
    await progress_store.initialize()

    service = AsyncMock()
    service.start_realtime.return_value = RealtimeStartResult(
        session=RealtimeSession(
            chat_key="chat:1",
            logical_thread_id="thread-1",
            codex_thread_id="codex-1",
            codex_backend_id="primary",
        )
    )
    service.wait_for_realtime_event.return_value = RealtimeEvent(
        event_type="error",
        chat_key="chat:1",
        logical_thread_id="thread-1",
        codex_thread_id="codex-1",
        codex_backend_id="primary",
        text="backend rejected realtime startup",
    )
    bot = AsyncMock()
    runner = TelegramBotRunner(
        bot,
        Dispatcher(),
        service,
        repository,
        progress_store,
        None,
        False,
    )

    handled = await runner._handle_command(
        ChatContext(chat_key="chat:1", chat_id=1, topic_id=None),
        "thread-1",
        ("realtime", ""),
        "/realtime",
    )

    assert handled is True
    sent_text = bot.send_message.await_args.kwargs["text"]
    assert "Realtime failed: backend rejected realtime startup" in sent_text
    assert "Realtime mode started" not in sent_text


@pytest.mark.asyncio
async def test_realtime_stop_command_stops_session(tmp_path: Path) -> None:
    repository = SQLiteStateRepository(tmp_path / "state.db")
    progress_store = SQLiteTelegramProgressStore(tmp_path / "state.db")
    await repository.initialize()
    await progress_store.initialize()

    service = AsyncMock()
    service.stop_realtime.return_value = "Realtime mode stopped."
    bot = AsyncMock()
    runner = TelegramBotRunner(
        bot,
        Dispatcher(),
        service,
        repository,
        progress_store,
        None,
        False,
    )

    handled = await runner._handle_command(
        ChatContext(chat_key="chat:1", chat_id=1, topic_id=None),
        "thread-1",
        ("realtime", "stop"),
        "/realtime stop",
    )

    assert handled is True
    service.stop_realtime.assert_awaited_once_with("chat:1", thread_id="thread-1")
    assert bot.send_message.await_args.kwargs["text"] == "Realtime mode stopped."


@pytest.mark.asyncio
async def test_codex_threads_default_lists_three_recent_threads_with_formatting(
    tmp_path: Path,
) -> None:
    repository = SQLiteStateRepository(tmp_path / "state.db")
    progress_store = SQLiteTelegramProgressStore(tmp_path / "state.db")
    await repository.initialize()
    await progress_store.initialize()

    service = AsyncMock()
    service.list_codex_threads.return_value = CodexThreadListResult(
        groups=[
            CodexThreadGroup(
                project="/agent",
                threads=[
                    CodexThread(
                        thread_id=f"codex-{index}",
                        cwd="/agent",
                        title=f"Recent thread {index}",
                        preview=None,
                        status="notLoaded",
                        created_at=1710000000 + index,
                        updated_at=1710000300 + index,
                        model_provider="openai",
                        anchor_status="focused",
                    )
                    for index in range(3)
                ],
            )
        ],
        failures=[],
    )
    bot = AsyncMock()
    runner = TelegramBotRunner(
        bot,
        Dispatcher(),
        service,
        repository,
        progress_store,
        None,
        False,
    )

    handled = await runner._handle_command(
        ChatContext(chat_key="chat:1", chat_id=1, topic_id=None),
        "thread-1",
        ("threads", "project"),
        "/threads project",
    )

    assert handled is True
    service.list_codex_threads.assert_awaited_once_with(
        "chat:1",
        backend_name=None,
        include_all=False,
        search="project",
        limit=50,
    )
    sent = bot.send_message.await_args.kwargs
    assert sent["parse_mode"] == "HTML"
    assert len(re.findall(r"/ct_[0-9a-f]{8}\b", sent["text"])) == 3
    assert "<b>Recent thread 0</b>" in sent["text"]
    assert "notLoaded" not in sent["text"]
    assert "codex-0" not in sent["text"]
    assert "/threads --full" in sent["text"]


@pytest.mark.asyncio
async def test_codex_threads_default_lists_three_threads_per_project(
    tmp_path: Path,
) -> None:
    repository = SQLiteStateRepository(tmp_path / "state.db")
    progress_store = SQLiteTelegramProgressStore(tmp_path / "state.db")
    await repository.initialize()
    await progress_store.initialize()

    service = AsyncMock()
    service.list_codex_threads.return_value = CodexThreadListResult(
        groups=[
            CodexThreadGroup(
                project=f"/agent/project-{project_index}",
                threads=[
                    CodexThread(
                        thread_id=f"codex-{project_index}-{thread_index}",
                        cwd=f"/agent/project-{project_index}",
                        title=f"Project {project_index} thread {thread_index}",
                        preview=None,
                        status="notLoaded",
                        created_at=1710000000 + thread_index,
                        updated_at=1710000300 + thread_index,
                        model_provider="openai",
                        anchor_status="unlinked",
                    )
                    for thread_index in range(4)
                ],
            )
            for project_index in range(2)
        ],
        failures=[],
    )
    bot = AsyncMock()
    runner = TelegramBotRunner(
        bot,
        Dispatcher(),
        service,
        repository,
        progress_store,
        None,
        False,
    )

    handled = await runner._handle_command(
        ChatContext(chat_key="chat:1", chat_id=1, topic_id=None),
        "thread-1",
        ("threads", "project"),
        "/threads project",
    )

    assert handled is True
    text = bot.send_message.await_args.kwargs["text"]
    assert len(re.findall(r"/ct_[0-9a-f]{8}\b", text)) == 6
    assert "Project 0 thread 2" in text
    assert "Project 0 thread 3" not in text
    assert "Project 1 thread 2" in text
    assert "Project 1 thread 3" not in text
    assert "2 more threads not shown" in text


@pytest.mark.asyncio
async def test_codex_threads_full_uses_larger_listing_limit(
    tmp_path: Path,
) -> None:
    repository = SQLiteStateRepository(tmp_path / "state.db")
    progress_store = SQLiteTelegramProgressStore(tmp_path / "state.db")
    await repository.initialize()
    await progress_store.initialize()

    service = AsyncMock()
    service.list_codex_threads.return_value = CodexThreadListResult(
        groups=[], failures=[]
    )
    bot = AsyncMock()
    runner = TelegramBotRunner(
        bot,
        Dispatcher(),
        service,
        repository,
        progress_store,
        None,
        False,
    )

    handled = await runner._handle_command(
        ChatContext(chat_key="chat:1", chat_id=1, topic_id=None),
        "thread-1",
        ("threads", "full irrigation"),
        "/threads full irrigation",
    )

    assert handled is True
    service.list_codex_threads.assert_awaited_once_with(
        "chat:1",
        backend_name=None,
        include_all=False,
        search="irrigation",
        limit=50,
    )


@pytest.mark.asyncio
async def test_codex_threads_parses_connection_option_full_and_search(
    tmp_path: Path,
) -> None:
    repository = SQLiteStateRepository(tmp_path / "state.db")
    progress_store = SQLiteTelegramProgressStore(tmp_path / "state.db")
    await repository.initialize()
    await progress_store.initialize()

    service = AsyncMock()
    service.list_codex_threads.return_value = CodexThreadListResult(
        groups=[], failures=[]
    )
    bot = AsyncMock()
    runner = TelegramBotRunner(
        bot,
        Dispatcher(),
        service,
        repository,
        progress_store,
        None,
        False,
    )

    handled = await runner._handle_command(
        ChatContext(chat_key="chat:1", chat_id=1, topic_id=None),
        "thread-1",
        ("threads", "--connection laptop --full irrigation"),
        "/threads --connection laptop --full irrigation",
    )

    assert handled is True
    service.list_codex_threads.assert_awaited_once_with(
        "chat:1",
        backend_name="laptop",
        include_all=False,
        search="irrigation",
        limit=50,
    )


@pytest.mark.asyncio
async def test_codex_threads_rejects_unknown_option(tmp_path: Path) -> None:
    repository = SQLiteStateRepository(tmp_path / "state.db")
    progress_store = SQLiteTelegramProgressStore(tmp_path / "state.db")
    await repository.initialize()
    await progress_store.initialize()

    service = AsyncMock()
    bot = AsyncMock()
    runner = TelegramBotRunner(
        bot,
        Dispatcher(),
        service,
        repository,
        progress_store,
        None,
        False,
    )

    handled = await runner._handle_command(
        ChatContext(chat_key="chat:1", chat_id=1, topic_id=None),
        "thread-1",
        ("threads", "--bogus"),
        "/threads --bogus",
    )

    assert handled is True
    service.list_codex_threads.assert_not_awaited()
    assert "Unknown option: --bogus" in bot.send_message.await_args.kwargs["text"]


@pytest.mark.asyncio
async def test_new_with_project_and_prompt_submits_first_turn(tmp_path: Path) -> None:
    repository = SQLiteStateRepository(tmp_path / "state.db")
    progress_store = SQLiteTelegramProgressStore(tmp_path / "state.db")
    await repository.initialize()
    await progress_store.initialize()

    thread = LogicalThread(
        thread_id="thread-1",
        chat_key="chat:1",
        title="New conversation",
        codex_thread_id=None,
        created_at="now",
        updated_at="now",
        turn_count=0,
        awaiting_reply=False,
        interrupted_notice=False,
        pending_turn_id=None,
    )
    service = AsyncMock()
    service.new_thread.return_value = thread
    service.run_turn.return_value = TurnRunResult(
        result=TurnResult(
            turn_id="turn-1",
            chat_key="chat:1",
            logical_thread_id="thread-1",
            codex_thread_id="codex-1",
            status="completed",
            final_text="first answer",
        ),
        remapped=False,
        remap_warning=None,
    )
    bot = AsyncMock()
    bot.send_message.return_value.message_id = 55
    runner = TelegramBotRunner(
        bot,
        Dispatcher(),
        service,
        repository,
        progress_store,
        None,
        False,
    )

    handled = await runner._handle_command(
        ChatContext(chat_key="chat:1", chat_id=1, topic_id=None),
        "old-thread",
        ("new", "--connection laptop --project app Fix failing tests"),
        "/new --connection laptop --project app Fix failing tests",
    )

    assert handled is True
    service.new_thread.assert_awaited_once_with(
        "chat:1",
        connection_name="laptop",
        project_selector="app",
    )
    service.run_turn.assert_awaited_once()
    assert service.run_turn.await_args.args[:2] == ("chat:1", "Fix failing tests")
    assert service.run_turn.await_args.kwargs["thread_id"] == "thread-1"
    assert callable(service.run_turn.await_args.kwargs["on_state_change"])
    assert bot.send_message.await_args.kwargs["text"] == "✅ first answer"


@pytest.mark.asyncio
async def test_new_without_arguments_shows_connection_picker(tmp_path: Path) -> None:
    repository = SQLiteStateRepository(tmp_path / "state.db")
    progress_store = SQLiteTelegramProgressStore(tmp_path / "state.db")
    await repository.initialize()
    await progress_store.initialize()

    service = AsyncMock()
    service.has_default_project = Mock(return_value=True)
    service.list_backend_connections.return_value = [
        SimpleNamespace(connection_id="primary", label="Home"),
        SimpleNamespace(connection_id="laptop", label="Laptop"),
    ]
    bot = AsyncMock()
    runner = TelegramBotRunner(
        bot,
        Dispatcher(),
        service,
        repository,
        progress_store,
        None,
        False,
    )

    handled = await runner._handle_command(
        ChatContext(chat_key="chat:1", chat_id=1, topic_id=None),
        "thread-1",
        ("new", ""),
        "/new",
    )

    assert handled is True
    service.new_thread.assert_not_awaited()
    service.new_thread_in_default_project.assert_not_awaited()
    labels = [
        button.text
        for row in bot.send_message.await_args.kwargs["reply_markup"].inline_keyboard
        for button in row
    ]
    assert labels == [
        "Default",
        "Recent projects",
        "Connection: primary",
        "Connection: laptop",
    ]


@pytest.mark.asyncio
async def test_new_default_callback_creates_thread_in_default_project(
    tmp_path: Path,
) -> None:
    repository = SQLiteStateRepository(tmp_path / "state.db")
    progress_store = SQLiteTelegramProgressStore(tmp_path / "state.db")
    await repository.initialize()
    await progress_store.initialize()
    token = await repository.create_callback_token(
        chat_key="chat:1",
        topic_id=None,
        action="new_default_project",
        payload={},
        expires_at="2999-01-01T00:00:00+00:00",
    )

    service = AsyncMock()
    service.new_thread_in_default_project.return_value = LogicalThread(
        thread_id="new-thread",
        chat_key="chat:1",
        title="New conversation",
        codex_thread_id=None,
        created_at="now",
        updated_at="now",
        turn_count=0,
        awaiting_reply=False,
        interrupted_notice=False,
        pending_turn_id=None,
        codex_backend_id="primary",
    )
    service.show_project_state.return_value = SimpleNamespace(active=None)
    bot = AsyncMock()
    runner = TelegramBotRunner(
        bot,
        Dispatcher(),
        service,
        repository,
        progress_store,
        None,
        False,
    )
    callback = SimpleNamespace(
        data=f"ct:{token}",
        message=SimpleNamespace(
            chat=SimpleNamespace(id=1),
            message_thread_id=None,
            message_id=55,
        ),
        answer=AsyncMock(),
    )

    await runner._on_callback_query(cast(CallbackQuery, callback))

    service.new_thread_in_default_project.assert_awaited_once_with("chat:1")
    assert bot.send_message.await_args.kwargs["text"] == (
        "Started new conversation New conversation.\n"
        "Connection: primary\n"
        "Project: (none)"
    )


@pytest.mark.asyncio
async def test_new_recent_projects_callback_lists_five_cross_connection_projects(
    tmp_path: Path,
) -> None:
    repository = SQLiteStateRepository(tmp_path / "state.db")
    progress_store = SQLiteTelegramProgressStore(tmp_path / "state.db")
    await repository.initialize()
    await progress_store.initialize()
    token = await repository.create_callback_token(
        chat_key="chat:1",
        topic_id=None,
        action="new_recent_projects",
        payload={},
        expires_at="2999-01-01T00:00:00+00:00",
    )

    service = AsyncMock()
    service.list_recent_projects.return_value = [
        Project(
            project_id=f"project-{index}",
            connection_id=f"backend-{index % 2}",
            root_path=f"/agent/project-{index}",
            label=f"Project {index}",
            created_at="now",
            updated_at="now",
        )
        for index in range(5)
    ]
    bot = AsyncMock()
    runner = TelegramBotRunner(
        bot,
        Dispatcher(),
        service,
        repository,
        progress_store,
        None,
        False,
    )
    callback = SimpleNamespace(
        data=f"ct:{token}",
        message=SimpleNamespace(
            chat=SimpleNamespace(id=1),
            message_thread_id=None,
            message_id=55,
        ),
        answer=AsyncMock(),
    )

    await runner._on_callback_query(cast(CallbackQuery, callback))

    service.list_recent_projects.assert_awaited_once_with(
        chat_key="chat:1",
        include_all=True,
        limit=6,
    )
    sent = bot.send_message.await_args.kwargs
    assert sent["text"] == "Choose a recent project."
    labels = [
        button.text for row in sent["reply_markup"].inline_keyboard for button in row
    ]
    assert labels == [
        "Project 0",
        "Project 1",
        "Project 2",
        "Project 3",
        "Project 4",
    ]


@pytest.mark.asyncio
async def test_new_recent_projects_callback_more_expands_to_twenty(
    tmp_path: Path,
) -> None:
    repository = SQLiteStateRepository(tmp_path / "state.db")
    progress_store = SQLiteTelegramProgressStore(tmp_path / "state.db")
    await repository.initialize()
    await progress_store.initialize()
    token = await repository.create_callback_token(
        chat_key="chat:1",
        topic_id=None,
        action="new_recent_projects",
        payload={},
        expires_at="2999-01-01T00:00:00+00:00",
    )

    def project(index: int) -> Project:
        return Project(
            project_id=f"project-{index}",
            connection_id=f"backend-{index % 2}",
            root_path=f"/agent/project-{index}",
            label=f"Project {index}",
            created_at="now",
            updated_at="now",
        )

    service = AsyncMock()
    service.list_recent_projects.side_effect = [
        [project(index) for index in range(6)],
        [project(index) for index in range(20)],
    ]
    bot = AsyncMock()
    runner = TelegramBotRunner(
        bot,
        Dispatcher(),
        service,
        repository,
        progress_store,
        None,
        False,
    )
    callback = SimpleNamespace(
        data=f"ct:{token}",
        message=SimpleNamespace(
            chat=SimpleNamespace(id=1),
            message_thread_id=None,
            message_id=55,
        ),
        answer=AsyncMock(),
    )

    await runner._on_callback_query(cast(CallbackQuery, callback))

    service.list_recent_projects.assert_awaited_once_with(
        chat_key="chat:1",
        include_all=True,
        limit=6,
    )
    compact = bot.send_message.await_args.kwargs
    labels = [
        button.text for row in compact["reply_markup"].inline_keyboard for button in row
    ]
    assert labels == [
        "Project 0",
        "Project 1",
        "Project 2",
        "Project 3",
        "Project 4",
        "More",
    ]
    more_button = compact["reply_markup"].inline_keyboard[-1][0]

    callback.data = more_button.callback_data
    bot.send_message.reset_mock()
    service.list_recent_projects.reset_mock()

    await runner._on_callback_query(cast(CallbackQuery, callback))

    service.list_recent_projects.assert_awaited_once_with(
        chat_key="chat:1",
        include_all=True,
        limit=20,
    )
    expanded = bot.send_message.await_args.kwargs
    expanded_labels = [
        button.text
        for row in expanded["reply_markup"].inline_keyboard
        for button in row
    ]
    assert expanded_labels == [f"Project {index}" for index in range(20)]


@pytest.mark.asyncio
async def test_new_connection_callback_lists_recent_projects(tmp_path: Path) -> None:
    repository = SQLiteStateRepository(tmp_path / "state.db")
    progress_store = SQLiteTelegramProgressStore(tmp_path / "state.db")
    await repository.initialize()
    await progress_store.initialize()
    token = await repository.create_callback_token(
        chat_key="chat:1",
        topic_id=None,
        action="new_connection",
        payload={"connection_id": "laptop", "connection_label": "Laptop"},
        expires_at="2999-01-01T00:00:00+00:00",
    )

    service = AsyncMock()
    service.list_recent_projects.return_value = [
        Project(
            project_id="project-1",
            connection_id="laptop",
            root_path="/agent/app",
            label="app",
            created_at="now",
            updated_at="now",
        )
    ]
    bot = AsyncMock()
    runner = TelegramBotRunner(
        bot,
        Dispatcher(),
        service,
        repository,
        progress_store,
        None,
        False,
    )
    callback = SimpleNamespace(
        data=f"ct:{token}",
        message=SimpleNamespace(
            chat=SimpleNamespace(id=1),
            message_thread_id=None,
            message_id=55,
        ),
        answer=AsyncMock(),
    )

    await runner._on_callback_query(cast(CallbackQuery, callback))

    service.list_recent_projects.assert_awaited_once_with(
        chat_key="chat:1",
        connection_id="laptop",
        include_all=False,
        limit=6,
    )
    labels = [
        button.text
        for row in bot.send_message.await_args.kwargs["reply_markup"].inline_keyboard
        for button in row
    ]
    assert labels == ["app"]


@pytest.mark.asyncio
async def test_new_project_callback_creates_thread_in_project(tmp_path: Path) -> None:
    repository = SQLiteStateRepository(tmp_path / "state.db")
    progress_store = SQLiteTelegramProgressStore(tmp_path / "state.db")
    await repository.initialize()
    await progress_store.initialize()
    token = await repository.create_callback_token(
        chat_key="chat:1",
        topic_id=None,
        action="new_project",
        payload={"project_id": "project-1"},
        expires_at="2999-01-01T00:00:00+00:00",
    )

    service = AsyncMock()
    service.new_thread_in_project.return_value = LogicalThread(
        thread_id="new-thread",
        chat_key="chat:1",
        title="New conversation",
        codex_thread_id=None,
        created_at="now",
        updated_at="now",
        turn_count=0,
        awaiting_reply=False,
        interrupted_notice=False,
        pending_turn_id=None,
        codex_backend_id="laptop",
    )
    service.show_project_state.return_value = SimpleNamespace(
        active=Project(
            project_id="project-1",
            connection_id="laptop",
            root_path="/agent/app",
            label="app",
            created_at="now",
            updated_at="now",
        )
    )
    service.list_conversations.return_value = []
    service.ensure_focused_bridge.return_value = SimpleNamespace(
        bridge_id="new-thread",
        title="New conversation",
        codex_backend_id="laptop",
        codex_thread_id=None,
    )
    bot = AsyncMock()
    bot.send_message.return_value.message_id = 88
    runner = TelegramBotRunner(
        bot,
        Dispatcher(),
        service,
        repository,
        progress_store,
        None,
        False,
    )
    callback = SimpleNamespace(
        data=f"ct:{token}",
        message=SimpleNamespace(
            chat=SimpleNamespace(id=1),
            message_thread_id=None,
            message_id=55,
        ),
        answer=AsyncMock(),
    )

    await runner._on_callback_query(cast(CallbackQuery, callback))

    service.new_thread_in_project.assert_awaited_once_with("chat:1", "project-1")
    assert bot.send_message.await_args_list[0].kwargs["text"] == (
        "Started new conversation New conversation.\n"
        "Connection: laptop\n"
        "Project: app (/agent/app)"
    )


@pytest.mark.asyncio
async def test_focus_delivers_final_messages_only(tmp_path: Path) -> None:
    repository = SQLiteStateRepository(tmp_path / "state.db")
    progress_store = SQLiteTelegramProgressStore(tmp_path / "state.db")
    await repository.initialize()
    await progress_store.initialize()

    service = AsyncMock()
    service.focus_bridge.return_value = ThreadSelectionResult(
        success=True,
        message="Focused conversation Deploy fix.",
        thread=LogicalThread(
            thread_id="bridge-1",
            chat_key="chat:1",
            title="Deploy fix",
            codex_thread_id="codex-1",
            created_at="now",
            updated_at="now",
            turn_count=1,
            awaiting_reply=False,
            interrupted_notice=False,
            pending_turn_id=None,
            codex_backend_id="laptop",
            anchor_id="anchor-1",
        ),
    )
    service.focus_final_messages.return_value = [
        SimpleNamespace(
            message=ThreadMessage(
                message_id=9,
                thread_id="bridge-1",
                role="assistant",
                kind="final",
                text="webhook answer",
                created_at="now",
            ),
            repeated=False,
        )
    ]
    service.list_conversations.return_value = []
    service.ensure_focused_bridge.return_value = SimpleNamespace(
        bridge_id="bridge-1",
        title="Deploy fix",
        codex_backend_id="laptop",
        codex_thread_id="codex-1",
    )
    bot = AsyncMock()
    bot.send_message.return_value.message_id = 88
    runner = TelegramBotRunner(
        bot,
        Dispatcher(),
        service,
        repository,
        progress_store,
        None,
        False,
    )

    handled = await runner._handle_command(
        ChatContext(chat_key="chat:1", chat_id=1, topic_id=None),
        "old-thread",
        ("focus", "anchor-1"),
        "/focus anchor-1",
    )

    assert handled is True
    service.focus_final_messages.assert_awaited_once_with(
        "chat:1",
        "bridge-1",
    )
    service.mark_thread_messages_delivered.assert_awaited_once_with(
        "chat:1",
        "bridge-1",
    )
    replay_text = bot.send_message.await_args_list[1].kwargs["text"]
    assert replay_text == "✅ webhook answer"


@pytest.mark.asyncio
async def test_focus_delivers_final_image_messages(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = SQLiteStateRepository(tmp_path / "state.db")
    progress_store = SQLiteTelegramProgressStore(tmp_path / "state.db")
    await repository.initialize()
    await progress_store.initialize()
    image_path = tmp_path / "generated.png"
    image_path.write_bytes(b"fake png bytes")
    monkeypatch.chdir(tmp_path)

    service = AsyncMock()
    service.focus_final_messages.return_value = [
        SimpleNamespace(
            message=ThreadMessage(
                message_id=9,
                thread_id="bridge-1",
                role="assistant",
                kind="final_image",
                text=(
                    '{"source": "'
                    + str(image_path)
                    + '", "caption": "A small watercolor house."}'
                ),
                created_at="now",
            ),
            repeated=False,
        )
    ]
    bot = AsyncMock()
    bot.send_photo.return_value = SimpleNamespace(message_id=88)
    runner = TelegramBotRunner(
        bot,
        Dispatcher(),
        service,
        repository,
        progress_store,
        None,
        False,
    )

    await runner._send_focus_final_messages(
        ChatContext(chat_key="chat:1", chat_id=1, topic_id=None),
        "bridge-1",
    )

    bot.send_message.assert_not_awaited()
    bot.send_photo.assert_awaited_once()
    sent_photo = bot.send_photo.await_args.kwargs
    assert sent_photo["chat_id"] == 1
    assert sent_photo["caption"] == "A small watercolor house."
    service.mark_thread_messages_delivered.assert_awaited_once_with(
        "chat:1",
        "bridge-1",
    )


@pytest.mark.asyncio
async def test_focus_command_replays_latest_anchor_bridge_messages_with_real_service(
    tmp_path: Path,
) -> None:
    repository = SQLiteStateRepository(tmp_path / "state.db")
    progress_store = SQLiteTelegramProgressStore(tmp_path / "state.db")
    await repository.initialize()
    await progress_store.initialize()
    client = AsyncMock()
    client.get_runtime_state = Mock(return_value=CodexRuntimeState())
    service = BotService(
        BotServiceConfig(
            default_profile="operator",
            client_default_profiles={},
            profiles={},
            turn_poll_seconds=0.01,
            wait_notice_seconds=180.0,
            bridge_window_ttl_seconds=900.0,
        ),
        repository,
        client,
    )
    anchor = await repository.upsert_conversation_anchor(
        chat_key="chat:1",
        codex_backend_id="primary",
        codex_thread_id="codex-1",
        title="Background deploy",
    )
    bridge = await repository.create_bridge(
        chat_key="chat:1",
        bridge_id="bridge-1",
        title="Background deploy",
        anchor_id=anchor.anchor_id,
        codex_backend_id="primary",
        focus=True,
    )
    await service.new_thread("chat:1")
    await repository.add_thread_message(
        bridge.bridge_id,
        role="assistant",
        kind="final",
        text="background answer",
    )
    bot = AsyncMock()
    bot.send_message.return_value.message_id = 88
    runner = TelegramBotRunner(
        bot,
        Dispatcher(),
        service,
        repository,
        progress_store,
        None,
        False,
    )

    handled = await runner._handle_command(
        ChatContext(chat_key="chat:1", chat_id=1, topic_id=None),
        "current-thread",
        ("focus", anchor.anchor_id),
        f"/focus {anchor.anchor_id}",
    )

    assert handled is True
    sent_texts = [call.kwargs["text"] for call in bot.send_message.await_args_list]
    assert "Previously undelivered messages" not in "\n".join(sent_texts)
    assert "User:" not in "\n".join(sent_texts)
    assert "✅ background answer" in sent_texts

    bot.send_message.reset_mock()
    handled_again = await runner._handle_command(
        ChatContext(chat_key="chat:1", chat_id=1, topic_id=None),
        "current-thread",
        ("focus", anchor.anchor_id),
        f"/focus {anchor.anchor_id}",
    )

    assert handled_again is True
    repeated_texts = [call.kwargs["text"] for call in bot.send_message.await_args_list]
    assert repeated_texts == [
        "Focused conversation Background deploy.",
        "<b>🔁 From: Background deploy</b>\n\nbackground answer",
    ]
    assert bot.send_message.await_args_list[-1].kwargs["parse_mode"] == "HTML"


@pytest.mark.asyncio
async def test_history_command_replays_recent_final_messages_without_truncation(
    tmp_path: Path,
) -> None:
    repository = SQLiteStateRepository(tmp_path / "state.db")
    progress_store = SQLiteTelegramProgressStore(tmp_path / "state.db")
    await repository.initialize()
    await progress_store.initialize()
    client = AsyncMock()
    client.get_runtime_state = Mock(return_value=CodexRuntimeState())
    service = BotService(
        BotServiceConfig(
            default_profile="operator",
            client_default_profiles={},
            profiles={},
            turn_poll_seconds=0.01,
            wait_notice_seconds=180.0,
            bridge_window_ttl_seconds=900.0,
        ),
        repository,
        client,
    )
    bridge = await service.new_thread("chat:1")
    long_final = "long final " + ("without truncation " * 40).strip()
    await repository.add_thread_message(
        bridge.thread_id,
        role="user",
        kind="prompt",
        text="do not replay me",
    )
    await repository.add_thread_message(
        bridge.thread_id,
        role="assistant",
        kind="final",
        text="older final",
    )
    await repository.add_thread_message(
        bridge.thread_id,
        role="assistant",
        kind="final",
        text=long_final,
    )
    bot = AsyncMock()
    bot.send_message.return_value.message_id = 88
    runner = TelegramBotRunner(
        bot,
        Dispatcher(),
        service,
        repository,
        progress_store,
        None,
        False,
    )

    handled = await runner._handle_command(
        ChatContext(chat_key="chat:1", chat_id=1, topic_id=None),
        bridge.thread_id,
        ("history", "2"),
        "/history 2",
    )

    assert handled is True
    sent_texts = [call.kwargs["text"] for call in bot.send_message.await_args_list]
    assert sent_texts == [
        "<b>🔁 From: New conversation</b>\n\nolder final",
        f"<b>🔁 From: New conversation</b>\n\n{long_final}",
    ]
    assert bot.send_message.await_args_list[-1].kwargs["parse_mode"] == "HTML"
    assert "do not replay me" not in "\n".join(sent_texts)
    assert "..." not in sent_texts[-1]


@pytest.mark.asyncio
async def test_status_card_threads_shortcut_sends_connection_picker_at_bottom(
    tmp_path: Path,
) -> None:
    repository = SQLiteStateRepository(tmp_path / "state.db")
    progress_store = SQLiteTelegramProgressStore(tmp_path / "state.db")
    await repository.initialize()
    await progress_store.initialize()
    token = await repository.create_callback_token(
        chat_key="chat:1",
        topic_id=None,
        action="show_threads",
        payload={},
        expires_at="2999-01-01T00:00:00+00:00",
    )

    service = AsyncMock()
    service.list_backend_connections.return_value = [
        SimpleNamespace(connection_id="primary", label="Home")
    ]
    bot = AsyncMock()
    runner = TelegramBotRunner(
        bot,
        Dispatcher(),
        service,
        repository,
        progress_store,
        None,
        False,
    )
    callback = SimpleNamespace(
        data=f"ct:{token}",
        message=SimpleNamespace(
            chat=SimpleNamespace(id=1),
            message_thread_id=None,
            message_id=55,
        ),
        answer=AsyncMock(),
    )

    await runner._on_callback_query(cast(CallbackQuery, callback))

    service.list_backend_connections.assert_awaited_once_with()
    bot.edit_message_text.assert_not_awaited()
    sent = bot.send_message.await_args.kwargs
    assert sent["text"] == "Choose a connection."
    labels = [
        button.text for row in sent["reply_markup"].inline_keyboard for button in row
    ]
    assert labels == ["Recent", "Recent projects", "Connection: primary"]


@pytest.mark.asyncio
async def test_status_card_projects_shortcut_sends_project_state_at_bottom(
    tmp_path: Path,
) -> None:
    repository = SQLiteStateRepository(tmp_path / "state.db")
    progress_store = SQLiteTelegramProgressStore(tmp_path / "state.db")
    await repository.initialize()
    await progress_store.initialize()
    token = await repository.create_callback_token(
        chat_key="chat:1",
        topic_id=None,
        action="show_projects",
        payload={},
        expires_at="2999-01-01T00:00:00+00:00",
    )

    service = AsyncMock()
    service.show_project_state.return_value = SimpleNamespace(
        thread=LogicalThread(
            thread_id="thread-1",
            chat_key="chat:1",
            title="Thread",
            codex_thread_id="codex-1",
            created_at="now",
            updated_at="now",
            turn_count=1,
            awaiting_reply=False,
            interrupted_notice=False,
            pending_turn_id=None,
        ),
        active=None,
        catalog=[],
        project_overrides=SessionOverrides(),
    )
    bot = AsyncMock()
    runner = TelegramBotRunner(
        bot,
        Dispatcher(),
        service,
        repository,
        progress_store,
        None,
        False,
    )
    callback = SimpleNamespace(
        data=f"ct:{token}",
        message=SimpleNamespace(
            chat=SimpleNamespace(id=1),
            message_thread_id=None,
            message_id=55,
        ),
        answer=AsyncMock(),
    )

    await runner._on_callback_query(cast(CallbackQuery, callback))

    service.show_project_state.assert_awaited_once_with("chat:1")
    bot.edit_message_text.assert_not_awaited()
    assert "<b>Active project</b> (none)" in bot.send_message.await_args.kwargs["text"]
    assert bot.send_message.await_args.kwargs["parse_mode"] == "HTML"


@pytest.mark.asyncio
async def test_threads_recent_projects_shortcut_lists_five_cross_connection_projects(
    tmp_path: Path,
) -> None:
    repository = SQLiteStateRepository(tmp_path / "state.db")
    progress_store = SQLiteTelegramProgressStore(tmp_path / "state.db")
    await repository.initialize()
    await progress_store.initialize()
    token = await repository.create_callback_token(
        chat_key="chat:1",
        topic_id=None,
        action="codex_threads_recent_projects",
        payload={},
        expires_at="2999-01-01T00:00:00+00:00",
    )

    service = AsyncMock()
    service.list_recent_projects.return_value = [
        Project(
            project_id=f"project-{index}",
            connection_id=f"backend-{index % 2}",
            root_path=f"/agent/project-{index}",
            label=f"Project {index}",
            created_at="now",
            updated_at="now",
        )
        for index in range(5)
    ]
    bot = AsyncMock()
    runner = TelegramBotRunner(
        bot,
        Dispatcher(),
        service,
        repository,
        progress_store,
        None,
        False,
    )
    callback = SimpleNamespace(
        data=f"ct:{token}",
        message=SimpleNamespace(
            chat=SimpleNamespace(id=1),
            message_thread_id=None,
            message_id=55,
        ),
        answer=AsyncMock(),
    )

    await runner._on_callback_query(cast(CallbackQuery, callback))

    service.list_recent_projects.assert_awaited_once_with(
        chat_key="chat:1",
        connection_id=None,
        include_all=True,
        limit=6,
    )
    bot.edit_message_text.assert_not_awaited()
    sent = bot.send_message.await_args.kwargs
    assert sent["text"] == "Choose a project on all connections."
    labels = [
        button.text for row in sent["reply_markup"].inline_keyboard for button in row
    ]
    assert labels == [
        "Project 0",
        "Project 1",
        "Project 2",
        "Project 3",
        "Project 4",
        "All projects",
    ]


@pytest.mark.asyncio
async def test_project_command_is_read_only_current_project(tmp_path: Path) -> None:
    repository = SQLiteStateRepository(tmp_path / "state.db")
    progress_store = SQLiteTelegramProgressStore(tmp_path / "state.db")
    await repository.initialize()
    await progress_store.initialize()

    service = AsyncMock()
    service.show_project_state.return_value = SimpleNamespace(
        thread=LogicalThread(
            thread_id="thread-1",
            chat_key="chat:1",
            title="Thread",
            codex_thread_id="codex-1",
            created_at="now",
            updated_at="now",
            turn_count=1,
            awaiting_reply=False,
            interrupted_notice=False,
            pending_turn_id=None,
        ),
        active=Project(
            project_id="project-1",
            connection_id="laptop",
            root_path="/agent/app",
            label="app",
            created_at="now",
            updated_at="now",
        ),
        catalog=[],
        project_overrides=SessionOverrides(model="gpt-5.4-mini", effort="high"),
    )
    bot = AsyncMock()
    runner = TelegramBotRunner(
        bot,
        Dispatcher(),
        service,
        repository,
        progress_store,
        None,
        False,
    )

    handled = await runner._handle_command(
        ChatContext(chat_key="chat:1", chat_id=1, topic_id=None),
        "thread-1",
        ("project", ""),
        "/project",
    )

    assert handled is True
    service.show_project_state.assert_awaited_once_with("chat:1")
    sent = bot.send_message.await_args.kwargs["text"]
    assert "<b>Active project</b> laptop:app -&gt; <code>/agent/app</code>" in sent
    assert "• <b>Model</b> <code>gpt-5.4-mini</code>" in sent
    assert bot.send_message.await_args.kwargs["parse_mode"] == "HTML"
    assert "Known projects" not in sent

    bot.reset_mock()
    handled = await runner._handle_command(
        ChatContext(chat_key="chat:1", chat_id=1, topic_id=None),
        "thread-1",
        ("project", "list"),
        "/project list",
    )

    assert handled is True
    assert "Usage: /project" in bot.send_message.await_args.kwargs["text"]
    service.show_project_state.assert_awaited_once()


@pytest.mark.asyncio
async def test_new_syncs_thread_status_card_pinned_preview(tmp_path: Path) -> None:
    repository = SQLiteStateRepository(tmp_path / "state.db")
    progress_store = SQLiteTelegramProgressStore(tmp_path / "state.db")
    await repository.initialize()
    await progress_store.initialize()
    await progress_store.save_status_card(
        "chat:1",
        chat_id=1,
        topic_id=None,
        message_id=77,
        rendered_text="old",
    )

    thread = LogicalThread(
        thread_id="thread-1",
        chat_key="chat:1",
        title="New conversation",
        codex_thread_id=None,
        created_at="now",
        updated_at="now",
        turn_count=0,
        awaiting_reply=False,
        interrupted_notice=False,
        pending_turn_id=None,
    )
    service = AsyncMock()
    service.new_thread.return_value = thread
    service.list_conversations.return_value = []
    service.ensure_focused_bridge.return_value = thread
    bot = AsyncMock()
    bot.get_chat.return_value = SimpleNamespace(
        pinned_message=SimpleNamespace(message_id=77)
    )
    runner = TelegramBotRunner(
        bot,
        Dispatcher(),
        service,
        repository,
        progress_store,
        None,
        False,
    )

    handled = await runner._handle_command(
        ChatContext(chat_key="chat:1", chat_id=1, topic_id=None),
        "old-thread",
        ("new", "--connection laptop"),
        "/new --connection laptop",
    )

    assert handled is True
    bot.edit_message_text.assert_awaited_once()
    assert bot.edit_message_text.await_args.kwargs["message_id"] == 77
    bot.unpin_chat_message.assert_awaited_once_with(chat_id=1, message_id=77)
    bot.pin_chat_message.assert_awaited_once_with(
        chat_id=1,
        message_id=77,
        disable_notification=True,
    )


@pytest.mark.asyncio
async def test_new_conversation_notice_includes_project_and_connection(
    tmp_path: Path,
) -> None:
    repository = SQLiteStateRepository(tmp_path / "state.db")
    progress_store = SQLiteTelegramProgressStore(tmp_path / "state.db")
    await repository.initialize()
    await progress_store.initialize()

    thread = LogicalThread(
        thread_id="thread-1",
        chat_key="chat:1",
        title="New conversation",
        codex_thread_id=None,
        created_at="now",
        updated_at="now",
        turn_count=0,
        awaiting_reply=False,
        interrupted_notice=False,
        pending_turn_id=None,
        codex_backend_id="laptop",
    )
    service = AsyncMock()
    service.new_thread.return_value = thread
    service.show_project_state.return_value = SimpleNamespace(
        active=Project(
            project_id="project-1",
            connection_id="laptop",
            root_path="/agent/app",
            label="app",
            created_at="now",
            updated_at="now",
        )
    )
    service.list_conversations.return_value = []
    service.ensure_focused_bridge.return_value = thread
    bot = AsyncMock()
    bot.send_message.return_value.message_id = 88
    runner = TelegramBotRunner(
        bot,
        Dispatcher(),
        service,
        repository,
        progress_store,
        None,
        False,
    )

    handled = await runner._handle_command(
        ChatContext(chat_key="chat:1", chat_id=1, topic_id=None),
        "old-thread",
        ("new", "--connection laptop"),
        "/new --connection laptop",
    )

    assert handled is True
    sent_text = bot.send_message.await_args_list[0].kwargs["text"]
    assert sent_text == (
        "Started new conversation New conversation.\n"
        "Connection: laptop\n"
        "Project: app (/agent/app)"
    )


@pytest.mark.asyncio
async def test_codex_threads_all_renders_partial_backend_failure(
    tmp_path: Path,
) -> None:
    repository = SQLiteStateRepository(tmp_path / "state.db")
    progress_store = SQLiteTelegramProgressStore(tmp_path / "state.db")
    await repository.initialize()
    await progress_store.initialize()

    service = AsyncMock()
    service.list_codex_threads.return_value = CodexThreadListResult(
        groups=[
            CodexThreadGroup(
                backend_id="home",
                backend_name="home",
                project="/agent/project-a",
                threads=[
                    CodexThread(
                        thread_id="codex-1",
                        cwd="/agent/project-a",
                        title="Healthy thread",
                        preview=None,
                        status="idle",
                        created_at=1710000000,
                        updated_at=1710000300,
                        model_provider="openai",
                        codex_backend_id="home",
                        codex_backend_name="home",
                    )
                ],
            )
        ],
        failures=[
            CodexThreadBackendFailure(
                backend_id="laptop",
                backend_name="laptop",
                error="connection refused",
            )
        ],
    )
    bot = AsyncMock()
    runner = TelegramBotRunner(
        bot,
        Dispatcher(),
        service,
        repository,
        progress_store,
        None,
        False,
    )

    handled = await runner._handle_command(
        ChatContext(chat_key="chat:1", chat_id=1, topic_id=None),
        "thread-1",
        ("threads", "all"),
        "/threads all",
    )

    assert handled is True
    service.list_codex_threads.assert_awaited_once_with(
        "chat:1",
        backend_name=None,
        include_all=True,
        search=None,
        limit=50,
    )
    text = bot.send_message.await_args.kwargs["text"]
    assert "<b>Backend</b> home" in text
    assert "Backend laptop unavailable" in text


@pytest.mark.asyncio
async def test_codex_threads_full_omission_hint_is_html_safe(
    tmp_path: Path,
) -> None:
    repository = SQLiteStateRepository(tmp_path / "state.db")
    progress_store = SQLiteTelegramProgressStore(tmp_path / "state.db")
    await repository.initialize()
    await progress_store.initialize()

    service = AsyncMock()
    service.list_codex_threads.return_value = CodexThreadListResult(
        groups=[
            CodexThreadGroup(
                project="/agent",
                threads=[
                    CodexThread(
                        thread_id=f"codex-{index}",
                        cwd="/agent",
                        title="A long thread title that makes the full list hit the limit "
                        * 3,
                        preview=None,
                        status="notLoaded",
                        created_at=1710000000 + index,
                        updated_at=1710000300 + index,
                        model_provider="openai",
                        anchor_status="focused",
                    )
                    for index in range(50)
                ],
            )
        ],
        failures=[],
    )
    bot = AsyncMock()
    runner = TelegramBotRunner(
        bot,
        Dispatcher(),
        service,
        repository,
        progress_store,
        None,
        False,
    )

    handled = await runner._handle_command(
        ChatContext(chat_key="chat:1", chat_id=1, topic_id=None),
        "thread-1",
        ("threads", "full"),
        "/threads full",
    )

    assert handled is True
    sent = bot.send_message.await_args.kwargs
    assert sent["parse_mode"] == "HTML"
    assert "<search>" not in sent["text"]


@pytest.mark.asyncio
async def test_codex_thread_text_shortcut_connects_thread(
    tmp_path: Path,
) -> None:
    repository = SQLiteStateRepository(tmp_path / "state.db")
    progress_store = SQLiteTelegramProgressStore(tmp_path / "state.db")
    await repository.initialize()
    await progress_store.initialize()
    token = await repository.create_callback_token(
        chat_key="chat:1",
        topic_id=None,
        action="attach_codex",
        payload={"codex_thread_id": "codex-1"},
        expires_at="2999-01-01T00:00:00+00:00",
    )

    service = AsyncMock()
    service.attach_codex_thread.return_value = SimpleNamespace(
        anchor=SimpleNamespace(
            anchor_id="anchor-1",
            title="Fix CI",
        ),
        bridge=SimpleNamespace(
            bridge_id="bridge-1",
            chat_key="chat:1",
            title="Fix CI",
            codex_thread_id="codex-1",
            created_at="now",
            updated_at="now",
            turn_count=0,
            awaiting_reply=False,
            interrupted_notice=False,
            pending_turn_id=None,
        ),
        codex_thread=CodexThread(
            thread_id="codex-1",
            cwd="/agent/project-a",
            title="Fix CI",
            preview=None,
            status="idle",
            created_at=1710000000,
            updated_at=1710000300,
            model_provider="openai",
        ),
    )
    bot = AsyncMock()
    runner = TelegramBotRunner(
        bot,
        Dispatcher(),
        service,
        repository,
        progress_store,
        None,
        False,
    )

    handled = await runner._handle_command(
        ChatContext(chat_key="chat:1", chat_id=1, topic_id=None),
        "thread-1",
        (f"ct_{token}", ""),
        f"/ct_{token}",
    )

    assert handled is True
    service.attach_codex_thread.assert_awaited_once_with(
        "chat:1",
        "codex-1",
        backend_id=None,
    )
    sent_text = bot.send_message.await_args.kwargs["text"]
    assert "🟢 <b>Attached Codex thread</b> Fix CI" in sent_text
    assert bot.send_message.await_args.kwargs["parse_mode"] == "HTML"
    assert "codex-1" not in sent_text
    assert "anchor-1" not in sent_text
    assert "bridge-1" not in sent_text


@pytest.mark.asyncio
async def test_attach_thread_uses_connection_option(tmp_path: Path) -> None:
    repository = SQLiteStateRepository(tmp_path / "state.db")
    progress_store = SQLiteTelegramProgressStore(tmp_path / "state.db")
    await repository.initialize()
    await progress_store.initialize()

    service = AsyncMock()
    service.attach_codex_thread.return_value = SimpleNamespace(
        anchor=SimpleNamespace(
            anchor_id="anchor-1",
            title="Fix CI",
        ),
        bridge=SimpleNamespace(
            bridge_id="bridge-1",
            chat_key="chat:1",
            title="Fix CI",
            codex_thread_id="codex-1",
            created_at="now",
            updated_at="now",
            turn_count=0,
            awaiting_reply=False,
            interrupted_notice=False,
            pending_turn_id=None,
        ),
        codex_thread=CodexThread(
            thread_id="codex-1",
            cwd="/agent/project-a",
            title="Fix CI",
            preview=None,
            status="idle",
            created_at=1710000000,
            updated_at=1710000300,
            model_provider="openai",
        ),
    )
    bot = AsyncMock()
    runner = TelegramBotRunner(
        bot,
        Dispatcher(),
        service,
        repository,
        progress_store,
        None,
        False,
    )

    handled = await runner._handle_command(
        ChatContext(chat_key="chat:1", chat_id=1, topic_id=None),
        "thread-1",
        ("attach_thread", "--connection laptop codex-1"),
        "/attach_thread --connection laptop codex-1",
    )

    assert handled is True
    service.attach_codex_thread.assert_awaited_once_with(
        "chat:1",
        "codex-1",
        backend_name="laptop",
    )


@pytest.mark.asyncio
async def test_identical_turn_update_does_not_emit_duplicate_message(
    tmp_path: Path,
) -> None:
    repository = SQLiteStateRepository(tmp_path / "state.db")
    progress_store = SQLiteTelegramProgressStore(tmp_path / "state.db")
    await repository.initialize()
    await progress_store.initialize()
    await progress_store.save_progress(
        "thread-1",
        message_id=55,
        rendered_text="same text",
    )

    bot = AsyncMock()
    runner = TelegramBotRunner(
        bot,
        Dispatcher(),
        AsyncMock(),
        repository,
        progress_store,
        None,
        False,
    )

    await runner._handle_turn_update(
        ChatContext(chat_key="chat:1", chat_id=1, topic_id=None),
        TurnUpdate(
            turn_id="turn-1",
            chat_key="chat:1",
            logical_thread_id="thread-1",
            codex_thread_id="codex-1",
            status="inProgress",
            source="item/completed",
            text="same text",
        ),
    )

    bot.edit_message_text.assert_not_awaited()
    bot.send_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_partial_turn_update_waits_for_sentence_boundary(
    tmp_path: Path,
) -> None:
    repository = SQLiteStateRepository(tmp_path / "state.db")
    progress_store = SQLiteTelegramProgressStore(tmp_path / "state.db")
    await repository.initialize()
    await progress_store.initialize()

    bot = AsyncMock()
    runner = TelegramBotRunner(
        bot,
        Dispatcher(),
        AsyncMock(),
        repository,
        progress_store,
        None,
        False,
    )

    await runner._handle_turn_update(
        ChatContext(chat_key="chat:1", chat_id=1, topic_id=None),
        TurnUpdate(
            turn_id="turn-1",
            chat_key="chat:1",
            logical_thread_id="thread-1",
            codex_thread_id="codex-1",
            status="inProgress",
            source="item/completed",
            text="Streaming word by word",
        ),
    )

    bot.edit_message_text.assert_not_awaited()
    bot.send_message.assert_not_awaited()
    assert await progress_store.get_progress("thread-1") is None


@pytest.mark.asyncio
async def test_progress_update_sends_only_completed_sentence(
    tmp_path: Path,
) -> None:
    repository = SQLiteStateRepository(tmp_path / "state.db")
    progress_store = SQLiteTelegramProgressStore(tmp_path / "state.db")
    await repository.initialize()
    await progress_store.initialize()

    bot = AsyncMock()
    bot.send_message.return_value.message_id = 77
    runner = TelegramBotRunner(
        bot,
        Dispatcher(),
        AsyncMock(),
        repository,
        progress_store,
        None,
        False,
    )

    await runner._handle_turn_update(
        ChatContext(chat_key="chat:1", chat_id=1, topic_id=None),
        TurnUpdate(
            turn_id="turn-1",
            chat_key="chat:1",
            logical_thread_id="thread-1",
            codex_thread_id="codex-1",
            status="inProgress",
            source="item/completed",
            text="First sentence. Second sentence is still arriving",
        ),
    )

    bot.send_message.assert_awaited_once_with(
        chat_id=1,
        text="💭 First sentence.",
        message_thread_id=None,
        parse_mode="HTML",
    )
    progress = await progress_store.get_progress("thread-1")
    assert progress is not None
    assert progress.message_id == 77
    assert progress.rendered_text == "First sentence."


@pytest.mark.asyncio
async def test_progress_update_renders_markdown_as_telegram_html(
    tmp_path: Path,
) -> None:
    repository = SQLiteStateRepository(tmp_path / "state.db")
    progress_store = SQLiteTelegramProgressStore(tmp_path / "state.db")
    await repository.initialize()
    await progress_store.initialize()

    bot = AsyncMock()
    bot.send_message.return_value.message_id = 77
    runner = TelegramBotRunner(
        bot,
        Dispatcher(),
        AsyncMock(),
        repository,
        progress_store,
        None,
        False,
    )

    await runner._handle_turn_update(
        ChatContext(chat_key="chat:1", chat_id=1, topic_id=None),
        TurnUpdate(
            turn_id="turn-1",
            chat_key="chat:1",
            logical_thread_id="thread-1",
            codex_thread_id="codex-1",
            status="inProgress",
            source="item/completed",
            text=(
                "# Smoke Heading\n\n"
                "## Summary\n"
                "- Use `inline` code.\n"
                "- Keep **bold** text.\n"
            ),
        ),
    )

    sent = bot.send_message.await_args.kwargs
    assert sent["parse_mode"] == "HTML"
    assert sent["text"] == (
        "💭 <b>Smoke Heading</b>\n\n"
        "<b>Summary</b>\n"
        "• Use <code>inline</code> code.\n"
        "• Keep <b>bold</b> text."
    )
    assert "#" not in sent["text"]
    assert "`" not in sent["text"]
    progress = await progress_store.get_progress("thread-1")
    assert progress is not None
    assert progress.rendered_text == (
        "# Smoke Heading\n\n"
        "## Summary\n"
        "- Use `inline` code.\n"
        "- Keep **bold** text."
    )


@pytest.mark.asyncio
async def test_retry_after_on_progress_edit_does_not_abort_turn_update(
    tmp_path: Path,
) -> None:
    repository = SQLiteStateRepository(tmp_path / "state.db")
    progress_store = SQLiteTelegramProgressStore(tmp_path / "state.db")
    await repository.initialize()
    await progress_store.initialize()
    await progress_store.save_progress(
        "thread-1",
        message_id=55,
        rendered_text="First sentence.",
    )

    bot = AsyncMock()
    bot.edit_message_text.side_effect = _RetryAfterError(0.0)
    runner = TelegramBotRunner(
        bot,
        Dispatcher(),
        AsyncMock(),
        repository,
        progress_store,
        None,
        False,
    )

    await runner._handle_turn_update(
        ChatContext(chat_key="chat:1", chat_id=1, topic_id=None),
        TurnUpdate(
            turn_id="turn-1",
            chat_key="chat:1",
            logical_thread_id="thread-1",
            codex_thread_id="codex-1",
            status="inProgress",
            source="item/completed",
            text="First sentence. Second sentence.",
        ),
    )

    bot.edit_message_text.assert_awaited_once()
    bot.send_message.assert_not_awaited()
    progress = await progress_store.get_progress("thread-1")
    assert progress is not None
    assert progress.message_id == 55
    assert progress.rendered_text == "First sentence."


@pytest.mark.asyncio
async def test_final_reply_edits_existing_progress_message(
    tmp_path: Path,
) -> None:
    repository = SQLiteStateRepository(tmp_path / "state.db")
    progress_store = SQLiteTelegramProgressStore(tmp_path / "state.db")
    await repository.initialize()
    await progress_store.initialize()
    await progress_store.save_progress(
        "thread-1",
        message_id=55,
        rendered_text="partial text",
    )

    bot = AsyncMock()
    runner = TelegramBotRunner(
        bot,
        Dispatcher(),
        AsyncMock(),
        repository,
        progress_store,
        None,
        False,
    )

    await runner._send_final(
        ChatContext(chat_key="chat:1", chat_id=1, topic_id=None),
        TurnResult(
            turn_id="turn-1",
            chat_key="chat:1",
            logical_thread_id="thread-1",
            codex_thread_id="codex-1",
            status="completed",
            final_text="final text",
        ),
    )

    bot.edit_message_text.assert_awaited_once_with(
        chat_id=1,
        message_id=55,
        text="✅ final text",
        parse_mode="HTML",
    )
    bot.send_message.assert_not_awaited()
    assert await progress_store.get_progress("thread-1") is None


@pytest.mark.asyncio
async def test_final_reply_records_message_for_later_reply_resume(
    tmp_path: Path,
) -> None:
    repository = SQLiteStateRepository(tmp_path / "state.db")
    progress_store = SQLiteTelegramProgressStore(tmp_path / "state.db")
    await repository.initialize()
    await progress_store.initialize()

    bot = AsyncMock()
    bot.send_message.return_value.message_id = 55
    runner = TelegramBotRunner(
        bot,
        Dispatcher(),
        AsyncMock(),
        repository,
        progress_store,
        None,
        False,
    )

    await runner._send_final(
        ChatContext(chat_key="chat:1", chat_id=1, topic_id=None),
        TurnResult(
            turn_id="turn-1",
            chat_key="chat:1",
            logical_thread_id="thread-1",
            codex_thread_id="codex-1",
            status="completed",
            final_text="final text",
        ),
    )

    target = await progress_store.get_final_message_by_reply("chat:1", 55)
    assert target is not None
    assert target.thread_id == "thread-1"
    assert target.rendered_text == "✅ final text"


@pytest.mark.asyncio
async def test_background_final_reply_uses_from_header_and_actions(
    tmp_path: Path,
) -> None:
    repository = SQLiteStateRepository(tmp_path / "state.db")
    progress_store = SQLiteTelegramProgressStore(tmp_path / "state.db")
    await repository.initialize()
    await progress_store.initialize()
    anchor = await repository.upsert_conversation_anchor(
        chat_key="chat:1",
        codex_backend_id="primary",
        codex_thread_id="codex-1",
        title="Background deploy",
    )
    await repository.create_bridge(
        chat_key="chat:1",
        bridge_id="background-thread",
        title="Background deploy",
        anchor_id=anchor.anchor_id,
        codex_backend_id="primary",
        focus=False,
    )
    service = AsyncMock()
    service.ensure_focused_bridge.return_value = SimpleNamespace(
        bridge_id="focused-thread"
    )
    bot = AsyncMock()
    bot.send_message.return_value.message_id = 55
    runner = TelegramBotRunner(
        bot,
        Dispatcher(),
        service,
        repository,
        progress_store,
        None,
        False,
    )

    await runner._send_final(
        ChatContext(chat_key="chat:1", chat_id=1, topic_id=None),
        TurnResult(
            turn_id="turn-1",
            chat_key="chat:1",
            logical_thread_id="background-thread",
            codex_thread_id="codex-1",
            status="completed",
            final_text="background <final> & done",
        ),
    )

    sent = bot.send_message.await_args.kwargs
    assert sent["text"] == (
        "<b>📬 From: Background deploy</b>\n\n" "background &lt;final&gt; &amp; done"
    )
    assert sent["parse_mode"] == "HTML"
    buttons = [
        button.text for row in sent["reply_markup"].inline_keyboard for button in row
    ]
    assert buttons == ["Reply", "Focus"]
    service.mark_thread_messages_delivered.assert_awaited_once_with(
        "chat:1",
        "background-thread",
    )


@pytest.mark.asyncio
async def test_final_reply_sends_native_images(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    image_path = tmp_path / "generated.png"
    image_path.write_bytes(b"image")

    repository = SQLiteStateRepository(tmp_path / "state.db")
    progress_store = SQLiteTelegramProgressStore(tmp_path / "state.db")
    await repository.initialize()
    await progress_store.initialize()

    bot = AsyncMock()
    bot.send_message.return_value = SimpleNamespace(message_id=55)
    bot.send_photo.return_value = SimpleNamespace(message_id=56)
    service = AsyncMock()
    runner = TelegramBotRunner(
        bot,
        Dispatcher(),
        service,
        repository,
        progress_store,
        None,
        False,
    )

    await runner._send_final(
        ChatContext(chat_key="chat:1", chat_id=1, topic_id=None),
        TurnResult(
            turn_id="turn-1",
            chat_key="chat:1",
            logical_thread_id="thread-1",
            codex_thread_id="codex-1",
            status="completed",
            final_text="Here is the image.",
            images=(
                TurnResultImage(
                    source=str(image_path),
                    caption="A small watercolor house.",
                ),
            ),
        ),
    )

    bot.send_photo.assert_awaited_once()
    sent_photo = bot.send_photo.await_args.kwargs
    assert sent_photo["chat_id"] == 1
    assert sent_photo["caption"] == "A small watercolor house."
    assert sent_photo["message_thread_id"] is None


@pytest.mark.asyncio
async def test_background_final_reply_is_chunked_without_truncation(
    tmp_path: Path,
) -> None:
    repository = SQLiteStateRepository(tmp_path / "state.db")
    progress_store = SQLiteTelegramProgressStore(tmp_path / "state.db")
    await repository.initialize()
    await progress_store.initialize()
    anchor = await repository.upsert_conversation_anchor(
        chat_key="chat:1",
        codex_backend_id="primary",
        codex_thread_id="codex-1",
        title="Background deploy",
    )
    await repository.create_bridge(
        chat_key="chat:1",
        bridge_id="background-thread",
        title="Background deploy",
        anchor_id=anchor.anchor_id,
        codex_backend_id="primary",
        focus=False,
    )
    service = AsyncMock()
    service.ensure_focused_bridge.return_value = SimpleNamespace(
        bridge_id="focused-thread"
    )
    bot = AsyncMock()
    bot.send_message.side_effect = [
        SimpleNamespace(message_id=55),
        SimpleNamespace(message_id=56),
    ]
    runner = TelegramBotRunner(
        bot,
        Dispatcher(),
        service,
        repository,
        progress_store,
        None,
        False,
    )
    final_text = "x" * (TELEGRAM_MESSAGE_TEXT_LIMIT + 500)

    await runner._send_final(
        ChatContext(chat_key="chat:1", chat_id=1, topic_id=None),
        TurnResult(
            turn_id="turn-1",
            chat_key="chat:1",
            logical_thread_id="background-thread",
            codex_thread_id="codex-1",
            status="completed",
            final_text=final_text,
        ),
    )

    sent_chunks = [call.kwargs["text"] for call in bot.send_message.await_args_list]
    assert len(sent_chunks) == 2
    assert "truncated to fit Telegram" not in "\n".join(sent_chunks)
    delivered_body = "".join(
        chunk.removeprefix("<b>📬 From: Background deploy</b>\n\n")
        for chunk in sent_chunks
    )
    assert delivered_body == final_text
    assert "reply_markup" not in bot.send_message.await_args_list[0].kwargs
    assert bot.send_message.await_args_list[-1].kwargs["reply_markup"] is not None


@pytest.mark.asyncio
async def test_finish_run_result_sends_final_reply(
    tmp_path: Path,
) -> None:
    repository = SQLiteStateRepository(tmp_path / "state.db")
    progress_store = SQLiteTelegramProgressStore(tmp_path / "state.db")
    await repository.initialize()
    await progress_store.initialize()
    await progress_store.save_final_message(
        "thread-1",
        chat_key="chat:1",
        message_id=55,
        rendered_text="✅ previous final",
    )

    bot = AsyncMock()
    bot.send_message.return_value.message_id = 88
    runner = TelegramBotRunner(
        bot,
        Dispatcher(),
        AsyncMock(),
        repository,
        progress_store,
        None,
        False,
    )

    await runner._finish_run_result(
        ChatContext(chat_key="chat:1", chat_id=1, topic_id=None),
        TurnRunResult(
            result=TurnResult(
                turn_id="turn-2",
                chat_key="chat:1",
                logical_thread_id="thread-2",
                codex_thread_id="codex-2",
                status="completed",
                final_text="new final",
            ),
            remapped=False,
            remap_warning=None,
        ),
    )

    bot.edit_message_text.assert_not_awaited()
    sent_texts = [call.kwargs["text"] for call in bot.send_message.await_args_list]
    assert sent_texts == ["✅ new final"]


@pytest.mark.asyncio
async def test_final_reply_renders_supported_markdown_as_telegram_html(
    tmp_path: Path,
) -> None:
    repository = SQLiteStateRepository(tmp_path / "state.db")
    progress_store = SQLiteTelegramProgressStore(tmp_path / "state.db")
    await repository.initialize()
    await progress_store.initialize()

    bot = AsyncMock()
    bot.send_message.return_value.message_id = 88
    runner = TelegramBotRunner(
        bot,
        Dispatcher(),
        AsyncMock(),
        repository,
        progress_store,
        None,
        False,
    )

    await runner._finish_run_result(
        ChatContext(chat_key="chat:1", chat_id=1, topic_id=None),
        TurnRunResult(
            result=TurnResult(
                turn_id="turn-2",
                chat_key="chat:1",
                logical_thread_id="thread-2",
                codex_thread_id="codex-2",
                status="completed",
                final_text=(
                    "Use **bold** and `x < y`.\n\n"
                    "```python\n"
                    'print("hi & bye")\n'
                    "```"
                ),
            ),
            remapped=False,
            remap_warning=None,
        ),
    )

    sent = bot.send_message.await_args.kwargs
    assert sent["parse_mode"] == "HTML"
    assert sent["text"] == (
        "✅ Use <b>bold</b> and <code>x &lt; y</code>.\n\n"
        '<pre><code class="language-python">print(&quot;hi &amp; bye&quot;)\n'
        "</code></pre>"
    )


@pytest.mark.asyncio
async def test_final_reply_renders_markdown_headings_and_lists_as_telegram_html(
    tmp_path: Path,
) -> None:
    repository = SQLiteStateRepository(tmp_path / "state.db")
    progress_store = SQLiteTelegramProgressStore(tmp_path / "state.db")
    await repository.initialize()
    await progress_store.initialize()

    bot = AsyncMock()
    bot.send_message.return_value.message_id = 88
    runner = TelegramBotRunner(
        bot,
        Dispatcher(),
        AsyncMock(),
        repository,
        progress_store,
        None,
        False,
    )

    await runner._finish_run_result(
        ChatContext(chat_key="chat:1", chat_id=1, topic_id=None),
        TurnRunResult(
            result=TurnResult(
                turn_id="turn-2",
                chat_key="chat:1",
                logical_thread_id="thread-2",
                codex_thread_id="codex-2",
                status="completed",
                final_text=(
                    "# Ring Snapshot Implementation\n\n"
                    "## Summary\n"
                    "Build a dashboard.\n\n"
                    "## Key Changes\n"
                    "- Use `custom:photo-frame` cards.\n"
                    "- Keep **visible** timestamps.\n"
                    "1. Verify folders.\n"
                ),
            ),
            remapped=False,
            remap_warning=None,
        ),
    )

    sent = bot.send_message.await_args.kwargs
    assert sent["parse_mode"] == "HTML"
    assert sent["text"] == (
        "✅ <b>Ring Snapshot Implementation</b>\n\n"
        "<b>Summary</b>\n"
        "Build a dashboard.\n\n"
        "<b>Key Changes</b>\n"
        "• Use <code>custom:photo-frame</code> cards.\n"
        "• Keep <b>visible</b> timestamps.\n"
        "1. Verify folders.\n"
    )
    assert "#" not in sent["text"]
    assert "`" not in sent["text"]


@pytest.mark.asyncio
async def test_background_and_replayed_finals_render_rich_markdown(
    tmp_path: Path,
) -> None:
    repository = SQLiteStateRepository(tmp_path / "state.db")
    progress_store = SQLiteTelegramProgressStore(tmp_path / "state.db")
    await repository.initialize()
    await progress_store.initialize()
    await repository.create_bridge(
        chat_key="chat:1",
        bridge_id="background-thread",
        title="Background deploy",
        anchor_id=None,
        codex_backend_id="primary",
        focus=False,
    )
    service = AsyncMock()
    service.ensure_focused_bridge.return_value = SimpleNamespace(
        bridge_id="focused-thread"
    )
    bot = AsyncMock()
    bot.send_message.side_effect = [
        SimpleNamespace(message_id=55),
        SimpleNamespace(message_id=56),
    ]
    runner = TelegramBotRunner(
        bot,
        Dispatcher(),
        service,
        repository,
        progress_store,
        None,
        False,
    )
    context = ChatContext(chat_key="chat:1", chat_id=1, topic_id=None)

    await runner._send_final(
        context,
        TurnResult(
            turn_id="turn-1",
            chat_key="chat:1",
            logical_thread_id="background-thread",
            codex_thread_id="codex-1",
            status="completed",
            final_text="Here is `code` and **bold**.",
        ),
    )
    await runner._send_focus_repeat_final(
        context,
        ThreadMessage(
            message_id=9,
            thread_id="background-thread",
            role="assistant",
            text="Replay:\n```text\nbla bla\n```",
            created_at="now",
            kind="final",
        ),
    )

    sent = [call.kwargs for call in bot.send_message.await_args_list]
    assert sent[0]["parse_mode"] == "HTML"
    assert sent[0]["text"] == (
        "<b>📬 From: Background deploy</b>\n\n"
        "Here is <code>code</code> and <b>bold</b>."
    )
    assert sent[1]["parse_mode"] == "HTML"
    assert sent[1]["text"] == (
        "<b>🔁 From: Background deploy</b>\n\n"
        'Replay:\n<pre><code class="language-text">bla bla\n</code></pre>'
    )


@pytest.mark.asyncio
async def test_long_rich_final_chunks_code_without_truncation(
    tmp_path: Path,
) -> None:
    repository = SQLiteStateRepository(tmp_path / "state.db")
    progress_store = SQLiteTelegramProgressStore(tmp_path / "state.db")
    await repository.initialize()
    await progress_store.initialize()

    bot = AsyncMock()
    message_ids = iter(range(88, 100))

    async def _send_message(**kwargs: object) -> object:
        del kwargs
        return SimpleNamespace(message_id=next(message_ids))

    bot.send_message.side_effect = _send_message
    runner = TelegramBotRunner(
        bot,
        Dispatcher(),
        AsyncMock(),
        repository,
        progress_store,
        None,
        False,
    )
    code = "x < y & z\n" * 600

    await runner._finish_run_result(
        ChatContext(chat_key="chat:1", chat_id=1, topic_id=None),
        TurnRunResult(
            result=TurnResult(
                turn_id="turn-2",
                chat_key="chat:1",
                logical_thread_id="thread-2",
                codex_thread_id="codex-2",
                status="completed",
                final_text=f"```text\n{code}```",
            ),
            remapped=False,
            remap_warning=None,
        ),
    )

    sent = [call.kwargs for call in bot.send_message.await_args_list]
    assert len(sent) > 1
    assert all(call["parse_mode"] == "HTML" for call in sent)
    assert all(len(call["text"]) <= TELEGRAM_MESSAGE_TEXT_LIMIT for call in sent)
    assert "truncated to fit Telegram" not in "\n".join(call["text"] for call in sent)
    assert all(
        call["text"].count("<pre>") == call["text"].count("</pre>") for call in sent
    )
    assert "```" not in "\n".join(call["text"] for call in sent)


@pytest.mark.asyncio
async def test_bridge_expiry_pass_only_marks_windows_expired(
    tmp_path: Path,
) -> None:
    repository = SQLiteStateRepository(tmp_path / "state.db")
    progress_store = SQLiteTelegramProgressStore(tmp_path / "state.db")
    await repository.initialize()
    await progress_store.initialize()
    await progress_store.save_final_message(
        "thread-1",
        chat_key="chat:1",
        message_id=55,
        rendered_text="✅ previous final",
    )

    bot = AsyncMock()
    service = AsyncMock()
    service.expire_idle_bridges.return_value = ["thread-1"]
    runner = TelegramBotRunner(
        bot,
        Dispatcher(),
        service,
        repository,
        progress_store,
        None,
        False,
    )

    await runner._expire_idle_bridges_once()

    bot.edit_message_text.assert_not_awaited()
    service.expire_idle_bridges.assert_awaited_once_with()


@pytest.mark.asyncio
async def test_reply_to_wrapped_final_resumes_previous_conversation(
    tmp_path: Path,
) -> None:
    repository = SQLiteStateRepository(tmp_path / "state.db")
    progress_store = SQLiteTelegramProgressStore(tmp_path / "state.db")
    await repository.initialize()
    await progress_store.initialize()
    await progress_store.save_final_message(
        "thread-1",
        chat_key="chat:1",
        message_id=55,
        rendered_text=(
            "✅ previous final\n\n"
            "This conversations is wrapped up due to inactivity. "
            "Reply to this message to resume the conversation."
        ),
    )

    service = AsyncMock()
    service.ensure_active_thread.return_value = LogicalThread(
        thread_id="thread-2",
        chat_key="chat:1",
        title="New conversation",
        codex_thread_id="codex-2",
        created_at="now",
        updated_at="now",
        turn_count=1,
        awaiting_reply=False,
        interrupted_notice=False,
        pending_turn_id=None,
    )
    service.take_interrupted_notice.return_value = False
    service.resolve_bridge.return_value = SimpleNamespace(
        success=True,
        thread=LogicalThread(
            thread_id="thread-1",
            chat_key="chat:1",
            title="Previous",
            codex_thread_id="codex-1",
            created_at="now",
            updated_at="now",
            turn_count=1,
            awaiting_reply=False,
            interrupted_notice=False,
            pending_turn_id=None,
        ),
    )
    service.run_turn.return_value = TurnRunResult(
        result=TurnResult(
            turn_id="turn-3",
            chat_key="chat:1",
            logical_thread_id="thread-1",
            codex_thread_id="codex-1",
            status="completed",
            final_text="resumed final",
        ),
        remapped=False,
        remap_warning=None,
    )
    service.pending_request_for_chat.return_value = None
    bot = AsyncMock()
    bot.send_message.return_value.message_id = 88
    runner = TelegramBotRunner(
        bot,
        Dispatcher(),
        service,
        repository,
        progress_store,
        None,
        False,
    )
    message = SimpleNamespace(
        chat=SimpleNamespace(id=1),
        message_thread_id=None,
        text="continue here",
        reply_to_message=SimpleNamespace(message_id=55),
    )

    await runner._on_message(cast(Message, message))

    service.resolve_bridge.assert_awaited_once_with(
        "chat:1",
        "thread-1",
        focus=False,
    )
    assert service.run_turn.await_args.kwargs["thread_id"] == "thread-1"
    sent_texts = [call.kwargs["text"] for call in bot.send_message.await_args_list]
    assert sent_texts[0] == "Resuming previous conversation..."
    assert sent_texts[-1] == "✅ resumed final"


@pytest.mark.asyncio
async def test_callback_connects_codex_thread_and_answers_query(tmp_path: Path) -> None:
    repository = SQLiteStateRepository(tmp_path / "state.db")
    progress_store = SQLiteTelegramProgressStore(tmp_path / "state.db")
    await repository.initialize()
    await progress_store.initialize()
    token = await repository.create_callback_token(
        chat_key="chat:1",
        topic_id=None,
        action="attach_codex",
        payload={"codex_thread_id": "codex-1"},
        expires_at="2999-01-01T00:00:00+00:00",
    )

    service = AsyncMock()
    service.attach_codex_thread.return_value = SimpleNamespace(
        anchor=SimpleNamespace(
            anchor_id="anchor-1",
            title="Fix CI",
        ),
        bridge=SimpleNamespace(
            bridge_id="bridge-1",
            chat_key="chat:1",
            title="Fix CI",
            codex_thread_id="codex-1",
            created_at="now",
            updated_at="now",
            turn_count=0,
            awaiting_reply=False,
            interrupted_notice=False,
            pending_turn_id=None,
        ),
        codex_thread=CodexThread(
            thread_id="codex-1",
            cwd="/agent/project-a",
            title="Fix CI",
            preview=None,
            status="idle",
            created_at=1710000000,
            updated_at=1710000300,
            model_provider="openai",
        ),
    )
    bot = AsyncMock()
    runner = TelegramBotRunner(
        bot,
        Dispatcher(),
        service,
        repository,
        progress_store,
        None,
        False,
    )
    callback = SimpleNamespace(
        data=f"ct:{token}",
        message=SimpleNamespace(chat=SimpleNamespace(id=1), message_thread_id=None),
        answer=AsyncMock(),
    )

    await runner._on_callback_query(cast(CallbackQuery, callback))

    service.attach_codex_thread.assert_awaited_once_with(
        "chat:1",
        "codex-1",
        backend_id=None,
    )
    callback.answer.assert_awaited_once()
    sent_text = bot.send_message.await_args.kwargs["text"]
    assert "🟢 <b>Attached Codex thread</b> Fix CI" in sent_text
    assert bot.send_message.await_args.kwargs["parse_mode"] == "HTML"
    assert "codex-1" not in sent_text
    assert "anchor-1" not in sent_text
    assert "bridge-1" not in sent_text


@pytest.mark.asyncio
async def test_callback_opens_bridge_thread_and_answers_query(tmp_path: Path) -> None:
    repository = SQLiteStateRepository(tmp_path / "state.db")
    progress_store = SQLiteTelegramProgressStore(tmp_path / "state.db")
    await repository.initialize()
    await progress_store.initialize()
    token = await repository.create_callback_token(
        chat_key="chat:1",
        topic_id=None,
        action="focus_conversation",
        payload={"anchor_id": "anchor-1"},
        expires_at="2999-01-01T00:00:00+00:00",
    )

    service = AsyncMock()
    service.focus_bridge.return_value = SimpleNamespace(
        success=True,
        message="Focused conversation Deploy fix.",
        thread=None,
    )
    bot = AsyncMock()
    runner = TelegramBotRunner(
        bot,
        Dispatcher(),
        service,
        repository,
        progress_store,
        None,
        False,
    )
    callback = SimpleNamespace(
        data=f"ct:{token}",
        message=SimpleNamespace(chat=SimpleNamespace(id=1), message_thread_id=None),
        answer=AsyncMock(),
    )

    await runner._on_callback_query(cast(CallbackQuery, callback))

    service.focus_bridge.assert_awaited_once_with("chat:1", "anchor-1")
    callback.answer.assert_awaited_once()
    assert (
        bot.send_message.await_args.kwargs["text"] == "Focused conversation Deploy fix."
    )


@pytest.mark.asyncio
async def test_reply_callback_uses_force_reply_for_one_off_background_reply(
    tmp_path: Path,
) -> None:
    repository = SQLiteStateRepository(tmp_path / "state.db")
    progress_store = SQLiteTelegramProgressStore(tmp_path / "state.db")
    await repository.initialize()
    await progress_store.initialize()
    token = await repository.create_callback_token(
        chat_key="chat:1",
        topic_id=None,
        action="reply_conversation",
        payload={"thread_id": "background-thread"},
        expires_at="2999-01-01T00:00:00+00:00",
    )
    service = AsyncMock()
    bot = AsyncMock()
    bot.send_message.return_value.message_id = 77
    runner = TelegramBotRunner(
        bot,
        Dispatcher(),
        service,
        repository,
        progress_store,
        None,
        False,
    )
    callback = SimpleNamespace(
        data=f"ct:{token}",
        message=SimpleNamespace(
            chat=SimpleNamespace(id=1),
            message_thread_id=None,
            message_id=55,
        ),
        answer=AsyncMock(),
    )

    await runner._on_callback_query(cast(CallbackQuery, callback))

    sent = bot.send_message.await_args.kwargs
    assert sent["text"] == "Reply to this prompt to answer that conversation once."
    assert isinstance(sent["reply_markup"], ForceReply)

    service.resolve_bridge.return_value = SimpleNamespace(
        success=True,
        message="Resolved.",
        thread=LogicalThread(
            thread_id="background-thread",
            chat_key="chat:1",
            title="Background",
            codex_thread_id="codex-1",
            created_at="now",
            updated_at="now",
            turn_count=1,
            awaiting_reply=False,
            interrupted_notice=False,
            pending_turn_id=None,
            codex_backend_id="primary",
            anchor_id="anchor-1",
        ),
    )
    service.take_interrupted_notice.return_value = False
    service.route_realtime_input.return_value = False
    service.run_turn.return_value = TurnRunResult(
        result=None,
        remapped=False,
        remap_warning=None,
        active_turn_continues=True,
    )
    reply_message = SimpleNamespace(
        chat=SimpleNamespace(id=1),
        message_thread_id=None,
        text="one off reply",
        reply_to_message=SimpleNamespace(message_id=77),
    )

    await runner._on_message(cast(Message, reply_message))

    service.resolve_bridge.assert_awaited_once_with(
        "chat:1",
        "background-thread",
        focus=False,
    )
    service.run_turn.assert_awaited_once()
    assert service.run_turn.await_args.kwargs["thread_id"] == "background-thread"


@pytest.mark.asyncio
async def test_callback_revoke_webhook_requires_confirmation_and_answers_query(
    tmp_path: Path,
) -> None:
    repository = SQLiteStateRepository(tmp_path / "state.db")
    progress_store = SQLiteTelegramProgressStore(tmp_path / "state.db")
    await repository.initialize()
    await progress_store.initialize()
    token = await repository.create_callback_token(
        chat_key="chat:1",
        topic_id=None,
        action="revoke_webhook",
        payload={"webhook_id": "wh_123"},
        expires_at="2999-01-01T00:00:00+00:00",
    )

    service = AsyncMock()
    bot = AsyncMock()
    runner = TelegramBotRunner(
        bot,
        Dispatcher(),
        service,
        repository,
        progress_store,
        None,
        False,
    )
    callback = SimpleNamespace(
        data=f"ct:{token}",
        message=SimpleNamespace(chat=SimpleNamespace(id=1), message_thread_id=None),
        answer=AsyncMock(),
    )

    await runner._on_callback_query(cast(CallbackQuery, callback))

    service.revoke_webhook_subscription.assert_not_awaited()
    callback.answer.assert_awaited_once()
    assert "Confirm webhook revoke" in bot.send_message.await_args.kwargs["text"]
    assert bot.send_message.await_args.kwargs["reply_markup"] is not None


@pytest.mark.asyncio
async def test_approval_request_includes_inline_decision_buttons(
    tmp_path: Path,
) -> None:
    repository = SQLiteStateRepository(tmp_path / "state.db")
    progress_store = SQLiteTelegramProgressStore(tmp_path / "state.db")
    await repository.initialize()
    await progress_store.initialize()

    bot = AsyncMock()
    service = AsyncMock()
    service.pending_request_for_chat.return_value = PendingApproval(
        request_id=7,
        chat_key="chat:1",
        logical_thread_id="thread-1",
        codex_thread_id="codex-1",
        turn_id="turn-1",
        method="item/commandExecution/requestApproval",
        command="uv run pytest",
        reason="Run tests",
    )
    runner = TelegramBotRunner(
        bot,
        Dispatcher(),
        service,
        repository,
        progress_store,
        None,
        False,
    )

    await runner._finish_run_result(
        ChatContext(chat_key="chat:1", chat_id=1, topic_id=None),
        TurnRunResult(
            result=TurnResult(
                turn_id="turn-1",
                chat_key="chat:1",
                logical_thread_id="thread-1",
                codex_thread_id="codex-1",
                status="approvalRequired",
                final_text="",
            ),
            remapped=False,
            remap_warning=None,
        ),
    )

    bot.send_message.assert_awaited_once()
    kwargs = bot.send_message.await_args.kwargs
    assert kwargs["text"] == (
        "⚠️ <b>Codex needs approval</b>\n"
        "<b>Command</b> <code>uv run pytest</code>\n"
        "<b>Reason</b> Run tests"
    )
    assert kwargs["parse_mode"] == "HTML"
    assert "/approve" not in kwargs["text"]
    markup = kwargs["reply_markup"]
    labels = [[button.text for button in row] for row in markup.inline_keyboard]
    assert labels == [
        ["Approve once", "Approve session"],
        ["Deny", "Cancel"],
    ]


@pytest.mark.asyncio
async def test_approval_request_includes_guardian_message(
    tmp_path: Path,
) -> None:
    repository = SQLiteStateRepository(tmp_path / "state.db")
    progress_store = SQLiteTelegramProgressStore(tmp_path / "state.db")
    await repository.initialize()
    await progress_store.initialize()

    bot = AsyncMock()
    service = AsyncMock()
    service.pending_request_for_chat.return_value = PendingApproval(
        request_id=7,
        chat_key="chat:1",
        logical_thread_id="thread-1",
        codex_thread_id="codex-1",
        turn_id="turn-1",
        method="item/commandExecution/requestApproval",
        command="uv run pytest",
        reason="Run tests",
        message="Guardian reviewed the approach before execution.",
    )
    runner = TelegramBotRunner(
        bot,
        Dispatcher(),
        service,
        repository,
        progress_store,
        None,
        False,
    )

    await runner._finish_run_result(
        ChatContext(chat_key="chat:1", chat_id=1, topic_id=None),
        TurnRunResult(
            result=TurnResult(
                turn_id="turn-1",
                chat_key="chat:1",
                logical_thread_id="thread-1",
                codex_thread_id="codex-1",
                status="approvalRequired",
                final_text="",
            ),
            remapped=False,
            remap_warning=None,
        ),
    )

    bot.send_message.assert_awaited_once()
    assert bot.send_message.await_args.kwargs["text"] == (
        "⚠️ <b>Codex needs approval</b>\n"
        "<b>Command</b> <code>uv run pytest</code>\n"
        "<b>Reason</b> Run tests\n"
        "<b>Guardian</b> Guardian reviewed the approach before execution."
    )
    assert bot.send_message.await_args.kwargs["parse_mode"] == "HTML"


@pytest.mark.asyncio
async def test_callback_resolves_matching_approval_and_answers_query(
    tmp_path: Path,
) -> None:
    repository = SQLiteStateRepository(tmp_path / "state.db")
    progress_store = SQLiteTelegramProgressStore(tmp_path / "state.db")
    await repository.initialize()
    await progress_store.initialize()
    token = await repository.create_callback_token(
        chat_key="chat:1",
        topic_id=None,
        action="resolve_approval",
        payload={"request_id": 7, "decision": "approve"},
        expires_at="2999-01-01T00:00:00+00:00",
    )
    pending = PendingApproval(
        request_id=7,
        chat_key="chat:1",
        logical_thread_id="thread-1",
        codex_thread_id="codex-1",
        turn_id="turn-1",
        method="item/commandExecution/requestApproval",
        command="uv run pytest",
        reason="Run tests",
    )

    service = AsyncMock()
    service.pending_request_for_chat.return_value = pending
    service.resolve_pending_request.return_value = "Request approved."
    bot = AsyncMock()
    runner = TelegramBotRunner(
        bot,
        Dispatcher(),
        service,
        repository,
        progress_store,
        None,
        False,
    )
    watched = AsyncMock()
    runner._watch_turn_after_approval = watched  # type: ignore[method-assign]
    scheduled: list[Coroutine[object, object, None]] = []

    def capture_background(coro: Coroutine[object, object, None]) -> None:
        scheduled.append(coro)

    runner._spawn_background = capture_background  # type: ignore[method-assign]
    callback = SimpleNamespace(
        data=f"ct:{token}",
        message=SimpleNamespace(chat=SimpleNamespace(id=1), message_thread_id=None),
        answer=AsyncMock(),
    )

    await runner._on_callback_query(cast(CallbackQuery, callback))

    service.resolve_pending_request.assert_awaited_once_with(7, "approve")
    callback.answer.assert_awaited_once()
    assert bot.send_message.await_args.kwargs["text"] == "Request approved."
    assert len(scheduled) == 1
    await scheduled[0]
    watched.assert_awaited_once_with(
        ChatContext(chat_key="chat:1", chat_id=1, topic_id=None),
        pending,
    )


@pytest.mark.asyncio
async def test_callback_rejects_mismatched_approval_token(
    tmp_path: Path,
) -> None:
    repository = SQLiteStateRepository(tmp_path / "state.db")
    progress_store = SQLiteTelegramProgressStore(tmp_path / "state.db")
    await repository.initialize()
    await progress_store.initialize()
    token = await repository.create_callback_token(
        chat_key="chat:1",
        topic_id=None,
        action="resolve_approval",
        payload={"request_id": 7, "decision": "approve"},
        expires_at="2999-01-01T00:00:00+00:00",
    )

    service = AsyncMock()
    service.pending_request_for_chat.return_value = PendingApproval(
        request_id=8,
        chat_key="chat:1",
        logical_thread_id="thread-1",
        codex_thread_id="codex-1",
        turn_id="turn-1",
        method="item/commandExecution/requestApproval",
        command="uv run pytest",
        reason="Run tests",
    )
    bot = AsyncMock()
    runner = TelegramBotRunner(
        bot,
        Dispatcher(),
        service,
        repository,
        progress_store,
        None,
        False,
    )
    runner._spawn_background = AsyncMock()  # type: ignore[method-assign]
    callback = SimpleNamespace(
        data=f"ct:{token}",
        message=SimpleNamespace(chat=SimpleNamespace(id=1), message_thread_id=None),
        answer=AsyncMock(),
    )

    await runner._on_callback_query(cast(CallbackQuery, callback))

    service.resolve_pending_request.assert_not_awaited()
    runner._spawn_background.assert_not_called()
    callback.answer.assert_awaited_once()
    assert (
        bot.send_message.await_args.kwargs["text"]
        == "That approval request is no longer pending."
    )


@pytest.mark.asyncio
async def test_slash_approve_is_not_a_text_fallback(
    tmp_path: Path,
) -> None:
    repository = SQLiteStateRepository(tmp_path / "state.db")
    progress_store = SQLiteTelegramProgressStore(tmp_path / "state.db")
    await repository.initialize()
    await progress_store.initialize()

    service = AsyncMock()
    service.ensure_active_thread.return_value = LogicalThread(
        thread_id="thread-1",
        chat_key="chat:1",
        title="Chat",
        codex_thread_id="codex-1",
        created_at="now",
        updated_at="now",
        turn_count=0,
        awaiting_reply=False,
        interrupted_notice=False,
        pending_turn_id=None,
    )
    service.take_interrupted_notice.return_value = False
    service.run_turn.return_value = TurnRunResult(
        result=TurnResult(
            turn_id="turn-1",
            chat_key="chat:1",
            logical_thread_id="thread-1",
            codex_thread_id="codex-1",
            status="completed",
            final_text="handled as message",
        ),
        remapped=False,
        remap_warning=None,
    )
    bot = AsyncMock()
    runner = TelegramBotRunner(
        bot,
        Dispatcher(),
        service,
        repository,
        progress_store,
        None,
        False,
    )
    message = SimpleNamespace(
        chat=SimpleNamespace(id=1),
        message_thread_id=None,
        text="/approve",
        reply_to_message=None,
    )

    await runner._on_message(cast(Message, message))

    service.pending_request_for_chat.assert_not_awaited()
    service.resolve_pending_request.assert_not_awaited()
    service.run_turn.assert_awaited_once()
    assert service.run_turn.await_args.args[1].text == "/approve"
    assert bot.send_message.await_args.kwargs["text"] == "✅ handled as message"


@pytest.mark.asyncio
async def test_message_routes_to_realtime_when_active(tmp_path: Path) -> None:
    repository = SQLiteStateRepository(tmp_path / "state.db")
    progress_store = SQLiteTelegramProgressStore(tmp_path / "state.db")
    await repository.initialize()
    await progress_store.initialize()

    service = AsyncMock()
    service.pending_user_input_for_chat.return_value = None
    service.ensure_active_thread.return_value = LogicalThread(
        thread_id="thread-1",
        chat_key="chat:1",
        title="Chat",
        codex_thread_id="codex-1",
        created_at="now",
        updated_at="now",
        turn_count=0,
        awaiting_reply=False,
        interrupted_notice=False,
        pending_turn_id=None,
    )
    service.take_interrupted_notice.return_value = False
    service.route_realtime_input.return_value = True
    bot = AsyncMock()
    runner = TelegramBotRunner(
        bot,
        Dispatcher(),
        service,
        repository,
        progress_store,
        None,
        False,
    )
    message = SimpleNamespace(
        chat=SimpleNamespace(id=1),
        message_thread_id=None,
        text="Keep going.",
        reply_to_message=None,
        photo=None,
        document=None,
        voice=None,
        audio=None,
    )

    await runner._on_message(cast(Message, message))

    service.route_realtime_input.assert_awaited_once()
    assert service.route_realtime_input.await_args.args[1].text == "Keep going."
    service.run_turn.assert_not_called()


@pytest.mark.asyncio
async def test_realtime_rejects_images_with_short_message(tmp_path: Path) -> None:
    repository = SQLiteStateRepository(tmp_path / "state.db")
    progress_store = SQLiteTelegramProgressStore(tmp_path / "state.db")
    await repository.initialize()
    await progress_store.initialize()

    service = AsyncMock()
    service.pending_user_input_for_chat.return_value = None
    service.ensure_active_thread.return_value = LogicalThread(
        thread_id="thread-1",
        chat_key="chat:1",
        title="Chat",
        codex_thread_id="codex-1",
        created_at="now",
        updated_at="now",
        turn_count=0,
        awaiting_reply=False,
        interrupted_notice=False,
        pending_turn_id=None,
    )
    service.take_interrupted_notice.return_value = False
    service.route_realtime_input.side_effect = ValueError(
        "Realtime mode only accepts text and voice notes for now."
    )
    bot = AsyncMock()
    bot.get_file.return_value = SimpleNamespace(file_path="image.jpg")

    async def download(_remote_file, *, destination: Path) -> None:
        destination.write_bytes(b"image")

    bot.download.side_effect = download
    runner = TelegramBotRunner(
        bot,
        Dispatcher(),
        service,
        repository,
        progress_store,
        None,
        False,
    )
    message = SimpleNamespace(
        chat=SimpleNamespace(id=1),
        message_thread_id=None,
        text="",
        reply_to_message=None,
        photo=[SimpleNamespace(file_id="photo-id", file_size=100)],
        document=None,
        voice=None,
        audio=None,
    )

    await runner._on_message(cast(Message, message))

    assert (
        bot.send_message.await_args.kwargs["text"]
        == "❌ Realtime mode only accepts text and voice notes for now."
    )
    service.run_turn.assert_not_called()


@pytest.mark.asyncio
async def test_voice_message_routes_transcript_to_realtime(tmp_path: Path) -> None:
    repository = SQLiteStateRepository(tmp_path / "state.db")
    progress_store = SQLiteTelegramProgressStore(tmp_path / "state.db")
    await repository.initialize()
    await progress_store.initialize()

    service = AsyncMock()
    service.pending_user_input_for_chat.return_value = None
    service.ensure_active_thread.return_value = LogicalThread(
        thread_id="thread-1",
        chat_key="chat:1",
        title="Chat",
        codex_thread_id="codex-1",
        created_at="now",
        updated_at="now",
        turn_count=0,
        awaiting_reply=False,
        interrupted_notice=False,
        pending_turn_id=None,
    )
    service.take_interrupted_notice.return_value = False
    service.route_realtime_input.return_value = True
    speech_client = AsyncMock()
    speech_client.transcribe.return_value = "voice transcript"
    bot = AsyncMock()
    bot.get_file.return_value = SimpleNamespace(file_path="voice.ogg")

    async def download(_remote_file, *, destination: Path) -> None:
        destination.write_bytes(b"audio")

    bot.download.side_effect = download
    runner = TelegramBotRunner(
        bot,
        Dispatcher(),
        service,
        repository,
        progress_store,
        None,
        False,
        speech_client,
    )
    message = SimpleNamespace(
        chat=SimpleNamespace(id=1),
        message_thread_id=None,
        text=None,
        reply_to_message=None,
        photo=None,
        document=None,
        voice=SimpleNamespace(file_id="voice-file-id"),
        audio=None,
    )

    await runner._on_message(cast(Message, message))

    service.route_realtime_input.assert_awaited_once()
    assert service.route_realtime_input.await_args.args[1].text == "voice transcript"
    service.run_turn.assert_not_called()


@pytest.mark.asyncio
async def test_approval_request_forks_progress_message_stream(
    tmp_path: Path,
) -> None:
    repository = SQLiteStateRepository(tmp_path / "state.db")
    progress_store = SQLiteTelegramProgressStore(tmp_path / "state.db")
    await repository.initialize()
    await progress_store.initialize()
    await progress_store.save_progress(
        "thread-1",
        message_id=55,
        rendered_text="pre-approval progress",
    )

    bot = AsyncMock()
    bot.send_message.return_value.message_id = 88
    service = AsyncMock()
    service.pending_request_for_chat.return_value = PendingApproval(
        request_id=7,
        chat_key="chat:1",
        logical_thread_id="thread-1",
        codex_thread_id="codex-1",
        turn_id="turn-1",
        method="item/commandExecution/requestApproval",
        command="python -V",
        reason="Inspect status",
    )
    runner = TelegramBotRunner(
        bot,
        Dispatcher(),
        service,
        repository,
        progress_store,
        None,
        False,
    )

    await runner._finish_run_result(
        ChatContext(chat_key="chat:1", chat_id=1, topic_id=None),
        TurnRunResult(
            result=TurnResult(
                turn_id="turn-1",
                chat_key="chat:1",
                logical_thread_id="thread-1",
                codex_thread_id="codex-1",
                status="approvalRequired",
                final_text="",
            ),
            remapped=False,
            remap_warning=None,
        ),
    )

    bot.send_message.assert_awaited_once()
    assert await progress_store.get_progress("thread-1") is None

    bot.reset_mock()
    bot.send_message.return_value.message_id = 89
    await runner._handle_turn_update(
        ChatContext(chat_key="chat:1", chat_id=1, topic_id=None),
        TurnUpdate(
            turn_id="turn-1",
            chat_key="chat:1",
            logical_thread_id="thread-1",
            codex_thread_id="codex-1",
            status="inProgress",
            source="item/completed",
            text="Post approval progress.",
        ),
    )

    bot.edit_message_text.assert_not_awaited()
    bot.send_message.assert_awaited_once_with(
        chat_id=1,
        text="💭 Post approval progress.",
        message_thread_id=None,
        parse_mode="HTML",
    )
    progress = await progress_store.get_progress("thread-1")
    assert progress is not None
    assert progress.message_id == 89


@pytest.mark.asyncio
async def test_user_input_request_sends_inline_keyboard(tmp_path: Path) -> None:
    repository = SQLiteStateRepository(tmp_path / "state.db")
    progress_store = SQLiteTelegramProgressStore(tmp_path / "state.db")
    await repository.initialize()
    await progress_store.initialize()
    await progress_store.save_progress(
        "thread-1",
        message_id=55,
        rendered_text="pre-question progress",
    )

    bot = AsyncMock()
    bot.send_message.return_value.message_id = 88
    service = AsyncMock()
    service.pending_user_input_for_chat.return_value = _pending_user_input()
    runner = TelegramBotRunner(
        bot,
        Dispatcher(),
        service,
        repository,
        progress_store,
        None,
        False,
    )

    await runner._finish_run_result(
        ChatContext(chat_key="chat:1", chat_id=1, topic_id=None),
        TurnRunResult(
            result=TurnResult(
                turn_id="turn-1",
                chat_key="chat:1",
                logical_thread_id="thread-1",
                codex_thread_id="codex-1",
                status="userInputRequired",
                final_text="",
            ),
            remapped=False,
            remap_warning=None,
        ),
    )

    bot.send_message.assert_awaited_once()
    assert "Which scope?" in bot.send_message.await_args.kwargs["text"]
    assert bot.send_message.await_args.kwargs["reply_markup"] is not None
    assert await progress_store.get_progress("thread-1") is None


@pytest.mark.asyncio
async def test_single_question_callback_submits_answer(tmp_path: Path) -> None:
    repository = SQLiteStateRepository(tmp_path / "state.db")
    progress_store = SQLiteTelegramProgressStore(tmp_path / "state.db")
    await repository.initialize()
    await progress_store.initialize()
    await repository.add_pending_user_input(_pending_user_input())
    token = await repository.create_callback_token(
        chat_key="chat:1",
        topic_id=None,
        action="user_input_select",
        payload={
            "request_id": 9,
            "question_id": "scope",
            "answer": "Native first",
        },
        expires_at="2999-01-01T00:00:00+00:00",
    )

    service = AsyncMock()
    service.resolve_pending_user_input.return_value = "Response submitted."
    service.continue_turn.return_value = TurnResult(
        turn_id="turn-1",
        chat_key="chat:1",
        logical_thread_id="thread-1",
        codex_thread_id="codex-1",
        status="completed",
        final_text="done",
    )
    bot = AsyncMock()
    bot.send_message.return_value.message_id = 88
    runner = TelegramBotRunner(
        bot,
        Dispatcher(),
        service,
        repository,
        progress_store,
        None,
        False,
    )
    callback = SimpleNamespace(
        data=f"ct:{token}",
        message=SimpleNamespace(
            chat=SimpleNamespace(id=1),
            message_thread_id=None,
            message_id=55,
        ),
        answer=AsyncMock(),
    )

    await runner._on_callback_query(cast(CallbackQuery, callback))
    await asyncio.sleep(0)

    service.resolve_pending_user_input.assert_awaited_once_with(
        9,
        {"scope": ("Native first",)},
    )
    callback.answer.assert_awaited_once()


@pytest.mark.asyncio
async def test_multi_question_callback_waits_for_submit(tmp_path: Path) -> None:
    repository = SQLiteStateRepository(tmp_path / "state.db")
    progress_store = SQLiteTelegramProgressStore(tmp_path / "state.db")
    await repository.initialize()
    await progress_store.initialize()
    await repository.add_pending_user_input(
        _pending_user_input(
            questions=(
                UserInputQuestion(
                    question_id="scope",
                    question="Which scope?",
                    options=(UserInputOption(label="Native first"),),
                ),
                UserInputQuestion(
                    question_id="rollout",
                    question="When deploy?",
                    options=(UserInputOption(label="Later"),),
                ),
            )
        )
    )
    scope_token = await repository.create_callback_token(
        chat_key="chat:1",
        topic_id=None,
        action="user_input_select",
        payload={
            "request_id": 9,
            "question_id": "scope",
            "answer": "Native first",
        },
        expires_at="2999-01-01T00:00:00+00:00",
    )
    rollout_token = await repository.create_callback_token(
        chat_key="chat:1",
        topic_id=None,
        action="user_input_select",
        payload={
            "request_id": 9,
            "question_id": "rollout",
            "answer": "Later",
        },
        expires_at="2999-01-01T00:00:00+00:00",
    )
    submit_token = await repository.create_callback_token(
        chat_key="chat:1",
        topic_id=None,
        action="user_input_submit",
        payload={"request_id": 9},
        expires_at="2999-01-01T00:00:00+00:00",
    )

    service = AsyncMock()
    service.resolve_pending_user_input.return_value = "Response submitted."
    service.continue_turn.return_value = TurnResult(
        turn_id="turn-1",
        chat_key="chat:1",
        logical_thread_id="thread-1",
        codex_thread_id="codex-1",
        status="completed",
        final_text="done",
    )
    bot = AsyncMock()
    bot.send_message.return_value.message_id = 88
    runner = TelegramBotRunner(
        bot,
        Dispatcher(),
        service,
        repository,
        progress_store,
        None,
        False,
    )

    for token in (scope_token, rollout_token):
        callback = SimpleNamespace(
            data=f"ct:{token}",
            message=SimpleNamespace(
                chat=SimpleNamespace(id=1),
                message_thread_id=None,
                message_id=55,
            ),
            answer=AsyncMock(),
        )
        await runner._on_callback_query(cast(CallbackQuery, callback))

    service.resolve_pending_user_input.assert_not_called()

    callback = SimpleNamespace(
        data=f"ct:{submit_token}",
        message=SimpleNamespace(
            chat=SimpleNamespace(id=1),
            message_thread_id=None,
            message_id=55,
        ),
        answer=AsyncMock(),
    )
    await runner._on_callback_query(cast(CallbackQuery, callback))
    await asyncio.sleep(0)

    service.resolve_pending_user_input.assert_awaited_once_with(
        9,
        {
            "scope": ("Native first",),
            "rollout": ("Later",),
        },
    )


@pytest.mark.asyncio
async def test_other_text_reply_submits_free_text_answer(tmp_path: Path) -> None:
    repository = SQLiteStateRepository(tmp_path / "state.db")
    progress_store = SQLiteTelegramProgressStore(tmp_path / "state.db")
    await repository.initialize()
    await progress_store.initialize()
    await repository.add_pending_user_input(_pending_user_input())
    await repository.update_pending_user_input_selection(
        9,
        question_id="scope",
        answers=(),
        awaiting_free_text=True,
    )

    service = AsyncMock()

    async def pending_for_chat(chat_key: str) -> PendingUserInput | None:
        return await repository.get_pending_user_input(chat_key)

    service.pending_user_input_for_chat.side_effect = pending_for_chat
    service.resolve_pending_user_input.return_value = "Response submitted."
    service.continue_turn.return_value = TurnResult(
        turn_id="turn-1",
        chat_key="chat:1",
        logical_thread_id="thread-1",
        codex_thread_id="codex-1",
        status="completed",
        final_text="done",
    )
    bot = AsyncMock()
    bot.send_message.return_value.message_id = 88
    runner = TelegramBotRunner(
        bot,
        Dispatcher(),
        service,
        repository,
        progress_store,
        None,
        False,
    )
    message = SimpleNamespace(
        chat=SimpleNamespace(id=1),
        message_thread_id=None,
        text="Use the native path only",
        reply_to_message=None,
    )

    await runner._on_message(cast(Message, message))
    await asyncio.sleep(0)

    service.run_turn.assert_not_called()
    service.resolve_pending_user_input.assert_awaited_once_with(
        9,
        {"scope": ("Use the native path only",)},
    )


@pytest.mark.asyncio
async def test_pending_user_input_blocks_new_turn(tmp_path: Path) -> None:
    repository = SQLiteStateRepository(tmp_path / "state.db")
    progress_store = SQLiteTelegramProgressStore(tmp_path / "state.db")
    await repository.initialize()
    await progress_store.initialize()
    await repository.add_pending_user_input(_pending_user_input())

    service = AsyncMock()

    async def pending_for_chat(chat_key: str) -> PendingUserInput | None:
        return await repository.get_pending_user_input(chat_key)

    service.pending_user_input_for_chat.side_effect = pending_for_chat
    bot = AsyncMock()
    bot.send_message.return_value.message_id = 88
    runner = TelegramBotRunner(
        bot,
        Dispatcher(),
        service,
        repository,
        progress_store,
        None,
        False,
    )
    message = SimpleNamespace(
        chat=SimpleNamespace(id=1),
        message_thread_id=None,
        text="This should not steer the active turn.",
        reply_to_message=None,
    )

    await runner._on_message(cast(Message, message))

    service.ensure_active_thread.assert_not_called()
    service.run_turn.assert_not_called()
    assert "Please answer" in bot.send_message.await_args.kwargs["text"]


@pytest.mark.asyncio
async def test_runtime_error_reports_actual_summary(tmp_path: Path) -> None:
    repository = SQLiteStateRepository(tmp_path / "state.db")
    progress_store = SQLiteTelegramProgressStore(tmp_path / "state.db")
    await repository.initialize()
    await progress_store.initialize()

    bot = AsyncMock()
    runner = TelegramBotRunner(
        bot,
        Dispatcher(),
        AsyncMock(),
        repository,
        progress_store,
        None,
        False,
    )

    await runner._send_runtime_error(
        ChatContext(chat_key="chat:1", chat_id=1, topic_id=None),
        _LongTelegramMessageError(),
    )

    bot.send_message.assert_awaited_once()
    sent_text = bot.send_message.await_args.kwargs["text"]
    assert sent_text == (
        "⚠️ <b>Message handling failed</b>\n"
        "Telegram rejected a bot message because it was too long."
    )
    assert bot.send_message.await_args.kwargs["parse_mode"] == "HTML"


@pytest.mark.asyncio
async def test_final_reply_retries_after_throttle_before_editing_existing_progress(
    tmp_path: Path,
) -> None:
    repository = SQLiteStateRepository(tmp_path / "state.db")
    progress_store = SQLiteTelegramProgressStore(tmp_path / "state.db")
    await repository.initialize()
    await progress_store.initialize()
    await progress_store.save_progress(
        "thread-1",
        message_id=55,
        rendered_text="partial text",
    )

    bot = AsyncMock()
    bot.edit_message_text.side_effect = [_RetryAfterError(0.0), None]
    runner = TelegramBotRunner(
        bot,
        Dispatcher(),
        AsyncMock(),
        repository,
        progress_store,
        None,
        False,
    )

    await runner._send_final(
        ChatContext(chat_key="chat:1", chat_id=1, topic_id=None),
        TurnResult(
            turn_id="turn-1",
            chat_key="chat:1",
            logical_thread_id="thread-1",
            codex_thread_id="codex-1",
            status="completed",
            final_text="final text",
        ),
    )

    assert bot.edit_message_text.await_count == 2
    bot.send_message.assert_not_awaited()
    assert await progress_store.get_progress("thread-1") is None
