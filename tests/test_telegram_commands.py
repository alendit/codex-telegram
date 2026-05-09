from __future__ import annotations

from types import TracebackType
from types import SimpleNamespace
from typing import Any, cast

import pytest
from aiogram.types import Message

from codex_telegram.adapters.telegram.commands import TelegramCommandExecutor
from codex_telegram.adapters.telegram.routing import ChatContext
from codex_telegram.application.models import (
    AccountUsage,
    McpServerCapability,
    McpToolCapability,
    SkillCapability,
    SkillCatalog,
    UsageState,
)
from codex_telegram.application.service import TurnRunResult
from codex_telegram.domain import LogicalThread

CONTEXT = ChatContext(chat_key="chat:123", chat_id=123, topic_id=None)


class _TypingLoop:
    def __init__(self, host: _Host) -> None:
        self._host = host

    async def __aenter__(self) -> None:
        self._host.typing_depth += 1
        return None

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self._host.typing_depth -= 1
        return None


class _FakeService:
    def __init__(self, host: _Host) -> None:
        self._host = host
        self.new_thread_calls: list[tuple[str, str | None, str | None]] = []
        self.run_turn_calls: list[tuple[str, str, str]] = []
        self.run_skill_turn_calls: list[tuple[str, str, str, str]] = []
        self.start_realtime_calls: list[tuple[str, str]] = []
        self.usage_state_calls: list[str] = []
        self.list_skills_calls: list[tuple[str, str | None, bool]] = []
        self.list_mcp_calls: list[str] = []
        self.list_mcp_typing_depths: list[int] = []

    async def new_thread(
        self,
        chat_key: str,
        *,
        connection_name: str | None = None,
        project_selector: str | None = None,
    ) -> LogicalThread:
        self.new_thread_calls.append((chat_key, connection_name, project_selector))
        return LogicalThread(
            thread_id="thread-1",
            chat_key=chat_key,
            title="Work",
            codex_thread_id="codex-1",
            created_at="2026-05-06T00:00:00Z",
            updated_at="2026-05-06T00:00:00Z",
            turn_count=0,
            awaiting_reply=False,
            interrupted_notice=False,
            pending_turn_id=None,
        )

    async def run_turn(
        self,
        chat_key: str,
        prompt: str,
        *,
        thread_id: str,
        on_update: Any,
        on_wait_notice: Any,
        on_state_change: Any,
    ) -> TurnRunResult:
        del on_update, on_wait_notice, on_state_change
        self.run_turn_calls.append((chat_key, prompt, thread_id))
        return TurnRunResult(
            result=None,
            remapped=False,
            remap_warning=None,
            active_turn_continues=True,
        )

    async def start_realtime(
        self,
        chat_key: str,
        *,
        thread_id: str,
    ) -> None:
        self.start_realtime_calls.append((chat_key, thread_id))
        raise RuntimeError("unknown method realtime")

    async def list_skills(
        self,
        chat_key: str,
        *,
        search: str | None = None,
        force_reload: bool = False,
    ) -> list[SkillCatalog]:
        self.list_skills_calls.append((chat_key, search, force_reload))
        return [
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
                ),
            )
        ]

    async def list_mcp_servers(self, chat_key: str) -> list[McpServerCapability]:
        self.list_mcp_calls.append(chat_key)
        self.list_mcp_typing_depths.append(self._host.typing_depth)
        return [
            McpServerCapability(
                name="filesystem",
                auth_status="unsupported",
                tools=(
                    McpToolCapability(
                        name="read_file",
                        description="<b>Read</b> files",
                    ),
                ),
            )
        ]

    async def run_skill_turn(
        self,
        chat_key: str,
        selector: str,
        prompt: str,
        *,
        thread_id: str,
        on_update: Any,
        on_wait_notice: Any,
        on_state_change: Any,
    ) -> TurnRunResult:
        del on_update, on_wait_notice, on_state_change
        self.run_skill_turn_calls.append((chat_key, selector, prompt, thread_id))
        return TurnRunResult(
            result=None,
            remapped=False,
            remap_warning=None,
            active_turn_continues=True,
        )

    async def usage_state(self, chat_key: str) -> UsageState:
        self.usage_state_calls.append(chat_key)
        return UsageState(
            conversation_name="Work",
            backend_id="primary",
            codex_thread_attached=True,
            token_usage={"total_tokens": 42},
            account=AccountUsage(
                status="unavailable",
                reason="app-server does not expose account limits",
            ),
        )


class _Host:
    def __init__(self) -> None:
        self.typing_depth = 0
        self._service = _FakeService(self)
        self.sent_texts: list[str] = []
        self.sent_parse_modes: list[str | None] = []
        self.new_picker_count = 0
        self.sync_count = 0
        self.finished_results: list[TurnRunResult] = []

    async def _send_text(
        self,
        context: ChatContext,
        text: str,
        *,
        reply_markup: Any = None,
        parse_mode: str | None = None,
    ) -> Message:
        del context, reply_markup
        self.sent_texts.append(text)
        self.sent_parse_modes.append(parse_mode)
        return cast(Message, SimpleNamespace(message_id=len(self.sent_texts)))

    def _typing_loop(
        self,
        context: ChatContext,
        *,
        thread_id: str | None = None,
    ) -> _TypingLoop:
        del context, thread_id
        return _TypingLoop(self)

    async def _new_conversation_notice(
        self,
        context: ChatContext,
        thread: LogicalThread,
    ) -> str:
        del context
        return f"Started {thread.thread_id}"

    async def _sync_thread_status_card(self, context: ChatContext) -> None:
        del context
        self.sync_count += 1

    async def _handle_turn_state_change(self, context: ChatContext) -> None:
        await self._sync_thread_status_card(context)

    async def _send_new_connection_picker(self, context: ChatContext) -> None:
        del context
        self.new_picker_count += 1

    async def _finish_run_result(
        self,
        context: ChatContext,
        run_result: TurnRunResult,
    ) -> None:
        del context
        self.finished_results.append(run_result)


@pytest.mark.asyncio
async def test_command_executor_shows_new_picker_without_prompt() -> None:
    host = _Host()
    handled = await TelegramCommandExecutor(cast(Any, host)).handle(
        CONTEXT,
        "thread-1",
        ("new", ""),
        "/new",
    )

    assert handled is True
    assert host.new_picker_count == 1
    assert host._service.new_thread_calls == []


@pytest.mark.asyncio
async def test_command_executor_runs_prompt_after_new_thread_creation() -> None:
    host = _Host()
    handled = await TelegramCommandExecutor(cast(Any, host)).handle(
        CONTEXT,
        "thread-1",
        ("new", "--connection work --project api explain this"),
        "/new --connection work --project api explain this",
    )

    assert handled is True
    assert host._service.new_thread_calls == [("chat:123", "work", "api")]
    assert host._service.run_turn_calls == [("chat:123", "explain this", "thread-1")]
    assert host.sent_texts == ["Started thread-1"]
    assert host.sync_count == 1
    assert len(host.finished_results) == 1


@pytest.mark.asyncio
async def test_command_executor_reports_realtime_feature_errors() -> None:
    host = _Host()
    handled = await TelegramCommandExecutor(cast(Any, host)).handle(
        CONTEXT,
        "thread-1",
        ("realtime", ""),
        "/realtime",
    )

    assert handled is True
    assert host._service.start_realtime_calls == [("chat:123", "thread-1")]
    assert "Realtime is not enabled in Codex config" in host.sent_texts[-1]
    assert "features.realtime_conversation = true" in host.sent_texts[-1]


@pytest.mark.asyncio
async def test_command_executor_renders_usage_state() -> None:
    host = _Host()
    handled = await TelegramCommandExecutor(cast(Any, host)).handle(
        CONTEXT,
        "thread-1",
        ("usage", ""),
        "/usage",
    )

    assert handled is True
    assert host._service.usage_state_calls == ["chat:123"]
    assert host.sent_parse_modes[-1] == "HTML"
    assert "<b>Usage</b>" in host.sent_texts[-1]
    assert "<b>Latest turn</b>" in host.sent_texts[-1]
    assert "Total: 42" in host.sent_texts[-1]


@pytest.mark.asyncio
async def test_command_executor_renders_skills_with_refresh_and_search() -> None:
    host = _Host()
    handled = await TelegramCommandExecutor(cast(Any, host)).handle(
        CONTEXT,
        "thread-1",
        ("skills", "--refresh webhook"),
        "/skills --refresh webhook",
    )

    assert handled is True
    assert host._service.list_skills_calls == [("chat:123", "webhook", True)]
    assert host.sent_parse_modes[-1] == "HTML"
    assert "/skill_codex_telegram_webhooks" in host.sent_texts[-1]


@pytest.mark.asyncio
async def test_command_executor_runs_skill_shortcut() -> None:
    host = _Host()
    handled = await TelegramCommandExecutor(cast(Any, host)).handle(
        CONTEXT,
        "thread-1",
        ("skill_codex_telegram_webhooks", "Create a status webhook."),
        "/skill_codex_telegram_webhooks Create a status webhook.",
    )

    assert handled is True
    assert host._service.run_skill_turn_calls == [
        (
            "chat:123",
            "codex_telegram_webhooks",
            "Create a status webhook.",
            "thread-1",
        )
    ]
    assert len(host.finished_results) == 1


@pytest.mark.asyncio
async def test_command_executor_renders_mcp_summary() -> None:
    host = _Host()
    handled = await TelegramCommandExecutor(cast(Any, host)).handle(
        CONTEXT,
        "thread-1",
        ("mcp", ""),
        "/mcp",
    )

    assert handled is True
    assert host._service.list_mcp_calls == ["chat:123"]
    assert host._service.list_mcp_typing_depths == [1]
    assert host.sent_parse_modes[-1] == "HTML"
    assert "filesystem" in host.sent_texts[-1]
