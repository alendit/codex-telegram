"""Runtime configuration loading."""

from __future__ import annotations

from dataclasses import dataclass, field
from collections.abc import Mapping
import os
from pathlib import Path
import re
import tomllib

APP_SERVER_NAME_RE = re.compile(r"^[A-Za-z0-9_-]+$")
RESERVED_APP_SERVER_NAMES = {"all", "full"}


@dataclass(slots=True)
class ProfileConfig:
    """One configured Codex runtime profile."""

    name: str
    model: str
    model_provider: str
    approval_policy: str
    sandbox_type: str
    network_access: bool
    writable_roots: list[str] = field(default_factory=list)
    effort: str = "medium"
    fast_mode: bool = False
    summary: str = "concise"
    verbosity: str = "verbose"
    command_verbosity: str = "errors"
    followup_mode: str = "steer"
    developer_instructions: str | None = None


@dataclass(slots=True)
class TelegramConfig:
    """Telegram-specific runtime configuration."""

    bot_token: str
    allow_from: str
    enable_topic_sessions: bool
    typing_refresh_seconds: float
    wait_notice_seconds: float
    bridge_window_ttl_seconds: float
    default_language: str | None
    focus_timeout_seconds: float = 900.0
    active_waiting_ttl_seconds: float = 3600.0


@dataclass(slots=True)
class SpeechToTextConfig:
    """Optional speech-to-text runtime configuration."""

    enabled: bool
    provider: str
    base_url: str | None
    api_key: str | None
    model: str
    language_hint: str | None
    request_timeout_seconds: float


@dataclass(slots=True)
class WebhookConfig:
    """Optional durable webhook configuration."""

    enabled: bool
    host: str
    port: int
    admin_token: str | None
    public_base_url: str | None


@dataclass(slots=True)
class AttachmentConfig:
    """Outbound attachment configuration."""

    shared_root: Path


@dataclass(frozen=True, slots=True)
class AppServerConfig:
    """One configured codex app-server websocket backend."""

    backend_id: str
    name: str
    url: str
    token: str | None = None
    primary: bool = False


@dataclass(frozen=True, slots=True)
class DefaultProjectConfig:
    """Configured project used by the New -> Default shortcut."""

    connection: str
    root_path: str
    label: str | None = None


@dataclass(frozen=True, slots=True)
class ProjectAccessConfig:
    """One configured project allowlist entry for a Telegram client id."""

    connection: str
    root_path: str


@dataclass(slots=True)
class AppConfig:
    """Complete application configuration."""

    telegram: TelegramConfig
    speech_to_text: SpeechToTextConfig
    webhook: WebhookConfig
    attachments: AttachmentConfig
    app_server_url: str
    app_server_token: str | None
    db_path: Path
    default_profile: str
    client_default_profiles: dict[str, str]
    profiles: dict[str, ProfileConfig]
    client_allowed_projects: dict[str, tuple[ProjectAccessConfig, ...]] = field(
        default_factory=dict
    )
    app_servers: dict[str, AppServerConfig] = field(default_factory=dict)
    primary_app_server_id: str = "primary"
    default_project: DefaultProjectConfig | None = None


def load_config(
    config_path: str | os.PathLike[str] | None = None,
    *,
    env: dict[str, str] | None = None,
) -> AppConfig:
    """Load TOML and environment configuration into one object."""
    resolved_env = env or os.environ
    path = Path(
        config_path
        or resolved_env.get("CODEX_TELEGRAM_CONFIG")
        or "config/codex_telegram.toml"
    )
    data = tomllib.loads(path.read_text(encoding="utf-8"))

    telegram_data = data["telegram"]
    default_language = str(telegram_data.get("default_language", "")).strip() or None

    profiles = {
        name: ProfileConfig(
            name=name,
            model=str(value["model"]),
            model_provider=str(value.get("model_provider", "openai")),
            approval_policy=str(value.get("approval_policy", "never")),
            sandbox_type=str(value.get("sandbox_type", "readOnly")),
            network_access=bool(value.get("network_access", False)),
            writable_roots=[str(root) for root in value.get("writable_roots", [])],
            effort=str(value.get("effort", "medium")),
            fast_mode=bool(value.get("fast_mode", False)),
            summary=str(value.get("summary", "concise")),
            verbosity=str(value.get("verbosity", "verbose")),
            command_verbosity=str(value.get("command_verbosity", "errors")),
            followup_mode=str(value.get("followup_mode", "steer")),
            developer_instructions=(
                str(value["developer_instructions"])
                if value.get("developer_instructions")
                else None
            ),
        )
        for name, value in data["profiles"].items()
    }
    speech_data = data.get("speech_to_text", {})
    speech_language = str(speech_data.get("language_hint", "")).strip() or None
    webhook_data = data.get("webhook", {})
    attachment_data = data.get("attachments", {})
    app_servers, primary_app_server_id = _load_app_servers(data, resolved_env)
    primary_app_server = app_servers[primary_app_server_id]
    app_server_url = primary_app_server.url
    defaults_data = data.get("defaults", {})
    speech_provider = str(speech_data.get("provider", "codex"))
    speech_base_url = resolved_env.get("SPEECH_TO_TEXT_BASE_URL") or (
        str(speech_data["base_url"]) if speech_data.get("base_url") else None
    )

    focus_timeout_seconds = float(
        telegram_data.get(
            "focus_timeout_seconds",
            telegram_data.get(
                "bridge_window_ttl_seconds",
                telegram_data.get("auto_new_thread_idle_seconds", 900.0),
            ),
        )
    )
    active_waiting_ttl_seconds = float(
        telegram_data.get(
            "active_waiting_ttl_seconds",
            3600.0,
        )
    )

    return AppConfig(
        telegram=TelegramConfig(
            bot_token=resolved_env["TELEGRAM_BOT_TOKEN"],
            allow_from=resolved_env.get("TELEGRAM_ALLOW_FROM", "*"),
            enable_topic_sessions=bool(
                telegram_data.get("enable_topic_sessions", False)
            ),
            typing_refresh_seconds=float(
                telegram_data.get("typing_refresh_seconds", 4.0)
            ),
            wait_notice_seconds=float(telegram_data.get("wait_notice_seconds", 180.0)),
            bridge_window_ttl_seconds=float(
                telegram_data.get(
                    "bridge_window_ttl_seconds",
                    telegram_data.get("auto_new_thread_idle_seconds", 900.0),
                )
            ),
            default_language=default_language,
            focus_timeout_seconds=focus_timeout_seconds,
            active_waiting_ttl_seconds=active_waiting_ttl_seconds,
        ),
        speech_to_text=SpeechToTextConfig(
            enabled=bool(speech_data.get("enabled", False)),
            provider=speech_provider,
            base_url=speech_base_url,
            api_key=resolved_env.get("SPEECH_TO_TEXT_API_KEY")
            or (str(speech_data["api_key"]) if speech_data.get("api_key") else None),
            model=str(speech_data.get("model", "whisper-1")),
            language_hint=speech_language,
            request_timeout_seconds=float(
                speech_data.get("request_timeout_seconds", 60.0)
            ),
        ),
        webhook=WebhookConfig(
            enabled=bool(webhook_data.get("enabled", False)),
            host=str(webhook_data.get("host", "127.0.0.1")),
            port=int(webhook_data.get("port", 8080)),
            admin_token=resolved_env.get("CODEX_TELEGRAM_WEBHOOK_TOKEN")
            or (
                str(webhook_data["admin_token"])
                if webhook_data.get("admin_token")
                else None
            ),
            public_base_url=resolved_env.get("CODEX_TELEGRAM_WEBHOOK_PUBLIC_BASE_URL")
            or (
                str(webhook_data["public_base_url"])
                if webhook_data.get("public_base_url")
                else None
            ),
        ),
        attachments=AttachmentConfig(
            shared_root=Path(
                str(attachment_data.get("shared_root", "/attachments"))
            ),
        ),
        app_server_url=app_server_url,
        app_server_token=primary_app_server.token,
        db_path=Path(
            resolved_env.get("CODEX_TELEGRAM_DB_PATH", "/state/codex-telegram.db")
        ),
        default_profile=str(defaults_data.get("profile", "operator")),
        client_default_profiles={
            str(key): str(value)
            for key, value in data.get("client_default_profiles", {}).items()
        },
        client_allowed_projects=_load_client_allowed_projects(data),
        profiles=profiles,
        app_servers=app_servers,
        primary_app_server_id=primary_app_server_id,
        default_project=_load_default_project(defaults_data),
    )


def _load_app_servers(
    data: dict[str, object],
    env: Mapping[str, str],
) -> tuple[dict[str, AppServerConfig], str]:
    raw_servers = data.get("app_servers")
    if not isinstance(raw_servers, dict) or not raw_servers:
        token = env.get("CODEX_APP_SERVER_WS_TOKEN") or None
        server = AppServerConfig(
            backend_id="primary",
            name="primary",
            url=env.get("CODEX_APP_SERVER_WS_URL", "ws://127.0.0.1:4312"),
            token=token,
            primary=True,
        )
        return {server.backend_id: server}, server.backend_id

    app_servers: dict[str, AppServerConfig] = {}
    seen_names: set[str] = set()
    primary_ids: list[str] = []
    for raw_backend_id, raw_value in raw_servers.items():
        if not isinstance(raw_value, dict):
            raise ValueError(f"app_servers.{raw_backend_id} must be a table.")
        backend_id = str(raw_backend_id).strip()
        name = str(raw_value.get("name", backend_id)).strip()
        _validate_app_server_name(name)
        folded_name = name.casefold()
        if folded_name in seen_names:
            raise ValueError(f"Duplicate app-server friendly name: {name}")
        seen_names.add(folded_name)
        primary = bool(raw_value.get("primary", False))
        if primary:
            primary_ids.append(backend_id)
        token_env = str(raw_value.get("token_env", "")).strip()
        token = (
            env.get(token_env)
            if token_env
            else (env.get("CODEX_APP_SERVER_WS_TOKEN") if primary else None)
        )
        app_servers[backend_id] = AppServerConfig(
            backend_id=backend_id,
            name=name,
            url=str(raw_value["url"]),
            token=token or None,
            primary=primary,
        )

    if len(primary_ids) != 1:
        raise ValueError("Exactly one app-server backend must be primary.")
    return app_servers, primary_ids[0]


def _validate_app_server_name(name: str) -> None:
    if not name:
        raise ValueError("App-server friendly name must not be empty.")
    if name.casefold() in RESERVED_APP_SERVER_NAMES:
        raise ValueError(f"App-server friendly name is reserved: {name}")
    if APP_SERVER_NAME_RE.fullmatch(name) is None:
        raise ValueError(
            "App-server friendly names may contain only letters, numbers, _, and -."
        )


def _load_default_project(defaults_data: object) -> DefaultProjectConfig | None:
    if not isinstance(defaults_data, dict):
        return None
    raw_project = defaults_data.get("default_project")
    if not isinstance(raw_project, dict):
        return None
    connection = str(raw_project.get("connection", "")).strip()
    root_path = str(raw_project.get("root_path", "")).strip()
    if not connection or not root_path:
        raise ValueError("defaults.default_project requires connection and root_path.")
    return DefaultProjectConfig(
        connection=connection,
        root_path=root_path,
        label=str(raw_project.get("label", "")).strip() or None,
    )


def _load_client_allowed_projects(
    data: Mapping[str, object],
) -> dict[str, tuple[ProjectAccessConfig, ...]]:
    raw_allowlists = data.get("client_allowed_projects", {})
    if not isinstance(raw_allowlists, dict):
        raise ValueError("client_allowed_projects must be a table.")
    allowlists: dict[str, tuple[ProjectAccessConfig, ...]] = {}
    for raw_chat_key, raw_rules in raw_allowlists.items():
        chat_key = str(raw_chat_key).strip()
        if not chat_key:
            raise ValueError("client_allowed_projects keys must not be empty.")
        if not isinstance(raw_rules, list):
            raise ValueError(
                f"client_allowed_projects.{chat_key} must be a list of projects."
            )
        rules: list[ProjectAccessConfig] = []
        for raw_rule in raw_rules:
            if not isinstance(raw_rule, dict):
                raise ValueError(
                    f"client_allowed_projects.{chat_key} entries must be tables."
                )
            connection = str(raw_rule.get("connection", "")).strip()
            root_path = str(raw_rule.get("root_path", "")).strip()
            if not connection or not root_path:
                raise ValueError(
                    f"client_allowed_projects.{chat_key} entries require "
                    "connection and root_path."
                )
            rules.append(
                ProjectAccessConfig(connection=connection, root_path=root_path)
            )
        allowlists[chat_key] = tuple(rules)
    return allowlists
