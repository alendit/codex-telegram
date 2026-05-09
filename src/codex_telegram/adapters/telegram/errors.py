"""Telegram API error classifiers."""

from __future__ import annotations

from aiogram.exceptions import TelegramRetryAfter


def _telegram_message_not_modified(err: Exception) -> bool:
    """Return whether Telegram rejected a no-op edit."""
    return "message is not modified" in str(err).lower()


def _telegram_retry_after_seconds(err: Exception) -> float | None:
    """Return Telegram retry-after seconds when the error is a flood-control response."""
    if isinstance(err, TelegramRetryAfter):
        return max(float(err.retry_after), 0.0)
    retry_after = getattr(err, "retry_after", None)
    if isinstance(retry_after, int | float):
        return max(float(retry_after), 0.0)
    return None
