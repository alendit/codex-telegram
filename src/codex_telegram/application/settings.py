"""Runtime settings and Project selection application service."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal, Protocol

from codex_telegram.application.models import EffectiveSettings
from codex_telegram.application.ports import CodexBackend
from codex_telegram.application.profiles import (
    COMMAND_VERBOSITY_VALUES,
    FOLLOWUP_MODE_VALUES,
    VERBOSITY_VALUES,
    canonical_profile_name,
    default_profile_for_chat,
    resolve_profile,
)
from codex_telegram.domain import (
    LogicalThread,
    Project,
    ProfileDefinition,
    SessionOverrides,
)

OverrideFieldName = Literal[
    "profile",
    "model",
    "effort",
    "summary",
    "cwd",
    "verbosity",
    "command_verbosity",
    "followup_mode",
    "collaboration_mode",
]

COLLABORATION_MODE_VALUES = {"plan", "default"}


@dataclass(frozen=True, slots=True)
class RuntimeSettingsPolicy:
    """Profile defaults used to resolve effective runtime settings."""

    default_profile: str
    client_default_profiles: Mapping[str, str]
    profiles: Mapping[str, ProfileDefinition]
    client_allowed_projects: Mapping[str, tuple["ProjectAccessRule", ...]]


@dataclass(frozen=True, slots=True)
class ProjectAccessRule:
    """One exact project root a configured chat is allowed to use."""

    connection: str
    root_path: str


class RuntimeSettingsRepository(Protocol):
    """State needed by profile, override, and Project selection policy."""

    async def get_thread(self, thread_id: str) -> LogicalThread | None: ...
    async def get_active_thread(self, chat_key: str) -> LogicalThread | None: ...
    async def get_overrides(self, thread_id: str) -> SessionOverrides: ...
    async def upsert_overrides(
        self, thread_id: str, overrides: SessionOverrides
    ) -> SessionOverrides: ...
    async def clear_overrides(self, thread_id: str) -> None: ...
    async def get_thread_project(self, thread_id: str) -> Project | None: ...
    async def get_project_overrides(self, project_id: str) -> SessionOverrides: ...
    async def upsert_project_overrides(
        self, project_id: str, overrides: SessionOverrides
    ) -> SessionOverrides: ...
    async def list_projects(
        self,
        *,
        connection_id: str | None = None,
        limit: int = 50,
    ) -> list[Project]: ...


class RuntimeSettingsService:
    """Own runtime settings, profile, and Project selection policy."""

    def __init__(
        self,
        policy: RuntimeSettingsPolicy,
        repository: RuntimeSettingsRepository,
        client: CodexBackend,
    ) -> None:
        self._policy = policy
        self._repository = repository
        self._client = client

    def resolve_profile(
        self, chat_key: str, overrides: SessionOverrides
    ) -> ProfileDefinition:
        """Resolve the effective profile for one chat and override set."""
        return resolve_profile(
            self._policy.profiles,
            self._policy.default_profile,
            default_profile_for_chat(
                dict(self._policy.client_default_profiles),
                chat_key,
                self._policy.default_profile,
            ),
            overrides,
        )

    async def get_settings(self, thread_id: str, chat_key: str) -> EffectiveSettings:
        """Return effective runtime settings for the active thread."""
        overrides = await self._repository.get_overrides(thread_id)
        return await self.resolve_effective_settings(chat_key, thread_id, overrides)

    async def clear_overrides(self, thread_id: str) -> EffectiveSettings:
        """Clear all runtime overrides and return effective settings."""
        await self._repository.clear_overrides(thread_id)
        thread = await self._repository.get_thread(thread_id)
        assert thread is not None
        return await self.get_settings(thread_id, thread.chat_key)

    async def update_override(
        self,
        thread_id: str,
        field_name: OverrideFieldName,
        value: str | None,
    ) -> EffectiveSettings:
        """Update one runtime override and return effective settings."""
        overrides = await self._repository.get_overrides(thread_id)
        if field_name == "profile" and value is not None:
            canonical = canonical_profile_name(value.strip())
            if canonical not in self._policy.profiles:
                raise ValueError(f"Unknown profile: {value}")
            overrides = overrides.with_value("profile", canonical)
        elif field_name == "verbosity" and value is not None:
            if value not in VERBOSITY_VALUES:
                raise ValueError(f"Unknown verbosity: {value}")
            overrides = overrides.with_value("verbosity", value)
        elif field_name == "command_verbosity" and value is not None:
            if value not in COMMAND_VERBOSITY_VALUES:
                raise ValueError(f"Unknown command verbosity: {value}")
            overrides = overrides.with_value("command_verbosity", value)
        elif field_name == "followup_mode" and value is not None:
            if value not in FOLLOWUP_MODE_VALUES:
                raise ValueError(f"Unknown follow-up mode: {value}")
            overrides = overrides.with_value("followup_mode", value)
        elif field_name == "collaboration_mode" and value is not None:
            if value not in COLLABORATION_MODE_VALUES:
                raise ValueError(f"Unknown collaboration mode: {value}")
            overrides = overrides.with_value("collaboration_mode", value)
        else:
            overrides = overrides.with_value(field_name, value)
        await self._repository.upsert_overrides(thread_id, overrides)
        if field_name in {"model", "effort"}:
            await self.remember_project_runtime_override(
                thread_id,
                field_name,
                value,
            )
        thread = await self._repository.get_thread(thread_id)
        assert thread is not None
        return await self.resolve_effective_settings(
            thread.chat_key,
            thread_id,
            overrides,
        )

    async def set_fast_mode(self, thread_id: str, enabled: bool) -> EffectiveSettings:
        """Update fast mode override."""
        overrides = await self._repository.get_overrides(thread_id)
        overrides = overrides.with_value("fast_mode", enabled)
        await self._repository.upsert_overrides(thread_id, overrides)
        await self.remember_project_runtime_override(
            thread_id,
            "fast_mode",
            enabled,
        )
        thread = await self._repository.get_thread(thread_id)
        assert thread is not None
        return await self.resolve_effective_settings(
            thread.chat_key,
            thread_id,
            overrides,
        )

    async def resolve_effective_settings(
        self,
        chat_key: str,
        thread_id: str,
        overrides: SessionOverrides,
    ) -> EffectiveSettings:
        """Resolve profile defaults, direct overrides, and Project cwd defaults."""
        profile = self.resolve_profile(chat_key, overrides)
        effective_overrides = await self.effective_overrides(thread_id, overrides)
        model = effective_overrides.model or profile.model
        return EffectiveSettings(
            profile=canonical_profile_name(overrides.profile or profile.name),
            model=model,
            model_provider=_resolve_model_provider_name(model, profile.model_provider),
            effort=effective_overrides.effort or profile.effort,
            summary=overrides.summary or profile.summary,
            cwd=effective_overrides.cwd or "",
            fast_mode=(
                profile.fast_mode
                if effective_overrides.fast_mode is None
                else effective_overrides.fast_mode
            ),
            verbosity=overrides.verbosity or profile.verbosity,
            command_verbosity=overrides.command_verbosity or profile.command_verbosity,
            followup_mode=overrides.followup_mode or profile.followup_mode,
            overrides=overrides.as_dict(),
            collaboration_mode=overrides.collaboration_mode or "default",
        )

    async def effective_overrides(
        self,
        thread_id: str,
        overrides: SessionOverrides,
    ) -> SessionOverrides:
        """Apply bound Project defaults where the thread has no explicit override."""
        project = await self._repository.get_thread_project(thread_id)
        if not isinstance(project, Project):
            return overrides
        effective = overrides
        if not effective.cwd:
            effective = effective.with_value("cwd", project.root_path)
        project_overrides = await self._repository.get_project_overrides(
            project.project_id
        )
        if effective.model is None and project_overrides.model is not None:
            effective = effective.with_value("model", project_overrides.model)
        if effective.effort is None and project_overrides.effort is not None:
            effective = effective.with_value("effort", project_overrides.effort)
        if effective.fast_mode is None and project_overrides.fast_mode is not None:
            effective = effective.with_value("fast_mode", project_overrides.fast_mode)
        return effective

    async def remember_project_runtime_override(
        self,
        thread_id: str,
        field_name: str,
        value: str | bool | None,
    ) -> None:
        """Persist runtime defaults on the bound Project."""
        project = await self._repository.get_thread_project(thread_id)
        if not isinstance(project, Project):
            return
        project_overrides = await self._repository.get_project_overrides(
            project.project_id
        )
        await self._repository.upsert_project_overrides(
            project.project_id,
            project_overrides.with_value(field_name, value),
        )

    async def restore_project_runtime_overrides(
        self,
        thread_id: str,
        project_id: str,
    ) -> None:
        """Copy stored Project runtime overrides onto a new bridge window."""
        project_overrides = await self._repository.get_project_overrides(project_id)
        if (
            project_overrides.model is None
            and project_overrides.effort is None
            and project_overrides.fast_mode is None
        ):
            return
        current = await self._repository.get_overrides(thread_id)
        if project_overrides.model is not None:
            current = current.with_value("model", project_overrides.model)
        if project_overrides.effort is not None:
            current = current.with_value("effort", project_overrides.effort)
        if project_overrides.fast_mode is not None:
            current = current.with_value("fast_mode", project_overrides.fast_mode)
        await self._repository.upsert_overrides(thread_id, current)

    async def project_overrides_for_thread(self, thread_id: str) -> SessionOverrides:
        """Return Project-level overrides for one thread, if it is Project-bound."""
        project = await self._repository.get_thread_project(thread_id)
        if not isinstance(project, Project):
            return SessionOverrides()
        return await self._repository.get_project_overrides(project.project_id)

    async def validate_thread_project_connection(self, thread: LogicalThread) -> None:
        """Reject turns whose bound Project belongs to a different backend."""
        project = await self._repository.get_thread_project(thread.thread_id)
        if not isinstance(project, Project):
            return
        self.validate_project_access(thread.chat_key, project)
        if (
            thread.codex_thread_id is not None
            and project.connection_id != thread.codex_backend_id
        ):
            raise ValueError(
                "Project connection mismatch: "
                f"project {project.label} uses {project.connection_id}, "
                f"but this conversation is bound to {thread.codex_backend_id}."
            )

    def validate_project_access(self, chat_key: str, project: Project) -> None:
        """Reject Projects outside the configured chat allowlist."""
        if self.project_is_allowed(chat_key, project):
            return
        raise ValueError(
            "Project is not allowed for this chat: "
            f"{project.connection_id}:{project.root_path}"
        )

    def filter_projects(self, chat_key: str, projects: list[Project]) -> list[Project]:
        """Return only Projects this chat is allowed to see."""
        return [
            project
            for project in projects
            if self.project_is_allowed(chat_key, project)
        ]

    def project_is_allowed(self, chat_key: str, project: Project) -> bool:
        """Return whether one chat may use the Project."""
        rules = self._policy.client_allowed_projects.get(chat_key)
        if not rules:
            return True
        normalized_project_root = _normalize_project_root(project.root_path)
        for rule in rules:
            configured_connection = rule.connection.strip()
            connection_matches = configured_connection == project.connection_id
            if not connection_matches:
                connection_matches = (
                    self.resolve_connection_id(configured_connection)
                    == project.connection_id
                )
            if (
                connection_matches
                and _normalize_project_root(rule.root_path) == normalized_project_root
            ):
                return True
        return False

    async def resolve_new_thread_project(
        self,
        chat_key: str,
        *,
        connection_name: str | None,
        project_selector: str | None,
    ) -> Project | None:
        """Resolve the Project a new bridge window should inherit."""
        if project_selector:
            return await self.select_project(
                chat_key,
                project_selector,
                connection_id=self.resolve_connection_id(connection_name),
            )
        active = await self._repository.get_active_thread(chat_key)
        if active is None:
            return None
        project = await self._repository.get_thread_project(active.thread_id)
        if not isinstance(project, Project):
            return None
        connection_id = self.resolve_connection_id(connection_name)
        if connection_id is not None and project.connection_id != connection_id:
            return None
        self.validate_project_access(chat_key, project)
        return project

    def resolve_connection_id(self, connection_name: str | None) -> str | None:
        """Resolve a user-facing connection name into a backend id."""
        if connection_name is None:
            return None
        resolver = getattr(type(self._client), "resolve_backend_id", None)
        if resolver is None:
            return connection_name
        return self._client.resolve_backend_id(backend_name=connection_name)

    async def select_project(
        self,
        chat_key: str,
        selector: str,
        *,
        connection_id: str | None = None,
    ) -> Project:
        """Select a Project from the known catalog."""
        catalog = self.filter_projects(
            chat_key,
            await self._repository.list_projects(
                connection_id=connection_id,
                limit=100,
            ),
        )
        chosen = _select_project(catalog, selector)
        if chosen is not None:
            return chosen
        label_matches = [
            project
            for project in catalog
            if project.label.casefold() == selector.strip().casefold()
        ]
        if connection_id is None and len(label_matches) > 1:
            pairs = ", ".join(
                f"{project.connection_id}:{project.label}" for project in label_matches
            )
            raise ValueError(f"Ambiguous project {selector!r}: {pairs}")
        raise ValueError("Unknown project. Use /project list.")


def _resolve_model_provider_name(model: str, default_provider: str) -> str:
    if model.startswith(("qwen", "llama", "mistral", "phi", "gemma", "deepseek")):
        return "llama"
    return default_provider


def _select_project(projects: list[Project], selector: str) -> Project | None:
    value = selector.strip()
    if value.isdigit():
        index = int(value)
        if 1 <= index <= len(projects):
            return projects[index - 1]
        return None
    lowered = value.casefold()
    matches = [project for project in projects if project.label.casefold() == lowered]
    if len(matches) == 1:
        return matches[0]
    return None


def _normalize_project_root(root_path: str) -> str:
    value = root_path.strip()
    if value == "/":
        return value
    return value.rstrip("/")
