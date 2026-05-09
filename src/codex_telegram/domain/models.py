"""Stable domain models."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Any


@dataclass(frozen=True, slots=True)
class ProfileDefinition:
    """One resolved profile definition."""

    name: str
    model: str
    model_provider: str
    approval_policy: str
    sandbox_type: str
    network_access: bool
    writable_roots: tuple[str, ...] = ()
    effort: str = "medium"
    fast_mode: bool = False
    summary: str = "concise"
    verbosity: str = "verbose"
    command_verbosity: str = "errors"
    followup_mode: str = "steer"
    developer_instructions: str | None = None


@dataclass(frozen=True, slots=True)
class SessionOverrides:
    """Session-scoped runtime overrides."""

    profile: str | None = None
    model: str | None = None
    effort: str | None = None
    summary: str | None = None
    cwd: str | None = None
    fast_mode: bool | None = None
    verbosity: str | None = None
    command_verbosity: str | None = None
    followup_mode: str | None = None
    collaboration_mode: str | None = None

    def as_dict(self) -> dict[str, Any]:
        """Serialize only non-empty overrides."""
        data: dict[str, Any] = {}
        if self.profile is not None:
            data["profile"] = self.profile
        if self.model is not None:
            data["model"] = self.model
        if self.effort is not None:
            data["effort"] = self.effort
        if self.summary is not None:
            data["summary"] = self.summary
        if self.cwd is not None:
            data["cwd"] = self.cwd
        if self.fast_mode is not None:
            data["fast_mode"] = self.fast_mode
        if self.verbosity is not None:
            data["verbosity"] = self.verbosity
        if self.command_verbosity is not None:
            data["command_verbosity"] = self.command_verbosity
        if self.followup_mode is not None:
            data["followup_mode"] = self.followup_mode
        if self.collaboration_mode is not None:
            data["collaboration_mode"] = self.collaboration_mode
        return data

    def with_value(self, field_name: str, value: Any) -> SessionOverrides:
        """Return a copy with one override changed."""
        return replace(self, **{field_name: value})


@dataclass(frozen=True, slots=True)
class UserTurnImage:
    """One image reference included in a user turn."""

    url: str


@dataclass(frozen=True, slots=True)
class UserTurnSkill:
    """One Codex skill attachment included in a user turn."""

    name: str
    path: str


@dataclass(frozen=True, slots=True)
class UserTurnInput:
    """Stable user-turn content passed from client adapters to the backend."""

    text: str = ""
    images: tuple[UserTurnImage, ...] = ()
    skills: tuple[UserTurnSkill, ...] = ()

    def display_text(self) -> str:
        """Return a human-readable prompt summary for history and thread titles."""
        image_count = len(self.images)
        skill_count = len(self.skills)
        attachment_note = (
            ""
            if image_count == 0
            else f"[Attached {image_count} image{'s' if image_count != 1 else ''}]"
        )
        skill_note = (
            ""
            if skill_count == 0
            else "[Skill: " + ", ".join(skill.name for skill in self.skills) + "]"
        )
        notes = [note for note in (attachment_note, skill_note) if note]
        if self.text and notes:
            return self.text + "\n" + "\n".join(notes)
        if self.text:
            return self.text
        if notes:
            return "\n".join(notes)
        return ""


@dataclass(frozen=True, slots=True)
class ConversationAnchor:
    """Durable Telegram-chat binding to one Codex backend thread."""

    anchor_id: str
    chat_key: str
    codex_backend_id: str
    codex_thread_id: str
    title: str
    alias: str | None
    project_id: str | None
    latest_bridge_id: str | None
    created_at: str
    updated_at: str
    broken_reason: str | None = None
    archived: bool = False
    latest_bridge_pending_turn_id: str | None = None
    latest_bridge_awaiting_reply: bool = False
    latest_bridge_expires_at: str | None = None
    latest_bridge_closed_at: str | None = None
    latest_bridge_pending_approval: bool = False
    latest_bridge_pending_user_input: bool = False


@dataclass(frozen=True, slots=True)
class BridgeThread:
    """Short-lived Telegram presentation window over a conversation anchor."""

    bridge_id: str
    chat_key: str
    title: str
    anchor_id: str | None
    codex_thread_id: str | None
    created_at: str
    updated_at: str
    turn_count: int
    awaiting_reply: bool
    interrupted_notice: bool
    pending_turn_id: str | None
    codex_backend_id: str = "primary"
    expires_at: str | None = None
    closed_at: str | None = None

    @property
    def thread_id(self) -> str:
        """Compatibility identifier used by backend event fields."""
        return self.bridge_id


@dataclass(frozen=True, slots=True)
class LogicalThread:
    """Telegram-facing bridge window state."""

    thread_id: str
    chat_key: str
    title: str
    codex_thread_id: str | None
    created_at: str
    updated_at: str
    turn_count: int
    awaiting_reply: bool
    interrupted_notice: bool
    pending_turn_id: str | None
    codex_backend_id: str = "primary"
    anchor_id: str | None = None
    expires_at: str | None = None
    closed_at: str | None = None

    @property
    def bridge_id(self) -> str:
        """Return the presentation-window identifier."""
        return self.thread_id


@dataclass(frozen=True, slots=True)
class RealtimeSession:
    """One ephemeral app-server realtime session bound to a bridge window."""

    chat_key: str
    logical_thread_id: str
    codex_thread_id: str
    codex_backend_id: str
    session_id: str | None = None
    status: str = "active"
    last_text: str = ""


@dataclass(frozen=True, slots=True)
class RealtimeEvent:
    """One app-server realtime notification translated for the application."""

    event_type: str
    chat_key: str
    logical_thread_id: str
    codex_thread_id: str
    codex_backend_id: str
    session_id: str | None = None
    role: str | None = None
    text: str = ""
    reason: str | None = None


@dataclass(frozen=True, slots=True)
class CodexThread:
    """One Codex backend thread returned by app-server."""

    thread_id: str
    cwd: str | None
    title: str | None
    preview: str | None
    status: str
    created_at: int
    updated_at: int
    model_provider: str | None
    anchor_status: str = "unlinked"
    codex_backend_id: str = "primary"
    codex_backend_name: str = "primary"


@dataclass(frozen=True, slots=True)
class ThreadMessage:
    """One compact Telegram-facing transcript entry."""

    message_id: int | None
    thread_id: str
    role: str
    kind: str
    text: str
    created_at: str
    turn_id: str | None = None


@dataclass(frozen=True, slots=True)
class DirectoryEntry:
    """One remembered working directory selection."""

    entry_id: int | None
    thread_id: str
    path: str
    selected_at: str


@dataclass(frozen=True, slots=True)
class AttachmentJob:
    """One queued Telegram attachment delivery request."""

    job_id: int | None
    chat_key: str
    logical_thread_id: str
    path: str
    caption: str | None
    status: str
    created_at: str
    updated_at: str
    error: str | None = None


@dataclass(frozen=True, slots=True)
class BridgeControlJob:
    """One queued Telegram bridge-control delivery request."""

    job_id: int | None
    chat_key: str
    logical_thread_id: str
    kind: str
    payload: dict[str, Any]
    status: str
    created_at: str
    updated_at: str
    error: str | None = None


@dataclass(frozen=True, slots=True)
class BridgeSnapshot:
    """Runtime-facing status for one Telegram bridge window."""

    logical_thread_id: str
    chat_key: str
    title: str
    anchor_id: str | None
    codex_backend_id: str
    codex_thread_id: str | None
    active: bool
    awaiting_reply: bool
    pending_turn_id: str | None
    expires_at: str | None
    closed_at: str | None


@dataclass(frozen=True, slots=True)
class Project:
    """One connection-scoped Codex project root."""

    project_id: str
    connection_id: str
    root_path: str
    label: str
    created_at: str
    updated_at: str


@dataclass(frozen=True, slots=True)
class WebhookSubscription:
    """One durable external-event subscription bound to a conversation anchor."""

    webhook_id: str
    chat_key: str
    anchor_id: str | None
    codex_backend_id: str | None
    codex_thread_id: str | None
    latest_bridge_id: str | None
    name: str
    enabled: bool
    created_at: str
    updated_at: str
    trigger_count: int
    last_triggered_at: str | None


@dataclass(frozen=True, slots=True)
class WebhookSubscriptionCreated:
    """New webhook subscription plus the raw event secret shown once."""

    subscription: WebhookSubscription
    event_secret: str


@dataclass(frozen=True, slots=True)
class WebhookEventDispatch:
    """Normalized webhook event ready for Telegram/Codex delivery."""

    subscription: WebhookSubscription
    prompt: str
    duplicate: bool = False


@dataclass(frozen=True, slots=True)
class PendingApproval:
    """One approval request awaiting user input."""

    request_id: int
    chat_key: str
    logical_thread_id: str
    codex_thread_id: str
    turn_id: str | None
    method: str
    command: str | None
    reason: str | None
    message: str | None = None
    raw_params: dict[str, Any] = field(default_factory=dict)
    codex_backend_id: str = "primary"


@dataclass(frozen=True, slots=True)
class UserInputOption:
    """One selectable answer option for a user-input question."""

    label: str
    description: str = ""


@dataclass(frozen=True, slots=True)
class UserInputQuestion:
    """One backend-requested user-input question."""

    question_id: str
    question: str
    header: str | None = None
    options: tuple[UserInputOption, ...] = ()


@dataclass(frozen=True, slots=True)
class PendingUserInput:
    """One backend request waiting for user-provided choices or text."""

    request_id: int
    chat_key: str
    logical_thread_id: str
    codex_thread_id: str
    turn_id: str | None
    method: str
    questions: tuple[UserInputQuestion, ...]
    selected_answers: dict[str, tuple[str, ...]] = field(default_factory=dict)
    awaiting_free_text_question_id: str | None = None
    raw_params: dict[str, Any] = field(default_factory=dict)
    codex_backend_id: str = "primary"


@dataclass(frozen=True, slots=True)
class CodexGoal:
    """Last known Codex goal for one runtime thread."""

    objective: str
    status: str = "active"
    token_budget: int | None = None
    tokens_used: int | None = None
    elapsed_seconds: float | None = None
    created_at: str | None = None
    updated_at: str | None = None


@dataclass(frozen=True, slots=True)
class ThreadBindingResult:
    """Result of ensuring a Codex thread binding."""

    codex_thread_id: str
    remapped: bool
    codex_backend_id: str = "primary"


@dataclass(frozen=True, slots=True)
class TurnAccepted:
    """Accepted turn metadata."""

    turn_id: str
    chat_key: str
    logical_thread_id: str
    codex_thread_id: str
    remapped: bool
    codex_backend_id: str = "primary"


@dataclass(frozen=True, slots=True)
class TurnUpdate:
    """One live turn update."""

    turn_id: str
    chat_key: str
    logical_thread_id: str
    codex_thread_id: str
    status: str
    source: str
    text: str
    visible: bool = True
    codex_backend_id: str = "primary"
    goal: CodexGoal | None = None


@dataclass(frozen=True, slots=True)
class TurnResultImage:
    """One image emitted as part of a completed assistant turn."""

    source: str
    caption: str | None = None


@dataclass(frozen=True, slots=True)
class TurnResult:
    """One completed turn result."""

    turn_id: str
    chat_key: str
    logical_thread_id: str
    codex_thread_id: str
    status: str
    final_text: str
    error: str | None = None
    token_usage: dict[str, int] | None = None
    images: tuple[TurnResultImage, ...] = ()
    codex_backend_id: str = "primary"
