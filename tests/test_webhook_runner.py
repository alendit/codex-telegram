from unittest.mock import AsyncMock

import pytest
from aiogram import Dispatcher

from codex_telegram.adapters.persistence.sqlite import (
    SQLiteStateRepository,
    SQLiteTelegramProgressStore,
)
from codex_telegram.adapters.telegram.bot import TelegramBotRunner
from codex_telegram.application.service import TurnRunResult
from codex_telegram.domain import (
    LogicalThread,
    TurnResult,
    TurnUpdate,
    WebhookSubscription,
)


def _subscription() -> WebhookSubscription:
    return WebhookSubscription(
        webhook_id="wh_123",
        chat_key="chat:1",
        anchor_id="anchor-1",
        codex_backend_id="primary",
        codex_thread_id="codex-1",
        latest_bridge_id="thread-1",
        name="front-door",
        enabled=True,
        created_at="now",
        updated_at="now",
        trigger_count=0,
        last_triggered_at=None,
    )


@pytest.mark.asyncio
async def test_webhook_runner_uses_normal_turn_flow(tmp_path) -> None:
    repository = SQLiteStateRepository(tmp_path / "state.db")
    progress_store = SQLiteTelegramProgressStore(tmp_path / "state.db")
    await repository.initialize()
    await progress_store.initialize()

    service = AsyncMock()
    service.run_webhook_turn.return_value = TurnRunResult(
        result=TurnResult(
            turn_id="turn-1",
            chat_key="chat:1",
            logical_thread_id="thread-1",
            codex_thread_id="codex-1",
            status="completed",
            final_text="done",
        ),
        remapped=False,
        remap_warning=None,
    )
    service.pending_request_for_chat.return_value = None
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

    await runner.run_webhook_event(_subscription(), "check latest build")

    service.run_webhook_turn.assert_awaited_once()
    assert service.run_webhook_turn.await_args.args[:2] == (
        _subscription(),
        "check latest build",
    )
    bot.send_message.assert_awaited()


@pytest.mark.asyncio
async def test_webhook_runner_sends_final_without_legacy_wrap_edit(
    tmp_path,
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

    service = AsyncMock()
    service.run_webhook_turn.return_value = TurnRunResult(
        result=TurnResult(
            turn_id="turn-2",
            chat_key="chat:1",
            logical_thread_id="thread-2",
            codex_thread_id="codex-2",
            status="completed",
            final_text="done",
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

    await runner.run_webhook_event(_subscription(), "hello again")

    bot.edit_message_text.assert_not_awaited()
    assert bot.send_message.await_args_list[0].kwargs["text"] == "✅ done"


@pytest.mark.asyncio
async def test_webhook_runner_sends_progress_without_idle_rollover_notice(
    tmp_path,
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

    service = AsyncMock()

    async def run_webhook_turn(*args, **kwargs):
        await kwargs["on_update"](
            TurnUpdate(
                chat_key="chat:1",
                codex_thread_id="codex-2",
                logical_thread_id="thread-2",
                status="inProgress",
                turn_id="turn-2",
                text="working.",
                source="item/completed",
                visible=True,
            )
        )
        return TurnRunResult(
            result=TurnResult(
                turn_id="turn-2",
                chat_key="chat:1",
                logical_thread_id="thread-2",
                codex_thread_id="codex-2",
                status="completed",
                final_text="done",
            ),
            remapped=False,
            remap_warning=None,
        )

    service.run_webhook_turn.side_effect = run_webhook_turn
    service.pending_request_for_chat.return_value = None
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

    await runner.run_webhook_event(_subscription(), "hello again")

    assert bot.send_message.await_args_list[0].kwargs["text"] == "💭 working."
    assert bot.edit_message_text.await_args_list[0].kwargs == {
        "chat_id": 1,
        "message_id": 77,
        "text": "✅ done",
        "parse_mode": "HTML",
    }


@pytest.mark.asyncio
async def test_runner_notifies_interrupted_threads_on_startup(tmp_path) -> None:
    repository = SQLiteStateRepository(tmp_path / "state.db")
    progress_store = SQLiteTelegramProgressStore(tmp_path / "state.db")
    await repository.initialize()
    await progress_store.initialize()
    await repository.ensure_chat("chat:1")
    await repository.create_thread("chat:1", "thread-1", "New thread")
    await repository.mark_turn_started("thread-1", "turn-1")
    await repository.mark_waiting_threads_interrupted()
    await progress_store.save_progress(
        "thread-1",
        message_id=55,
        rendered_text="partial reply",
    )

    service = AsyncMock()
    service.list_interrupted_threads.return_value = [
        LogicalThread(
            thread_id="thread-1",
            chat_key="chat:1",
            title="Thread",
            codex_thread_id="codex-1",
            created_at="now",
            updated_at="now",
            turn_count=1,
            awaiting_reply=False,
            interrupted_notice=True,
            pending_turn_id=None,
        )
    ]
    service.take_interrupted_notice.return_value = True
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

    await runner._notify_interrupted_threads()

    service.list_interrupted_threads.assert_awaited_once_with()
    service.take_interrupted_notice.assert_awaited_once_with("thread-1")
    bot.send_message.assert_awaited_once()
    progress = await progress_store.get_progress("thread-1")
    assert progress is None


@pytest.mark.asyncio
async def test_webhook_runner_reports_steered_followup_without_final(tmp_path) -> None:
    repository = SQLiteStateRepository(tmp_path / "state.db")
    progress_store = SQLiteTelegramProgressStore(tmp_path / "state.db")
    await repository.initialize()
    await progress_store.initialize()

    service = AsyncMock()
    service.run_webhook_turn.return_value = TurnRunResult(
        result=None,
        remapped=False,
        remap_warning=None,
        active_turn_continues=True,
        active_turn_notice="Added your follow-up to the active Codex turn.",
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

    await runner.run_webhook_event(_subscription(), "follow up")

    bot.send_message.assert_awaited_once_with(
        chat_id=1,
        text="💭 Added your follow-up to the active Codex turn.",
        message_thread_id=None,
    )
    bot.edit_message_text.assert_not_awaited()
