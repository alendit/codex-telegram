"""Main process entrypoint."""

from __future__ import annotations

import asyncio
import os

from aiohttp import ClientSession, web
from aiogram import Bot, Dispatcher

from codex_telegram.adapters.codex_app_server.client import CodexAppServerClient
from codex_telegram.adapters.codex_app_server.multi import MultiCodexBackend
from codex_telegram.adapters.filesystem import LocalDirectoryResolver
from codex_telegram.adapters.persistence.sqlite import (
    SQLiteStateRepository,
    SQLiteTelegramProgressStore,
)
from codex_telegram.adapters.speech_to_text import (
    CodexSpeechToTextClient,
    OpenAISpeechToTextClient,
    SpeechToTextClient,
)
from codex_telegram.adapters.telegram.bot import TelegramBotRunner
from codex_telegram.application.profiles import build_profiles
from codex_telegram.application.service import (
    BotService,
    BotServiceConfig,
    DefaultProjectConfig,
)
from codex_telegram.application.settings import ProjectAccessRule
from codex_telegram.config import SpeechToTextConfig, TelegramConfig, load_config
from codex_telegram.entrypoints.webhook import build_webhook_app
from codex_telegram.observability import configure_logging


async def _run() -> None:
    config = load_config()
    configure_logging(os.environ.get("CODEX_TELEGRAM_LOG_LEVEL", "INFO"))
    async with ClientSession() as http_session:
        repository = SQLiteStateRepository(
            config.db_path,
            default_backend_id=config.primary_app_server_id,
        )
        progress_store = SQLiteTelegramProgressStore(config.db_path)
        client = MultiCodexBackend(
            {
                backend_id: CodexAppServerClient(
                    http_session,
                    app_server.url,
                    app_server.token,
                    backend_id=backend_id,
                    backend_name=app_server.name,
                )
                for backend_id, app_server in config.app_servers.items()
            },
            config.app_servers,
            config.primary_app_server_id,
        )
        speech_client = build_speech_client(
            http_session,
            config.speech_to_text,
            config.telegram,
        )
        service = BotService(
            BotServiceConfig(
                default_profile=config.default_profile,
                client_default_profiles=config.client_default_profiles,
                client_allowed_projects={
                    chat_key: tuple(
                        ProjectAccessRule(
                            connection=rule.connection,
                            root_path=rule.root_path,
                        )
                        for rule in rules
                    )
                    for chat_key, rules in config.client_allowed_projects.items()
                },
                profiles=build_profiles(config.profiles),
                turn_poll_seconds=config.telegram.typing_refresh_seconds,
                wait_notice_seconds=config.telegram.wait_notice_seconds,
                bridge_window_ttl_seconds=config.telegram.bridge_window_ttl_seconds,
                focus_timeout_seconds=config.telegram.focus_timeout_seconds,
                active_waiting_ttl_seconds=(config.telegram.active_waiting_ttl_seconds),
                default_project=(
                    DefaultProjectConfig(
                        connection=config.default_project.connection,
                        root_path=config.default_project.root_path,
                        label=config.default_project.label,
                    )
                    if config.default_project is not None
                    else None
                ),
            ),
            repository,
            client,
            directory_resolver=LocalDirectoryResolver(),
        )
        await service.initialize()
        await progress_store.initialize()

        allow_from = parse_allow_from(config.telegram.allow_from)
        runner = TelegramBotRunner(
            Bot(config.telegram.bot_token),
            Dispatcher(),
            service,
            repository,
            progress_store,
            allow_from,
            config.telegram.enable_topic_sessions,
            speech_client=speech_client,
            webhook_public_base_url=config.webhook.public_base_url,
            webhook_local_base_url=webhook_local_base_url(
                config.webhook.host,
                config.webhook.port,
            ),
            attachment_roots=(config.attachments.shared_root,),
        )
        webhook_runner: web.AppRunner | None = None
        if config.webhook.enabled and config.webhook.admin_token:
            webhook_app = build_webhook_app(
                admin_token=config.webhook.admin_token,
                service=service,
                trigger_event=runner.run_webhook_event,
                public_base_url=config.webhook.public_base_url,
                local_base_url=webhook_local_base_url(
                    config.webhook.host,
                    config.webhook.port,
                ),
                bridge_command=runner.run_bridge_command,
                bridge_control=runner.enqueue_bridge_control,
                attachment_roots=(config.attachments.shared_root,),
            )
            webhook_runner = web.AppRunner(webhook_app)
            await webhook_runner.setup()
            site = web.TCPSite(
                webhook_runner,
                host=config.webhook.host,
                port=config.webhook.port,
            )
            await site.start()
        try:
            await runner.run()
        finally:
            await runner.close()
            if webhook_runner is not None:
                await webhook_runner.cleanup()
            await client.async_close()


def main() -> None:
    """Run the codex-telegram process."""
    asyncio.run(_run())


if __name__ == "__main__":
    main()


def build_speech_client(
    http_session: ClientSession,
    speech_config: SpeechToTextConfig,
    telegram_config: TelegramConfig,
) -> SpeechToTextClient | None:
    """Build the configured speech-to-text client, if enabled."""
    if not speech_config.enabled:
        return None
    if speech_config.provider == "openai" and not speech_config.base_url:
        raise ValueError("Speech-to-text is enabled but base_url is not configured.")

    language_hint = speech_config.language_hint or telegram_config.default_language
    if speech_config.provider == "openai":
        assert speech_config.base_url is not None
        return OpenAISpeechToTextClient(
            http_session,
            base_url=speech_config.base_url,
            api_key=speech_config.api_key,
            model=speech_config.model,
            language_hint=language_hint,
            request_timeout_seconds=speech_config.request_timeout_seconds,
        )
    if speech_config.provider == "codex":
        return CodexSpeechToTextClient(
            http_session,
            base_url=speech_config.base_url,
            language_hint=language_hint,
            request_timeout_seconds=speech_config.request_timeout_seconds,
        )
    raise ValueError(f"Unknown speech-to-text provider: {speech_config.provider}")


def parse_allow_from(raw_value: str) -> set[int] | None:
    """Parse a comma-separated allow list."""
    value = raw_value.strip()
    if not value or value == "*":
        return None
    allowed: set[int] = set()
    for item in value.split(","):
        item = item.strip()
        if item:
            allowed.add(int(item))
    return allowed


def webhook_local_base_url(host: str, port: int) -> str:
    """Return a local URL suitable for helper output when no public URL is set."""
    display_host = "127.0.0.1" if host in {"0.0.0.0", "::"} else host
    return f"http://{display_host}:{port}"
