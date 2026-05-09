from unittest.mock import AsyncMock

import pytest

from codex_telegram.adapters.codex_app_server.client import CodexAppServerError
from codex_telegram.adapters.codex_app_server.multi import MultiCodexBackend
from codex_telegram.config import AppServerConfig
from codex_telegram.domain import CodexThread


def _thread(thread_id: str, *, backend_id: str, backend_name: str) -> CodexThread:
    return CodexThread(
        thread_id=thread_id,
        cwd="/agent/project",
        title=thread_id,
        preview=None,
        status="idle",
        created_at=1710000000,
        updated_at=1710000300,
        model_provider="openai",
        codex_backend_id=backend_id,
        codex_backend_name=backend_name,
    )


def _backend() -> tuple[MultiCodexBackend, AsyncMock, AsyncMock]:
    home = AsyncMock()
    laptop = AsyncMock()
    backend = MultiCodexBackend(
        clients={"home": home, "laptop": laptop},
        configs={
            "home": AppServerConfig(
                backend_id="home",
                name="home",
                url="ws://home.example",
                primary=True,
            ),
            "laptop": AppServerConfig(
                backend_id="laptop",
                name="laptop",
                url="ws://laptop.example",
            ),
        },
        primary_backend_id="home",
    )
    return backend, home, laptop


@pytest.mark.asyncio
async def test_multi_backend_lists_primary_by_default() -> None:
    backend, home, laptop = _backend()
    home.list_codex_threads.return_value = [
        _thread("home-1", backend_id="home", backend_name="home")
    ]

    threads = await backend.list_codex_threads()

    assert [thread.thread_id for thread in threads] == ["home-1"]
    home.list_codex_threads.assert_awaited_once_with(search=None, limit=50)
    laptop.list_codex_threads.assert_not_called()


@pytest.mark.asyncio
async def test_multi_backend_routes_named_backend_listing() -> None:
    backend, home, laptop = _backend()
    laptop.list_codex_threads.return_value = [
        _thread("laptop-1", backend_id="laptop", backend_name="laptop")
    ]

    threads = await backend.list_codex_threads(backend_name="laptop", search="ci")

    assert [thread.thread_id for thread in threads] == ["laptop-1"]
    home.list_codex_threads.assert_not_called()
    laptop.list_codex_threads.assert_awaited_once_with(search="ci", limit=50)


@pytest.mark.asyncio
async def test_multi_backend_all_records_partial_failures() -> None:
    backend, home, laptop = _backend()
    home.list_codex_threads.return_value = [
        _thread("home-1", backend_id="home", backend_name="home")
    ]
    laptop.list_codex_threads.side_effect = RuntimeError("connection refused")

    threads = await backend.list_codex_threads(include_all=True)

    assert [thread.thread_id for thread in threads] == ["home-1"]
    assert len(backend.listing_failures) == 1
    assert backend.listing_failures[0].backend_name == "laptop"
    assert backend.listing_failures[0].error == "connection refused"


@pytest.mark.asyncio
async def test_multi_backend_selected_failure_is_visible_error() -> None:
    backend, _home, laptop = _backend()
    laptop.list_codex_threads.side_effect = RuntimeError("connection refused")

    with pytest.raises(CodexAppServerError, match="Backend laptop unavailable"):
        await backend.list_codex_threads(backend_name="laptop")
