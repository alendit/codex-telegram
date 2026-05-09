"""Telegram attachment job delivery."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Sequence
from pathlib import Path
from typing import Protocol

from aiogram import Bot
from aiogram.types import FSInputFile

from codex_telegram.adapters.telegram.rendering import WARNING_PREFIX
from codex_telegram.adapters.telegram.routing import ChatContext, chat_context_from_key
from codex_telegram.domain import AttachmentJob
from codex_telegram.observability import get_logger, log_exception

LOGGER = get_logger(__name__)

ATTACHMENT_POLL_SECONDS = 1.0
ATTACHMENT_MAX_SIZE_BYTES = 45 * 1024 * 1024
ATTACHMENT_IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".gif", ".webp"}

SendText = Callable[[ChatContext, str], Awaitable[object]]


class AttachmentRepository(Protocol):
    async def list_pending_attachment_jobs(
        self,
        *,
        limit: int = 20,
    ) -> list[AttachmentJob]: ...

    async def mark_attachment_job_delivered(self, job_id: int) -> None: ...

    async def mark_attachment_job_failed(self, job_id: int, error: str) -> None: ...


class TelegramAttachmentDelivery:
    """Drain queued attachment jobs and deliver files through Telegram."""

    def __init__(
        self,
        bot: Bot,
        repository: AttachmentRepository,
        send_text: SendText,
        *,
        allowed_roots: Sequence[Path] | None = None,
    ) -> None:
        self._bot = bot
        self._repository = repository
        self._send_text = send_text
        self._allowed_roots = tuple(allowed_roots or (Path.cwd(),))

    async def run_loop(self) -> None:
        while True:
            await self.drain_once()
            await asyncio.sleep(ATTACHMENT_POLL_SECONDS)

    async def drain_once(self) -> None:
        jobs = await self._repository.list_pending_attachment_jobs(limit=20)
        for job in jobs:
            try:
                await self.deliver(job)
            except Exception as exc:
                log_exception(
                    LOGGER,
                    "telegram_attachment_delivery_failed",
                    err=exc,
                    job_id=job.job_id,
                    logical_thread_id=job.logical_thread_id,
                )
                if job.job_id is not None:
                    await self._repository.mark_attachment_job_failed(
                        job.job_id, str(exc)
                    )
                context = chat_context_from_key(job.chat_key)
                await self._send_text(
                    context,
                    WARNING_PREFIX
                    + f"Attachment delivery failed for {Path(job.path).name}: {exc}",
                )
            else:
                if job.job_id is not None:
                    await self._repository.mark_attachment_job_delivered(job.job_id)

    async def deliver(self, job: AttachmentJob) -> None:
        path = validate_external_attachment_path(
            job.path,
            allowed_roots=self._allowed_roots,
        )
        context = chat_context_from_key(job.chat_key)
        caption = job.caption
        if _attachment_is_photo(path):
            await self._bot.send_photo(
                chat_id=context.chat_id,
                photo=FSInputFile(str(path)),
                caption=caption,
                message_thread_id=context.topic_id,
            )
            return
        await self._bot.send_document(
            chat_id=context.chat_id,
            document=FSInputFile(str(path)),
            caption=caption,
            message_thread_id=context.topic_id,
        )


def _validate_attachment_path(raw_path: str) -> Path:
    return _validate_path_against_roots(raw_path, allowed_roots=_default_image_roots())


def validate_external_attachment_path(
    raw_path: str,
    *,
    allowed_roots: Sequence[Path],
) -> Path:
    return _validate_path_against_roots(raw_path, allowed_roots=allowed_roots)


def _validate_path_against_roots(
    raw_path: str,
    *,
    allowed_roots: Sequence[Path],
) -> Path:
    path = Path(raw_path).expanduser().resolve()
    if not path.exists():
        raise ValueError(f"Attachment path does not exist: {path}")
    if not path.is_file():
        raise ValueError(f"Attachment path is not a file: {path}")
    if path.stat().st_size > ATTACHMENT_MAX_SIZE_BYTES:
        raise ValueError(f"Attachment is too large: {path.name}")
    if not _path_is_allowed(path, allowed_roots):
        raise ValueError(f"Attachment path is not allowlisted: {path}")
    return path


def _default_image_roots() -> tuple[Path, ...]:
    return (Path("/agent"), Path("/tmp"), Path.cwd())


def _path_is_allowed(path: Path, allowed_roots: Sequence[Path]) -> bool:
    for root in allowed_roots:
        try:
            path.relative_to(root.resolve())
        except ValueError:
            continue
        return True
    return False


def _attachment_is_photo(path: Path) -> bool:
    return path.suffix.lower() in ATTACHMENT_IMAGE_EXTENSIONS
