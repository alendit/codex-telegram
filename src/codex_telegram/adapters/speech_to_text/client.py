"""Speech-to-text adapter implementations."""

from __future__ import annotations

import base64
import asyncio
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import json
import mimetypes
import os
import platform
from pathlib import Path
import secrets
import tomllib
from typing import Protocol
import urllib.error
import urllib.request
from urllib.parse import urlsplit, urlunsplit

from aiohttp import ClientSession, ClientTimeout, FormData


class SpeechToTextError(RuntimeError):
    """Raised when speech-to-text transcription fails."""


class _CodexTranscribeHttpStatusError(SpeechToTextError):
    def __init__(self, status: int, detail: object) -> None:
        super().__init__(str(detail))
        self.status = status


class SpeechToTextClient(Protocol):
    """Telegram-facing speech-to-text boundary."""

    async def transcribe(self, path: Path) -> str: ...


_DEFAULT_CODEX_CHATGPT_BASE_URL = "https://chatgpt.com/backend-api"
_DEFAULT_CODEX_AUTH_BASE_URL = "https://auth.openai.com"
_DEFAULT_CODEX_OAUTH_CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"
_DEFAULT_CODEX_OAUTH_SCOPE = "openid profile email"
_DEFAULT_CODEX_USER_AGENT = "Codex Desktop"
_TOKEN_REFRESH_INTERVAL = timedelta(days=8)


@dataclass(slots=True)
class _CodexRuntimeAuth:
    raw: dict[str, object]
    access_token: str
    refresh_token: str | None
    account_id: str | None
    last_refresh_at: datetime | None


def _resolve_codex_auth_path() -> Path:
    codex_home = os.environ.get("CODEX_HOME")
    if codex_home:
        return Path(codex_home) / "auth.json"
    return Path.home() / ".codex" / "auth.json"


def _resolve_codex_config_path() -> Path:
    codex_home = os.environ.get("CODEX_HOME")
    if codex_home:
        return Path(codex_home) / "config.toml"
    return Path.home() / ".codex" / "config.toml"


def _normalize_chatgpt_base_url(url: str) -> str:
    normalized = url.strip().rstrip("/")
    if not normalized:
        return _DEFAULT_CODEX_CHATGPT_BASE_URL
    parts = urlsplit(normalized)
    if parts.scheme not in {"http", "https"} or not parts.netloc:
        return normalized
    if parts.netloc not in {"chatgpt.com", "chat.openai.com"}:
        return normalized
    path = parts.path.rstrip("/")
    if not path:
        path = "/backend-api"
    elif "/backend-api" not in path:
        path = f"{path}/backend-api"
    return urlunsplit((parts.scheme, parts.netloc, path, parts.query, parts.fragment))


def _load_runtime_config(config_path: Path) -> dict[str, object]:
    try:
        raw = tomllib.loads(config_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, tomllib.TOMLDecodeError):
        return {}
    return raw if isinstance(raw, dict) else {}


def _load_runtime_chatgpt_base_url(config_path: Path) -> str:
    raw = _load_runtime_config(config_path)
    configured = raw.get("chatgpt_base_url")
    if isinstance(configured, str) and configured.strip():
        return _normalize_chatgpt_base_url(configured)
    return _DEFAULT_CODEX_CHATGPT_BASE_URL


async def _load_runtime_chatgpt_base_url_async(config_path: Path) -> str:
    return await asyncio.to_thread(_load_runtime_chatgpt_base_url, config_path)


def _load_enabled_codex_beta_features(config_path: Path) -> str | None:
    raw = _load_runtime_config(config_path)
    features = raw.get("features")
    if not isinstance(features, dict):
        return None
    enabled = [
        key.strip()
        for key, value in features.items()
        if isinstance(key, str) and key.strip() and value is True
    ]
    if not enabled:
        return None
    return ",".join(enabled)


async def _load_enabled_codex_beta_features_async(config_path: Path) -> str | None:
    return await asyncio.to_thread(_load_enabled_codex_beta_features, config_path)


async def _decode_response_payload(response) -> object:
    try:
        return await response.json(content_type=None)
    except Exception:
        text_payload = await response.text()
        return text_payload


def _get_token_value(
    raw_tokens: dict[str, object], snake_key: str, camel_key: str
) -> str | None:
    value = raw_tokens.get(snake_key)
    if value in (None, ""):
        value = raw_tokens.get(camel_key)
    if not isinstance(value, str) or not value.strip():
        return None
    return value.strip()


def _parse_timestamp(raw_value: object) -> datetime | None:
    if not isinstance(raw_value, str) or not raw_value.strip():
        return None
    normalized = raw_value.strip()
    if normalized.endswith("Z"):
        normalized = f"{normalized[:-1]}+00:00"
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _format_timestamp(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _decode_jwt_payload(token: str) -> dict[str, object] | None:
    parts = token.split(".")
    if len(parts) < 2:
        return None
    payload = parts[1]
    padding = "=" * (-len(payload) % 4)
    try:
        decoded = base64.urlsafe_b64decode(payload + padding)
        raw = json.loads(decoded)
    except Exception:
        return None
    if not isinstance(raw, dict):
        return None
    return raw


def _extract_account_id_from_id_token(id_token: str) -> str | None:
    payload = _decode_jwt_payload(id_token)
    if not isinstance(payload, dict):
        return None
    auth_claims = payload.get("https://api.openai.com/auth")
    if isinstance(auth_claims, dict):
        account_id = auth_claims.get("chatgpt_account_id")
        if isinstance(account_id, str) and account_id.strip():
            return account_id.strip()
    account_id = payload.get("chatgpt_account_id")
    if isinstance(account_id, str) and account_id.strip():
        return account_id.strip()
    return None


def _set_mapping_value(
    raw_mapping: dict[str, object],
    snake_key: str,
    camel_key: str,
    value: object,
) -> None:
    updated = False
    for key in (snake_key, camel_key):
        if key in raw_mapping:
            raw_mapping[key] = value
            updated = True
    if not updated:
        raw_mapping[snake_key] = value


def _build_runtime_auth_payload(
    raw_auth: dict[str, object],
    *,
    access_token: str,
    refresh_token: str,
    id_token: str,
    account_id: str | None,
    refreshed_at: datetime,
) -> dict[str, object]:
    updated = dict(raw_auth)
    raw_tokens = updated.get("tokens")
    if not isinstance(raw_tokens, dict):
        raw_tokens = {}
    else:
        raw_tokens = dict(raw_tokens)
    updated["tokens"] = raw_tokens
    _set_mapping_value(raw_tokens, "access_token", "accessToken", access_token)
    _set_mapping_value(raw_tokens, "refresh_token", "refreshToken", refresh_token)
    _set_mapping_value(raw_tokens, "id_token", "idToken", id_token)
    if account_id is not None:
        _set_mapping_value(raw_tokens, "account_id", "accountId", account_id)
    _set_mapping_value(
        updated, "last_refresh", "lastRefreshAt", _format_timestamp(refreshed_at)
    )
    return updated


def _persist_runtime_auth(auth_path: Path, raw_auth: dict[str, object]) -> None:
    auth_path.write_text(f"{json.dumps(raw_auth, indent=2)}\n", encoding="utf-8")


async def _persist_runtime_auth_async(
    auth_path: Path, raw_auth: dict[str, object]
) -> None:
    await asyncio.to_thread(_persist_runtime_auth, auth_path, raw_auth)


async def _read_file_bytes(path: Path) -> bytes:
    return await asyncio.to_thread(path.read_bytes)


def _extract_error_detail(payload: object) -> object:
    if isinstance(payload, dict):
        detail = payload.get("error")
        if isinstance(detail, dict):
            return detail.get("message") or detail
        if detail is not None:
            return detail
        detail = payload.get("detail")
        if detail is not None:
            return detail
    return payload


def _is_chatgpt_backend_url(url: str) -> bool:
    parts = urlsplit(url)
    return parts.scheme in {"http", "https"} and parts.netloc in {
        "chatgpt.com",
        "chat.openai.com",
    }


def _guess_audio_content_type(path: Path) -> str:
    guessed, _ = mimetypes.guess_type(path.name)
    return guessed or "application/octet-stream"


def _safe_multipart_filename(path: Path) -> str:
    name = path.name or "audio"
    if path.suffix.lower() == ".oga":
        name = f"{path.stem}.ogg"
    return (
        name.replace("\\", "_").replace('"', "_").replace("\r", "_").replace("\n", "_")
    )


def _build_multipart_body(
    *,
    path: Path,
    audio_bytes: bytes,
    language_hint: str | None,
) -> tuple[bytes, str]:
    boundary = f"----CodexTelegramBoundary{secrets.token_hex(12)}"
    filename = _safe_multipart_filename(path)
    content_type = _guess_audio_content_type(path)
    chunks = [
        f"--{boundary}\r\n".encode("utf-8"),
        (
            'Content-Disposition: form-data; name="file"; ' f'filename="{filename}"\r\n'
        ).encode("utf-8"),
        f"Content-Type: {content_type}\r\n\r\n".encode("utf-8"),
        audio_bytes,
        b"\r\n",
    ]
    if language_hint:
        chunks.extend(
            [
                f"--{boundary}\r\n".encode("utf-8"),
                b'Content-Disposition: form-data; name="prompt"\r\n\r\n',
                f"Language: {language_hint}".encode("utf-8"),
                b"\r\n",
            ]
        )
    chunks.append(f"--{boundary}--\r\n".encode("utf-8"))
    return b"".join(chunks), boundary


def _post_transcribe_with_blocking_stdlib(
    *,
    url: str,
    path: Path,
    audio_bytes: bytes,
    headers: dict[str, str],
    language_hint: str | None,
    timeout_seconds: float,
) -> object:
    body, boundary = _build_multipart_body(
        path=path,
        audio_bytes=audio_bytes,
        language_hint=language_hint,
    )
    request_headers = dict(headers)
    request_headers["Content-Type"] = f"multipart/form-data; boundary={boundary}"
    request = urllib.request.Request(
        url,
        data=body,
        headers=request_headers,
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
            payload = response.read().decode("utf-8", errors="replace")
            try:
                return json.loads(payload)
            except json.JSONDecodeError:
                return payload
    except urllib.error.HTTPError as exc:
        payload = exc.read().decode("utf-8", errors="replace")
        try:
            decoded: object = json.loads(payload)
        except json.JSONDecodeError:
            decoded = payload
        if exc.code >= 400:
            raise _CodexTranscribeHttpStatusError(
                exc.code,
                _extract_error_detail(decoded),
            ) from exc
        return decoded


def _should_refresh_auth(runtime_auth: _CodexRuntimeAuth) -> bool:
    if not runtime_auth.refresh_token:
        return False
    if runtime_auth.last_refresh_at is None:
        return True
    return (_utc_now() - runtime_auth.last_refresh_at) > _TOKEN_REFRESH_INTERVAL


def _load_runtime_auth(auth_path: Path) -> _CodexRuntimeAuth:
    try:
        raw = json.loads(auth_path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise SpeechToTextError(
            f"Codex runtime auth file not found at {auth_path}."
        ) from exc
    except (OSError, json.JSONDecodeError) as exc:
        raise SpeechToTextError(
            f"Codex runtime auth file at {auth_path} is unreadable."
        ) from exc

    if not isinstance(raw, dict):
        raise SpeechToTextError(f"Codex runtime auth file at {auth_path} is invalid.")
    tokens = raw.get("tokens")
    if not isinstance(tokens, dict):
        raise SpeechToTextError(f"Codex runtime auth file at {auth_path} is invalid.")
    access_token = _get_token_value(tokens, "access_token", "accessToken")
    if access_token is None:
        raise SpeechToTextError(
            f"Codex runtime auth file at {auth_path} is missing an access token."
        )
    return _CodexRuntimeAuth(
        raw=raw,
        access_token=access_token,
        refresh_token=_get_token_value(tokens, "refresh_token", "refreshToken"),
        account_id=_get_token_value(tokens, "account_id", "accountId"),
        last_refresh_at=_parse_timestamp(
            raw.get("last_refresh") or raw.get("lastRefreshAt")
        ),
    )


async def _load_runtime_auth_async(auth_path: Path) -> _CodexRuntimeAuth:
    return await asyncio.to_thread(_load_runtime_auth, auth_path)


class OpenAISpeechToTextClient:
    """Minimal Whisper-compatible HTTP speech-to-text client."""

    def __init__(
        self,
        http_session: ClientSession,
        *,
        base_url: str,
        api_key: str | None,
        model: str,
        language_hint: str | None = None,
        request_timeout_seconds: float = 60.0,
    ) -> None:
        self._http_session = http_session
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._model = model
        self._language_hint = language_hint
        self._request_timeout_seconds = request_timeout_seconds

    async def transcribe(self, path: Path) -> str:
        """Transcribe one local audio file into plain text."""
        audio_bytes = await _read_file_bytes(path)
        form = FormData()
        form.add_field("model", self._model)
        if self._language_hint:
            form.add_field("language", self._language_hint)
        form.add_field(
            "file",
            audio_bytes,
            filename=path.name,
            content_type="application/octet-stream",
        )
        headers = (
            {"Authorization": f"Bearer {self._api_key}"} if self._api_key else None
        )
        async with self._http_session.post(
            f"{self._base_url}/audio/transcriptions",
            data=form,
            headers=headers,
            timeout=ClientTimeout(total=self._request_timeout_seconds),
        ) as response:
            payload = await _decode_response_payload(response)
            if response.status >= 400:
                detail = payload.get("error") if isinstance(payload, dict) else payload
                raise SpeechToTextError(str(detail))
        text = payload.get("text") if isinstance(payload, dict) else None
        if not isinstance(text, str) or not text.strip():
            raise SpeechToTextError("Speech-to-text returned no transcript.")
        return text.strip()


class CodexSpeechToTextClient:
    """Direct client for Codex internal ``/transcribe`` endpoint."""

    def __init__(
        self,
        http_session: ClientSession,
        *,
        base_url: str | None,
        language_hint: str | None = None,
        request_timeout_seconds: float = 60.0,
        auth_path: Path | None = None,
        config_path: Path | None = None,
        auth_base_url: str = _DEFAULT_CODEX_AUTH_BASE_URL,
        oauth_client_id: str = _DEFAULT_CODEX_OAUTH_CLIENT_ID,
        oauth_scope: str = _DEFAULT_CODEX_OAUTH_SCOPE,
        user_agent: str | None = None,
        client_id: str | None = None,
        client_version: str | None = None,
        client_os: str | None = None,
        client_arch: str | None = None,
    ) -> None:
        self._http_session = http_session
        self._language_hint = language_hint
        self._request_timeout_seconds = request_timeout_seconds
        self._auth_path = auth_path or _resolve_codex_auth_path()
        self._config_path = config_path or _resolve_codex_config_path()
        self._auth_base_url = auth_base_url.rstrip("/")
        self._oauth_client_id = oauth_client_id
        self._oauth_scope = oauth_scope
        self._base_url: str | None = (
            _normalize_chatgpt_base_url(base_url) if base_url is not None else None
        )
        self._user_agent = (
            user_agent
            or os.environ.get("CODEX_INTERNAL_ORIGINATOR_OVERRIDE")
            or _DEFAULT_CODEX_USER_AGENT
        )
        self._client_id = client_id or oauth_client_id
        self._client_version = client_version or None
        self._client_os = client_os or platform.system().lower() or None
        self._client_arch = client_arch or platform.machine().lower() or None
        self._codex_beta_features = os.environ.get(
            "CODEX_INTERNAL_BETA_FEATURES_OVERRIDE"
        )
        self._codex_beta_features_loaded = self._codex_beta_features is not None
        self._codex_turn_metadata = (
            os.environ.get("CODEX_INTERNAL_TURN_METADATA_OVERRIDE") or None
        )

    async def _transcribe_base_url(self) -> str:
        if self._base_url is None:
            self._base_url = _normalize_chatgpt_base_url(
                await _load_runtime_chatgpt_base_url_async(self._config_path)
            )
        return self._base_url

    async def _ensure_config_derived_headers_loaded(self) -> None:
        if self._codex_beta_features_loaded:
            return
        self._codex_beta_features = await _load_enabled_codex_beta_features_async(
            self._config_path
        )
        self._codex_beta_features_loaded = True

    def _build_transcribe_headers(
        self, runtime_auth: _CodexRuntimeAuth
    ) -> dict[str, str]:
        headers = {
            "Authorization": f"Bearer {runtime_auth.access_token}",
            "User-Agent": self._user_agent,
            "x-openai-client-id": self._client_id,
            "x-openai-client-user-agent": self._user_agent,
        }
        if runtime_auth.account_id:
            headers["chatgpt-account-id"] = runtime_auth.account_id
        if self._client_version:
            headers["x-openai-client-version"] = self._client_version
        if self._client_os:
            headers["x-openai-client-os"] = self._client_os
        if self._client_arch:
            headers["x-openai-client-arch"] = self._client_arch
        if self._codex_beta_features:
            headers["x-codex-beta-features"] = self._codex_beta_features
        if self._codex_turn_metadata:
            headers["x-codex-turn-metadata"] = self._codex_turn_metadata
        return headers

    async def _refresh_runtime_auth(
        self, runtime_auth: _CodexRuntimeAuth
    ) -> _CodexRuntimeAuth:
        if not runtime_auth.refresh_token:
            raise SpeechToTextError(
                "Codex runtime auth file is missing a refresh token."
            )
        async with self._http_session.post(
            f"{self._auth_base_url}/oauth/token",
            json={
                "grant_type": "refresh_token",
                "client_id": self._oauth_client_id,
                "refresh_token": runtime_auth.refresh_token,
                "scope": self._oauth_scope,
            },
            timeout=ClientTimeout(total=self._request_timeout_seconds),
        ) as response:
            payload = await _decode_response_payload(response)
            if response.status >= 400:
                raise SpeechToTextError(str(_extract_error_detail(payload)))
        if not isinstance(payload, dict):
            raise SpeechToTextError("Codex auth refresh returned an invalid payload.")
        access_token = payload.get("access_token")
        refresh_token = payload.get("refresh_token")
        id_token = payload.get("id_token")
        if not isinstance(access_token, str) or not access_token.strip():
            raise SpeechToTextError("Codex auth refresh returned no access token.")
        if not isinstance(refresh_token, str) or not refresh_token.strip():
            raise SpeechToTextError("Codex auth refresh returned no refresh token.")
        if not isinstance(id_token, str) or not id_token.strip():
            raise SpeechToTextError("Codex auth refresh returned no ID token.")
        refreshed_at = _utc_now()
        account_id = (
            _extract_account_id_from_id_token(id_token) or runtime_auth.account_id
        )
        raw_auth = _build_runtime_auth_payload(
            runtime_auth.raw,
            access_token=access_token.strip(),
            refresh_token=refresh_token.strip(),
            id_token=id_token.strip(),
            account_id=account_id,
            refreshed_at=refreshed_at,
        )
        await _persist_runtime_auth_async(self._auth_path, raw_auth)
        return _CodexRuntimeAuth(
            raw=raw_auth,
            access_token=access_token.strip(),
            refresh_token=refresh_token.strip(),
            account_id=account_id,
            last_refresh_at=refreshed_at,
        )

    async def transcribe(self, path: Path) -> str:
        """Transcribe one local audio file via the internal Codex endpoint."""
        runtime_auth = await _load_runtime_auth_async(self._auth_path)
        if _should_refresh_auth(runtime_auth):
            runtime_auth = await self._refresh_runtime_auth(runtime_auth)
        await self._ensure_config_derived_headers_loaded()
        base_url = await self._transcribe_base_url()
        audio_bytes = await _read_file_bytes(path)
        transcribe_url = f"{base_url}/transcribe"
        for attempt in range(2):
            if _is_chatgpt_backend_url(base_url):
                try:
                    payload = await asyncio.to_thread(
                        _post_transcribe_with_blocking_stdlib,
                        url=transcribe_url,
                        path=path,
                        audio_bytes=audio_bytes,
                        headers=self._build_transcribe_headers(runtime_auth),
                        language_hint=self._language_hint,
                        timeout_seconds=self._request_timeout_seconds,
                    )
                except _CodexTranscribeHttpStatusError as exc:
                    if (
                        exc.status == 401
                        and attempt == 0
                        and runtime_auth.refresh_token
                    ):
                        runtime_auth = await self._refresh_runtime_auth(runtime_auth)
                        continue
                    raise
                text = payload.get("text") if isinstance(payload, dict) else None
                if not isinstance(text, str) or not text.strip():
                    raise SpeechToTextError("Speech-to-text returned no transcript.")
                return text.strip()
            form = FormData()
            form.add_field(
                "file",
                audio_bytes,
                filename=path.name,
                content_type="application/octet-stream",
            )
            if self._language_hint:
                form.add_field("prompt", f"Language: {self._language_hint}")
            async with self._http_session.post(
                transcribe_url,
                data=form,
                headers=self._build_transcribe_headers(runtime_auth),
                timeout=ClientTimeout(total=self._request_timeout_seconds),
            ) as response:
                payload = await _decode_response_payload(response)
                if (
                    response.status == 401
                    and attempt == 0
                    and runtime_auth.refresh_token
                ):
                    runtime_auth = await self._refresh_runtime_auth(runtime_auth)
                    continue
                if response.status >= 400:
                    raise SpeechToTextError(str(_extract_error_detail(payload)))
            text = payload.get("text") if isinstance(payload, dict) else None
            if not isinstance(text, str) or not text.strip():
                raise SpeechToTextError("Speech-to-text returned no transcript.")
            return text.strip()
        raise SpeechToTextError("Speech-to-text transcription failed.")
