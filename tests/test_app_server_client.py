import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from codex_telegram.adapters.codex_app_server.client import (
    CodexAppServerError,
    CodexAppServerClient,
    _build_turn_input,
    _extract_user_input_questions,
    _sandbox_policy,
)
from codex_telegram.application.models import RuntimeUsageMetrics
from codex_telegram.domain import (
    CodexGoal,
    CodexThread,
    PendingApproval,
    PendingUserInput,
    RealtimeEvent,
    ProfileDefinition,
    SessionOverrides,
    TurnResult,
    TurnResultImage,
    TurnUpdate,
    UserTurnImage,
    UserTurnInput,
    UserTurnSkill,
)


@pytest.mark.asyncio
async def test_get_usage_fetches_account_rate_limits() -> None:
    client = CodexAppServerClient(AsyncMock(), "ws://127.0.0.1:4312", "token")
    client._ws_request = AsyncMock(  # type: ignore[method-assign]
        return_value={
            "rateLimits": {
                "limitId": "codex",
                "limitName": "Codex",
                "planType": "prolite",
                "primary": {
                    "usedPercent": 22.5,
                    "windowMinutes": 300,
                    "resetsAt": 1778171508,
                },
                "secondary": {
                    "usedPercent": 53,
                    "windowMinutes": 10080,
                    "resetsAt": 1778549353,
                },
            }
        }
    )

    usage = await client.get_usage()

    assert usage.status == "available"
    assert usage.reason == "prolite"
    assert [limit.label for limit in usage.limits] == [
        "5h limit",
        "Weekly limit",
    ]
    assert usage.limits[0].used_percent == 22.5
    assert usage.limits[0].window_minutes == 300
    assert usage.limits[0].resets_at == "2026-05-07T16:31:48Z"
    client._ws_request.assert_awaited_once_with("account/rateLimits/read", {})  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_get_usage_returns_unavailable_for_missing_rate_limits() -> None:
    client = CodexAppServerClient(AsyncMock(), "ws://127.0.0.1:4312", "token")
    client._ws_request = AsyncMock(return_value={})  # type: ignore[method-assign]

    usage = await client.get_usage()

    assert usage.status == "unavailable"
    assert usage.reason == "app-server did not return account rate limits"
    assert usage.limits == ()


@pytest.mark.asyncio
async def test_list_skills_fetches_runtime_skill_catalog() -> None:
    client = CodexAppServerClient(AsyncMock(), "ws://127.0.0.1:4312", "token")
    client._ws_request = AsyncMock(  # type: ignore[method-assign]
        return_value={
            "data": [
                {
                    "cwd": "/agent/app",
                    "errors": [],
                    "skills": [
                        {
                            "name": "codex-telegram-webhooks",
                            "path": "/root/.codex/skills/codex-telegram-webhooks/SKILL.md",
                            "scope": "repo",
                            "description": "Manage Telegram webhooks.",
                            "shortDescription": "Manage webhooks.",
                            "enabled": True,
                        }
                    ],
                }
            ]
        }
    )

    catalogs = await client.list_skills(cwd="/agent/app", force_reload=True)

    assert len(catalogs) == 1
    assert catalogs[0].cwd == "/agent/app"
    assert catalogs[0].skills[0].name == "codex-telegram-webhooks"
    assert catalogs[0].skills[0].scope == "repo"
    assert catalogs[0].skills[0].enabled is True
    client._ws_request.assert_awaited_once_with(  # type: ignore[attr-defined]
        "skills/list",
        {"cwds": ["/agent/app"], "forceReload": True},
    )


@pytest.mark.asyncio
async def test_list_mcp_servers_fetches_paginated_status() -> None:
    client = CodexAppServerClient(AsyncMock(), "ws://127.0.0.1:4312", "token")
    client._ws_request = AsyncMock(  # type: ignore[method-assign]
        side_effect=[
            {
                "data": [
                    {
                        "name": "filesystem",
                        "authStatus": "unsupported",
                        "tools": {
                            "read_file": {
                                "name": "read_file",
                                "title": "Read file",
                                "description": "Read a file.",
                                "inputSchema": {},
                            }
                        },
                        "resources": [
                            {
                                "name": "repo",
                                "uri": "file:///agent",
                                "title": "Repo",
                                "description": "Workspace.",
                            }
                        ],
                        "resourceTemplates": [],
                    }
                ],
                "nextCursor": "next",
            },
            {
                "data": [
                    {
                        "name": "browser",
                        "authStatus": "notLoggedIn",
                        "tools": {},
                        "resources": [],
                        "resourceTemplates": [
                            {
                                "name": "tab",
                                "uriTemplate": "browser://tabs/{id}",
                                "title": "Tab",
                            }
                        ],
                    }
                ],
                "nextCursor": None,
            },
        ]
    )

    servers = await client.list_mcp_servers()

    assert [server.name for server in servers] == ["filesystem", "browser"]
    assert servers[0].tools[0].name == "read_file"
    assert servers[0].resources[0].uri == "file:///agent"
    assert servers[1].resource_templates[0].uri == "browser://tabs/{id}"
    assert client._ws_request.await_args_list[0].args == (  # type: ignore[attr-defined]
        "mcpServerStatus/list",
        {"detail": "full"},
    )
    assert client._ws_request.await_args_list[1].args == (  # type: ignore[attr-defined]
        "mcpServerStatus/list",
        {"detail": "full", "cursor": "next"},
    )


def test_build_turn_input_includes_skill_items() -> None:
    turn_input = UserTurnInput(
        text="Use this skill to create a webhook.",
        skills=(
            UserTurnSkill(
                name="codex-telegram-webhooks",
                path="/root/.codex/skills/codex-telegram-webhooks/SKILL.md",
            ),
        ),
    )

    assert _build_turn_input(turn_input) == [
        {"type": "text", "text": "Use this skill to create a webhook."},
        {
            "type": "skill",
            "name": "codex-telegram-webhooks",
            "path": "/root/.codex/skills/codex-telegram-webhooks/SKILL.md",
        },
    ]


@pytest.mark.asyncio
async def test_thread_token_usage_updates_runtime_state() -> None:
    client = CodexAppServerClient(AsyncMock(), "ws://127.0.0.1:4312", "token")
    client._turns["turn-1"] = SimpleNamespace(  # type: ignore[assignment]
        turn_id="turn-1",
        chat_key="chat:1",
        logical_thread_id="thread-1",
        codex_thread_id="codex-1",
        codex_backend_id="primary",
        status="inProgress",
        token_usage=None,
    )

    client._handle_notification(
        "thread/tokenUsage/updated",
        {
            "turnId": "turn-1",
            "thread": {
                "tokenUsage": {
                    "inputTokens": 12,
                    "cachedInputTokens": 3,
                    "outputTokens": 5,
                    "reasoningOutputTokens": 2,
                    "totalTokens": 17,
                }
            },
        },
    )

    assert client.get_runtime_state("codex-1").token_usage == {
        "input_tokens": 12,
        "cached_input_tokens": 3,
        "output_tokens": 5,
        "reasoning_output_tokens": 2,
        "total_tokens": 17,
    }


@pytest.mark.asyncio
async def test_thread_token_usage_uses_app_server_last_snapshot() -> None:
    client = CodexAppServerClient(AsyncMock(), "ws://127.0.0.1:4312", "token")
    client._turns["turn-1"] = SimpleNamespace(  # type: ignore[assignment]
        turn_id="turn-1",
        chat_key="chat:1",
        logical_thread_id="thread-1",
        codex_thread_id="codex-1",
        codex_backend_id="primary",
        status="inProgress",
        token_usage=None,
    )

    client._handle_notification(
        "thread/tokenUsage/updated",
        {
            "threadId": "codex-1",
            "turnId": "turn-1",
            "tokenUsage": {
                "last": {
                    "inputTokens": 12,
                    "cachedInputTokens": 2,
                    "outputTokens": 5,
                    "reasoningOutputTokens": 1,
                    "totalTokens": 17,
                },
                "total": {
                    "inputTokens": 120,
                    "cachedInputTokens": 20,
                    "outputTokens": 50,
                    "reasoningOutputTokens": 10,
                    "totalTokens": 170,
                },
                "modelContextWindow": 128000,
            },
        },
    )

    assert client.get_runtime_state("codex-1").token_usage == {
        "input_tokens": 12,
        "cached_input_tokens": 2,
        "output_tokens": 5,
        "reasoning_output_tokens": 1,
        "total_tokens": 17,
    }
    assert client.get_runtime_state("codex-1").usage_metrics == RuntimeUsageMetrics(
        total_token_usage={
            "input_tokens": 120,
            "cached_input_tokens": 20,
            "output_tokens": 50,
            "reasoning_output_tokens": 10,
            "total_tokens": 170,
        },
        last_token_usage={
            "input_tokens": 12,
            "cached_input_tokens": 2,
            "output_tokens": 5,
            "reasoning_output_tokens": 1,
            "total_tokens": 17,
        },
        model_context_window=128000,
    )


@pytest.mark.asyncio
async def test_turn_completed_uses_app_server_turn_usage_for_runtime_state() -> None:
    client = CodexAppServerClient(AsyncMock(), "ws://127.0.0.1:4312", "token")
    client._turns["turn-1"] = SimpleNamespace(  # type: ignore[assignment]
        turn_id="turn-1",
        chat_key="chat:1",
        logical_thread_id="thread-1",
        codex_thread_id="codex-1",
        codex_backend_id="primary",
        status="inProgress",
        final_text="Done",
        final_images=[],
        error=None,
        token_usage=None,
        completed=asyncio.get_running_loop().create_future(),
        queue=asyncio.Queue(),
    )

    client._handle_notification(
        "turn/completed",
        {
            "threadId": "codex-1",
            "turn": {
                "id": "turn-1",
                "status": "completed",
                "usage": {
                    "inputTokens": 12,
                    "cachedInputTokens": 2,
                    "outputTokens": 5,
                    "reasoningOutputTokens": 1,
                    "totalTokens": 17,
                },
            },
        },
    )

    assert client.get_runtime_state("codex-1").token_usage == {
        "input_tokens": 12,
        "cached_input_tokens": 2,
        "output_tokens": 5,
        "reasoning_output_tokens": 1,
        "total_tokens": 17,
    }


def test_thread_token_usage_updates_runtime_metrics_state() -> None:
    client = CodexAppServerClient(AsyncMock(), "ws://127.0.0.1:4312", "token")
    client._turns["turn-1"] = SimpleNamespace(  # type: ignore[assignment]
        turn_id="turn-1",
        chat_key="chat:1",
        logical_thread_id="thread-1",
        codex_thread_id="codex-1",
        codex_backend_id="primary",
        status="inProgress",
        token_usage=None,
    )

    client._handle_notification(
        "thread/tokenUsage/updated",
        {
            "turnId": "turn-1",
            "info": {
                "total_token_usage": {
                    "input_tokens": 120,
                    "cached_input_tokens": 40,
                    "output_tokens": 30,
                    "reasoning_output_tokens": 10,
                    "total_tokens": 160,
                },
                "last_token_usage": {
                    "input_tokens": 50,
                    "cached_input_tokens": 20,
                    "output_tokens": 7,
                    "reasoning_output_tokens": 2,
                    "total_tokens": 57,
                },
                "model_context_window": 258400,
            },
        },
    )

    assert client.get_runtime_state("codex-1").usage_metrics == RuntimeUsageMetrics(
        total_token_usage={
            "input_tokens": 120,
            "cached_input_tokens": 40,
            "output_tokens": 30,
            "reasoning_output_tokens": 10,
            "total_tokens": 160,
        },
        last_token_usage={
            "input_tokens": 50,
            "cached_input_tokens": 20,
            "output_tokens": 7,
            "reasoning_output_tokens": 2,
            "total_tokens": 57,
        },
        model_context_window=258400,
    )


def test_build_turn_input_uses_text_items() -> None:
    assert _build_turn_input("Hello") == [{"type": "text", "text": "Hello"}]


def test_build_turn_input_includes_inline_images() -> None:
    assert _build_turn_input(
        UserTurnInput(
            text="Check this",
            images=(
                UserTurnImage(url="data:image/png;base64,YWJj"),
                UserTurnImage(url="data:image/jpeg;base64,ZA=="),
            ),
        )
    ) == [
        {"type": "text", "text": "Check this"},
        {"type": "image", "url": "data:image/png;base64,YWJj"},
        {"type": "image", "url": "data:image/jpeg;base64,ZA=="},
    ]


def test_sandbox_policy_uses_workspace_write_variant() -> None:
    profile = ProfileDefinition(
        name="operator",
        model="gpt-5.4",
        model_provider="openai",
        approval_policy="untrusted",
        sandbox_type="workspaceWrite",
        network_access=False,
        writable_roots=("/tmp",),
    )

    assert _sandbox_policy(profile) == {
        "type": "workspaceWrite",
        "writableRoots": ["/tmp"],
        "networkAccess": False,
    }


def test_sandbox_policy_uses_read_only_variant() -> None:
    profile = ProfileDefinition(
        name="readonly",
        model="gpt-5.4",
        model_provider="openai",
        approval_policy="never",
        sandbox_type="readOnly",
        network_access=False,
    )

    assert _sandbox_policy(profile) == {
        "type": "readOnly",
        "networkAccess": False,
    }


def _build_profile() -> ProfileDefinition:
    return ProfileDefinition(
        name="operator",
        model="gpt-5.4",
        model_provider="openai",
        approval_policy="untrusted",
        sandbox_type="workspaceWrite",
        network_access=False,
    )


@pytest.mark.asyncio
async def test_steer_turn_uses_turn_steer_api() -> None:
    client = CodexAppServerClient(AsyncMock(), "ws://127.0.0.1:4312", "token")
    requests: list[tuple[str, dict[str, object]]] = []

    async def fake_ws_request(
        method: str, params: dict[str, object]
    ) -> dict[str, object]:
        requests.append((method, params))
        if method == "turn/start":
            return {"turn": {"id": "turn-1"}}
        if method == "turn/steer":
            return {"turnId": "turn-1"}
        raise AssertionError(f"Unexpected method: {method}")

    client._ws_request = AsyncMock(side_effect=fake_ws_request)  # type: ignore[method-assign]
    profile = _build_profile()
    await client.start_turn(
        "chat:1",
        "thread-1",
        "codex-1",
        "Initial prompt",
        profile,
        SessionOverrides(followup_mode="queue"),
    )

    await client.steer_turn(
        logical_thread_id="thread-1",
        codex_thread_id="codex-1",
        turn_id="turn-1",
        text="Actually focus on failing tests first.",
    )

    assert requests[-1] == (
        "turn/steer",
        {
            "threadId": "codex-1",
            "input": [
                {"type": "text", "text": "Actually focus on failing tests first."}
            ],
            "expectedTurnId": "turn-1",
        },
    )


@pytest.mark.asyncio
async def test_stale_active_turn_mismatch_fails_local_turn_on_interrupt() -> None:
    client = CodexAppServerClient(AsyncMock(), "ws://127.0.0.1:4312", "token")

    async def fake_ws_request(
        method: str, params: dict[str, object]
    ) -> dict[str, object]:
        if method == "turn/start":
            return {"turn": {"id": "turn-1"}}
        if method == "turn/interrupt":
            raise CodexAppServerError(
                "expected active turn id turn-1 but found turn-other"
            )
        raise AssertionError(f"Unexpected method: {method}")

    client._ws_request = AsyncMock(side_effect=fake_ws_request)  # type: ignore[method-assign]
    profile = _build_profile()
    accepted = await client.start_turn(
        "chat:1",
        "thread-1",
        "codex-1",
        "Initial prompt",
        profile,
        SessionOverrides(followup_mode="queue"),
    )

    await client.interrupt_turn(accepted.turn_id)

    event = await client.wait_for_turn_event(accepted.turn_id, timeout=0.1)
    assert isinstance(event, TurnResult)
    assert event.status == "failed"
    assert event.error == "expected active turn id turn-1 but found turn-other"


@pytest.mark.asyncio
async def test_stale_active_turn_mismatch_fails_local_turn_on_steer() -> None:
    client = CodexAppServerClient(AsyncMock(), "ws://127.0.0.1:4312", "token")

    async def fake_ws_request(
        method: str, params: dict[str, object]
    ) -> dict[str, object]:
        if method == "turn/start":
            return {"turn": {"id": "turn-1"}}
        if method == "turn/steer":
            raise CodexAppServerError(
                "expected active turn id turn-1 but found turn-other"
            )
        raise AssertionError(f"Unexpected method: {method}")

    client._ws_request = AsyncMock(side_effect=fake_ws_request)  # type: ignore[method-assign]
    profile = _build_profile()
    accepted = await client.start_turn(
        "chat:1",
        "thread-1",
        "codex-1",
        "Initial prompt",
        profile,
        SessionOverrides(followup_mode="queue"),
    )

    with pytest.raises(CodexAppServerError):
        await client.steer_turn(
            logical_thread_id="thread-1",
            codex_thread_id="codex-1",
            turn_id=accepted.turn_id,
            text="Follow up.",
        )

    event = await client.wait_for_turn_event(accepted.turn_id, timeout=0.1)
    assert isinstance(event, TurnResult)
    assert event.status == "failed"
    assert event.error == "expected active turn id turn-1 but found turn-other"


@pytest.mark.asyncio
async def test_interrupt_treats_no_active_turn_as_stale_local_turn() -> None:
    client = CodexAppServerClient(AsyncMock(), "ws://127.0.0.1:4312", "token")

    async def fake_ws_request(
        method: str, params: dict[str, object]
    ) -> dict[str, object]:
        if method == "turn/start":
            return {"turn": {"id": "turn-1"}}
        if method == "turn/interrupt":
            raise CodexAppServerError("no active turn to interrupt")
        raise AssertionError(f"Unexpected method: {method}")

    client._ws_request = AsyncMock(side_effect=fake_ws_request)  # type: ignore[method-assign]
    profile = _build_profile()
    accepted = await client.start_turn(
        "chat:1",
        "thread-1",
        "codex-1",
        "Initial prompt",
        profile,
        SessionOverrides(followup_mode="queue"),
    )

    await client.interrupt_turn(
        accepted.turn_id,
        codex_thread_id="codex-1",
    )

    result = await client.wait_for_turn_event(accepted.turn_id, timeout=0.1)
    assert isinstance(result, TurnResult)
    assert result.status == "failed"
    assert result.error == "no active turn to interrupt"


@pytest.mark.asyncio
async def test_interrupt_treats_no_active_turn_as_reconciled_after_restart() -> None:
    client = CodexAppServerClient(AsyncMock(), "ws://127.0.0.1:4312", "token")

    async def fake_ws_request(
        method: str, params: dict[str, object]
    ) -> dict[str, object]:
        if method == "turn/interrupt":
            raise CodexAppServerError("no active turn to interrupt")
        raise AssertionError(f"Unexpected method: {method}")

    client._ws_request = AsyncMock(side_effect=fake_ws_request)  # type: ignore[method-assign]

    await client.interrupt_turn("turn-1", codex_thread_id="codex-1")


@pytest.mark.asyncio
async def test_realtime_start_append_and_stop_use_thread_realtime_api() -> None:
    client = CodexAppServerClient(AsyncMock(), "ws://127.0.0.1:4312", "token")
    requests: list[tuple[str, dict[str, object]]] = []

    async def fake_ws_request(
        method: str, params: dict[str, object]
    ) -> dict[str, object]:
        requests.append((method, params))
        return {}

    client._ws_request = AsyncMock(side_effect=fake_ws_request)  # type: ignore[method-assign]

    session = await client.start_realtime(
        "chat:1",
        "thread-1",
        "codex-1",
        codex_backend_id="primary",
    )
    await client.append_realtime_text(
        "codex-1",
        "Keep going.",
        codex_backend_id="primary",
    )
    await client.stop_realtime("codex-1", codex_backend_id="primary")

    assert session.logical_thread_id == "thread-1"
    assert session.codex_thread_id == "codex-1"
    assert requests == [
        (
            "thread/realtime/start",
            {
                "threadId": "codex-1",
                "outputModality": "text",
            },
        ),
        (
            "thread/realtime/appendText",
            {
                "threadId": "codex-1",
                "text": "Keep going.",
            },
        ),
        ("thread/realtime/stop", {"threadId": "codex-1"}),
    ]


@pytest.mark.asyncio
async def test_realtime_notifications_are_routed_to_matching_thread() -> None:
    client = CodexAppServerClient(AsyncMock(), "ws://127.0.0.1:4312", "token")
    client._ws_request = AsyncMock(return_value={})  # type: ignore[method-assign]

    await client.start_realtime(
        "chat:1",
        "thread-1",
        "codex-1",
        codex_backend_id="primary",
    )
    client._handle_notification(
        "thread/realtime/transcript/delta",
        {
            "threadId": "codex-1",
            "role": "assistant",
            "delta": "Hello",
        },
    )
    client._handle_notification(
        "thread/realtime/transcript/delta",
        {
            "threadId": "codex-other",
            "role": "assistant",
            "delta": "Wrong",
        },
    )

    event = await client.wait_for_realtime_event(
        "codex-1",
        timeout=0.01,
        codex_backend_id="primary",
    )

    assert isinstance(event, RealtimeEvent)
    assert event.event_type == "transcript_delta"
    assert event.text == "Hello"
    assert event.logical_thread_id == "thread-1"


@pytest.mark.asyncio
async def test_realtime_started_notification_uses_realtime_session_id() -> None:
    client = CodexAppServerClient(AsyncMock(), "ws://127.0.0.1:4312", "token")
    client._ws_request = AsyncMock(return_value={})  # type: ignore[method-assign]

    await client.start_realtime(
        "chat:1",
        "thread-1",
        "codex-1",
        codex_backend_id="primary",
    )
    client._handle_notification(
        "thread/realtime/started",
        {
            "threadId": "codex-1",
            "realtimeSessionId": "sess-1",
        },
    )

    event = await client.wait_for_realtime_event(
        "codex-1",
        timeout=0.01,
        codex_backend_id="primary",
    )

    assert event.event_type == "started"
    assert event.session_id == "sess-1"


@pytest.mark.asyncio
async def test_thread_binding_injects_telegram_webhook_runtime_context() -> None:
    client = CodexAppServerClient(AsyncMock(), "ws://127.0.0.1:4312", "token")
    requests: list[tuple[str, dict[str, object]]] = []

    async def fake_ws_request(
        method: str, params: dict[str, object]
    ) -> dict[str, object]:
        requests.append((method, params))
        if method == "thread/start":
            return {"thread": {"id": "codex-1"}}
        raise AssertionError(f"Unexpected method: {method}")

    client._ensure_connected = AsyncMock()  # type: ignore[method-assign]
    client._ws_request = AsyncMock(side_effect=fake_ws_request)  # type: ignore[method-assign]
    profile = ProfileDefinition(
        name="operator",
        model="gpt-5.4",
        model_provider="openai",
        approval_policy="untrusted",
        sandbox_type="workspaceWrite",
        network_access=False,
        developer_instructions="Base profile instructions.",
    )

    await client.ensure_thread_binding(
        "chat:1",
        "thread-1",
        None,
        profile,
        SessionOverrides(),
        anchor_id="anchor-1",
    )

    assert requests[0][0] == "thread/start"
    developer_instructions = requests[0][1]["developerInstructions"]
    assert isinstance(developer_instructions, str)
    assert "Base profile instructions." in developer_instructions
    assert "chat_key: chat:1" in developer_instructions
    assert "logical_thread_id: thread-1" in developer_instructions
    assert "anchor_id: anchor-1" in developer_instructions
    assert "logical_thread_id is the short-lived Telegram bridge id" in (
        developer_instructions
    )
    assert "anchor_id is the durable conversation id" in developer_instructions
    assert "codex-telegram-webhook" in developer_instructions
    assert "codex-telegram-send-attachment" in developer_instructions
    assert "/attachments" in developer_instructions


@pytest.mark.asyncio
async def test_thread_resume_injects_known_codex_thread_id() -> None:
    client = CodexAppServerClient(AsyncMock(), "ws://127.0.0.1:4312", "token")
    requests: list[tuple[str, dict[str, object]]] = []

    async def fake_ws_request(
        method: str, params: dict[str, object]
    ) -> dict[str, object]:
        requests.append((method, params))
        if method == "thread/resume":
            return {}
        raise AssertionError(f"Unexpected method: {method}")

    client._ensure_connected = AsyncMock()  # type: ignore[method-assign]
    client._ws_request = AsyncMock(side_effect=fake_ws_request)  # type: ignore[method-assign]

    await client.ensure_thread_binding(
        "chat:1",
        "thread-1",
        "codex-1",
        _build_profile(),
        SessionOverrides(),
        anchor_id="anchor-1",
    )

    assert requests[0][0] == "thread/resume"
    developer_instructions = requests[0][1]["developerInstructions"]
    assert isinstance(developer_instructions, str)
    assert "anchor_id: anchor-1" in developer_instructions
    assert "codex_thread_id: codex-1" in developer_instructions
    assert "backend thread id; informational" in developer_instructions


@pytest.mark.asyncio
async def test_thread_binding_sends_cwd_without_legacy_permission_defaults() -> None:
    client = CodexAppServerClient(AsyncMock(), "ws://127.0.0.1:4312", "token")
    requests: list[tuple[str, dict[str, object]]] = []

    async def fake_ws_request(
        method: str, params: dict[str, object]
    ) -> dict[str, object]:
        requests.append((method, params))
        if method == "thread/start":
            return {"thread": {"id": "codex-1"}}
        raise AssertionError(f"Unexpected method: {method}")

    client._ensure_connected = AsyncMock()  # type: ignore[method-assign]
    client._ws_request = AsyncMock(side_effect=fake_ws_request)  # type: ignore[method-assign]

    await client.ensure_thread_binding(
        "chat:1",
        "thread-1",
        None,
        _build_profile(),
        SessionOverrides(cwd="/agent/project-a"),
    )

    params = requests[0][1]
    assert params["cwd"] == "/agent/project-a"
    assert "approvalPolicy" not in params
    assert "sandboxPolicy" not in params
    assert "permissions" not in params
    assert "model" not in params
    assert "modelProvider" not in params


@pytest.mark.asyncio
async def test_turn_start_uses_profile_permission_only_when_explicitly_overridden() -> (
    None
):
    client = CodexAppServerClient(AsyncMock(), "ws://127.0.0.1:4312", "token")
    requests: list[tuple[str, dict[str, object]]] = []

    async def fake_ws_request(
        method: str, params: dict[str, object]
    ) -> dict[str, object]:
        requests.append((method, params))
        if method == "turn/start":
            return {"turn": {"id": "turn-1"}}
        raise AssertionError(f"Unexpected method: {method}")

    client._ws_request = AsyncMock(side_effect=fake_ws_request)  # type: ignore[method-assign]

    await client.start_turn(
        "chat:1",
        "thread-1",
        "codex-1",
        "hello",
        _build_profile(),
        SessionOverrides(
            profile="operator",
            model="gpt-5.4-mini",
            effort="high",
            summary="detailed",
            cwd="/agent/project-a",
        ),
    )

    params = requests[0][1]
    assert params["cwd"] == "/agent/project-a"
    assert params["permissions"] == {"type": "profile", "id": "operator"}
    assert params["model"] == "gpt-5.4-mini"
    assert params["modelProvider"] == "openai"
    assert params["effort"] == "high"
    assert params["summary"] == "detailed"
    assert "approvalPolicy" not in params
    assert "sandboxPolicy" not in params


@pytest.mark.asyncio
async def test_list_codex_threads_maps_app_server_thread_list() -> None:
    client = CodexAppServerClient(AsyncMock(), "ws://127.0.0.1:4312", "token")
    requests: list[tuple[str, dict[str, object]]] = []

    async def fake_ws_request(
        method: str, params: dict[str, object]
    ) -> dict[str, object]:
        requests.append((method, params))
        if method == "thread/list":
            return {
                "data": [
                    {
                        "id": "codex-1",
                        "cwd": "/agent/project-a",
                        "title": "Fix CI",
                        "preview": "Fix failing tests",
                        "status": {"type": "idle"},
                        "createdAt": 1710000000,
                        "updatedAt": 1710000300,
                        "modelProvider": "openai",
                    }
                ],
                "nextCursor": None,
            }
        raise AssertionError(f"Unexpected method: {method}")

    client._ensure_connected = AsyncMock()  # type: ignore[method-assign]
    client._ws_request = AsyncMock(side_effect=fake_ws_request)  # type: ignore[method-assign]

    threads = await client.list_codex_threads(search="ci")

    assert requests == [
        (
            "thread/list",
            {
                "archived": False,
                "limit": 50,
                "searchTerm": "ci",
                "sortKey": "updated_at",
                "sortDirection": "desc",
            },
        )
    ]
    assert threads == [
        CodexThread(
            thread_id="codex-1",
            cwd="/agent/project-a",
            title="Fix CI",
            preview="Fix failing tests",
            status="idle",
            created_at=1710000000,
            updated_at=1710000300,
            model_provider="openai",
            anchor_status="unlinked",
        )
    ]


@pytest.mark.asyncio
async def test_client_reconnects_when_existing_websocket_is_closed() -> None:
    session = AsyncMock()
    replacement_ws = SimpleNamespace(closed=False)
    session.ws_connect.return_value = replacement_ws
    client = CodexAppServerClient(session, "ws://127.0.0.1:4312", "token")
    client._ws = SimpleNamespace(closed=True)  # type: ignore[assignment]
    client._ws_request = AsyncMock(return_value={})  # type: ignore[method-assign]
    client._ws_notify = AsyncMock()  # type: ignore[method-assign]
    client._reader_loop = AsyncMock()  # type: ignore[method-assign]

    await client._ensure_connected()

    assert client._ws is replacement_ws
    session.ws_connect.assert_awaited_once()
    client._ws_request.assert_awaited_once_with(  # type: ignore[attr-defined]
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


@pytest.mark.asyncio
async def test_get_codex_thread_uses_thread_read() -> None:
    client = CodexAppServerClient(AsyncMock(), "ws://127.0.0.1:4312", "token")
    requests: list[tuple[str, dict[str, object]]] = []

    async def fake_ws_request(
        method: str, params: dict[str, object]
    ) -> dict[str, object]:
        requests.append((method, params))
        if method == "thread/read":
            return {
                "thread": {
                    "id": "codex-1",
                    "cwd": "/agent/project-a",
                    "title": None,
                    "preview": "Investigate deploy",
                    "status": {"type": "active"},
                    "createdAt": 1710000000,
                    "updatedAt": 1710000400,
                    "modelProvider": "openai",
                }
            }
        raise AssertionError(f"Unexpected method: {method}")

    client._ensure_connected = AsyncMock()  # type: ignore[method-assign]
    client._ws_request = AsyncMock(side_effect=fake_ws_request)  # type: ignore[method-assign]

    thread = await client.get_codex_thread("codex-1")

    assert requests == [("thread/read", {"threadId": "codex-1", "includeTurns": False})]
    assert thread.thread_id == "codex-1"
    assert thread.title is None
    assert thread.preview == "Investigate deploy"
    assert thread.status == "active"


@pytest.mark.asyncio
async def test_start_turn_does_not_block_other_logical_threads_in_same_chat() -> None:
    client = CodexAppServerClient(AsyncMock(), "ws://127.0.0.1:4312", "token")
    requests: list[tuple[str, dict[str, object]]] = []
    turn_ids = iter(("turn-1", "turn-2"))

    async def fake_ws_request(
        method: str, params: dict[str, object]
    ) -> dict[str, object]:
        requests.append((method, params))
        if method != "turn/start":
            raise AssertionError(f"Unexpected method: {method}")
        return {"turn": {"id": next(turn_ids)}}

    client._ws_request = AsyncMock(side_effect=fake_ws_request)  # type: ignore[method-assign]
    profile = _build_profile()

    first = await client.start_turn(
        "chat:1",
        "thread-1",
        "codex-1",
        "first prompt",
        profile,
        SessionOverrides(followup_mode="queue"),
    )
    second = await asyncio.wait_for(
        client.start_turn(
            "chat:1",
            "thread-2",
            "codex-2",
            "second prompt",
            profile,
            SessionOverrides(followup_mode="queue"),
        ),
        timeout=0.1,
    )

    assert first.turn_id == "turn-1"
    assert second.turn_id == "turn-2"
    assert [method for method, _ in requests] == ["turn/start", "turn/start"]


@pytest.mark.asyncio
async def test_start_turn_serializes_logical_threads_sharing_codex_thread() -> None:
    client = CodexAppServerClient(AsyncMock(), "ws://127.0.0.1:4312", "token")
    requests: list[tuple[str, dict[str, object]]] = []
    turn_ids = iter(("turn-1", "turn-2"))

    async def fake_ws_request(
        method: str, params: dict[str, object]
    ) -> dict[str, object]:
        requests.append((method, params))
        if method != "turn/start":
            raise AssertionError(f"Unexpected method: {method}")
        return {"turn": {"id": next(turn_ids)}}

    client._ws_request = AsyncMock(side_effect=fake_ws_request)  # type: ignore[method-assign]
    profile = _build_profile()

    first = await client.start_turn(
        "chat:1",
        "thread-1",
        "codex-1",
        "first prompt",
        profile,
        SessionOverrides(followup_mode="queue"),
    )
    second_task = asyncio.create_task(
        client.start_turn(
            "chat:1",
            "thread-2",
            "codex-1",
            "second prompt",
            profile,
            SessionOverrides(followup_mode="queue"),
        )
    )
    await asyncio.sleep(0)

    assert second_task.done() is False
    client._turns[first.turn_id].completed.set_result(  # type: ignore[attr-defined]
        TurnResult(
            turn_id="turn-1",
            chat_key="chat:1",
            logical_thread_id="thread-1",
            codex_thread_id="codex-1",
            status="completed",
            final_text="done",
        )
    )
    second = await asyncio.wait_for(second_task, timeout=0.1)

    assert second.turn_id == "turn-2"
    assert [params["threadId"] for _, params in requests] == ["codex-1", "codex-1"]


@pytest.mark.parametrize(
    "method",
    (
        "item/tool/requestUserInput",
        "request_user_input",
    ),
)
@pytest.mark.asyncio
async def test_server_request_maps_user_input_questions(method: str) -> None:
    client = CodexAppServerClient(AsyncMock(), "ws://127.0.0.1:4312", "token")
    client._ws_request = AsyncMock(  # type: ignore[method-assign]
        return_value={"turn": {"id": "turn-1"}}
    )
    profile = _build_profile()
    accepted = await client.start_turn(
        "chat:1",
        "thread-1",
        "codex-1",
        "Plan the work.",
        profile,
        SessionOverrides(followup_mode="queue"),
    )

    client._handle_ws_message(  # type: ignore[attr-defined]
        {
            "id": 42,
            "method": method,
            "params": {
                "threadId": "codex-1",
                "turnId": accepted.turn_id,
                "questions": [
                    {
                        "id": "scope",
                        "header": "Scope",
                        "question": "Which scope?",
                        "options": [
                            {
                                "label": "Native first",
                                "description": "Use app-server requests.",
                            },
                            {
                                "label": "MCP shim",
                                "description": "Expose a fallback tool.",
                            },
                        ],
                    }
                ],
            },
        }
    )

    event = await client.wait_for_turn_event(accepted.turn_id, timeout=0.1)

    assert isinstance(event, PendingUserInput)
    assert event.request_id == 42
    assert event.codex_backend_id == "primary"
    assert event.questions[0].question_id == "scope"
    assert event.questions[0].options[0].label == "Native first"


@pytest.mark.asyncio
async def test_server_request_maps_approval_message() -> None:
    client = CodexAppServerClient(AsyncMock(), "ws://127.0.0.1:4312", "token")
    client._ws_request = AsyncMock(  # type: ignore[method-assign]
        return_value={"turn": {"id": "turn-1"}}
    )
    profile = _build_profile()
    accepted = await client.start_turn(
        "chat:1",
        "thread-1",
        "codex-1",
        "Run the tests.",
        profile,
        SessionOverrides(followup_mode="queue"),
    )

    client._handle_ws_message(  # type: ignore[attr-defined]
        {
            "id": 42,
            "method": "item/commandExecution/requestApproval",
            "params": {
                "threadId": "codex-1",
                "turnId": accepted.turn_id,
                "command": "uv run pytest",
                "reason": "Run tests",
                "message": "Guardian reviewed the approach before execution.",
            },
        }
    )

    event = await client.wait_for_turn_event(accepted.turn_id, timeout=0.1)

    assert isinstance(event, PendingApproval)
    assert event.message == "Guardian reviewed the approach before execution."


@pytest.mark.asyncio
async def test_item_completed_maps_native_image_generation() -> None:
    client = CodexAppServerClient(AsyncMock(), "ws://127.0.0.1:4312", "token")
    client._ws_request = AsyncMock(  # type: ignore[method-assign]
        return_value={"turn": {"id": "turn-1"}}
    )
    profile = _build_profile()
    accepted = await client.start_turn(
        "chat:1",
        "thread-1",
        "codex-1",
        "Generate an image.",
        profile,
        SessionOverrides(followup_mode="queue"),
    )

    client._handle_ws_message(  # type: ignore[attr-defined]
        {
            "method": "item/completed",
            "params": {
                "threadId": "codex-1",
                "turnId": accepted.turn_id,
                "item": {
                    "type": "imageGeneration",
                    "id": "img-1",
                    "status": "completed",
                    "result": "generated",
                    "savedPath": "/agent/generated.png",
                    "revisedPrompt": "A small watercolor house.",
                },
            },
        }
    )
    client._handle_ws_message(  # type: ignore[attr-defined]
        {
            "method": "turn/completed",
            "params": {
                "turn": {"id": accepted.turn_id, "status": "completed"},
            },
        }
    )

    event = await client.wait_for_turn_event(accepted.turn_id, timeout=0.1)

    assert isinstance(event, TurnResult)
    assert event.images == (
        TurnResultImage(
            source="/agent/generated.png",
            caption="A small watercolor house.",
        ),
    )


@pytest.mark.asyncio
async def test_item_completed_plan_maps_to_final_text() -> None:
    client = CodexAppServerClient(AsyncMock(), "ws://127.0.0.1:4312", "token")
    client._ws_request = AsyncMock(  # type: ignore[method-assign]
        return_value={"turn": {"id": "turn-1"}}
    )
    profile = _build_profile()
    accepted = await client.start_turn(
        "chat:1",
        "thread-1",
        "codex-1",
        "Plan the work.",
        profile,
        SessionOverrides(followup_mode="queue"),
    )
    plan_text = (
        "# Ring Snapshot Dashboard Tabs\n\n"
        "## Key Changes\n"
        "- Change snapshot storage to camera-first.\n"
        "- Keep stamping each snapshot."
    )

    client._handle_ws_message(  # type: ignore[attr-defined]
        {
            "method": "item/completed",
            "params": {
                "threadId": "codex-1",
                "turnId": accepted.turn_id,
                "item": {
                    "type": "Plan",
                    "id": "turn-1-plan",
                    "text": plan_text,
                },
            },
        }
    )
    event = await client.wait_for_turn_event(accepted.turn_id, timeout=0.1)
    assert isinstance(event, TurnUpdate)
    assert event.text == plan_text

    client._handle_ws_message(  # type: ignore[attr-defined]
        {
            "method": "turn/completed",
            "params": {
                "turn": {"id": accepted.turn_id, "status": "completed"},
            },
        }
    )

    result = await client.wait_for_turn_event(accepted.turn_id, timeout=0.1)

    assert isinstance(result, TurnResult)
    assert result.final_text == plan_text


@pytest.mark.asyncio
async def test_item_completed_before_turn_start_response_maps_to_final_text() -> None:
    client = CodexAppServerClient(AsyncMock(), "ws://127.0.0.1:4312", "token")
    profile = _build_profile()
    plan_text = (
        "- Confirm the smoke target is intentionally no-op.\n"
        "- Report a concise verdict with evidence."
    )

    async def ws_request(method: str, params: dict[str, object]) -> dict[str, object]:
        assert method == "turn/start"
        assert params["threadId"] == "codex-1"
        client._handle_ws_message(  # type: ignore[attr-defined]
            {
                "method": "item/completed",
                "params": {
                    "threadId": "codex-1",
                    "turnId": "turn-1",
                    "item": {
                        "type": "Plan",
                        "id": "turn-1-plan",
                        "text": plan_text,
                    },
                },
            }
        )
        return {"turn": {"id": "turn-1"}}

    client._ws_request = AsyncMock(side_effect=ws_request)  # type: ignore[method-assign]

    accepted = await client.start_turn(
        "chat:1",
        "thread-1",
        "codex-1",
        "Plan the work.",
        profile,
        SessionOverrides(followup_mode="queue"),
    )

    event = await client.wait_for_turn_event(accepted.turn_id, timeout=0.1)
    assert isinstance(event, TurnUpdate)
    assert event.text == plan_text

    client._handle_ws_message(  # type: ignore[attr-defined]
        {
            "method": "turn/completed",
            "params": {
                "turn": {"id": accepted.turn_id, "status": "completed"},
            },
        }
    )

    result = await client.wait_for_turn_event(accepted.turn_id, timeout=0.1)

    assert isinstance(result, TurnResult)
    assert result.final_text == plan_text


@pytest.mark.asyncio
async def test_completed_turn_with_plan_item_maps_to_final_text() -> None:
    client = CodexAppServerClient(AsyncMock(), "ws://127.0.0.1:4312", "token")
    client._ws_request = AsyncMock(  # type: ignore[method-assign]
        return_value={"turn": {"id": "turn-1"}}
    )
    profile = _build_profile()
    plan_text = "- Perform a bounded read-only check.\n" "- Return a concise verdict."

    accepted = await client.start_turn(
        "chat:1",
        "thread-1",
        "codex-1",
        "Plan the work.",
        profile,
        SessionOverrides(followup_mode="queue"),
    )

    client._handle_ws_message(  # type: ignore[attr-defined]
        {
            "method": "turn/completed",
            "params": {
                "turn": {
                    "id": accepted.turn_id,
                    "status": "completed",
                    "items": [
                        {
                            "type": "userMessage",
                            "id": "item-1",
                            "content": [],
                        },
                        {
                            "type": "plan",
                            "id": "turn-1-plan",
                            "text": plan_text,
                        },
                    ],
                },
            },
        }
    )

    event = await client.wait_for_turn_event(accepted.turn_id, timeout=0.1)
    assert isinstance(event, TurnUpdate)
    assert event.text == plan_text

    result = await client.wait_for_turn_event(accepted.turn_id, timeout=0.1)

    assert isinstance(result, TurnResult)
    assert result.final_text == plan_text


@pytest.mark.asyncio
async def test_plan_delta_streams_until_completed_plan_item_overwrites() -> None:
    client = CodexAppServerClient(AsyncMock(), "ws://127.0.0.1:4312", "token")
    client._ws_request = AsyncMock(  # type: ignore[method-assign]
        return_value={"turn": {"id": "turn-1"}}
    )
    profile = _build_profile()
    accepted = await client.start_turn(
        "chat:1",
        "thread-1",
        "codex-1",
        "Plan the work.",
        profile,
        SessionOverrides(followup_mode="queue"),
    )

    client._handle_ws_message(  # type: ignore[attr-defined]
        {
            "method": "item/plan/delta",
            "params": {
                "threadId": "codex-1",
                "turnId": accepted.turn_id,
                "itemId": "turn-1-plan",
                "delta": "partial",
            },
        }
    )
    event = await client.wait_for_turn_event(accepted.turn_id, timeout=0.1)
    assert isinstance(event, TurnUpdate)
    assert event.text == "partial"

    client._handle_ws_message(  # type: ignore[attr-defined]
        {
            "method": "item/completed",
            "params": {
                "threadId": "codex-1",
                "turnId": accepted.turn_id,
                "item": {
                    "type": "plan",
                    "id": "turn-1-plan",
                    "text": "complete plan",
                },
            },
        }
    )
    event = await client.wait_for_turn_event(accepted.turn_id, timeout=0.1)
    assert isinstance(event, TurnUpdate)
    assert event.text == "complete plan"

    client._handle_ws_message(  # type: ignore[attr-defined]
        {
            "method": "turn/completed",
            "params": {
                "turn": {"id": accepted.turn_id, "status": "completed"},
            },
        }
    )

    result = await client.wait_for_turn_event(accepted.turn_id, timeout=0.1)

    assert isinstance(result, TurnResult)
    assert result.final_text == "complete plan"


@pytest.mark.asyncio
async def test_unsupported_server_request_gets_jsonrpc_error_response() -> None:
    client = CodexAppServerClient(AsyncMock(), "ws://127.0.0.1:4312", "token")
    client._thread_context["codex-1"] = ("chat:1", "thread-1")  # type: ignore[attr-defined]
    ws = SimpleNamespace(closed=False, send_json=AsyncMock())
    client._ws = ws  # type: ignore[assignment]

    client._handle_ws_message(  # type: ignore[attr-defined]
        {
            "id": 42,
            "method": "item/tool/unsupported",
            "params": {"threadId": "codex-1", "turnId": "turn-1"},
        }
    )
    await asyncio.sleep(0)

    ws.send_json.assert_awaited_once_with(
        {
            "id": 42,
            "error": {
                "code": -32601,
                "message": "Unsupported server request method: item/tool/unsupported",
            },
        }
    )


@pytest.mark.asyncio
async def test_contextless_server_request_gets_jsonrpc_error_response() -> None:
    client = CodexAppServerClient(AsyncMock(), "ws://127.0.0.1:4312", "token")
    ws = SimpleNamespace(closed=False, send_json=AsyncMock())
    client._ws = ws  # type: ignore[assignment]

    client._handle_ws_message(  # type: ignore[attr-defined]
        {
            "id": 43,
            "method": "item/tool/requestUserInput",
            "params": {"threadId": "unknown-codex-thread", "turnId": "turn-1"},
        }
    )
    await asyncio.sleep(0)

    ws.send_json.assert_awaited_once_with(
        {
            "id": 43,
            "error": {
                "code": -32602,
                "message": "No Telegram context is registered for this Codex thread.",
            },
        }
    )


def test_request_user_input_method_accepts_empty_question_payload() -> None:
    assert (
        _extract_user_input_questions(
            "item/tool/requestUserInput",
            {"questions": []},
        )
        == ()
    )


@pytest.mark.asyncio
async def test_completed_get_goal_item_updates_runtime_goal_state() -> None:
    client = CodexAppServerClient(AsyncMock(), "ws://127.0.0.1:4312", "token")
    client._ws_request = AsyncMock(  # type: ignore[method-assign]
        return_value={"turn": {"id": "turn-1"}}
    )
    profile = _build_profile()
    accepted = await client.start_turn(
        "chat:1",
        "thread-1",
        "codex-1",
        "What is the status?",
        profile,
        SessionOverrides(followup_mode="queue"),
    )

    client._handle_ws_message(  # type: ignore[attr-defined]
        {
            "method": "item/completed",
            "params": {
                "turnId": accepted.turn_id,
                "item": {
                    "type": "toolCall",
                    "name": "get_goal",
                    "output": {
                        "objective": "Ship overview status improvements",
                        "status": "active",
                    },
                },
            },
        }
    )

    await client.wait_for_turn_event(accepted.turn_id, timeout=0.1)
    runtime = client.get_runtime_state("codex-1")

    assert runtime.goal is not None
    assert runtime.goal.objective == "Ship overview status improvements"
    assert runtime.goal.status == "active"


@pytest.mark.asyncio
async def test_goal_rpc_methods_use_app_server_thread_goal_api() -> None:
    client = CodexAppServerClient(AsyncMock(), "ws://127.0.0.1:4312", "token")
    requests: list[tuple[str, dict[str, object]]] = []

    async def fake_ws_request(
        method: str, params: dict[str, object]
    ) -> dict[str, object]:
        requests.append((method, params))
        if method == "thread/goal/get":
            return {
                "goal": {
                    "objective": "Ship overview",
                    "status": "active",
                    "tokenBudget": 1000,
                    "tokensUsed": 125,
                    "elapsedSeconds": 12.5,
                    "createdAt": "2026-05-06T00:00:00Z",
                    "updatedAt": "2026-05-06T00:01:00Z",
                }
            }
        if method == "thread/goal/set":
            return {
                "goal": {
                    "objective": params["objective"],
                    "status": params["status"],
                    "tokenBudget": params["tokenBudget"],
                }
            }
        if method == "thread/goal/clear":
            return {}
        raise AssertionError(f"Unexpected method: {method}")

    client._ws_request = AsyncMock(side_effect=fake_ws_request)  # type: ignore[method-assign]

    current = await client.get_thread_goal("codex-1")
    updated = await client.set_thread_goal(
        "codex-1",
        objective="Ship goal command",
        token_budget=500,
        status="paused",
        update_token_budget=True,
    )
    await client.clear_thread_goal("codex-1")

    assert current == CodexGoal(
        objective="Ship overview",
        status="active",
        token_budget=1000,
        tokens_used=125,
        elapsed_seconds=12.5,
        created_at="2026-05-06T00:00:00Z",
        updated_at="2026-05-06T00:01:00Z",
    )
    assert updated == CodexGoal(
        objective="Ship goal command",
        status="paused",
        token_budget=500,
    )
    assert requests == [
        ("thread/goal/get", {"threadId": "codex-1"}),
        (
            "thread/goal/set",
            {
                "threadId": "codex-1",
                "objective": "Ship goal command",
                "status": "paused",
                "tokenBudget": 500,
            },
        ),
        ("thread/goal/clear", {"threadId": "codex-1"}),
    ]
    assert client.get_runtime_state("codex-1").goal is None


@pytest.mark.asyncio
async def test_set_thread_goal_refreshes_when_app_server_omits_goal() -> None:
    client = CodexAppServerClient(AsyncMock(), "ws://127.0.0.1:4312", "token")
    requests: list[tuple[str, dict[str, object]]] = []

    async def fake_ws_request(
        method: str, params: dict[str, object]
    ) -> dict[str, object]:
        requests.append((method, params))
        if method == "thread/goal/set":
            return {}
        if method == "thread/goal/get":
            return {"goal": {"objective": "Ship goal command", "status": "paused"}}
        raise AssertionError(f"Unexpected method: {method}")

    client._ws_request = AsyncMock(side_effect=fake_ws_request)  # type: ignore[method-assign]

    goal = await client.set_thread_goal(
        "codex-1",
        objective="Ship goal command",
        status="active",
    )

    assert goal == CodexGoal("Ship goal command", status="paused")
    assert client.get_runtime_state("codex-1").goal == goal
    assert requests == [
        (
            "thread/goal/set",
            {
                "threadId": "codex-1",
                "objective": "Ship goal command",
                "status": "active",
            },
        ),
        ("thread/goal/get", {"threadId": "codex-1"}),
    ]


@pytest.mark.asyncio
async def test_start_turn_omits_collaboration_mode_without_explicit_override() -> None:
    client = CodexAppServerClient(AsyncMock(), "ws://127.0.0.1:4312", "token")
    requests: list[tuple[str, dict[str, object]]] = []

    async def fake_ws_request(
        method: str, params: dict[str, object]
    ) -> dict[str, object]:
        requests.append((method, params))
        return {"turn": {"id": "turn-1"}}

    client._ws_request = AsyncMock(side_effect=fake_ws_request)  # type: ignore[method-assign]

    await client.start_turn(
        "chat:1",
        "thread-1",
        "codex-1",
        "Hello",
        _build_profile(),
        SessionOverrides(),
    )

    assert "collaborationMode" not in requests[0][1]


@pytest.mark.asyncio
async def test_start_turn_sends_collaboration_mode_settings() -> None:
    client = CodexAppServerClient(AsyncMock(), "ws://127.0.0.1:4312", "token")
    requests: list[tuple[str, dict[str, object]]] = []

    async def fake_ws_request(
        method: str, params: dict[str, object]
    ) -> dict[str, object]:
        requests.append((method, params))
        return {"turn": {"id": "turn-1"}}

    client._ws_request = AsyncMock(side_effect=fake_ws_request)  # type: ignore[method-assign]

    await client.start_turn(
        "chat:1",
        "thread-1",
        "codex-1",
        "Plan this",
        _build_profile(),
        SessionOverrides(
            model="gpt-5.4-mini",
            effort="high",
            collaboration_mode="plan",
        ),
    )

    assert requests[0][1]["collaborationMode"] == {
        "mode": "plan",
        "settings": {
            "model": "gpt-5.4-mini",
            "reasoning_effort": "high",
            "developer_instructions": None,
        },
    }


def test_goal_notifications_update_runtime_state() -> None:
    client = CodexAppServerClient(AsyncMock(), "ws://127.0.0.1:4312", "token")

    client._handle_notification(  # type: ignore[attr-defined]
        "thread/goal/updated",
        {
            "threadId": "codex-1",
            "goal": {"objective": "Ship goal command", "status": "active"},
        },
    )
    assert client.get_runtime_state("codex-1").goal == CodexGoal(
        objective="Ship goal command",
        status="active",
    )

    client._handle_notification(  # type: ignore[attr-defined]
        "thread/goal/cleared",
        {"threadId": "codex-1"},
    )
    assert client.get_runtime_state("codex-1").goal is None


@pytest.mark.asyncio
async def test_tool_items_update_runtime_plan_state() -> None:
    client = CodexAppServerClient(AsyncMock(), "ws://127.0.0.1:4312", "token")
    client._ws_request = AsyncMock(  # type: ignore[method-assign]
        return_value={"turn": {"id": "turn-1"}}
    )
    profile = _build_profile()
    accepted = await client.start_turn(
        "chat:1",
        "thread-1",
        "codex-1",
        "Plan the work.",
        profile,
        SessionOverrides(followup_mode="queue"),
    )

    client._handle_ws_message(  # type: ignore[attr-defined]
        {
            "method": "item/started",
            "params": {
                "turnId": accepted.turn_id,
                "item": {
                    "type": "toolCall",
                    "name": "update_plan",
                    "input": {
                        "plan": [
                            {"step": "Inspect status card", "status": "completed"},
                            {"step": "Render plan state", "status": "in_progress"},
                        ],
                    },
                },
            },
        }
    )

    event = await client.wait_for_turn_event(accepted.turn_id, timeout=0.1)
    runtime = client.get_runtime_state("codex-1")

    assert isinstance(event, TurnUpdate)
    assert event.visible is False
    assert [item.step for item in runtime.plan_items] == [
        "Inspect status card",
        "Render plan state",
    ]
    assert runtime.plan_items[1].status == "in_progress"


@pytest.mark.asyncio
async def test_runtime_plan_state_survives_turn_completion() -> None:
    client = CodexAppServerClient(AsyncMock(), "ws://127.0.0.1:4312", "token")
    client._ws_request = AsyncMock(  # type: ignore[method-assign]
        return_value={"turn": {"id": "turn-1"}}
    )
    profile = _build_profile()
    accepted = await client.start_turn(
        "chat:1",
        "thread-1",
        "codex-1",
        "Plan the work.",
        profile,
        SessionOverrides(followup_mode="queue"),
    )

    client._handle_ws_message(  # type: ignore[attr-defined]
        {
            "method": "item/started",
            "params": {
                "turnId": accepted.turn_id,
                "item": {
                    "type": "toolCall",
                    "name": "update_plan",
                    "input": {
                        "plan": [
                            {"step": "Inspect status card", "status": "completed"},
                            {"step": "Render plan state", "status": "in_progress"},
                        ],
                    },
                },
            },
        }
    )
    await client.wait_for_turn_event(accepted.turn_id, timeout=0.1)

    client._handle_ws_message(  # type: ignore[attr-defined]
        {
            "method": "turn/completed",
            "params": {
                "turn": {
                    "id": accepted.turn_id,
                    "status": "completed",
                },
            },
        }
    )
    result = await client.wait_for_turn_event(accepted.turn_id, timeout=0.1)
    runtime = client.get_runtime_state("codex-1")

    assert isinstance(result, TurnResult)
    assert [item.step for item in runtime.plan_items] == [
        "Inspect status card",
        "Render plan state",
    ]
