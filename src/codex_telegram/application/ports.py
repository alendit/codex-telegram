"""Application ports."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any, Protocol, TypeAlias

from codex_telegram.application.models import (
    AccountUsage,
    BackendConnection,
    CallbackToken,
    CodexRuntimeState,
    FinalMessageState,
    McpServerCapability,
    PendingReplyTarget,
    ProgressMessageState,
    SkillCatalog,
    StatusCardState,
)
from codex_telegram.domain import (
    AttachmentJob,
    BridgeControlJob,
    BridgeThread,
    CodexThread,
    CodexGoal,
    ConversationAnchor,
    DirectoryEntry,
    LogicalThread,
    PendingApproval,
    PendingUserInput,
    Project,
    ProfileDefinition,
    RealtimeEvent,
    RealtimeSession,
    SessionOverrides,
    ThreadMessage,
    ThreadBindingResult,
    TurnAccepted,
    TurnResult,
    TurnUpdate,
    UserTurnInput,
    WebhookSubscription,
)

TurnUpdateHandler: TypeAlias = Callable[[TurnUpdate], Awaitable[None]]
WaitNoticeHandler: TypeAlias = Callable[[], Awaitable[None]]
TurnStateChangeHandler: TypeAlias = Callable[[], Awaitable[None]]


class CodexBackendError(RuntimeError):
    """Expected failure at a configured Codex backend boundary."""

    def __init__(self, message: str, *, backend_id: str | None = None) -> None:
        super().__init__(message)
        self.backend_id = backend_id


class CodexBackend(Protocol):
    """Stable application-facing backend port."""

    def resolve_backend_id(
        self,
        *,
        backend_id: str | None = None,
        backend_name: str | None = None,
    ) -> str: ...

    async def list_backend_connections(self) -> list[BackendConnection]: ...

    async def async_healthcheck(self) -> dict[str, str]: ...

    async def get_usage(
        self,
        *,
        codex_backend_id: str | None = None,
    ) -> AccountUsage: ...

    async def list_skills(
        self,
        *,
        cwd: str | None = None,
        force_reload: bool = False,
        codex_backend_id: str | None = None,
    ) -> list[SkillCatalog]: ...

    async def list_mcp_servers(
        self,
        *,
        codex_backend_id: str | None = None,
    ) -> list[McpServerCapability]: ...

    async def list_codex_threads(
        self,
        *,
        search: str | None = None,
        limit: int = 50,
        backend_id: str | None = None,
        backend_name: str | None = None,
        include_all: bool = False,
    ) -> list[CodexThread]: ...

    async def get_codex_thread(
        self,
        thread_id: str,
        *,
        backend_id: str | None = None,
        backend_name: str | None = None,
    ) -> CodexThread: ...

    async def ensure_thread_binding(
        self,
        chat_key: str,
        logical_thread_id: str,
        existing_codex_thread_id: str | None,
        profile: ProfileDefinition,
        overrides: SessionOverrides,
        *,
        codex_backend_id: str,
        anchor_id: str | None = None,
    ) -> ThreadBindingResult: ...

    async def start_turn(
        self,
        chat_key: str,
        logical_thread_id: str,
        codex_thread_id: str,
        text: str | UserTurnInput,
        profile: ProfileDefinition,
        overrides: SessionOverrides,
        *,
        codex_backend_id: str,
    ) -> TurnAccepted: ...

    async def steer_turn(
        self,
        *,
        logical_thread_id: str,
        codex_thread_id: str,
        codex_backend_id: str,
        turn_id: str,
        text: str | UserTurnInput,
    ) -> None: ...

    async def interrupt_turn(
        self,
        turn_id: str,
        *,
        codex_thread_id: str | None = None,
        codex_backend_id: str | None = None,
    ) -> None: ...

    async def wait_for_turn_event(
        self, turn_id: str, timeout: float, *, codex_backend_id: str | None = None
    ) -> TurnUpdate | PendingApproval | PendingUserInput | TurnResult: ...

    def get_runtime_state(
        self,
        codex_thread_id: str,
        *,
        codex_backend_id: str | None = None,
    ) -> CodexRuntimeState: ...

    async def get_thread_goal(
        self,
        codex_thread_id: str,
        *,
        codex_backend_id: str | None = None,
    ) -> CodexGoal | None: ...

    async def set_thread_goal(
        self,
        codex_thread_id: str,
        *,
        objective: str,
        token_budget: int | None = None,
        status: str = "active",
        update_token_budget: bool = False,
        codex_backend_id: str | None = None,
    ) -> CodexGoal: ...

    async def clear_thread_goal(
        self,
        codex_thread_id: str,
        *,
        codex_backend_id: str | None = None,
    ) -> None: ...

    async def resolve_server_request(
        self, request_id: int, result: Any, *, codex_backend_id: str | None = None
    ) -> None: ...

    async def start_realtime(
        self,
        chat_key: str,
        logical_thread_id: str,
        codex_thread_id: str,
        *,
        codex_backend_id: str,
    ) -> RealtimeSession: ...

    async def append_realtime_text(
        self,
        codex_thread_id: str,
        text: str,
        *,
        codex_backend_id: str,
    ) -> None: ...

    async def stop_realtime(
        self,
        codex_thread_id: str,
        *,
        codex_backend_id: str,
    ) -> None: ...

    async def wait_for_realtime_event(
        self,
        codex_thread_id: str,
        timeout: float,
        *,
        codex_backend_id: str,
    ) -> RealtimeEvent: ...


class DirectoryResolver(Protocol):
    """Filesystem boundary for directory command path resolution."""

    async def default_base_path(self) -> str: ...

    async def resolve_directory(self, raw_path: str, *, base_path: str) -> str: ...


class StateRepository(Protocol):
    """Durable application state port."""

    async def initialize(self) -> None: ...
    async def mark_waiting_threads_interrupted(self) -> None: ...
    async def ensure_chat(self, chat_key: str) -> None: ...
    async def create_thread(
        self,
        chat_key: str,
        thread_id: str,
        title: str,
        *,
        codex_backend_id: str | None = None,
    ) -> None: ...
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
    async def get_focused_bridge(self, chat_key: str) -> BridgeThread | None: ...
    async def get_bridge(self, bridge_id: str) -> BridgeThread | None: ...
    async def set_focused_bridge(self, chat_key: str, bridge_id: str) -> None: ...
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
    async def update_conversation_anchor_title(
        self, anchor_id: str, title: str
    ) -> None: ...
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
    async def list_conversation_anchors(
        self, chat_key: str
    ) -> list[ConversationAnchor]: ...
    async def get_active_thread(self, chat_key: str) -> LogicalThread | None: ...
    async def list_threads(self, chat_key: str) -> list[LogicalThread]: ...
    async def expire_idle_bridges(
        self, *, now: str, focus_expired_before: str
    ) -> list[str]: ...
    async def list_interrupted_threads(self) -> list[LogicalThread]: ...
    async def get_thread(self, thread_id: str) -> LogicalThread | None: ...
    async def set_active_thread(self, chat_key: str, thread_id: str) -> None: ...
    async def update_codex_thread_binding(
        self, thread_id: str, codex_thread_id: str, *, codex_backend_id: str
    ) -> None: ...
    async def update_thread_title_if_empty(
        self, thread_id: str, title: str
    ) -> None: ...
    async def mark_turn_started(self, thread_id: str, turn_id: str) -> None: ...
    async def mark_turn_completed(self, thread_id: str) -> None: ...
    async def mark_turn_failed(self, thread_id: str) -> None: ...
    async def take_interrupted_notice(self, thread_id: str) -> bool: ...
    async def get_overrides(self, thread_id: str) -> SessionOverrides: ...
    async def upsert_overrides(
        self, thread_id: str, overrides: SessionOverrides
    ) -> SessionOverrides: ...
    async def clear_overrides(self, thread_id: str) -> None: ...
    async def add_thread_message(
        self,
        thread_id: str,
        *,
        role: str,
        kind: str,
        text: str,
        turn_id: str | None = None,
    ) -> None: ...
    async def list_thread_messages(
        self,
        thread_id: str,
        *,
        limit: int = 10,
    ) -> list[ThreadMessage]: ...
    async def list_final_thread_messages(
        self,
        thread_id: str,
        *,
        limit: int = 10,
    ) -> list[ThreadMessage]: ...
    async def list_undelivered_final_thread_messages(
        self,
        *,
        chat_key: str,
        anchor_id: str,
        thread_id: str,
        limit: int = 20,
    ) -> list[ThreadMessage]: ...
    async def get_latest_final_thread_message(
        self,
        thread_id: str,
    ) -> ThreadMessage | None: ...
    async def mark_thread_messages_delivered(
        self,
        *,
        chat_key: str,
        anchor_id: str,
        thread_id: str,
    ) -> None: ...
    async def remember_directory(self, thread_id: str, path: str) -> None: ...
    async def list_directories(
        self,
        thread_id: str,
        *,
        limit: int = 10,
    ) -> list[DirectoryEntry]: ...
    async def enqueue_attachment_job(
        self,
        thread_id: str,
        path: str,
        *,
        caption: str | None = None,
    ) -> AttachmentJob: ...
    async def list_pending_attachment_jobs(
        self,
        *,
        limit: int = 20,
    ) -> list[AttachmentJob]: ...
    async def mark_attachment_job_delivered(self, job_id: int) -> None: ...
    async def mark_attachment_job_failed(self, job_id: int, error: str) -> None: ...
    async def enqueue_bridge_control_job(
        self,
        thread_id: str,
        kind: str,
        payload: dict[str, object],
    ) -> BridgeControlJob: ...
    async def list_pending_bridge_control_jobs(
        self,
        *,
        limit: int = 20,
    ) -> list[BridgeControlJob]: ...
    async def mark_bridge_control_job_delivered(self, job_id: int) -> None: ...
    async def mark_bridge_control_job_failed(self, job_id: int, error: str) -> None: ...
    async def upsert_project(
        self,
        *,
        connection_id: str,
        root_path: str,
        label: str,
    ) -> Project: ...
    async def list_projects(
        self,
        *,
        connection_id: str | None = None,
        limit: int = 50,
    ) -> list[Project]: ...
    async def get_project(self, project_id: str) -> Project | None: ...
    async def bind_thread_project(self, thread_id: str, project_id: str) -> None: ...
    async def get_thread_project(self, thread_id: str) -> Project | None: ...
    async def clear_thread_project(self, thread_id: str) -> None: ...
    async def get_project_overrides(self, project_id: str) -> SessionOverrides: ...
    async def upsert_project_overrides(
        self, project_id: str, overrides: SessionOverrides
    ) -> SessionOverrides: ...
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
        self,
        webhook_id: str,
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
    async def create_callback_token(
        self,
        *,
        chat_key: str,
        topic_id: int | None,
        action: str,
        payload: dict[str, object],
        expires_at: str,
    ) -> str: ...
    async def consume_callback_token(
        self,
        token: str,
        *,
        chat_key: str,
        topic_id: int | None,
    ) -> CallbackToken | None: ...
    async def add_pending_request(self, request: PendingApproval) -> None: ...
    async def get_pending_request(self, chat_key: str) -> PendingApproval | None: ...
    async def pop_pending_request(self, request_id: int) -> PendingApproval | None: ...
    async def clear_pending_for_thread(self, thread_id: str) -> None: ...
    async def add_pending_user_input(self, request: PendingUserInput) -> None: ...
    async def get_pending_user_input(
        self, chat_key: str
    ) -> PendingUserInput | None: ...
    async def update_pending_user_input_selection(
        self,
        request_id: int,
        *,
        question_id: str,
        answers: tuple[str, ...],
        awaiting_free_text: bool,
    ) -> None: ...
    async def pop_pending_user_input(
        self, request_id: int
    ) -> PendingUserInput | None: ...
    async def clear_pending_user_input_for_thread(self, thread_id: str) -> None: ...


class ProgressMessageStore(Protocol):
    """Telegram-shell progress state port."""

    async def initialize(self) -> None: ...
    async def get_progress(self, thread_id: str) -> ProgressMessageState | None: ...
    async def save_progress(
        self,
        thread_id: str,
        *,
        message_id: int | None = None,
        rendered_text: str | None = None,
    ) -> None: ...
    async def clear_progress(self, thread_id: str) -> None: ...
    async def save_final_message(
        self,
        thread_id: str,
        *,
        chat_key: str,
        message_id: int,
        rendered_text: str,
    ) -> None: ...
    async def get_final_message(
        self,
        thread_id: str,
    ) -> FinalMessageState | None: ...
    async def get_final_message_by_reply(
        self,
        chat_key: str,
        message_id: int,
    ) -> FinalMessageState | None: ...
    async def save_pending_reply_target(
        self,
        *,
        chat_key: str,
        prompt_message_id: int,
        target_thread_id: str,
        expires_at: str,
    ) -> None: ...
    async def consume_pending_reply_target(
        self,
        *,
        chat_key: str,
        prompt_message_id: int,
    ) -> PendingReplyTarget | None: ...
    async def get_status_card(self, chat_key: str) -> StatusCardState | None: ...
    async def save_status_card(
        self,
        chat_key: str,
        *,
        chat_id: int,
        topic_id: int | None,
        message_id: int,
        rendered_text: str,
    ) -> None: ...
