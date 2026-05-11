from codex_telegram.adapters.telegram.bot import (
    _coalesce_progress_text,
    _parse_command_options,
    build_chat_key,
    overview_action_anchors,
    render_current_thread,
    render_directory_state,
    render_goal_status,
    render_help,
    render_history,
    render_mcp_servers,
    render_overview,
    render_conversations,
    render_codex_threads,
    render_approval_request,
    render_skills,
    render_status,
    render_usage,
    render_user_input_request,
    render_webhook_created,
    render_webhooks,
    render_project_state,
    split_command,
    telegram_bot_commands,
)
from codex_telegram.adapters.telegram.rendering import TELEGRAM_MESSAGE_TEXT_LIMIT
from codex_telegram.application.models import (
    AccountUsage,
    AccountUsageLimit,
    CodexPlanItem,
    CodexThreadGroup,
    CodexRuntimeState,
    CurrentThreadState,
    DirectoryState,
    EffectiveSettings,
    ThreadHistory,
    ProjectState,
    RuntimeUsageMetrics,
    McpResourceCapability,
    McpServerCapability,
    McpToolCapability,
    SkillCapability,
    SkillCatalog,
    UsageState,
)
from codex_telegram.domain import (
    CodexGoal,
    CodexThread,
    ConversationAnchor,
    DirectoryEntry,
    LogicalThread,
    PendingApproval,
    PendingUserInput,
    SessionOverrides,
    ThreadMessage,
    UserInputOption,
    UserInputQuestion,
    WebhookSubscription,
    Project,
)
import pytest


def test_split_command_handles_bot_suffix() -> None:
    assert split_command("/threads@codex_bot") == ("threads", "")
    assert split_command("/focus abc") == ("focus", "abc")


def test_published_commands_use_threads_name_for_backend_thread_listing() -> None:
    commands = {command.command for command in telegram_bot_commands()}

    assert "threads" in commands


def test_parse_command_options_accepts_shared_flags_and_positionals() -> None:
    parsed = _parse_command_options(
        "--connection laptop --project app fix failing tests",
        allowed_flags={"connection", "project"},
    )

    assert parsed.connection == "laptop"
    assert parsed.project == "app"
    assert parsed.positionals == ["fix", "failing", "tests"]


def test_parse_command_options_rejects_unknown_flags() -> None:
    with pytest.raises(ValueError, match="Unknown option: --bogus"):
        _parse_command_options("--bogus value", allowed_flags={"connection"})


def test_coalesce_progress_text_waits_for_complete_line() -> None:
    assert _coalesce_progress_text("Plan:\nStill streaming", None) == "Plan:"


def test_coalesce_progress_text_prefers_completed_sentence_before_line() -> None:
    assert (
        _coalesce_progress_text("Plan:\nFirst sentence. Still streaming", "Plan:")
        == "Plan:\nFirst sentence."
    )


def test_render_conversations_marks_focused_bridge() -> None:
    anchors = [
        ConversationAnchor(
            anchor_id="anchor-a",
            chat_key="chat:1",
            title="First",
            codex_backend_id="primary",
            codex_thread_id="codex-a",
            alias=None,
            project_id=None,
            latest_bridge_id="bridge-a",
            created_at="now",
            updated_at="now",
        ),
        ConversationAnchor(
            anchor_id="anchor-b",
            chat_key="chat:1",
            title="Second",
            codex_backend_id="primary",
            codex_thread_id="codex-b",
            alias=None,
            project_id=None,
            latest_bridge_id="bridge-b",
            created_at="now",
            updated_at="now",
        ),
    ]

    rendered = render_conversations(anchors, "bridge-b")
    assert "* Focused Second" in rendered
    assert "-  First" in rendered
    assert "anchor-a" not in rendered
    assert "anchor-b" not in rendered
    assert "/focus <id>" not in rendered


def test_render_overview_groups_focused_and_broken_conversations() -> None:
    rendered = render_overview(
        [
            ConversationAnchor(
                anchor_id="anchor-a",
                chat_key="chat:1",
                codex_backend_id="primary",
                codex_thread_id="codex-a",
                title="Focused",
                alias=None,
                project_id=None,
                latest_bridge_id="bridge-a",
                created_at="now",
                updated_at="now",
            ),
            ConversationAnchor(
                anchor_id="anchor-b",
                chat_key="chat:1",
                codex_backend_id="primary",
                codex_thread_id="codex-b",
                title="Broken",
                alias=None,
                project_id=None,
                latest_bridge_id="bridge-b",
                created_at="now",
                updated_at="now",
                broken_reason="missing backend",
            ),
        ],
        "bridge-a",
    )

    assert "Overview:" not in rendered
    assert "<b>[f]:</b> Focused" in rendered
    assert "<b>(!)</b> codex-b Broken (missing backend)" in rendered
    assert "missing backend" in rendered
    assert "Shortcuts:" not in rendered
    assert "anchor-a" not in rendered
    assert "codex-a" not in rendered


def test_render_overview_collapses_dormant_recent_conversations() -> None:
    anchors = [
        ConversationAnchor(
            anchor_id="anchor-focused",
            chat_key="chat:1",
            codex_backend_id="primary",
            codex_thread_id="codex-focused",
            title="Focused",
            alias=None,
            project_id=None,
            latest_bridge_id="bridge-focused",
            created_at="now",
            updated_at="now",
        ),
        *[
            ConversationAnchor(
                anchor_id=f"anchor-{index}",
                chat_key="chat:1",
                codex_backend_id="primary",
                codex_thread_id=f"codex-{index}",
                title="New conversation",
                alias=None,
                project_id=None,
                latest_bridge_id=f"bridge-{index}",
                created_at="now",
                updated_at="now",
            )
            for index in range(5)
        ],
    ]

    rendered = render_overview(anchors, "bridge-focused")

    assert "Dormant:" not in rendered
    assert rendered.count("<b>[f]:</b>") == 1
    assert "anchor-4" not in rendered
    assert "New conversation" not in rendered
    assert "Recent:" not in rendered


def test_render_overview_lists_nonfocused_running_and_waiting_conversations() -> None:
    future = "2999-01-01T00:00:00+00:00"
    anchors = [
        ConversationAnchor(
            anchor_id="anchor-focused",
            chat_key="chat:1",
            codex_backend_id="primary",
            codex_thread_id="codex-focused",
            title="Focused",
            alias=None,
            project_id=None,
            latest_bridge_id="bridge-focused",
            created_at="now",
            updated_at="now",
        ),
        ConversationAnchor(
            anchor_id="anchor-running",
            chat_key="chat:1",
            codex_backend_id="primary",
            codex_thread_id="019df8d4-c061-74f0-a043-running",
            title="Background work",
            alias=None,
            project_id=None,
            latest_bridge_id="bridge-running",
            created_at="now",
            updated_at="now",
            latest_bridge_pending_turn_id="turn-running",
            latest_bridge_awaiting_reply=True,
        ),
        ConversationAnchor(
            anchor_id="anchor-dormant",
            chat_key="chat:1",
            codex_backend_id="primary",
            codex_thread_id="codex-dormant",
            title="Dormant",
            alias=None,
            project_id=None,
            latest_bridge_id="bridge-dormant",
            created_at="now",
            updated_at="now",
        ),
        ConversationAnchor(
            anchor_id="anchor-waiting",
            chat_key="chat:1",
            codex_backend_id="primary",
            codex_thread_id="019df8d4-c061-74f0-a043-waiting",
            title="Waiting work",
            alias=None,
            project_id=None,
            latest_bridge_id="bridge-waiting",
            created_at="now",
            updated_at="now",
            latest_bridge_expires_at=future,
        ),
    ]

    rendered = render_overview(
        anchors,
        "bridge-focused",
        focus_commands={
            "anchor-running": "/ct_running",
            "anchor-waiting": "/ct_waiting",
        },
    )

    assert "Running:" not in rendered
    assert "<b>(r)</b> /ct_running Background work" in rendered
    assert "<b>(w)</b> /ct_waiting Waiting work" in rendered
    assert "019df8d4-c061-74f0-a043-running" not in rendered
    assert "019df8d4-c061-74f0-a043-waiting" not in rendered
    assert "(running)" not in rendered
    assert "anchor-running" not in rendered
    assert "turn-running" not in rendered
    assert "Dormant:" not in rendered
    assert "codex-dormant" not in rendered


def test_render_overview_includes_unanchored_focused_bridge() -> None:
    rendered = render_overview(
        [],
        "bridge-1",
        focused_title="New conversation",
        focused_codex_backend_id="primary",
        focused_codex_thread_id=None,
    )

    assert "<b>[f]:</b> New conversation" in rendered
    assert "(unattached)" not in rendered
    assert "bridge-1" not in rendered
    assert "Overview is empty" not in rendered


def test_render_overview_uses_actual_focused_status_markers() -> None:
    future = "2999-01-01T00:00:00+00:00"

    cases = [
        (
            ConversationAnchor(
                anchor_id="anchor-approval",
                chat_key="chat:1",
                codex_backend_id="primary",
                codex_thread_id="codex-approval",
                title="Needs approval",
                alias=None,
                project_id=None,
                latest_bridge_id="bridge-approval",
                created_at="now",
                updated_at="now",
                latest_bridge_pending_approval=True,
                latest_bridge_pending_turn_id="turn-approval",
                latest_bridge_awaiting_reply=True,
            ),
            "bridge-approval",
            "<b>[f]:</b> Needs approval",
        ),
        (
            ConversationAnchor(
                anchor_id="anchor-input",
                chat_key="chat:1",
                codex_backend_id="primary",
                codex_thread_id="codex-input",
                title="Needs input",
                alias=None,
                project_id=None,
                latest_bridge_id="bridge-input",
                created_at="now",
                updated_at="now",
                latest_bridge_pending_user_input=True,
                latest_bridge_pending_turn_id="turn-input",
                latest_bridge_awaiting_reply=True,
            ),
            "bridge-input",
            "<b>[f]:</b> Needs input",
        ),
        (
            ConversationAnchor(
                anchor_id="anchor-running",
                chat_key="chat:1",
                codex_backend_id="primary",
                codex_thread_id="codex-running",
                title="Running",
                alias=None,
                project_id=None,
                latest_bridge_id="bridge-running",
                created_at="now",
                updated_at="now",
                latest_bridge_pending_turn_id="turn-running",
                latest_bridge_awaiting_reply=True,
            ),
            "bridge-running",
            "<b>[f]:</b> Running",
        ),
        (
            ConversationAnchor(
                anchor_id="anchor-waiting",
                chat_key="chat:1",
                codex_backend_id="primary",
                codex_thread_id="codex-waiting",
                title="Waiting",
                alias=None,
                project_id=None,
                latest_bridge_id="bridge-waiting",
                created_at="now",
                updated_at="now",
                latest_bridge_expires_at=future,
            ),
            "bridge-waiting",
            "<b>[f]:</b> Waiting",
        ),
        (
            ConversationAnchor(
                anchor_id="anchor-idle",
                chat_key="chat:1",
                codex_backend_id="primary",
                codex_thread_id="codex-idle",
                title="Idle",
                alias=None,
                project_id=None,
                latest_bridge_id="bridge-idle",
                created_at="now",
                updated_at="now",
            ),
            "bridge-idle",
            "<b>[f]:</b> Idle",
        ),
    ]

    for anchor, focused_bridge_id, expected in cases:
        rendered = render_overview([anchor], focused_bridge_id)
        assert expected in rendered
        assert "<b>[r]</b>" not in rendered
        assert "<b>[w]</b>" not in rendered
        assert "<b>[a]</b>" not in rendered
        assert "<b>[u]</b>" not in rendered


def test_render_overview_uses_pending_status_for_nonfocused_conversations() -> None:
    anchors = [
        ConversationAnchor(
            anchor_id="anchor-focused",
            chat_key="chat:1",
            codex_backend_id="primary",
            codex_thread_id="codex-focused",
            title="Focused",
            alias=None,
            project_id=None,
            latest_bridge_id="bridge-focused",
            created_at="now",
            updated_at="now",
        ),
        ConversationAnchor(
            anchor_id="anchor-approval",
            chat_key="chat:1",
            codex_backend_id="primary",
            codex_thread_id="codex-approval",
            title="Approve <cmd>",
            alias=None,
            project_id=None,
            latest_bridge_id="bridge-approval",
            created_at="now",
            updated_at="now",
            latest_bridge_pending_approval=True,
            latest_bridge_pending_turn_id="turn-approval",
            latest_bridge_awaiting_reply=True,
        ),
        ConversationAnchor(
            anchor_id="anchor-input",
            chat_key="chat:1",
            codex_backend_id="primary",
            codex_thread_id="codex-input",
            title="Choose & continue",
            alias=None,
            project_id=None,
            latest_bridge_id="bridge-input",
            created_at="now",
            updated_at="now",
            latest_bridge_pending_user_input=True,
            latest_bridge_pending_turn_id="turn-input",
            latest_bridge_awaiting_reply=True,
        ),
    ]

    rendered = render_overview(
        anchors,
        "bridge-focused",
        focus_commands={
            "anchor-approval": "/ct_approval",
            "anchor-input": "/ct_input",
        },
    )

    assert "<b>(a)</b> /ct_approval Approve &lt;cmd&gt;" in rendered
    assert "<b>(u)</b> /ct_input Choose &amp; continue" in rendered
    assert "(running)" not in rendered
    assert "<b>[f]:</b> Focused" in rendered


def test_render_overview_includes_active_goal() -> None:
    rendered = render_overview(
        [],
        "bridge-1",
        focused_title="Focused",
        focused_codex_backend_id="primary",
        focused_codex_thread_id=None,
        runtime=CodexRuntimeState(
            goal=CodexGoal(
                objective="Ship <overview> & status improvements",
                status="active",
            )
        ),
    )

    assert (
        "<b>Goal</b> Ship &lt;overview&gt; &amp; status improvements " "(<i>active</i>)"
    ) in rendered


def test_render_status_includes_active_goal() -> None:
    rendered = render_status(
        "thread-1",
        EffectiveSettings(
            profile="operator",
            model="gpt-5.4",
            model_provider="openai",
            effort="medium",
            summary="concise",
            cwd="/agent",
            fast_mode=False,
            verbosity="verbose",
            command_verbosity="errors",
            followup_mode="steer",
            overrides={},
        ),
        pending=None,
        runtime=CodexRuntimeState(
            goal=CodexGoal(
                objective="Ship overview status improvements",
                status="active",
            )
        ),
    )

    assert "<b>Goal</b> Ship overview status improvements (<i>active</i>)" in rendered


def test_render_overview_includes_plan_state() -> None:
    rendered = render_overview(
        [],
        "bridge-1",
        focused_title="Focused",
        focused_codex_backend_id="primary",
        focused_codex_thread_id="codex-1",
        runtime=CodexRuntimeState(
            plan_items=(
                CodexPlanItem(step="Inspect status card", status="completed"),
                CodexPlanItem(step="Render plan state", status="in_progress"),
            )
        ),
    )

    assert "<b>Plan</b>" in rendered
    assert "• [done] Inspect status card" in rendered
    assert "• [doing] Render plan state" in rendered


def test_overview_action_anchors_excludes_focused_and_dormant_conversations() -> None:
    focused = ConversationAnchor(
        anchor_id="anchor-focused",
        chat_key="chat:1",
        codex_backend_id="primary",
        codex_thread_id="codex-focused",
        title="Focused",
        alias=None,
        project_id=None,
        latest_bridge_id="bridge-focused",
        created_at="now",
        updated_at="now",
    )
    broken = ConversationAnchor(
        anchor_id="anchor-broken",
        chat_key="chat:1",
        codex_backend_id="primary",
        codex_thread_id="codex-broken",
        title="Broken",
        alias=None,
        project_id=None,
        latest_bridge_id="bridge-broken",
        created_at="now",
        updated_at="now",
        broken_reason="missing backend",
    )
    dormant = ConversationAnchor(
        anchor_id="anchor-dormant",
        chat_key="chat:1",
        codex_backend_id="primary",
        codex_thread_id="codex-dormant",
        title="Dormant",
        alias=None,
        project_id=None,
        latest_bridge_id="bridge-dormant",
        created_at="now",
        updated_at="now",
    )

    assert overview_action_anchors([focused, broken, dormant], "bridge-focused") == [
        broken
    ]


def test_render_conversations_can_show_focus_shortcuts() -> None:
    anchors = [
        ConversationAnchor(
            anchor_id="anchor-a",
            chat_key="chat:1",
            title="First",
            codex_backend_id="primary",
            codex_thread_id="codex-a",
            alias=None,
            project_id=None,
            latest_bridge_id="bridge-a",
            created_at="now",
            updated_at="now",
        ),
        ConversationAnchor(
            anchor_id="anchor-b",
            chat_key="chat:1",
            title="Second",
            codex_backend_id="primary",
            codex_thread_id="codex-b",
            alias=None,
            project_id=None,
            latest_bridge_id="bridge-b",
            created_at="now",
            updated_at="now",
        ),
    ]

    rendered = render_conversations(
        anchors,
        "bridge-b",
        focus_commands={"anchor-a": "/cf_deadbeef", "anchor-b": "/cf_focused"},
    )

    assert "- /cf_deadbeef First" in rendered
    assert "* Focused Second" in rendered
    assert "anchor-b" not in rendered
    assert "/cf_focused" not in rendered


def test_render_conversations_omits_entries_that_would_exceed_text_limit() -> None:
    anchors = [
        ConversationAnchor(
            anchor_id="anchor-focused",
            chat_key="chat:1",
            title="Focused",
            codex_backend_id="primary",
            codex_thread_id="codex-focused",
            alias=None,
            project_id=None,
            latest_bridge_id="bridge-focused",
            created_at="now",
            updated_at="now",
        ),
        *[
            ConversationAnchor(
                anchor_id=f"anchor-{index}",
                chat_key="chat:1",
                title=f"Conversation {index}",
                codex_backend_id="primary",
                codex_thread_id=f"codex-{index}",
                alias=None,
                project_id=None,
                latest_bridge_id=f"bridge-{index}",
                created_at="now",
                updated_at="now",
            )
            for index in range(20)
        ],
    ]

    rendered = render_conversations(
        anchors,
        "bridge-focused",
        focus_commands={
            anchor.anchor_id: f"/cf_{anchor.anchor_id}" for anchor in anchors
        },
        text_limit=420,
    )

    assert len(rendered) <= 420
    assert "more conversations not shown" in rendered
    assert "Conversation 19" not in rendered


def test_render_codex_threads_groups_by_project_and_marks_anchor_status() -> None:
    rendered = render_codex_threads(
        [
            CodexThreadGroup(
                project="/agent/project-a",
                threads=[
                    CodexThread(
                        thread_id="codex-1",
                        cwd="/agent/project-a",
                        title="Fix CI",
                        preview="Fix failing tests",
                        status="idle",
                        created_at=1710000000,
                        updated_at=1710000300,
                        model_provider="openai",
                        anchor_status="focused",
                    )
                ],
            )
        ]
    )

    assert "<b>Project</b> project-a" in rendered
    assert "codex-1" in rendered
    assert "focused" in rendered
    assert "Fix CI" in rendered
    assert "<b>loaded</b>" in rendered
    assert "/attach_thread --connection primary codex-1" in rendered


def test_telegram_bot_commands_match_supported_command_surface() -> None:
    commands = telegram_bot_commands()
    names = [command.command for command in commands]

    assert names == [
        "new",
        "threads",
        "goal",
        "plan",
        "implement",
        "attach_thread",
        "focus",
        "to",
        "current",
        "history",
        "overview",
        "help",
        "usage",
        "interrupt",
        "resetparams",
        "profile",
        "model",
        "effort",
        "summary",
        "dir",
        "project",
        "skills",
        "skill",
        "mcp",
        "webhooks",
        "webhook",
        "realtime",
        "verbosity",
        "command_verbosity",
        "followup_mode",
        "fast",
    ]
    assert all(command.description for command in commands)


def test_render_skills_groups_by_scope_and_shows_shortcuts() -> None:
    rendered = render_skills(
        [
            SkillCatalog(
                cwd="/agent/app",
                skills=(
                    SkillCapability(
                        name="codex-telegram-webhooks",
                        path="/root/.codex/skills/codex-telegram-webhooks/SKILL.md",
                        scope="repo",
                        description="Manage Telegram webhooks.",
                        enabled=True,
                    ),
                    SkillCapability(
                        name="ask-user-when-uncertain",
                        path="/root/.codex/skills/ask-user-when-uncertain/SKILL.md",
                        scope="user",
                        description="Ask the user when blocked.",
                        enabled=True,
                    ),
                ),
            )
        ]
    )

    assert "<b>Skills</b>" in rendered
    assert "<b>repo</b>" in rendered
    assert "/skill_codex_telegram_webhooks" in rendered
    assert "Manage Telegram webhooks." in rendered
    assert "<b>user</b>" in rendered
    assert "/skill_ask_user_when_uncertain" in rendered


def test_render_mcp_servers_shows_summary_tools_and_resources() -> None:
    servers = [
        McpServerCapability(
            name="filesystem",
            auth_status="unsupported",
            tools=(
                McpToolCapability(
                    name="read_file",
                    title="Read file",
                    description="Read a file.",
                ),
            ),
            resources=(
                McpResourceCapability(
                    name="repo",
                    uri="file:///agent",
                    title="Repo",
                    description="Workspace.",
                ),
            ),
            resource_templates=(
                McpResourceCapability(
                    name="tab",
                    uri="browser://tabs/{id}",
                    title="Tab",
                    description=None,
                ),
            ),
        )
    ]

    assert "filesystem" in render_mcp_servers(servers)
    assert "1 tool" in render_mcp_servers(servers)
    assert "read_file" in render_mcp_servers(servers, view="tools")
    assert "file:///agent" in render_mcp_servers(servers, view="resources")
    assert "browser://tabs/{id}" in render_mcp_servers(servers, view="resources")


def test_render_mcp_servers_sanitizes_tool_descriptions() -> None:
    servers = [
        McpServerCapability(
            name="filesystem",
            auth_status="unsupported",
            tools=(
                McpToolCapability(
                    name="read_file",
                    description="<b>Read</b> &amp; return<br>raw HTML.",
                ),
            ),
        )
    ]

    rendered = render_mcp_servers(servers, view="tools")

    assert "Read &amp; return raw HTML." in rendered
    assert "&lt;b&gt;" not in rendered
    assert "&lt;br&gt;" not in rendered


def test_render_mcp_servers_tools_view_stays_under_telegram_text_limit() -> None:
    servers = [
        McpServerCapability(
            name="large",
            auth_status="unsupported",
            tools=tuple(
                McpToolCapability(
                    name=f"tool_{index:03d}",
                    description=" ".join(["Detailed tool description"] * 8),
                )
                for index in range(150)
            ),
        )
    ]

    rendered = render_mcp_servers(servers, view="tools")

    assert len(rendered) <= TELEGRAM_MESSAGE_TEXT_LIMIT
    assert "more tools not shown" in rendered


def test_render_goal_status_includes_budget_usage_and_timestamps() -> None:
    rendered = render_goal_status(
        CodexGoal(
            objective="Ship <goal> command",
            status="paused",
            token_budget=1000,
            tokens_used=250,
            elapsed_seconds=12.5,
            created_at="1778489333",
            updated_at="2026-05-06T00:01:00Z",
        )
    )

    assert "<b>Objective</b> Ship &lt;goal&gt; command" in rendered
    assert "<b>Status</b> paused" in rendered
    assert "<b>Tokens</b> 250 / 1000" in rendered
    assert "<b>Elapsed</b> 12.5s" in rendered
    assert "<b>Created</b> 2026-05-11 08:48:53 UTC" in rendered
    assert "<b>Updated</b> 2026-05-06 00:01:00 UTC" in rendered


def test_render_goal_status_handles_empty_goal() -> None:
    assert render_goal_status(None) == "<b>Goal</b>\nNo active goal."


def test_render_approval_request_uses_html_labels_and_escapes_values() -> None:
    rendered = render_approval_request(
        PendingApproval(
            request_id=7,
            chat_key="chat:1",
            logical_thread_id="thread-1",
            codex_thread_id="codex-1",
            turn_id="turn-1",
            method="item/commandExecution/requestApproval",
            command="uv run pytest <all>",
            reason="Run tests & checks",
            message="Guardian <ok>",
        )
    )

    assert rendered == (
        "⚠️ <b>Codex needs approval</b>\n"
        "<b>Command</b> <code>uv run pytest &lt;all&gt;</code>\n"
        "<b>Reason</b> Run tests &amp; checks\n"
        "<b>Guardian</b> Guardian &lt;ok&gt;"
    )


def test_render_user_input_request_uses_html_labels_and_list_structure() -> None:
    rendered = render_user_input_request(
        PendingUserInput(
            request_id=9,
            chat_key="chat:1",
            logical_thread_id="thread-1",
            codex_thread_id="codex-1",
            turn_id="turn-1",
            method="request_user_input",
            questions=(
                UserInputQuestion(
                    question_id="scope",
                    header="Scope <type>",
                    question="Which scope & why?",
                    options=(
                        UserInputOption(
                            label="Native first",
                            description="Use <native> path.",
                        ),
                    ),
                ),
            ),
        )
    )

    assert rendered == (
        "⚠️ <b>Codex needs your input</b>\n"
        "<b>Scope &lt;type&gt;</b> Which scope &amp; why?\n"
        "• <b>Native first</b> Use &lt;native&gt; path."
    )


def test_render_usage_shows_unavailable_account_limits_and_latest_tokens() -> None:
    rendered = render_usage(
        UsageState(
            conversation_name="Build <status>",
            backend_id="primary",
            codex_thread_attached=True,
            token_usage={
                "input_tokens": 1200,
                "cached_input_tokens": 400,
                "output_tokens": 30,
                "reasoning_output_tokens": 10,
                "total_tokens": 1230,
            },
            account=AccountUsage(
                status="unavailable",
                reason="app-server does not expose account limits",
            ),
        )
    )

    assert rendered.startswith("<b>Usage</b>\n")
    assert "<b>Conversation</b> Build &lt;status&gt;" in rendered
    assert "<b>Connection</b> <code>primary</code>" in rendered
    assert "<b>Codex thread</b> attached" in rendered
    assert "<b>Account limits</b>" in rendered
    assert "<b>Unavailable</b> app-server does not expose account limits" in rendered
    assert "<b>Latest turn</b>" in rendered
    assert "<b>Total</b> 1,230" in rendered
    assert "<b>Input</b> 1,200" in rendered
    assert "<b>Cached</b> 400" in rendered
    assert "<b>Output</b> 30" in rendered
    assert "<b>Reasoning</b> 10" in rendered


def test_render_usage_shows_account_rate_limits_and_runtime_metrics() -> None:
    rendered = render_usage(
        UsageState(
            conversation_name="Build status",
            backend_id="primary",
            codex_thread_attached=True,
            token_usage={"total_tokens": 57},
            runtime_metrics=RuntimeUsageMetrics(
                total_token_usage={
                    "input_tokens": 120,
                    "cached_input_tokens": 40,
                    "output_tokens": 30,
                    "reasoning_output_tokens": 10,
                    "total_tokens": 160,
                },
                last_token_usage={"total_tokens": 57},
                model_context_window=258400,
            ),
            account=AccountUsage(
                status="available",
                reason="prolite",
                limits=(
                    AccountUsageLimit(
                        label="5h limit",
                        used_percent=22.5,
                        window_minutes=300,
                        resets_at="2026-05-07T16:31:48Z",
                    ),
                    AccountUsageLimit(
                        label="Weekly limit",
                        used_percent=53,
                        window_minutes=10080,
                        resets_at="2026-05-12T01:29:13Z",
                    ),
                ),
            ),
        )
    )

    assert "<b>Account limits</b>" in rendered
    assert "<b>5h limit</b> 22.5% used" in rendered
    assert "<b>Resets</b> 2026-05-07 16:31:48 UTC" in rendered
    assert "<b>Weekly limit</b> 53% used" in rendered
    assert "<b>Runtime totals</b>" in rendered
    assert "<b>Total</b> 160" in rendered
    assert "<b>Input</b> 120" in rendered
    assert "<b>Runtime last turn</b>" in rendered
    assert "<b>Total</b> 57" in rendered
    assert "<b>Context window</b> 258,400" in rendered


def test_render_usage_handles_unattached_thread_without_token_usage() -> None:
    rendered = render_usage(
        UsageState(
            conversation_name="New conversation",
            backend_id="primary",
            codex_thread_attached=False,
            token_usage=None,
            runtime_metrics=None,
            account=AccountUsage(
                status="unavailable",
                reason="app-server does not expose account limits",
            ),
        )
    )

    assert "<b>Codex thread</b> unattached" in rendered
    assert "<b>Latest turn</b>" in rendered
    assert "No token usage recorded for this conversation yet." in rendered


def test_render_status_includes_collaboration_mode() -> None:
    rendered = render_status(
        "thread-1",
        EffectiveSettings(
            profile="operator",
            model="gpt-5.4",
            model_provider="openai",
            effort="medium",
            summary="concise",
            cwd="/agent",
            fast_mode=False,
            verbosity="verbose",
            command_verbosity="errors",
            followup_mode="steer",
            overrides={"collaboration_mode": "plan"},
            collaboration_mode="plan",
        ),
        pending=None,
    )

    assert "<b>Mode</b> plan" in rendered


def test_render_webhooks_lists_bound_threads() -> None:
    rendered = render_webhooks(
        [
            WebhookSubscription(
                webhook_id="wh_123",
                chat_key="chat:1",
                anchor_id="anchor-1",
                codex_backend_id="primary",
                codex_thread_id="codex-1",
                latest_bridge_id="bridge-1",
                name="front-door",
                enabled=True,
                created_at="now",
                updated_at="now",
                trigger_count=2,
                last_triggered_at="later",
            )
        ]
    )

    assert "<b>Webhook subscriptions</b>" in rendered
    assert "front-door" in rendered
    assert "wh_123" not in rendered
    assert "anchor-1" not in rendered
    assert "codex-1" not in rendered
    assert "2 triggers" in rendered


def test_render_webhook_created_shows_secret_once_and_event_url() -> None:
    rendered = render_webhook_created(
        WebhookSubscription(
            webhook_id="wh_123",
            chat_key="chat:1",
            anchor_id="anchor-1",
            codex_backend_id="primary",
            codex_thread_id="codex-1",
            latest_bridge_id="bridge-1",
            name="front-door",
            enabled=True,
            created_at="now",
            updated_at="now",
            trigger_count=0,
            last_triggered_at=None,
        ),
        event_secret="event-secret",
        event_url="https://codex.example/events/wh_123",
    )

    assert "<b>Created webhook</b> front-door" in rendered
    assert "https://codex.example/events/wh_123" in rendered
    assert "event-secret" in rendered
    assert "shown once" in rendered


def test_build_chat_key_respects_topic_setting() -> None:
    assert build_chat_key(7, None, enable_topic_sessions=False) == "chat:7"
    assert build_chat_key(7, 11, enable_topic_sessions=False) == "chat:7"
    assert build_chat_key(7, 11, enable_topic_sessions=True) == "chat:7:11"


def test_render_current_thread_includes_binding_and_pending_state() -> None:
    rendered = render_current_thread(
        CurrentThreadState(
            thread=LogicalThread(
                thread_id="thread-1",
                chat_key="chat:1",
                title="Thread",
                codex_thread_id="codex-1",
                created_at="now",
                updated_at="now",
                turn_count=3,
                awaiting_reply=True,
                interrupted_notice=False,
                pending_turn_id="turn-1",
            ),
            settings=EffectiveSettings(
                profile="operator",
                model="gpt-5.4",
                model_provider="openai",
                effort="medium",
                summary="concise",
                cwd="/agent",
                fast_mode=False,
                verbosity="verbose",
                command_verbosity="errors",
                followup_mode="steer",
                overrides={},
            ),
            pending=None,
        )
    )

    assert "<b>Conversation</b> Thread" in rendered
    assert "<b>Connection</b> primary" in rendered
    assert "<b>Status</b> running" in rendered
    assert "thread-1" not in rendered
    assert "codex-1" not in rendered
    assert "turn-1" not in rendered


def test_render_history_shows_compact_transcript_labels() -> None:
    rendered = render_history(
        ThreadHistory(
            thread=LogicalThread(
                thread_id="thread-1",
                chat_key="chat:1",
                title="Thread",
                codex_thread_id=None,
                created_at="now",
                updated_at="now",
                turn_count=2,
                awaiting_reply=False,
                interrupted_notice=False,
                pending_turn_id=None,
            ),
            entries=[
                ThreadMessage(
                    message_id=1,
                    thread_id="thread-1",
                    role="user",
                    kind="prompt",
                    text="hello",
                    created_at="now",
                ),
                ThreadMessage(
                    message_id=2,
                    thread_id="thread-1",
                    role="assistant",
                    kind="final",
                    text="world",
                    created_at="now",
                ),
            ],
        )
    )

    assert "<b>User</b> hello" in rendered
    assert "<b>Assistant</b> world" in rendered


def test_render_directory_state_shows_numbered_history() -> None:
    rendered = render_directory_state(
        DirectoryState(
            thread=LogicalThread(
                thread_id="thread-1",
                chat_key="chat:1",
                title="Thread",
                codex_thread_id=None,
                created_at="now",
                updated_at="now",
                turn_count=0,
                awaiting_reply=False,
                interrupted_notice=False,
                pending_turn_id=None,
            ),
            current_path="/tmp/current",
            history=[
                DirectoryEntry(
                    entry_id=1,
                    thread_id="thread-1",
                    path="/tmp/current",
                    selected_at="now",
                ),
                DirectoryEntry(
                    entry_id=2,
                    thread_id="thread-1",
                    path="/tmp/previous",
                    selected_at="now",
                ),
            ],
        )
    )

    assert "• <b>1.</b> <code>/tmp/current</code> (current)" in rendered
    assert "• <b>2.</b> <code>/tmp/previous</code>" in rendered


def test_render_project_state_shows_active_binding_and_project_config() -> None:
    rendered = render_project_state(
        ProjectState(
            thread=LogicalThread(
                thread_id="thread-1",
                chat_key="chat:1",
                title="Thread",
                codex_thread_id=None,
                created_at="now",
                updated_at="now",
                turn_count=0,
                awaiting_reply=False,
                interrupted_notice=False,
                pending_turn_id=None,
            ),
            active=Project(
                project_id="project-1",
                connection_id="laptop",
                root_path="/tmp/current",
                label="current",
                created_at="now",
                updated_at="now",
            ),
            catalog=[
                Project(
                    project_id="project-1",
                    connection_id="laptop",
                    root_path="/tmp/current",
                    label="current",
                    created_at="now",
                    updated_at="now",
                ),
                Project(
                    project_id="project-2",
                    connection_id="home",
                    root_path="/tmp/other",
                    label="other",
                    created_at="now",
                    updated_at="now",
                ),
            ],
            project_overrides=SessionOverrides(
                model="gpt-5.4-mini",
                effort="high",
                fast_mode=True,
            ),
        )
    )

    assert (
        "<b>Active project</b> laptop:current -&gt; <code>/tmp/current</code>"
        in rendered
    )
    assert "<b>Root</b> <code>/tmp/current</code>" in rendered
    assert "<b>Connection</b> laptop" in rendered
    assert "• <b>Model</b> <code>gpt-5.4-mini</code>" in rendered
    assert "• <b>Effort</b> high" in rendered
    assert "• <b>Fast mode</b> on" in rendered
    assert "Known projects" not in rendered


def test_render_help_reuses_published_command_catalog() -> None:
    rendered = render_help()

    assert "• <code>/new</code> - Start a new conversation" in rendered
    assert "• <code>/help</code> - Show supported bot commands" in rendered
    assert "• <code>/dir</code> - Show or change the working directory" in rendered
    assert "• <code>/project</code> - Show the active project and config" in rendered
    assert "<code>/status</code>" not in rendered
