from pathlib import Path
from typing import cast

import pytest

from codex_telegram.adapters.persistence.sqlite import SQLiteStateRepository
from codex_telegram.application.ports import CodexBackend
from codex_telegram.application.profiles import build_profiles
from codex_telegram.application.settings import (
    RuntimeSettingsPolicy,
    RuntimeSettingsService,
)
from codex_telegram.config import ProfileConfig
from codex_telegram.domain import SessionOverrides


class _Backend:
    def resolve_backend_id(
        self,
        *,
        backend_id: str | None = None,
        backend_name: str | None = None,
    ) -> str:
        return backend_id or backend_name or "primary"


def _service(repository: SQLiteStateRepository) -> RuntimeSettingsService:
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
        cast(CodexBackend, _Backend()),
    )


@pytest.mark.asyncio
async def test_effective_settings_use_project_cwd_until_explicit_override(
    tmp_path: Path,
) -> None:
    repository = SQLiteStateRepository(tmp_path / "state.db")
    await repository.initialize()
    await repository.create_thread("chat:1", "thread-1", "Thread")
    project = await repository.upsert_project(
        connection_id="primary",
        root_path=str(tmp_path),
        label="work",
    )
    await repository.bind_thread_project("thread-1", project.project_id)
    service = _service(repository)

    settings = await service.get_settings("thread-1", "chat:1")
    assert settings.cwd == str(tmp_path)

    override_path = tmp_path / "nested"
    override_path.mkdir()
    settings = await service.update_override("thread-1", "cwd", str(override_path))
    assert settings.cwd == str(override_path)


@pytest.mark.asyncio
async def test_project_runtime_overrides_restore_to_new_thread(tmp_path: Path) -> None:
    repository = SQLiteStateRepository(tmp_path / "state.db")
    await repository.initialize()
    await repository.create_thread("chat:1", "thread-1", "Thread 1")
    await repository.create_thread("chat:1", "thread-2", "Thread 2")
    project = await repository.upsert_project(
        connection_id="primary",
        root_path=str(tmp_path),
        label="work",
    )
    await repository.bind_thread_project("thread-1", project.project_id)
    await repository.bind_thread_project("thread-2", project.project_id)
    service = _service(repository)

    await service.update_override("thread-1", "model", "gpt-5.4-mini")
    await service.update_override("thread-1", "effort", "high")
    await service.set_fast_mode("thread-1", True)
    await service.restore_project_runtime_overrides("thread-2", project.project_id)

    restored = await repository.get_overrides("thread-2")
    assert restored.model == "gpt-5.4-mini"
    assert restored.effort == "high"
    assert restored.fast_mode is True


@pytest.mark.asyncio
async def test_effective_settings_inherit_project_runtime_defaults(
    tmp_path: Path,
) -> None:
    repository = SQLiteStateRepository(tmp_path / "state.db")
    await repository.initialize()
    await repository.create_thread("chat:1", "thread-1", "Thread")
    project = await repository.upsert_project(
        connection_id="primary",
        root_path=str(tmp_path),
        label="work",
    )
    await repository.bind_thread_project("thread-1", project.project_id)
    await repository.upsert_project_overrides(
        project.project_id,
        overrides=SessionOverrides(
            model="gpt-5.4-mini",
            effort="high",
            fast_mode=True,
        ),
    )
    service = _service(repository)

    settings = await service.get_settings("thread-1", "chat:1")

    assert settings.model == "gpt-5.4-mini"
    assert settings.effort == "high"
    assert settings.fast_mode is True
