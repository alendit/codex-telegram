from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiogram import Dispatcher

from codex_telegram.adapters.persistence.sqlite import (
    SQLiteStateRepository,
    SQLiteTelegramProgressStore,
)
from codex_telegram.adapters.speech_to_text import SpeechToTextError
from codex_telegram.adapters.telegram.bot import ChatContext, TelegramBotRunner
from codex_telegram.domain import UserTurnImage, UserTurnInput


@pytest.mark.asyncio
async def test_voice_messages_are_transcribed_before_submission(
    tmp_path: Path,
) -> None:
    repository = SQLiteStateRepository(tmp_path / "state.db")
    progress_store = SQLiteTelegramProgressStore(tmp_path / "state.db")
    await repository.initialize()
    await progress_store.initialize()

    bot = AsyncMock()
    bot.get_file.return_value = SimpleNamespace(file_path="voice.ogg")

    async def _download(remote_file, destination: Path) -> None:
        destination.write_bytes(b"audio")

    bot.download.side_effect = _download
    speech_client = AsyncMock()
    speech_client.transcribe.return_value = (
        "The to-do MD has a list of features. Implement them one by one and "
        "commit individually."
    )

    runner = TelegramBotRunner(
        bot,
        Dispatcher(),
        AsyncMock(),
        repository,
        progress_store,
        None,
        False,
        speech_client=speech_client,
    )
    message = SimpleNamespace(
        text=None,
        voice=SimpleNamespace(file_id="voice-file-id"),
        audio=None,
    )
    context = ChatContext(chat_key="chat:1", chat_id=1, topic_id=None)

    turn_input = await runner._resolve_message_input(message, context)  # type: ignore[arg-type]

    assert turn_input == UserTurnInput(
        text=(
            "The to-do MD has a list of features. Implement them one by one and "
            "commit individually."
        )
    )
    speech_client.transcribe.assert_awaited_once()
    bot.send_message.assert_awaited()
    assert (
        bot.send_message.await_args.kwargs["text"]
        == "🟢 Transcribed: The to-do MD has a list of features. "
        "Implement them one by one and commit individually."
    )


@pytest.mark.asyncio
async def test_voice_messages_fail_cleanly_when_disabled(tmp_path: Path) -> None:
    repository = SQLiteStateRepository(tmp_path / "state.db")
    progress_store = SQLiteTelegramProgressStore(tmp_path / "state.db")
    await repository.initialize()
    await progress_store.initialize()

    bot = AsyncMock()
    runner = TelegramBotRunner(
        bot,
        Dispatcher(),
        AsyncMock(),
        repository,
        progress_store,
        None,
        False,
        speech_client=None,
    )
    message = SimpleNamespace(
        text=None,
        voice=SimpleNamespace(file_id="voice-file-id"),
        audio=None,
    )
    context = ChatContext(chat_key="chat:1", chat_id=1, topic_id=None)

    turn_input = await runner._resolve_message_input(message, context)  # type: ignore[arg-type]

    assert turn_input is None
    bot.send_message.assert_awaited_once()


@pytest.mark.asyncio
async def test_voice_transcription_failure_message_is_shortened(tmp_path: Path) -> None:
    repository = SQLiteStateRepository(tmp_path / "state.db")
    progress_store = SQLiteTelegramProgressStore(tmp_path / "state.db")
    await repository.initialize()
    await progress_store.initialize()

    bot = AsyncMock()
    bot.get_file.return_value = SimpleNamespace(file_path="voice.ogg")

    async def _download(remote_file, destination: Path) -> None:
        destination.write_bytes(b"audio")

    bot.download.side_effect = _download
    speech_client = AsyncMock()
    speech_client.transcribe.side_effect = SpeechToTextError("x" * 5000)

    runner = TelegramBotRunner(
        bot,
        Dispatcher(),
        AsyncMock(),
        repository,
        progress_store,
        None,
        False,
        speech_client=speech_client,
    )
    message = SimpleNamespace(
        text=None,
        voice=SimpleNamespace(file_id="voice-file-id"),
        audio=None,
    )
    context = ChatContext(chat_key="chat:1", chat_id=1, topic_id=None)

    turn_input = await runner._resolve_message_input(message, context)  # type: ignore[arg-type]

    assert turn_input is None
    bot.send_message.assert_awaited_once()
    sent_text = bot.send_message.await_args.kwargs["text"]
    assert sent_text.startswith("❌ Voice transcription failed: ")
    assert len(sent_text) < 400


@pytest.mark.asyncio
async def test_photo_messages_include_image_and_caption(tmp_path: Path) -> None:
    repository = SQLiteStateRepository(tmp_path / "state.db")
    progress_store = SQLiteTelegramProgressStore(tmp_path / "state.db")
    await repository.initialize()
    await progress_store.initialize()

    bot = AsyncMock()
    bot.get_file.return_value = SimpleNamespace(file_path="photo.png")

    async def _download(remote_file, destination: Path) -> None:
        destination.write_bytes(b"png-bytes")

    bot.download.side_effect = _download

    runner = TelegramBotRunner(
        bot,
        Dispatcher(),
        AsyncMock(),
        repository,
        progress_store,
        None,
        False,
        speech_client=None,
    )
    message = SimpleNamespace(
        text=None,
        caption="What is in this image?",
        photo=[SimpleNamespace(file_id="small"), SimpleNamespace(file_id="large")],
        document=None,
        voice=None,
        audio=None,
    )
    context = ChatContext(chat_key="chat:1", chat_id=1, topic_id=None)

    turn_input = await runner._resolve_message_input(message, context)  # type: ignore[arg-type]

    assert turn_input == UserTurnInput(
        text="What is in this image?",
        images=(UserTurnImage(url="data:image/png;base64,cG5nLWJ5dGVz"),),
    )
    bot.send_message.assert_not_awaited()
