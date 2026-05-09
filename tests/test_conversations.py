from pathlib import Path
from typing import cast

import pytest

from codex_telegram.adapters.persistence.sqlite import SQLiteStateRepository
from codex_telegram.application.conversations import (
    ConversationLifecycleConfig,
    ConversationLifecycleService,
    DefaultProjectConfig,
)
from codex_telegram.application.ports import CodexBackend
from codex_telegram.application.profiles import build_profiles
from codex_telegram.application.settings import (
    RuntimeSettingsPolicy,
    RuntimeSettingsService,
)
from codex_telegram.config import ProfileConfig
from codex_telegram.domain import CodexThread


class _Backend:
    def __init__(self, threads: dict[tuple[str, str], CodexThread] | None = None):
        self._threads = threads or {}

    def resolve_backend_id(
        self,
        *,
        backend_id: str | None = None,
        backend_name: str | None = None,
    ) -> str:
        if backend_id is not None:
            return backend_id
        if backend_name == "Laptop":
            return "laptop"
        return backend_name or "primary"

    async def get_codex_thread(
        self,
        thread_id: str,
        *,
        backend_id: str | None = None,
        backend_name: str | None = None,
    ) -> CodexThread:
        key = (
            self.resolve_backend_id(backend_id=backend_id, backend_name=backend_name),
            thread_id,
        )
        return self._threads[key]


def _settings(
    repository: SQLiteStateRepository,
    backend: _Backend,
) -> RuntimeSettingsService:
    return RuntimeSettingsService(
        RuntimeSettingsPolicy(
            default_profile="operator",
            client_default_profiles={},
            client_allowed_projects={},
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
        ),
        repository,
        cast(CodexBackend, backend),
    )


def _service(
    repository: SQLiteStateRepository,
    backend: _Backend,
    *,
    default_project: DefaultProjectConfig | None = None,
    focus_timeout_seconds: float = 30.0,
    active_waiting_ttl_seconds: float = 30.0,
) -> ConversationLifecycleService:
    return ConversationLifecycleService(
        ConversationLifecycleConfig(
            focus_timeout_seconds=focus_timeout_seconds,
            active_waiting_ttl_seconds=active_waiting_ttl_seconds,
            default_project=default_project,
        ),
        repository,
        cast(CodexBackend, backend),
        _settings(repository, backend),
    )


@pytest.mark.asyncio
async def test_new_thread_in_default_project_creates_project_binding(
    tmp_path: Path,
) -> None:
    repository = SQLiteStateRepository(tmp_path / "state.db")
    await repository.initialize()
    project_root = tmp_path / "workspace"
    service = _service(
        repository,
        _Backend(),
        default_project=DefaultProjectConfig(
            connection="Laptop",
            root_path=str(project_root),
        ),
    )

    thread = await service.new_thread_in_default_project("chat:1")

    project = await repository.get_thread_project(thread.thread_id)
    assert project is not None
    assert project.connection_id == "laptop"
    assert project.root_path == str(project_root)
    assert project.label == "workspace"
    updated_thread = await repository.get_thread(thread.thread_id)
    assert updated_thread is not None
    assert updated_thread.codex_backend_id == "laptop"


@pytest.mark.asyncio
async def test_attach_codex_thread_creates_anchor_bridge_and_project(
    tmp_path: Path,
) -> None:
    codex_thread = CodexThread(
        thread_id="codex-1",
        cwd=str(tmp_path / "project"),
        title="Existing backend work",
        preview=None,
        status="idle",
        created_at=10,
        updated_at=20,
        model_provider="openai",
        codex_backend_id="laptop",
        codex_backend_name="Laptop",
    )
    repository = SQLiteStateRepository(tmp_path / "state.db")
    await repository.initialize()
    service = _service(
        repository,
        _Backend({("laptop", "codex-1"): codex_thread}),
    )

    attachment = await service.attach_codex_thread(
        "chat:1",
        "codex-1",
        backend_id="laptop",
    )

    assert attachment.anchor.codex_thread_id == "codex-1"
    assert attachment.anchor.latest_bridge_id == attachment.bridge.bridge_id
    assert attachment.bridge.codex_thread_id == "codex-1"
    assert attachment.bridge.anchor_id == attachment.anchor.anchor_id
    project = await repository.get_thread_project(attachment.bridge.bridge_id)
    assert project is not None
    assert project.connection_id == "laptop"
    assert project.root_path == str(tmp_path / "project")


@pytest.mark.asyncio
async def test_focus_bridge_revives_expired_anchor_without_refocusing_when_resolved(
    tmp_path: Path,
) -> None:
    codex_thread = CodexThread(
        thread_id="codex-1",
        cwd=None,
        title="Long running conversation",
        preview=None,
        status="idle",
        created_at=10,
        updated_at=20,
        model_provider="openai",
        codex_backend_id="primary",
        codex_backend_name="primary",
    )
    repository = SQLiteStateRepository(tmp_path / "state.db")
    await repository.initialize()
    service = _service(
        repository,
        _Backend({("primary", "codex-1"): codex_thread}),
        active_waiting_ttl_seconds=0.0,
    )
    attachment = await service.attach_codex_thread(
        "chat:1",
        "codex-1",
        backend_id="primary",
    )
    other = await service.new_thread("chat:1")

    resolved = await service.resolve_bridge(
        "chat:1",
        attachment.anchor.anchor_id,
        focus=False,
    )

    assert resolved.success is True
    assert resolved.thread is not None
    assert resolved.thread.anchor_id == attachment.anchor.anchor_id
    assert resolved.thread.thread_id != attachment.bridge.bridge_id
    focused = await repository.get_active_thread("chat:1")
    assert focused is not None
    assert focused.thread_id == other.thread_id
