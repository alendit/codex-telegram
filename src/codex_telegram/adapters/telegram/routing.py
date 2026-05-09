"""Telegram chat routing helpers."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ChatContext:
    """Telegram routing metadata for one message."""

    chat_key: str
    chat_id: int
    topic_id: int | None


def build_chat_key(
    chat_id: int,
    topic_id: int | None,
    *,
    enable_topic_sessions: bool,
) -> str:
    """Build the persistence key for one Telegram chat or topic."""
    if enable_topic_sessions and topic_id is not None:
        return f"chat:{chat_id}:{topic_id}"
    return f"chat:{chat_id}"


def chat_context_from_key(chat_key: str) -> ChatContext:
    """Recover a Telegram chat context from a stored chat key."""
    parts = chat_key.split(":")
    if len(parts) == 2 and parts[0] == "chat":
        return ChatContext(chat_key=chat_key, chat_id=int(parts[1]), topic_id=None)
    if len(parts) == 3 and parts[0] == "chat":
        return ChatContext(
            chat_key=chat_key,
            chat_id=int(parts[1]),
            topic_id=int(parts[2]),
        )
    raise ValueError(f"Unsupported chat key: {chat_key}")
