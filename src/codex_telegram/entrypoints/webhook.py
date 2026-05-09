"""Durable webhook management and event endpoints."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping
import hmac
from pathlib import Path
from typing import Any, cast

from aiohttp import web

from codex_telegram.adapters.telegram.attachments import (
    validate_external_attachment_path,
)
from codex_telegram.application.service import BotService
from codex_telegram.domain import (
    AttachmentJob,
    BridgeControlJob,
    BridgeSnapshot,
    WebhookSubscription,
    WebhookSubscriptionCreated,
)

WebhookEventCallback = Callable[[WebhookSubscription, str], Awaitable[None]]
BridgeCommandCallback = Callable[[str, str], Awaitable[Mapping[str, object]]]
BridgeControlCallback = Callable[
    [str, str, dict[str, object]], Awaitable[Mapping[str, object]]
]
TRIGGER_EVENT_KEY = web.AppKey("trigger_event", object)


def build_webhook_app(
    *,
    admin_token: str,
    service: BotService,
    trigger_event: WebhookEventCallback,
    public_base_url: str | None,
    local_base_url: str,
    bridge_command: BridgeCommandCallback | None = None,
    bridge_control: BridgeControlCallback | None = None,
    attachment_roots: tuple[Path, ...] = (),
) -> web.Application:
    """Build the authenticated durable webhook application."""
    app = web.Application()
    app[TRIGGER_EVENT_KEY] = _fire_and_forget(trigger_event)

    async def handle_create(request: web.Request) -> web.Response:
        _require_admin(request, admin_token)
        payload = await _read_json_object(request)
        chat_key = _required_string(payload, "chat_key")
        anchor_id = _optional_payload_string(payload, "anchor_id")
        codex_backend_id = _optional_payload_string(payload, "codex_backend_id")
        codex_thread_id = _optional_payload_string(payload, "codex_thread_id")
        if anchor_id is None and codex_thread_id is None:
            raise web.HTTPBadRequest(text="anchor_id or codex_thread_id is required")
        name = _required_string(payload, "name")
        created = await service.create_webhook_subscription(
            chat_key=chat_key,
            anchor_id=anchor_id,
            codex_backend_id=codex_backend_id,
            codex_thread_id=codex_thread_id,
            name=name,
        )
        base_url = _effective_base_url(public_base_url, local_base_url)
        return web.json_response(
            _created_subscription_response(created, base_url),
            status=201,
        )

    async def handle_list(request: web.Request) -> web.Response:
        _require_admin(request, admin_token)
        include_disabled = request.query.get("include_disabled", "").lower() in {
            "1",
            "true",
            "yes",
        }
        subscriptions = await service.list_webhook_subscriptions(
            chat_key=_optional_query_string(request, "chat_key"),
            anchor_id=_optional_query_string(request, "anchor_id"),
            include_disabled=include_disabled,
        )
        return web.json_response(
            {
                "webhooks": [
                    _subscription_response(subscription)
                    for subscription in subscriptions
                ]
            }
        )

    async def handle_revoke(request: web.Request) -> web.Response:
        _require_admin(request, admin_token)
        revoked = await service.revoke_webhook_subscription(
            request.match_info["webhook_id"],
            chat_key=_optional_query_string(request, "chat_key"),
        )
        if not revoked:
            raise web.HTTPNotFound(text="Webhook not found")
        return web.json_response({"revoked": True})

    async def handle_event(request: web.Request) -> web.Response:
        payload = await _read_json_object(request)
        if not payload:
            raise web.HTTPBadRequest(text="Webhook event payload must not be empty")
        event_secret = _bearer_token(request)
        if not event_secret:
            raise web.HTTPUnauthorized(text="Unauthorized")
        try:
            dispatch = await service.accept_webhook_event(
                request.match_info["webhook_id"],
                event_secret,
                payload,
                idempotency_key=_optional_header_string(request, "Idempotency-Key"),
            )
        except LookupError as exc:
            raise web.HTTPNotFound(text=str(exc)) from exc
        except PermissionError as exc:
            raise web.HTTPUnauthorized(text=str(exc)) from exc
        except ValueError as exc:
            raise web.HTTPBadRequest(text=str(exc)) from exc
        if dispatch.duplicate:
            return web.json_response({"accepted": True, "duplicate": True}, status=202)
        trigger_event = cast(
            Callable[[WebhookSubscription, str], None],
            request.app[TRIGGER_EVENT_KEY],
        )
        trigger_event(dispatch.subscription, dispatch.prompt)
        return web.json_response({"accepted": True, "duplicate": False}, status=202)

    async def handle_attachment(request: web.Request) -> web.Response:
        _require_admin(request, admin_token)
        payload = await _read_json_object(request)
        thread_id = _required_string(payload, "thread_id")
        path = _required_string(payload, "path")
        caption = _optional_payload_string(payload, "caption")
        try:
            resolved_path = validate_external_attachment_path(
                path,
                allowed_roots=attachment_roots,
            )
            job = await service.enqueue_attachment_job(
                thread_id,
                str(resolved_path),
                caption=caption,
            )
        except ValueError as exc:
            raise web.HTTPBadRequest(text=str(exc)) from exc
        return web.json_response(_attachment_job_response(job), status=201)

    async def handle_bridge_status(request: web.Request) -> web.Response:
        _require_admin(request, admin_token)
        try:
            snapshot = await service.bridge_snapshot(request.match_info["thread_id"])
        except ValueError as exc:
            raise web.HTTPNotFound(text=str(exc)) from exc
        return web.json_response(_bridge_snapshot_response(snapshot))

    async def handle_bridge_command(request: web.Request) -> web.Response:
        _require_admin(request, admin_token)
        if bridge_command is None:
            raise web.HTTPServiceUnavailable(text="Bridge command handler unavailable")
        payload = await _read_json_object(request)
        thread_id = _required_string(payload, "thread_id")
        text = _required_string(payload, "text")
        try:
            result = await bridge_command(thread_id, text)
        except ValueError as exc:
            raise web.HTTPBadRequest(text=str(exc)) from exc
        return web.json_response(result, status=202)

    async def handle_bridge_control(request: web.Request) -> web.Response:
        _require_admin(request, admin_token)
        payload = await _read_json_object(request)
        thread_id = _required_string(payload, "thread_id")
        action = _required_string(payload, "action")
        if action == "refresh":
            action = "refresh_status_card"
        control_payload = _bridge_control_payload(action, payload)
        try:
            if bridge_control is not None:
                result = await bridge_control(thread_id, action, control_payload)
            else:
                job = await service.enqueue_bridge_control_job(
                    thread_id,
                    action,
                    control_payload,
                )
                result = _bridge_control_job_response(job)
        except ValueError as exc:
            raise web.HTTPBadRequest(text=str(exc)) from exc
        return web.json_response(result, status=202)

    app.router.add_post("/webhooks", handle_create)
    app.router.add_get("/webhooks", handle_list)
    app.router.add_delete("/webhooks/{webhook_id}", handle_revoke)
    app.router.add_post("/events/{webhook_id}", handle_event)
    app.router.add_post("/attachments", handle_attachment)
    app.router.add_get("/bridges/{thread_id}", handle_bridge_status)
    app.router.add_post("/bridge-command", handle_bridge_command)
    app.router.add_post("/bridge-control", handle_bridge_control)
    return app


def _fire_and_forget(
    callback: WebhookEventCallback,
) -> Callable[[WebhookSubscription, str], None]:
    def trigger(subscription: WebhookSubscription, prompt: str) -> None:
        asyncio.ensure_future(callback(subscription, prompt))

    return trigger


async def _read_json_object(request: web.Request) -> dict[str, Any]:
    try:
        payload = await request.json()
    except Exception as exc:
        raise web.HTTPBadRequest(text="Request body must be a JSON object") from exc
    if not isinstance(payload, dict):
        raise web.HTTPBadRequest(text="Request body must be a JSON object")
    return payload


def _require_admin(request: web.Request, admin_token: str) -> None:
    token = _bearer_token(request)
    if token is None or not hmac.compare_digest(token, admin_token):
        raise web.HTTPUnauthorized(text="Unauthorized")


def _bearer_token(request: web.Request) -> str | None:
    auth_header = request.headers.get("Authorization", "")
    prefix = "Bearer "
    if not auth_header.startswith(prefix):
        return None
    token = auth_header[len(prefix) :].strip()
    return token or None


def _required_string(payload: Mapping[str, object], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value.strip():
        raise web.HTTPBadRequest(text=f"{key} is required")
    return value.strip()


def _optional_payload_string(payload: Mapping[str, object], key: str) -> str | None:
    value = payload.get(key)
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise web.HTTPBadRequest(text=f"{key} must be a non-empty string")
    return value.strip()


def _optional_query_string(request: web.Request, key: str) -> str | None:
    value = request.query.get(key)
    if value is None or not value.strip():
        return None
    return value.strip()


def _optional_header_string(request: web.Request, key: str) -> str | None:
    value = request.headers.get(key)
    if value is None or not value.strip():
        return None
    return value.strip()


def _effective_base_url(public_base_url: str | None, local_base_url: str) -> str:
    return (public_base_url or local_base_url).rstrip("/")


def _created_subscription_response(
    created: WebhookSubscriptionCreated,
    base_url: str,
) -> dict[str, object]:
    subscription = created.subscription
    event_url = f"{base_url}/events/{subscription.webhook_id}"
    return {
        **_subscription_response(subscription),
        "event_url": event_url,
        "event_secret": created.event_secret,
        "example_curl": (
            "curl -X POST "
            f"{event_url} "
            f"-H 'Authorization: Bearer {created.event_secret}' "
            "-H 'Content-Type: application/json' "
            '-d \'{"input":"event text","payload":{}}\''
        ),
    }


def _subscription_response(subscription: WebhookSubscription) -> dict[str, object]:
    return {
        "id": subscription.webhook_id,
        "name": subscription.name,
        "chat_key": subscription.chat_key,
        "anchor_id": subscription.anchor_id,
        "codex_backend_id": subscription.codex_backend_id,
        "codex_thread_id": subscription.codex_thread_id,
        "latest_bridge_id": subscription.latest_bridge_id,
        "enabled": subscription.enabled,
        "created_at": subscription.created_at,
        "updated_at": subscription.updated_at,
        "trigger_count": subscription.trigger_count,
        "last_triggered_at": subscription.last_triggered_at,
    }


def _attachment_job_response(job: AttachmentJob) -> dict[str, object]:
    return {
        "job_id": job.job_id,
        "thread_id": job.logical_thread_id,
        "chat_key": job.chat_key,
        "path": job.path,
        "caption": job.caption,
        "status": job.status,
        "created_at": job.created_at,
        "updated_at": job.updated_at,
        "error": job.error,
    }


def _bridge_snapshot_response(snapshot: BridgeSnapshot) -> dict[str, object]:
    return {
        "logical_thread_id": snapshot.logical_thread_id,
        "thread_id": snapshot.logical_thread_id,
        "chat_key": snapshot.chat_key,
        "title": snapshot.title,
        "anchor_id": snapshot.anchor_id,
        "codex_backend_id": snapshot.codex_backend_id,
        "codex_thread_id": snapshot.codex_thread_id,
        "active": snapshot.active,
        "awaiting_reply": snapshot.awaiting_reply,
        "pending_turn_id": snapshot.pending_turn_id,
        "expires_at": snapshot.expires_at,
        "closed_at": snapshot.closed_at,
    }


def _bridge_control_payload(
    action: str,
    payload: Mapping[str, object],
) -> dict[str, object]:
    if action == "notify":
        text = _required_string(payload, "text")
        level = _optional_payload_string(payload, "level") or "info"
        if level not in {"info", "warning"}:
            raise web.HTTPBadRequest(text="level must be info or warning")
        return {"text": text, "level": level}
    if action == "refresh_status_card":
        return {}
    raise web.HTTPBadRequest(text=f"Unsupported bridge control action: {action}")


def _bridge_control_job_response(job: BridgeControlJob) -> dict[str, object]:
    return {
        "accepted": True,
        "job_id": job.job_id,
        "status": job.status,
        "thread_id": job.logical_thread_id,
    }
