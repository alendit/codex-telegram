import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from codex_telegram.domain import (
    AttachmentJob,
    BridgeSnapshot,
    WebhookEventDispatch,
    WebhookSubscription,
    WebhookSubscriptionCreated,
)
from codex_telegram.entrypoints.webhook import build_webhook_app


def _subscription(*, enabled: bool = True) -> WebhookSubscription:
    return WebhookSubscription(
        webhook_id="wh_123",
        chat_key="chat:1",
        anchor_id="anchor-1",
        codex_backend_id="laptop",
        codex_thread_id="codex-1",
        latest_bridge_id="bridge-1",
        name="front-door",
        enabled=enabled,
        created_at="2026-05-02T00:00:00+00:00",
        updated_at="2026-05-02T00:00:00+00:00",
        trigger_count=0,
        last_triggered_at=None,
    )


def _app(
    service: AsyncMock,
    trigger_event: AsyncMock,
    *,
    bridge_command: AsyncMock | None = None,
    bridge_control: AsyncMock | None = None,
    attachment_roots: tuple[Path, ...] = (),
) -> web.Application:
    return build_webhook_app(
        admin_token="admin-secret",
        service=service,
        trigger_event=trigger_event,
        bridge_command=bridge_command,
        bridge_control=bridge_control,
        public_base_url="https://codex.example",
        local_base_url="http://127.0.0.1:8080",
        attachment_roots=attachment_roots,
    )


@asynccontextmanager
async def _test_client(app: web.Application) -> AsyncIterator[TestClient]:
    server = TestServer(app)
    client = TestClient(server)
    await client.start_server()
    try:
        yield client
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_webhook_management_requires_admin_bearer_and_trigger_is_removed() -> (
    None
):
    service = AsyncMock()
    callback = AsyncMock()
    app = _app(service, callback)

    async with _test_client(app) as client:
        unauthorized = await client.get("/webhooks")
        removed_trigger = await client.post(
            "/trigger",
            headers={"Authorization": "Bearer admin-secret"},
            json={"chat_key": "chat:1", "prompt": "legacy"},
        )

    assert unauthorized.status == 401
    assert removed_trigger.status == 404
    callback.assert_not_called()


@pytest.mark.asyncio
async def test_webhook_create_list_and_revoke_use_admin_api() -> None:
    subscription = _subscription()
    service = AsyncMock()
    service.create_webhook_subscription.return_value = WebhookSubscriptionCreated(
        subscription=subscription,
        event_secret="event-secret",
    )
    service.list_webhook_subscriptions.return_value = [subscription]
    service.revoke_webhook_subscription.return_value = True
    callback = AsyncMock()
    app = _app(service, callback)

    async with _test_client(app) as client:
        created = await client.post(
            "/webhooks",
            headers={"Authorization": "Bearer admin-secret"},
            json={
                "chat_key": "chat:1",
                "anchor_id": "anchor-1",
                "name": "front-door",
            },
        )
        listed = await client.get(
            "/webhooks?chat_key=chat:1",
            headers={"Authorization": "Bearer admin-secret"},
        )
        revoked = await client.delete(
            "/webhooks/wh_123",
            headers={"Authorization": "Bearer admin-secret"},
        )
        created_body = await created.json()
        listed_body = await listed.json()
        revoked_body = await revoked.json()

    assert created.status == 201
    assert created_body["id"] == "wh_123"
    assert created_body["event_secret"] == "event-secret"
    assert created_body["event_url"] == "https://codex.example/events/wh_123"
    assert "curl" in created_body["example_curl"]
    service.create_webhook_subscription.assert_awaited_once_with(
        chat_key="chat:1",
        anchor_id="anchor-1",
        codex_backend_id=None,
        codex_thread_id=None,
        name="front-door",
    )

    assert listed.status == 200
    assert listed_body["webhooks"][0]["name"] == "front-door"
    assert "event_secret" not in listed_body["webhooks"][0]
    service.list_webhook_subscriptions.assert_awaited_once_with(
        chat_key="chat:1",
        anchor_id=None,
        include_disabled=False,
    )

    assert revoked.status == 200
    assert revoked_body == {"revoked": True}
    service.revoke_webhook_subscription.assert_awaited_once_with(
        "wh_123",
        chat_key=None,
    )


@pytest.mark.asyncio
async def test_attachment_enqueue_uses_admin_api_and_validates_path(
    tmp_path: Path,
) -> None:
    attachment_root = tmp_path / "attachments"
    attachment_root.mkdir()
    allowed = attachment_root / "report.txt"
    allowed.write_text("hello", encoding="utf-8")
    blocked = tmp_path / "outside.txt"
    blocked.write_text("nope", encoding="utf-8")
    service = AsyncMock()
    service.enqueue_attachment_job.return_value = AttachmentJob(
        job_id=7,
        chat_key="chat:1",
        logical_thread_id="thread-1",
        path=str(allowed),
        caption="report",
        status="pending",
        created_at="2026-05-07T00:00:00+00:00",
        updated_at="2026-05-07T00:00:00+00:00",
    )
    callback = AsyncMock()
    app = _app(service, callback, attachment_roots=(attachment_root,))

    async with _test_client(app) as client:
        created = await client.post(
            "/attachments",
            headers={"Authorization": "Bearer admin-secret"},
            json={
                "thread_id": "thread-1",
                "path": str(allowed),
                "caption": "report",
            },
        )
        rejected = await client.post(
            "/attachments",
            headers={"Authorization": "Bearer admin-secret"},
            json={
                "thread_id": "thread-1",
                "path": str(blocked),
            },
        )
        unauthorized = await client.post(
            "/attachments",
            json={"thread_id": "thread-1", "path": str(allowed)},
        )
        created_body = await created.json()

    assert created.status == 201
    assert created_body["job_id"] == 7
    assert created_body["status"] == "pending"
    service.enqueue_attachment_job.assert_awaited_once_with(
        "thread-1",
        str(allowed),
        caption="report",
    )
    assert rejected.status == 400
    assert unauthorized.status == 401


@pytest.mark.asyncio
async def test_bridge_status_and_command_use_admin_api() -> None:
    service = AsyncMock()
    service.bridge_snapshot.return_value = BridgeSnapshot(
        logical_thread_id="bridge-1",
        chat_key="chat:1",
        title="Deploy fix",
        anchor_id="anchor-1",
        codex_backend_id="laptop",
        codex_thread_id="codex-1",
        active=True,
        awaiting_reply=False,
        pending_turn_id=None,
        expires_at=None,
        closed_at=None,
    )
    callback = AsyncMock()
    bridge_command = AsyncMock(return_value={"accepted": True, "messages": ["status"]})
    app = _app(service, callback, bridge_command=bridge_command)

    async with _test_client(app) as client:
        status = await client.get(
            "/bridges/bridge-1",
            headers={"Authorization": "Bearer admin-secret"},
        )
        command = await client.post(
            "/bridge-command",
            headers={"Authorization": "Bearer admin-secret"},
            json={"thread_id": "bridge-1", "text": "/status"},
        )
        status_body = await status.json()
        command_body = await command.json()

    assert status.status == 200
    assert status_body["logical_thread_id"] == "bridge-1"
    assert status_body["anchor_id"] == "anchor-1"
    assert status_body["codex_thread_id"] == "codex-1"
    service.bridge_snapshot.assert_awaited_once_with("bridge-1")
    assert command.status == 202
    assert command_body == {"accepted": True, "messages": ["status"]}
    bridge_command.assert_awaited_once_with("bridge-1", "/status")


@pytest.mark.asyncio
async def test_bridge_control_queues_notify_and_refresh() -> None:
    service = AsyncMock()
    callback = AsyncMock()
    bridge_control = AsyncMock(return_value={"accepted": True, "job_id": 12})
    app = _app(service, callback, bridge_control=bridge_control)

    async with _test_client(app) as client:
        notify = await client.post(
            "/bridge-control",
            headers={"Authorization": "Bearer admin-secret"},
            json={
                "thread_id": "bridge-1",
                "action": "notify",
                "text": "runtime note",
                "level": "warning",
            },
        )
        refresh = await client.post(
            "/bridge-control",
            headers={"Authorization": "Bearer admin-secret"},
            json={"thread_id": "bridge-1", "action": "refresh_status_card"},
        )
        unauthorized = await client.post(
            "/bridge-control",
            json={"thread_id": "bridge-1", "action": "refresh_status_card"},
        )
        notify_body = await notify.json()
        refresh_body = await refresh.json()

    assert notify.status == 202
    assert notify_body["job_id"] == 12
    assert refresh.status == 202
    assert refresh_body["job_id"] == 12
    assert unauthorized.status == 401
    bridge_control.assert_any_await(
        "bridge-1",
        "notify",
        {"text": "runtime note", "level": "warning"},
    )
    bridge_control.assert_any_await("bridge-1", "refresh_status_card", {})


@pytest.mark.asyncio
async def test_webhook_event_requires_per_webhook_secret_and_schedules_delivery() -> (
    None
):
    subscription = _subscription()
    service = AsyncMock()
    service.accept_webhook_event.return_value = WebhookEventDispatch(
        subscription=subscription,
        prompt="External event from front-door\nHuman input: door opened",
        duplicate=False,
    )
    callback = AsyncMock()
    app = _app(service, callback)

    async with _test_client(app) as client:
        response = await client.post(
            "/events/wh_123",
            headers={
                "Authorization": "Bearer event-secret",
                "Idempotency-Key": "event-1",
            },
            json={
                "input": "door opened",
                "metadata": {"source": "sensor"},
                "payload": {"state": "open"},
            },
        )

        assert response.status == 202
        await asyncio.sleep(0)

    service.accept_webhook_event.assert_awaited_once_with(
        "wh_123",
        "event-secret",
        {
            "input": "door opened",
            "metadata": {"source": "sensor"},
            "payload": {"state": "open"},
        },
        idempotency_key="event-1",
    )
    callback.assert_awaited_once_with(
        subscription, service.accept_webhook_event.return_value.prompt
    )


@pytest.mark.asyncio
async def test_webhook_event_rejects_missing_useful_input() -> None:
    service = AsyncMock()
    callback = AsyncMock()
    app = _app(service, callback)

    async with _test_client(app) as client:
        response = await client.post(
            "/events/wh_123",
            headers={"Authorization": "Bearer event-secret"},
            json={},
        )

    assert response.status == 400
    service.accept_webhook_event.assert_not_called()
    callback.assert_not_called()


@pytest.mark.asyncio
async def test_webhook_event_deduplicates_idempotency_keys() -> None:
    subscription = _subscription()
    service = AsyncMock()
    service.accept_webhook_event.return_value = WebhookEventDispatch(
        subscription=subscription,
        prompt="already handled",
        duplicate=True,
    )
    callback = AsyncMock()
    app = _app(service, callback)

    async with _test_client(app) as client:
        response = await client.post(
            "/events/wh_123",
            headers={
                "Authorization": "Bearer event-secret",
                "Idempotency-Key": "event-1",
            },
            json={"input": "door opened"},
        )
        body = await response.json()

    assert response.status == 202
    assert body == {"accepted": True, "duplicate": True}
    callback.assert_not_called()
