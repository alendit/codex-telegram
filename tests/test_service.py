from pathlib import Path
from datetime import UTC, datetime, timedelta
from typing import cast
from unittest.mock import AsyncMock, Mock

import aiosqlite
import pytest

from codex_telegram.adapters.filesystem import LocalDirectoryResolver
from codex_telegram.adapters.persistence.sqlite import SQLiteStateRepository
from codex_telegram.application.ports import CodexBackend
from codex_telegram.application.profiles import build_profiles
from codex_telegram.application.models import CodexThreadListResult
from codex_telegram.application.models import AccountUsage
from codex_telegram.application.models import CodexRuntimeState
from codex_telegram.application.models import SkillCapability
from codex_telegram.application.models import SkillCatalog
from codex_telegram.application.models import RuntimeUsageMetrics
from codex_telegram.application.ports import CodexBackendError
from codex_telegram.application.service import BotService, BotServiceConfig
from codex_telegram.application.service import DefaultProjectConfig
from codex_telegram.application.settings import ProjectAccessRule
from codex_telegram.config import (
    AttachmentConfig,
    AppConfig,
    ProfileConfig,
    SpeechToTextConfig,
    TelegramConfig,
    WebhookConfig,
)
from codex_telegram.domain import (
    CodexGoal,
    CodexThread,
    LogicalThread,
    PendingUserInput,
    Project,
    RealtimeSession,
    SessionOverrides,
    ThreadBindingResult,
    TurnAccepted,
    TurnResult,
    UserTurnInput,
    UserTurnSkill,
    UserInputOption,
    UserInputQuestion,
)


def _build_config() -> BotServiceConfig:
    app_config = AppConfig(
        telegram=TelegramConfig(
            bot_token="token",
            allow_from="*",
            enable_topic_sessions=False,
            typing_refresh_seconds=4.0,
            wait_notice_seconds=180.0,
            bridge_window_ttl_seconds=900.0,
            default_language=None,
        ),
        speech_to_text=SpeechToTextConfig(
            enabled=False,
            provider="openai",
            base_url=None,
            api_key=None,
            model="whisper-1",
            language_hint=None,
            request_timeout_seconds=60.0,
        ),
        webhook=WebhookConfig(
            enabled=False,
            host="127.0.0.1",
            port=8080,
            admin_token=None,
            public_base_url=None,
        ),
        attachments=AttachmentConfig(shared_root=Path("/attachments")),
        app_server_url="ws://127.0.0.1:4312",
        app_server_token="token",
        db_path=Path("/tmp/state.db"),
        default_profile="operator",
        client_default_profiles={},
        profiles={
            "operator": ProfileConfig(
                name="operator",
                model="gpt-5.4",
                model_provider="openai",
                approval_policy="untrusted",
                sandbox_type="workspaceWrite",
                network_access=False,
            )
        },
    )
    return BotServiceConfig(
        default_profile=app_config.default_profile,
        client_default_profiles=app_config.client_default_profiles,
        profiles=build_profiles(app_config.profiles),
        turn_poll_seconds=app_config.telegram.typing_refresh_seconds,
        wait_notice_seconds=app_config.telegram.wait_notice_seconds,
        focus_timeout_seconds=app_config.telegram.focus_timeout_seconds,
        active_waiting_ttl_seconds=app_config.telegram.active_waiting_ttl_seconds,
        default_project=None,
    )


def _build_config_with_default_project() -> BotServiceConfig:
    return BotServiceConfig(
        default_profile="operator",
        client_default_profiles={},
        profiles=build_profiles(
            {
                "operator": ProfileConfig(
                    name="operator",
                    model="gpt-5.4",
                    model_provider="openai",
                    approval_policy="untrusted",
                    sandbox_type="workspaceWrite",
                    network_access=False,
                )
            }
        ),
        turn_poll_seconds=4.0,
        wait_notice_seconds=180.0,
        focus_timeout_seconds=900.0,
        active_waiting_ttl_seconds=3600.0,
        default_project=DefaultProjectConfig(
            connection="laptop",
            root_path="/agent/app",
            label="app",
        ),
    )


def _build_config_with_project_restriction() -> BotServiceConfig:
    config = _build_config()
    return BotServiceConfig(
        default_profile=config.default_profile,
        client_default_profiles=config.client_default_profiles,
        client_allowed_projects={
            "chat:1": (
                ProjectAccessRule(connection="laptop", root_path="/agent/allowed"),
            )
        },
        profiles=config.profiles,
        turn_poll_seconds=config.turn_poll_seconds,
        wait_notice_seconds=config.wait_notice_seconds,
        bridge_window_ttl_seconds=config.bridge_window_ttl_seconds,
        focus_timeout_seconds=config.focus_timeout_seconds,
        active_waiting_ttl_seconds=config.active_waiting_ttl_seconds,
        default_project=config.default_project,
    )


class _BackendWithFriendlyNames:
    def resolve_backend_id(
        self,
        *,
        backend_id: str | None = None,
        backend_name: str | None = None,
    ) -> str:
        del backend_id
        if backend_name == "laptop":
            return "backend-laptop"
        return backend_name or "primary"


@pytest.mark.asyncio
async def test_interrupt_active_turn_requests_backend_interrupt() -> None:
    repository = AsyncMock()
    client = AsyncMock()
    repository.get_active_thread.return_value = LogicalThread(
        thread_id="thread-1",
        chat_key="chat:1",
        title="Thread",
        codex_thread_id="codex-thread-1",
        created_at="now",
        updated_at="now",
        turn_count=2,
        awaiting_reply=True,
        interrupted_notice=False,
        pending_turn_id="turn-1",
    )

    service = BotService(_build_config(), repository, client)

    message = await service.interrupt_active_turn("chat:1")

    assert message == "Interrupt requested."
    client.interrupt_turn.assert_awaited_once_with(
        "turn-1",
        codex_thread_id="codex-thread-1",
        codex_backend_id="primary",
    )
    repository.mark_turn_completed.assert_awaited_once_with("thread-1")


@pytest.mark.asyncio
async def test_interrupt_active_turn_handles_missing_active_turn() -> None:
    repository = AsyncMock()
    client = AsyncMock()
    repository.get_active_thread.return_value = LogicalThread(
        thread_id="thread-1",
        chat_key="chat:1",
        title="Thread",
        codex_thread_id="codex-thread-1",
        created_at="now",
        updated_at="now",
        turn_count=2,
        awaiting_reply=False,
        interrupted_notice=False,
        pending_turn_id=None,
    )

    service = BotService(_build_config(), repository, client)

    message = await service.interrupt_active_turn("chat:1")

    assert message == "No active turn to interrupt."
    client.interrupt_turn.assert_not_called()


@pytest.mark.asyncio
async def test_run_turn_steers_same_thread_followup_without_starting_new_turn() -> None:
    repository = AsyncMock()
    client = AsyncMock()
    repository.get_active_thread.return_value = LogicalThread(
        thread_id="thread-1",
        chat_key="chat:1",
        title="Thread",
        codex_thread_id="codex-1",
        created_at="now",
        updated_at="now",
        turn_count=2,
        awaiting_reply=True,
        interrupted_notice=False,
        pending_turn_id="turn-1",
    )
    repository.get_overrides.return_value = SessionOverrides(followup_mode="steer")

    service = BotService(_build_config(), repository, client)

    run_result = await service.run_turn(
        "chat:1", "Actually focus on failing tests first."
    )

    assert run_result.result is None
    assert run_result.active_turn_continues is True
    assert (
        run_result.active_turn_notice
        == "Added your follow-up to the active Codex turn."
    )
    repository.add_thread_message.assert_awaited_once_with(
        "thread-1",
        role="user",
        kind="prompt",
        text="Actually focus on failing tests first.",
    )
    client.steer_turn.assert_awaited_once_with(
        logical_thread_id="thread-1",
        codex_thread_id="codex-1",
        codex_backend_id="primary",
        turn_id="turn-1",
        text="Actually focus on failing tests first.",
    )
    client.ensure_thread_binding.assert_not_called()


@pytest.mark.asyncio
async def test_list_skills_uses_effective_thread_cwd_and_filters_search() -> None:
    repository = AsyncMock()
    client = AsyncMock()
    repository.get_active_thread.return_value = LogicalThread(
        thread_id="thread-1",
        chat_key="chat:1",
        title="Thread",
        codex_thread_id="codex-1",
        created_at="now",
        updated_at="now",
        turn_count=2,
        awaiting_reply=False,
        interrupted_notice=False,
        pending_turn_id=None,
        codex_backend_id="laptop",
    )
    repository.get_overrides.return_value = SessionOverrides(cwd="/agent/app")
    repository.get_thread_project.return_value = None
    client.list_skills.return_value = [
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

    service = BotService(_build_config(), repository, client)

    catalogs = await service.list_skills(
        "chat:1",
        search="webhook",
        force_reload=True,
    )

    assert [skill.name for skill in catalogs[0].skills] == ["codex-telegram-webhooks"]
    client.list_skills.assert_awaited_once_with(
        cwd="/agent/app",
        force_reload=True,
        codex_backend_id="laptop",
    )


@pytest.mark.asyncio
async def test_list_skills_uses_default_directory_when_thread_has_no_cwd() -> None:
    repository = AsyncMock()
    client = AsyncMock()
    directory_resolver = AsyncMock()
    directory_resolver.default_base_path.return_value = "/agent"
    repository.get_active_thread.return_value = LogicalThread(
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
        codex_backend_id="primary",
    )
    repository.get_overrides.return_value = SessionOverrides()
    repository.get_thread_project.return_value = None
    client.list_skills.return_value = [
        SkillCatalog(
            cwd="/agent",
            skills=(
                SkillCapability(
                    name="spellcheck",
                    path="/root/.codex/skills/spellcheck/SKILL.md",
                    scope="user",
                    description="Spell correction.",
                    enabled=True,
                ),
            ),
        )
    ]

    service = BotService(
        _build_config(),
        repository,
        client,
        directory_resolver=directory_resolver,
    )

    catalogs = await service.list_skills("chat:1", search="spell")

    assert [skill.name for skill in catalogs[0].skills] == ["spellcheck"]
    directory_resolver.default_base_path.assert_awaited_once_with()
    client.list_skills.assert_awaited_once_with(
        cwd="/agent",
        force_reload=False,
        codex_backend_id="primary",
    )


@pytest.mark.asyncio
async def test_list_skills_prefers_default_project_over_local_app_cwd() -> None:
    repository = AsyncMock()
    client = AsyncMock()
    directory_resolver = AsyncMock()
    directory_resolver.default_base_path.return_value = "/app"
    repository.get_active_thread.return_value = LogicalThread(
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
        codex_backend_id="laptop",
    )
    repository.get_overrides.return_value = SessionOverrides()
    repository.get_thread_project.return_value = None
    client.list_skills.return_value = [
        SkillCatalog(
            cwd="/agent/app",
            skills=(
                SkillCapability(
                    name="spellcheck",
                    path="/root/.codex/skills/spellcheck/SKILL.md",
                    scope="user",
                    description="Spell correction.",
                    enabled=True,
                ),
            ),
        )
    ]

    service = BotService(
        _build_config_with_default_project(),
        repository,
        client,
        directory_resolver=directory_resolver,
    )

    catalogs = await service.list_skills("chat:1")

    assert [skill.name for skill in catalogs[0].skills] == ["spellcheck"]
    directory_resolver.default_base_path.assert_not_awaited()
    client.list_skills.assert_awaited_once_with(
        cwd="/agent/app",
        force_reload=False,
        codex_backend_id="laptop",
    )


@pytest.mark.asyncio
async def test_run_skill_turn_resolves_slug_and_routes_skill_input() -> None:
    repository = AsyncMock()
    client = AsyncMock()
    repository.get_active_thread.return_value = LogicalThread(
        thread_id="thread-1",
        chat_key="chat:1",
        title="Thread",
        codex_thread_id="codex-1",
        created_at="now",
        updated_at="now",
        turn_count=2,
        awaiting_reply=True,
        interrupted_notice=False,
        pending_turn_id="turn-1",
        codex_backend_id="laptop",
    )
    repository.get_overrides.return_value = SessionOverrides(followup_mode="steer")
    repository.get_thread_project.return_value = None
    client.list_skills.return_value = [
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

    service = BotService(_build_config(), repository, client)

    run_result = await service.run_skill_turn(
        "chat:1",
        "codex_telegram_webhooks",
        "Create a status webhook.",
    )

    assert run_result.active_turn_continues is True
    client.steer_turn.assert_awaited_once_with(
        logical_thread_id="thread-1",
        codex_thread_id="codex-1",
        codex_backend_id="laptop",
        turn_id="turn-1",
        text=UserTurnInput(
            text="Create a status webhook.",
            skills=(
                UserTurnSkill(
                    name="codex-telegram-webhooks",
                    path="/root/.codex/skills/codex-telegram-webhooks/SKILL.md",
                ),
            ),
        ),
    )
    client.start_turn.assert_not_called()


@pytest.mark.asyncio
async def test_start_realtime_ensures_unbound_thread_binding(tmp_path: Path) -> None:
    repository = SQLiteStateRepository(tmp_path / "state.db")
    client = AsyncMock()
    client.ensure_thread_binding.return_value = ThreadBindingResult(
        codex_thread_id="codex-1",
        remapped=False,
        codex_backend_id="primary",
    )

    async def start_realtime(
        chat_key: str,
        logical_thread_id: str,
        codex_thread_id: str,
        *,
        codex_backend_id: str,
    ) -> RealtimeSession:
        return RealtimeSession(
            chat_key=chat_key,
            logical_thread_id=logical_thread_id,
            codex_thread_id=codex_thread_id,
            codex_backend_id=codex_backend_id,
        )

    client.start_realtime.side_effect = start_realtime
    await repository.initialize()
    service = BotService(_build_config(), repository, client)

    thread = await service.ensure_active_thread("chat:1")
    result = await service.start_realtime("chat:1")
    updated = await repository.get_thread(thread.thread_id)

    assert updated is not None
    assert updated.codex_thread_id == "codex-1"
    client.ensure_thread_binding.assert_awaited_once()
    client.start_realtime.assert_awaited_once_with(
        "chat:1",
        thread.thread_id,
        "codex-1",
        codex_backend_id="primary",
    )
    assert result.session.logical_thread_id == thread.thread_id


@pytest.mark.asyncio
async def test_start_realtime_reuses_bridge_thread_binding(tmp_path: Path) -> None:
    repository = SQLiteStateRepository(tmp_path / "state.db")
    client = AsyncMock()
    client.ensure_thread_binding.return_value = ThreadBindingResult(
        codex_thread_id="codex-bridge",
        remapped=False,
        codex_backend_id="laptop",
    )
    client.start_realtime.return_value = RealtimeSession(
        chat_key="chat:1",
        logical_thread_id="thread-1",
        codex_thread_id="codex-bridge",
        codex_backend_id="laptop",
    )
    await repository.initialize()
    await repository.create_thread(
        "chat:1",
        "thread-1",
        "Existing bridge",
        codex_backend_id="laptop",
    )
    await repository.update_codex_thread_binding(
        "thread-1",
        "codex-bridge",
        codex_backend_id="laptop",
    )
    service = BotService(_build_config(), repository, client)

    await service.start_realtime("chat:1")

    client.ensure_thread_binding.assert_awaited_once()
    assert client.ensure_thread_binding.await_args.args[:3] == (
        "chat:1",
        "thread-1",
        "codex-bridge",
    )
    assert (
        client.ensure_thread_binding.await_args.kwargs["codex_backend_id"] == "laptop"
    )
    client.start_realtime.assert_awaited_once_with(
        "chat:1",
        "thread-1",
        "codex-bridge",
        codex_backend_id="laptop",
    )


@pytest.mark.asyncio
async def test_start_realtime_rejects_active_regular_turn() -> None:
    repository = AsyncMock()
    client = AsyncMock()
    repository.get_active_thread.return_value = LogicalThread(
        thread_id="thread-1",
        chat_key="chat:1",
        title="Thread",
        codex_thread_id="codex-1",
        created_at="now",
        updated_at="now",
        turn_count=1,
        awaiting_reply=True,
        interrupted_notice=False,
        pending_turn_id="turn-1",
    )
    service = BotService(_build_config(), repository, client)

    with pytest.raises(ValueError, match="regular Codex turn"):
        await service.start_realtime("chat:1")

    client.start_realtime.assert_not_called()


@pytest.mark.asyncio
async def test_run_turn_reports_binding_backend_failure_without_waiting_state(
    tmp_path: Path,
) -> None:
    repository = SQLiteStateRepository(tmp_path / "state.db")
    client = AsyncMock()
    client.resolve_backend_id.return_value = "mac"
    client.ensure_thread_binding.side_effect = CodexBackendError(
        "Connection timeout to host wss://mac.example",
        backend_id="mac",
    )
    await repository.initialize()
    service = BotService(_build_config(), repository, client)
    thread = await service.new_thread("chat:1", connection_name="mac")

    run_result = await service.run_turn("chat:1", "Hello", thread_id=thread.thread_id)

    assert run_result.result is not None
    assert run_result.result.status == "failed"
    assert run_result.result.error == (
        "Codex backend 'mac' is unavailable: "
        "Connection timeout to host wss://mac.example. "
        "The bridge cleared this turn, so this conversation is idle. "
        "Try again after the backend is reachable or start a new conversation "
        "with another --connection."
    )
    updated = await repository.get_thread(thread.thread_id)
    assert updated is not None
    assert updated.awaiting_reply is False
    assert updated.pending_turn_id is None
    history = await repository.list_thread_messages(thread.thread_id, limit=10)
    assert [entry.kind for entry in history] == ["prompt", "error"]


@pytest.mark.asyncio
async def test_route_realtime_text_bypasses_regular_turn_and_steer(
    tmp_path: Path,
) -> None:
    repository = SQLiteStateRepository(tmp_path / "state.db")
    client = AsyncMock()
    client.ensure_thread_binding.return_value = ThreadBindingResult(
        codex_thread_id="codex-1",
        remapped=False,
        codex_backend_id="primary",
    )

    async def start_realtime(
        chat_key: str,
        logical_thread_id: str,
        codex_thread_id: str,
        *,
        codex_backend_id: str,
    ) -> RealtimeSession:
        return RealtimeSession(
            chat_key=chat_key,
            logical_thread_id=logical_thread_id,
            codex_thread_id=codex_thread_id,
            codex_backend_id=codex_backend_id,
        )

    client.start_realtime.side_effect = start_realtime
    await repository.initialize()
    service = BotService(_build_config(), repository, client)
    await service.ensure_active_thread("chat:1")
    await service.start_realtime("chat:1")

    routed = await service.route_realtime_input(
        "chat:1",
        UserTurnInput(text="Keep going."),
    )

    assert routed is True
    client.append_realtime_text.assert_awaited_once_with(
        "codex-1",
        "Keep going.",
        codex_backend_id="primary",
    )
    client.steer_turn.assert_not_called()
    client.start_turn.assert_not_called()


@pytest.mark.asyncio
async def test_stop_realtime_clears_active_session(tmp_path: Path) -> None:
    repository = SQLiteStateRepository(tmp_path / "state.db")
    client = AsyncMock()
    client.ensure_thread_binding.return_value = ThreadBindingResult(
        codex_thread_id="codex-1",
        remapped=False,
        codex_backend_id="primary",
    )

    async def start_realtime(
        chat_key: str,
        logical_thread_id: str,
        codex_thread_id: str,
        *,
        codex_backend_id: str,
    ) -> RealtimeSession:
        return RealtimeSession(
            chat_key=chat_key,
            logical_thread_id=logical_thread_id,
            codex_thread_id=codex_thread_id,
            codex_backend_id=codex_backend_id,
        )

    client.start_realtime.side_effect = start_realtime
    await repository.initialize()
    service = BotService(_build_config(), repository, client)
    await service.ensure_active_thread("chat:1")
    await service.start_realtime("chat:1")

    message = await service.stop_realtime("chat:1")

    assert message == "Realtime mode stopped."
    assert await service.realtime_state("chat:1") is None
    client.stop_realtime.assert_awaited_once_with(
        "codex-1",
        codex_backend_id="primary",
    )


@pytest.mark.asyncio
async def test_stop_realtime_is_idempotent_when_session_already_closed(
    tmp_path: Path,
) -> None:
    repository = SQLiteStateRepository(tmp_path / "state.db")
    client = AsyncMock()
    await repository.initialize()
    service = BotService(_build_config(), repository, client)
    await service.ensure_active_thread("chat:1")

    message = await service.stop_realtime("chat:1")

    assert message == "Realtime mode stopped."
    client.stop_realtime.assert_not_called()


@pytest.mark.asyncio
async def test_run_turn_persists_prompt_and_final_reply(tmp_path: Path) -> None:
    repository = SQLiteStateRepository(tmp_path / "state.db")
    client = AsyncMock()
    client.ensure_thread_binding.return_value = ThreadBindingResult(
        codex_thread_id="codex-1",
        remapped=False,
    )
    client.start_turn.return_value = TurnAccepted(
        turn_id="turn-1",
        chat_key="chat:1",
        logical_thread_id="ignored",
        codex_thread_id="codex-1",
        codex_backend_id="laptop",
        remapped=False,
    )
    client.wait_for_turn_event.return_value = TurnResult(
        turn_id="turn-1",
        chat_key="chat:1",
        logical_thread_id="ignored",
        codex_thread_id="codex-1",
        status="completed",
        final_text="All done.",
    )

    service = BotService(_build_config(), repository, client)
    await repository.initialize()

    run = await service.run_turn("chat:1", "Hello from Telegram")
    thread = await repository.get_active_thread("chat:1")
    assert thread is not None
    history = await repository.list_thread_messages(
        thread.thread_id,
        limit=10,
    )

    assert [(entry.role, entry.kind, entry.text) for entry in history] == [
        ("user", "prompt", "Hello from Telegram"),
        ("assistant", "final", "All done."),
    ]


@pytest.mark.asyncio
async def test_run_turn_persists_pending_user_input(tmp_path: Path) -> None:
    repository = SQLiteStateRepository(tmp_path / "state.db")
    client = AsyncMock()
    client.ensure_thread_binding.return_value = ThreadBindingResult(
        codex_thread_id="codex-1",
        remapped=False,
    )
    client.start_turn.return_value = TurnAccepted(
        turn_id="turn-1",
        chat_key="chat:1",
        logical_thread_id="ignored",
        codex_thread_id="codex-1",
        codex_backend_id="laptop",
        remapped=False,
    )
    client.wait_for_turn_event.return_value = PendingUserInput(
        request_id=9,
        chat_key="chat:1",
        logical_thread_id="thread-from-backend",
        codex_thread_id="codex-1",
        codex_backend_id="laptop",
        turn_id="turn-1",
        method="request_user_input",
        questions=(
            UserInputQuestion(
                question_id="scope",
                header="Scope",
                question="Which scope?",
                options=(UserInputOption(label="Native first"),),
            ),
        ),
    )
    service = BotService(_build_config(), repository, client)
    await repository.initialize()

    run = await service.run_turn("chat:1", "Plan this.")
    pending = await service.pending_user_input_for_chat("chat:1")

    assert run.result is not None
    assert run.result.status == "userInputRequired"
    assert pending is not None
    assert pending.request_id == 9
    assert pending.codex_backend_id == "laptop"
    assert pending.questions[0].question_id == "scope"


@pytest.mark.asyncio
async def test_resolve_pending_user_input_sends_answers_and_clears_state(
    tmp_path: Path,
) -> None:
    repository = SQLiteStateRepository(tmp_path / "state.db")
    client = AsyncMock()
    service = BotService(_build_config(), repository, client)
    await repository.initialize()
    await repository.add_pending_user_input(
        PendingUserInput(
            request_id=9,
            chat_key="chat:1",
            logical_thread_id="thread-1",
            codex_thread_id="codex-1",
            codex_backend_id="laptop",
            turn_id="turn-1",
            method="request_user_input",
            questions=(
                UserInputQuestion(
                    question_id="scope",
                    question="Which scope?",
                    options=(UserInputOption(label="Native first"),),
                ),
            ),
        )
    )

    message = await service.resolve_pending_user_input(
        9,
        {"scope": ("Native first",)},
    )

    assert message == "Response submitted."
    client.resolve_server_request.assert_awaited_once_with(
        9,
        {"answers": {"scope": {"answers": ["Native first"]}}},
        codex_backend_id="laptop",
    )
    assert await repository.get_pending_user_input("chat:1") is None


@pytest.mark.asyncio
async def test_run_turn_rolls_to_new_thread_after_idle_timeout(tmp_path: Path) -> None:
    repository = SQLiteStateRepository(tmp_path / "state.db")
    client = AsyncMock()
    client.ensure_thread_binding.return_value = ThreadBindingResult(
        codex_thread_id="codex-2",
        remapped=False,
    )
    client.start_turn.return_value = TurnAccepted(
        turn_id="turn-2",
        chat_key="chat:1",
        logical_thread_id="ignored",
        codex_thread_id="codex-2",
        remapped=False,
    )
    client.wait_for_turn_event.return_value = TurnResult(
        turn_id="turn-2",
        chat_key="chat:1",
        logical_thread_id="ignored",
        codex_thread_id="codex-2",
        status="completed",
        final_text="Fresh thread reply.",
    )
    service = BotService(_build_config(), repository, client)
    await repository.initialize()

    original = await service.ensure_active_thread("chat:1")
    await repository.mark_turn_started(original.thread_id, "turn-1")
    await repository.mark_turn_completed(original.thread_id)
    stale_time = (datetime.now(UTC) - timedelta(seconds=901)).isoformat()

    async with aiosqlite.connect(repository._path) as db:
        await db.execute(
            "UPDATE bridge_threads SET expires_at = ?, updated_at = ? WHERE bridge_id = ?",
            (stale_time, stale_time, original.thread_id),
        )
        await db.commit()

    run_result = await service.run_turn("chat:1", "Hello after idle")
    active = await repository.get_active_thread("chat:1")
    assert active is not None
    assert active.thread_id != original.thread_id
    assert run_result.result is not None

    old_history = await repository.list_thread_messages(original.thread_id, limit=10)
    new_history = await repository.list_thread_messages(active.thread_id, limit=10)

    assert old_history == []
    assert [(entry.role, entry.kind, entry.text) for entry in new_history] == [
        ("user", "prompt", "Hello after idle"),
        ("assistant", "final", "Fresh thread reply."),
    ]


@pytest.mark.asyncio
async def test_run_turn_keeps_idle_thread_with_enabled_webhook(
    tmp_path: Path,
) -> None:
    repository = SQLiteStateRepository(tmp_path / "state.db")
    client = AsyncMock()
    client.ensure_thread_binding.return_value = ThreadBindingResult(
        codex_thread_id="codex-1",
        remapped=False,
    )
    client.start_turn.return_value = TurnAccepted(
        turn_id="turn-1",
        chat_key="chat:1",
        logical_thread_id="ignored",
        codex_thread_id="codex-1",
        remapped=False,
    )
    client.wait_for_turn_event.return_value = TurnResult(
        turn_id="turn-1",
        chat_key="chat:1",
        logical_thread_id="ignored",
        codex_thread_id="codex-1",
        status="completed",
        final_text="Kept durable thread.",
    )
    service = BotService(_build_config(), repository, client)
    await repository.initialize()

    original = await service.ensure_active_thread("chat:1")
    await repository.mark_turn_started(original.thread_id, "turn-old")
    await repository.mark_turn_completed(original.thread_id)
    await repository.create_webhook_subscription(
        webhook_id="wh_123",
        chat_key="chat:1",
        thread_id=original.thread_id,
        name="front-door",
        secret_hash="hash-1",
    )
    stale_time = (datetime.now(UTC) - timedelta(seconds=901)).isoformat()

    async with aiosqlite.connect(repository._path) as db:
        await db.execute(
            "UPDATE threads SET updated_at = ? WHERE thread_id = ?",
            (stale_time, original.thread_id),
        )
        await db.commit()

    run_result = await service.run_turn("chat:1", "External event")
    active = await repository.get_active_thread("chat:1")

    assert active is not None
    assert active.thread_id == original.thread_id
    assert run_result.result is not None


@pytest.mark.asyncio
async def test_attach_codex_thread_creates_new_focused_bridge_thread(
    tmp_path: Path,
) -> None:
    repository = SQLiteStateRepository(tmp_path / "state.db")
    client = AsyncMock()
    client.get_codex_thread.return_value = CodexThread(
        thread_id="codex-1",
        cwd="/agent/project-a",
        title="Investigate deploy",
        preview="Investigate deploy failure",
        status="idle",
        created_at=1710000000,
        updated_at=1710000300,
        model_provider="openai",
    )
    service = BotService(_build_config(), repository, client)
    await repository.initialize()
    original = await service.ensure_active_thread("chat:1")

    connected = await service.attach_codex_thread("chat:1", "codex-1")
    active = await repository.get_active_thread("chat:1")
    threads = await repository.list_threads("chat:1")

    assert active is not None
    assert connected.bridge.bridge_id == active.thread_id
    assert connected.bridge.bridge_id != original.thread_id
    assert connected.bridge.codex_thread_id == "codex-1"
    assert connected.bridge.title == "Investigate deploy"
    assert connected.anchor.codex_thread_id == "codex-1"
    assert {thread.thread_id for thread in threads} == {
        original.thread_id,
        connected.bridge.bridge_id,
    }


@pytest.mark.asyncio
async def test_attach_codex_thread_binds_project_from_backend_cwd(
    tmp_path: Path,
) -> None:
    repository = SQLiteStateRepository(tmp_path / "state.db")
    client = AsyncMock()
    client.get_codex_thread.return_value = CodexThread(
        thread_id="codex-1",
        cwd="/agent/project-a",
        title="Investigate deploy",
        preview=None,
        status="idle",
        created_at=1710000000,
        updated_at=1710000300,
        model_provider="openai",
        codex_backend_id="laptop",
        codex_backend_name="laptop",
    )
    service = BotService(_build_config(), repository, client)
    await repository.initialize()

    connected = await service.attach_codex_thread(
        "chat:1",
        "codex-1",
        backend_name="laptop",
    )
    project = await repository.get_thread_project(connected.bridge.bridge_id)

    assert project == Project(
        project_id=project.project_id if project else "",
        connection_id="laptop",
        root_path="/agent/project-a",
        label="project-a",
        created_at=project.created_at if project else "",
        updated_at=project.updated_at if project else "",
    )
    assert connected.bridge.codex_backend_id == "laptop"


@pytest.mark.asyncio
async def test_run_turn_revives_expired_focused_bridge_for_same_anchor(
    tmp_path: Path,
) -> None:
    repository = SQLiteStateRepository(tmp_path / "state.db")
    client = AsyncMock()
    client.get_codex_thread.return_value = CodexThread(
        thread_id="codex-1",
        cwd="/agent/project-a",
        title="Investigate deploy",
        preview=None,
        status="idle",
        created_at=1710000000,
        updated_at=1710000300,
        model_provider="openai",
    )
    client.ensure_thread_binding.return_value = ThreadBindingResult(
        codex_thread_id="codex-1",
        codex_backend_id="primary",
        remapped=False,
    )
    client.start_turn.return_value = TurnAccepted(
        turn_id="turn-1",
        chat_key="chat:1",
        logical_thread_id="ignored",
        codex_thread_id="codex-1",
        remapped=False,
        codex_backend_id="primary",
    )
    client.wait_for_turn_event.return_value = TurnResult(
        turn_id="turn-1",
        chat_key="chat:1",
        logical_thread_id="ignored",
        codex_thread_id="codex-1",
        codex_backend_id="primary",
        status="completed",
        final_text="Fresh bridge reply.",
    )
    service = BotService(_build_config(), repository, client)
    await service.initialize()

    connected = await service.attach_codex_thread("chat:1", "codex-1")
    expired = (
        datetime.now(UTC)
        - timedelta(seconds=service.config.bridge_window_ttl_seconds + 1)
    ).isoformat()
    async with aiosqlite.connect(repository._path) as db:
        await db.execute(
            "UPDATE bridge_threads SET expires_at = ?, updated_at = ? WHERE bridge_id = ?",
            (expired, expired, connected.bridge.bridge_id),
        )
        await db.commit()

    run_result = await service.run_turn("chat:1", "Hello after expiry")
    focused = await repository.get_focused_bridge("chat:1")

    assert focused is not None
    assert focused.bridge_id != connected.bridge.bridge_id
    assert focused.anchor_id == connected.anchor.anchor_id
    assert run_result.result is not None
    assert run_result.result.logical_thread_id == focused.bridge_id


@pytest.mark.asyncio
async def test_webhook_event_reuses_anchor_without_changing_focus(
    tmp_path: Path,
) -> None:
    repository = SQLiteStateRepository(tmp_path / "state.db")
    client = AsyncMock()
    client.get_codex_thread.return_value = CodexThread(
        thread_id="codex-hook",
        cwd="/agent/project-a",
        title="Hook thread",
        preview=None,
        status="idle",
        created_at=1710000000,
        updated_at=1710000300,
        model_provider="openai",
    )
    client.ensure_thread_binding.return_value = ThreadBindingResult(
        codex_thread_id="codex-hook",
        codex_backend_id="primary",
        remapped=False,
    )
    client.start_turn.return_value = TurnAccepted(
        turn_id="turn-1",
        chat_key="chat:1",
        logical_thread_id="ignored",
        codex_thread_id="codex-hook",
        remapped=False,
        codex_backend_id="primary",
    )
    client.wait_for_turn_event.return_value = TurnResult(
        turn_id="turn-1",
        chat_key="chat:1",
        logical_thread_id="ignored",
        codex_thread_id="codex-hook",
        codex_backend_id="primary",
        status="completed",
        final_text="Hook reply.",
    )
    service = BotService(_build_config(), repository, client)
    await service.initialize()

    focused = await service.ensure_focused_bridge("chat:1")
    attached = await service.attach_codex_thread("chat:1", "codex-hook")
    await service.focus_bridge("chat:1", focused.bridge_id)
    created = await service.create_webhook_subscription(
        chat_key="chat:1",
        anchor_id=attached.anchor.anchor_id,
        name="ci",
    )

    run_result = await service.run_webhook_turn(created.subscription, "External event")
    still_focused = await repository.get_focused_bridge("chat:1")

    assert still_focused is not None
    assert still_focused.bridge_id == focused.bridge_id
    assert run_result.result is not None
    assert run_result.result.logical_thread_id != focused.bridge_id


@pytest.mark.asyncio
async def test_run_turn_titles_new_anchor_from_first_prompt(tmp_path: Path) -> None:
    repository = SQLiteStateRepository(tmp_path / "state.db")
    client = AsyncMock()
    client.ensure_thread_binding.return_value = ThreadBindingResult(
        codex_thread_id="codex-1",
        codex_backend_id="primary",
        remapped=False,
    )
    client.start_turn.return_value = TurnAccepted(
        turn_id="turn-1",
        chat_key="chat:1",
        logical_thread_id="ignored",
        codex_thread_id="codex-1",
        remapped=False,
        codex_backend_id="primary",
    )
    client.wait_for_turn_event.return_value = TurnResult(
        turn_id="turn-1",
        chat_key="chat:1",
        logical_thread_id="ignored",
        codex_thread_id="codex-1",
        codex_backend_id="primary",
        status="completed",
        final_text="Done.",
    )
    service = BotService(_build_config(), repository, client)
    await repository.initialize()

    run_result = await service.run_turn(
        "chat:1",
        "Fix the unhelpful Telegram overview titles",
    )
    conversations = await service.list_conversations("chat:1")
    focused = await repository.get_active_thread("chat:1")

    assert run_result.result is not None
    assert focused is not None
    assert focused.title == "Fix the unhelpful Telegram overview titles"
    assert conversations[0].title == "Fix the unhelpful Telegram overview titles"


@pytest.mark.asyncio
async def test_current_thread_state_includes_runtime_goal_state(tmp_path: Path) -> None:
    repository = SQLiteStateRepository(tmp_path / "state.db")
    client = AsyncMock()
    client.get_runtime_state = Mock(
        return_value=CodexRuntimeState(
            goal=CodexGoal(
                objective="Ship overview status improvements",
                status="active",
            )
        )
    )
    service = BotService(_build_config(), repository, client)
    await repository.initialize()
    anchor = await repository.upsert_conversation_anchor(
        chat_key="chat:1",
        codex_backend_id="primary",
        codex_thread_id="codex-1",
        title="Status",
    )
    await repository.create_bridge(
        chat_key="chat:1",
        bridge_id="thread-1",
        title="Status",
        anchor_id=anchor.anchor_id,
        codex_backend_id="primary",
        focus=True,
    )

    current = await service.current_thread_state("chat:1")

    assert current.runtime.goal is not None
    assert current.runtime.goal.objective == "Ship overview status improvements"
    client.get_runtime_state.assert_called_once_with(
        "codex-1",
        codex_backend_id="primary",
    )


@pytest.mark.asyncio
async def test_usage_state_includes_thread_token_usage_and_account_status(
    tmp_path: Path,
) -> None:
    repository = SQLiteStateRepository(tmp_path / "state.db")
    client = AsyncMock()
    client.get_runtime_state = Mock(
        return_value=CodexRuntimeState(
            token_usage={"total_tokens": 42},
            usage_metrics=RuntimeUsageMetrics(
                total_token_usage={"total_tokens": 100},
                last_token_usage={"total_tokens": 42},
                model_context_window=258400,
            ),
        )
    )
    client.get_usage.return_value = AccountUsage(
        status="unavailable",
        reason="app-server does not expose account limits",
    )
    service = BotService(_build_config(), repository, client)
    await repository.initialize()
    anchor = await repository.upsert_conversation_anchor(
        chat_key="chat:1",
        codex_backend_id="primary",
        codex_thread_id="codex-1",
        title="Usage",
    )
    await repository.create_bridge(
        chat_key="chat:1",
        bridge_id="thread-1",
        title="Usage",
        anchor_id=anchor.anchor_id,
        codex_backend_id="primary",
        focus=True,
    )

    usage = await service.usage_state("chat:1")

    assert usage.conversation_name == "Usage"
    assert usage.backend_id == "primary"
    assert usage.codex_thread_attached is True
    assert usage.token_usage == {"total_tokens": 42}
    assert usage.runtime_metrics == RuntimeUsageMetrics(
        total_token_usage={"total_tokens": 100},
        last_token_usage={"total_tokens": 42},
        model_context_window=258400,
    )
    assert usage.account.status == "unavailable"
    client.get_runtime_state.assert_called_once_with(
        "codex-1",
        codex_backend_id="primary",
    )
    client.get_usage.assert_awaited_once_with(codex_backend_id="primary")


@pytest.mark.asyncio
async def test_usage_state_handles_unattached_active_thread(tmp_path: Path) -> None:
    repository = SQLiteStateRepository(tmp_path / "state.db")
    client = AsyncMock()
    client.get_usage.return_value = AccountUsage(
        status="unavailable",
        reason="app-server does not expose account limits",
    )
    service = BotService(_build_config(), repository, client)
    await repository.initialize()

    usage = await service.usage_state("chat:1")

    assert usage.codex_thread_attached is False
    assert usage.token_usage is None
    assert usage.account.status == "unavailable"
    client.get_runtime_state.assert_not_called()
    client.get_usage.assert_awaited_once_with(codex_backend_id="primary")


@pytest.mark.asyncio
async def test_run_turn_publishes_runtime_goal_to_current_state(tmp_path: Path) -> None:
    repository = SQLiteStateRepository(tmp_path / "state.db")
    client = AsyncMock()
    client.ensure_thread_binding.return_value = ThreadBindingResult(
        codex_thread_id="codex-1",
        codex_backend_id="primary",
        remapped=False,
    )
    client.start_turn.return_value = TurnAccepted(
        turn_id="turn-1",
        chat_key="chat:1",
        logical_thread_id="ignored",
        codex_thread_id="codex-1",
        remapped=False,
        codex_backend_id="primary",
    )
    client.wait_for_turn_event.return_value = TurnResult(
        turn_id="turn-1",
        chat_key="chat:1",
        logical_thread_id="ignored",
        codex_thread_id="codex-1",
        codex_backend_id="primary",
        status="completed",
        final_text="Done.",
    )
    client.get_runtime_state = Mock(
        return_value=CodexRuntimeState(
            goal=CodexGoal(
                objective="Ship overview status improvements",
                status="active",
            )
        )
    )
    service = BotService(_build_config(), repository, client)
    await repository.initialize()

    await service.run_turn("chat:1", "status")
    current = await service.current_thread_state("chat:1")

    assert current.runtime.goal is not None
    assert current.runtime.goal.objective == "Ship overview status improvements"


@pytest.mark.asyncio
async def test_set_goal_binds_focused_thread_and_calls_backend(tmp_path: Path) -> None:
    repository = SQLiteStateRepository(tmp_path / "state.db")
    client = AsyncMock(spec=CodexBackend)
    client.ensure_thread_binding.return_value = ThreadBindingResult(
        codex_thread_id="codex-1",
        remapped=False,
        codex_backend_id="primary",
    )
    client.set_thread_goal.return_value = CodexGoal(
        objective="Ship goal command",
        status="active",
        token_budget=500,
    )
    service = BotService(_build_config(), repository, client)
    await repository.initialize()

    goal = await service.set_goal(
        "chat:1",
        objective="Ship goal command",
        token_budget=500,
        status="active",
        update_token_budget=True,
    )
    thread = await repository.get_active_thread("chat:1")

    assert goal == CodexGoal(
        objective="Ship goal command",
        status="active",
        token_budget=500,
    )
    assert thread is not None
    assert thread.codex_thread_id == "codex-1"
    client.ensure_thread_binding.assert_awaited_once()
    client.set_thread_goal.assert_awaited_once_with(
        "codex-1",
        objective="Ship goal command",
        token_budget=500,
        status="active",
        update_token_budget=True,
        codex_backend_id="primary",
    )


@pytest.mark.asyncio
async def test_update_goal_budget_requires_existing_goal(tmp_path: Path) -> None:
    repository = SQLiteStateRepository(tmp_path / "state.db")
    await repository.initialize()
    await repository.ensure_chat("chat:1")
    await repository.create_thread("chat:1", "thread-1", "Focused")
    await repository.update_codex_thread_binding(
        "thread-1",
        "codex-1",
        codex_backend_id="primary",
    )
    client = AsyncMock(spec=CodexBackend)
    client.get_thread_goal.return_value = None
    service = BotService(_build_config(), repository, client)

    with pytest.raises(ValueError, match="No active goal"):
        await service.update_goal_budget("chat:1", 250)


@pytest.mark.asyncio
async def test_set_collaboration_mode_validates_and_persists_override(
    tmp_path: Path,
) -> None:
    repository = SQLiteStateRepository(tmp_path / "state.db")
    client = AsyncMock(spec=CodexBackend)
    service = BotService(_build_config(), repository, client)
    await repository.initialize()
    thread = await service.ensure_active_thread("chat:1")

    settings = await service.set_collaboration_mode("chat:1", "plan")
    loaded = await repository.get_overrides(thread.thread_id)

    assert loaded.collaboration_mode == "plan"
    assert settings.collaboration_mode == "plan"
    with pytest.raises(ValueError, match="Unknown collaboration mode"):
        await service.set_collaboration_mode("chat:1", "edit")


@pytest.mark.asyncio
async def test_plan_mode_implementation_trigger_switches_to_default(
    tmp_path: Path,
) -> None:
    repository = SQLiteStateRepository(tmp_path / "state.db")
    client = AsyncMock(spec=CodexBackend)
    service = BotService(_build_config(), repository, client)
    await repository.initialize()
    thread = await service.ensure_active_thread("chat:1")
    await service.set_collaboration_mode("chat:1", "plan")

    matched = await service.apply_implementation_trigger_if_needed(
        "chat:1",
        "Implement as planned",
    )
    loaded = await repository.get_overrides(thread.thread_id)

    assert matched is True
    assert loaded.collaboration_mode == "default"


@pytest.mark.asyncio
async def test_start_plan_implementation_switches_target_thread_to_default(
    tmp_path: Path,
) -> None:
    repository = SQLiteStateRepository(tmp_path / "state.db")
    client = AsyncMock(spec=CodexBackend)
    client.ensure_thread_binding.return_value = ThreadBindingResult(
        codex_thread_id="codex-1",
        remapped=False,
    )
    client.start_turn.return_value = TurnAccepted(
        turn_id="turn-1",
        chat_key="chat:1",
        logical_thread_id="thread-1",
        codex_thread_id="codex-1",
        remapped=False,
    )
    client.wait_for_turn_event.return_value = TurnResult(
        turn_id="turn-1",
        chat_key="chat:1",
        logical_thread_id="thread-1",
        codex_thread_id="codex-1",
        status="completed",
        final_text="Implemented.",
    )
    service = BotService(_build_config(), repository, client)
    await repository.initialize()
    await repository.ensure_chat("chat:1")
    await repository.create_thread("chat:1", "thread-1", "Plan thread")
    await service.update_override("thread-1", "collaboration_mode", "plan")

    run = await service.start_plan_implementation("chat:1", "thread-1")
    loaded = await repository.get_overrides("thread-1")

    assert loaded.collaboration_mode == "default"
    assert run.result is not None
    assert run.result.final_text == "Implemented."
    client.start_turn.assert_awaited_once()
    assert client.start_turn.await_args.args[3] == "Implement as planned."


@pytest.mark.asyncio
async def test_clear_goal_calls_backend_for_bound_thread(tmp_path: Path) -> None:
    repository = SQLiteStateRepository(tmp_path / "state.db")
    await repository.initialize()
    await repository.ensure_chat("chat:1")
    await repository.create_thread("chat:1", "thread-1", "Focused")
    await repository.update_codex_thread_binding(
        "thread-1",
        "codex-1",
        codex_backend_id="primary",
    )
    client = AsyncMock(spec=CodexBackend)
    service = BotService(_build_config(), repository, client)

    await service.clear_goal("chat:1")

    client.clear_thread_goal.assert_awaited_once_with(
        "codex-1",
        codex_backend_id="primary",
    )


@pytest.mark.asyncio
async def test_list_conversations_backfills_placeholder_titles_from_history(
    tmp_path: Path,
) -> None:
    repository = SQLiteStateRepository(tmp_path / "state.db")
    service = BotService(_build_config(), repository, AsyncMock())
    await repository.initialize()
    anchor = await repository.upsert_conversation_anchor(
        chat_key="chat:1",
        codex_backend_id="primary",
        codex_thread_id="codex-1",
        title="New conversation",
    )
    bridge = await repository.create_bridge(
        chat_key="chat:1",
        bridge_id="bridge-1",
        title="New conversation",
        anchor_id=anchor.anchor_id,
        codex_backend_id="primary",
        focus=True,
    )
    await repository.add_thread_message(
        bridge.bridge_id,
        role="user",
        kind="prompt",
        text="Publish learning thermostat",
    )

    conversations = await service.list_conversations("chat:1")
    stored = await repository.get_conversation_anchor(anchor.anchor_id)

    assert conversations[0].title == "Publish learning thermostat"
    assert stored is not None
    assert stored.title == "Publish learning thermostat"


@pytest.mark.asyncio
async def test_list_conversations_backfills_placeholder_titles_from_backend(
    tmp_path: Path,
) -> None:
    repository = SQLiteStateRepository(tmp_path / "state.db")
    client = AsyncMock()
    client.get_codex_thread.return_value = CodexThread(
        thread_id="codex-1",
        cwd="/agent/project-a",
        title="Backend title",
        preview=None,
        status="idle",
        created_at=1710000000,
        updated_at=1710000300,
        model_provider="openai",
    )
    service = BotService(_build_config(), repository, client)
    await repository.initialize()
    anchor = await repository.upsert_conversation_anchor(
        chat_key="chat:1",
        codex_backend_id="primary",
        codex_thread_id="codex-1",
        title="New conversation",
    )
    await repository.create_bridge(
        chat_key="chat:1",
        bridge_id="bridge-1",
        title="New conversation",
        anchor_id=anchor.anchor_id,
        codex_backend_id="primary",
        focus=True,
    )

    conversations = await service.list_conversations("chat:1")

    assert conversations[0].title == "Backend title"
    client.get_codex_thread.assert_awaited_once_with(
        "codex-1",
        backend_id="primary",
    )


@pytest.mark.asyncio
async def test_focus_bridge_reports_thread_name_when_selector_is_anchor(
    tmp_path: Path,
) -> None:
    repository = SQLiteStateRepository(tmp_path / "state.db")
    service = BotService(_build_config(), repository, AsyncMock())
    await repository.initialize()
    anchor = await repository.upsert_conversation_anchor(
        chat_key="chat:1",
        codex_backend_id="primary",
        codex_thread_id="codex-1",
        title="Fix Telegram overview",
    )

    result = await service.focus_bridge("chat:1", anchor.anchor_id)

    assert result.success is True
    assert result.message == "Focused conversation Fix Telegram overview."
    assert anchor.anchor_id not in result.message
    assert "codex-1" not in result.message
    assert result.thread is not None
    assert result.thread.thread_id not in result.message


@pytest.mark.asyncio
async def test_focus_bridge_reuses_latest_anchor_bridge_for_delivery(
    tmp_path: Path,
) -> None:
    repository = SQLiteStateRepository(tmp_path / "state.db")
    service = BotService(_build_config(), repository, AsyncMock())
    await repository.initialize()
    anchor = await repository.upsert_conversation_anchor(
        chat_key="chat:1",
        codex_backend_id="primary",
        codex_thread_id="codex-1",
        title="Background deployment",
    )
    bridge = await repository.create_bridge(
        chat_key="chat:1",
        bridge_id="bridge-1",
        title="Background deployment",
        anchor_id=anchor.anchor_id,
        codex_backend_id="primary",
        focus=True,
    )
    await service.new_thread("chat:1")
    await repository.add_thread_message(
        bridge.bridge_id,
        role="assistant",
        kind="final",
        text="background answer",
    )

    result = await service.focus_bridge("chat:1", anchor.anchor_id)
    entries = await service.focus_final_messages("chat:1", bridge.bridge_id)

    assert result.success is True
    assert result.thread is not None
    assert result.thread.thread_id == bridge.bridge_id
    assert [(entry.message.text, entry.repeated) for entry in entries] == [
        ("background answer", False)
    ]


@pytest.mark.asyncio
async def test_new_thread_inherits_active_project(tmp_path: Path) -> None:
    repository = SQLiteStateRepository(tmp_path / "state.db")
    service = BotService(_build_config(), repository, AsyncMock())
    await repository.initialize()
    active = await service.ensure_active_thread("chat:1")
    project = await repository.upsert_project(
        connection_id="laptop",
        root_path="/agent/project-a",
        label="project-a",
    )
    await repository.bind_thread_project(active.thread_id, project.project_id)

    created = await service.new_thread("chat:1")
    bound = await repository.get_thread_project(created.thread_id)

    assert bound == project
    assert created.codex_backend_id == "laptop"


@pytest.mark.asyncio
async def test_new_thread_can_select_project_by_connection_and_label(
    tmp_path: Path,
) -> None:
    repository = SQLiteStateRepository(tmp_path / "state.db")
    service = BotService(_build_config(), repository, AsyncMock())
    await repository.initialize()
    await repository.upsert_project(
        connection_id="home",
        root_path="/agent/project-a",
        label="project-a",
    )
    laptop = await repository.upsert_project(
        connection_id="laptop",
        root_path="/agent/project-a",
        label="project-a",
    )

    created = await service.new_thread(
        "chat:1",
        connection_name="laptop",
        project_selector="project-a",
    )

    assert await repository.get_thread_project(created.thread_id) == laptop
    assert created.codex_backend_id == "laptop"


@pytest.mark.asyncio
async def test_new_thread_in_project_binds_project_by_id(tmp_path: Path) -> None:
    repository = SQLiteStateRepository(tmp_path / "state.db")
    service = BotService(_build_config(), repository, AsyncMock())
    await repository.initialize()
    project = await repository.upsert_project(
        connection_id="laptop",
        root_path="/agent/project-a",
        label="project-a",
    )

    created = await service.new_thread_in_project("chat:1", project.project_id)

    assert await repository.get_thread_project(created.thread_id) == project
    assert created.codex_backend_id == "laptop"


@pytest.mark.asyncio
async def test_new_thread_in_default_project_uses_configured_project(
    tmp_path: Path,
) -> None:
    repository = SQLiteStateRepository(tmp_path / "state.db")
    service = BotService(_build_config_with_default_project(), repository, AsyncMock())
    await repository.initialize()

    created = await service.new_thread_in_default_project("chat:1")

    project = await repository.get_thread_project(created.thread_id)
    assert project is not None
    assert project.connection_id == "laptop"
    assert project.root_path == "/agent/app"
    assert project.label == "app"
    assert created.codex_backend_id == "laptop"


@pytest.mark.asyncio
async def test_restricted_chat_can_only_start_allowed_project_threads(
    tmp_path: Path,
) -> None:
    repository = SQLiteStateRepository(tmp_path / "state.db")
    service = BotService(
        _build_config_with_project_restriction(),
        repository,
        AsyncMock(),
    )
    await repository.initialize()
    await repository.upsert_project(
        connection_id="laptop",
        root_path="/agent/allowed",
        label="allowed",
    )
    blocked = await repository.upsert_project(
        connection_id="laptop",
        root_path="/agent/blocked",
        label="blocked",
    )

    with pytest.raises(ValueError, match="Project is not allowed"):
        await service.new_thread_in_project("chat:1", blocked.project_id)


@pytest.mark.asyncio
async def test_restricted_chat_project_catalog_only_shows_allowed_projects(
    tmp_path: Path,
) -> None:
    repository = SQLiteStateRepository(tmp_path / "state.db")
    service = BotService(
        _build_config_with_project_restriction(),
        repository,
        AsyncMock(),
    )
    await repository.initialize()
    await repository.upsert_project(
        connection_id="laptop",
        root_path="/agent/allowed",
        label="allowed",
    )
    await repository.upsert_project(
        connection_id="laptop",
        root_path="/agent/blocked",
        label="blocked",
    )

    state = await service.show_project_state("chat:1")

    assert [project.label for project in state.catalog] == ["allowed"]


@pytest.mark.asyncio
async def test_restricted_chat_codex_thread_listing_only_shows_allowed_projects(
    tmp_path: Path,
) -> None:
    repository = SQLiteStateRepository(tmp_path / "state.db")
    client = AsyncMock()
    client.list_codex_threads.return_value = [
        CodexThread(
            thread_id="codex-allowed",
            cwd="/agent/allowed",
            title="Allowed",
            preview=None,
            status="idle",
            created_at=1710000000,
            updated_at=1710000300,
            model_provider="openai",
            codex_backend_id="laptop",
        ),
        CodexThread(
            thread_id="codex-blocked",
            cwd="/agent/blocked",
            title="Blocked",
            preview=None,
            status="idle",
            created_at=1710000000,
            updated_at=1710000300,
            model_provider="openai",
            codex_backend_id="laptop",
        ),
    ]
    service = BotService(
        _build_config_with_project_restriction(),
        repository,
        client,
    )
    await repository.initialize()

    listing = await service.list_codex_threads("chat:1", include_all=True)

    assert [
        thread.thread_id for group in listing.groups for thread in group.threads
    ] == ["codex-allowed"]


@pytest.mark.asyncio
async def test_project_runtime_defaults_restore_on_new_project_thread(
    tmp_path: Path,
) -> None:
    repository = SQLiteStateRepository(tmp_path / "state.db")
    service = BotService(_build_config(), repository, AsyncMock())
    await repository.initialize()
    project = await repository.upsert_project(
        connection_id="laptop",
        root_path="/agent/project-a",
        label="project-a",
    )
    first = await service.new_thread_in_project("chat:1", project.project_id)

    await service.update_override(first.thread_id, "model", "gpt-5.4-mini")
    await service.update_override(first.thread_id, "effort", "high")
    await service.set_fast_mode(first.thread_id, True)
    second = await service.new_thread_in_project("chat:1", project.project_id)

    restored = await repository.get_overrides(second.thread_id)
    assert restored.model == "gpt-5.4-mini"
    assert restored.effort == "high"
    assert restored.fast_mode is True


@pytest.mark.asyncio
async def test_project_runtime_defaults_are_used_for_first_turn_on_new_project_thread(
    tmp_path: Path,
) -> None:
    repository = SQLiteStateRepository(tmp_path / "state.db")
    client = AsyncMock(spec=CodexBackend)
    client.ensure_thread_binding.return_value = ThreadBindingResult(
        codex_thread_id="codex-2",
        remapped=False,
        codex_backend_id="laptop",
    )
    client.start_turn.return_value = TurnAccepted(
        turn_id="turn-1",
        chat_key="chat:1",
        logical_thread_id="thread-2",
        codex_thread_id="codex-2",
        remapped=False,
    )
    client.wait_for_turn_event.return_value = TurnResult(
        turn_id="turn-1",
        chat_key="chat:1",
        logical_thread_id="thread-2",
        codex_thread_id="codex-2",
        status="completed",
        final_text="Done.",
    )
    service = BotService(_build_config(), repository, client)
    await repository.initialize()
    project = await repository.upsert_project(
        connection_id="laptop",
        root_path="/agent/project-a",
        label="project-a",
    )
    first = await service.new_thread_in_project("chat:1", project.project_id)
    await service.update_override(first.thread_id, "model", "gpt-5.4-mini")
    await service.update_override(first.thread_id, "effort", "high")

    second = await service.new_thread_in_project("chat:1", project.project_id)
    await service.run_turn(
        "chat:1",
        "Use the project defaults.",
        thread_id=second.thread_id,
    )

    binding_overrides = client.ensure_thread_binding.await_args.args[4]
    turn_overrides = client.start_turn.await_args.args[5]
    assert binding_overrides.model == "gpt-5.4-mini"
    assert binding_overrides.effort == "high"
    assert binding_overrides.cwd == "/agent/project-a"
    assert turn_overrides.model == "gpt-5.4-mini"
    assert turn_overrides.effort == "high"
    assert turn_overrides.cwd == "/agent/project-a"


@pytest.mark.asyncio
async def test_thread_delivery_watermark_returns_only_undelivered_anchor_messages(
    tmp_path: Path,
) -> None:
    repository = SQLiteStateRepository(tmp_path / "state.db")
    service = BotService(_build_config(), repository, AsyncMock())
    await repository.initialize()
    anchor = await repository.upsert_conversation_anchor(
        chat_key="chat:1",
        codex_backend_id="laptop",
        codex_thread_id="codex-1",
        title="Existing Codex thread",
    )
    first = await repository.create_bridge(
        chat_key="chat:1",
        bridge_id="bridge-1",
        title="Existing Codex thread",
        anchor_id=anchor.anchor_id,
        codex_backend_id="laptop",
    )
    await repository.add_thread_message(
        first.bridge_id,
        role="assistant",
        kind="final",
        text="already delivered",
    )
    await service.mark_thread_messages_delivered("chat:1", first.bridge_id)
    await repository.add_thread_message(
        first.bridge_id,
        role="user",
        kind="prompt",
        text="new webhook prompt",
    )
    await repository.add_thread_message(
        first.bridge_id,
        role="assistant",
        kind="final",
        text="new webhook answer",
    )

    entries = await service.focus_final_messages("chat:1", first.bridge_id)

    assert [(entry.message.text, entry.repeated) for entry in entries] == [
        ("new webhook answer", False),
    ]


@pytest.mark.asyncio
async def test_thread_focus_replay_returns_latest_message_after_watermark(
    tmp_path: Path,
) -> None:
    repository = SQLiteStateRepository(tmp_path / "state.db")
    service = BotService(_build_config(), repository, AsyncMock())
    await repository.initialize()
    anchor = await repository.upsert_conversation_anchor(
        chat_key="chat:1",
        codex_backend_id="laptop",
        codex_thread_id="codex-1",
        title="Existing Codex thread",
    )
    bridge = await repository.create_bridge(
        chat_key="chat:1",
        bridge_id="bridge-1",
        title="Existing Codex thread",
        anchor_id=anchor.anchor_id,
        codex_backend_id="laptop",
    )
    await repository.add_thread_message(
        bridge.bridge_id,
        role="assistant",
        kind="final",
        text="latest delivered answer",
    )
    await service.mark_thread_messages_delivered("chat:1", bridge.bridge_id)

    entries = await service.focus_final_messages("chat:1", bridge.bridge_id)

    assert [(entry.message.text, entry.repeated) for entry in entries] == [
        ("latest delivered answer", True)
    ]


@pytest.mark.asyncio
async def test_thread_delivery_watermark_is_scoped_to_bridge_window(
    tmp_path: Path,
) -> None:
    repository = SQLiteStateRepository(tmp_path / "state.db")
    service = BotService(_build_config(), repository, AsyncMock())
    await repository.initialize()
    anchor = await repository.upsert_conversation_anchor(
        chat_key="chat:1",
        codex_backend_id="laptop",
        codex_thread_id="codex-1",
        title="Existing Codex thread",
    )
    first = await repository.create_bridge(
        chat_key="chat:1",
        bridge_id="bridge-1",
        title="Existing Codex thread",
        anchor_id=anchor.anchor_id,
        codex_backend_id="laptop",
    )
    await repository.add_thread_message(
        first.bridge_id,
        role="assistant",
        kind="final",
        text="older bridge answer",
    )
    second = await repository.create_bridge(
        chat_key="chat:1",
        bridge_id="bridge-2",
        title="Existing Codex thread",
        anchor_id=anchor.anchor_id,
        codex_backend_id="laptop",
    )
    await repository.add_thread_message(
        second.bridge_id,
        role="assistant",
        kind="final",
        text="newer bridge answer",
    )
    await service.mark_thread_messages_delivered("chat:1", second.bridge_id)

    entries = await service.focus_final_messages("chat:1", first.bridge_id)

    assert [(entry.message.text, entry.repeated) for entry in entries] == [
        ("older bridge answer", False)
    ]


@pytest.mark.asyncio
async def test_new_thread_resolves_connection_name_to_backend_id(
    tmp_path: Path,
) -> None:
    repository = SQLiteStateRepository(tmp_path / "state.db")
    service = BotService(
        _build_config(),
        repository,
        cast(CodexBackend, _BackendWithFriendlyNames()),
    )
    await repository.initialize()

    created = await service.new_thread("chat:1", connection_name="laptop")

    assert created.codex_backend_id == "backend-laptop"


@pytest.mark.asyncio
async def test_new_thread_rejects_ambiguous_project_selector(
    tmp_path: Path,
) -> None:
    repository = SQLiteStateRepository(tmp_path / "state.db")
    service = BotService(_build_config(), repository, AsyncMock())
    await repository.initialize()
    await repository.upsert_project(
        connection_id="home",
        root_path="/agent/home",
        label="agent",
    )
    await repository.upsert_project(
        connection_id="laptop",
        root_path="/agent/laptop",
        label="agent",
    )

    with pytest.raises(ValueError, match="Ambiguous project"):
        await service.new_thread("chat:1", project_selector="agent")


@pytest.mark.asyncio
async def test_run_turn_rejects_project_connection_mismatch(
    tmp_path: Path,
) -> None:
    repository = SQLiteStateRepository(tmp_path / "state.db")
    service = BotService(_build_config(), repository, AsyncMock())
    await repository.initialize()
    thread = await service.ensure_active_thread("chat:1")
    project = await repository.upsert_project(
        connection_id="home",
        root_path="/agent/home",
        label="home",
    )
    await repository.bind_thread_project(thread.thread_id, project.project_id)
    await repository.update_codex_thread_binding(
        thread.thread_id,
        "codex-1",
        codex_backend_id="laptop",
    )

    with pytest.raises(ValueError, match="Project connection mismatch"):
        await service.run_turn("chat:1", "hello")


@pytest.mark.asyncio
async def test_list_codex_threads_adds_current_chat_anchor_status(
    tmp_path: Path,
) -> None:
    repository = SQLiteStateRepository(tmp_path / "state.db")
    client = AsyncMock()
    client.list_codex_threads.return_value = [
        CodexThread(
            thread_id="codex-1",
            cwd="/agent/project-a",
            title="One",
            preview=None,
            status="idle",
            created_at=1710000000,
            updated_at=1710000300,
            model_provider="openai",
        ),
        CodexThread(
            thread_id="codex-2",
            cwd="/agent/project-a",
            title="Two",
            preview=None,
            status="idle",
            created_at=1710000000,
            updated_at=1710000200,
            model_provider="openai",
        ),
    ]
    service = BotService(_build_config(), repository, client)
    await repository.initialize()
    thread = await service.new_thread("chat:1")
    await repository.update_codex_thread_binding(
        thread.thread_id,
        "codex-1",
        codex_backend_id="primary",
    )

    listing = await service.list_codex_threads("chat:1")
    grouped = listing.groups

    assert [group.project for group in grouped] == ["/agent/project-a"]
    assert [(item.thread_id, item.anchor_status) for item in grouped[0].threads] == [
        ("codex-1", "focused"),
        ("codex-2", "unlinked"),
    ]


@pytest.mark.asyncio
async def test_list_recent_codex_threads_returns_five_total_across_backends(
    tmp_path: Path,
) -> None:
    repository = SQLiteStateRepository(tmp_path / "state.db")
    client = AsyncMock()
    client.list_codex_threads.return_value = [
        CodexThread(
            thread_id=f"home-{index}",
            cwd="/agent/home",
            title=f"Home {index}",
            preview=None,
            status="idle",
            created_at=1710000000 + index,
            updated_at=1710000000 + index,
            model_provider="openai",
            codex_backend_id="home",
            codex_backend_name="Home",
        )
        for index in range(4)
    ] + [
        CodexThread(
            thread_id=f"mac-{index}",
            cwd="/agent/mac",
            title=f"Mac {index}",
            preview=None,
            status="idle",
            created_at=1710000100 + index,
            updated_at=1710000100 + index,
            model_provider="openai",
            codex_backend_id="mac",
            codex_backend_name="Mac",
        )
        for index in range(4)
    ]
    service = BotService(_build_config(), repository, client)
    await repository.initialize()

    listing = await service.list_recent_codex_threads("chat:1", limit=5)
    threads = [thread for group in listing.groups for thread in group.threads]

    client.list_codex_threads.assert_awaited_once_with(
        search=None,
        limit=50,
        backend_id=None,
        backend_name=None,
        include_all=True,
    )
    assert [thread.thread_id for thread in threads] == [
        "mac-3",
        "mac-2",
        "mac-1",
        "mac-0",
        "home-3",
    ]


@pytest.mark.asyncio
async def test_list_codex_threads_uses_primary_backend_by_default(
    tmp_path: Path,
) -> None:
    repository = SQLiteStateRepository(tmp_path / "state.db")
    client = AsyncMock()
    client.list_codex_threads.return_value = [
        CodexThread(
            thread_id="codex-1",
            cwd="/agent/project-a",
            title="One",
            preview=None,
            status="idle",
            created_at=1710000000,
            updated_at=1710000300,
            model_provider="openai",
            codex_backend_id="home",
            codex_backend_name="home",
        )
    ]
    service = BotService(_build_config(), repository, client)
    await repository.initialize()

    listing = await service.list_codex_threads("chat:1")

    client.list_codex_threads.assert_awaited_once_with(
        search=None,
        limit=50,
        backend_id=None,
        backend_name=None,
        include_all=False,
    )
    assert isinstance(listing, CodexThreadListResult)
    assert listing.groups[0].backend_name == "home"


@pytest.mark.asyncio
async def test_list_codex_threads_limits_to_five_recent_projects_per_backend(
    tmp_path: Path,
) -> None:
    repository = SQLiteStateRepository(tmp_path / "state.db")
    client = AsyncMock()
    client.list_codex_threads.return_value = [
        CodexThread(
            thread_id=f"codex-{index}",
            cwd=f"/agent/project-{index}",
            title=f"Thread {index}",
            preview=None,
            status="idle",
            created_at=1710000000 + index,
            updated_at=1710000300 + index,
            model_provider="openai",
            codex_backend_id="home",
            codex_backend_name="home",
        )
        for index in range(6)
    ]
    service = BotService(_build_config(), repository, client)
    await repository.initialize()

    listing = await service.list_codex_threads("chat:1")

    assert [group.project for group in listing.groups] == [
        "/agent/project-5",
        "/agent/project-4",
        "/agent/project-3",
        "/agent/project-2",
        "/agent/project-1",
    ]


@pytest.mark.asyncio
async def test_expire_idle_bridges_unfocuses_stale_focused_bridge(
    tmp_path: Path,
) -> None:
    repository = SQLiteStateRepository(tmp_path / "state.db")
    service = BotService(_build_config(), repository, AsyncMock())
    await repository.initialize()

    original = await service.ensure_active_thread("chat:1")
    await repository.mark_turn_started(original.thread_id, "turn-old")
    await repository.mark_turn_completed(original.thread_id)
    stale_focus_time = (datetime.now(UTC) - timedelta(seconds=901)).isoformat()

    async with aiosqlite.connect(repository._path) as db:
        await db.execute(
            """
            UPDATE bridge_threads
               SET updated_at = ?
             WHERE bridge_id = ?
            """,
            (stale_focus_time, original.thread_id),
        )
        await db.execute(
            """
            UPDATE threads
               SET updated_at = ?
             WHERE thread_id = ?
            """,
            (stale_focus_time, original.thread_id),
        )
        await db.commit()

    expired = await service.expire_idle_bridges()
    active = await repository.get_active_thread("chat:1")
    focused = await repository.get_focused_bridge("chat:1")
    bridge = await repository.get_bridge(original.thread_id)

    assert expired == [original.thread_id]
    assert active is not None
    assert active.thread_id == original.thread_id
    assert focused is None
    assert bridge is not None
    assert bridge.closed_at is None


@pytest.mark.asyncio
async def test_expire_idle_bridges_closes_waiting_bridge_after_card_ttl(
    tmp_path: Path,
) -> None:
    repository = SQLiteStateRepository(tmp_path / "state.db")
    service = BotService(_build_config(), repository, AsyncMock())
    await repository.initialize()

    original = await service.ensure_active_thread("chat:1")
    await repository.mark_turn_started(original.thread_id, "turn-old")
    await repository.mark_turn_completed(original.thread_id)
    stale_time = (datetime.now(UTC) - timedelta(seconds=3601)).isoformat()

    async with aiosqlite.connect(repository._path) as db:
        await db.execute(
            "UPDATE bridge_threads SET expires_at = ? WHERE bridge_id = ?",
            (stale_time, original.thread_id),
        )
        await db.commit()

    expired = await service.expire_idle_bridges()
    bridge = await repository.get_bridge(original.thread_id)

    assert expired == [original.thread_id]
    assert bridge is not None
    assert bridge.closed_at is not None


@pytest.mark.asyncio
async def test_plain_turn_after_focus_expiry_starts_default_project_thread(
    tmp_path: Path,
) -> None:
    repository = SQLiteStateRepository(tmp_path / "state.db")
    client = AsyncMock()
    client.ensure_thread_binding.return_value = ThreadBindingResult(
        codex_thread_id="codex-default",
        codex_backend_id="laptop",
        remapped=False,
    )
    client.start_turn.return_value = TurnAccepted(
        turn_id="turn-default",
        chat_key="chat:1",
        logical_thread_id="ignored",
        codex_thread_id="codex-default",
        codex_backend_id="laptop",
        remapped=False,
    )
    client.wait_for_turn_event.return_value = TurnResult(
        turn_id="turn-default",
        chat_key="chat:1",
        logical_thread_id="ignored",
        codex_thread_id="codex-default",
        status="completed",
        final_text="Default project reply.",
    )
    service = BotService(_build_config_with_default_project(), repository, client)
    await repository.initialize()
    old_project = await repository.upsert_project(
        connection_id="primary",
        root_path="/agent/old",
        label="old",
    )
    original = await service.new_thread_in_project("chat:1", old_project.project_id)
    stale_focus_time = (datetime.now(UTC) - timedelta(seconds=901)).isoformat()

    async with aiosqlite.connect(repository._path) as db:
        await db.execute(
            """
            UPDATE bridge_threads
               SET updated_at = ?
             WHERE bridge_id = ?
            """,
            (stale_focus_time, original.thread_id),
        )
        await db.execute(
            """
            UPDATE threads
               SET updated_at = ?
             WHERE thread_id = ?
            """,
            (stale_focus_time, original.thread_id),
        )
        await db.commit()
    await service.expire_idle_bridges()

    run_result = await service.run_turn("chat:1", "Hello after wrap-up")
    active = await repository.get_active_thread("chat:1")
    focused = await repository.get_focused_bridge("chat:1")
    assert run_result.result is not None
    assert active is not None
    assert focused is not None
    assert active.thread_id == focused.bridge_id
    assert active.thread_id != original.thread_id
    original_bridge = await repository.get_bridge(original.thread_id)
    assert original_bridge is not None
    assert original_bridge.closed_at is None

    project = await repository.get_thread_project(active.thread_id)
    assert project is not None
    assert project.connection_id == "laptop"
    assert project.root_path == "/agent/app"
    assert (
        client.ensure_thread_binding.await_args.kwargs["codex_backend_id"] == "laptop"
    )


@pytest.mark.asyncio
async def test_expire_idle_bridges_does_not_close_waiting_bridge(
    tmp_path: Path,
) -> None:
    repository = SQLiteStateRepository(tmp_path / "state.db")
    service = BotService(_build_config(), repository, AsyncMock())
    await repository.initialize()

    original = await service.ensure_active_thread("chat:1")
    await repository.mark_turn_started(original.thread_id, "turn-old")
    stale_time = (datetime.now(UTC) - timedelta(seconds=901)).isoformat()

    async with aiosqlite.connect(repository._path) as db:
        await db.execute(
            "UPDATE bridge_threads SET expires_at = ? WHERE bridge_id = ?",
            (stale_time, original.thread_id),
        )
        await db.commit()

    expired = await service.expire_idle_bridges()
    bridge = await repository.get_bridge(original.thread_id)

    assert expired == []
    assert bridge is not None
    assert bridge.closed_at is None


@pytest.mark.asyncio
async def test_webhook_event_acceptance_verifies_secret_and_normalizes_payload(
    tmp_path: Path,
) -> None:
    repository = SQLiteStateRepository(tmp_path / "state.db")
    service = BotService(_build_config(), repository, AsyncMock())
    await repository.initialize()
    anchor = await repository.upsert_conversation_anchor(
        chat_key="chat:1",
        codex_backend_id="primary",
        codex_thread_id="codex-1",
        title="front-door",
    )
    created = await service.create_webhook_subscription(
        chat_key="chat:1",
        anchor_id=anchor.anchor_id,
        name="front-door",
    )

    with pytest.raises(PermissionError):
        await service.accept_webhook_event(
            created.subscription.webhook_id,
            "wrong-secret",
            {"input": "door opened"},
        )

    dispatch = await service.accept_webhook_event(
        created.subscription.webhook_id,
        created.event_secret,
        {
            "input": "door opened",
            "metadata": {"source": "sensor"},
            "payload": {"state": "open"},
        },
        idempotency_key="event-1",
    )

    assert dispatch.duplicate is False
    assert dispatch.subscription.trigger_count == 1
    assert "Webhook: front-door" in dispatch.prompt
    assert "Human input:\ndoor opened" in dispatch.prompt
    assert '"state": "open"' in dispatch.prompt

    duplicate = await service.accept_webhook_event(
        created.subscription.webhook_id,
        created.event_secret,
        {"input": "door opened again"},
        idempotency_key="event-1",
    )

    assert duplicate.duplicate is True

    assert await repository.disable_webhook_subscription(
        created.subscription.webhook_id
    )
    with pytest.raises(PermissionError):
        await service.accept_webhook_event(
            created.subscription.webhook_id,
            created.event_secret,
            {"input": "door opened"},
        )


@pytest.mark.asyncio
async def test_run_turn_notifies_running_state_before_waiting_for_events(
    tmp_path: Path,
) -> None:
    repository = SQLiteStateRepository(tmp_path / "state.db")
    events: list[str] = []
    client = AsyncMock()
    client.ensure_thread_binding.return_value = ThreadBindingResult(
        codex_thread_id="codex-1",
        remapped=False,
    )
    client.start_turn.return_value = TurnAccepted(
        turn_id="turn-1",
        chat_key="chat:1",
        logical_thread_id="thread-1",
        codex_thread_id="codex-1",
        remapped=False,
    )

    async def wait_for_turn_event(*args, **kwargs) -> TurnResult:
        events.append("wait_for_turn_event")
        return TurnResult(
            turn_id="turn-1",
            chat_key="chat:1",
            logical_thread_id="thread-1",
            codex_thread_id="codex-1",
            status="completed",
            final_text="Done.",
        )

    async def on_state_change() -> None:
        active = await repository.get_active_thread("chat:1")
        assert active is not None
        events.append(f"state:{active.pending_turn_id or 'idle'}")

    client.wait_for_turn_event.side_effect = wait_for_turn_event
    service = BotService(_build_config(), repository, client)
    await repository.initialize()

    await service.run_turn(
        "chat:1",
        "Hello from Telegram",
        on_state_change=on_state_change,
    )

    assert events[:2] == ["state:turn-1", "wait_for_turn_event"]
    assert events[-1] == "state:idle"


@pytest.mark.asyncio
async def test_run_turn_announces_idle_rollover_before_backend_turn(
    tmp_path: Path,
) -> None:
    repository = SQLiteStateRepository(tmp_path / "state.db")
    events: list[str] = []
    client = AsyncMock()
    client.ensure_thread_binding.return_value = ThreadBindingResult(
        codex_thread_id="codex-2",
        remapped=False,
    )

    async def start_turn(*args, **kwargs) -> TurnAccepted:
        events.append("start_turn")
        return TurnAccepted(
            turn_id="turn-2",
            chat_key="chat:1",
            logical_thread_id="ignored",
            codex_thread_id="codex-2",
            remapped=False,
        )

    async def announce_notice(notice: str) -> None:
        events.append(f"notice:{notice}")

    client.start_turn.side_effect = start_turn
    client.wait_for_turn_event.return_value = TurnResult(
        turn_id="turn-2",
        chat_key="chat:1",
        logical_thread_id="ignored",
        codex_thread_id="codex-2",
        status="completed",
        final_text="Fresh thread reply.",
    )
    service = BotService(_build_config(), repository, client)
    await repository.initialize()

    original = await service.ensure_active_thread("chat:1")
    await repository.mark_turn_started(original.thread_id, "turn-1")
    await repository.mark_turn_completed(original.thread_id)
    stale_time = (datetime.now(UTC) - timedelta(seconds=901)).isoformat()

    async with aiosqlite.connect(repository._path) as db:
        await db.execute(
            "UPDATE bridge_threads SET expires_at = ?, updated_at = ? WHERE bridge_id = ?",
            (stale_time, stale_time, original.thread_id),
        )
        await db.commit()

    run_result = await service.run_turn("chat:1", "Hello after idle")

    assert events == ["start_turn"]
    assert run_result.result is not None


@pytest.mark.asyncio
async def test_run_turn_keeps_blank_thread_even_if_creation_is_old(
    tmp_path: Path,
) -> None:
    repository = SQLiteStateRepository(tmp_path / "state.db")
    client = AsyncMock()
    client.ensure_thread_binding.return_value = ThreadBindingResult(
        codex_thread_id="codex-1",
        remapped=False,
    )
    client.start_turn.return_value = TurnAccepted(
        turn_id="turn-1",
        chat_key="chat:1",
        logical_thread_id="ignored",
        codex_thread_id="codex-1",
        remapped=False,
    )
    client.wait_for_turn_event.return_value = TurnResult(
        turn_id="turn-1",
        chat_key="chat:1",
        logical_thread_id="ignored",
        codex_thread_id="codex-1",
        status="completed",
        final_text="First reply.",
    )
    service = BotService(_build_config(), repository, client)
    await repository.initialize()

    original = await service.ensure_active_thread("chat:1")
    stale_time = (datetime.now(UTC) - timedelta(seconds=901)).isoformat()

    async with aiosqlite.connect(repository._path) as db:
        await db.execute(
            """
            UPDATE threads
               SET created_at = ?,
                   updated_at = ?
             WHERE thread_id = ?
            """,
            (stale_time, stale_time, original.thread_id),
        )
        await db.commit()

    run_result = await service.run_turn("chat:1", "First message")
    active = await repository.get_active_thread("chat:1")

    assert active is not None
    assert active.thread_id == original.thread_id
    assert run_result.result is not None


@pytest.mark.asyncio
async def test_run_turn_can_resume_idle_thread_without_rollover(tmp_path: Path) -> None:
    repository = SQLiteStateRepository(tmp_path / "state.db")
    client = AsyncMock()
    client.ensure_thread_binding.return_value = ThreadBindingResult(
        codex_thread_id="codex-1",
        remapped=False,
    )
    client.start_turn.return_value = TurnAccepted(
        turn_id="turn-2",
        chat_key="chat:1",
        logical_thread_id="ignored",
        codex_thread_id="codex-1",
        remapped=False,
    )
    client.wait_for_turn_event.return_value = TurnResult(
        turn_id="turn-2",
        chat_key="chat:1",
        logical_thread_id="ignored",
        codex_thread_id="codex-1",
        status="completed",
        final_text="Resumed reply.",
    )
    service = BotService(_build_config(), repository, client)
    await repository.initialize()

    original = await service.ensure_active_thread("chat:1")
    await repository.mark_turn_started(original.thread_id, "turn-1")
    await repository.mark_turn_completed(original.thread_id)
    stale_time = (datetime.now(UTC) - timedelta(seconds=901)).isoformat()

    async with aiosqlite.connect(repository._path) as db:
        await db.execute(
            "UPDATE threads SET updated_at = ? WHERE thread_id = ?",
            (stale_time, original.thread_id),
        )
        await db.commit()

    run_result = await service.run_turn("chat:1", "Resume this conversation")
    active = await repository.get_active_thread("chat:1")

    assert active is not None
    assert active.thread_id == original.thread_id
    assert run_result.result is not None
    history = await repository.list_thread_messages(original.thread_id, limit=10)
    assert [(entry.role, entry.kind, entry.text) for entry in history] == [
        ("user", "prompt", "Resume this conversation"),
        ("assistant", "final", "Resumed reply."),
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "error", "kind"),
    [
        ("failed", "backend failed", "error"),
        ("interrupted", "turn interrupted", "interrupted"),
    ],
)
async def test_run_turn_persists_terminal_error_states(
    tmp_path: Path,
    status: str,
    error: str,
    kind: str,
) -> None:
    repository = SQLiteStateRepository(tmp_path / "state.db")
    client = AsyncMock()
    client.ensure_thread_binding.return_value = ThreadBindingResult(
        codex_thread_id="codex-1",
        remapped=False,
    )
    client.start_turn.return_value = TurnAccepted(
        turn_id="turn-1",
        chat_key="chat:1",
        logical_thread_id="ignored",
        codex_thread_id="codex-1",
        remapped=False,
    )
    client.wait_for_turn_event.return_value = TurnResult(
        turn_id="turn-1",
        chat_key="chat:1",
        logical_thread_id="ignored",
        codex_thread_id="codex-1",
        status=status,
        final_text="",
        error=error,
    )

    service = BotService(_build_config(), repository, client)
    await repository.initialize()

    run = await service.run_turn("chat:1", "Hello from Telegram")
    thread = await repository.get_active_thread("chat:1")
    assert thread is not None
    history = await repository.list_thread_messages(
        thread.thread_id,
        limit=10,
    )

    assert [(entry.role, entry.kind, entry.text) for entry in history] == [
        ("user", "prompt", "Hello from Telegram"),
        ("system", kind, error),
    ]


@pytest.mark.asyncio
async def test_directory_commands_resolve_history_and_reset(tmp_path: Path) -> None:
    repository = SQLiteStateRepository(tmp_path / "state.db")
    client = AsyncMock()
    service = BotService(
        _build_config(),
        repository,
        client,
        directory_resolver=LocalDirectoryResolver(),
    )
    await repository.initialize()
    await service.ensure_active_thread("chat:1")

    workspace = tmp_path / "workspace"
    nested = workspace / "nested"
    nested.mkdir(parents=True)

    state = await service.set_directory("chat:1", str(workspace))
    assert state.current_path == str(workspace.resolve())

    state = await service.set_directory("chat:1", "nested")
    assert state.current_path == str(nested.resolve())

    state = await service.switch_previous_directory("chat:1")
    assert state.current_path == str(workspace.resolve())

    state = await service.switch_directory_from_history("chat:1", 2)
    assert state.current_path == str(nested.resolve())

    state = await service.reset_directory("chat:1")
    assert state.current_path == ""


@pytest.mark.asyncio
async def test_project_binding_sets_effective_directory_precedence(
    tmp_path: Path,
) -> None:
    repository = SQLiteStateRepository(tmp_path / "state.db")
    client = AsyncMock()
    service = BotService(
        _build_config(),
        repository,
        client,
        directory_resolver=LocalDirectoryResolver(),
    )
    await repository.initialize()
    thread = await service.ensure_active_thread("chat:1")

    project_root = tmp_path / "project"
    nested = project_root / "nested"
    nested.mkdir(parents=True)

    project_state = await service.add_project("chat:1", str(project_root))
    assert project_state.active is not None
    settings = await service.get_settings(thread.thread_id, "chat:1")
    assert settings.cwd == str(project_root.resolve())

    await service.set_directory("chat:1", "nested")
    settings = await service.get_settings(thread.thread_id, "chat:1")
    assert settings.cwd == str(nested.resolve())

    await service.reset_directory("chat:1")
    settings = await service.get_settings(thread.thread_id, "chat:1")
    assert settings.cwd == str(project_root.resolve())

    await service.unbind_project("chat:1")
    settings = await service.get_settings(thread.thread_id, "chat:1")
    assert settings.cwd == ""


@pytest.mark.asyncio
async def test_webhook_thread_targets_requested_thread(tmp_path: Path) -> None:
    repository = SQLiteStateRepository(tmp_path / "state.db")
    client = AsyncMock()
    service = BotService(_build_config(), repository, client)
    await repository.initialize()
    first = await service.ensure_active_thread("chat:1")
    second = await service.new_thread("chat:1")
    await service.focus_bridge("chat:1", first.thread_id)

    selected = await service.resolve_bridge("chat:1", second.thread_id, focus=False)
    active = await repository.get_active_thread("chat:1")

    assert selected.thread is not None
    assert selected.thread.thread_id == second.thread_id
    assert active is not None
    assert active.thread_id == first.thread_id
    assert first.thread_id != second.thread_id


@pytest.mark.asyncio
async def test_run_turn_can_target_dormant_thread_without_selecting_it(
    tmp_path: Path,
) -> None:
    repository = SQLiteStateRepository(tmp_path / "state.db")
    client = AsyncMock()
    client.ensure_thread_binding.return_value = ThreadBindingResult(
        codex_thread_id="codex-1",
        remapped=False,
    )
    client.start_turn.return_value = TurnAccepted(
        turn_id="turn-1",
        chat_key="chat:1",
        logical_thread_id="ignored",
        codex_thread_id="codex-1",
        remapped=False,
    )
    client.wait_for_turn_event.return_value = TurnResult(
        turn_id="turn-1",
        chat_key="chat:1",
        logical_thread_id="ignored",
        codex_thread_id="codex-1",
        status="completed",
        final_text="Dormant reply.",
    )
    service = BotService(_build_config(), repository, client)
    await repository.initialize()
    dormant = await service.ensure_active_thread("chat:1")
    active = await service.new_thread("chat:1")

    run_result = await service.run_turn(
        "chat:1",
        "External event",
        thread_id=dormant.thread_id,
    )
    current = await repository.get_active_thread("chat:1")

    assert current is not None
    assert current.thread_id == active.thread_id
    assert run_result.result is not None
    assert run_result.result.final_text == "Dormant reply."
