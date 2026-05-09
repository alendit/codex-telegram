"""Codex turn stream collection and terminal result persistence."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import UTC, datetime
import json
from typing import Protocol

from codex_telegram.application.ports import (
    CodexBackend,
    CodexBackendError,
    TurnStateChangeHandler,
    TurnUpdateHandler,
    WaitNoticeHandler,
)
from codex_telegram.domain import (
    LogicalThread,
    PendingApproval,
    PendingUserInput,
    ThreadMessage,
    TurnResult,
    TurnUpdate,
)


@dataclass(frozen=True, slots=True)
class TurnStreamConfig:
    """Polling and wait-notice timing for Codex turn streams."""

    turn_poll_seconds: float
    wait_notice_seconds: float


class TurnStreamRepository(Protocol):
    """State needed by turn stream collection and terminal persistence."""

    async def get_thread(self, thread_id: str) -> LogicalThread | None: ...
    async def add_pending_request(self, request: PendingApproval) -> None: ...
    async def add_pending_user_input(self, request: PendingUserInput) -> None: ...
    async def clear_pending_for_thread(self, thread_id: str) -> None: ...
    async def clear_pending_user_input_for_thread(self, thread_id: str) -> None: ...
    async def mark_turn_completed(self, thread_id: str) -> None: ...
    async def mark_turn_failed(self, thread_id: str) -> None: ...
    async def add_thread_message(
        self,
        thread_id: str,
        *,
        role: str,
        kind: str,
        text: str,
        turn_id: str | None = None,
    ) -> None: ...


class TurnStreamService:
    """Own app-server turn stream collection and terminal transcript persistence."""

    def __init__(
        self,
        config: TurnStreamConfig,
        repository: TurnStreamRepository,
        client: CodexBackend,
    ) -> None:
        self._config = config
        self._repository = repository
        self._client = client

    async def continue_turn(
        self,
        chat_key: str,
        thread_id: str,
        turn_id: str,
        *,
        on_update: TurnUpdateHandler | None = None,
        on_wait_notice: WaitNoticeHandler | None = None,
        on_state_change: TurnStateChangeHandler | None = None,
    ) -> TurnResult:
        """Continue watching an already-started turn after approval."""
        thread = await self._repository.get_thread(thread_id)
        codex_backend_id = thread.codex_backend_id if thread is not None else "primary"
        return await self.complete_turn(
            chat_key,
            thread_id,
            turn_id,
            codex_backend_id,
            on_update=on_update,
            on_wait_notice=on_wait_notice,
            on_state_change=on_state_change,
        )

    async def complete_turn(
        self,
        chat_key: str,
        thread_id: str,
        turn_id: str,
        codex_backend_id: str,
        *,
        on_update: TurnUpdateHandler | None,
        on_wait_notice: WaitNoticeHandler | None,
        on_state_change: TurnStateChangeHandler | None = None,
    ) -> TurnResult:
        """Collect a started turn result and persist terminal transcript rows."""
        result = await self.consume_turn_stream(
            chat_key,
            thread_id,
            turn_id,
            codex_backend_id,
            on_update=on_update,
            on_wait_notice=on_wait_notice,
            on_state_change=on_state_change,
        )
        await self.persist_terminal_result(thread_id, result)
        return result

    async def consume_turn_stream(
        self,
        chat_key: str,
        thread_id: str,
        turn_id: str,
        codex_backend_id: str,
        *,
        on_update: TurnUpdateHandler | None,
        on_wait_notice: WaitNoticeHandler | None,
        on_state_change: TurnStateChangeHandler | None = None,
    ) -> TurnResult:
        """Poll app-server events until the turn blocks or reaches a result."""
        last_notice_at = datetime.now(UTC)
        while True:
            try:
                event = await self._client.wait_for_turn_event(
                    turn_id,
                    timeout=self._config.turn_poll_seconds,
                    codex_backend_id=codex_backend_id,
                )
            except TimeoutError:
                now = datetime.now(UTC)
                if (
                    now - last_notice_at
                ).total_seconds() >= self._config.wait_notice_seconds:
                    last_notice_at = now
                    if on_wait_notice is not None:
                        await on_wait_notice()
                continue
            except CodexBackendError as err:
                await self._repository.clear_pending_for_thread(thread_id)
                await self._repository.clear_pending_user_input_for_thread(thread_id)
                await self._repository.mark_turn_failed(thread_id)
                await _notify_state_change(on_state_change)
                return TurnResult(
                    turn_id=turn_id,
                    chat_key=chat_key,
                    logical_thread_id=thread_id,
                    codex_thread_id="",
                    codex_backend_id=err.backend_id or codex_backend_id,
                    status="failed",
                    final_text="",
                    error=backend_failure_message(err, codex_backend_id),
                )

            if isinstance(event, TurnUpdate):
                if on_update is not None:
                    await on_update(event)
                continue

            if isinstance(event, PendingApproval):
                await self._repository.add_pending_request(event)
                await _notify_state_change(on_state_change)
                return TurnResult(
                    turn_id=turn_id,
                    chat_key=chat_key,
                    logical_thread_id=thread_id,
                    codex_thread_id=event.codex_thread_id,
                    codex_backend_id=event.codex_backend_id,
                    status="approvalRequired",
                    final_text="",
                )

            if isinstance(event, PendingUserInput):
                await self._repository.add_pending_user_input(event)
                await _notify_state_change(on_state_change)
                return TurnResult(
                    turn_id=turn_id,
                    chat_key=chat_key,
                    logical_thread_id=thread_id,
                    codex_thread_id=event.codex_thread_id,
                    codex_backend_id=event.codex_backend_id,
                    status="userInputRequired",
                    final_text="",
                )

            await self._repository.clear_pending_for_thread(thread_id)
            await self._repository.clear_pending_user_input_for_thread(thread_id)
            await self._repository.mark_turn_completed(thread_id)
            await _notify_state_change(on_state_change)
            return replace(event, logical_thread_id=thread_id, chat_key=chat_key)

    async def persist_terminal_result(
        self,
        thread_id: str,
        result: TurnResult,
    ) -> None:
        """Persist terminal assistant or system transcript rows when present."""
        messages = _thread_messages_for_result(thread_id, result)
        if not messages:
            return
        for message in messages:
            await self._repository.add_thread_message(
                thread_id,
                role=message.role,
                kind=message.kind,
                text=message.text,
                turn_id=message.turn_id,
            )


def _thread_messages_for_result(
    thread_id: str,
    result: TurnResult,
) -> tuple[ThreadMessage, ...]:
    if result.status == "approvalRequired":
        return ()
    if result.error:
        kind = "interrupted" if result.status == "interrupted" else "error"
        return (
            ThreadMessage(
                message_id=None,
                thread_id=thread_id,
                role="system",
                kind=kind,
                text=result.error,
                created_at="",
                turn_id=result.turn_id,
            ),
        )
    messages: list[ThreadMessage] = []
    if result.final_text:
        messages.append(
            ThreadMessage(
                message_id=None,
                thread_id=thread_id,
                role="assistant",
                kind="final",
                text=result.final_text,
                created_at="",
                turn_id=result.turn_id,
            )
        )
    for image in result.images:
        messages.append(
            ThreadMessage(
                message_id=None,
                thread_id=thread_id,
                role="assistant",
                kind="final_image",
                text=json.dumps({"source": image.source, "caption": image.caption}),
                created_at="",
                turn_id=result.turn_id,
            )
        )
    return tuple(messages)


def backend_failure_message(err: CodexBackendError, fallback_backend_id: str) -> str:
    backend_id = err.backend_id or fallback_backend_id
    reason = " ".join(str(err).split()) or err.__class__.__name__
    return (
        f"Codex backend '{backend_id}' is unavailable: {reason}. "
        "The bridge cleared this turn, so this conversation is idle. "
        "Try again after the backend is reachable or start a new conversation "
        "with another --connection."
    )


async def _notify_state_change(
    on_state_change: TurnStateChangeHandler | None,
) -> None:
    if on_state_change is not None:
        await on_state_change()
