"""Application-facing read models."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from codex_telegram.domain import (
    BridgeThread,
    CodexGoal,
    CodexThread,
    ConversationAnchor,
    DirectoryEntry,
    LogicalThread,
    PendingApproval,
    Project,
    RealtimeSession,
    SessionOverrides,
    ThreadMessage,
)


@dataclass(frozen=True, slots=True)
class EffectiveSettings:
    """Resolved runtime settings for one bridge window."""

    profile: str
    model: str
    model_provider: str
    effort: str
    summary: str
    cwd: str
    fast_mode: bool
    verbosity: str
    command_verbosity: str
    followup_mode: str
    overrides: dict[str, Any]
    collaboration_mode: str = "default"


@dataclass(frozen=True, slots=True)
class BackendConnection:
    """One configured Codex backend connection."""

    connection_id: str
    label: str


@dataclass(frozen=True, slots=True)
class SkillCapability:
    """One Codex runtime skill exposed by app-server."""

    name: str
    path: str
    scope: str
    description: str
    enabled: bool
    short_description: str | None = None
    display_name: str | None = None


@dataclass(frozen=True, slots=True)
class SkillCatalog:
    """Skills discovered for one runtime cwd."""

    cwd: str
    skills: tuple[SkillCapability, ...]
    errors: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class McpToolCapability:
    """One tool exposed by an MCP server."""

    name: str
    title: str | None = None
    description: str | None = None


@dataclass(frozen=True, slots=True)
class McpResourceCapability:
    """One resource or resource template exposed by an MCP server."""

    name: str
    uri: str
    title: str | None = None
    description: str | None = None


@dataclass(frozen=True, slots=True)
class McpServerCapability:
    """User-visible MCP server inventory."""

    name: str
    auth_status: str
    tools: tuple[McpToolCapability, ...] = ()
    resources: tuple[McpResourceCapability, ...] = ()
    resource_templates: tuple[McpResourceCapability, ...] = ()


@dataclass(frozen=True, slots=True)
class ProgressMessageState:
    """Telegram-shell progress rendering state."""

    message_id: int | None
    rendered_text: str | None


@dataclass(frozen=True, slots=True)
class FinalMessageState:
    """Telegram-shell final response message mapping."""

    thread_id: str
    chat_key: str
    message_id: int
    rendered_text: str


@dataclass(frozen=True, slots=True)
class PendingReplyTarget:
    """One Telegram ForceReply prompt mapped to a bridge window."""

    chat_key: str
    prompt_message_id: int
    target_thread_id: str


@dataclass(frozen=True, slots=True)
class FocusFinalMessage:
    """One assistant final to deliver when focusing a conversation."""

    message: ThreadMessage
    repeated: bool = False


@dataclass(frozen=True, slots=True)
class StatusCardState:
    """Telegram-shell sticky status card mapping."""

    chat_key: str
    chat_id: int
    topic_id: int | None
    message_id: int
    rendered_text: str
    updated_at: str


@dataclass(frozen=True, slots=True)
class CodexPlanItem:
    """One current Codex plan row."""

    step: str
    status: str


@dataclass(frozen=True, slots=True)
class AccountUsageLimit:
    """One account-level usage or quota row."""

    label: str
    used: int | None = None
    limit: int | None = None
    remaining: int | None = None
    resets_at: str | None = None
    used_percent: float | None = None
    window_minutes: int | None = None


@dataclass(frozen=True, slots=True)
class AccountUsage:
    """Account-level usage data returned by a Codex backend."""

    status: str
    limits: tuple[AccountUsageLimit, ...] = ()
    reason: str | None = None


@dataclass(frozen=True, slots=True)
class RuntimeUsageMetrics:
    """Runtime metrics for one Codex thread when the backend emits them."""

    total_token_usage: dict[str, int] | None = None
    last_token_usage: dict[str, int] | None = None
    model_context_window: int | None = None


@dataclass(frozen=True, slots=True)
class CodexRuntimeState:
    """Last known goal and planning state from the Codex runtime."""

    goal: CodexGoal | None = None
    plan_items: tuple[CodexPlanItem, ...] = ()
    token_usage: dict[str, int] | None = None
    usage_metrics: RuntimeUsageMetrics | None = None

    @property
    def is_empty(self) -> bool:
        """Return whether there is anything user-visible to render."""
        return self.goal is None and not self.plan_items


@dataclass(frozen=True, slots=True)
class CurrentThreadState:
    """Current active-thread status for Telegram rendering."""

    thread: LogicalThread
    settings: EffectiveSettings
    pending: PendingApproval | None
    realtime: RealtimeSession | None = None
    runtime: CodexRuntimeState = CodexRuntimeState()


@dataclass(frozen=True, slots=True)
class UsageState:
    """Focused conversation usage state for Telegram rendering."""

    conversation_name: str
    backend_id: str
    codex_thread_attached: bool
    token_usage: dict[str, int] | None
    account: AccountUsage
    runtime_metrics: RuntimeUsageMetrics | None = None


@dataclass(frozen=True, slots=True)
class RealtimeStartResult:
    """Result of starting realtime mode for one bridge window."""

    session: RealtimeSession
    remap_warning: str | None = None


@dataclass(frozen=True, slots=True)
class CodexThreadGroup:
    """Codex threads grouped by project/cwd for Telegram listings."""

    project: str
    threads: list[CodexThread]
    backend_id: str = "primary"
    backend_name: str = "primary"


@dataclass(frozen=True, slots=True)
class CodexThreadBackendFailure:
    """One unavailable backend encountered while listing Codex threads."""

    backend_id: str
    backend_name: str
    error: str


@dataclass(frozen=True, slots=True)
class CodexThreadListResult:
    """Codex backend thread listing plus any backend availability warnings."""

    groups: list[CodexThreadGroup]
    failures: list[CodexThreadBackendFailure]


@dataclass(frozen=True, slots=True)
class ConversationAttachment:
    """Result of attaching a Telegram conversation anchor to a Codex thread."""

    anchor: ConversationAnchor
    bridge: BridgeThread
    codex_thread: CodexThread


@dataclass(frozen=True, slots=True)
class CallbackToken:
    """One short-lived Telegram callback action."""

    token: str
    chat_key: str
    topic_id: int | None
    action: str
    payload: dict[str, object]
    expires_at: str


@dataclass(frozen=True, slots=True)
class ThreadHistory:
    """Recent compact transcript entries for one thread."""

    thread: LogicalThread
    entries: list[ThreadMessage]


@dataclass(frozen=True, slots=True)
class DirectoryState:
    """Current effective working directory plus recent selections."""

    thread: LogicalThread
    current_path: str
    history: list[DirectoryEntry]


@dataclass(frozen=True, slots=True)
class ProjectState:
    """Current thread project binding plus connection-scoped project catalog."""

    thread: LogicalThread
    active: Project | None
    catalog: list[Project]
    project_overrides: SessionOverrides = SessionOverrides()
