from unittest.mock import AsyncMock

import pytest

from codex_telegram.application.approvals import ApprovalService
from codex_telegram.domain import PendingApproval, PendingUserInput


@pytest.mark.asyncio
async def test_resolve_pending_request_sends_normalized_decision() -> None:
    repository = AsyncMock()
    client = AsyncMock()
    repository.pop_pending_request.return_value = PendingApproval(
        request_id=7,
        chat_key="chat:1",
        logical_thread_id="thread-1",
        codex_thread_id="codex-1",
        turn_id="turn-1",
        method="exec",
        codex_backend_id="laptop",
        command="uv run pytest",
        reason="Run tests",
    )
    service = ApprovalService(repository, client)

    message = await service.resolve_pending_request(7, "approve for session")

    assert message == "Request approved for the session."
    client.resolve_server_request.assert_awaited_once_with(
        7,
        {"decision": "acceptForSession"},
        codex_backend_id="laptop",
    )
    repository.clear_pending_for_thread.assert_awaited_once_with("thread-1")


@pytest.mark.asyncio
async def test_resolve_pending_user_input_sends_answers_and_clears_state() -> None:
    repository = AsyncMock()
    client = AsyncMock()
    repository.pop_pending_user_input.return_value = PendingUserInput(
        request_id=9,
        chat_key="chat:1",
        logical_thread_id="thread-1",
        codex_thread_id="codex-1",
        turn_id="turn-1",
        method="request_user_input",
        questions=(),
        codex_backend_id="desktop",
    )
    service = ApprovalService(repository, client)

    message = await service.resolve_pending_user_input(
        9,
        {"choice": ("yes",), "tools": ("pytest", "mypy")},
    )

    assert message == "Response submitted."
    client.resolve_server_request.assert_awaited_once_with(
        9,
        {
            "answers": {
                "choice": {"answers": ["yes"]},
                "tools": {"answers": ["pytest", "mypy"]},
            }
        },
        codex_backend_id="desktop",
    )
    repository.clear_pending_user_input_for_thread.assert_awaited_once_with("thread-1")
