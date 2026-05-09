"""Websocket client for codex app-server."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
import itertools
import json
from typing import Any

from aiohttp import ClientSession, ClientWebSocketResponse, WSMessage, WSMsgType

from codex_telegram.application.models import (
    AccountUsage,
    AccountUsageLimit,
    BackendConnection,
    CodexPlanItem,
    CodexRuntimeState,
    McpResourceCapability,
    McpServerCapability,
    McpToolCapability,
    RuntimeUsageMetrics,
    SkillCapability,
    SkillCatalog,
)
from codex_telegram.application.ports import CodexBackendError
from codex_telegram.domain import (
    CodexThread,
    CodexGoal,
    PendingApproval,
    PendingUserInput,
    ProfileDefinition,
    RealtimeEvent,
    RealtimeSession,
    SessionOverrides,
    ThreadBindingResult,
    TurnAccepted,
    TurnResult,
    TurnResultImage,
    TurnUpdate,
    UserInputOption,
    UserInputQuestion,
    UserTurnInput,
)
from codex_telegram.observability import (
    get_logger,
    log_debug,
    log_exception,
    log_info,
    log_warning,
)


class CodexAppServerError(CodexBackendError):
    """Raised when the app-server transport fails."""


LOGGER = get_logger(__name__)
JSONRPC_METHOD_NOT_FOUND = -32601
JSONRPC_INVALID_PARAMS = -32602
_TURN_SCOPED_NOTIFICATION_METHODS = {
    "item/agentMessage/delta",
    "item/plan/delta",
    "item/completed",
    "item/started",
    "rawResponseItem/completed",
    "thread/tokenUsage/updated",
    "turn/completed",
}


@dataclass(slots=True)
class _TurnState:
    turn_id: str
    chat_key: str
    logical_thread_id: str
    codex_thread_id: str
    codex_backend_id: str
    status: str = "inProgress"
    final_text: str = ""
    final_images: list[TurnResultImage] = field(default_factory=list)
    error: str | None = None
    token_usage: dict[str, int] | None = None
    verbosity: str = "verbose"
    command_verbosity: str = "errors"
    queue: asyncio.Queue[
        TurnUpdate | PendingApproval | PendingUserInput | TurnResult
    ] = field(default_factory=asyncio.Queue)
    completed: asyncio.Future[TurnResult] = field(
        default_factory=lambda: asyncio.get_running_loop().create_future()
    )


@dataclass(slots=True)
class _RealtimeState:
    chat_key: str
    logical_thread_id: str
    codex_thread_id: str
    codex_backend_id: str
    session_id: str | None = None
    status: str = "starting"
    last_text: str = ""
    queue: asyncio.Queue[RealtimeEvent] = field(default_factory=asyncio.Queue)


class CodexAppServerClient:
    """Minimal async JSON-RPC websocket client for codex app-server."""

    def __init__(
        self,
        http_session: ClientSession,
        base_url: str,
        token: str | None,
        *,
        backend_id: str = "primary",
        backend_name: str = "primary",
    ) -> None:
        self._http_session = http_session
        self._base_url = base_url
        self._token = token
        self._backend_id = backend_id
        self._backend_name = backend_name
        self._ws: ClientWebSocketResponse | None = None
        self._reader_task: asyncio.Task[None] | None = None
        self._request_ids = itertools.count(1)
        self._pending: dict[int, asyncio.Future[dict[str, Any]]] = {}
        self._turns: dict[str, _TurnState] = {}
        self._thread_context: dict[str, tuple[str, str]] = {}
        self._active_turn_by_thread: dict[str, str] = {}
        self._active_turn_by_codex_thread: dict[str, str] = {}
        self._pending_turn_notifications: dict[
            str,
            list[tuple[str, dict[str, Any]]],
        ] = {}
        self._realtime_by_codex_thread: dict[str, _RealtimeState] = {}
        self._runtime_by_codex_thread: dict[str, CodexRuntimeState] = {}
        self._thread_locks: dict[str, asyncio.Lock] = {}
        self._codex_thread_locks: dict[str, asyncio.Lock] = {}

    async def async_close(self) -> None:
        """Close the websocket and background tasks."""
        log_info(LOGGER, "codex_app_server_client_closing")
        if self._reader_task is not None:
            self._reader_task.cancel()
            self._reader_task = None
        if self._ws is not None and not self._ws.closed:
            await self._ws.close()
        self._ws = None

    async def async_healthcheck(self) -> dict[str, str]:
        """Ensure the websocket is reachable and initialized."""
        await self._ensure_connected()
        log_debug(LOGGER, "codex_app_server_healthcheck_passed")
        return {"status": "ok"}

    async def get_usage(
        self,
        *,
        codex_backend_id: str | None = None,
    ) -> AccountUsage:
        """Return account usage from the app-server account rate-limit RPC."""
        self.resolve_backend_id(backend_id=codex_backend_id)
        try:
            result = await self._ws_request("account/rateLimits/read", {})
        except CodexAppServerError as err:
            return AccountUsage(status="unavailable", reason=str(err))
        return _account_usage_from_rate_limits_response(result)

    async def list_skills(
        self,
        *,
        cwd: str | None = None,
        force_reload: bool = False,
        codex_backend_id: str | None = None,
    ) -> list[SkillCatalog]:
        """List runtime skills through app-server."""
        self.resolve_backend_id(backend_id=codex_backend_id)
        params: dict[str, object] = {"forceReload": force_reload}
        if cwd:
            params["cwds"] = [cwd]
        result = await self._ws_request("skills/list", params)
        data = result.get("data", [])
        if not isinstance(data, list):
            return []
        return [
            _skill_catalog_from_payload(item) for item in data if isinstance(item, dict)
        ]

    async def list_mcp_servers(
        self,
        *,
        codex_backend_id: str | None = None,
    ) -> list[McpServerCapability]:
        """List MCP servers and their read-only inventory through app-server."""
        self.resolve_backend_id(backend_id=codex_backend_id)
        servers: list[McpServerCapability] = []
        cursor: str | None = None
        while True:
            params: dict[str, object] = {"detail": "full"}
            if cursor:
                params["cursor"] = cursor
            result = await self._ws_request("mcpServerStatus/list", params)
            data = result.get("data", [])
            if isinstance(data, list):
                servers.extend(
                    _mcp_server_from_payload(item)
                    for item in data
                    if isinstance(item, dict)
                )
            next_cursor = result.get("nextCursor")
            cursor = next_cursor if isinstance(next_cursor, str) else None
            if cursor is None:
                return servers

    def resolve_backend_id(
        self,
        *,
        backend_id: str | None = None,
        backend_name: str | None = None,
    ) -> str:
        """Resolve this single backend by id or name."""
        if backend_id and backend_id != self._backend_id:
            raise CodexAppServerError(f"Unknown Codex backend: {backend_id}")
        if backend_name and backend_name.casefold() not in {
            self._backend_id.casefold(),
            self._backend_name.casefold(),
        }:
            raise CodexAppServerError(f"Unknown Codex backend: {backend_name}")
        return self._backend_id

    async def list_backend_connections(self) -> list[BackendConnection]:
        """Return this single configured app-server connection."""
        return [
            BackendConnection(
                connection_id=self._backend_id,
                label=self._backend_name,
            )
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
        """List persisted Codex backend threads from app-server."""
        await self._ensure_connected()
        params: dict[str, object] = {
            "archived": False,
            "limit": limit,
            "sortKey": "updated_at",
            "sortDirection": "desc",
        }
        if search and search.strip():
            params["searchTerm"] = search.strip()
        result = await self._ws_request("thread/list", params)
        data = result.get("data", [])
        if not isinstance(data, list):
            return []
        return [
            replace(
                _codex_thread_from_payload(item),
                codex_backend_id=self._backend_id,
                codex_backend_name=self._backend_name,
            )
            for item in data
            if isinstance(item, dict)
        ]

    async def get_codex_thread(
        self,
        thread_id: str,
        *,
        backend_id: str | None = None,
        backend_name: str | None = None,
    ) -> CodexThread:
        """Read one Codex backend thread by id."""
        await self._ensure_connected()
        result = await self._ws_request(
            "thread/read",
            {"threadId": thread_id, "includeTurns": False},
        )
        thread = result.get("thread")
        if not isinstance(thread, dict):
            raise CodexAppServerError("Malformed thread/read response.")
        return replace(
            _codex_thread_from_payload(thread),
            codex_backend_id=self._backend_id,
            codex_backend_name=self._backend_name,
        )

    async def ensure_thread_binding(
        self,
        chat_key: str,
        logical_thread_id: str,
        existing_codex_thread_id: str | None,
        profile: ProfileDefinition,
        overrides: SessionOverrides,
        *,
        codex_backend_id: str = "primary",
        anchor_id: str | None = None,
    ) -> ThreadBindingResult:
        """Create or resume the Codex thread for one bridge window."""
        await self._ensure_connected()
        params: dict[str, object] = {}
        if overrides.profile:
            params["permissions"] = {"type": "profile", "id": overrides.profile}
        if overrides.model:
            params["model"] = overrides.model
            params["modelProvider"] = _resolve_model_provider(overrides.model, profile)
        if overrides.effort:
            params["effort"] = overrides.effort
        if overrides.summary:
            params["summary"] = overrides.summary
        if overrides.cwd:
            params["cwd"] = overrides.cwd
        params["developerInstructions"] = _developer_instructions(
            profile.developer_instructions,
            chat_key=chat_key,
            logical_thread_id=logical_thread_id,
            anchor_id=anchor_id,
            codex_thread_id=existing_codex_thread_id,
        )

        if existing_codex_thread_id is not None:
            try:
                await self._ws_request(
                    "thread/resume",
                    {"threadId": existing_codex_thread_id, **params},
                )
            except Exception as err:
                log_exception(
                    LOGGER,
                    "codex_thread_resume_failed",
                    level="warning",
                    err=err,
                    chat_key=chat_key,
                    logical_thread_id=logical_thread_id,
                    codex_thread_id=existing_codex_thread_id,
                )
            else:
                self._thread_context[existing_codex_thread_id] = (
                    chat_key,
                    logical_thread_id,
                )
                return ThreadBindingResult(
                    codex_thread_id=existing_codex_thread_id,
                    remapped=False,
                    codex_backend_id=self._backend_id,
                )

        result = await self._ws_request("thread/start", params)
        codex_thread_id = str(result["thread"]["id"])
        log_info(
            LOGGER,
            "codex_thread_started",
            chat_key=chat_key,
            logical_thread_id=logical_thread_id,
            codex_thread_id=codex_thread_id,
        )
        self._thread_context[codex_thread_id] = (chat_key, logical_thread_id)
        return ThreadBindingResult(
            codex_thread_id=codex_thread_id,
            remapped=existing_codex_thread_id not in {None, codex_thread_id},
            codex_backend_id=self._backend_id,
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
        codex_backend_id: str = "primary",
    ) -> TurnAccepted:
        """Start one turn after any prior turn for this bridge window completes."""
        async with self._codex_thread_lock(codex_thread_id):
            active_turn_id = self._active_turn_by_codex_thread.get(codex_thread_id)
            if active_turn_id is not None:
                active_turn = self._turns.get(active_turn_id)
                if active_turn is not None:
                    await active_turn.completed
                self._active_turn_by_codex_thread.pop(codex_thread_id, None)

            result = await self._ws_request(
                "turn/start",
                _turn_start_params(
                    thread_id=codex_thread_id,
                    text=text,
                    profile=profile,
                    overrides=overrides,
                ),
            )
            turn_id = str(result["turn"]["id"])
            log_info(
                LOGGER,
                "codex_turn_started",
                chat_key=chat_key,
                logical_thread_id=logical_thread_id,
                codex_thread_id=codex_thread_id,
                turn_id=turn_id,
            )
            self._thread_context[codex_thread_id] = (chat_key, logical_thread_id)
            state = _TurnState(
                turn_id=turn_id,
                chat_key=chat_key,
                logical_thread_id=logical_thread_id,
                codex_thread_id=codex_thread_id,
                codex_backend_id=self._backend_id,
                verbosity=overrides.verbosity or profile.verbosity,
                command_verbosity=(
                    overrides.command_verbosity or profile.command_verbosity
                ),
            )
            self._turns[turn_id] = state
            self._active_turn_by_thread[logical_thread_id] = turn_id
            self._active_turn_by_codex_thread[codex_thread_id] = turn_id
            self._drain_pending_turn_notifications(turn_id)
            return TurnAccepted(
                turn_id=turn_id,
                chat_key=chat_key,
                logical_thread_id=logical_thread_id,
                codex_thread_id=codex_thread_id,
                remapped=False,
                codex_backend_id=self._backend_id,
            )

    async def steer_turn(
        self,
        *,
        logical_thread_id: str,
        codex_thread_id: str,
        codex_backend_id: str = "primary",
        turn_id: str,
        text: str | UserTurnInput,
    ) -> None:
        """Append user input to an already-running regular turn."""
        async with self._thread_lock(logical_thread_id):
            state = self._turns.get(turn_id)
            if state is None:
                raise CodexAppServerError(f"Unknown turn: {turn_id}")
            if state.logical_thread_id != logical_thread_id:
                raise CodexAppServerError(
                    f"Turn {turn_id} is not active for bridge window {logical_thread_id}"
                )
            try:
                result = await self._ws_request(
                    "turn/steer",
                    {
                        "threadId": codex_thread_id,
                        "input": _build_turn_input(text),
                        "expectedTurnId": turn_id,
                    },
                )
            except CodexAppServerError as err:
                if _is_active_turn_mismatch(str(err), turn_id):
                    self._complete_turn(turn_id, "failed", str(err), None)
                raise
            accepted_turn_id = result.get("turnId")
            if not isinstance(accepted_turn_id, str):
                raise CodexAppServerError(
                    "App Server returned no turnId for turn/steer"
                )
            if accepted_turn_id != turn_id:
                raise CodexAppServerError(
                    "App Server steered a different turn than expected"
                )
            log_info(
                LOGGER,
                "codex_turn_steered",
                logical_thread_id=logical_thread_id,
                codex_thread_id=codex_thread_id,
                turn_id=turn_id,
            )

    async def interrupt_turn(
        self,
        turn_id: str,
        *,
        codex_thread_id: str | None = None,
        codex_backend_id: str | None = None,
    ) -> None:
        """Interrupt one in-flight turn."""
        state = self._turns.get(turn_id)
        if state is None and codex_thread_id is None:
            raise CodexAppServerError(f"Unknown turn: {turn_id}")
        resolved_thread_id = (
            state.codex_thread_id if state is not None else str(codex_thread_id)
        )
        log_info(LOGGER, "codex_turn_interrupt_requested", turn_id=turn_id)
        try:
            await self._ws_request(
                "turn/interrupt",
                {"threadId": resolved_thread_id, "turnId": turn_id},
            )
        except CodexAppServerError as err:
            if _is_active_turn_mismatch(str(err), turn_id):
                if state is not None:
                    self._complete_turn(turn_id, "failed", str(err), None)
                return
            raise

    async def wait_for_turn_event(
        self, turn_id: str, timeout: float, *, codex_backend_id: str | None = None
    ) -> TurnUpdate | PendingApproval | PendingUserInput | TurnResult:
        """Wait for the next event for a turn."""
        state = self._turns.get(turn_id)
        if state is None:
            raise CodexAppServerError(f"Unknown turn: {turn_id}")
        return await asyncio.wait_for(state.queue.get(), timeout)

    def get_runtime_state(
        self,
        codex_thread_id: str,
        *,
        codex_backend_id: str | None = None,
    ) -> CodexRuntimeState:
        """Return the last known runtime state for one backend thread."""
        del codex_backend_id
        return self._runtime_by_codex_thread.get(codex_thread_id, CodexRuntimeState())

    async def get_thread_goal(
        self,
        codex_thread_id: str,
        *,
        codex_backend_id: str | None = None,
    ) -> CodexGoal | None:
        """Fetch the app-server goal for one Codex thread."""
        self.resolve_backend_id(backend_id=codex_backend_id)
        result = await self._ws_request(
            "thread/goal/get",
            {"threadId": codex_thread_id},
        )
        goal_result = _goal_from_get_goal_payload(result)
        if goal_result is _NO_GOAL or goal_result is None:
            self._set_runtime_goal(codex_thread_id, None)
            return None
        assert isinstance(goal_result, CodexGoal)
        self._set_runtime_goal(codex_thread_id, goal_result)
        return goal_result

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
        """Set or update the app-server goal for one Codex thread."""
        self.resolve_backend_id(backend_id=codex_backend_id)
        params: dict[str, Any] = {
            "threadId": codex_thread_id,
            "objective": objective,
            "status": status,
        }
        if update_token_budget:
            params["tokenBudget"] = token_budget
        result = await self._ws_request("thread/goal/set", params)
        goal = _goal_from_get_goal_payload(result) or _goal_from_payload(result)
        if not isinstance(goal, CodexGoal):
            goal = CodexGoal(
                objective=objective,
                status=status,
                token_budget=token_budget if update_token_budget else None,
            )
        self._set_runtime_goal(codex_thread_id, goal)
        return goal

    async def clear_thread_goal(
        self,
        codex_thread_id: str,
        *,
        codex_backend_id: str | None = None,
    ) -> None:
        """Clear the app-server goal for one Codex thread."""
        self.resolve_backend_id(backend_id=codex_backend_id)
        await self._ws_request("thread/goal/clear", {"threadId": codex_thread_id})
        self._set_runtime_goal(codex_thread_id, None)

    async def resolve_server_request(
        self, request_id: int, result: Any, *, codex_backend_id: str | None = None
    ) -> None:
        """Resolve one pending server request."""
        await self._ws_respond(request_id, result)

    async def start_realtime(
        self,
        chat_key: str,
        logical_thread_id: str,
        codex_thread_id: str,
        *,
        codex_backend_id: str = "primary",
    ) -> RealtimeSession:
        """Start one thread-scoped realtime session."""
        state = _RealtimeState(
            chat_key=chat_key,
            logical_thread_id=logical_thread_id,
            codex_thread_id=codex_thread_id,
            codex_backend_id=self._backend_id,
        )
        self._thread_context[codex_thread_id] = (chat_key, logical_thread_id)
        self._realtime_by_codex_thread[codex_thread_id] = state
        try:
            await self._ws_request(
                "thread/realtime/start",
                {
                    "threadId": codex_thread_id,
                    "outputModality": "text",
                },
            )
        except Exception:
            self._realtime_by_codex_thread.pop(codex_thread_id, None)
            raise
        log_info(
            LOGGER,
            "codex_realtime_started",
            chat_key=chat_key,
            logical_thread_id=logical_thread_id,
            codex_thread_id=codex_thread_id,
        )
        return _realtime_session(state)

    async def append_realtime_text(
        self,
        codex_thread_id: str,
        text: str,
        *,
        codex_backend_id: str = "primary",
    ) -> None:
        """Append text to an active realtime session."""
        if codex_thread_id not in self._realtime_by_codex_thread:
            raise CodexAppServerError(f"No active realtime session: {codex_thread_id}")
        await self._ws_request(
            "thread/realtime/appendText",
            {"threadId": codex_thread_id, "text": text},
        )

    async def stop_realtime(
        self,
        codex_thread_id: str,
        *,
        codex_backend_id: str = "primary",
    ) -> None:
        """Stop an active realtime session."""
        try:
            await self._ws_request(
                "thread/realtime/stop",
                {"threadId": codex_thread_id},
            )
        finally:
            self._realtime_by_codex_thread.pop(codex_thread_id, None)

    async def wait_for_realtime_event(
        self,
        codex_thread_id: str,
        timeout: float,
        *,
        codex_backend_id: str = "primary",
    ) -> RealtimeEvent:
        """Wait for the next realtime event for a Codex thread."""
        state = self._realtime_by_codex_thread.get(codex_thread_id)
        if state is None:
            raise CodexAppServerError(f"No active realtime session: {codex_thread_id}")
        return await asyncio.wait_for(state.queue.get(), timeout)

    async def _ensure_connected(self) -> None:
        if (
            self._ws is not None
            and not self._ws.closed
            and (self._reader_task is None or not self._reader_task.done())
        ):
            return
        log_info(
            LOGGER,
            "codex_websocket_connecting",
            v={"url": self._base_url},
            codex_backend_id=self._backend_id,
        )
        try:
            self._ws = await self._http_session.ws_connect(
                self._base_url,
                headers=(
                    {"Authorization": f"Bearer {self._token}"}
                    if self._token is not None
                    else None
                ),
                heartbeat=30.0,
            )
        except Exception as err:
            log_exception(
                LOGGER,
                "codex_websocket_connect_failed",
                err=err,
                v={"url": self._base_url},
                codex_backend_id=self._backend_id,
            )
            raise CodexAppServerError(
                f"Unable to connect to app-server at {self._base_url}: {err}",
                backend_id=self._backend_id,
            ) from err
        self._reader_task = asyncio.create_task(self._reader_loop())
        await self._ws_request(
            "initialize",
            {
                "clientInfo": {
                    "name": "codex_telegram",
                    "title": "codex-telegram",
                    "version": "0.1.0",
                },
                "capabilities": {"experimentalApi": True},
            },
        )
        await self._ws_notify("initialized", {})
        log_info(LOGGER, "codex_websocket_initialized")

    async def _ws_request(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        await self._ensure_connected()
        request_id = next(self._request_ids)
        log_debug(
            LOGGER,
            "codex_websocket_request_sent",
            request_id=request_id,
            v={"method": method},
        )
        future: asyncio.Future[dict[str, Any]] = (
            asyncio.get_running_loop().create_future()
        )
        self._pending[request_id] = future
        await self._ws_send({"id": request_id, "method": method, "params": params})
        return await future

    async def _ws_notify(self, method: str, params: dict[str, Any]) -> None:
        await self._ws_send({"method": method, "params": params})

    async def _ws_respond(self, request_id: int, result: Any) -> None:
        await self._ws_send({"id": request_id, "result": result})

    async def _ws_reject(self, request_id: int, code: int, message: str) -> None:
        await self._ws_send(
            {"id": request_id, "error": {"code": code, "message": message}}
        )

    async def _ws_send(self, payload: dict[str, Any]) -> None:
        if self._ws is None or self._ws.closed:
            raise CodexAppServerError("App Server websocket is not connected")
        await self._ws.send_json(payload)

    async def _reader_loop(self) -> None:
        close_reason = "App Server connection closed"
        try:
            assert self._ws is not None
            while True:
                message = await self._ws.receive()
                if message.type in (
                    WSMsgType.CLOSE,
                    WSMsgType.CLOSED,
                    WSMsgType.CLOSING,
                ):
                    break
                if message.type is WSMsgType.ERROR:
                    raise CodexAppServerError(str(self._ws.exception()))
                if message.type not in (WSMsgType.TEXT, WSMsgType.BINARY):
                    continue
                for payload in _decode_ws_messages(message):
                    self._handle_ws_message(payload)
        except asyncio.CancelledError:
            close_reason = "App Server connection cancelled"
            raise
        except Exception as err:  # pragma: no cover - exercised through queue failure
            close_reason = str(err)
            log_exception(LOGGER, "codex_websocket_reader_failed", err=err)
        finally:
            log_warning(
                LOGGER,
                "codex_websocket_closed",
                v={"reason": close_reason},
            )
            for future in self._pending.values():
                if not future.done():
                    future.set_exception(CodexAppServerError(close_reason))
            self._pending.clear()
            for turn_id, state in list(self._turns.items()):
                result = TurnResult(
                    turn_id=turn_id,
                    chat_key=state.chat_key,
                    logical_thread_id=state.logical_thread_id,
                    codex_thread_id=state.codex_thread_id,
                    status="failed",
                    final_text=state.final_text,
                    error=close_reason,
                    token_usage=state.token_usage,
                    images=tuple(state.final_images),
                    codex_backend_id=state.codex_backend_id,
                )
                if not state.completed.done():
                    state.completed.set_result(result)
                state.queue.put_nowait(result)
            self._turns.clear()
            self._active_turn_by_thread.clear()
            for realtime_state in list(self._realtime_by_codex_thread.values()):
                realtime_state.status = "closed"
                realtime_state.queue.put_nowait(
                    RealtimeEvent(
                        event_type="closed",
                        chat_key=realtime_state.chat_key,
                        logical_thread_id=realtime_state.logical_thread_id,
                        codex_thread_id=realtime_state.codex_thread_id,
                        codex_backend_id=realtime_state.codex_backend_id,
                        session_id=realtime_state.session_id,
                        text=realtime_state.last_text,
                        reason=close_reason,
                    )
                )
            self._realtime_by_codex_thread.clear()
            self._ws = None
            self._reader_task = None

    def _handle_ws_message(self, message: dict[str, Any]) -> None:
        if "id" in message and "method" not in message:
            self._handle_ws_response(message)
            return
        if "id" in message and "method" in message:
            self._handle_server_request(message)
            return
        if "method" in message:
            self._handle_notification(message["method"], message.get("params", {}))

    def _handle_ws_response(self, message: dict[str, Any]) -> None:
        request_id = int(message["id"])
        future = self._pending.pop(request_id, None)
        if future is None or future.done():
            return
        if "error" in message:
            error_message = _extract_error_message(message["error"])
            log_warning(
                LOGGER,
                "codex_websocket_request_failed",
                request_id=request_id,
                v={"error": {"message": error_message}},
            )
            future.set_exception(
                CodexAppServerError(error_message or "App Server request failed")
            )
            return
        result = message.get("result")
        if not isinstance(result, dict):
            future.set_exception(
                CodexAppServerError("App Server returned a non-object response")
            )
            return
        log_debug(LOGGER, "codex_websocket_response_received", request_id=request_id)
        future.set_result(result)

    def _handle_server_request(self, message: dict[str, Any]) -> None:
        request_id = _server_request_id(message)
        method = str(message.get("method", ""))
        if request_id is None:
            log_warning(
                LOGGER,
                "codex_server_request_invalid_id",
                v={"method": method, "id": str(message.get("id"))},
            )
            return
        params = message.get("params", {})
        if not isinstance(params, dict):
            self._reject_server_request(
                request_id,
                method,
                None,
                "Server request params must be an object.",
                code=JSONRPC_INVALID_PARAMS,
            )
            return
        codex_thread_id = params.get("threadId")
        if not isinstance(codex_thread_id, str):
            self._reject_server_request(
                request_id,
                method,
                None,
                "Server request is missing threadId.",
                code=JSONRPC_INVALID_PARAMS,
            )
            return
        context = self._thread_context.get(codex_thread_id)
        if context is None:
            self._reject_server_request(
                request_id,
                method,
                codex_thread_id,
                "No Telegram context is registered for this Codex thread.",
                code=JSONRPC_INVALID_PARAMS,
            )
            return
        chat_key, logical_thread_id = context
        questions = _extract_user_input_questions(method, params)
        if questions is not None:
            turn_id = params.get("turnId")
            if not isinstance(turn_id, str):
                self._reject_server_request(
                    request_id,
                    method,
                    codex_thread_id,
                    "Server request is missing turnId.",
                    code=JSONRPC_INVALID_PARAMS,
                )
                return
            request = PendingUserInput(
                request_id=request_id,
                chat_key=chat_key,
                logical_thread_id=logical_thread_id,
                codex_thread_id=codex_thread_id,
                turn_id=turn_id,
                method=method,
                questions=questions,
                raw_params=params,
                codex_backend_id=self._backend_id,
            )
            state = self._turns.get(turn_id)
            if state is not None:
                state.queue.put_nowait(request)
                return
            self._reject_server_request(
                request_id,
                method,
                codex_thread_id,
                "No active turn is registered for this server request.",
                code=JSONRPC_INVALID_PARAMS,
            )
            return

        if not _is_approval_request(method):
            self._reject_server_request(
                request_id,
                method,
                codex_thread_id,
                f"Unsupported server request method: {method}",
                code=JSONRPC_METHOD_NOT_FOUND,
            )
            return

        turn_id = params.get("turnId")
        if not isinstance(turn_id, str):
            self._reject_server_request(
                request_id,
                method,
                codex_thread_id,
                "Server request is missing turnId.",
                code=JSONRPC_INVALID_PARAMS,
            )
            return
        approval = PendingApproval(
            request_id=request_id,
            chat_key=chat_key,
            logical_thread_id=logical_thread_id,
            codex_thread_id=codex_thread_id,
            codex_backend_id=self._backend_id,
            turn_id=turn_id,
            method=method,
            command=_extract_command_text(params),
            reason=_extract_reason_text(params),
            message=_extract_approval_message_text(params),
            raw_params=params,
        )
        state = self._turns.get(turn_id)
        if state is not None:
            state.queue.put_nowait(approval)
            return
        self._reject_server_request(
            request_id,
            method,
            codex_thread_id,
            "No active turn is registered for this server request.",
            code=JSONRPC_INVALID_PARAMS,
        )

    def _reject_server_request(
        self,
        request_id: int,
        method: str,
        codex_thread_id: str | None,
        message: str,
        *,
        code: int,
    ) -> None:
        log_warning(
            LOGGER,
            "codex_server_request_rejected",
            request_id=request_id,
            codex_thread_id=codex_thread_id,
            v={"method": method, "error": {"code": code, "message": message}},
        )
        task = asyncio.create_task(self._ws_reject(request_id, code, message))
        task.add_done_callback(
            lambda done: self._log_server_request_response_failure(done, request_id)
        )

    def _log_server_request_response_failure(
        self,
        task: asyncio.Task[None],
        request_id: int,
    ) -> None:
        try:
            task.result()
        except Exception as err:
            log_exception(
                LOGGER,
                "codex_server_request_reject_failed",
                err=err,
                request_id=request_id,
            )

    def _set_runtime_goal(
        self,
        codex_thread_id: str,
        goal: CodexGoal | None,
    ) -> None:
        runtime = self._runtime_by_codex_thread.get(
            codex_thread_id,
            CodexRuntimeState(),
        )
        self._runtime_by_codex_thread[codex_thread_id] = replace(runtime, goal=goal)

    def _set_runtime_token_usage(
        self,
        codex_thread_id: str,
        token_usage: dict[str, int] | None,
    ) -> None:
        runtime = self._runtime_by_codex_thread.get(
            codex_thread_id,
            CodexRuntimeState(),
        )
        self._runtime_by_codex_thread[codex_thread_id] = replace(
            runtime,
            token_usage=token_usage,
        )

    def _set_runtime_usage_metrics(
        self,
        codex_thread_id: str,
        usage_metrics: RuntimeUsageMetrics | None,
    ) -> None:
        runtime = self._runtime_by_codex_thread.get(
            codex_thread_id,
            CodexRuntimeState(),
        )
        self._runtime_by_codex_thread[codex_thread_id] = replace(
            runtime,
            usage_metrics=usage_metrics,
        )

    def _handle_notification(self, method: str, params: dict[str, Any]) -> None:
        if method.startswith("thread/realtime/"):
            self._handle_realtime_notification(method, params)
            return

        if method == "thread/goal/updated":
            codex_thread_id = params.get("threadId")
            goal = _goal_from_get_goal_payload(params) or _goal_from_payload(params)
            if isinstance(codex_thread_id, str) and isinstance(goal, CodexGoal):
                self._set_runtime_goal(codex_thread_id, goal)
            return

        if method == "thread/goal/cleared":
            codex_thread_id = params.get("threadId")
            if isinstance(codex_thread_id, str):
                self._set_runtime_goal(codex_thread_id, None)
            return

        if self._buffer_unregistered_turn_notification(method, params):
            return

        if method == "item/agentMessage/delta":
            turn_id = params.get("turnId")
            delta = params.get("delta", "")
            if isinstance(turn_id, str) and isinstance(delta, str):
                self._append_text(turn_id, delta, method)
            return

        if method == "item/plan/delta":
            turn_id = params.get("turnId")
            delta = params.get("delta", "")
            if isinstance(turn_id, str) and isinstance(delta, str):
                self._append_text(turn_id, delta, method)
            return

        if method == "item/completed":
            turn_id = params.get("turnId")
            item = params.get("item", {})
            if not isinstance(turn_id, str) or not isinstance(item, dict):
                return
            self._maybe_update_runtime_state(turn_id, item, method)
            self._append_result_images(turn_id, _extract_result_images(item))
            if item.get("type") in {"agentMessage", "Plan", "plan"} and isinstance(
                item.get("text"), str
            ):
                self._set_text(turn_id, item["text"], method)
                return
            self._maybe_emit_command_result(turn_id, item, method)
            return

        if method == "item/started":
            turn_id = params.get("turnId")
            item = params.get("item", {})
            if isinstance(turn_id, str) and isinstance(item, dict):
                self._maybe_update_runtime_state(turn_id, item, method)
                self._maybe_emit_command_start(turn_id, item, method)
            return

        if method == "rawResponseItem/completed":
            turn_id = params.get("turnId")
            item = params.get("item", {})
            if isinstance(turn_id, str):
                text = _extract_raw_response_text(item)
                if text is not None:
                    self._set_text(turn_id, text, method)
                self._append_result_images(turn_id, _extract_result_images(item))
            return

        if method == "thread/tokenUsage/updated":
            turn_id = params.get("turnId")
            state = self._turns.get(str(turn_id))
            token_usage = _extract_thread_token_usage(params)
            usage_metrics = _extract_runtime_usage_metrics(params)
            if state is not None and token_usage is not None:
                state.token_usage = token_usage
                self._set_runtime_token_usage(state.codex_thread_id, token_usage)
            if state is not None and usage_metrics is not None:
                self._set_runtime_usage_metrics(state.codex_thread_id, usage_metrics)
            return

        if method == "turn/completed":
            turn = params.get("turn", {})
            if not isinstance(turn, dict):
                return
            turn_id = turn.get("id")
            if not isinstance(turn_id, str):
                return
            text = _terminal_turn_text(turn)
            if text is not None:
                self._set_text(turn_id, text, method)
            self._complete_turn(
                turn_id,
                str(turn.get("status", "unknown")),
                _extract_error_message(turn.get("error")),
                _extract_token_usage(params) or _extract_token_usage(turn),
            )

    def _handle_realtime_notification(
        self, method: str, params: dict[str, Any]
    ) -> None:
        codex_thread_id = params.get("threadId")
        if not isinstance(codex_thread_id, str):
            return
        state = self._realtime_by_codex_thread.get(codex_thread_id)
        if state is None:
            return

        event_type = method.removeprefix("thread/realtime/").replace("/", "_")
        text = ""
        if event_type == "transcript_delta":
            delta = str(params.get("delta", ""))
            state.last_text += delta
            text = state.last_text
        elif event_type == "transcript_done":
            text = str(params.get("text", ""))
            state.last_text = text
        elif event_type == "started":
            session_id = params.get("realtimeSessionId")
            state.session_id = session_id if isinstance(session_id, str) else None
            state.status = "active"
        elif event_type == "error":
            text = str(params.get("message", ""))
            state.status = "error"
        elif event_type == "closed":
            reason = params.get("reason")
            state.status = "closed"
            self._realtime_by_codex_thread.pop(codex_thread_id, None)
            state.queue.put_nowait(
                RealtimeEvent(
                    event_type=event_type,
                    chat_key=state.chat_key,
                    logical_thread_id=state.logical_thread_id,
                    codex_thread_id=state.codex_thread_id,
                    codex_backend_id=state.codex_backend_id,
                    session_id=state.session_id,
                    text=state.last_text,
                    reason=reason if isinstance(reason, str) else None,
                )
            )
            return

        role = params.get("role")
        state.queue.put_nowait(
            RealtimeEvent(
                event_type=event_type,
                chat_key=state.chat_key,
                logical_thread_id=state.logical_thread_id,
                codex_thread_id=state.codex_thread_id,
                codex_backend_id=state.codex_backend_id,
                session_id=state.session_id,
                role=role if isinstance(role, str) else None,
                text=text,
                reason=text if event_type == "error" else None,
            )
        )

    def _append_text(self, turn_id: str, delta: str, source: str) -> None:
        state = self._turns.get(turn_id)
        if state is None:
            return
        state.final_text += delta
        if state.verbosity == "verbose":
            state.queue.put_nowait(
                TurnUpdate(
                    turn_id=turn_id,
                    chat_key=state.chat_key,
                    logical_thread_id=state.logical_thread_id,
                    codex_thread_id=state.codex_thread_id,
                    codex_backend_id=state.codex_backend_id,
                    status=state.status,
                    source=source,
                    text=state.final_text,
                    visible=True,
                )
            )

    def _set_text(self, turn_id: str, text: str, source: str) -> None:
        state = self._turns.get(turn_id)
        if state is None:
            return
        state.final_text = text
        if state.verbosity == "verbose":
            state.queue.put_nowait(
                TurnUpdate(
                    turn_id=turn_id,
                    chat_key=state.chat_key,
                    logical_thread_id=state.logical_thread_id,
                    codex_thread_id=state.codex_thread_id,
                    codex_backend_id=state.codex_backend_id,
                    status=state.status,
                    source=source,
                    text=text,
                    visible=True,
                )
            )

    def _append_result_images(
        self,
        turn_id: str,
        images: tuple[TurnResultImage, ...],
    ) -> None:
        state = self._turns.get(turn_id)
        if state is None or not images:
            return
        seen = {(image.source, image.caption) for image in state.final_images}
        for image in images:
            key = (image.source, image.caption)
            if key in seen:
                continue
            state.final_images.append(image)
            seen.add(key)

    def _maybe_emit_command_start(
        self, turn_id: str, item: dict[str, Any], source: str
    ) -> None:
        state = self._turns.get(turn_id)
        command = _extract_command_from_item(item)
        if state is None or not command:
            return
        if state.command_verbosity not in {"always", "verbose"}:
            return
        state.queue.put_nowait(
            TurnUpdate(
                turn_id=turn_id,
                chat_key=state.chat_key,
                logical_thread_id=state.logical_thread_id,
                codex_thread_id=state.codex_thread_id,
                codex_backend_id=state.codex_backend_id,
                status=state.status,
                source=source,
                text=f"Running command\n```text\n{command}\n```",
                visible=True,
            )
        )

    def _maybe_emit_command_result(
        self, turn_id: str, item: dict[str, Any], source: str
    ) -> None:
        state = self._turns.get(turn_id)
        command = _extract_command_from_item(item)
        if state is None or not command:
            return
        exit_code = item.get("exitCode")
        stderr = item.get("stderr")
        if exit_code not in {None, 0} or stderr:
            should_emit = state.command_verbosity in {
                "errors",
                "always",
                "verbose",
                "approval_only",
            }
            label = "Command failed"
        else:
            should_emit = state.command_verbosity == "verbose"
            label = "Command finished"
        if not should_emit:
            return
        details = [f"```text\n{command}\n```"]
        if exit_code not in {None, 0}:
            details.append(f"exit code: {exit_code}")
        if stderr:
            details.append(f"stderr:\n```text\n{stderr}\n```")
        state.queue.put_nowait(
            TurnUpdate(
                turn_id=turn_id,
                chat_key=state.chat_key,
                logical_thread_id=state.logical_thread_id,
                codex_thread_id=state.codex_thread_id,
                codex_backend_id=state.codex_backend_id,
                status=state.status,
                source=source,
                text=f"{label}\n" + "\n".join(details),
                visible=True,
            )
        )

    def _maybe_update_runtime_state(
        self, turn_id: str, item: dict[str, Any], source: str
    ) -> None:
        state = self._turns.get(turn_id)
        if state is None:
            return
        tool_name = _extract_tool_name_from_item(item)
        if tool_name is None:
            return
        normalized_tool_name = tool_name.rsplit(".", maxsplit=1)[-1]
        runtime = self._runtime_by_codex_thread.get(
            state.codex_thread_id,
            CodexRuntimeState(),
        )
        updated = _runtime_state_after_tool_item(runtime, tool_name, item)
        if updated == runtime:
            return
        goal_update = updated.goal if updated.goal != runtime.goal else None
        if (
            goal_update is None
            and runtime.goal is not None
            and updated.goal is None
            and normalized_tool_name in {"get_goal", "update_goal"}
        ):
            goal_update = replace(runtime.goal, status="complete")
        self._runtime_by_codex_thread[state.codex_thread_id] = updated
        state.queue.put_nowait(
            TurnUpdate(
                turn_id=turn_id,
                chat_key=state.chat_key,
                logical_thread_id=state.logical_thread_id,
                codex_thread_id=state.codex_thread_id,
                codex_backend_id=state.codex_backend_id,
                status=state.status,
                source=source,
                text="",
                visible=False,
                goal=goal_update,
            )
        )

    def _complete_turn(
        self,
        turn_id: str,
        status: str,
        error: str | None,
        token_usage: dict[str, int] | None,
    ) -> None:
        state = self._turns.get(turn_id)
        if state is None:
            return
        value: dict[str, object] = {"status": status}
        if error is not None:
            value["error"] = {"message": error}
        log_info(
            LOGGER,
            "codex_turn_completed",
            turn_id=turn_id,
            logical_thread_id=state.logical_thread_id,
            v=value,
        )
        resolved_token_usage = token_usage or state.token_usage
        state.status = status
        state.error = error
        state.token_usage = resolved_token_usage
        self._set_runtime_token_usage(state.codex_thread_id, resolved_token_usage)
        result = TurnResult(
            turn_id=turn_id,
            chat_key=state.chat_key,
            logical_thread_id=state.logical_thread_id,
            codex_thread_id=state.codex_thread_id,
            codex_backend_id=state.codex_backend_id,
            status=status,
            final_text=state.final_text,
            error=error,
            token_usage=resolved_token_usage,
            images=tuple(state.final_images),
        )
        if not state.completed.done():
            state.completed.set_result(result)
        state.queue.put_nowait(result)
        active_turn_id = self._active_turn_by_thread.get(state.logical_thread_id)
        if active_turn_id == turn_id:
            self._active_turn_by_thread.pop(state.logical_thread_id, None)
        codex_active_turn_id = self._active_turn_by_codex_thread.get(
            state.codex_thread_id
        )
        if codex_active_turn_id == turn_id:
            self._active_turn_by_codex_thread.pop(state.codex_thread_id, None)

    def _buffer_unregistered_turn_notification(
        self,
        method: str,
        params: dict[str, Any],
    ) -> bool:
        if method not in _TURN_SCOPED_NOTIFICATION_METHODS:
            return False
        turn_id = _turn_id_from_notification(method, params)
        if turn_id is None or turn_id in self._turns:
            return False
        self._pending_turn_notifications.setdefault(turn_id, []).append(
            (method, dict(params))
        )
        log_debug(
            LOGGER,
            "codex_turn_notification_buffered",
            turn_id=turn_id,
            v={"method": method},
        )
        return True

    def _drain_pending_turn_notifications(self, turn_id: str) -> None:
        buffered = self._pending_turn_notifications.pop(turn_id, [])
        if not buffered:
            return
        log_debug(
            LOGGER,
            "codex_turn_notifications_draining",
            turn_id=turn_id,
            v={"count": len(buffered)},
        )
        for method, params in buffered:
            self._handle_notification(method, params)

    def _thread_lock(self, logical_thread_id: str) -> asyncio.Lock:
        lock = self._thread_locks.get(logical_thread_id)
        if lock is None:
            lock = asyncio.Lock()
            self._thread_locks[logical_thread_id] = lock
        return lock

    def _codex_thread_lock(self, codex_thread_id: str) -> asyncio.Lock:
        lock = self._codex_thread_locks.get(codex_thread_id)
        if lock is None:
            lock = asyncio.Lock()
            self._codex_thread_locks[codex_thread_id] = lock
        return lock


def _decode_ws_messages(message: WSMessage) -> list[dict[str, Any]]:
    if message.type is WSMsgType.BINARY:
        data = message.data.decode("utf-8")
    else:
        data = str(message.data)
    data = data.strip()
    if not data:
        return []
    try:
        payload = json.loads(data)
    except json.JSONDecodeError:
        return [json.loads(line) for line in data.splitlines() if line.strip()]
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    return [payload] if isinstance(payload, dict) else []


def _turn_id_from_notification(
    method: str,
    params: dict[str, Any],
) -> str | None:
    if method == "turn/completed":
        turn = params.get("turn", {})
        if isinstance(turn, dict) and isinstance(turn.get("id"), str):
            return turn["id"]
        return None
    turn_id = params.get("turnId")
    return turn_id if isinstance(turn_id, str) else None


def _terminal_turn_text(turn: dict[str, Any]) -> str | None:
    items = turn.get("items", [])
    if not isinstance(items, list):
        return None
    for item in reversed(items):
        text = _terminal_turn_item_text(item)
        if text is not None:
            return text
    return None


def _terminal_turn_item_text(item: Any) -> str | None:
    if not isinstance(item, dict):
        return None
    item_type = item.get("type")
    text = item.get("text")
    if not isinstance(text, str):
        return None
    if item_type in {"Plan", "plan"}:
        return text
    if item_type == "agentMessage" and item.get("phase") in {None, "final_answer"}:
        return text
    return None


def _server_request_id(message: dict[str, Any]) -> int | None:
    raw_id = message.get("id")
    if isinstance(raw_id, int) and not isinstance(raw_id, bool):
        return raw_id
    if not isinstance(raw_id, str):
        return None
    try:
        return int(raw_id)
    except ValueError:
        return None


def _realtime_session(state: _RealtimeState) -> RealtimeSession:
    return RealtimeSession(
        chat_key=state.chat_key,
        logical_thread_id=state.logical_thread_id,
        codex_thread_id=state.codex_thread_id,
        codex_backend_id=state.codex_backend_id,
        session_id=state.session_id,
        status=state.status,
        last_text=state.last_text,
    )


def _codex_thread_from_payload(payload: dict[str, Any]) -> CodexThread:
    """Translate an app-server Thread object into the stable domain model."""
    return CodexThread(
        thread_id=str(payload["id"]),
        cwd=_optional_str(payload.get("cwd")),
        title=_optional_str(payload.get("title")),
        preview=_optional_str(payload.get("preview")),
        status=_thread_status(payload.get("status")),
        created_at=_int_timestamp(payload.get("createdAt")),
        updated_at=_int_timestamp(payload.get("updatedAt")),
        model_provider=_optional_str(payload.get("modelProvider")),
    )


def _skill_catalog_from_payload(payload: dict[str, Any]) -> SkillCatalog:
    errors = tuple(
        str(item.get("message", ""))
        for item in payload.get("errors", [])
        if isinstance(item, dict) and item.get("message")
    )
    return SkillCatalog(
        cwd=str(payload.get("cwd", "")),
        skills=tuple(
            _skill_from_payload(item)
            for item in payload.get("skills", [])
            if isinstance(item, dict)
        ),
        errors=errors,
    )


def _skill_from_payload(payload: dict[str, Any]) -> SkillCapability:
    interface = payload.get("interface")
    display_name = None
    if isinstance(interface, dict):
        display_name = _optional_str(interface.get("displayName"))
    return SkillCapability(
        name=str(payload.get("name", "")),
        path=str(payload.get("path", "")),
        scope=str(payload.get("scope", "user")),
        description=str(payload.get("description", "")),
        short_description=_optional_str(payload.get("shortDescription")),
        display_name=display_name,
        enabled=bool(payload.get("enabled", False)),
    )


def _mcp_server_from_payload(payload: dict[str, Any]) -> McpServerCapability:
    tools = payload.get("tools", {})
    resources = payload.get("resources", [])
    resource_templates = payload.get("resourceTemplates", [])
    return McpServerCapability(
        name=str(payload.get("name", "")),
        auth_status=str(payload.get("authStatus", "unsupported")),
        tools=tuple(
            _mcp_tool_from_payload(item)
            for item in (tools.values() if isinstance(tools, dict) else [])
            if isinstance(item, dict)
        ),
        resources=tuple(
            _mcp_resource_from_payload(item, template=False)
            for item in resources
            if isinstance(item, dict)
        ),
        resource_templates=tuple(
            _mcp_resource_from_payload(item, template=True)
            for item in resource_templates
            if isinstance(item, dict)
        ),
    )


def _mcp_tool_from_payload(payload: dict[str, Any]) -> McpToolCapability:
    return McpToolCapability(
        name=str(payload.get("name", "")),
        title=_optional_str(payload.get("title")),
        description=_optional_str(payload.get("description")),
    )


def _mcp_resource_from_payload(
    payload: dict[str, Any],
    *,
    template: bool,
) -> McpResourceCapability:
    uri_key = "uriTemplate" if template else "uri"
    return McpResourceCapability(
        name=str(payload.get("name", "")),
        uri=str(payload.get(uri_key, "")),
        title=_optional_str(payload.get("title")),
        description=_optional_str(payload.get("description")),
    )


def _optional_str(value: Any) -> str | None:
    if value is None:
        return None
    return str(value)


def _optional_int(value: Any) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _optional_float(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _int_timestamp(value: Any) -> int:
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str) and value.isdigit():
        return int(value)
    return 0


def _thread_status(value: Any) -> str:
    if isinstance(value, dict):
        status_type = value.get("type")
        if isinstance(status_type, str):
            return status_type
    if isinstance(value, str):
        return value
    return "unknown"


def _is_active_turn_mismatch(message: str, turn_id: str) -> bool:
    if message == "no active turn to interrupt":
        return True
    return (
        message.startswith(f"expected active turn id {turn_id} ")
        and " but found " in message
    )


def _build_turn_input(turn_input: str | UserTurnInput) -> list[dict[str, str]]:
    """Build app-server input items for one Telegram user message."""
    resolved = (
        turn_input
        if isinstance(turn_input, UserTurnInput)
        else UserTurnInput(text=turn_input)
    )
    items: list[dict[str, str]] = []
    if resolved.text:
        items.append({"type": "text", "text": resolved.text})
    for image in resolved.images:
        items.append({"type": "image", "url": image.url})
    for skill in resolved.skills:
        items.append({"type": "skill", "name": skill.name, "path": skill.path})
    return items


def _turn_start_params(
    *,
    thread_id: str,
    text: str | UserTurnInput,
    profile: ProfileDefinition,
    overrides: SessionOverrides,
) -> dict[str, object]:
    params: dict[str, object] = {
        "threadId": thread_id,
        "input": _build_turn_input(text),
    }
    if overrides.profile:
        params["permissions"] = {"type": "profile", "id": overrides.profile}
    if overrides.effort:
        params["effort"] = overrides.effort
    if overrides.summary:
        params["summary"] = overrides.summary
    if overrides.model:
        params["model"] = overrides.model
        params["modelProvider"] = _resolve_model_provider(overrides.model, profile)
    if overrides.cwd is not None:
        params["cwd"] = overrides.cwd
    if overrides.collaboration_mode is not None:
        params["collaborationMode"] = {
            "mode": overrides.collaboration_mode,
            "settings": {
                "model": overrides.model or profile.model,
                "reasoning_effort": overrides.effort or profile.effort,
                "developer_instructions": None,
            },
        }
    return params


def _developer_instructions(
    base: str | None,
    *,
    chat_key: str,
    logical_thread_id: str,
    anchor_id: str | None = None,
    codex_thread_id: str | None = None,
) -> str:
    """Append deployment context every Codex thread should know."""
    lines = [
        "Codex Telegram runtime context:",
        f"- chat_key: {chat_key}",
        (
            f"- logical_thread_id: {logical_thread_id} "
            "(logical_thread_id is the short-lived Telegram bridge id; use it "
            "for immediate Telegram bridge commands, notifications, refresh, "
            "and attachments.)"
        ),
    ]
    if anchor_id:
        lines.append(
            f"- anchor_id: {anchor_id} "
            "(anchor_id is the durable conversation id; use it for long-lived "
            "webhooks or subscriptions.)"
        )
    if codex_thread_id:
        lines.append(
            f"- codex_thread_id: {codex_thread_id} "
            "(backend thread id; informational unless a helper explicitly asks "
            "for it.)"
        )
    lines.extend(
        [
            (
                "- External event webhooks for this thread are managed by "
                "`codex-telegram`; use the `codex-telegram-webhook` helper or "
                "the bundled codex-telegram-webhooks skill."
            ),
            (
                "- Existing local files can be sent back to Telegram from the "
                "shared `/attachments` inbox with "
                "`codex-telegram-send-attachment --thread-id <logical_thread_id> "
                "/attachments/<name>`."
            ),
            (
                "- Do not create a separate webhook listener, Telegram bot, or "
                "remote shell execution endpoint from inside this runtime."
            ),
        ]
    )
    runtime_context = "\n".join(lines)
    if base and base.strip():
        return base.strip() + "\n\n" + runtime_context
    return runtime_context


def _extract_error_message(error: Any) -> str | None:
    if error is None:
        return None
    if isinstance(error, dict):
        message = error.get("message")
        if isinstance(message, str):
            return message
        return json.dumps(error)
    return str(error)


def _extract_command_text(params: dict[str, Any]) -> str | None:
    command = params.get("command")
    if isinstance(command, str):
        return command
    if isinstance(command, dict):
        text = command.get("command")
        return text if isinstance(text, str) else None
    return None


def _extract_reason_text(params: dict[str, Any]) -> str | None:
    reason = params.get("reason")
    return reason.strip() if isinstance(reason, str) and reason.strip() else None


def _extract_approval_message_text(params: dict[str, Any]) -> str | None:
    for key in ("message", "approvalMessage", "guardianMessage"):
        value = params.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    for container_name in ("guardian", "approval", "review"):
        container = params.get(container_name)
        if not isinstance(container, dict):
            continue
        for key in ("message", "summary", "approach"):
            value = container.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    return None


def _is_approval_request(method: str) -> bool:
    return method.endswith("/requestApproval") or "approval" in method.lower()


def _extract_user_input_questions(
    method: str, params: dict[str, Any]
) -> tuple[UserInputQuestion, ...] | None:
    questions_data = _question_payload(params)
    is_user_input_method = method in {
        "item/tool/requestUserInput",
        "request_user_input",
        "functions.request_user_input",
        "user_input/request",
    }
    if questions_data is None and not is_user_input_method:
        return None
    if questions_data is None:
        questions_data = []
    questions = tuple(
        question
        for index, item in enumerate(questions_data)
        if (question := _user_input_question_from_payload(item, index)) is not None
    )
    return questions if questions or is_user_input_method else None


def _question_payload(params: dict[str, Any]) -> list[Any] | None:
    questions = params.get("questions")
    if isinstance(questions, list):
        return questions
    for container_name in ("input", "arguments"):
        container = params.get(container_name)
        if isinstance(container, dict) and isinstance(container.get("questions"), list):
            return container["questions"]
    return None


def _user_input_question_from_payload(
    item: Any,
    index: int,
) -> UserInputQuestion | None:
    if not isinstance(item, dict):
        return None
    raw_question = item.get("question")
    if not isinstance(raw_question, str) or not raw_question.strip():
        return None
    raw_id = item.get("id")
    raw_header = item.get("header")
    raw_options = item.get("options")
    options = raw_options if isinstance(raw_options, list) else []
    return UserInputQuestion(
        question_id=(
            raw_id.strip()
            if isinstance(raw_id, str) and raw_id.strip()
            else f"question_{index + 1}"
        ),
        header=raw_header.strip() if isinstance(raw_header, str) else None,
        question=raw_question.strip(),
        options=tuple(
            option
            for option_item in options
            if (option := _user_input_option_from_payload(option_item)) is not None
        ),
    )


def _user_input_option_from_payload(item: Any) -> UserInputOption | None:
    if not isinstance(item, dict):
        return None
    label = item.get("label")
    if not isinstance(label, str) or not label.strip():
        return None
    description = item.get("description")
    return UserInputOption(
        label=label.strip(),
        description=description.strip() if isinstance(description, str) else "",
    )


def _extract_command_from_item(item: dict[str, Any]) -> str | None:
    command = item.get("command")
    if isinstance(command, str):
        return command
    if isinstance(command, dict):
        text = command.get("command")
        if isinstance(text, str):
            return text
    return None


def _extract_tool_name_from_item(item: dict[str, Any]) -> str | None:
    for key in ("name", "toolName", "method"):
        value = item.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    for key in ("tool", "function", "call"):
        value = item.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
        if isinstance(value, dict):
            name = value.get("name")
            if isinstance(name, str) and name.strip():
                return name.strip()
    return None


def _runtime_state_after_tool_item(
    current: CodexRuntimeState,
    tool_name: str,
    item: dict[str, Any],
) -> CodexRuntimeState:
    normalized = tool_name.rsplit(".", maxsplit=1)[-1]
    input_payload = _tool_object_payload(item, ("input", "arguments", "args"))
    result_payload = _tool_object_payload(item, ("result", "output", "content"))
    if normalized == "update_plan":
        plan = _extract_plan_items(input_payload) or _extract_plan_items(result_payload)
        if plan is None:
            return current
        return replace(current, plan_items=plan)
    if normalized == "create_goal":
        goal = _goal_from_payload(input_payload) or _goal_from_payload(result_payload)
        if goal is None:
            return current
        return replace(current, goal=goal)
    if normalized == "get_goal":
        goal_result = _goal_from_get_goal_payload(result_payload or input_payload)
        if goal_result is _NO_GOAL:
            return replace(current, goal=None)
        if isinstance(goal_result, CodexGoal):
            return replace(current, goal=goal_result)
        return current
    if normalized == "update_goal":
        status = _optional_str((input_payload or {}).get("status"))
        if status in {"complete", "completed"}:
            return replace(current, goal=None)
        if current.goal is not None and status:
            return replace(current, goal=replace(current.goal, status=status))
    return current


def _extract_plan_items(
    payload: dict[str, Any] | None,
) -> tuple[CodexPlanItem, ...] | None:
    if payload is None:
        return None
    raw_plan = payload.get("plan")
    if not isinstance(raw_plan, list):
        return None
    items: list[CodexPlanItem] = []
    for raw_item in raw_plan:
        if not isinstance(raw_item, dict):
            continue
        step = raw_item.get("step")
        status = raw_item.get("status")
        if isinstance(step, str) and step.strip() and isinstance(status, str):
            items.append(CodexPlanItem(step=step.strip(), status=status.strip()))
    return tuple(items)


def _tool_object_payload(
    item: dict[str, Any],
    keys: tuple[str, ...],
) -> dict[str, Any] | None:
    for key in keys:
        value = item.get(key)
        payload = _coerce_object_payload(value)
        if payload is not None:
            return payload
    return None


def _coerce_object_payload(value: Any) -> dict[str, Any] | None:
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return None
        return parsed if isinstance(parsed, dict) else None
    if isinstance(value, list):
        text = "".join(
            block.get("text", "")
            for block in value
            if isinstance(block, dict) and isinstance(block.get("text"), str)
        )
        if text:
            return _coerce_object_payload(text)
    return None


_NO_GOAL = object()


def _goal_from_get_goal_payload(
    payload: dict[str, Any] | None,
) -> CodexGoal | object | None:
    if payload is None:
        return None
    raw_goal = payload.get("goal")
    if "goal" not in payload:
        return _goal_from_payload(payload)
    if raw_goal is None:
        return _NO_GOAL
    if isinstance(raw_goal, dict):
        goal = _goal_from_payload(raw_goal)
        if goal is not None:
            return goal
    return None


def _goal_from_payload(payload: dict[str, Any] | None) -> CodexGoal | None:
    if payload is None:
        return None
    objective = payload.get("objective")
    if not isinstance(objective, str) or not objective.strip():
        return None
    raw_status = payload.get("status")
    status = raw_status if isinstance(raw_status, str) and raw_status else "active"
    if status in {"complete", "completed"}:
        return None
    return CodexGoal(
        objective=objective.strip(),
        status=status,
        token_budget=_optional_int(
            payload.get("tokenBudget", payload.get("token_budget"))
        ),
        tokens_used=_optional_int(
            payload.get("tokensUsed", payload.get("tokens_used"))
        ),
        elapsed_seconds=_optional_float(
            payload.get("elapsedSeconds", payload.get("elapsed_seconds"))
        ),
        created_at=_optional_str(payload.get("createdAt", payload.get("created_at"))),
        updated_at=_optional_str(payload.get("updatedAt", payload.get("updated_at"))),
    )


def _extract_raw_response_text(item: Any) -> str | None:
    if not isinstance(item, dict):
        return None
    text = item.get("text")
    if isinstance(text, str):
        return text
    content = item.get("content")
    if isinstance(content, list):
        parts = [
            block.get("text")
            for block in content
            if isinstance(block, dict) and isinstance(block.get("text"), str)
        ]
        if parts:
            return "".join(str(part) for part in parts)
    return None


def _extract_result_images(item: Any) -> tuple[TurnResultImage, ...]:
    if not isinstance(item, dict):
        return ()
    images: list[TurnResultImage] = []
    item_type = item.get("type")
    if item_type == "imageView":
        image = _image_from_source(item.get("path"))
        if image is not None:
            images.append(image)
    elif item_type == "imageGeneration":
        caption = _optional_str(item.get("revisedPrompt"))
        source = _image_source(item.get("savedPath")) or _image_source(
            item.get("result")
        )
        if source is not None:
            images.append(TurnResultImage(source=source, caption=caption))
    content = item.get("content")
    if isinstance(content, list):
        images.extend(
            image
            for block in content
            if isinstance(block, dict)
            if (image := _image_from_content_block(block)) is not None
        )
    return tuple(images)


def _image_from_content_block(block: dict[str, Any]) -> TurnResultImage | None:
    block_type = block.get("type")
    if not isinstance(block_type, str) or "image" not in block_type.lower():
        return None
    caption = _optional_str(block.get("caption"))
    for key in ("url", "imageUrl", "image_url", "path", "data"):
        source = _image_source(block.get(key))
        if source is not None:
            return TurnResultImage(source=source, caption=caption)
    nested = block.get("image_url")
    if isinstance(nested, dict):
        source = _image_source(nested.get("url"))
        if source is not None:
            return TurnResultImage(source=source, caption=caption)
    return None


def _image_from_source(value: Any) -> TurnResultImage | None:
    source = _image_source(value)
    if source is None:
        return None
    return TurnResultImage(source=source)


def _image_source(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    source = value.strip()
    if not source:
        return None
    if source.startswith(("/", "file://", "http://", "https://", "data:image/")):
        return source
    return None


def _extract_token_usage(payload: dict[str, Any]) -> dict[str, int] | None:
    usage: Any = payload
    if isinstance(payload.get("tokenUsage"), dict):
        usage = payload["tokenUsage"]
    elif isinstance(payload.get("usage"), dict):
        usage = payload["usage"]
    if not isinstance(usage, dict):
        return None
    normalized = _normalize_token_usage(usage)
    return normalized if normalized else None


def _extract_thread_token_usage(payload: dict[str, Any]) -> dict[str, int] | None:
    thread = payload.get("thread")
    if isinstance(thread, dict):
        usage = thread.get("tokenUsage")
        if isinstance(usage, dict):
            normalized = _extract_token_usage(usage.get("last", usage))
            if normalized is not None:
                return normalized
    usage = payload.get("tokenUsage")
    if isinstance(usage, dict):
        normalized = _extract_token_usage(usage.get("last", usage))
        if normalized is not None:
            return normalized
    info = payload.get("info")
    if isinstance(info, dict):
        usage = info.get("last_token_usage") or info.get("lastTokenUsage")
        if isinstance(usage, dict):
            return _extract_token_usage(usage)
    return None


def _extract_runtime_usage_metrics(
    payload: dict[str, Any],
) -> RuntimeUsageMetrics | None:
    info = payload.get("info")
    if not isinstance(info, dict):
        usage = payload.get("tokenUsage")
        if isinstance(usage, dict):
            info = usage
    if not isinstance(info, dict):
        thread = payload.get("thread")
        if isinstance(thread, dict):
            info = thread.get("runtimeMetrics") or thread.get("metrics")
    if not isinstance(info, dict):
        return None

    total = (
        info.get("total_token_usage")
        or info.get("totalTokenUsage")
        or info.get("total")
    )
    last = (
        info.get("last_token_usage") or info.get("lastTokenUsage") or info.get("last")
    )
    context_window = info.get("model_context_window") or info.get("modelContextWindow")
    metrics = RuntimeUsageMetrics(
        total_token_usage=(
            _extract_token_usage(total) if isinstance(total, dict) else None
        ),
        last_token_usage=_extract_token_usage(last) if isinstance(last, dict) else None,
        model_context_window=(
            int(context_window) if context_window is not None else None
        ),
    )
    if (
        metrics.total_token_usage is None
        and metrics.last_token_usage is None
        and metrics.model_context_window is None
    ):
        return None
    return metrics


def _normalize_token_usage(usage: dict[str, Any]) -> dict[str, int]:
    keys = (
        "input_tokens",
        "cached_input_tokens",
        "output_tokens",
        "reasoning_output_tokens",
        "total_tokens",
    )
    normalized: dict[str, int] = {}
    for key in keys:
        raw = usage.get(key) or usage.get(_snake_to_camel(key))
        if raw is not None:
            normalized[key] = int(raw)
    return normalized


def _account_usage_from_rate_limits_response(result: dict[str, Any]) -> AccountUsage:
    rate_limits = result.get("rateLimits") or result.get("rate_limits")
    if not isinstance(rate_limits, dict):
        by_id = result.get("rateLimitsByLimitId") or result.get(
            "rate_limits_by_limit_id"
        )
        if isinstance(by_id, dict):
            first = next(
                (value for value in by_id.values() if isinstance(value, dict)), None
            )
            rate_limits = first
    if not isinstance(rate_limits, dict):
        return AccountUsage(
            status="unavailable",
            reason="app-server did not return account rate limits",
        )

    limits = tuple(
        limit
        for key in ("primary", "secondary", "tertiary")
        if (limit := _account_usage_limit_from_window(key, rate_limits.get(key)))
        is not None
    )
    if not limits:
        return AccountUsage(
            status="unavailable",
            reason="app-server returned no account rate-limit windows",
        )
    plan_type = _optional_str(
        rate_limits.get("planType") or rate_limits.get("plan_type")
    )
    return AccountUsage(status="available", limits=limits, reason=plan_type)


def _account_usage_limit_from_window(
    window_name: str,
    window: Any,
) -> AccountUsageLimit | None:
    if not isinstance(window, dict):
        return None
    window_minutes = _optional_int(
        window.get("windowMinutes") or window.get("window_minutes")
    )
    used_percent = _optional_float(
        window.get("usedPercent") or window.get("used_percent")
    )
    resets_at = _format_reset_time(window.get("resetsAt") or window.get("resets_at"))
    if used_percent is None and window_minutes is None and resets_at is None:
        return None
    return AccountUsageLimit(
        label=_rate_limit_label(window_name, window_minutes),
        used_percent=used_percent,
        window_minutes=window_minutes,
        resets_at=resets_at,
    )


def _rate_limit_label(window_name: str, window_minutes: int | None) -> str:
    if window_minutes == 300:
        return "5h limit"
    if window_minutes == 10080:
        return "Weekly limit"
    if window_minutes is not None and 40320 <= window_minutes <= 44640:
        return "Monthly limit"
    return f"{window_name.capitalize()} limit"


def _format_reset_time(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        return value
    try:
        timestamp = int(value)
    except (TypeError, ValueError):
        return None
    return datetime.fromtimestamp(timestamp, tz=UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _snake_to_camel(value: str) -> str:
    first, *rest = value.split("_")
    return first + "".join(part.capitalize() for part in rest)


def _sandbox_policy(profile: ProfileDefinition) -> dict[str, Any]:
    sandbox_type = profile.sandbox_type
    if sandbox_type == "workspaceWrite":
        return {
            "type": "workspaceWrite",
            "writableRoots": list(profile.writable_roots),
            "networkAccess": profile.network_access,
        }
    if sandbox_type == "readOnly":
        return {"type": "readOnly", "networkAccess": profile.network_access}
    return {"type": sandbox_type, "networkAccess": profile.network_access}


def _resolve_model_provider(model: str, profile: ProfileDefinition) -> str:
    if model.startswith(("qwen", "llama", "mistral", "phi", "gemma", "deepseek")):
        return "llama"
    return profile.model_provider
