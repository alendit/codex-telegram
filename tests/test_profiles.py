from codex_telegram.application.profiles import (
    build_profiles,
    default_profile_for_chat,
)
from codex_telegram.config import load_config


def test_profiles_use_configured_names_only() -> None:
    config = load_config(
        env={
            "TELEGRAM_BOT_TOKEN": "token",
            "TELEGRAM_ALLOW_FROM": "*",
            "CODEX_APP_SERVER_WS_URL": "ws://127.0.0.1:4312",
            "CODEX_APP_SERVER_WS_TOKEN": "secret",
            "CODEX_TELEGRAM_DB_PATH": "/tmp/state.db",
        }
    )
    profiles = build_profiles(config.profiles)

    assert "readonly" in profiles
    assert (
        default_profile_for_chat(
            config.client_default_profiles, "chat:example", "operator"
        )
        == "operator"
    )
