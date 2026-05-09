from pathlib import Path
from typing import cast
from unittest.mock import AsyncMock

import pytest

from codex_telegram.adapters.persistence.sqlite import SQLiteStateRepository
from codex_telegram.application.ports import CodexBackend
from codex_telegram.application.webhooks import WebhookService


class _Backend:
    def resolve_backend_id(
        self,
        *,
        backend_id: str | None = None,
        backend_name: str | None = None,
    ) -> str:
        return backend_id or backend_name or "primary"


@pytest.mark.asyncio
async def test_create_webhook_subscription_anchors_existing_codex_thread(
    tmp_path: Path,
) -> None:
    repository = SQLiteStateRepository(tmp_path / "state.db")
    await repository.initialize()
    service = WebhookService(repository, cast(CodexBackend, _Backend()), AsyncMock())

    created = await service.create_webhook_subscription(
        chat_key="chat:1",
        codex_backend_id="laptop",
        codex_thread_id="codex-1",
        name="  front   door  ",
    )

    assert created.subscription.chat_key == "chat:1"
    assert created.subscription.name == "front door"
    assert created.subscription.anchor_id is not None
    assert created.event_secret
    anchor = await repository.get_conversation_anchor(created.subscription.anchor_id)
    assert anchor is not None
    assert anchor.codex_backend_id == "laptop"
    assert anchor.codex_thread_id == "codex-1"


@pytest.mark.asyncio
async def test_accept_webhook_event_verifies_secret_and_deduplicates(
    tmp_path: Path,
) -> None:
    repository = SQLiteStateRepository(tmp_path / "state.db")
    await repository.initialize()
    service = WebhookService(repository, cast(CodexBackend, _Backend()), AsyncMock())
    created = await service.create_webhook_subscription(
        chat_key="chat:1",
        codex_thread_id="codex-1",
        name="front-door",
    )

    with pytest.raises(PermissionError):
        await service.accept_webhook_event(
            created.subscription.webhook_id,
            "wrong-secret",
            {"input": "door opened"},
        )

    dispatch = await service.accept_webhook_event(
        created.subscription.webhook_id,
        created.event_secret,
        {
            "input": "door opened",
            "metadata": {"source": "sensor"},
            "payload": {"state": "open"},
        },
        idempotency_key="event-1",
    )

    assert dispatch.duplicate is False
    assert dispatch.subscription.trigger_count == 1
    assert "Webhook: front-door" in dispatch.prompt
    assert "Human input:\ndoor opened" in dispatch.prompt
    assert '"state": "open"' in dispatch.prompt

    duplicate = await service.accept_webhook_event(
        created.subscription.webhook_id,
        created.event_secret,
        {"input": "door opened again"},
        idempotency_key="event-1",
    )

    assert duplicate.duplicate is True
