from pathlib import Path

import pytest

from codex_telegram.config import load_config


def test_load_config_uses_env_and_toml(tmp_path: Path) -> None:
    config_path = tmp_path / "codex_telegram.toml"
    config_path.write_text(
        """
[telegram]
enable_topic_sessions = false
typing_refresh_seconds = 4.0
wait_notice_seconds = 180.0
bridge_window_ttl_seconds = 900.0
default_language = ""

[defaults]
profile = "operator"

[client_default_profiles]
"chat:1" = "operator"

[profiles.operator]
model = "gpt-5.4"
model_provider = "openai"
approval_policy = "untrusted"
sandbox_type = "workspaceWrite"
network_access = false
verbosity = "verbose"
command_verbosity = "verbose"
followup_mode = "steer"
        """.strip(),
        encoding="utf-8",
    )

    config = load_config(
        config_path,
        env={
            "TELEGRAM_BOT_TOKEN": "token",
            "TELEGRAM_ALLOW_FROM": "*",
            "CODEX_APP_SERVER_WS_URL": "ws://127.0.0.1:4312",
            "CODEX_APP_SERVER_WS_TOKEN": "secret",
            "CODEX_TELEGRAM_DB_PATH": str(tmp_path / "state.db"),
        },
    )

    assert config.telegram.bot_token == "token"
    assert config.app_server_token == "secret"
    assert config.primary_app_server_id == "primary"
    assert config.app_servers["primary"].name == "primary"
    assert config.app_servers["primary"].url == "ws://127.0.0.1:4312"
    assert config.app_servers["primary"].token == "secret"
    assert config.default_profile == "operator"
    assert config.client_default_profiles["chat:1"] == "operator"
    assert config.profiles["operator"].model == "gpt-5.4"
    assert config.speech_to_text.enabled is False
    assert config.speech_to_text.provider == "codex"
    assert config.webhook.enabled is False
    assert str(config.attachments.shared_root) == "/attachments"


def test_load_config_supports_client_allowed_projects(tmp_path: Path) -> None:
    config_path = tmp_path / "codex_telegram.toml"
    config_path.write_text(
        """
[telegram]
enable_topic_sessions = false
typing_refresh_seconds = 4.0
wait_notice_seconds = 180.0
bridge_window_ttl_seconds = 900.0
default_language = ""

[defaults]
profile = "operator"

[client_allowed_projects]
"chat:1" = [
    { connection = "primary", root_path = "/agent" },
    { connection = "mac", root_path = "/workspace/project-b" },
]

[profiles.operator]
model = "gpt-5.4"
model_provider = "openai"
approval_policy = "untrusted"
sandbox_type = "workspaceWrite"
network_access = false
        """.strip(),
        encoding="utf-8",
    )

    config = load_config(
        config_path,
        env={
            "TELEGRAM_BOT_TOKEN": "token",
            "TELEGRAM_ALLOW_FROM": "*",
            "CODEX_APP_SERVER_WS_URL": "ws://127.0.0.1:4312",
            "CODEX_TELEGRAM_DB_PATH": str(tmp_path / "state.db"),
        },
    )

    assert [
        (rule.connection, rule.root_path)
        for rule in config.client_allowed_projects["chat:1"]
    ] == [
        ("primary", "/agent"),
        ("mac", "/workspace/project-b"),
    ]


def test_sample_config_enables_network_for_default_operator_profile() -> None:
    config_path = Path(__file__).resolve().parents[1] / "config" / "codex_telegram.toml"

    config = load_config(
        config_path,
        env={
            "TELEGRAM_BOT_TOKEN": "token",
            "TELEGRAM_ALLOW_FROM": "*",
            "CODEX_APP_SERVER_WS_URL": "ws://127.0.0.1:4312",
            "CODEX_TELEGRAM_DB_PATH": "/tmp/codex-telegram-test.db",
        },
    )

    assert config.default_profile == "operator"
    assert config.profiles["operator"].network_access is True


def test_sample_config_enables_codex_speech_to_text_by_default() -> None:
    config_path = Path(__file__).resolve().parents[1] / "config" / "codex_telegram.toml"

    config = load_config(
        config_path,
        env={
            "TELEGRAM_BOT_TOKEN": "token",
            "TELEGRAM_ALLOW_FROM": "*",
            "CODEX_APP_SERVER_WS_URL": "ws://127.0.0.1:4312",
            "CODEX_TELEGRAM_DB_PATH": "/tmp/codex-telegram-test.db",
        },
    )

    assert config.speech_to_text.enabled is True
    assert config.speech_to_text.provider == "codex"
    assert config.speech_to_text.base_url is None


def test_sample_config_uses_15_minute_conversation_idle_window() -> None:
    config_path = Path(__file__).resolve().parents[1] / "config" / "codex_telegram.toml"

    config = load_config(
        config_path,
        env={
            "TELEGRAM_BOT_TOKEN": "token",
            "TELEGRAM_ALLOW_FROM": "*",
            "CODEX_APP_SERVER_WS_URL": "ws://127.0.0.1:4312",
            "CODEX_TELEGRAM_DB_PATH": "/tmp/codex-telegram-test.db",
        },
    )

    assert config.telegram.focus_timeout_seconds == 900.0
    assert config.telegram.bridge_window_ttl_seconds == 900.0


def test_sample_config_uses_60_minute_active_waiting_window() -> None:
    config_path = Path(__file__).resolve().parents[1] / "config" / "codex_telegram.toml"

    config = load_config(
        config_path,
        env={
            "TELEGRAM_BOT_TOKEN": "token",
            "TELEGRAM_ALLOW_FROM": "*",
            "CODEX_APP_SERVER_WS_URL": "ws://127.0.0.1:4312",
            "CODEX_TELEGRAM_DB_PATH": "/tmp/codex-telegram-test.db",
        },
    )

    assert config.telegram.active_waiting_ttl_seconds == 3600.0


def test_load_config_does_not_map_bridge_ttl_to_focus_timeout(tmp_path: Path) -> None:
    config_path = tmp_path / "codex_telegram.toml"
    config_path.write_text(
        """
[telegram]
enable_topic_sessions = false
typing_refresh_seconds = 4.0
wait_notice_seconds = 180.0
bridge_window_ttl_seconds = 120.0
default_language = ""

[defaults]
profile = "operator"

[profiles.operator]
model = "gpt-5.4"
model_provider = "openai"
approval_policy = "untrusted"
sandbox_type = "workspaceWrite"
network_access = false
        """.strip(),
        encoding="utf-8",
    )

    config = load_config(
        config_path,
        env={
            "TELEGRAM_BOT_TOKEN": "token",
            "TELEGRAM_ALLOW_FROM": "*",
            "CODEX_APP_SERVER_WS_URL": "ws://127.0.0.1:4312",
            "CODEX_TELEGRAM_DB_PATH": "/tmp/codex-telegram-test.db",
        },
    )

    assert config.telegram.focus_timeout_seconds == 900.0
    assert config.telegram.active_waiting_ttl_seconds == 3600.0


def test_load_config_supports_named_app_servers(tmp_path: Path) -> None:
    config_path = tmp_path / "codex_telegram.toml"
    config_path.write_text(
        """
[telegram]
enable_topic_sessions = false
typing_refresh_seconds = 4.0
wait_notice_seconds = 180.0
bridge_window_ttl_seconds = 900.0
default_language = ""

[app_servers.home]
name = "home"
url = "ws://home.example:4312"
token_env = "HOME_CODEX_TOKEN"
primary = true

[app_servers.laptop]
name = "laptop"
url = "ws://laptop.example:4312"

[defaults]
profile = "operator"

[defaults.default_project]
connection = "home"
root_path = "/agent/app"
label = "app"

[profiles.operator]
model = "gpt-5.4"
model_provider = "openai"
approval_policy = "untrusted"
sandbox_type = "workspaceWrite"
network_access = false
        """.strip(),
        encoding="utf-8",
    )

    config = load_config(
        config_path,
        env={
            "TELEGRAM_BOT_TOKEN": "token",
            "TELEGRAM_ALLOW_FROM": "*",
            "HOME_CODEX_TOKEN": "home-secret",
            "CODEX_TELEGRAM_DB_PATH": str(tmp_path / "state.db"),
        },
    )

    assert config.primary_app_server_id == "home"
    assert config.app_servers["home"].name == "home"
    assert config.app_servers["home"].token == "home-secret"
    assert config.app_servers["laptop"].name == "laptop"
    assert config.app_servers["laptop"].token is None
    assert config.default_project is not None
    assert config.default_project.connection == "home"
    assert config.default_project.root_path == "/agent/app"
    assert config.default_project.label == "app"


def test_load_config_rejects_reserved_app_server_names(tmp_path: Path) -> None:
    config_path = tmp_path / "codex_telegram.toml"
    config_path.write_text(
        """
[telegram]
enable_topic_sessions = false
typing_refresh_seconds = 4.0
wait_notice_seconds = 180.0
bridge_window_ttl_seconds = 900.0
default_language = ""

[app_servers.primary]
name = "all"
url = "ws://127.0.0.1:4312"
primary = true

[defaults]
profile = "operator"

[profiles.operator]
model = "gpt-5.4"
model_provider = "openai"
approval_policy = "untrusted"
sandbox_type = "workspaceWrite"
network_access = false
        """.strip(),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="reserved"):
        load_config(
            config_path,
            env={
                "TELEGRAM_BOT_TOKEN": "token",
                "TELEGRAM_ALLOW_FROM": "*",
                "CODEX_TELEGRAM_DB_PATH": str(tmp_path / "state.db"),
            },
        )


def test_load_config_reads_webhook_admin_token_and_public_base_url(
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "codex_telegram.toml"
    config_path.write_text(
        """
[telegram]
enable_topic_sessions = false
typing_refresh_seconds = 4.0
wait_notice_seconds = 180.0
bridge_window_ttl_seconds = 900.0
default_language = ""

[webhook]
enabled = true
host = "0.0.0.0"
port = 8080
public_base_url = "https://codex.example"

[defaults]
profile = "operator"

[profiles.operator]
model = "gpt-5.4"
model_provider = "openai"
approval_policy = "untrusted"
sandbox_type = "workspaceWrite"
network_access = false
        """.strip(),
        encoding="utf-8",
    )

    config = load_config(
        config_path,
        env={
            "TELEGRAM_BOT_TOKEN": "token",
            "TELEGRAM_ALLOW_FROM": "*",
            "CODEX_APP_SERVER_WS_URL": "ws://127.0.0.1:4312",
            "CODEX_TELEGRAM_DB_PATH": str(tmp_path / "state.db"),
            "CODEX_TELEGRAM_WEBHOOK_TOKEN": "admin-secret",
        },
    )

    assert config.webhook.enabled is True
    assert config.webhook.admin_token == "admin-secret"
    assert config.webhook.public_base_url == "https://codex.example"


def test_load_config_reads_attachment_shared_root(tmp_path: Path) -> None:
    config_path = tmp_path / "codex_telegram.toml"
    config_path.write_text(
        """
[telegram]
enable_topic_sessions = false
typing_refresh_seconds = 4.0
wait_notice_seconds = 180.0
bridge_window_ttl_seconds = 900.0
default_language = ""

[attachments]
shared_root = "/shared-attachments"

[defaults]
profile = "operator"

[profiles.operator]
model = "gpt-5.4"
model_provider = "openai"
approval_policy = "untrusted"
sandbox_type = "workspaceWrite"
network_access = false
        """.strip(),
        encoding="utf-8",
    )

    config = load_config(
        config_path,
        env={
            "TELEGRAM_BOT_TOKEN": "token",
            "TELEGRAM_ALLOW_FROM": "*",
            "CODEX_APP_SERVER_WS_URL": "ws://127.0.0.1:4312",
            "CODEX_TELEGRAM_DB_PATH": str(tmp_path / "state.db"),
        },
    )

    assert config.attachments.shared_root == Path("/shared-attachments")


def test_load_config_supports_codex_speech_provider(tmp_path: Path) -> None:
    config_path = tmp_path / "codex_telegram.toml"
    config_path.write_text(
        """
[telegram]
enable_topic_sessions = false
typing_refresh_seconds = 4.0
wait_notice_seconds = 180.0
bridge_window_ttl_seconds = 900.0
default_language = "fr"

[speech_to_text]
enabled = true
provider = "codex"
model = "ignored-model"

[defaults]
profile = "operator"

[profiles.operator]
model = "gpt-5.4"
model_provider = "openai"
approval_policy = "untrusted"
sandbox_type = "workspaceWrite"
network_access = false
verbosity = "verbose"
command_verbosity = "verbose"
followup_mode = "steer"
        """.strip(),
        encoding="utf-8",
    )

    config = load_config(
        config_path,
        env={
            "TELEGRAM_BOT_TOKEN": "token",
            "TELEGRAM_ALLOW_FROM": "*",
            "CODEX_APP_SERVER_WS_URL": "ws://127.0.0.1:4312",
            "CODEX_TELEGRAM_DB_PATH": str(tmp_path / "state.db"),
            "SPEECH_TO_TEXT_BASE_URL": "https://codex.example",
            "SPEECH_TO_TEXT_API_KEY": "unused-key",
        },
    )

    assert config.speech_to_text.enabled is True
    assert config.speech_to_text.provider == "codex"
    assert config.speech_to_text.base_url == "https://codex.example"
    assert config.speech_to_text.api_key == "unused-key"
    assert config.speech_to_text.model == "ignored-model"
    assert config.speech_to_text.language_hint is None


def test_load_config_defaults_speech_provider_to_codex(tmp_path: Path) -> None:
    config_path = tmp_path / "codex_telegram.toml"
    config_path.write_text(
        """
[telegram]
enable_topic_sessions = false
typing_refresh_seconds = 4.0
wait_notice_seconds = 180.0
bridge_window_ttl_seconds = 900.0
default_language = ""

[speech_to_text]
enabled = true

[defaults]
profile = "operator"

[profiles.operator]
model = "gpt-5.4"
model_provider = "openai"
approval_policy = "untrusted"
sandbox_type = "workspaceWrite"
network_access = false
verbosity = "verbose"
command_verbosity = "verbose"
followup_mode = "steer"
        """.strip(),
        encoding="utf-8",
    )

    config = load_config(
        config_path,
        env={
            "TELEGRAM_BOT_TOKEN": "token",
            "TELEGRAM_ALLOW_FROM": "*",
            "CODEX_APP_SERVER_WS_URL": "ws://127.0.0.1:4312",
            "CODEX_TELEGRAM_DB_PATH": str(tmp_path / "state.db"),
            "SPEECH_TO_TEXT_BASE_URL": "https://codex.example",
        },
    )

    assert config.speech_to_text.enabled is True
    assert config.speech_to_text.provider == "codex"
