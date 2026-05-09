"""Telegram image and voice input resolution."""

from __future__ import annotations

import base64
from collections.abc import Awaitable, Callable
import mimetypes
from pathlib import Path
import tempfile
from typing import Any

from aiogram import Bot
from aiogram.types import Message

from codex_telegram.adapters.speech_to_text import SpeechToTextClient, SpeechToTextError
from codex_telegram.adapters.telegram.attachments import ATTACHMENT_MAX_SIZE_BYTES
from codex_telegram.adapters.telegram.rendering import (
    COMMAND_FAILURE_PREFIX,
    COMMAND_SUCCESS_PREFIX,
    _public_error_summary,
)
from codex_telegram.adapters.telegram.routing import ChatContext
from codex_telegram.domain import UserTurnImage, UserTurnInput

SendText = Callable[[ChatContext, str], Awaitable[object]]


class TelegramMediaInputResolver:
    """Resolve Telegram image and voice messages into Codex turn input."""

    def __init__(
        self,
        bot: Bot,
        speech_client: SpeechToTextClient | None,
        send_text: SendText,
    ) -> None:
        self._bot = bot
        self._speech_client = speech_client
        self._send_text = send_text

    async def resolve(
        self,
        message: Message,
        context: ChatContext,
    ) -> UserTurnInput | None:
        image_input = await self.resolve_image_input(message, context)
        if image_input is not None:
            return image_input
        media = getattr(message, "voice", None) or getattr(message, "audio", None)
        if media is None:
            return None
        if self._speech_client is None:
            await self._send_text(
                context,
                COMMAND_FAILURE_PREFIX + "Voice input is not enabled.",
            )
            return None
        try:
            transcript = await self.transcribe_media(str(media.file_id))
        except SpeechToTextError as exc:
            await self._send_text(
                context,
                COMMAND_FAILURE_PREFIX
                + "Voice transcription failed: "
                + _public_error_summary(exc),
            )
            return None
        await self._send_text(
            context,
            COMMAND_SUCCESS_PREFIX + f"Transcribed: {transcript}",
        )
        return UserTurnInput(text=transcript)

    async def resolve_image_input(
        self,
        message: Message,
        context: ChatContext,
    ) -> UserTurnInput | None:
        photo = _preferred_photo(message)
        document = _image_document(message)
        media = photo or document
        if media is None:
            return None
        file_size = getattr(media, "file_size", None)
        if isinstance(file_size, int) and file_size > ATTACHMENT_MAX_SIZE_BYTES:
            await self._send_text(
                context,
                COMMAND_FAILURE_PREFIX + "Image is too large to send to Codex.",
            )
            return None
        try:
            data_url = await self.download_image_data_url(
                str(getattr(media, "file_id")),
                mime_type=(
                    document.mime_type
                    if document is not None and isinstance(document.mime_type, str)
                    else None
                ),
            )
        except ValueError as exc:
            await self._send_text(context, COMMAND_FAILURE_PREFIX + str(exc))
            return None
        caption = str(getattr(message, "caption", "") or "").strip()
        return UserTurnInput(
            text=caption,
            images=(UserTurnImage(url=data_url),),
        )

    async def transcribe_media(self, file_id: str) -> str:
        if self._speech_client is None:
            raise SpeechToTextError("Voice input is not enabled.")
        temp_path: Path | None = None
        try:
            remote_file = await self._bot.get_file(file_id)
            suffix = Path(str(getattr(remote_file, "file_path", ""))).suffix or ".ogg"
            with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as handle:
                temp_path = Path(handle.name)
            await self._bot.download(remote_file, destination=temp_path)
            return await self._speech_client.transcribe(temp_path)
        except SpeechToTextError:
            raise
        except Exception as exc:
            raise SpeechToTextError(str(exc)) from exc
        finally:
            if temp_path is not None:
                temp_path.unlink(missing_ok=True)

    async def download_image_data_url(
        self,
        file_id: str,
        *,
        mime_type: str | None,
    ) -> str:
        temp_path: Path | None = None
        try:
            remote_file = await self._bot.get_file(file_id)
            suffix = Path(str(getattr(remote_file, "file_path", ""))).suffix or ".img"
            with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as handle:
                temp_path = Path(handle.name)
            await self._bot.download(remote_file, destination=temp_path)
            payload = temp_path.read_bytes()
            if len(payload) > ATTACHMENT_MAX_SIZE_BYTES:
                raise ValueError("Image is too large to send to Codex.")
            resolved_mime_type = (
                mime_type
                or mimetypes.guess_type(str(getattr(remote_file, "file_path", "")))[0]
            )
            if not resolved_mime_type:
                resolved_mime_type = "image/jpeg"
            encoded = base64.b64encode(payload).decode("ascii")
            return f"data:{resolved_mime_type};base64,{encoded}"
        except ValueError:
            raise
        except Exception as exc:
            raise ValueError(f"Image download failed: {exc}") from exc
        finally:
            if temp_path is not None:
                temp_path.unlink(missing_ok=True)


def _preferred_photo(message: Message) -> Any | None:
    photo = getattr(message, "photo", None)
    if isinstance(photo, list) and photo:
        return photo[-1]
    return None


def _image_document(message: Message) -> Any | None:
    document = getattr(message, "document", None)
    if document is None:
        return None
    mime_type = getattr(document, "mime_type", None)
    if isinstance(mime_type, str) and mime_type.startswith("image/"):
        return document
    file_name = getattr(document, "file_name", None)
    guessed_type = (
        mimetypes.guess_type(file_name)[0] if isinstance(file_name, str) else None
    )
    if isinstance(guessed_type, str) and guessed_type.startswith("image/"):
        return document
    return None
