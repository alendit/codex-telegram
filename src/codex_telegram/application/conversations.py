"""Conversation anchor and bridge-window lifecycle service."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
import secrets
from typing import Protocol

from codex_telegram.application.models import ConversationAttachment
from codex_telegram.application.ports import CodexBackend
from codex_telegram.application.settings import RuntimeSettingsService
from codex_telegram.domain import (
    BridgeThread,
    CodexThread,
    ConversationAnchor,
    LogicalThread,
    Project,
    ThreadMessage,
)


@dataclass(frozen=True, slots=True)
class DefaultProjectConfig:
    """Configured Project used by the New -> Default shortcut."""

    connection: str
    root_path: str
    label: str | None = None


@dataclass(frozen=True, slots=True)
class ConversationLifecycleConfig:
    """Conversation lifecycle policy."""

    focus_timeout_seconds: float
    active_waiting_ttl_seconds: float
    default_project: DefaultProjectConfig | None = None


@dataclass(frozen=True, slots=True)
class ThreadSelectionResult:
    success: bool
    message: str
    thread: LogicalThread | None = None


class ConversationRepository(Protocol):
    """State needed by anchor and bridge-window lifecycle policy."""

    async def ensure_chat(self, chat_key: str) -> None: ...
    async def get_focused_bridge(self, chat_key: str) -> BridgeThread | None: ...
    async def get_active_thread(self, chat_key: str) -> LogicalThread | None: ...
    async def create_bridge(
        self,
        *,
        chat_key: str,
        bridge_id: str,
        title: str,
        anchor_id: str | None,
        codex_backend_id: str | None = None,
        expires_at: str | None = None,
        focus: bool = True,
    ) -> BridgeThread: ...
    async def get_bridge(self, bridge_id: str) -> BridgeThread | None: ...
    async def set_focused_bridge(self, chat_key: str, bridge_id: str) -> None: ...
    async def get_thread(self, thread_id: str) -> LogicalThread | None: ...
    async def list_threads(self, chat_key: str) -> list[LogicalThread]: ...
    async def list_interrupted_threads(self) -> list[LogicalThread]: ...
    async def expire_idle_bridges(
        self, *, now: str, focus_expired_before: str
    ) -> list[str]: ...
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
    async def get_conversation_anchor(
        self, anchor_id: str
    ) -> ConversationAnchor | None: ...
    async def list_conversation_anchors(
        self, chat_key: str
    ) -> list[ConversationAnchor]: ...
    async def update_conversation_anchor_title(
        self, anchor_id: str, title: str
    ) -> None: ...
    async def list_thread_messages(
        self,
        thread_id: str,
        *,
        limit: int = 10,
    ) -> list[ThreadMessage]: ...
    async def update_thread_title_if_empty(
        self, thread_id: str, title: str
    ) -> None: ...
    async def get_project(self, project_id: str) -> Project | None: ...
    async def upsert_project(
        self,
        *,
        connection_id: str,
        root_path: str,
        label: str,
    ) -> Project: ...
    async def bind_thread_project(self, thread_id: str, project_id: str) -> None: ...


class ConversationLifecycleService:
    """Own durable anchors and short-lived bridge-window lifecycle policy."""

    def __init__(
        self,
        config: ConversationLifecycleConfig,
        repository: ConversationRepository,
        client: CodexBackend,
        settings: RuntimeSettingsService,
    ) -> None:
        self._config = config
        self._repository = repository
        self._client = client
        self._settings = settings

    async def ensure_active_thread(self, chat_key: str) -> LogicalThread:
        """Return the focused bridge using the thread-facing read model."""
        return _logical_from_bridge(await self.ensure_focused_bridge(chat_key))

    async def ensure_focused_bridge(self, chat_key: str) -> BridgeThread:
        """Return the focused bridge, creating or reviving one if needed."""
        await self._repository.ensure_chat(chat_key)
        bridge = await self._repository.get_focused_bridge(chat_key)
        if bridge is None:
            return _bridge_from_logical(await self._new_default_thread(chat_key))
        if self.bridge_focus_is_expired(bridge):
            await self.expire_idle_bridges()
            focused = await self._repository.get_focused_bridge(chat_key)
            if focused is None:
                return _bridge_from_logical(await self._new_default_thread(chat_key))
            bridge = focused
        if self.bridge_is_expired(bridge) and bridge.anchor_id is not None:
            return await self.revive_anchor(
                chat_key,
                bridge.anchor_id,
                focus=True,
            )
        if self.bridge_is_expired(bridge):
            return _bridge_from_logical(await self.new_thread(chat_key))
        return bridge

    async def new_thread(
        self,
        chat_key: str,
        *,
        connection_name: str | None = None,
        project_selector: str | None = None,
    ) -> LogicalThread:
        """Create and focus a new unanchored bridge window."""
        project = await self._settings.resolve_new_thread_project(
            chat_key,
            connection_name=connection_name,
            project_selector=project_selector,
        )
        connection_id = self._settings.resolve_connection_id(connection_name)
        codex_backend_id = (
            project.connection_id if project is not None else connection_id
        )
        thread = await self._create_thread(
            chat_key,
            "New conversation",
            codex_backend_id=codex_backend_id,
        )
        if project is not None:
            await self._repository.bind_thread_project(
                thread.thread_id, project.project_id
            )
            await self._settings.restore_project_runtime_overrides(
                thread.thread_id,
                project.project_id,
            )
            updated = await self._repository.get_thread(thread.thread_id)
            assert updated is not None
            return updated
        return thread

    async def new_thread_in_project(
        self,
        chat_key: str,
        project_id: str,
    ) -> LogicalThread:
        """Create and focus a new bridge window bound to a specific Project."""
        project = await self._repository.get_project(project_id.strip())
        if project is None:
            raise ValueError("Unknown project.")
        self._settings.validate_project_access(chat_key, project)
        thread = await self._create_thread(
            chat_key,
            "New conversation",
            codex_backend_id=project.connection_id,
        )
        await self._repository.bind_thread_project(thread.thread_id, project.project_id)
        await self._settings.restore_project_runtime_overrides(
            thread.thread_id,
            project.project_id,
        )
        updated = await self._repository.get_thread(thread.thread_id)
        assert updated is not None
        return updated

    def has_default_project(self) -> bool:
        """Return whether a New -> Default Project is configured."""
        return self._config.default_project is not None

    async def new_thread_in_default_project(self, chat_key: str) -> LogicalThread:
        """Create and focus a new bridge window bound to the configured default Project."""
        configured = self._config.default_project
        if configured is None:
            raise ValueError("No default project configured.")
        connection_id = self._settings.resolve_connection_id(configured.connection)
        project = await self._repository.upsert_project(
            connection_id=connection_id or configured.connection,
            root_path=configured.root_path,
            label=configured.label or project_label(configured.root_path),
        )
        return await self.new_thread_in_project(chat_key, project.project_id)

    async def attach_codex_thread(
        self,
        chat_key: str,
        codex_thread_id: str,
        *,
        backend_id: str | None = None,
        backend_name: str | None = None,
        focus: bool = True,
    ) -> ConversationAttachment:
        """Attach or revive this chat's anchor for an existing Codex thread."""
        resolved_thread_id = codex_thread_id.strip()
        codex_thread = await self._client.get_codex_thread(
            resolved_thread_id,
            backend_id=backend_id,
            backend_name=backend_name,
        )
        title = codex_thread_title(codex_thread)
        project: Project | None = None
        if codex_thread.cwd:
            self._settings.validate_project_access(
                chat_key,
                Project(
                    project_id="",
                    connection_id=codex_thread.codex_backend_id,
                    root_path=codex_thread.cwd,
                    label=project_label(codex_thread.cwd),
                    created_at="",
                    updated_at="",
                ),
            )
            project = await self._repository.upsert_project(
                connection_id=codex_thread.codex_backend_id,
                root_path=codex_thread.cwd,
                label=project_label(codex_thread.cwd),
            )
        anchor = await self._repository.upsert_conversation_anchor(
            chat_key=chat_key,
            codex_backend_id=codex_thread.codex_backend_id,
            codex_thread_id=codex_thread.thread_id,
            title=title,
        )
        bridge = await self.create_bridge_for_anchor(
            chat_key,
            anchor,
            title=title,
            focus=focus,
        )
        if project is not None:
            await self._repository.bind_thread_project(
                bridge.bridge_id,
                project.project_id,
            )
        anchor = await self._repository.upsert_conversation_anchor(
            chat_key=chat_key,
            codex_backend_id=codex_thread.codex_backend_id,
            codex_thread_id=codex_thread.thread_id,
            title=title,
            project_id=project.project_id if project else None,
            latest_bridge_id=bridge.bridge_id,
        )
        return ConversationAttachment(
            anchor=anchor,
            bridge=bridge,
            codex_thread=codex_thread,
        )

    async def list_threads(self, chat_key: str) -> list[LogicalThread]:
        """Return all bridge windows for the chat."""
        await self._repository.ensure_chat(chat_key)
        return await self._repository.list_threads(chat_key)

    async def list_conversations(self, chat_key: str) -> list[ConversationAnchor]:
        """Return durable conversation anchors for the chat."""
        await self._repository.ensure_chat(chat_key)
        anchors = await self._repository.list_conversation_anchors(chat_key)
        return await self._backfill_placeholder_anchor_titles(anchors)

    async def list_interrupted_threads(self) -> list[LogicalThread]:
        """Return threads that were interrupted by a process restart."""
        return await self._repository.list_interrupted_threads()

    async def expire_idle_bridges(self) -> list[str]:
        """Clear stale focus and close expired presentation windows."""
        now = datetime.now(UTC)
        focus_expired_before = (
            now - timedelta(seconds=self._config.focus_timeout_seconds)
        ).isoformat()
        return await self._repository.expire_idle_bridges(
            now=now.isoformat(),
            focus_expired_before=focus_expired_before,
        )

    async def focus_bridge(self, chat_key: str, selector: str) -> ThreadSelectionResult:
        """Focus a bridge id or revive an anchor id."""
        return await self.resolve_bridge(chat_key, selector, focus=True)

    async def resolve_bridge(
        self, chat_key: str, selector: str, *, focus: bool
    ) -> ThreadSelectionResult:
        """Resolve a bridge or anchor selector, optionally focusing it."""
        value = selector.strip()
        bridge = await self._repository.get_bridge(value)
        if bridge is not None and bridge.chat_key == chat_key:
            if self.bridge_is_expired(bridge) and bridge.anchor_id is not None:
                bridge = await self.revive_anchor(
                    chat_key,
                    bridge.anchor_id,
                    focus=focus,
                )
            elif focus:
                await self._repository.set_focused_bridge(chat_key, bridge.bridge_id)
            return ThreadSelectionResult(
                success=True,
                message=f"Focused conversation {_bridge_display_name(bridge)}.",
                thread=_logical_from_bridge(bridge),
            )
        anchor = await self._repository.get_conversation_anchor(value)
        if anchor is not None and anchor.chat_key == chat_key:
            bridge = (
                await self._repository.get_bridge(anchor.latest_bridge_id)
                if anchor.latest_bridge_id is not None
                else None
            )
            if bridge is None or self.bridge_is_expired(bridge):
                bridge = await self.revive_anchor(
                    chat_key,
                    anchor.anchor_id,
                    focus=focus,
                )
            elif focus:
                await self._repository.set_focused_bridge(chat_key, bridge.bridge_id)
            label = anchor.alias or anchor.title
            return ThreadSelectionResult(
                success=True,
                message=f"Focused conversation {_display_name(label)}.",
                thread=_logical_from_bridge(bridge),
            )
        return ThreadSelectionResult(
            success=False,
            message=(
                "Unknown conversation. Use /overview for current status or "
                "/threads to attach threads."
            ),
        )

    async def turn_thread(
        self,
        chat_key: str,
        thread_id: str | None,
    ) -> LogicalThread:
        """Resolve the target bridge window for a turn without changing focus."""
        if thread_id is None:
            return await self.ensure_active_thread(chat_key)
        bridge = await self._repository.get_bridge(thread_id.strip())
        if bridge is not None and bridge.chat_key == chat_key:
            if self.bridge_is_expired(bridge) and bridge.anchor_id is not None:
                return _logical_from_bridge(
                    await self.revive_anchor(chat_key, bridge.anchor_id, focus=False)
                )
            return _logical_from_bridge(bridge)
        thread = await self._repository.get_thread(thread_id.strip())
        if thread is None or thread.chat_key != chat_key:
            raise ValueError(
                "Unknown conversation. Use /overview for current status or "
                "/threads to attach threads."
            )
        if self.bridge_is_expired(thread) and thread.anchor_id is not None:
            return _logical_from_bridge(
                await self.revive_anchor(chat_key, thread.anchor_id, focus=False)
            )
        return thread

    async def revive_anchor(
        self,
        chat_key: str,
        anchor_id: str,
        *,
        focus: bool,
    ) -> BridgeThread:
        """Create a fresh bridge window for an existing conversation anchor."""
        anchor = await self._repository.get_conversation_anchor(anchor_id)
        if anchor is None or anchor.chat_key != chat_key:
            raise ValueError("Unknown conversation anchor.")
        return await self.create_bridge_for_anchor(
            chat_key,
            anchor,
            title=anchor.title,
            focus=focus,
        )

    def bridge_is_expired(self, bridge: BridgeThread | LogicalThread) -> bool:
        """Return whether a bridge window should be revived before use."""
        if bridge.awaiting_reply or bridge.pending_turn_id is not None:
            return False
        expires_at = bridge.expires_at
        if not expires_at:
            updated_at = datetime.fromisoformat(bridge.updated_at)
            return (
                datetime.now(UTC) - updated_at
            ).total_seconds() >= self._config.active_waiting_ttl_seconds
        return datetime.fromisoformat(expires_at) <= datetime.now(UTC)

    def bridge_focus_is_expired(self, bridge: BridgeThread | LogicalThread) -> bool:
        """Return whether an idle focused bridge should lose chat focus."""
        if bridge.awaiting_reply or bridge.pending_turn_id is not None:
            return False
        updated_at = datetime.fromisoformat(bridge.updated_at)
        return (
            datetime.now(UTC) - updated_at
        ).total_seconds() >= self._config.focus_timeout_seconds

    async def create_bridge_for_anchor(
        self,
        chat_key: str,
        anchor: ConversationAnchor,
        *,
        title: str | None = None,
        focus: bool,
    ) -> BridgeThread:
        """Create a bridge window over an existing conversation anchor."""
        bridge_id = secrets.token_hex(4)
        return await self._repository.create_bridge(
            chat_key=chat_key,
            bridge_id=bridge_id,
            title=title or anchor.title,
            anchor_id=anchor.anchor_id,
            codex_backend_id=anchor.codex_backend_id,
            expires_at=self._active_waiting_expires_at(),
            focus=focus,
        )

    async def _create_thread(
        self,
        chat_key: str,
        title: str,
        *,
        codex_backend_id: str | None = None,
    ) -> LogicalThread:
        """Create and focus a bridge window with the supplied title."""
        thread_id = secrets.token_hex(4)
        await self._repository.create_bridge(
            chat_key=chat_key,
            bridge_id=thread_id,
            title=title,
            anchor_id=None,
            codex_backend_id=codex_backend_id,
            expires_at=self._active_waiting_expires_at(),
            focus=True,
        )
        thread = await self._repository.get_thread(thread_id)
        assert thread is not None
        return thread

    async def _backfill_placeholder_anchor_titles(
        self, anchors: list[ConversationAnchor]
    ) -> list[ConversationAnchor]:
        """Use local bridge transcript prompts to repair generic anchor titles."""
        updated: list[ConversationAnchor] = []
        for anchor in anchors:
            title = await self._anchor_backfill_title(anchor)
            if title is None:
                updated.append(anchor)
                continue
            await self._repository.update_conversation_anchor_title(
                anchor.anchor_id, title
            )
            if anchor.latest_bridge_id:
                await self._repository.update_thread_title_if_empty(
                    anchor.latest_bridge_id, title
                )
            updated.append(replace(anchor, title=title))
        return updated

    async def _anchor_backfill_title(self, anchor: ConversationAnchor) -> str | None:
        if not _is_placeholder_title(anchor.title) or anchor.latest_bridge_id is None:
            return None
        messages = await self._repository.list_thread_messages(
            anchor.latest_bridge_id,
            limit=20,
        )
        for message in messages:
            if (
                message.role == "user"
                and message.kind == "prompt"
                and message.text.strip()
            ):
                return short_text(message.text)
        try:
            codex_thread = await self._client.get_codex_thread(
                anchor.codex_thread_id,
                backend_id=anchor.codex_backend_id,
            )
        except (LookupError, RuntimeError, TimeoutError, ValueError):
            return None
        if not isinstance(codex_thread, CodexThread):
            return None
        title = codex_thread_title(codex_thread)
        return None if _is_placeholder_title(title) else title

    async def _new_default_thread(self, chat_key: str) -> LogicalThread:
        if self.has_default_project():
            return await self.new_thread_in_default_project(chat_key)
        return await self.new_thread(chat_key)

    def _active_waiting_expires_at(self) -> str:
        return (
            datetime.now(UTC)
            + timedelta(seconds=self._config.active_waiting_ttl_seconds)
        ).isoformat()


def logical_from_bridge(bridge: BridgeThread) -> LogicalThread:
    """Convert a bridge window into the thread-facing read model."""
    return _logical_from_bridge(bridge)


def _logical_from_bridge(bridge: BridgeThread) -> LogicalThread:
    return LogicalThread(
        thread_id=bridge.bridge_id,
        chat_key=bridge.chat_key,
        title=bridge.title,
        codex_thread_id=bridge.codex_thread_id,
        created_at=bridge.created_at,
        updated_at=bridge.updated_at,
        turn_count=bridge.turn_count,
        awaiting_reply=bridge.awaiting_reply,
        interrupted_notice=bridge.interrupted_notice,
        pending_turn_id=bridge.pending_turn_id,
        codex_backend_id=bridge.codex_backend_id,
        anchor_id=bridge.anchor_id,
        expires_at=bridge.expires_at,
        closed_at=bridge.closed_at,
    )


def _bridge_from_logical(thread: LogicalThread) -> BridgeThread:
    return BridgeThread(
        bridge_id=thread.thread_id,
        chat_key=thread.chat_key,
        anchor_id=thread.anchor_id,
        title=thread.title,
        codex_thread_id=thread.codex_thread_id,
        created_at=thread.created_at,
        updated_at=thread.updated_at,
        turn_count=thread.turn_count,
        awaiting_reply=thread.awaiting_reply,
        interrupted_notice=thread.interrupted_notice,
        pending_turn_id=thread.pending_turn_id,
        codex_backend_id=thread.codex_backend_id,
        expires_at=thread.expires_at,
        closed_at=thread.closed_at,
    )


def codex_thread_title(thread: CodexThread) -> str:
    return short_text(thread.title or thread.preview or thread.thread_id)


def project_label(root_path: str) -> str:
    return Path(root_path).name or root_path


def short_text(text: str, limit: int = 48) -> str:
    value = " ".join(text.split())
    if len(value) <= limit:
        return value or "New conversation"
    return value[: limit - 3] + "..."


def _display_name(value: str | None) -> str:
    return short_text(value or "", limit=80)


def _bridge_display_name(bridge: BridgeThread) -> str:
    return _display_name(bridge.title)


def _is_placeholder_title(title: str) -> bool:
    return title in {"New thread", "New conversation"}
