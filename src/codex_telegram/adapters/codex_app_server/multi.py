"""Multi-backend routing adapter for codex app-server clients."""

from __future__ import annotations

from typing import Any

from codex_telegram.adapters.codex_app_server.client import (
    CodexAppServerClient,
    CodexAppServerError,
)
from codex_telegram.application.models import (
    AccountUsage,
    BackendConnection,
    CodexThreadBackendFailure,
    CodexRuntimeState,
    McpServerCapability,
    SkillCatalog,
)
from codex_telegram.config import AppServerConfig
from codex_telegram.domain import (
    CodexGoal,
    CodexThread,
    PendingApproval,
    PendingUserInput,
    ProfileDefinition,
    RealtimeEvent,
    RealtimeSession,
    SessionOverrides,
    ThreadBindingResult,
    TurnAccepted,
    TurnResult,
    TurnUpdate,
    UserTurnInput,
)


class MultiCodexBackend:
    """Route application backend calls to configured app-server clients."""

    def __init__(
        self,
        clients: dict[str, CodexAppServerClient],
        configs: dict[str, AppServerConfig],
        primary_backend_id: str,
    ) -> None:
        self._clients = clients
        self._configs = configs
        self._primary_backend_id = primary_backend_id
        self._name_to_id = {
            config.name.casefold(): backend_id for backend_id, config in configs.items()
        }
        self._listing_failures: list[CodexThreadBackendFailure] = []

    @property
    def listing_failures(self) -> list[CodexThreadBackendFailure]:
        """Return failures from the most recent thread listing call."""
        return list(self._listing_failures)

    async def async_close(self) -> None:
        """Close every configured backend client."""
        for client in self._clients.values():
            await client.async_close()

    async def async_healthcheck(self) -> dict[str, str]:
        """Healthcheck the primary backend."""
        return await self._client(self._primary_backend_id).async_healthcheck()

    async def get_usage(
        self,
        *,
        codex_backend_id: str | None = None,
    ) -> AccountUsage:
        """Return account usage from the selected backend."""
        resolved_backend_id = self._resolve_backend_id(backend_id=codex_backend_id)
        return await self._client(resolved_backend_id).get_usage(
            codex_backend_id=resolved_backend_id,
        )

    async def list_skills(
        self,
        *,
        cwd: str | None = None,
        force_reload: bool = False,
        codex_backend_id: str | None = None,
    ) -> list[SkillCatalog]:
        """Return skills from the selected backend."""
        resolved_backend_id = self._resolve_backend_id(backend_id=codex_backend_id)
        return await self._client(resolved_backend_id).list_skills(
            cwd=cwd,
            force_reload=force_reload,
            codex_backend_id=resolved_backend_id,
        )

    async def list_mcp_servers(
        self,
        *,
        codex_backend_id: str | None = None,
    ) -> list[McpServerCapability]:
        """Return MCP inventory from the selected backend."""
        resolved_backend_id = self._resolve_backend_id(backend_id=codex_backend_id)
        return await self._client(resolved_backend_id).list_mcp_servers(
            codex_backend_id=resolved_backend_id,
        )

    def resolve_backend_id(
        self,
        *,
        backend_id: str | None = None,
        backend_name: str | None = None,
    ) -> str:
        """Resolve a configured backend id or friendly name."""
        return self._resolve_backend_id(
            backend_id=backend_id,
            backend_name=backend_name,
        )

    async def list_backend_connections(self) -> list[BackendConnection]:
        """Return every configured app-server connection."""
        return [
            BackendConnection(
                connection_id=backend_id,
                label=self._configs[backend_id].name,
            )
            for backend_id in self._clients
        ]

    async def list_codex_threads(
        self,
        *,
        search: str | None = None,
        limit: int = 50,
        backend_id: str | None = None,
        backend_name: str | None = None,
        include_all: bool = False,
    ) -> list[CodexThread]:
        """List threads from the primary, selected, or every backend."""
        self._listing_failures = []
        backend_ids = (
            list(self._clients)
            if include_all
            else [
                self._resolve_backend_id(
                    backend_id=backend_id, backend_name=backend_name
                )
            ]
        )
        threads: list[CodexThread] = []
        for resolved_backend_id in backend_ids:
            try:
                threads.extend(
                    await self._client(resolved_backend_id).list_codex_threads(
                        search=search,
                        limit=limit,
                    )
                )
            except Exception as err:
                failure = self._failure(resolved_backend_id, err)
                if include_all:
                    self._listing_failures.append(failure)
                    continue
                raise CodexAppServerError(
                    f"Backend {failure.backend_name} unavailable: {failure.error}"
                ) from err
        return threads

    async def get_codex_thread(
        self,
        thread_id: str,
        *,
        backend_id: str | None = None,
        backend_name: str | None = None,
    ) -> CodexThread:
        """Read one thread from a selected backend."""
        resolved_backend_id = self._resolve_backend_id(
            backend_id=backend_id,
            backend_name=backend_name,
        )
        return await self._client(resolved_backend_id).get_codex_thread(thread_id)

    async def ensure_thread_binding(
        self,
        chat_key: str,
        logical_thread_id: str,
        existing_codex_thread_id: str | None,
        profile: ProfileDefinition,
        overrides: SessionOverrides,
        *,
        codex_backend_id: str,
        anchor_id: str | None = None,
    ) -> ThreadBindingResult:
        """Ensure a thread binding on its pinned backend."""
        resolved_backend_id = self._resolve_backend_id(backend_id=codex_backend_id)
        return await self._client(resolved_backend_id).ensure_thread_binding(
            chat_key,
            logical_thread_id,
            existing_codex_thread_id,
            profile,
            overrides,
            codex_backend_id=resolved_backend_id,
            anchor_id=anchor_id,
        )

    async def start_turn(
        self,
        chat_key: str,
        logical_thread_id: str,
        codex_thread_id: str,
        text: str | UserTurnInput,
        profile: ProfileDefinition,
        overrides: SessionOverrides,
        *,
        codex_backend_id: str,
    ) -> TurnAccepted:
        """Start a turn on the thread's pinned backend."""
        resolved_backend_id = self._resolve_backend_id(backend_id=codex_backend_id)
        return await self._client(resolved_backend_id).start_turn(
            chat_key,
            logical_thread_id,
            codex_thread_id,
            text,
            profile,
            overrides,
            codex_backend_id=resolved_backend_id,
        )

    async def steer_turn(
        self,
        *,
        logical_thread_id: str,
        codex_thread_id: str,
        codex_backend_id: str,
        turn_id: str,
        text: str | UserTurnInput,
    ) -> None:
        """Steer a turn on the thread's pinned backend."""
        resolved_backend_id = self._resolve_backend_id(backend_id=codex_backend_id)
        await self._client(resolved_backend_id).steer_turn(
            logical_thread_id=logical_thread_id,
            codex_thread_id=codex_thread_id,
            codex_backend_id=resolved_backend_id,
            turn_id=turn_id,
            text=text,
        )

    async def interrupt_turn(
        self,
        turn_id: str,
        *,
        codex_thread_id: str | None = None,
        codex_backend_id: str | None = None,
    ) -> None:
        """Interrupt a turn on the selected backend."""
        resolved_backend_id = self._resolve_backend_id(backend_id=codex_backend_id)
        await self._client(resolved_backend_id).interrupt_turn(
            turn_id,
            codex_thread_id=codex_thread_id,
            codex_backend_id=resolved_backend_id,
        )

    async def wait_for_turn_event(
        self, turn_id: str, timeout: float, *, codex_backend_id: str | None = None
    ) -> TurnUpdate | PendingApproval | PendingUserInput | TurnResult:
        """Wait for an event on the selected backend."""
        resolved_backend_id = self._resolve_backend_id(backend_id=codex_backend_id)
        return await self._client(resolved_backend_id).wait_for_turn_event(
            turn_id,
            timeout,
            codex_backend_id=resolved_backend_id,
        )

    def get_runtime_state(
        self,
        codex_thread_id: str,
        *,
        codex_backend_id: str | None = None,
    ) -> CodexRuntimeState:
        """Return in-memory runtime state from the selected backend."""
        resolved_backend_id = self._resolve_backend_id(backend_id=codex_backend_id)
        return self._client(resolved_backend_id).get_runtime_state(
            codex_thread_id,
            codex_backend_id=resolved_backend_id,
        )

    async def get_thread_goal(
        self,
        codex_thread_id: str,
        *,
        codex_backend_id: str | None = None,
    ) -> CodexGoal | None:
        """Fetch the goal from the selected backend."""
        resolved_backend_id = self._resolve_backend_id(backend_id=codex_backend_id)
        return await self._client(resolved_backend_id).get_thread_goal(
            codex_thread_id,
            codex_backend_id=resolved_backend_id,
        )

    async def set_thread_goal(
        self,
        codex_thread_id: str,
        *,
        objective: str,
        token_budget: int | None = None,
        status: str = "active",
        update_token_budget: bool = False,
        codex_backend_id: str | None = None,
    ) -> CodexGoal:
        """Set the goal on the selected backend."""
        resolved_backend_id = self._resolve_backend_id(backend_id=codex_backend_id)
        return await self._client(resolved_backend_id).set_thread_goal(
            codex_thread_id,
            objective=objective,
            token_budget=token_budget,
            status=status,
            update_token_budget=update_token_budget,
            codex_backend_id=resolved_backend_id,
        )

    async def clear_thread_goal(
        self,
        codex_thread_id: str,
        *,
        codex_backend_id: str | None = None,
    ) -> None:
        """Clear the goal on the selected backend."""
        resolved_backend_id = self._resolve_backend_id(backend_id=codex_backend_id)
        await self._client(resolved_backend_id).clear_thread_goal(
            codex_thread_id,
            codex_backend_id=resolved_backend_id,
        )

    async def resolve_server_request(
        self, request_id: int, result: Any, *, codex_backend_id: str | None = None
    ) -> None:
        """Resolve an app-server request on the selected backend."""
        resolved_backend_id = self._resolve_backend_id(backend_id=codex_backend_id)
        await self._client(resolved_backend_id).resolve_server_request(
            request_id,
            result,
            codex_backend_id=resolved_backend_id,
        )

    async def start_realtime(
        self,
        chat_key: str,
        logical_thread_id: str,
        codex_thread_id: str,
        *,
        codex_backend_id: str,
    ) -> RealtimeSession:
        """Start realtime on the thread's pinned backend."""
        resolved_backend_id = self._resolve_backend_id(backend_id=codex_backend_id)
        return await self._client(resolved_backend_id).start_realtime(
            chat_key,
            logical_thread_id,
            codex_thread_id,
            codex_backend_id=resolved_backend_id,
        )

    async def append_realtime_text(
        self,
        codex_thread_id: str,
        text: str,
        *,
        codex_backend_id: str,
    ) -> None:
        """Append realtime text on the selected backend."""
        resolved_backend_id = self._resolve_backend_id(backend_id=codex_backend_id)
        await self._client(resolved_backend_id).append_realtime_text(
            codex_thread_id,
            text,
            codex_backend_id=resolved_backend_id,
        )

    async def stop_realtime(
        self,
        codex_thread_id: str,
        *,
        codex_backend_id: str,
    ) -> None:
        """Stop realtime on the selected backend."""
        resolved_backend_id = self._resolve_backend_id(backend_id=codex_backend_id)
        await self._client(resolved_backend_id).stop_realtime(
            codex_thread_id,
            codex_backend_id=resolved_backend_id,
        )

    async def wait_for_realtime_event(
        self,
        codex_thread_id: str,
        timeout: float,
        *,
        codex_backend_id: str,
    ) -> RealtimeEvent:
        """Wait for realtime events on the selected backend."""
        resolved_backend_id = self._resolve_backend_id(backend_id=codex_backend_id)
        return await self._client(resolved_backend_id).wait_for_realtime_event(
            codex_thread_id,
            timeout,
            codex_backend_id=resolved_backend_id,
        )

    def _client(self, backend_id: str) -> CodexAppServerClient:
        try:
            return self._clients[backend_id]
        except KeyError as err:
            raise CodexAppServerError(f"Unknown Codex backend: {backend_id}") from err

    def _resolve_backend_id(
        self,
        *,
        backend_id: str | None = None,
        backend_name: str | None = None,
    ) -> str:
        if backend_id:
            return backend_id
        if backend_name:
            try:
                return self._name_to_id[backend_name.casefold()]
            except KeyError as err:
                raise CodexAppServerError(
                    f"Unknown Codex backend: {backend_name}"
                ) from err
        return self._primary_backend_id

    def _failure(self, backend_id: str, err: Exception) -> CodexThreadBackendFailure:
        config = self._configs[backend_id]
        return CodexThreadBackendFailure(
            backend_id=backend_id,
            backend_name=config.name,
            error=str(err),
        )
