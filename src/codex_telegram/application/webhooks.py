"""Webhook subscription and event application service."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
import hashlib
import hmac
import json
import secrets
from typing import Protocol

from codex_telegram.application.ports import CodexBackend
from codex_telegram.domain import (
    BridgeThread,
    ConversationAnchor,
    WebhookEventDispatch,
    WebhookSubscription,
    WebhookSubscriptionCreated,
)

FocusedBridgeProvider = Callable[[str], Awaitable[BridgeThread]]


class WebhookRepository(Protocol):
    """State needed by webhook subscription and event policy."""

    async def get_conversation_anchor(
        self, anchor_id: str
    ) -> ConversationAnchor | None: ...
    async def get_conversation_anchor_for_backend_thread(
        self,
        *,
        chat_key: str,
        codex_backend_id: str,
        codex_thread_id: str,
    ) -> ConversationAnchor | None: ...
    async def upsert_conversation_anchor(
        self,
        *,
        chat_key: str,
        codex_backend_id: str,
        codex_thread_id: str,
        title: str,
        alias: str | None = None,
        project_id: str | None = None,
        latest_bridge_id: str | None = None,
    ) -> ConversationAnchor: ...
    async def create_webhook_subscription(
        self,
        *,
        webhook_id: str,
        chat_key: str,
        anchor_id: str | None = None,
        name: str,
        secret_hash: str,
    ) -> WebhookSubscription: ...
    async def list_webhook_subscriptions(
        self,
        *,
        chat_key: str | None = None,
        anchor_id: str | None = None,
        include_disabled: bool = False,
    ) -> list[WebhookSubscription]: ...
    async def get_webhook_subscription(
        self, webhook_id: str
    ) -> WebhookSubscription | None: ...
    async def get_webhook_secret_hash(self, webhook_id: str) -> str | None: ...
    async def disable_webhook_subscription(self, webhook_id: str) -> bool: ...
    async def record_webhook_delivery(
        self,
        webhook_id: str,
        *,
        idempotency_key: str | None,
    ) -> bool: ...
    async def mark_webhook_triggered(self, webhook_id: str) -> None: ...


class WebhookService:
    """Own webhook subscription and event acceptance policy."""

    def __init__(
        self,
        repository: WebhookRepository,
        client: CodexBackend,
        focused_bridge_provider: FocusedBridgeProvider,
    ) -> None:
        self._repository = repository
        self._client = client
        self._focused_bridge_provider = focused_bridge_provider

    async def create_webhook_subscription(
        self,
        *,
        chat_key: str,
        anchor_id: str | None = None,
        codex_backend_id: str | None = None,
        codex_thread_id: str | None = None,
        name: str,
    ) -> WebhookSubscriptionCreated:
        """Create a durable webhook subscription for one conversation anchor."""
        normalized_name = _normalize_webhook_name(name)
        anchor: ConversationAnchor | None = None
        if anchor_id:
            anchor = await self._repository.get_conversation_anchor(anchor_id.strip())
        elif codex_thread_id:
            backend = self._client.resolve_backend_id(
                backend_id=codex_backend_id.strip() if codex_backend_id else None
            )
            anchor = await self._repository.get_conversation_anchor_for_backend_thread(
                chat_key=chat_key.strip(),
                codex_backend_id=backend,
                codex_thread_id=codex_thread_id.strip(),
            )
            if anchor is None:
                anchor = await self._repository.upsert_conversation_anchor(
                    chat_key=chat_key.strip(),
                    codex_backend_id=backend,
                    codex_thread_id=codex_thread_id.strip(),
                    title=codex_thread_id.strip(),
                )
        else:
            bridge = await self._focused_bridge_provider(chat_key)
            if bridge.anchor_id:
                anchor = await self._repository.get_conversation_anchor(
                    bridge.anchor_id
                )
        if anchor is None or anchor.chat_key != chat_key.strip():
            raise ValueError("Webhook requires an anchored Codex conversation.")
        secret = secrets.token_urlsafe(32)
        subscription = await self._repository.create_webhook_subscription(
            webhook_id="wh_" + secrets.token_hex(8),
            chat_key=anchor.chat_key,
            anchor_id=anchor.anchor_id,
            name=normalized_name,
            secret_hash=_secret_hash(secret),
        )
        return WebhookSubscriptionCreated(
            subscription=subscription,
            event_secret=secret,
        )

    async def list_webhook_subscriptions(
        self,
        *,
        chat_key: str | None = None,
        anchor_id: str | None = None,
        include_disabled: bool = False,
    ) -> list[WebhookSubscription]:
        """List durable webhook subscriptions."""
        return await self._repository.list_webhook_subscriptions(
            chat_key=chat_key.strip() if chat_key else None,
            anchor_id=anchor_id.strip() if anchor_id else None,
            include_disabled=include_disabled,
        )

    async def revoke_webhook_subscription(
        self,
        selector: str,
        *,
        chat_key: str | None = None,
    ) -> bool:
        """Disable one webhook by id, or by name when scoped to a chat."""
        value = selector.strip()
        if not value:
            raise ValueError("Webhook id or name is required.")
        if value.startswith("wh_"):
            return await self._repository.disable_webhook_subscription(value)
        if chat_key is None:
            return False
        subscriptions = await self._repository.list_webhook_subscriptions(
            chat_key=chat_key,
            include_disabled=False,
        )
        matches = [item for item in subscriptions if item.name == value]
        if len(matches) != 1:
            return False
        return await self._repository.disable_webhook_subscription(
            matches[0].webhook_id
        )

    async def accept_webhook_event(
        self,
        webhook_id: str,
        event_secret: str,
        payload: Mapping[str, object],
        *,
        idempotency_key: str | None = None,
    ) -> WebhookEventDispatch:
        """Authenticate, deduplicate, and normalize one external event."""
        subscription = await self._repository.get_webhook_subscription(webhook_id)
        if subscription is None:
            raise LookupError("Unknown webhook.")
        if not subscription.enabled:
            raise PermissionError("Webhook is disabled.")
        stored_hash = await self._repository.get_webhook_secret_hash(webhook_id)
        if stored_hash is None or not _secret_matches(event_secret, stored_hash):
            raise PermissionError("Invalid webhook secret.")
        prompt = _webhook_event_prompt(subscription, payload)
        is_new = await self._repository.record_webhook_delivery(
            webhook_id,
            idempotency_key=idempotency_key.strip() if idempotency_key else None,
        )
        if not is_new:
            return WebhookEventDispatch(
                subscription=subscription,
                prompt=prompt,
                duplicate=True,
            )
        await self._repository.mark_webhook_triggered(webhook_id)
        updated = await self._repository.get_webhook_subscription(webhook_id)
        return WebhookEventDispatch(
            subscription=updated or subscription,
            prompt=prompt,
            duplicate=False,
        )


def _normalize_webhook_name(name: str) -> str:
    value = " ".join(name.strip().split())
    if not value:
        raise ValueError("Webhook name is required.")
    if len(value) > 80:
        raise ValueError("Webhook name is too long.")
    return value


def _secret_hash(secret: str) -> str:
    return hashlib.sha256(secret.encode("utf-8")).hexdigest()


def _secret_matches(secret: str, stored_hash: str) -> bool:
    return hmac.compare_digest(_secret_hash(secret), stored_hash)


def _webhook_event_prompt(
    subscription: WebhookSubscription,
    payload: Mapping[str, object],
) -> str:
    if not payload:
        raise ValueError("Webhook event payload must not be empty.")
    human_input = _webhook_human_input(payload)
    metadata = payload.get("metadata")
    metadata_text = (
        _json_block(metadata)
        if isinstance(metadata, Mapping) and len(metadata) > 0
        else None
    )
    source = _webhook_source(payload)

    lines = [
        "External event received by codex-telegram.",
        f"Webhook: {subscription.name}",
        f"Webhook id: {subscription.webhook_id}",
        f"Bound chat_key: {subscription.chat_key}",
        f"Bound anchor_id: {subscription.anchor_id or '(missing)'}",
        f"Bound codex_thread_id: {subscription.codex_thread_id or '(missing)'}",
    ]
    if source:
        lines.append(f"Source: {source}")
    if human_input:
        lines.extend(["", "Human input:", human_input])
    if metadata_text:
        lines.extend(["", "Metadata:", metadata_text])
    lines.extend(["", "JSON payload:", _json_block(payload)])
    return "\n".join(lines)


def _webhook_human_input(payload: Mapping[str, object]) -> str | None:
    for key in ("input", "prompt", "text", "message", "human_input"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _webhook_source(payload: Mapping[str, object]) -> str | None:
    for key in ("source", "event", "name"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    metadata = payload.get("metadata")
    if isinstance(metadata, Mapping):
        source = metadata.get("source")
        if isinstance(source, str) and source.strip():
            return source.strip()
    return None


def _json_block(value: object) -> str:
    return json.dumps(value, indent=2, sort_keys=True, default=str)
