from pathlib import Path
import importlib.util
from importlib.machinery import SourceFileLoader
from unittest.mock import AsyncMock
from urllib import request

import pytest
from aiogram import Dispatcher

from codex_telegram.adapters.persistence.sqlite import (
    SQLiteStateRepository,
    SQLiteTelegramProgressStore,
)
from codex_telegram.adapters.telegram.bot import (
    TelegramBotRunner,
    _attachment_is_photo,
    chat_context_from_key,
)
from codex_telegram.domain import BridgeControlJob
from codex_telegram.entrypoints import send_attachment


def test_enqueue_attachment_helper_calls_admin_api(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    class _Response:
        def __enter__(self) -> "_Response":
            return self

        def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
            return None

        def read(self) -> bytes:
            return b'{"job_id": 9, "status": "pending"}'

    def fake_urlopen(req: request.Request, timeout: int = 30) -> _Response:
        captured["url"] = req.full_url
        captured["method"] = req.get_method()
        headers = {key.lower(): value for key, value in req.headers.items()}
        captured["authorization"] = headers["authorization"]
        captured["content_type"] = headers["content-type"]
        captured["body"] = req.data
        captured["timeout"] = timeout
        return _Response()

    monkeypatch.setattr(send_attachment.request, "urlopen", fake_urlopen)

    response = send_attachment.enqueue_attachment_job(
        "http://127.0.0.1:19080",
        "admin-secret",
        thread_id="thread-1",
        path="/attachments/example.txt",
        caption="hello",
    )

    assert response["job_id"] == 9
    assert captured["url"] == "http://127.0.0.1:19080/attachments"
    assert captured["method"] == "POST"
    assert captured["authorization"] == "Bearer admin-secret"
    assert captured["content_type"] == "application/json"
    body = captured["body"]
    assert isinstance(body, bytes)
    assert b'"thread_id": "thread-1"' in body
    assert b'"path": "/attachments/example.txt"' in body
    assert b'"caption": "hello"' in body


def test_bridge_helper_command_calls_bridge_command_api(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    helper_path = (
        Path(__file__).parents[1]
        / "deploy"
        / "codex-app-server"
        / "scripts"
        / "codex-telegram-bridge"
    )
    loader = SourceFileLoader("codex_telegram_bridge", str(helper_path))
    spec = importlib.util.spec_from_loader("codex_telegram_bridge", loader)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    captured: dict[str, object] = {}

    class _Response:
        def __enter__(self) -> "_Response":
            return self

        def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
            return None

        def read(self) -> bytes:
            return b'{"accepted": true}'

    def fake_urlopen(req: request.Request, timeout: int = 30) -> _Response:
        captured["url"] = req.full_url
        captured["method"] = req.get_method()
        captured["body"] = req.data
        captured["timeout"] = timeout
        return _Response()

    monkeypatch.setenv("CODEX_TELEGRAM_WEBHOOK_ADMIN_TOKEN", "admin-secret")
    monkeypatch.setenv("CODEX_TELEGRAM_WEBHOOK_ADMIN_URL", "http://127.0.0.1:19080")
    monkeypatch.setattr(module.request, "urlopen", fake_urlopen)

    exit_code = module.main(["command", "--thread-id", "thread-1", "/current"])

    assert exit_code == 0
    assert captured["url"] == "http://127.0.0.1:19080/bridge-command"
    assert captured["method"] == "POST"
    body = captured["body"]
    assert isinstance(body, bytes)
    assert b'"thread_id": "thread-1"' in body
    assert b'"text": "/current"' in body


@pytest.mark.asyncio
async def test_attachment_runner_sends_photo_and_document(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    photo_path = tmp_path / "image.png"
    photo_path.write_bytes(b"image")
    doc_path = tmp_path / "notes.txt"
    doc_path.write_text("hello", encoding="utf-8")

    repository = SQLiteStateRepository(tmp_path / "state.db")
    progress_store = SQLiteTelegramProgressStore(tmp_path / "state.db")
    await repository.initialize()
    await progress_store.initialize()
    await repository.ensure_chat("chat:1")
    await repository.create_thread("chat:1", "thread-1", "New thread")
    await repository.enqueue_attachment_job(
        "thread-1", str(photo_path), caption="photo"
    )
    await repository.enqueue_attachment_job("thread-1", str(doc_path), caption="doc")

    bot = AsyncMock()
    runner = TelegramBotRunner(
        bot,
        Dispatcher(),
        AsyncMock(),
        repository,
        progress_store,
        None,
        False,
    )

    await runner._drain_attachment_jobs_once()

    bot.send_photo.assert_awaited_once()
    bot.send_document.assert_awaited_once()
    assert await repository.list_pending_attachment_jobs(limit=10) == []


@pytest.mark.asyncio
async def test_bridge_control_runner_sends_notify_and_refreshes_status_card(
    tmp_path: Path,
) -> None:
    repository = SQLiteStateRepository(tmp_path / "state.db")
    progress_store = SQLiteTelegramProgressStore(tmp_path / "state.db")
    await repository.initialize()
    await progress_store.initialize()
    await repository.ensure_chat("chat:1")
    await repository.create_thread("chat:1", "thread-1", "New thread")
    await repository.enqueue_bridge_control_job(
        "thread-1",
        "notify",
        {"text": "runtime note", "level": "warning"},
    )
    await repository.enqueue_bridge_control_job(
        "thread-1",
        "refresh_status_card",
        {},
    )

    bot = AsyncMock()
    service = AsyncMock()
    service.ensure_focused_bridge.return_value = type(
        "Focused",
        (),
        {"bridge_id": "thread-1"},
    )()
    service.current_thread_state.return_value = None
    runner = TelegramBotRunner(
        bot,
        Dispatcher(),
        service,
        repository,
        progress_store,
        None,
        False,
    )
    runner._sync_thread_status_card = AsyncMock()  # type: ignore[method-assign]

    await runner._drain_bridge_control_jobs_once()

    bot.send_message.assert_awaited_once()
    assert "runtime note" in bot.send_message.await_args.kwargs["text"]
    runner._sync_thread_status_card.assert_awaited_once()
    assert await repository.list_pending_bridge_control_jobs(limit=10) == []


@pytest.mark.asyncio
async def test_repository_round_trips_bridge_control_jobs(tmp_path: Path) -> None:
    repository = SQLiteStateRepository(tmp_path / "state.db")
    await repository.initialize()
    await repository.ensure_chat("chat:1")
    await repository.create_thread("chat:1", "thread-1", "New thread")

    created = await repository.enqueue_bridge_control_job(
        "thread-1",
        "notify",
        {"text": "hello"},
    )

    assert isinstance(created, BridgeControlJob)
    assert created.chat_key == "chat:1"
    assert created.payload == {"text": "hello"}
    pending = await repository.list_pending_bridge_control_jobs(limit=10)
    assert pending == [created]
    assert created.job_id is not None
    await repository.mark_bridge_control_job_delivered(created.job_id)
    assert await repository.list_pending_bridge_control_jobs(limit=10) == []


def test_attachment_helpers_detect_context_and_media_type() -> None:
    context = chat_context_from_key("chat:7:11")

    assert context.chat_id == 7
    assert context.topic_id == 11
    assert _attachment_is_photo(Path("/tmp/example.png")) is True
    assert _attachment_is_photo(Path("/tmp/example.txt")) is False
