from unittest.mock import AsyncMock

import pytest

from codex_telegram.application.ports import CodexBackendError
from codex_telegram.application.turn_stream import TurnStreamConfig, TurnStreamService
from codex_telegram.domain import PendingApproval, TurnResult, TurnResultImage


def _service(repository: AsyncMock, client: AsyncMock) -> TurnStreamService:
    return TurnStreamService(
        TurnStreamConfig(turn_poll_seconds=0.01, wait_notice_seconds=60.0),
        repository,
        client,
    )


@pytest.mark.asyncio
async def test_complete_turn_persists_final_result_and_clears_pending_state() -> None:
    repository = AsyncMock()
    client = AsyncMock()
    client.wait_for_turn_event.return_value = TurnResult(
        turn_id="turn-1",
        chat_key="chat:1",
        logical_thread_id="backend-thread",
        codex_thread_id="codex-1",
        codex_backend_id="laptop",
        status="completed",
        final_text="Done.",
    )
    service = _service(repository, client)

    result = await service.complete_turn(
        "chat:1",
        "thread-1",
        "turn-1",
        "laptop",
        on_update=None,
        on_wait_notice=None,
    )

    assert result.logical_thread_id == "thread-1"
    assert result.chat_key == "chat:1"
    repository.clear_pending_for_thread.assert_awaited_once_with("thread-1")
    repository.clear_pending_user_input_for_thread.assert_awaited_once_with("thread-1")
    repository.mark_turn_completed.assert_awaited_once_with("thread-1")
    repository.add_thread_message.assert_awaited_once_with(
        "thread-1",
        role="assistant",
        kind="final",
        text="Done.",
        turn_id="turn-1",
    )


@pytest.mark.asyncio
async def test_complete_turn_persists_final_result_images() -> None:
    repository = AsyncMock()
    client = AsyncMock()
    client.wait_for_turn_event.return_value = TurnResult(
        turn_id="turn-1",
        chat_key="chat:1",
        logical_thread_id="backend-thread",
        codex_thread_id="codex-1",
        codex_backend_id="laptop",
        status="completed",
        final_text="Done.",
        images=(TurnResultImage(source="/agent/generated.png", caption="caption"),),
    )
    service = _service(repository, client)

    await service.complete_turn(
        "chat:1",
        "thread-1",
        "turn-1",
        "laptop",
        on_update=None,
        on_wait_notice=None,
    )

    assert repository.add_thread_message.await_args_list[-1].kwargs == {
        "role": "assistant",
        "kind": "final_image",
        "text": '{"source": "/agent/generated.png", "caption": "caption"}',
        "turn_id": "turn-1",
    }


@pytest.mark.asyncio
async def test_consume_turn_stream_persists_pending_approval_without_completion() -> (
    None
):
    repository = AsyncMock()
    client = AsyncMock()
    pending = PendingApproval(
        request_id=7,
        chat_key="chat:1",
        logical_thread_id="thread-1",
        codex_thread_id="codex-1",
        turn_id="turn-1",
        method="exec",
        command="uv run pytest",
        reason="Run tests",
        codex_backend_id="laptop",
    )
    client.wait_for_turn_event.return_value = pending
    service = _service(repository, client)

    result = await service.consume_turn_stream(
        "chat:1",
        "thread-1",
        "turn-1",
        "laptop",
        on_update=None,
        on_wait_notice=None,
    )

    assert result.status == "approvalRequired"
    assert result.codex_thread_id == "codex-1"
    repository.add_pending_request.assert_awaited_once_with(pending)
    repository.mark_turn_completed.assert_not_called()
    repository.add_thread_message.assert_not_called()


@pytest.mark.asyncio
async def test_complete_turn_returns_backend_failure_and_clears_pending_state() -> None:
    repository = AsyncMock()
    client = AsyncMock()
    client.wait_for_turn_event.side_effect = CodexBackendError(
        "No PONG received after 15.0 seconds",
        backend_id="mac",
    )
    service = _service(repository, client)

    result = await service.complete_turn(
        "chat:1",
        "thread-1",
        "turn-1",
        "mac",
        on_update=None,
        on_wait_notice=None,
    )

    assert result.status == "failed"
    assert result.error == (
        "Codex backend 'mac' is unavailable: "
        "No PONG received after 15.0 seconds. "
        "The bridge cleared this turn, so this conversation is idle. "
        "Try again after the backend is reachable or start a new conversation "
        "with another --connection."
    )
    repository.clear_pending_for_thread.assert_awaited_once_with("thread-1")
    repository.clear_pending_user_input_for_thread.assert_awaited_once_with("thread-1")
    repository.mark_turn_failed.assert_awaited_once_with("thread-1")
    repository.add_thread_message.assert_awaited_once_with(
        "thread-1",
        role="system",
        kind="error",
        text=result.error,
        turn_id="turn-1",
    )
