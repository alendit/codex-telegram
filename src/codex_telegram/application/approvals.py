"""Approval and user-input request application service."""

from __future__ import annotations

from typing import Protocol

from codex_telegram.application.ports import CodexBackend
from codex_telegram.domain import PendingApproval, PendingUserInput

APPROVAL_DECISIONS = {
    "approve": "accept",
    "approve for session": "acceptForSession",
    "deny": "decline",
    "cancel": "cancel",
}


class ApprovalRepository(Protocol):
    """State needed by approval and user-input resolution policy."""

    async def get_pending_request(self, chat_key: str) -> PendingApproval | None: ...
    async def pop_pending_request(self, request_id: int) -> PendingApproval | None: ...
    async def clear_pending_for_thread(self, thread_id: str) -> None: ...
    async def get_pending_user_input(
        self, chat_key: str
    ) -> PendingUserInput | None: ...
    async def pop_pending_user_input(
        self, request_id: int
    ) -> PendingUserInput | None: ...
    async def clear_pending_user_input_for_thread(self, thread_id: str) -> None: ...


class ApprovalService:
    """Own pending approval and user-input resolution policy."""

    def __init__(self, repository: ApprovalRepository, client: CodexBackend) -> None:
        self._repository = repository
        self._client = client

    async def pending_request_for_chat(self, chat_key: str) -> PendingApproval | None:
        """Return the oldest pending approval for this chat."""
        return await self._repository.get_pending_request(chat_key)

    async def pending_user_input_for_chat(
        self, chat_key: str
    ) -> PendingUserInput | None:
        """Return the oldest pending user-input request for this chat."""
        return await self._repository.get_pending_user_input(chat_key)

    async def resolve_pending_request(self, request_id: int, decision: str) -> str:
        """Resolve one pending approval request."""
        if decision not in APPROVAL_DECISIONS:
            raise ValueError(f"Unknown approval decision: {decision}")
        pending = await self._repository.pop_pending_request(request_id)
        if pending is None:
            return "No pending approval."
        await self._client.resolve_server_request(
            request_id,
            {"decision": APPROVAL_DECISIONS[decision]},
            codex_backend_id=pending.codex_backend_id,
        )
        await self._repository.clear_pending_for_thread(pending.logical_thread_id)
        if decision == "cancel":
            return "Turn canceled."
        if decision == "deny":
            return "Request denied."
        if decision == "approve for session":
            return "Request approved for the session."
        return "Request approved."

    async def resolve_pending_user_input(
        self,
        request_id: int,
        answers: dict[str, tuple[str, ...]],
    ) -> str:
        """Resolve one pending user-input request."""
        pending = await self._repository.pop_pending_user_input(request_id)
        if pending is None:
            return "No pending question."
        await self._client.resolve_server_request(
            request_id,
            {
                "answers": {
                    question_id: {"answers": list(values)}
                    for question_id, values in answers.items()
                }
            },
            codex_backend_id=pending.codex_backend_id,
        )
        await self._repository.clear_pending_user_input_for_thread(
            pending.logical_thread_id
        )
        return "Response submitted."
