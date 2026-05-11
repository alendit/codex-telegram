"""Telegram text rendering helpers."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from html import escape, unescape
import re

from aiogram.types import BotCommand

from codex_telegram.application.models import (
    AccountUsageLimit,
    CodexRuntimeState,
    CodexThreadBackendFailure,
    ConversationAttachment,
    CodexThreadGroup,
    CurrentThreadState,
    DirectoryState,
    EffectiveSettings,
    McpServerCapability,
    ProjectState,
    StatusCardState,
    SkillCatalog,
    ThreadHistory,
    UsageState,
)
from codex_telegram.domain import (
    CodexGoal,
    CodexThread,
    ConversationAnchor,
    LogicalThread,
    PendingApproval,
    PendingUserInput,
    Project,
    RealtimeSession,
    ThreadMessage,
    WebhookSubscription,
)

COMMAND_SUCCESS_PREFIX = "🟢 "
COMMAND_FAILURE_PREFIX = "❌ "
WARNING_PREFIX = "⚠️ "
TELEGRAM_MESSAGE_TEXT_LIMIT = 4096
TELEGRAM_TRUNCATION_NOTICE = "\n\n... truncated to fit Telegram."
CODEX_THREADS_DEFAULT_LIMIT = 3
STATUS_CARD_ROTATION_SECONDS = 3600.0
SENTENCE_BOUNDARY_RE = re.compile(r"[.!?](?=(?:\s|$))")
HTML_TAG_RE = re.compile(r"<[^>]+>")
UTC_TIMESTAMP_FORMAT = "%Y-%m-%d %H:%M:%S UTC"
WRAPPED_CONVERSATION_NOTICE = (
    "This conversations is wrapped up due to inactivity. "
    "Reply to this message to resume the conversation."
)

TELEGRAM_COMMAND_SPECS: tuple[tuple[str, str], ...] = (
    ("new", "Start a new conversation"),
    ("threads", "List backend threads by project"),
    ("goal", "Show or update the focused Codex goal"),
    ("plan", "Switch the focused conversation to Plan mode"),
    ("implement", "Switch to Default mode and implement the plan"),
    ("attach_thread", "Attach this chat to a Codex thread"),
    ("focus", "Focus a conversation shortcut"),
    ("to", "Send one message to a conversation"),
    ("current", "Show focused conversation status and effective settings"),
    ("history", "Show recent user and assistant history"),
    ("overview", "Show the conversation overview"),
    ("help", "Show supported bot commands"),
    ("usage", "Show usage and account-limit visibility"),
    ("interrupt", "Interrupt the currently running turn"),
    ("resetparams", "Clear all runtime overrides for this conversation"),
    ("profile", "Get or set the active profile"),
    ("model", "Get or set the active model"),
    ("effort", "Get or set the reasoning effort"),
    ("summary", "Get or set the summary mode"),
    ("dir", "Show or change the working directory"),
    ("project", "Show the active project and config"),
    ("skills", "List available Codex skills"),
    ("skill", "Invoke a Codex skill by name"),
    ("mcp", "List available MCP servers and capabilities"),
    ("webhooks", "List external event webhooks for this chat"),
    ("webhook", "Create or revoke an external event webhook"),
    ("realtime", "Start or stop realtime mode"),
    ("verbosity", "Get or set assistant update verbosity"),
    ("command_verbosity", "Get or set command update verbosity"),
    ("followup_mode", "Get or set follow-up behavior"),
    ("fast", "Toggle fast mode on or off"),
)


@dataclass(frozen=True, slots=True)
class CodexThreadListing:
    """Telegram-safe rendered Codex thread listing and matching button scope."""

    text: str
    groups: list[CodexThreadGroup]


def _telegram_delivery_text(
    text: str,
    *,
    parse_mode: str | None = None,
) -> tuple[str, str | None]:
    if len(text) <= TELEGRAM_MESSAGE_TEXT_LIMIT:
        return text, parse_mode
    budget = TELEGRAM_MESSAGE_TEXT_LIMIT - len(TELEGRAM_TRUNCATION_NOTICE)
    if budget <= 0:
        return text[:TELEGRAM_MESSAGE_TEXT_LIMIT], None
    return text[:budget].rstrip() + TELEGRAM_TRUNCATION_NOTICE, None


def _coalesce_progress_text(text: str, previous_text: str | None) -> str | None:
    """Render only completed lines or completed sentences from progress text."""
    normalized = text.strip()
    if not normalized:
        return None
    last_boundary = normalized.rfind("\n")
    for match in SENTENCE_BOUNDARY_RE.finditer(normalized):
        last_boundary = max(last_boundary, match.end())
    if last_boundary < 0:
        return None
    candidate = normalized[:last_boundary].strip()
    if not candidate:
        return None
    if candidate == previous_text:
        return None
    return candidate


def _append_wrapped_notice(text: str) -> str:
    """Append the inactivity wrapped notice once."""
    stripped = text.rstrip()
    if WRAPPED_CONVERSATION_NOTICE in stripped:
        return stripped
    return stripped + "\n\n" + WRAPPED_CONVERSATION_NOTICE


def render_conversations(
    anchors: list[ConversationAnchor],
    focused_bridge_id: str | None,
    *,
    focus_commands: dict[str, str] | None = None,
    text_limit: int = TELEGRAM_MESSAGE_TEXT_LIMIT,
) -> str:
    """Render durable conversation anchors."""
    if not anchors:
        return "No attached conversations yet. Use /attach_thread to add one."
    lines = ["Conversations:"]
    commands = focus_commands or {}
    footer = "Tap a /cf_ shortcut to focus a conversation."
    omitted = 0
    for index, anchor in enumerate(anchors):
        marker = "*" if anchor.latest_bridge_id == focused_bridge_id else "-"
        status = "broken" if anchor.broken_reason else "ready"
        label = _anchor_display_name(anchor)
        selector = (
            "Focused"
            if anchor.latest_bridge_id == focused_bridge_id
            else commands.get(anchor.anchor_id, "")
        )
        entry = f"{marker} {selector}  {label}  ({status})".replace("  ", " ", 1)
        remaining = len(anchors) - index - 1
        candidate = [*lines, entry]
        if remaining:
            candidate.append(_conversation_omission_line(remaining))
        candidate.append(footer)
        if len("\n".join(candidate)) > text_limit:
            omitted = len(anchors) - index
            break
        lines.append(entry)
    if omitted:
        while (
            lines
            and len("\n".join([*lines, _conversation_omission_line(omitted), footer]))
            > text_limit
        ):
            if len(lines) == 1:
                break
            lines.pop()
            omitted += 1
        lines.append(_conversation_omission_line(omitted))
    lines.append(footer)
    return "\n".join(lines)


def render_overview(
    anchors: list[ConversationAnchor],
    focused_bridge_id: str | None,
    *,
    focused_title: str | None = None,
    focused_codex_backend_id: str | None = None,
    focused_codex_thread_id: str | None = None,
    runtime: CodexRuntimeState | None = None,
    focus_commands: dict[str, str] | None = None,
) -> str:
    """Render the operational conversation overview."""
    del focused_codex_backend_id, focused_codex_thread_id
    if not anchors and focused_bridge_id is None:
        return "No attached conversations yet. Use /attach_thread to add a Codex conversation."
    lines: list[str] = []
    focused = [
        anchor for anchor in anchors if anchor.latest_bridge_id == focused_bridge_id
    ]
    visible_background = [
        anchor
        for anchor in anchors
        if anchor.latest_bridge_id != focused_bridge_id
        and _overview_anchor_status(anchor) != "i"
    ]

    commands = focus_commands or {}
    if focused:
        for anchor in focused[:1]:
            lines.append(_overview_focused_line(_anchor_display_name(anchor)))
    elif focused_bridge_id is not None:
        lines.append(_overview_focused_line(focused_title))

    runtime_lines = _render_runtime_state_lines(runtime or CodexRuntimeState())
    if runtime_lines:
        if lines:
            lines.append("")
        lines.extend(runtime_lines)

    if visible_background:
        if lines:
            lines.append("")
        for anchor in visible_background:
            lines.append(
                _overview_anchor_line(
                    anchor,
                    focused=False,
                    thread_shortcut=commands.get(anchor.anchor_id),
                )
            )

    if not lines:
        lines.append("No active conversations.")
    return "\n".join(lines)


def overview_action_anchors(
    anchors: list[ConversationAnchor],
    focused_bridge_id: str | None,
) -> list[ConversationAnchor]:
    """Return only overview-visible anchors that deserve inline actions."""
    broken = [anchor for anchor in anchors if anchor.broken_reason]
    return [
        anchor for anchor in broken[:3] if anchor.latest_bridge_id != focused_bridge_id
    ]


def _overview_anchor_line(
    anchor: ConversationAnchor,
    *,
    focused: bool,
    thread_shortcut: str | None = None,
) -> str:
    label = escape(_anchor_display_name(anchor))
    marker = _overview_anchor_status(anchor)
    thread_ref = thread_shortcut or anchor.codex_thread_id
    thread_id = f"{escape(thread_ref)} " if not focused else ""
    if anchor.broken_reason:
        badge = escape(anchor.broken_reason)
    else:
        badge = None
    suffix = f" ({badge})" if badge else ""
    return f"{_overview_marker(marker, focused=focused)} {thread_id}{label}{suffix}"


def _overview_focused_line(title: str | None) -> str:
    return f"<b>[f]:</b> {escape(_display_name(title))}"


def _overview_marker(marker: str, *, focused: bool) -> str:
    open_marker, close_marker = ("[", "]") if focused else ("(", ")")
    return f"<b>{open_marker}{escape(marker)}{close_marker}</b>"


def _overview_anchor_status(anchor: ConversationAnchor) -> str:
    if anchor.broken_reason:
        return "!"
    if anchor.latest_bridge_pending_approval:
        return "a"
    if anchor.latest_bridge_pending_user_input:
        return "u"
    if _anchor_is_running(anchor):
        return "r"
    if _anchor_is_waiting(anchor):
        return "w"
    return "i"


def _anchor_is_running(anchor: ConversationAnchor) -> bool:
    return anchor.latest_bridge_awaiting_reply and bool(
        anchor.latest_bridge_pending_turn_id
    )


def _anchor_is_waiting(anchor: ConversationAnchor) -> bool:
    if anchor.latest_bridge_id is None or anchor.latest_bridge_closed_at is not None:
        return False
    expires_at = anchor.latest_bridge_expires_at
    if not expires_at:
        return False
    try:
        expiry = datetime.fromisoformat(expires_at)
    except ValueError:
        return False
    if expiry.tzinfo is None:
        expiry = expiry.replace(tzinfo=UTC)
    return expiry > datetime.now(UTC)


def _count_phrase(count: int, label: str) -> str:
    return f"{count} {label}"


def _status_card_is_stale(card: StatusCardState) -> bool:
    try:
        updated_at = datetime.fromisoformat(card.updated_at)
    except ValueError:
        return False
    if updated_at.tzinfo is None:
        updated_at = updated_at.replace(tzinfo=UTC)
    return (
        datetime.now(UTC) - updated_at
    ).total_seconds() >= STATUS_CARD_ROTATION_SECONDS


def _render_runtime_state_lines(runtime: CodexRuntimeState) -> list[str]:
    if runtime.is_empty:
        return []
    lines: list[str] = []
    if runtime.goal is not None:
        lines.append(
            f"<b>Goal</b> {escape(runtime.goal.objective)} "
            f"(<i>{escape(runtime.goal.status)}</i>)"
        )
    if runtime.plan_items:
        lines.append("<b>Plan</b>")
        for item in runtime.plan_items[:5]:
            lines.append(
                f"• [{escape(_plan_status_label(item.status))}] {escape(item.step)}"
            )
        omitted = len(runtime.plan_items) - 5
        if omitted > 0:
            suffix = "" if omitted == 1 else "s"
            lines.append(f"• ... {omitted} more step{suffix}")
    return lines


def render_goal_status(goal: CodexGoal | None) -> str:
    """Render the focused thread's Codex goal."""
    if goal is None:
        return "<b>Goal</b>\nNo active goal."
    lines = [
        "<b>Goal</b>",
        f"<b>Objective</b> {escape(goal.objective)}",
        f"<b>Status</b> {escape(goal.status)}",
    ]
    tokens_used = goal.tokens_used or 0
    if goal.token_budget is not None:
        lines.append(f"<b>Tokens</b> {tokens_used} / {goal.token_budget}")
    elif goal.tokens_used is not None:
        lines.append(f"<b>Tokens</b> {goal.tokens_used}")
    if goal.elapsed_seconds is not None:
        lines.append(f"<b>Elapsed</b> {goal.elapsed_seconds:g}s")
    if goal.created_at:
        lines.append(f"<b>Created</b> {_format_timestamp_for_display(goal.created_at)}")
    if goal.updated_at:
        lines.append(f"<b>Updated</b> {_format_timestamp_for_display(goal.updated_at)}")
    return "\n".join(lines)


def _format_timestamp_for_display(value: str) -> str:
    parsed = _parse_timestamp(value)
    if parsed is None:
        return f"<code>{escape(value)}</code>"
    return escape(parsed.astimezone(UTC).strftime(UTC_TIMESTAMP_FORMAT))


def _parse_timestamp(value: str) -> datetime | None:
    stripped = value.strip()
    if not stripped:
        return None
    try:
        timestamp = float(stripped)
    except ValueError:
        timestamp = None
    if timestamp is not None:
        if abs(timestamp) > 10_000_000_000:
            timestamp = timestamp / 1000
        try:
            return datetime.fromtimestamp(timestamp, tz=UTC)
        except (OSError, OverflowError, ValueError):
            return None
    try:
        parsed = datetime.fromisoformat(stripped.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed


def render_usage(state: UsageState) -> str:
    """Render account and focused-thread usage visibility."""
    lines = [
        "<b>Usage</b>",
        f"<b>Conversation</b> {escape(_display_name(state.conversation_name))}",
        f"<b>Connection</b> <code>{escape(state.backend_id)}</code>",
        f"<b>Codex thread</b> {'attached' if state.codex_thread_attached else 'unattached'}",
        "",
    ]
    lines.extend(_render_account_usage_lines(state))
    lines.append("")
    lines.extend(
        _render_token_usage_section(
            "Latest turn",
            state.token_usage,
            empty_text="No token usage recorded for this conversation yet.",
        )
    )
    runtime_metrics = _render_runtime_metrics_lines(state)
    if runtime_metrics:
        lines.append("")
        lines.extend(runtime_metrics)
    return "\n".join(lines)


def _render_account_usage_lines(state: UsageState) -> list[str]:
    account = state.account
    if account.status != "available" or not account.limits:
        reason = account.reason or account.status
        return ["<b>Account limits</b>", f"<b>Unavailable</b> {escape(reason)}"]
    lines = ["<b>Account limits</b>"]
    for limit in account.limits:
        lines.extend(_render_account_usage_limit_lines(limit))
    return lines


def _render_account_usage_limit_lines(limit: AccountUsageLimit) -> list[str]:
    parts: list[str] = []
    if limit.used_percent is not None:
        parts.append(f"{_format_number(limit.used_percent)}% used")
    if limit.used is not None and limit.limit is not None:
        parts.append(
            f"{_format_usage_number(limit.used)} / {_format_usage_number(limit.limit)}"
        )
    elif limit.used is not None:
        parts.append(f"used {_format_usage_number(limit.used)}")
    elif limit.limit is not None:
        parts.append(f"limit {_format_usage_number(limit.limit)}")
    if limit.remaining is not None:
        parts.append(f"remaining {_format_usage_number(limit.remaining)}")
    if not parts and limit.window_minutes is not None:
        parts.append(f"window {_format_usage_number(limit.window_minutes)}m")
    if not parts:
        lines = [f"<b>{escape(limit.label)}</b>"]
    else:
        lines = [f"<b>{escape(limit.label)}</b> " + ", ".join(parts)]
    if limit.resets_at:
        lines.append(f"<b>Resets</b> {_format_timestamp_for_display(limit.resets_at)}")
    return lines


def _render_token_usage_section(
    title: str,
    token_usage: dict[str, int] | None,
    *,
    empty_text: str | None = None,
) -> list[str]:
    lines = [f"<b>{escape(title)}</b>"]
    if not token_usage:
        if empty_text is not None:
            lines.append(escape(empty_text))
        return lines
    labels = (
        ("total_tokens", "Total"),
        ("input_tokens", "Input"),
        ("cached_input_tokens", "Cached"),
        ("output_tokens", "Output"),
        ("reasoning_output_tokens", "Reasoning"),
    )
    lines.extend(
        f"<b>{label}</b> {_format_usage_number(token_usage[key])}"
        for key, label in labels
        if key in token_usage
    )
    if len(lines) == 1 and empty_text is not None:
        lines.append(escape(empty_text))
    return lines


def _render_runtime_metrics_lines(state: UsageState) -> list[str]:
    metrics = state.runtime_metrics
    if metrics is None:
        return []
    lines: list[str] = []
    if metrics.total_token_usage:
        lines.extend(
            _render_token_usage_section("Runtime totals", metrics.total_token_usage)
        )
    if metrics.last_token_usage:
        if lines:
            lines.append("")
        lines.extend(
            _render_token_usage_section(
                "Runtime last turn",
                metrics.last_token_usage,
            )
        )
    if metrics.model_context_window is not None:
        if not lines:
            lines.append("<b>Runtime metrics</b>")
        lines.append(
            f"<b>Context window</b> {_format_usage_number(metrics.model_context_window)}"
        )
    return lines


def _format_usage_number(value: int) -> str:
    return f"{value:,}"


def _format_number(value: float) -> str:
    if isinstance(value, int):
        return str(value)
    if value.is_integer():
        return str(int(value))
    return str(value)


def _plan_status_label(status: str) -> str:
    if status == "in_progress":
        return "doing"
    if status == "completed":
        return "done"
    return status


def _conversation_omission_line(omitted: int) -> str:
    suffix = "" if omitted == 1 else "s"
    return f"... {omitted} more conversation{suffix} not shown."


def render_codex_threads(groups: list[CodexThreadGroup]) -> str:
    """Render Codex backend threads grouped by project."""
    return build_codex_thread_listing(groups).text


def build_codex_thread_listing(
    groups: list[CodexThreadGroup],
    *,
    failures: list[CodexThreadBackendFailure] | None = None,
    connect_commands: dict[str, str] | None = None,
    full: bool = False,
    text_limit: int = TELEGRAM_MESSAGE_TEXT_LIMIT,
) -> CodexThreadListing:
    """Render a Codex thread list without exceeding Telegram's text limit."""
    backend_failures = failures or []
    if not groups and not backend_failures:
        return CodexThreadListing(text="No threads found.", groups=[])
    commands = connect_commands or {}
    lines = ["<b>Threads</b>"]
    if full:
        lines.append("Full list. Tap a /ct_ shortcut to attach.")
    else:
        lines.append("Most recent 3 per project. Use /threads --full for more.")
    visible_groups: list[CodexThreadGroup] = []
    total_threads = sum(len(group.threads) for group in groups)
    shown_threads = 0
    stop = False
    for group in groups:
        visible_threads: list[CodexThread] = []
        for thread in group.threads:
            if not full and len(visible_threads) >= CODEX_THREADS_DEFAULT_LIMIT:
                break
            group_prefix = (
                []
                if visible_threads
                else [
                    "",
                    f"<b>Backend</b> {escape(group.backend_name)}",
                    f"<b>Project</b> {escape(_project_display_name(group.project))}",
                ]
            )
            entry_lines = _codex_thread_listing_lines(
                thread,
                connect_command=commands.get(_codex_thread_key(thread)),
            )
            remaining_after_thread = total_threads - shown_threads - 1
            candidate = lines + group_prefix + entry_lines
            if remaining_after_thread:
                candidate += _codex_threads_omission_lines(remaining_after_thread)
            if len("\n".join(candidate)) > text_limit:
                stop = True
                break
            if group_prefix:
                lines.extend(group_prefix)
            lines.extend(entry_lines)
            visible_threads.append(thread)
            shown_threads += 1
        if visible_threads:
            visible_groups.append(
                CodexThreadGroup(project=group.project, threads=visible_threads)
            )
        if stop:
            break
    omitted_threads = total_threads - shown_threads
    if omitted_threads:
        lines.extend(_codex_threads_omission_lines(omitted_threads))
    for failure in backend_failures:
        lines.extend(
            [
                "",
                (
                    f"Backend {escape(failure.backend_name)} unavailable: "
                    f"{escape(_short_line(failure.error, limit=120))}"
                ),
            ]
        )
    return CodexThreadListing(text="\n".join(lines), groups=visible_groups)


def build_recent_codex_thread_listing(
    groups: list[CodexThreadGroup],
    *,
    failures: list[CodexThreadBackendFailure] | None = None,
    connect_commands: dict[str, str] | None = None,
    text_limit: int = TELEGRAM_MESSAGE_TEXT_LIMIT,
) -> CodexThreadListing:
    """Render a flat recent thread list without grouping by connection."""
    backend_failures = failures or []
    threads = sorted(
        [thread for group in groups for thread in group.threads],
        key=lambda thread: -thread.updated_at,
    )
    if not threads and not backend_failures:
        return CodexThreadListing(text="No threads found.", groups=[])
    commands = connect_commands or {}
    lines = ["<b>Recent threads</b>", "Tap a /ct_ shortcut to attach."]
    visible_threads: list[CodexThread] = []
    for thread in threads:
        entry_lines = _recent_codex_thread_listing_lines(
            thread,
            connect_command=commands.get(_codex_thread_key(thread)),
        )
        candidate = lines + [""] + entry_lines
        if len("\n".join(candidate)) > text_limit:
            break
        lines.extend(["", *entry_lines])
        visible_threads.append(thread)
    for failure in backend_failures:
        lines.extend(
            [
                "",
                (
                    f"Backend {escape(failure.backend_name)} unavailable: "
                    f"{escape(_short_line(failure.error, limit=120))}"
                ),
            ]
        )
    return CodexThreadListing(
        text="\n".join(lines),
        groups=_group_visible_threads_by_backend_project(visible_threads),
    )


def _codex_thread_key(thread: CodexThread) -> str:
    return f"{thread.codex_backend_id}:{thread.thread_id}"


def _codex_thread_listing_lines(
    thread: CodexThread,
    *,
    connect_command: str | None = None,
) -> list[str]:
    title = escape(
        _short_line(thread.title or thread.preview or "(untitled)", limit=80)
    )
    command = connect_command or (
        f"/attach_thread --connection {thread.codex_backend_name} "
        f"{thread.thread_id}"
    )
    metadata = [f"<i>{escape(thread.anchor_status)}</i>"]
    status = _codex_thread_status_label(thread.status)
    if status is not None:
        metadata.insert(0, f"<b>{escape(status)}</b>")
    return [f"{command}  <b>{title}</b>", "  " + " | ".join(metadata)]


def _recent_codex_thread_listing_lines(
    thread: CodexThread,
    *,
    connect_command: str | None = None,
) -> list[str]:
    title = escape(
        _short_line(thread.title or thread.preview or "(untitled)", limit=80)
    )
    command = connect_command or (
        f"/attach_thread --connection {thread.codex_backend_name} "
        f"{thread.thread_id}"
    )
    metadata = [
        f"Connection {escape(thread.codex_backend_name)}",
        f"Project {escape(_project_display_name(thread.cwd or '(no project)'))}",
    ]
    status = _codex_thread_status_label(thread.status)
    if status is not None:
        metadata.append(status)
    metadata.append(thread.anchor_status)
    return [f"{command}  <b>{title}</b>", "  " + " | ".join(metadata)]


def _group_visible_threads_by_backend_project(
    threads: list[CodexThread],
) -> list[CodexThreadGroup]:
    groups: dict[tuple[str, str, str], list[CodexThread]] = {}
    for thread in threads:
        groups.setdefault(
            (
                thread.codex_backend_id,
                thread.codex_backend_name,
                thread.cwd or "(no project)",
            ),
            [],
        ).append(thread)
    return [
        CodexThreadGroup(
            project=project,
            threads=group_threads,
            backend_id=backend_id,
            backend_name=backend_name,
        )
        for (backend_id, backend_name, project), group_threads in groups.items()
    ]


def _project_display_name(project: str) -> str:
    stripped = project.rstrip("/")
    if not stripped:
        return project
    return stripped.rsplit("/", 1)[-1] or stripped


def _codex_thread_status_label(status: str) -> str | None:
    if status == "notLoaded":
        return None
    if status == "idle":
        return "loaded"
    return status


def _codex_threads_omission_lines(omitted_threads: int) -> list[str]:
    thread_unit = "thread" if omitted_threads == 1 else "threads"
    return [
        "",
        f"... {omitted_threads} more {thread_unit} not shown. "
        "Use /threads --full search-term to narrow the list.",
    ]


def render_codex_thread_attached(connection: ConversationAttachment) -> str:
    """Render a successful conversation attachment."""
    title = _display_name(
        connection.anchor.title
        or connection.codex_thread.title
        or connection.codex_thread.preview
    )
    return "\n".join(
        [
            COMMAND_SUCCESS_PREFIX + f"<b>Attached Codex thread</b> {escape(title)}",
            f"<b>Conversation</b> {escape(title)}",
        ]
    )


def render_settings(settings: EffectiveSettings) -> str:
    """Render effective settings."""
    return "\n".join(
        [
            f"<b>Profile</b> {escape(settings.profile)}",
            f"<b>Model</b> <code>{escape(settings.model)}</code>",
            f"<b>Provider</b> {escape(settings.model_provider)}",
            f"<b>Effort</b> {escape(settings.effort)}",
            f"<b>Summary</b> {escape(settings.summary)}",
            f"<b>CWD</b> <code>{escape(settings.cwd or '(default)')}</code>",
            f"<b>Fast mode</b> {'on' if settings.fast_mode else 'off'}",
            f"<b>Verbosity</b> {escape(settings.verbosity)}",
            f"<b>Command verbosity</b> {escape(settings.command_verbosity)}",
            f"<b>Follow-up mode</b> {escape(settings.followup_mode)}",
            f"<b>Mode</b> {escape(settings.collaboration_mode)}",
        ]
    )


def render_current_thread(current: CurrentThreadState) -> str:
    """Render detailed active-thread state."""
    thread = current.thread
    status = "running" if thread.awaiting_reply or thread.pending_turn_id else "idle"
    lines = [
        f"<b>Conversation</b> {escape(_logical_thread_name(thread))}",
        f"<b>Connection</b> {escape(thread.codex_backend_id or '(default)')}",
        f"<b>Codex thread</b> {'attached' if thread.codex_thread_id else 'unattached'}",
        f"<b>Status</b> {status}",
        f"<b>Turns</b> {thread.turn_count}",
        render_settings(current.settings),
    ]
    if current.pending is not None:
        lines.append(
            "<b>Pending approval</b> "
            + escape(current.pending.command or current.pending.method)
        )
    if current.realtime is not None:
        lines.append(f"<b>Realtime</b> active ({escape(current.realtime.status)})")
    lines.extend(_render_runtime_state_lines(current.runtime))
    return "\n".join(lines)


def render_directory_state(state: DirectoryState) -> str:
    """Render current directory and recent history."""
    lines = [
        f"<b>Conversation</b> {escape(_logical_thread_name(state.thread))}",
        f"<b>Current directory</b> <code>{escape(state.current_path or '(profile default)')}</code>",
    ]
    if not state.history:
        lines.append("<b>History</b> (empty)")
        lines.append("Use /dir <path>, /dir -, /dir <index>, or /dir reset.")
        return "\n".join(lines)
    lines.append("<b>Recent directories</b>")
    for index, entry in enumerate(state.history, start=1):
        suffix = " (current)" if entry.path == state.current_path else ""
        lines.append(f"• <b>{index}.</b> <code>{escape(entry.path)}</code>{suffix}")
    lines.append("Use /dir <path>, /dir -, /dir <index>, or /dir reset.")
    return "\n".join(lines)


def render_project_state(state: ProjectState) -> str:
    """Render the active Project binding and project-scoped config."""
    lines = [
        f"<b>Conversation</b> {escape(_logical_thread_name(state.thread))}",
        "<b>Active project</b> "
        + (_render_project_ref(state.active) if state.active is not None else "(none)"),
    ]
    if state.active is not None:
        lines.append(f"<b>Root</b> <code>{escape(state.active.root_path)}</code>")
        lines.append(f"<b>Connection</b> {escape(state.active.connection_id)}")
        lines.append(f"<b>Label</b> {escape(state.active.label)}")
    lines.append("<b>Project config</b>")
    lines.append(
        f"• <b>Model</b> <code>{escape(state.project_overrides.model or '(default)')}</code>"
    )
    lines.append(
        f"• <b>Effort</b> {escape(state.project_overrides.effort or '(default)')}"
    )
    fast_mode = state.project_overrides.fast_mode
    lines.append(
        "• <b>Fast mode</b> "
        + ("(default)" if fast_mode is None else "on" if fast_mode else "off")
    )
    return "\n".join(lines)


def _render_project_ref(project: Project) -> str:
    return (
        f"{escape(project.connection_id)}:{escape(project.label)}"
        f" -&gt; <code>{escape(project.root_path)}</code>"
    )


def render_webhooks(subscriptions: list[WebhookSubscription]) -> str:
    """Render webhook subscriptions for one chat."""
    if not subscriptions:
        return (
            "<b>Webhooks</b>\nNo webhook subscriptions for this chat. "
            "Use /webhook create <name> to bind one to the focused conversation."
        )
    lines = ["<b>Webhook subscriptions</b>"]
    for subscription in subscriptions:
        status = "enabled" if subscription.enabled else "disabled"
        trigger_unit = "trigger" if subscription.trigger_count == 1 else "triggers"
        lines.append(
            f"• <b>{escape(subscription.name)}</b> "
            + f"({status}, {subscription.trigger_count} {trigger_unit})"
        )
    lines.append("Use /webhook revoke <id-or-name> to disable one.")
    return "\n".join(lines)


def render_webhook_created(
    subscription: WebhookSubscription,
    *,
    event_secret: str,
    event_url: str,
) -> str:
    """Render newly created webhook details, including the one-time secret."""
    return "\n".join(
        [
            COMMAND_SUCCESS_PREFIX
            + f"<b>Created webhook</b> {escape(subscription.name)}",
            f"<b>ID</b> <code>{escape(subscription.webhook_id)}</code>",
            f"<b>Event URL</b> <code>{escape(event_url)}</code>",
            f"<b>Bearer secret</b> <code>{escape(event_secret)}</code> (shown once)",
        ]
    )


def render_single_setting(name: str, settings: EffectiveSettings) -> str:
    """Render one setting after mutation or query."""
    if name == "fast":
        return f"<b>Fast mode</b> {'on' if settings.fast_mode else 'off'}"
    return f"<b>{escape(name)}</b> {escape(str(getattr(settings, name)))}"


def render_status(
    conversation_name: str,
    settings: EffectiveSettings,
    pending: PendingApproval | None,
    realtime=None,
    *,
    runtime: CodexRuntimeState | None = None,
) -> str:
    """Render a compact status message."""
    lines = [
        f"<b>Conversation</b> {escape(_display_name(conversation_name))}",
        render_settings(settings),
    ]
    if pending is not None:
        lines.append(
            "<b>Pending approval</b> " + escape(pending.command or pending.method)
        )
    if isinstance(realtime, RealtimeSession):
        lines.append(f"<b>Realtime</b> active ({escape(realtime.status)})")
    if runtime is not None:
        lines.extend(_render_runtime_state_lines(runtime))
    return "\n".join(lines)


def render_approval_request(pending: PendingApproval) -> str:
    """Render a Telegram-facing approval prompt."""
    command = pending.command or "(no command text)"
    lines = [
        WARNING_PREFIX + "<b>Codex needs approval</b>",
        f"<b>Command</b> <code>{escape(command)}</code>",
    ]
    if pending.reason:
        lines.append(f"<b>Reason</b> {escape(pending.reason)}")
    if pending.message and pending.message != pending.reason:
        lines.append(f"<b>Guardian</b> {escape(pending.message)}")
    return "\n".join(lines)


def render_user_input_request(pending: PendingUserInput) -> str:
    """Render a Telegram-facing user-input prompt."""
    lines = [WARNING_PREFIX + "<b>Codex needs your input</b>"]
    for index, question in enumerate(pending.questions, start=1):
        prefix = f"{index}. " if len(pending.questions) > 1 else ""
        header = f"<b>{escape(question.header)}</b> " if question.header else ""
        lines.append(prefix + header + escape(question.question))
        selected = pending.selected_answers.get(question.question_id)
        if selected:
            lines.append("<b>Selected</b> " + escape(", ".join(selected)))
        elif pending.awaiting_free_text_question_id == question.question_id:
            lines.append("<b>Waiting</b> for custom answer text.")
        for option in question.options:
            if option.description:
                lines.append(
                    f"• <b>{escape(option.label)}</b> {escape(option.description)}"
                )
    return "\n".join(lines)


def _user_input_complete(pending: PendingUserInput) -> bool:
    if pending.awaiting_free_text_question_id is not None:
        return False
    return all(
        bool(pending.selected_answers.get(question.question_id))
        for question in pending.questions
    )


def render_history(history: ThreadHistory) -> str:
    """Render recent compact thread history."""
    title = _logical_thread_name(history.thread)
    if not history.entries:
        return f"<b>History</b>\nNo saved history for conversation {escape(title)} yet."
    lines = [f"<b>Recent history</b> {escape(title)}"]
    for entry in history.entries:
        lines.append(
            f"<b>{escape(_thread_message_label(entry, assistant_label='Assistant'))}</b> "
            f"{escape(_short_line(entry.text))}"
        )
    return "\n".join(lines)


def _thread_message_label(entry: ThreadMessage, *, assistant_label: str) -> str:
    return {
        ("user", "prompt"): "User",
        ("assistant", "final"): assistant_label,
        ("system", "error"): "Error",
        ("system", "interrupted"): "Interrupted",
    }.get((entry.role, entry.kind), f"{entry.role}/{entry.kind}")


def render_skills(catalogs: list[SkillCatalog]) -> str:
    """Render available Codex skills."""
    skills = [skill for catalog in catalogs for skill in catalog.skills]
    if not skills:
        return "<b>Skills</b>\nNo skills found for the current conversation."
    lines = ["<b>Skills</b>"]
    for scope in ("repo", "user", "system", "admin"):
        scoped = [skill for skill in skills if skill.scope == scope and skill.enabled]
        if not scoped:
            continue
        lines.append("")
        lines.append(f"<b>{escape(scope)}</b>")
        for skill in sorted(scoped, key=lambda item: item.name.casefold()):
            shortcut = f"/skill_{_skill_slug(skill.name)}"
            description = skill.short_description or skill.description
            suffix = f" - {escape(_short_line(description, 90))}" if description else ""
            lines.append(f"{escape(shortcut)} {escape(skill.name)}{suffix}")
    disabled = [skill for skill in skills if not skill.enabled]
    if disabled:
        lines.append("")
        lines.append(f"Disabled: {_count_phrase(len(disabled), 'skill')}")
    return "\n".join(lines)


def render_mcp_servers(
    servers: list[McpServerCapability],
    *,
    view: str = "summary",
    server_name: str | None = None,
    text_limit: int = TELEGRAM_MESSAGE_TEXT_LIMIT,
) -> str:
    """Render MCP server inventory."""
    selected = [
        server
        for server in servers
        if server_name is None or server.name.casefold() == server_name.casefold()
    ]
    if not selected:
        return "<b>MCP</b>\nNo MCP servers found."
    lines = ["<b>MCP</b>"]
    if view == "tools":
        total_tools = sum(len(server.tools) for server in selected)
        shown_tools = 0
        omitted_added = False
        for server in selected:
            header = ["", f"<b>{escape(server.name)}</b> tools"]
            if not server.tools:
                candidate = lines + header + ["No tools."]
                if len("\n".join(candidate)) <= text_limit:
                    lines = candidate
                continue
            header_added = False
            for tool in sorted(server.tools, key=lambda item: item.name.casefold()):
                description = tool.description or tool.title or ""
                suffix = (
                    f" - {escape(_plain_capability_line(description, 90))}"
                    if description
                    else ""
                )
                entry = f"{escape(tool.name)}{suffix}"
                prefix = [] if header_added else header
                remaining_after_tool = total_tools - shown_tools - 1
                candidate = lines + prefix + [entry]
                if remaining_after_tool:
                    candidate.append(_mcp_omission_line(remaining_after_tool, "tool"))
                if len("\n".join(candidate)) > text_limit:
                    _append_limited_line(
                        lines,
                        _mcp_omission_line(total_tools - shown_tools, "tool"),
                        text_limit=text_limit,
                    )
                    omitted_added = True
                    break
                lines.extend(prefix)
                header_added = True
                lines.append(entry)
                shown_tools += 1
            if omitted_added:
                break
        return "\n".join(lines)
    if view == "resources":
        for server in selected:
            lines.append("")
            lines.append(f"<b>{escape(server.name)}</b> resources")
            if not server.resources and not server.resource_templates:
                lines.append("No resources.")
                continue
            for resource in server.resources:
                lines.append(f"{escape(resource.name)} - {escape(resource.uri)}")
            for resource in server.resource_templates:
                lines.append(f"{escape(resource.name)} - {escape(resource.uri)}")
        return "\n".join(lines)
    for server in selected:
        lines.append(
            f"{escape(server.name)} ({escape(server.auth_status)}): "
            f"{_count_phrase(len(server.tools), 'tool')}, "
            f"{_count_phrase(len(server.resources), 'resource')}, "
            f"{_count_phrase(len(server.resource_templates), 'template')}"
        )
    lines.append("Use /mcp tools [server] or /mcp resources [server].")
    return "\n".join(lines)


def render_help() -> str:
    """Render Telegram command help from the published catalog."""
    lines = ["<b>Supported commands</b>"]
    for command in telegram_bot_commands():
        lines.append(
            f"• <code>/{escape(command.command)}</code> - {escape(command.description)}"
        )
    return "\n".join(lines)


def telegram_bot_commands() -> list[BotCommand]:
    """Return the Telegram command catalog published via BotFather clients."""
    return [
        BotCommand(command=command, description=description)
        for command, description in TELEGRAM_COMMAND_SPECS
    ]


def _short_line(text: str, limit: int = 120) -> str:
    value = " ".join(text.split())
    if len(value) <= limit:
        return value
    return value[: limit - 3] + "..."


def _plain_capability_line(text: str, limit: int = 120) -> str:
    return _short_line(HTML_TAG_RE.sub(" ", unescape(text)), limit=limit)


def _mcp_omission_line(omitted: int, unit: str) -> str:
    suffix = "" if omitted == 1 else "s"
    return f"... {omitted} more {unit}{suffix} not shown."


def _append_limited_line(
    lines: list[str],
    line: str,
    *,
    text_limit: int,
) -> None:
    while len("\n".join([*lines, line])) > text_limit and len(lines) > 1:
        lines.pop()
    if len("\n".join([*lines, line])) <= text_limit:
        lines.append(line)


def _skill_slug(name: str) -> str:
    return "".join(char if char.isalnum() else "_" for char in name.casefold()).strip(
        "_"
    )


def _display_name(value: str | None, *, fallback: str = "New conversation") -> str:
    normalized = _short_line(value or "", limit=80)
    return normalized or fallback


def _logical_thread_name(thread: LogicalThread) -> str:
    return _display_name(thread.title)


def _anchor_display_name(anchor: ConversationAnchor) -> str:
    return _display_name(anchor.alias or anchor.title)


def _public_error_summary(err: BaseException, *, limit: int = 240) -> str:
    message = " ".join(str(err).split())
    if not message:
        message = err.__class__.__name__
    lowered = message.lower()
    if "message is too long" in lowered:
        return "Telegram rejected a bot message because it was too long."
    return _short_line(message, limit=limit)
