"""Core orchestration for Telegram-facing Codex sessions."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field, replace

from codex_telegram.application.approvals import ApprovalService
from codex_telegram.application.conversations import (
    ConversationLifecycleConfig,
    ConversationLifecycleService,
    DefaultProjectConfig,
    ThreadSelectionResult,
    project_label,
    short_text,
)
from codex_telegram.application.models import (
    AccountUsage,
    BackendConnection,
    CodexRuntimeState,
    CodexThreadBackendFailure,
    ConversationAttachment,
    CodexThreadGroup,
    CodexThreadListResult,
    CurrentThreadState,
    DirectoryState,
    EffectiveSettings,
    FocusFinalMessage,
    McpServerCapability,
    ProjectState,
    RealtimeStartResult,
    SkillCapability,
    SkillCatalog,
    ThreadHistory,
    UsageState,
)
from codex_telegram.application.ports import (
    CodexBackend,
    CodexBackendError,
    DirectoryResolver,
    StateRepository,
    TurnStateChangeHandler,
    TurnUpdateHandler,
    WaitNoticeHandler,
)
from codex_telegram.application.settings import (
    OverrideFieldName,
    ProjectAccessRule,
    RuntimeSettingsPolicy,
    RuntimeSettingsService,
)
from codex_telegram.application.turn_stream import (
    TurnStreamConfig,
    TurnStreamService,
    backend_failure_message,
)
from codex_telegram.application.webhooks import WebhookService
from codex_telegram.domain import (
    AttachmentJob,
    BridgeControlJob,
    BridgeSnapshot,
    BridgeThread,
    CodexThread,
    CodexGoal,
    ConversationAnchor,
    LogicalThread,
    PendingApproval,
    PendingUserInput,
    Project,
    ProfileDefinition,
    RealtimeEvent,
    RealtimeSession,
    SessionOverrides,
    ThreadMessage,
    TurnResult,
    UserTurnInput,
    UserTurnSkill,
    WebhookEventDispatch,
    WebhookSubscription,
    WebhookSubscriptionCreated,
)

CODEX_THREAD_PROJECT_LIMIT_PER_BACKEND = 5


@dataclass(frozen=True, slots=True)
class BotServiceConfig:
    """Resolved application policy and timing."""

    default_profile: str
    client_default_profiles: Mapping[str, str]
    profiles: Mapping[str, ProfileDefinition]
    turn_poll_seconds: float
    wait_notice_seconds: float
    bridge_window_ttl_seconds: float = 900.0
    focus_timeout_seconds: float | None = None
    active_waiting_ttl_seconds: float | None = None
    default_project: DefaultProjectConfig | None = None
    client_allowed_projects: Mapping[str, tuple[ProjectAccessRule, ...]] = field(
        default_factory=dict
    )


@dataclass(frozen=True, slots=True)
class TurnRunResult:
    result: TurnResult | None
    remapped: bool
    remap_warning: str | None
    active_turn_continues: bool = False
    active_turn_notice: str | None = None


class BotService:
    """Application orchestration for chats and app-server turns."""

    def __init__(
        self,
        config: BotServiceConfig,
        repository: StateRepository,
        client: CodexBackend,
        directory_resolver: DirectoryResolver | None = None,
    ) -> None:
        self._config = config
        self._repository = repository
        self._client = client
        self._directory_resolver = directory_resolver
        focus_timeout_seconds = (
            config.focus_timeout_seconds
            if config.focus_timeout_seconds is not None
            else config.bridge_window_ttl_seconds
        )
        active_waiting_ttl_seconds = (
            config.active_waiting_ttl_seconds
            if config.active_waiting_ttl_seconds is not None
            else config.bridge_window_ttl_seconds
        )
        self._approvals = ApprovalService(repository, client)
        self._settings = RuntimeSettingsService(
            RuntimeSettingsPolicy(
                default_profile=config.default_profile,
                client_default_profiles=config.client_default_profiles,
                client_allowed_projects=config.client_allowed_projects,
                profiles=config.profiles,
            ),
            repository,
            client,
        )
        self._conversations = ConversationLifecycleService(
            ConversationLifecycleConfig(
                focus_timeout_seconds=focus_timeout_seconds,
                active_waiting_ttl_seconds=active_waiting_ttl_seconds,
                default_project=config.default_project,
            ),
            repository,
            client,
            self._settings,
        )
        self._turn_stream = TurnStreamService(
            TurnStreamConfig(
                turn_poll_seconds=config.turn_poll_seconds,
                wait_notice_seconds=config.wait_notice_seconds,
            ),
            repository,
            client,
        )
        self._webhooks = WebhookService(
            repository,
            client,
            self.ensure_focused_bridge,
        )
        self._realtime_sessions: dict[str, RealtimeSession] = {}

    @property
    def config(self) -> BotServiceConfig:
        """Return immutable runtime policy."""
        return self._config

    async def initialize(self) -> None:
        """Initialize durable state and mark interrupted turns."""
        await self._repository.initialize()
        await self._repository.mark_waiting_threads_interrupted()
        await self._client.async_healthcheck()

    async def ensure_active_thread(self, chat_key: str) -> LogicalThread:
        """Return the focused bridge using the thread-facing read model."""
        return await self._conversations.ensure_active_thread(chat_key)

    async def ensure_focused_bridge(self, chat_key: str) -> BridgeThread:
        """Return the focused bridge, creating or reviving one if needed."""
        return await self._conversations.ensure_focused_bridge(chat_key)

    async def new_thread(
        self,
        chat_key: str,
        *,
        connection_name: str | None = None,
        project_selector: str | None = None,
    ) -> LogicalThread:
        """Create and focus a new unanchored bridge window."""
        return await self._conversations.new_thread(
            chat_key,
            connection_name=connection_name,
            project_selector=project_selector,
        )

    async def new_thread_in_project(
        self,
        chat_key: str,
        project_id: str,
    ) -> LogicalThread:
        """Create and focus a new bridge window bound to a specific Project."""
        return await self._conversations.new_thread_in_project(chat_key, project_id)

    def has_default_project(self) -> bool:
        """Return whether a New -> Default Project is configured."""
        return self._conversations.has_default_project()

    async def new_thread_in_default_project(self, chat_key: str) -> LogicalThread:
        """Create and focus a new bridge window bound to the configured default Project."""
        return await self._conversations.new_thread_in_default_project(chat_key)

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
        return await self._conversations.attach_codex_thread(
            chat_key,
            codex_thread_id,
            backend_id=backend_id,
            backend_name=backend_name,
            focus=focus,
        )

    async def list_codex_threads(
        self,
        chat_key: str,
        *,
        backend_id: str | None = None,
        backend_name: str | None = None,
        include_all: bool = False,
        project_id: str | None = None,
        search: str | None = None,
        limit: int = 50,
    ) -> CodexThreadListResult:
        """Return Codex backend threads grouped by project for a Telegram chat."""
        await self._repository.ensure_chat(chat_key)
        project = (
            await self._repository.get_project(project_id)
            if project_id is not None
            else None
        )
        if project is not None:
            self._settings.validate_project_access(chat_key, project)
        codex_threads = await self._client.list_codex_threads(
            search=search,
            limit=limit,
            backend_id=project.connection_id if project is not None else backend_id,
            backend_name=backend_name,
            include_all=include_all,
        )
        if project is not None:
            codex_threads = [
                thread for thread in codex_threads if thread.cwd == project.root_path
            ]
        elif self._project_access_is_restricted(chat_key):
            codex_threads = [
                thread
                for thread in codex_threads
                if thread.cwd
                and self._settings.project_is_allowed(
                    chat_key,
                    Project(
                        project_id="",
                        connection_id=thread.codex_backend_id,
                        root_path=thread.cwd,
                        label=project_label(thread.cwd),
                        created_at="",
                        updated_at="",
                    ),
                )
            ]
        failures = _listing_failures(self._client)
        anchors = await self._repository.list_conversation_anchors(chat_key)
        focused_bridge = await self._repository.get_focused_bridge(chat_key)
        pending_approval = await self._repository.get_pending_request(chat_key)
        pending_input = await self._repository.get_pending_user_input(chat_key)
        anchor_statuses: dict[tuple[str, str], str] = {}
        for anchor in anchors:
            bridge = (
                await self._repository.get_bridge(anchor.latest_bridge_id)
                if anchor.latest_bridge_id
                else None
            )
            status = "linked"
            if bridge is not None and (
                (
                    pending_approval is not None
                    and pending_approval.logical_thread_id == bridge.bridge_id
                )
                or (
                    pending_input is not None
                    and pending_input.logical_thread_id == bridge.bridge_id
                )
            ):
                status = "needs-you"
            elif bridge is not None and (
                bridge.awaiting_reply or bridge.pending_turn_id is not None
            ):
                status = "running"
            elif (
                focused_bridge is not None
                and anchor.latest_bridge_id == focused_bridge.bridge_id
            ):
                status = "focused"
            anchor_statuses[(anchor.codex_backend_id, anchor.codex_thread_id)] = status
        enriched = [
            replace(
                thread,
                anchor_status=anchor_statuses.get(
                    (thread.codex_backend_id, thread.thread_id),
                    "unlinked",
                ),
            )
            for thread in codex_threads
        ]
        for thread in enriched:
            if thread.cwd:
                await self._repository.upsert_project(
                    connection_id=thread.codex_backend_id,
                    root_path=thread.cwd,
                    label=project_label(thread.cwd),
                )
        return CodexThreadListResult(
            groups=_group_codex_threads(enriched),
            failures=failures,
        )

    async def list_recent_codex_threads(
        self,
        chat_key: str,
        *,
        limit: int = 5,
    ) -> CodexThreadListResult:
        """Return the most recently updated Codex threads across every backend."""
        listing = await self.list_codex_threads(
            chat_key,
            include_all=True,
            limit=50,
        )
        threads = [thread for group in listing.groups for thread in group.threads]
        recent = sorted(threads, key=lambda thread: -thread.updated_at)[:limit]
        return CodexThreadListResult(
            groups=_group_codex_threads(recent),
            failures=listing.failures,
        )

    async def list_backend_connections(self) -> list[BackendConnection]:
        """Return configured Codex backend connections."""
        return await self._client.list_backend_connections()

    async def usage_state(self, chat_key: str) -> UsageState:
        """Return usage state for the focused conversation."""
        thread = await self.ensure_active_thread(chat_key)
        token_usage: dict[str, int] | None = None
        runtime_metrics = None
        if thread.codex_thread_id is not None:
            runtime = self._client.get_runtime_state(
                thread.codex_thread_id,
                codex_backend_id=thread.codex_backend_id,
            )
            token_usage = runtime.token_usage
            runtime_metrics = runtime.usage_metrics
        account = await self._client.get_usage(codex_backend_id=thread.codex_backend_id)
        if not isinstance(account, AccountUsage):
            account = AccountUsage(
                status="unavailable",
                reason="app-server did not return account usage",
            )
        return UsageState(
            conversation_name=thread.title,
            backend_id=thread.codex_backend_id,
            codex_thread_attached=thread.codex_thread_id is not None,
            token_usage=token_usage,
            account=account,
            runtime_metrics=runtime_metrics,
        )

    async def list_skills(
        self,
        chat_key: str,
        *,
        search: str | None = None,
        force_reload: bool = False,
    ) -> list[SkillCatalog]:
        """Return skills for the focused conversation's backend and cwd."""
        thread = await self.ensure_active_thread(chat_key)
        settings = await self.get_settings(thread.thread_id, chat_key)
        cwd = settings.cwd or self._default_project_cwd(thread.codex_backend_id)
        if not cwd and self._directory_resolver is not None:
            cwd = await self._directory_resolver.default_base_path()
        catalogs = await self._client.list_skills(
            cwd=cwd or None,
            force_reload=force_reload,
            codex_backend_id=thread.codex_backend_id,
        )
        needle = search.strip().casefold() if search and search.strip() else None
        if needle is None:
            return catalogs
        return [
            replace(
                catalog,
                skills=tuple(
                    skill
                    for skill in catalog.skills
                    if needle in skill.name.casefold()
                    or needle in skill.description.casefold()
                    or (
                        skill.short_description is not None
                        and needle in skill.short_description.casefold()
                    )
                ),
            )
            for catalog in catalogs
        ]

    async def list_mcp_servers(self, chat_key: str) -> list[McpServerCapability]:
        """Return MCP inventory for the focused conversation's backend."""
        thread = await self.ensure_active_thread(chat_key)
        return await self._client.list_mcp_servers(
            codex_backend_id=thread.codex_backend_id,
        )

    async def run_skill_turn(
        self,
        chat_key: str,
        selector: str,
        prompt: str,
        *,
        thread_id: str | None = None,
        on_update: TurnUpdateHandler | None = None,
        on_wait_notice: WaitNoticeHandler | None = None,
        on_state_change: TurnStateChangeHandler | None = None,
    ) -> TurnRunResult:
        """Run a normal Codex turn with one resolved skill attachment."""
        skill = await self._resolve_skill(chat_key, selector)
        if not skill.enabled:
            raise ValueError(f"Skill is disabled: {skill.name}")
        return await self.run_turn(
            chat_key,
            UserTurnInput(
                text=prompt,
                skills=(UserTurnSkill(name=skill.name, path=skill.path),),
            ),
            thread_id=thread_id,
            on_update=on_update,
            on_wait_notice=on_wait_notice,
            on_state_change=on_state_change,
        )

    async def list_recent_projects(
        self,
        *,
        chat_key: str,
        connection_id: str | None = None,
        include_all: bool = False,
        limit: int = 5,
    ) -> list[Project]:
        """Return recent known Projects for a picker."""
        repository_limit = (
            100 if self._project_access_is_restricted(chat_key) else limit
        )
        projects = await self._repository.list_projects(
            connection_id=None if include_all else connection_id,
            limit=repository_limit,
        )
        return self._settings.filter_projects(chat_key, projects)[:limit]

    async def _resolve_skill(self, chat_key: str, selector: str) -> SkillCapability:
        normalized = selector.strip()
        if not normalized:
            raise ValueError("Usage: /skill <name-or-shortcut> <prompt>")
        skills = [
            skill
            for catalog in await self.list_skills(chat_key)
            for skill in catalog.skills
        ]
        exact = [
            skill for skill in skills if skill.name.casefold() == normalized.casefold()
        ]
        if len(exact) == 1:
            return exact[0]
        slug_matches = [
            skill
            for skill in skills
            if _skill_slug(skill.name) == normalized.casefold()
        ]
        if len(slug_matches) == 1:
            return slug_matches[0]
        if len(slug_matches) > 1:
            names = ", ".join(skill.name for skill in slug_matches)
            raise ValueError(
                f"Ambiguous skill shortcut: {selector}. Use one of: {names}"
            )
        raise ValueError(f"Unknown skill: {selector}")

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
        return await self._webhooks.create_webhook_subscription(
            chat_key=chat_key,
            anchor_id=anchor_id,
            codex_backend_id=codex_backend_id,
            codex_thread_id=codex_thread_id,
            name=name,
        )

    async def list_webhook_subscriptions(
        self,
        *,
        chat_key: str | None = None,
        anchor_id: str | None = None,
        include_disabled: bool = False,
    ) -> list[WebhookSubscription]:
        """List durable webhook subscriptions."""
        return await self._webhooks.list_webhook_subscriptions(
            chat_key=chat_key,
            anchor_id=anchor_id,
            include_disabled=include_disabled,
        )

    async def revoke_webhook_subscription(
        self,
        selector: str,
        *,
        chat_key: str | None = None,
    ) -> bool:
        """Disable one webhook by id, or by name when scoped to a chat."""
        return await self._webhooks.revoke_webhook_subscription(
            selector,
            chat_key=chat_key,
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
        return await self._webhooks.accept_webhook_event(
            webhook_id,
            event_secret,
            payload,
            idempotency_key=idempotency_key,
        )

    async def enqueue_attachment_job(
        self,
        thread_id: str,
        path: str,
        *,
        caption: str | None = None,
    ) -> AttachmentJob:
        """Queue one outbound attachment for an existing logical thread."""
        return await self._repository.enqueue_attachment_job(
            thread_id,
            path,
            caption=caption,
        )

    async def bridge_snapshot(self, thread_id: str) -> BridgeSnapshot:
        """Return runtime-facing status for one bridge window."""
        thread = await self._repository.get_thread(thread_id)
        if thread is None:
            raise ValueError(f"Unknown bridge: {thread_id}")
        active = thread.closed_at is None and not self._conversations.bridge_is_expired(
            thread
        )
        return BridgeSnapshot(
            logical_thread_id=thread.thread_id,
            chat_key=thread.chat_key,
            title=thread.title,
            anchor_id=thread.anchor_id,
            codex_backend_id=thread.codex_backend_id,
            codex_thread_id=thread.codex_thread_id,
            active=active,
            awaiting_reply=thread.awaiting_reply,
            pending_turn_id=thread.pending_turn_id,
            expires_at=thread.expires_at,
            closed_at=thread.closed_at,
        )

    async def enqueue_bridge_control_job(
        self,
        thread_id: str,
        kind: str,
        payload: dict[str, object],
    ) -> BridgeControlJob:
        """Queue one Telegram bridge-control side effect."""
        snapshot = await self.bridge_snapshot(thread_id)
        if not snapshot.active:
            raise ValueError(
                "Bridge is expired. Use anchor_id for durable flows, or refresh/focus "
                "the conversation from Telegram before bridge control."
            )
        if kind not in {"notify", "refresh_status_card"}:
            raise ValueError(f"Unsupported bridge control action: {kind}")
        return await self._repository.enqueue_bridge_control_job(
            thread_id,
            kind,
            payload,
        )

    async def list_threads(self, chat_key: str) -> list[LogicalThread]:
        """Return all bridge windows for the chat."""
        return await self._conversations.list_threads(chat_key)

    async def list_conversations(self, chat_key: str) -> list[ConversationAnchor]:
        """Return durable conversation anchors for the chat."""
        return await self._conversations.list_conversations(chat_key)

    async def list_interrupted_threads(self) -> list[LogicalThread]:
        """Return threads that were interrupted by a process restart."""
        return await self._conversations.list_interrupted_threads()

    async def expire_idle_bridges(self) -> list[str]:
        """Expire idle presentation windows without changing conversation focus."""
        return await self._conversations.expire_idle_bridges()

    async def focus_bridge(self, chat_key: str, selector: str) -> ThreadSelectionResult:
        """Focus a bridge id or revive an anchor id."""
        return await self._conversations.focus_bridge(chat_key, selector)

    async def resolve_bridge(
        self, chat_key: str, selector: str, *, focus: bool
    ) -> ThreadSelectionResult:
        """Resolve a bridge or anchor selector, optionally focusing it."""
        return await self._conversations.resolve_bridge(chat_key, selector, focus=focus)

    async def run_webhook_turn(
        self,
        subscription: WebhookSubscription,
        prompt: str,
        *,
        on_update: TurnUpdateHandler | None = None,
        on_wait_notice: WaitNoticeHandler | None = None,
        on_state_change: TurnStateChangeHandler | None = None,
    ) -> TurnRunResult:
        """Run an externally triggered event without changing focus."""
        if subscription.anchor_id is None:
            raise ValueError("Webhook is not bound to a Codex conversation anchor.")
        anchor = await self._repository.get_conversation_anchor(subscription.anchor_id)
        if anchor is None:
            raise ValueError("Webhook conversation anchor is missing.")
        bridge: BridgeThread | None = None
        if anchor.latest_bridge_id is not None:
            bridge = await self._repository.get_bridge(anchor.latest_bridge_id)
        if bridge is None or self._conversations.bridge_is_expired(bridge):
            bridge = await self._conversations.revive_anchor(
                anchor.chat_key,
                anchor.anchor_id,
                focus=False,
            )
        return await self.run_turn(
            anchor.chat_key,
            prompt,
            thread_id=bridge.bridge_id,
            on_update=on_update,
            on_wait_notice=on_wait_notice,
            on_state_change=on_state_change,
        )

    async def take_interrupted_notice(self, thread_id: str) -> bool:
        """Return whether the active thread was interrupted by a restart."""
        return await self._repository.take_interrupted_notice(thread_id)

    async def interrupt_active_turn(self, chat_key: str) -> str:
        """Interrupt the active turn for the chat, if one exists."""
        thread = await self._repository.get_active_thread(chat_key)
        if thread is None:
            return "No active thread."
        if (
            not thread.awaiting_reply
            or thread.pending_turn_id is None
            or thread.codex_thread_id is None
        ):
            return "No active turn to interrupt."
        await self._client.interrupt_turn(
            thread.pending_turn_id,
            codex_thread_id=thread.codex_thread_id,
            codex_backend_id=thread.codex_backend_id,
        )
        await self._repository.mark_turn_completed(thread.thread_id)
        return "Interrupt requested."

    async def current_thread_state(self, chat_key: str) -> CurrentThreadState:
        """Return the active thread together with effective settings."""
        thread = await self.ensure_active_thread(chat_key)
        return CurrentThreadState(
            thread=thread,
            settings=await self.get_settings(thread.thread_id, chat_key),
            pending=await self.pending_request_for_chat(chat_key),
            realtime=self._realtime_sessions.get(thread.thread_id),
            runtime=await self.runtime_state_for_thread(thread),
        )

    async def runtime_state_for_thread(
        self,
        thread: LogicalThread | BridgeThread,
    ) -> CodexRuntimeState:
        """Return active runtime state for one bridge window."""
        runtime = CodexRuntimeState()
        if thread.codex_thread_id is not None:
            runtime = self._client.get_runtime_state(
                thread.codex_thread_id,
                codex_backend_id=thread.codex_backend_id,
            )
        return runtime

    async def get_goal(self, chat_key: str) -> CodexGoal | None:
        """Return the current goal for the focused Codex thread."""
        thread = await self.ensure_active_thread(chat_key)
        if thread.codex_thread_id is None:
            return None
        return await self._client.get_thread_goal(
            thread.codex_thread_id,
            codex_backend_id=thread.codex_backend_id,
        )

    async def set_goal(
        self,
        chat_key: str,
        *,
        objective: str,
        token_budget: int | None = None,
        status: str = "active",
        update_token_budget: bool = False,
    ) -> CodexGoal:
        """Set the current goal for the focused Codex thread."""
        if status not in {"active", "paused"}:
            raise ValueError(f"Unsupported goal status: {status}")
        if not objective.strip():
            raise ValueError("Goal objective cannot be empty.")
        thread = await self._ensure_goal_thread(chat_key)
        assert thread.codex_thread_id is not None
        return await self._client.set_thread_goal(
            thread.codex_thread_id,
            objective=objective.strip(),
            token_budget=token_budget,
            status=status,
            update_token_budget=update_token_budget,
            codex_backend_id=thread.codex_backend_id,
        )

    async def update_goal_budget(
        self,
        chat_key: str,
        token_budget: int | None,
    ) -> CodexGoal:
        """Update or clear the budget on the current goal."""
        current = await self.get_goal(chat_key)
        if current is None:
            raise ValueError("No active goal.")
        return await self.set_goal(
            chat_key,
            objective=current.objective,
            token_budget=token_budget,
            status=current.status,
            update_token_budget=True,
        )

    async def update_goal_status(self, chat_key: str, status: str) -> CodexGoal:
        """Pause or resume the current goal."""
        if status not in {"active", "paused"}:
            raise ValueError(f"Unsupported goal status: {status}")
        current = await self.get_goal(chat_key)
        if current is None:
            raise ValueError("No active goal.")
        return await self.set_goal(
            chat_key,
            objective=current.objective,
            token_budget=current.token_budget,
            status=status,
            update_token_budget=current.token_budget is not None,
        )

    async def clear_goal(self, chat_key: str) -> None:
        """Clear the current goal for the focused Codex thread."""
        thread = await self.ensure_active_thread(chat_key)
        if thread.codex_thread_id is None:
            return
        await self._client.clear_thread_goal(
            thread.codex_thread_id,
            codex_backend_id=thread.codex_backend_id,
        )

    async def _ensure_goal_thread(self, chat_key: str) -> LogicalThread:
        thread = await self.ensure_active_thread(chat_key)
        if thread.codex_thread_id is not None:
            return thread
        raw_overrides = await self._repository.get_overrides(thread.thread_id)
        profile = self._settings.resolve_profile(chat_key, raw_overrides)
        overrides = await self._settings.effective_overrides(
            thread.thread_id,
            raw_overrides,
        )
        binding = await self._client.ensure_thread_binding(
            chat_key,
            thread.thread_id,
            thread.codex_thread_id,
            profile,
            overrides,
            codex_backend_id=thread.codex_backend_id,
            anchor_id=thread.anchor_id,
        )
        await self._repository.update_codex_thread_binding(
            thread.thread_id,
            binding.codex_thread_id,
            codex_backend_id=binding.codex_backend_id,
        )
        updated = await self._repository.get_thread(thread.thread_id)
        assert updated is not None
        return updated

    async def realtime_state(self, chat_key: str) -> RealtimeSession | None:
        """Return the active realtime session for one chat, if any."""
        thread = await self.ensure_active_thread(chat_key)
        return self._realtime_sessions.get(thread.thread_id)

    async def thread_history(self, chat_key: str, limit: int = 10) -> ThreadHistory:
        """Return recent transcript entries for the active thread."""
        thread = await self.ensure_active_thread(chat_key)
        entries = await self._repository.list_thread_messages(
            thread.thread_id,
            limit=max(1, min(limit, 50)),
        )
        return ThreadHistory(thread=thread, entries=entries)

    async def thread_final_history(
        self,
        chat_key: str,
        limit: int = 10,
    ) -> ThreadHistory:
        """Return recent assistant finals for the active thread."""
        thread = await self.ensure_active_thread(chat_key)
        entries = await self._repository.list_final_thread_messages(
            thread.thread_id,
            limit=max(1, min(limit, 50)),
        )
        return ThreadHistory(thread=thread, entries=entries)

    async def focus_final_messages(
        self,
        chat_key: str,
        thread_id: str,
        *,
        limit: int = 20,
    ) -> list[FocusFinalMessage]:
        """Return assistant finals that should be delivered after focusing a bridge."""
        thread = await self._repository.get_thread(thread_id)
        if thread is None or thread.chat_key != chat_key or thread.anchor_id is None:
            return []
        undelivered = await self._repository.list_undelivered_final_thread_messages(
            chat_key=chat_key,
            anchor_id=thread.anchor_id,
            thread_id=thread.thread_id,
            limit=limit,
        )
        deliveries = [
            FocusFinalMessage(message=entry, repeated=False) for entry in undelivered
        ]
        latest = await self._repository.get_latest_final_thread_message(
            thread.thread_id
        )
        if latest is None:
            return deliveries
        undelivered_ids = {entry.message_id for entry in undelivered}
        if latest.message_id not in undelivered_ids:
            deliveries.append(FocusFinalMessage(message=latest, repeated=True))
        return deliveries

    async def mark_thread_messages_delivered(
        self,
        chat_key: str,
        thread_id: str,
    ) -> None:
        """Advance this anchor's Telegram delivery watermark."""
        thread = await self._repository.get_thread(thread_id)
        if thread is None or thread.chat_key != chat_key or thread.anchor_id is None:
            return
        await self._repository.mark_thread_messages_delivered(
            chat_key=chat_key,
            anchor_id=thread.anchor_id,
            thread_id=thread.thread_id,
        )

    async def show_directory_state(self, chat_key: str) -> DirectoryState:
        """Return current effective directory and recent selections."""
        thread = await self.ensure_active_thread(chat_key)
        settings = await self.get_settings(thread.thread_id, chat_key)
        history = await self._repository.list_directories(thread.thread_id, limit=10)
        return DirectoryState(
            thread=thread,
            current_path=settings.cwd,
            history=history,
        )

    async def set_directory(self, chat_key: str, raw_path: str) -> DirectoryState:
        """Resolve, validate, and store the next-turn working directory."""
        thread = await self.ensure_active_thread(chat_key)
        resolved = await self._resolve_directory_path(
            raw_path,
            base_path=await self._current_directory_base(chat_key, thread.thread_id),
        )
        await self.update_override(thread.thread_id, "cwd", resolved)
        await self._repository.remember_directory(thread.thread_id, resolved)
        return await self.show_directory_state(chat_key)

    async def switch_previous_directory(self, chat_key: str) -> DirectoryState:
        """Switch back to the previously selected directory."""
        thread = await self.ensure_active_thread(chat_key)
        history = await self._repository.list_directories(thread.thread_id, limit=2)
        if len(history) < 2:
            raise ValueError("No previous directory available.")
        await self.update_override(thread.thread_id, "cwd", history[1].path)
        await self._repository.remember_directory(thread.thread_id, history[1].path)
        return await self.show_directory_state(chat_key)

    async def switch_directory_from_history(
        self,
        chat_key: str,
        index: int,
    ) -> DirectoryState:
        """Select a recent directory by its displayed history index."""
        thread = await self.ensure_active_thread(chat_key)
        history = await self._repository.list_directories(
            thread.thread_id,
            limit=max(index, 10),
        )
        if index < 1 or index > len(history):
            raise ValueError("Unknown directory history entry.")
        selected = history[index - 1]
        await self.update_override(thread.thread_id, "cwd", selected.path)
        await self._repository.remember_directory(thread.thread_id, selected.path)
        return await self.show_directory_state(chat_key)

    async def reset_directory(self, chat_key: str) -> DirectoryState:
        """Clear the explicit working directory override."""
        thread = await self.ensure_active_thread(chat_key)
        await self.update_override(thread.thread_id, "cwd", None)
        return await self.show_directory_state(chat_key)

    async def show_project_state(
        self,
        chat_key: str,
        *,
        connection_name: str | None = None,
        include_all: bool = False,
    ) -> ProjectState:
        """Return the active Project binding and known Project catalog."""
        thread = await self.ensure_active_thread(chat_key)
        connection_id = (
            None
            if include_all
            else self._settings.resolve_connection_id(connection_name)
        )
        return ProjectState(
            thread=thread,
            active=await self._repository.get_thread_project(thread.thread_id),
            catalog=self._settings.filter_projects(
                chat_key,
                await self._repository.list_projects(
                    connection_id=connection_id,
                    limit=100 if self._project_access_is_restricted(chat_key) else 50,
                ),
            ),
            project_overrides=(
                await self._settings.project_overrides_for_thread(thread.thread_id)
            ),
        )

    async def add_project(
        self,
        chat_key: str,
        raw_path: str,
        *,
        connection_name: str | None = None,
        label: str | None = None,
    ) -> ProjectState:
        """Add a Project to the catalog and bind it to the active thread."""
        thread = await self.ensure_active_thread(chat_key)
        resolved = await self._resolve_directory_path(
            raw_path,
            base_path=await self._current_directory_base(chat_key, thread.thread_id),
        )
        connection_id = (
            self._settings.resolve_connection_id(connection_name)
            or thread.codex_backend_id
        )
        self._settings.validate_project_access(
            chat_key,
            Project(
                project_id="",
                connection_id=connection_id,
                root_path=resolved,
                label=label or project_label(resolved),
                created_at="",
                updated_at="",
            ),
        )
        project = await self._repository.upsert_project(
            connection_id=connection_id,
            root_path=resolved,
            label=label or project_label(resolved),
        )
        await self._repository.bind_thread_project(thread.thread_id, project.project_id)
        await self.update_override(thread.thread_id, "cwd", None)
        return await self.show_project_state(chat_key)

    async def use_project(
        self,
        chat_key: str,
        selector: str,
        *,
        connection_name: str | None = None,
    ) -> ProjectState:
        """Select a Project from the catalog."""
        thread = await self.ensure_active_thread(chat_key)
        chosen = await self._settings.select_project(
            chat_key,
            selector,
            connection_id=self._settings.resolve_connection_id(connection_name),
        )
        self._settings.validate_project_access(chat_key, chosen)
        await self._repository.bind_thread_project(thread.thread_id, chosen.project_id)
        await self.update_override(thread.thread_id, "cwd", None)
        return await self.show_project_state(chat_key)

    async def unbind_project(self, chat_key: str) -> ProjectState:
        """Remove the current thread Project binding."""
        thread = await self.ensure_active_thread(chat_key)
        await self._repository.clear_thread_project(thread.thread_id)
        await self.update_override(thread.thread_id, "cwd", None)
        return await self.show_project_state(chat_key)

    async def get_settings(self, thread_id: str, chat_key: str) -> EffectiveSettings:
        """Return effective runtime settings for the active thread."""
        return await self._settings.get_settings(thread_id, chat_key)

    async def clear_overrides(self, thread_id: str) -> EffectiveSettings:
        """Clear all runtime overrides and return effective settings."""
        return await self._settings.clear_overrides(thread_id)

    async def update_override(
        self,
        thread_id: str,
        field_name: OverrideFieldName,
        value: str | None,
    ) -> EffectiveSettings:
        """Update one runtime override and return effective settings."""
        return await self._settings.update_override(thread_id, field_name, value)

    async def set_fast_mode(self, thread_id: str, enabled: bool) -> EffectiveSettings:
        """Update fast mode override."""
        return await self._settings.set_fast_mode(thread_id, enabled)

    async def set_collaboration_mode(
        self,
        chat_key: str,
        mode: str,
    ) -> EffectiveSettings:
        """Set the focused thread's sticky collaboration mode."""
        thread = await self.ensure_active_thread(chat_key)
        return await self.update_override(thread.thread_id, "collaboration_mode", mode)

    def _project_access_is_restricted(self, chat_key: str) -> bool:
        return bool(self._config.client_allowed_projects.get(chat_key))

    async def apply_implementation_trigger_if_needed(
        self,
        chat_key: str,
        text: str,
    ) -> bool:
        """Switch Plan mode to Default for exact implementation triggers."""
        normalized = " ".join(text.casefold().strip().split())
        if normalized not in {
            "implement as planned",
            "implement the plan",
            "implement this plan",
            "complete the implementation",
        }:
            return False
        thread = await self.ensure_active_thread(chat_key)
        settings = await self.get_settings(thread.thread_id, chat_key)
        if settings.collaboration_mode != "plan":
            return False
        await self.set_collaboration_mode(chat_key, "default")
        return True

    async def start_plan_implementation(
        self,
        chat_key: str,
        thread_id: str,
        prompt: str = "Implement as planned.",
        *,
        on_update: TurnUpdateHandler | None = None,
        on_wait_notice: WaitNoticeHandler | None = None,
        on_state_change: TurnStateChangeHandler | None = None,
    ) -> TurnRunResult:
        """Switch one plan thread back to Default mode and start implementation."""
        thread = await self._repository.get_thread(thread_id)
        if thread is None or thread.chat_key != chat_key:
            raise ValueError("Unknown plan conversation.")
        await self.update_override(thread_id, "collaboration_mode", "default")
        resolved_prompt = prompt.strip() or "Implement as planned."
        return await self.run_turn(
            chat_key,
            resolved_prompt,
            thread_id=thread_id,
            on_update=on_update,
            on_wait_notice=on_wait_notice,
            on_state_change=on_state_change,
        )

    async def pending_request_for_chat(self, chat_key: str) -> PendingApproval | None:
        """Return the oldest pending approval for this chat."""
        return await self._approvals.pending_request_for_chat(chat_key)

    async def pending_user_input_for_chat(
        self, chat_key: str
    ) -> PendingUserInput | None:
        """Return the oldest pending user-input request for this chat."""
        return await self._approvals.pending_user_input_for_chat(chat_key)

    async def resolve_pending_request(self, request_id: int, decision: str) -> str:
        """Resolve one pending approval request."""
        return await self._approvals.resolve_pending_request(request_id, decision)

    async def resolve_pending_user_input(
        self,
        request_id: int,
        answers: dict[str, tuple[str, ...]],
    ) -> str:
        """Resolve one pending user-input request."""
        return await self._approvals.resolve_pending_user_input(request_id, answers)

    async def continue_turn(
        self,
        chat_key: str,
        thread_id: str,
        turn_id: str,
        on_update: TurnUpdateHandler | None = None,
        on_wait_notice: WaitNoticeHandler | None = None,
        on_state_change: TurnStateChangeHandler | None = None,
    ) -> TurnResult:
        """Continue watching an already-started turn after approval."""
        return await self._turn_stream.continue_turn(
            chat_key,
            thread_id,
            turn_id,
            on_update=on_update,
            on_wait_notice=on_wait_notice,
            on_state_change=on_state_change,
        )

    async def start_realtime(
        self,
        chat_key: str,
        *,
        thread_id: str | None = None,
    ) -> RealtimeStartResult:
        """Start realtime mode for the focused bridge window."""
        thread = await self._conversations.turn_thread(chat_key, thread_id)
        if thread.thread_id in self._realtime_sessions:
            return RealtimeStartResult(
                session=self._realtime_sessions[thread.thread_id]
            )
        if thread.awaiting_reply and thread.pending_turn_id is not None:
            raise ValueError(
                "A regular Codex turn is still running. Use /interrupt or wait "
                "for it to finish before starting realtime."
            )
        await self._settings.validate_thread_project_connection(thread)
        raw_overrides = await self._repository.get_overrides(thread.thread_id)
        profile = self._settings.resolve_profile(chat_key, raw_overrides)
        overrides = await self._settings.effective_overrides(
            thread.thread_id, raw_overrides
        )
        binding = await self._client.ensure_thread_binding(
            chat_key,
            thread.thread_id,
            thread.codex_thread_id,
            profile,
            overrides,
            codex_backend_id=thread.codex_backend_id,
            anchor_id=thread.anchor_id,
        )
        await self._repository.update_codex_thread_binding(
            thread.thread_id,
            binding.codex_thread_id,
            codex_backend_id=binding.codex_backend_id,
        )
        remap_warning = None
        if binding.remapped and thread.codex_thread_id:
            remap_warning = (
                "Warning: the stored Codex thread could not be resumed, so this "
                "chat was rebound to a fresh backend thread."
            )
        session = await self._client.start_realtime(
            chat_key,
            thread.thread_id,
            binding.codex_thread_id,
            codex_backend_id=binding.codex_backend_id,
        )
        self._realtime_sessions[thread.thread_id] = session
        return RealtimeStartResult(session=session, remap_warning=remap_warning)

    async def stop_realtime(
        self,
        chat_key: str,
        *,
        thread_id: str | None = None,
    ) -> str:
        """Stop realtime mode for the focused bridge window."""
        thread = await self._conversations.turn_thread(chat_key, thread_id)
        session = self._realtime_sessions.pop(thread.thread_id, None)
        if session is None:
            return "Realtime mode stopped."
        try:
            await self._client.stop_realtime(
                session.codex_thread_id,
                codex_backend_id=session.codex_backend_id,
            )
        finally:
            self._realtime_sessions.pop(thread.thread_id, None)
        return "Realtime mode stopped."

    async def route_realtime_input(
        self,
        chat_key: str,
        turn_input: UserTurnInput,
    ) -> bool:
        """Route a user message into realtime mode when active."""
        thread = await self.ensure_active_thread(chat_key)
        session = self._realtime_sessions.get(thread.thread_id)
        if session is None:
            return False
        if turn_input.images:
            raise ValueError("Realtime mode only accepts text and voice notes for now.")
        text = turn_input.text.strip()
        if not text:
            return True
        await self._repository.add_thread_message(
            thread.thread_id,
            role="user",
            kind="realtime",
            text=text,
        )
        await self._client.append_realtime_text(
            session.codex_thread_id,
            text,
            codex_backend_id=session.codex_backend_id,
        )
        return True

    async def wait_for_realtime_event(
        self,
        logical_thread_id: str,
        timeout: float,
    ) -> RealtimeEvent:
        """Wait for the next realtime event for one bridge window."""
        session = self._realtime_sessions.get(logical_thread_id)
        if session is None:
            raise ValueError("Realtime mode is not active for this conversation.")
        event = await self._client.wait_for_realtime_event(
            session.codex_thread_id,
            timeout,
            codex_backend_id=session.codex_backend_id,
        )
        if event.event_type in {"closed", "error"}:
            self._realtime_sessions.pop(logical_thread_id, None)
        return event

    async def run_turn(
        self,
        chat_key: str,
        turn_input: str | UserTurnInput,
        *,
        thread_id: str | None = None,
        on_update: TurnUpdateHandler | None = None,
        on_wait_notice: WaitNoticeHandler | None = None,
        on_state_change: TurnStateChangeHandler | None = None,
    ) -> TurnRunResult:
        """Run one Codex turn for the active thread."""
        resolved_input = _coerce_turn_input(turn_input)
        display_text = resolved_input.display_text()
        thread = await self._conversations.turn_thread(chat_key, thread_id)
        await self._settings.validate_thread_project_connection(thread)
        await self._repository.add_thread_message(
            thread.thread_id,
            role="user",
            kind="prompt",
            text=display_text,
        )
        raw_overrides = await self._repository.get_overrides(thread.thread_id)
        profile = self._settings.resolve_profile(chat_key, raw_overrides)
        if _should_steer_active_turn(thread, raw_overrides, profile):
            assert thread.codex_thread_id is not None
            assert thread.pending_turn_id is not None
            try:
                await self._client.steer_turn(
                    logical_thread_id=thread.thread_id,
                    codex_thread_id=thread.codex_thread_id,
                    codex_backend_id=thread.codex_backend_id,
                    turn_id=thread.pending_turn_id,
                    text=_backend_turn_input(resolved_input),
                )
            except CodexBackendError as err:
                return await self._backend_failure_run_result(
                    chat_key,
                    thread,
                    err,
                    thread.pending_turn_id,
                    on_state_change=on_state_change,
                )
            return TurnRunResult(
                result=None,
                remapped=False,
                remap_warning=None,
                active_turn_continues=True,
                active_turn_notice="Added your follow-up to the active Codex turn.",
            )
        overrides = await self._settings.effective_overrides(
            thread.thread_id, raw_overrides
        )
        await self._repository.update_thread_title_if_empty(
            thread.thread_id, short_text(display_text)
        )
        try:
            binding = await self._client.ensure_thread_binding(
                chat_key,
                thread.thread_id,
                thread.codex_thread_id,
                profile,
                overrides,
                codex_backend_id=thread.codex_backend_id,
                anchor_id=thread.anchor_id,
            )
        except CodexBackendError as err:
            return await self._backend_failure_run_result(
                chat_key,
                thread,
                err,
                None,
                on_state_change=on_state_change,
            )
        remap_warning = None
        if binding.remapped and thread.codex_thread_id:
            remap_warning = (
                "Warning: the stored Codex thread could not be resumed, so this chat "
                "was rebound to a fresh backend thread."
            )
        await self._repository.update_codex_thread_binding(
            thread.thread_id,
            binding.codex_thread_id,
            codex_backend_id=binding.codex_backend_id,
        )
        try:
            accepted = await self._client.start_turn(
                chat_key,
                thread.thread_id,
                binding.codex_thread_id,
                _backend_turn_input(resolved_input),
                profile,
                overrides,
                codex_backend_id=binding.codex_backend_id,
            )
        except CodexBackendError as err:
            return await self._backend_failure_run_result(
                chat_key,
                replace(
                    thread,
                    codex_thread_id=binding.codex_thread_id,
                    codex_backend_id=binding.codex_backend_id,
                ),
                err,
                None,
                on_state_change=on_state_change,
            )
        await self._repository.mark_turn_started(thread.thread_id, accepted.turn_id)
        await _notify_state_change(on_state_change)

        result = await self._turn_stream.complete_turn(
            chat_key,
            thread.thread_id,
            accepted.turn_id,
            accepted.codex_backend_id,
            on_update=on_update,
            on_wait_notice=on_wait_notice,
            on_state_change=on_state_change,
        )
        return TurnRunResult(
            result=result,
            remapped=binding.remapped,
            remap_warning=remap_warning,
        )

    async def _backend_failure_run_result(
        self,
        chat_key: str,
        thread: LogicalThread,
        err: CodexBackendError,
        turn_id: str | None,
        *,
        on_state_change: TurnStateChangeHandler | None,
    ) -> TurnRunResult:
        await self._repository.clear_pending_for_thread(thread.thread_id)
        await self._repository.clear_pending_user_input_for_thread(thread.thread_id)
        await self._repository.mark_turn_failed(thread.thread_id)
        await _notify_state_change(on_state_change)
        result = TurnResult(
            turn_id=turn_id or "",
            chat_key=chat_key,
            logical_thread_id=thread.thread_id,
            codex_thread_id=thread.codex_thread_id or "",
            codex_backend_id=err.backend_id or thread.codex_backend_id,
            status="failed",
            final_text="",
            error=backend_failure_message(err, thread.codex_backend_id),
        )
        await self._turn_stream.persist_terminal_result(thread.thread_id, result)
        return TurnRunResult(result=result, remapped=False, remap_warning=None)

    async def _current_directory_base(self, chat_key: str, thread_id: str) -> str:
        settings = await self.get_settings(thread_id, chat_key)
        if settings.cwd:
            return settings.cwd
        return await self._require_directory_resolver().default_base_path()

    def _default_project_cwd(self, codex_backend_id: str) -> str | None:
        default_project = self._config.default_project
        if (
            default_project is not None
            and default_project.connection == codex_backend_id
        ):
            return default_project.root_path
        return None

    async def _resolve_directory_path(self, raw_path: str, *, base_path: str) -> str:
        return await self._require_directory_resolver().resolve_directory(
            raw_path,
            base_path=base_path,
        )

    def _require_directory_resolver(self) -> DirectoryResolver:
        if self._directory_resolver is None:
            raise RuntimeError("Directory resolver is not configured.")
        return self._directory_resolver


def _coerce_turn_input(turn_input: str | UserTurnInput) -> UserTurnInput:
    if isinstance(turn_input, UserTurnInput):
        return turn_input
    return UserTurnInput(text=turn_input)


def _backend_turn_input(turn_input: UserTurnInput) -> str | UserTurnInput:
    if not turn_input.images and not turn_input.skills:
        return turn_input.text
    return turn_input


def _skill_slug(name: str) -> str:
    return "".join(char if char.isalnum() else "_" for char in name.casefold()).strip(
        "_"
    )


async def _notify_state_change(
    on_state_change: TurnStateChangeHandler | None,
) -> None:
    if on_state_change is not None:
        await on_state_change()


def _codex_thread_project(thread: CodexThread) -> str:
    return thread.cwd or "(no project)"


def _group_codex_threads(threads: list[CodexThread]) -> list[CodexThreadGroup]:
    backend_order: dict[str, int] = {}
    grouped: dict[tuple[str, str, str], list[CodexThread]] = {}
    for thread in threads:
        backend_order.setdefault(thread.codex_backend_id, len(backend_order))
        grouped.setdefault(
            (
                thread.codex_backend_id,
                thread.codex_backend_name,
                _codex_thread_project(thread),
            ),
            [],
        ).append(thread)
    groups = [
        CodexThreadGroup(
            project=project,
            threads=sorted(
                project_threads,
                key=lambda item: (
                    _status_sort_key(item.status),
                    -item.updated_at,
                ),
            ),
            backend_id=backend_id,
            backend_name=backend_name,
        )
        for (backend_id, backend_name, project), project_threads in sorted(
            grouped.items(),
            key=lambda item: (
                backend_order[item[0][0]],
                -max(thread.updated_at for thread in item[1]),
                _project_sort_key(item[0][2]),
                item[0][2],
            ),
        )
    ]
    return _limit_projects_per_backend(groups)


def _limit_projects_per_backend(
    groups: list[CodexThreadGroup],
) -> list[CodexThreadGroup]:
    counts: dict[str, int] = {}
    limited: list[CodexThreadGroup] = []
    for group in groups:
        count = counts.get(group.backend_id, 0)
        if count >= CODEX_THREAD_PROJECT_LIMIT_PER_BACKEND:
            continue
        counts[group.backend_id] = count + 1
        limited.append(group)
    return limited


def _listing_failures(client: CodexBackend) -> list[CodexThreadBackendFailure]:
    failures = getattr(client, "listing_failures", None)
    if failures is None:
        return []
    return [
        failure
        for failure in failures
        if isinstance(failure, CodexThreadBackendFailure)
    ]


def _project_sort_key(project: str) -> int:
    return 1 if project == "(no project)" else 0


def _status_sort_key(status: str) -> int:
    return 0 if status == "active" else 1


def _should_steer_active_turn(
    thread: LogicalThread,
    overrides: SessionOverrides,
    profile: ProfileDefinition,
) -> bool:
    return (
        thread.awaiting_reply
        and thread.pending_turn_id is not None
        and thread.codex_thread_id is not None
        and (overrides.followup_mode or profile.followup_mode) == "steer"
    )
