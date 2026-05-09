"""Telegram command parsing helpers."""

from __future__ import annotations

from collections.abc import Coroutine
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from typing import Any, Literal, Protocol, cast

from aiogram.types import InlineKeyboardMarkup, Message

from codex_telegram.adapters.telegram.rendering import (
    COMMAND_FAILURE_PREFIX,
    COMMAND_SUCCESS_PREFIX,
    WARNING_PREFIX,
    _logical_thread_name,
    _short_line,
    render_codex_thread_attached,
    render_current_thread,
    render_directory_state,
    render_goal_status,
    render_help,
    render_history,
    render_mcp_servers,
    render_project_state,
    render_settings,
    render_single_setting,
    render_skills,
    render_usage,
    render_webhook_created,
    render_webhooks,
)
from codex_telegram.adapters.telegram.routing import ChatContext
from codex_telegram.application.service import (
    BotService,
    ThreadSelectionResult,
    TurnRunResult,
)
from codex_telegram.domain import (
    Project,
    RealtimeEvent,
    TurnUpdate,
    WebhookSubscription,
)


@dataclass(frozen=True, slots=True)
class CodexThreadsCommand:
    """Parsed `/threads` command arguments."""

    full: bool
    backend_name: str | None
    include_all: bool
    search: str | None


@dataclass(frozen=True, slots=True)
class CommandOptions:
    """Shared parser output for Telegram command options."""

    connection: str | None
    project: str | None
    all: bool
    full: bool
    label: str | None
    positionals: list[str]


@dataclass(frozen=True, slots=True)
class GoalCommand:
    """Parsed `/goal` command arguments."""

    objective: str
    token_budget: int | None
    update_token_budget: bool
    budget_only: bool


class CommandHost(Protocol):
    _service: BotService

    async def _send_text(
        self,
        context: ChatContext,
        text: str,
        *,
        reply_markup: InlineKeyboardMarkup | None = None,
        parse_mode: str | None = None,
    ) -> Message: ...

    def _typing_loop(
        self,
        context: ChatContext,
        *,
        thread_id: str | None = None,
    ) -> AbstractAsyncContextManager[None]: ...

    async def _handle_turn_update(
        self,
        context: ChatContext,
        update: TurnUpdate,
    ) -> None: ...

    async def _handle_wait_notice(self, context: ChatContext) -> None: ...

    async def _handle_turn_state_change(self, context: ChatContext) -> None: ...

    async def _finish_run_result(
        self,
        context: ChatContext,
        run_result: TurnRunResult,
    ) -> None: ...

    async def _handle_text_shortcut(self, context: ChatContext, token: str) -> None: ...

    async def _focus_bridge(
        self,
        context: ChatContext,
        selector: str,
    ) -> ThreadSelectionResult: ...

    async def _new_conversation_notice(
        self,
        context: ChatContext,
        thread: Any,
    ) -> str: ...

    async def _sync_thread_status_card(self, context: ChatContext) -> None: ...

    async def _show_status_card(self, context: ChatContext) -> None: ...

    async def _send_codex_threads_connection_picker(
        self,
        context: ChatContext,
    ) -> None: ...

    async def _send_new_connection_picker(self, context: ChatContext) -> None: ...

    async def _send_codex_threads_listing(
        self,
        context: ChatContext,
        codex_options: CodexThreadsCommand,
    ) -> None: ...

    async def _send_focus_final_messages(
        self,
        context: ChatContext,
        thread_id: str,
    ) -> None: ...

    def _consume_realtime_events(
        self,
        context: ChatContext,
        logical_thread_id: str,
        *,
        announce_started: bool = False,
    ) -> Coroutine[object, object, None]: ...

    def _spawn_background(self, coro: Coroutine[object, object, None]) -> None: ...

    async def _webhooks_markup(
        self,
        context: ChatContext,
        subscriptions: list[WebhookSubscription],
    ) -> InlineKeyboardMarkup | None: ...

    def _webhook_event_url(self, webhook_id: str) -> str: ...


class TelegramCommandExecutor:
    """Execute parsed Telegram slash commands."""

    def __init__(self, host: CommandHost) -> None:
        self._host = host

    async def handle(
        self,
        context: ChatContext,
        thread_id: str,
        command: tuple[str, str],
        raw_text: str,
    ) -> bool:
        del raw_text
        host = self._host
        name, argument = command
        if name.startswith("ct_"):
            await host._handle_text_shortcut(context, name.removeprefix("ct_"))
            return True
        if name.startswith("cf_"):
            await host._handle_text_shortcut(context, name.removeprefix("cf_"))
            return True
        if name.startswith("skill_"):
            return await self._handle_skill_turn(
                context,
                thread_id,
                name.removeprefix("skill_"),
                argument,
            )
        if name == "new":
            if not argument.strip():
                await host._send_new_connection_picker(context)
                return True
            try:
                parsed = _parse_command_options(
                    argument,
                    allowed_flags={"connection", "project"},
                )
            except ValueError as exc:
                await host._send_text(context, COMMAND_FAILURE_PREFIX + str(exc))
                return True
            prompt = " ".join(parsed.positionals).strip()
            thread = await host._service.new_thread(
                context.chat_key,
                connection_name=parsed.connection,
                project_selector=parsed.project,
            )
            await host._send_text(
                context, await host._new_conversation_notice(context, thread)
            )
            await host._sync_thread_status_card(context)
            if prompt:
                async with host._typing_loop(context, thread_id=thread.thread_id):
                    run_result = await host._service.run_turn(
                        context.chat_key,
                        prompt,
                        thread_id=thread.thread_id,
                        on_update=lambda update: host._handle_turn_update(
                            context, update
                        ),
                        on_wait_notice=lambda: host._handle_wait_notice(context),
                        on_state_change=lambda: host._handle_turn_state_change(context),
                    )
                await host._finish_run_result(context, run_result)
            return True
        if name == "overview":
            await host._show_status_card(context)
            return True
        if name == "threads":
            if not argument.strip():
                await host._send_codex_threads_connection_picker(context)
                return True
            try:
                codex_options = _parse_codex_threads_argument(argument)
            except ValueError as exc:
                await host._send_text(context, COMMAND_FAILURE_PREFIX + str(exc))
                return True
            await host._send_codex_threads_listing(context, codex_options)
            return True
        if name == "attach_thread":
            try:
                parsed = _parse_command_options(
                    argument,
                    allowed_flags={"connection"},
                )
            except ValueError as exc:
                await host._send_text(context, COMMAND_FAILURE_PREFIX + str(exc))
                return True
            if len(parsed.positionals) != 1 or ":" in parsed.positionals[0]:
                await host._send_text(
                    context,
                    COMMAND_FAILURE_PREFIX
                    + "Usage: /attach_thread [--connection <name|id>] <id>",
                )
                return True
            connection = await host._service.attach_codex_thread(
                context.chat_key,
                parsed.positionals[0],
                backend_name=parsed.connection,
            )
            await host._send_text(
                context, render_codex_thread_attached(connection), parse_mode="HTML"
            )
            await host._sync_thread_status_card(context)
            return True
        if name == "focus":
            result = await host._focus_bridge(context, argument)
            await host._send_text(context, result.message)
            if result.success and result.thread is not None:
                await host._send_focus_final_messages(
                    context,
                    result.thread.thread_id,
                )
            await host._sync_thread_status_card(context)
            return True
        if name == "to":
            target, _, prompt = argument.strip().partition(" ")
            if not target or not prompt:
                await host._send_text(
                    context,
                    COMMAND_FAILURE_PREFIX + "Usage: /to <conversation-id> <message>",
                )
                return True
            selected = await host._service.resolve_bridge(
                context.chat_key,
                target,
                focus=False,
            )
            if not selected.success or selected.thread is None:
                await host._send_text(context, selected.message)
                return True
            async with host._typing_loop(context, thread_id=selected.thread.thread_id):
                run_result = await host._service.run_turn(
                    context.chat_key,
                    prompt,
                    thread_id=selected.thread.thread_id,
                    on_update=lambda update: host._handle_turn_update(context, update),
                    on_wait_notice=lambda: host._handle_wait_notice(context),
                    on_state_change=lambda: host._handle_turn_state_change(context),
                )
            await host._finish_run_result(context, run_result)
            return True
        if name in {"current", "status", "settings"}:
            current = await host._service.current_thread_state(context.chat_key)
            await host._send_text(
                context, render_current_thread(current), parse_mode="HTML"
            )
            return True
        if name == "history":
            limit = 10
            if argument:
                try:
                    limit = int(argument)
                except ValueError:
                    await host._send_text(
                        context,
                        COMMAND_FAILURE_PREFIX + "Usage: /history [count]",
                    )
                    return True
            history = await host._service.thread_history(context.chat_key, limit)
            await host._send_text(context, render_history(history), parse_mode="HTML")
            return True
        if name == "help":
            await host._send_text(context, render_help(), parse_mode="HTML")
            return True
        if name == "skills":
            search, force_reload = _parse_skills_argument(argument)
            try:
                catalogs = await host._service.list_skills(
                    context.chat_key,
                    search=search,
                    force_reload=force_reload,
                )
            except ValueError as exc:
                await host._send_text(context, COMMAND_FAILURE_PREFIX + str(exc))
                return True
            await host._send_text(context, render_skills(catalogs), parse_mode="HTML")
            return True
        if name == "skill":
            selector, _, prompt = argument.strip().partition(" ")
            return await self._handle_skill_turn(context, thread_id, selector, prompt)
        if name == "mcp":
            return await self._handle_mcp(context, thread_id, argument)
        if name == "usage":
            usage = await host._service.usage_state(context.chat_key)
            await host._send_text(context, render_usage(usage), parse_mode="HTML")
            return True
        if name == "goal":
            return await self._handle_goal(context, argument)
        if name == "plan":
            return await self._handle_plan(context, thread_id, argument)
        if name == "implement":
            return await self._handle_implement(context, thread_id, argument)
        if name in {"interrupt", "abort", "stop"}:
            await host._send_text(
                context,
                await host._service.interrupt_active_turn(context.chat_key),
            )
            return True
        if name == "realtime":
            return await self._handle_realtime(context, thread_id, argument)
        if name in {"backends", "select_backend", "select-backend"}:
            await host._send_text(
                context,
                "This bot is Codex-only. Backend switching is not supported here.",
            )
            return True
        if name in {"resetparams", "clearparams"}:
            settings = await host._service.clear_overrides(thread_id)
            await host._send_text(
                context,
                "Session overrides cleared.\n" + render_settings(settings),
                parse_mode="HTML",
            )
            return True
        if name in {"dir", "cd", "cwd"}:
            try:
                if not argument:
                    state = await host._service.show_directory_state(context.chat_key)
                elif argument == "-":
                    state = await host._service.switch_previous_directory(
                        context.chat_key
                    )
                elif argument.isdigit():
                    state = await host._service.switch_directory_from_history(
                        context.chat_key,
                        int(argument),
                    )
                elif argument.lower() == "reset":
                    state = await host._service.reset_directory(context.chat_key)
                else:
                    state = await host._service.set_directory(
                        context.chat_key, argument
                    )
            except ValueError as exc:
                await host._send_text(context, COMMAND_FAILURE_PREFIX + str(exc))
                return True
            await host._send_text(
                context, render_directory_state(state), parse_mode="HTML"
            )
            return True
        if name == "project":
            if argument.strip():
                await host._send_text(
                    context,
                    COMMAND_FAILURE_PREFIX + "Usage: /project",
                )
                return True
            try:
                project_state = await host._service.show_project_state(
                    context.chat_key,
                )
            except ValueError as exc:
                await host._send_text(context, COMMAND_FAILURE_PREFIX + str(exc))
                return True
            await host._send_text(
                context, render_project_state(project_state), parse_mode="HTML"
            )
            return True
        if name == "webhooks" or (name == "webhook" and argument.lower() == "list"):
            subscriptions = await host._service.list_webhook_subscriptions(
                chat_key=context.chat_key,
            )
            await host._send_text(
                context,
                render_webhooks(subscriptions),
                reply_markup=await host._webhooks_markup(context, subscriptions),
                parse_mode="HTML",
            )
            return True
        if name == "webhook":
            return await self._handle_webhook(context, argument)
        if name in {
            "profile",
            "model",
            "effort",
            "summary",
            "verbosity",
            "command_verbosity",
            "followup_mode",
        }:
            field_name = cast(
                Literal[
                    "profile",
                    "model",
                    "effort",
                    "summary",
                    "verbosity",
                    "command_verbosity",
                    "followup_mode",
                ],
                name,
            )
            if not argument:
                settings = await host._service.get_settings(thread_id, context.chat_key)
                await host._send_text(
                    context,
                    render_single_setting(field_name, settings),
                    parse_mode="HTML",
                )
                return True
            if argument == "default":
                settings = await host._service.update_override(
                    thread_id, field_name, None
                )
            else:
                settings = await host._service.update_override(
                    thread_id, field_name, argument
                )
            await host._send_text(
                context,
                render_single_setting(field_name, settings),
                parse_mode="HTML",
            )
            return True
        if name == "fast":
            if not argument:
                settings = await host._service.get_settings(thread_id, context.chat_key)
                await host._send_text(
                    context, render_single_setting(name, settings), parse_mode="HTML"
                )
                return True
            normalized = argument.lower()
            enabled = normalized in {"on", "true", "1", "yes"}
            settings = await host._service.set_fast_mode(thread_id, enabled)
            await host._send_text(
                context, render_single_setting(name, settings), parse_mode="HTML"
            )
            return True
        return False

    async def _handle_goal(self, context: ChatContext, argument: str) -> bool:
        host = self._host
        stripped = argument.strip()
        try:
            if not stripped or stripped.casefold() == "status":
                goal = await host._service.get_goal(context.chat_key)
                await host._send_text(
                    context, render_goal_status(goal), parse_mode="HTML"
                )
                return True
            folded = stripped.casefold()
            if folded == "clear":
                await host._service.clear_goal(context.chat_key)
                await host._send_text(context, "Goal cleared.")
                return True
            if folded == "pause":
                goal = await host._service.update_goal_status(
                    context.chat_key,
                    "paused",
                )
                await host._send_text(
                    context, render_goal_status(goal), parse_mode="HTML"
                )
                return True
            if folded == "resume":
                goal = await host._service.update_goal_status(
                    context.chat_key,
                    "active",
                )
                await host._send_text(
                    context, render_goal_status(goal), parse_mode="HTML"
                )
                return True

            parsed = _parse_goal_argument(stripped)
            if parsed.budget_only:
                goal = await host._service.update_goal_budget(
                    context.chat_key,
                    parsed.token_budget,
                )
            else:
                if not parsed.objective:
                    await host._send_text(
                        context,
                        COMMAND_FAILURE_PREFIX
                        + "Usage: /goal [--budget N] <objective>",
                    )
                    return True
                goal = await host._service.set_goal(
                    context.chat_key,
                    objective=parsed.objective,
                    token_budget=parsed.token_budget,
                    status="active",
                    update_token_budget=parsed.update_token_budget,
                )
        except ValueError as exc:
            await host._send_text(context, COMMAND_FAILURE_PREFIX + str(exc))
            return True
        await host._send_text(context, render_goal_status(goal), parse_mode="HTML")
        return True

    async def _handle_plan(
        self,
        context: ChatContext,
        thread_id: str,
        argument: str,
    ) -> bool:
        host = self._host
        prompt = argument.strip()
        if prompt.casefold() == "off":
            settings = await host._service.set_collaboration_mode(
                context.chat_key,
                "default",
            )
            await host._send_text(
                context,
                "Plan mode off.\n" + render_settings(settings),
                parse_mode="HTML",
            )
            return True
        settings = await host._service.set_collaboration_mode(context.chat_key, "plan")
        if not prompt:
            await host._send_text(
                context,
                "Plan mode on.\n" + render_settings(settings),
                parse_mode="HTML",
            )
            return True
        async with host._typing_loop(context, thread_id=thread_id):
            run_result = await host._service.run_turn(
                context.chat_key,
                prompt,
                on_update=lambda update: host._handle_turn_update(context, update),
                on_wait_notice=lambda: host._handle_wait_notice(context),
                on_state_change=lambda: host._handle_turn_state_change(context),
            )
        await host._finish_run_result(context, run_result)
        return True

    async def _handle_implement(
        self,
        context: ChatContext,
        thread_id: str,
        argument: str,
    ) -> bool:
        host = self._host
        prompt = argument.strip() or "Implement as planned."
        await host._service.set_collaboration_mode(context.chat_key, "default")
        async with host._typing_loop(context, thread_id=thread_id):
            run_result = await host._service.run_turn(
                context.chat_key,
                prompt,
                on_update=lambda update: host._handle_turn_update(context, update),
                on_wait_notice=lambda: host._handle_wait_notice(context),
                on_state_change=lambda: host._handle_turn_state_change(context),
            )
        await host._finish_run_result(context, run_result)
        return True

    async def _handle_skill_turn(
        self,
        context: ChatContext,
        thread_id: str,
        selector: str,
        prompt: str,
    ) -> bool:
        host = self._host
        if not selector.strip() or not prompt.strip():
            await host._send_text(
                context,
                COMMAND_FAILURE_PREFIX + "Usage: /skill <name-or-shortcut> <prompt>",
            )
            return True
        try:
            async with host._typing_loop(context, thread_id=thread_id):
                run_result = await host._service.run_skill_turn(
                    context.chat_key,
                    selector.strip(),
                    prompt.strip(),
                    thread_id=thread_id,
                    on_update=lambda update: host._handle_turn_update(context, update),
                    on_wait_notice=lambda: host._handle_wait_notice(context),
                    on_state_change=lambda: host._handle_turn_state_change(context),
                )
        except ValueError as exc:
            await host._send_text(context, COMMAND_FAILURE_PREFIX + str(exc))
            return True
        await host._finish_run_result(context, run_result)
        return True

    async def _handle_mcp(
        self,
        context: ChatContext,
        thread_id: str,
        argument: str,
    ) -> bool:
        host = self._host
        try:
            action, server_name = _split_mcp_command(argument)
        except ValueError as exc:
            await host._send_text(context, COMMAND_FAILURE_PREFIX + str(exc))
            return True
        async with host._typing_loop(context, thread_id=thread_id):
            servers = await host._service.list_mcp_servers(context.chat_key)
        await host._send_text(
            context,
            render_mcp_servers(servers, view=action, server_name=server_name),
            parse_mode="HTML",
        )
        return True

    async def _handle_realtime(
        self,
        context: ChatContext,
        thread_id: str,
        argument: str,
    ) -> bool:
        host = self._host
        action = argument.strip().lower()
        if action == "stop":
            await host._send_text(
                context,
                await host._service.stop_realtime(
                    context.chat_key,
                    thread_id=thread_id,
                ),
            )
            return True
        if action:
            await host._send_text(
                context,
                COMMAND_FAILURE_PREFIX + "Usage: /realtime or /realtime stop",
            )
            return True
        try:
            realtime_result = await host._service.start_realtime(
                context.chat_key,
                thread_id=thread_id,
            )
        except ValueError as exc:
            await host._send_text(context, COMMAND_FAILURE_PREFIX + str(exc))
            return True
        except Exception as err:
            if _realtime_feature_error(err):
                await host._send_text(
                    context,
                    COMMAND_FAILURE_PREFIX
                    + "Realtime is not enabled in Codex config. Set "
                    "features.realtime_conversation = true in the app-server "
                    "config.toml.",
                )
                return True
            raise
        if realtime_result.remap_warning:
            await host._send_text(
                context, WARNING_PREFIX + realtime_result.remap_warning
            )
        try:
            initial_event = await host._service.wait_for_realtime_event(
                realtime_result.session.logical_thread_id,
                timeout=5.0,
            )
        except TimeoutError:
            await host._send_text(
                context,
                "Realtime mode is starting. I will send an update when it is " "ready.",
            )
            host._spawn_background(
                host._consume_realtime_events(
                    context,
                    realtime_result.session.logical_thread_id,
                    announce_started=True,
                )
            )
            return True
        except ValueError as exc:
            await host._send_text(context, COMMAND_FAILURE_PREFIX + str(exc))
            return True
        if not isinstance(initial_event, RealtimeEvent):
            await host._send_text(
                context,
                COMMAND_FAILURE_PREFIX + "Realtime failed: unexpected startup event.",
            )
            return True
        if initial_event.event_type == "error":
            await host._send_text(
                context,
                COMMAND_FAILURE_PREFIX
                + "Realtime failed: "
                + _short_line(
                    initial_event.reason or initial_event.text or "unknown error"
                ),
            )
            return True
        if initial_event.event_type == "closed":
            await host._send_text(
                context,
                COMMAND_FAILURE_PREFIX
                + "Realtime failed: session closed before it became active.",
            )
            return True
        if initial_event.event_type == "started":
            await host._send_text(context, _realtime_started_text())
        else:
            await host._send_text(context, _realtime_started_text())
        host._spawn_background(
            host._consume_realtime_events(
                context,
                realtime_result.session.logical_thread_id,
            )
        )
        return True

    async def _handle_webhook(self, context: ChatContext, argument: str) -> bool:
        host = self._host
        if not argument:
            await host._send_text(
                context,
                COMMAND_FAILURE_PREFIX
                + "Usage: /webhook create <name> | /webhook revoke <id-or-name> | /webhook list",
            )
            return True
        action, remainder = _split_webhook_command(argument)
        if action == "create":
            if not remainder:
                await host._send_text(
                    context,
                    COMMAND_FAILURE_PREFIX + "Usage: /webhook create <name>",
                )
                return True
            try:
                created = await host._service.create_webhook_subscription(
                    chat_key=context.chat_key,
                    name=remainder,
                )
            except ValueError as exc:
                await host._send_text(context, COMMAND_FAILURE_PREFIX + str(exc))
                return True
            await host._send_text(
                context,
                render_webhook_created(
                    created.subscription,
                    event_secret=created.event_secret,
                    event_url=host._webhook_event_url(created.subscription.webhook_id),
                ),
                parse_mode="HTML",
            )
            return True
        if action in {"revoke", "delete", "remove"}:
            if not remainder:
                await host._send_text(
                    context,
                    COMMAND_FAILURE_PREFIX + "Usage: /webhook revoke <id-or-name>",
                )
                return True
            try:
                revoked = await host._service.revoke_webhook_subscription(
                    remainder,
                    chat_key=context.chat_key,
                )
            except ValueError as exc:
                await host._send_text(context, COMMAND_FAILURE_PREFIX + str(exc))
                return True
            message = (
                COMMAND_SUCCESS_PREFIX + f"Revoked webhook {remainder}."
                if revoked
                else COMMAND_FAILURE_PREFIX + f"Unknown webhook {remainder}."
            )
            await host._send_text(context, message)
            return True
        await host._send_text(
            context,
            COMMAND_FAILURE_PREFIX
            + "Usage: /webhook create <name> | /webhook revoke <id-or-name> | /webhook list",
        )
        return True


def split_command(text: str) -> tuple[str, str] | None:
    """Extract a slash command and its argument."""
    if not text.startswith("/"):
        return None
    first, *rest = text.split(maxsplit=1)
    name = first[1:].split("@", 1)[0]
    argument = rest[0].strip() if rest else ""
    return name, argument


def _parse_codex_threads_argument(argument: str) -> CodexThreadsCommand:
    parsed = _parse_command_options(
        argument,
        allowed_flags={"connection", "all", "full"},
    )
    full = parsed.full
    include_all = parsed.all
    backend_name = parsed.connection
    search = " ".join(parsed.positionals).strip() or None
    return CodexThreadsCommand(
        full=full,
        backend_name=backend_name,
        include_all=include_all,
        search=search,
    )


def _parse_skills_argument(argument: str) -> tuple[str | None, bool]:
    tokens = argument.split()
    force_reload = False
    search: list[str] = []
    for token in tokens:
        if token == "--refresh":
            force_reload = True
        elif token.startswith("--"):
            raise ValueError(f"Unknown option: {token}")
        else:
            search.append(token)
    return " ".join(search).strip() or None, force_reload


def _split_mcp_command(argument: str) -> tuple[str, str | None]:
    tokens = argument.split()
    if not tokens:
        return "summary", None
    action = tokens[0].casefold()
    if action not in {"tools", "resources"}:
        raise ValueError("Usage: /mcp [tools|resources] [server]")
    return action, tokens[1] if len(tokens) > 1 else None


def _parse_command_options(
    argument: str,
    *,
    allowed_flags: set[str],
) -> CommandOptions:
    tokens = argument.split()
    connection: str | None = None
    project: str | None = None
    label: str | None = None
    include_all = False
    full = False
    positionals: list[str] = []
    index = 0
    value_flags = {"connection", "project", "label"}
    while index < len(tokens):
        token = tokens[index]
        if not token.startswith("--"):
            folded = token.casefold()
            if folded == "full" and "full" in allowed_flags:
                full = True
                index += 1
                continue
            if folded == "all" and "all" in allowed_flags:
                include_all = True
                index += 1
                continue
            positionals.append(token)
            index += 1
            continue
        name = token[2:]
        if name not in allowed_flags:
            raise ValueError(f"Unknown option: --{name}")
        if name in value_flags:
            if index + 1 >= len(tokens) or tokens[index + 1].startswith("--"):
                raise ValueError(f"Missing value for --{name}")
            value = tokens[index + 1]
            if name == "connection":
                connection = value
            elif name == "project":
                project = value
            else:
                label = value
            index += 2
            continue
        if name == "all":
            include_all = True
        elif name == "full":
            full = True
        index += 1
    return CommandOptions(
        connection=connection,
        project=project,
        all=include_all,
        full=full,
        label=label,
        positionals=positionals,
    )


def _parse_goal_argument(argument: str) -> GoalCommand:
    tokens = argument.split()
    objective_tokens: list[str] = []
    token_budget: int | None = None
    update_token_budget = False
    index = 0
    while index < len(tokens):
        token = tokens[index]
        if token != "--budget":
            objective_tokens.append(token)
            index += 1
            continue
        if index + 1 >= len(tokens):
            raise ValueError("Missing value for --budget")
        raw_budget = tokens[index + 1].casefold()
        update_token_budget = True
        if raw_budget in {"none", "unlimited"}:
            token_budget = None
        else:
            try:
                token_budget = int(raw_budget)
            except ValueError as exc:
                raise ValueError(
                    "--budget must be a positive integer or unlimited"
                ) from exc
            if token_budget <= 0:
                raise ValueError("--budget must be a positive integer or unlimited")
        index += 2
    objective = " ".join(objective_tokens).strip()
    return GoalCommand(
        objective=objective,
        token_budget=token_budget,
        update_token_budget=update_token_budget,
        budget_only=update_token_budget and not objective,
    )


def _split_project_command(argument: str) -> tuple[str, str]:
    stripped = argument.strip()
    if not stripped:
        return "", ""
    head, _, tail = stripped.partition(" ")
    return head.casefold(), tail.strip()


def _split_webhook_command(argument: str) -> tuple[str, str]:
    parts = argument.split(maxsplit=1)
    if not parts:
        return "", ""
    action = parts[0].strip().lower()
    remainder = parts[1].strip() if len(parts) > 1 else ""
    return action, remainder


def _realtime_feature_error(err: BaseException) -> bool:
    message = str(err).lower()
    return (
        "realtime" in message
        or "unknown method" in message
        or "method not found" in message
        or "feature" in message
    )


def _realtime_started_text() -> str:
    return (
        "Realtime mode started for this conversation. Send text or voice "
        "notes; use /realtime stop to end it."
    )
