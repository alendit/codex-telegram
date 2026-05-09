"""Developer smoke test for messaging the Telegram bot as a real user."""

from __future__ import annotations

import argparse
import asyncio
from dataclasses import dataclass
import os
from pathlib import Path
import sys

from telethon import TelegramClient  # type: ignore[import-untyped]
from telethon.tl.custom.conversation import Conversation  # type: ignore[import-untyped]

from codex_telegram.observability import configure_logging, get_logger, log_info

LOGGER = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class SmokeConfig:
    """Settings for a one-shot Telegram smoke test."""

    api_id: int
    api_hash: str
    bot: str
    message: str
    session: str
    timeout_seconds: float
    reset_session: bool


def _parse_args(argv: list[str] | None = None) -> SmokeConfig:
    parser = argparse.ArgumentParser(
        description="Send one Telegram message to the bot as a real user session."
    )
    parser.add_argument(
        "--bot",
        default=os.environ.get("TELEGRAM_TEST_BOT"),
        help="Bot username or entity, for example @agent_mauzi_bot.",
    )
    parser.add_argument(
        "--message",
        default="ping",
        help="Message to send to the bot.",
    )
    parser.add_argument(
        "--session",
        default=os.environ.get(
            "TELEGRAM_TEST_SESSION",
            str(Path(".state/telegram-smoke").resolve()),
        ),
        help="Telethon session path. Defaults to .state/telegram-smoke.",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=float(os.environ.get("TELEGRAM_TEST_TIMEOUT", "60")),
        help="Seconds to wait for the bot reply.",
    )
    parser.add_argument(
        "--reset-session",
        action="store_true",
        help="Delete the existing Telethon session before logging in again.",
    )

    namespace = parser.parse_args(argv)
    api_id = os.environ.get("TELEGRAM_TEST_API_ID")
    api_hash = os.environ.get("TELEGRAM_TEST_API_HASH")

    if not api_id:
        raise SystemExit("TELEGRAM_TEST_API_ID is required.")
    if not api_hash:
        raise SystemExit("TELEGRAM_TEST_API_HASH is required.")
    if not namespace.bot:
        raise SystemExit("--bot or TELEGRAM_TEST_BOT is required.")

    return SmokeConfig(
        api_id=int(api_id),
        api_hash=api_hash,
        bot=namespace.bot,
        message=namespace.message,
        session=namespace.session,
        timeout_seconds=namespace.timeout,
        reset_session=namespace.reset_session,
    )


async def _run_smoke(config: SmokeConfig) -> None:
    session_path = Path(config.session)
    session_path.parent.mkdir(parents=True, exist_ok=True)
    if config.reset_session:
        _remove_session_files(session_path)

    log_info(
        LOGGER,
        "telegram_smoke_starting",
        v={
            "bot": config.bot,
            "session": str(session_path),
            "timeout_seconds": config.timeout_seconds,
            "reset_session": config.reset_session,
        },
    )

    async with TelegramClient(
        str(session_path),
        config.api_id,
        config.api_hash,
    ) as client:
        me = await client.get_me()
        if getattr(me, "bot", False):
            raise SystemExit(
                "This Telethon session is logged in as a bot. Re-run with "
                "--reset-session and complete the login as your Telegram user account."
            )
        async with client.conversation(
            config.bot,
            timeout=config.timeout_seconds,
        ) as conversation:
            await _send_and_wait(conversation, config.message)


async def _send_and_wait(conversation: Conversation, message: str) -> None:
    log_info(
        LOGGER,
        "telegram_smoke_message_sending",
        v={"message_length": len(message)},
    )
    await conversation.send_message(message)
    response = await conversation.get_response()
    reply_text = response.raw_text or "<non-text reply>"
    log_info(
        LOGGER,
        "telegram_smoke_reply_received",
        v={
            "message_id": response.id,
            "text_length": len(reply_text),
        },
    )
    print(reply_text)


def main() -> None:
    """Run the smoke test."""
    configure_logging(os.environ.get("CODEX_TELEGRAM_LOG_LEVEL", "INFO"))
    config = _parse_args()
    asyncio.run(_run_smoke(config))


def _remove_session_files(session_path: Path) -> None:
    """Delete Telethon session files for a clean user login."""
    for suffix in ("", ".session", ".session-journal"):
        candidate = session_path if not suffix else Path(f"{session_path}{suffix}")
        if candidate.exists():
            candidate.unlink()
            log_info(
                LOGGER,
                "telegram_smoke_session_removed",
                v={"path": str(candidate)},
            )


if __name__ == "__main__":
    main()
