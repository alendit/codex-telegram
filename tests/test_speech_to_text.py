import base64
import json
from pathlib import Path
from typing import Any

import pytest

import codex_telegram.adapters.speech_to_text.client as speech_client_module
from codex_telegram.adapters.speech_to_text import (
    CodexSpeechToTextClient,
    OpenAISpeechToTextClient,
    SpeechToTextError,
)


class _FakeResponse:
    def __init__(
        self,
        status: int,
        payload: dict[str, object] | str,
        *,
        raise_on_json: bool = False,
    ) -> None:
        self.status = status
        self._payload = payload
        self._raise_on_json = raise_on_json

    async def __aenter__(self) -> "_FakeResponse":
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        return None

    async def json(self, content_type=None):
        if self._raise_on_json:
            raise ValueError("unexpected mimetype")
        return self._payload

    async def text(self) -> str:
        if isinstance(self._payload, str):
            return self._payload
        return str(self._payload)


class _FakeSession:
    def __init__(self, response: _FakeResponse | list[_FakeResponse]) -> None:
        if isinstance(response, list):
            self._responses = response
        else:
            self._responses = [response]
        self.calls: list[tuple[tuple[object, ...], dict[str, Any]]] = []

    def post(self, *args, **kwargs) -> _FakeResponse:
        self.calls.append((args, kwargs))
        index = min(len(self.calls) - 1, len(self._responses) - 1)
        return self._responses[index]


class _FakeStdlibTranscribe:
    def __init__(self, payload: dict[str, object] | str | None = None) -> None:
        self.payload = payload or {"text": "hello codex"}
        self.calls: list[dict[str, object]] = []

    def __call__(self, **kwargs) -> dict[str, object] | str:
        self.calls.append(kwargs)
        return self.payload


def _encode_jwt(payload: dict[str, object]) -> str:
    raw = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    body = base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")
    return f"header.{body}.sig"


def test_codex_multipart_renames_telegram_oga_voice_to_ogg(tmp_path: Path) -> None:
    audio_path = tmp_path / "voice.oga"
    audio_path.write_bytes(b"audio")

    body, _ = speech_client_module._build_multipart_body(
        path=audio_path,
        audio_bytes=audio_path.read_bytes(),
        language_hint=None,
    )

    assert b'filename="voice.ogg"' in body
    assert b"Content-Type: audio/ogg" in body


def test_codex_error_detail_uses_detail_payload() -> None:
    assert (
        speech_client_module._extract_error_detail({"detail": "Error in ASR API"})
        == "Error in ASR API"
    )


@pytest.mark.asyncio
async def test_openai_speech_client_returns_transcript(tmp_path: Path) -> None:
    audio_path = tmp_path / "voice.ogg"
    audio_path.write_bytes(b"audio")
    client = OpenAISpeechToTextClient(
        _FakeSession(_FakeResponse(200, {"text": "hello world"})),  # type: ignore[arg-type]
        base_url="https://stt.example",
        api_key="secret",
        model="whisper-1",
    )

    assert await client.transcribe(audio_path) == "hello world"


@pytest.mark.asyncio
async def test_openai_speech_client_raises_for_error_payload(tmp_path: Path) -> None:
    audio_path = tmp_path / "voice.ogg"
    audio_path.write_bytes(b"audio")
    client = OpenAISpeechToTextClient(
        _FakeSession(_FakeResponse(400, {"error": "bad request"})),  # type: ignore[arg-type]
        base_url="https://stt.example",
        api_key="secret",
        model="whisper-1",
    )

    with pytest.raises(SpeechToTextError):
        await client.transcribe(audio_path)


@pytest.mark.asyncio
async def test_codex_speech_client_returns_transcript_from_runtime_auth(
    tmp_path: Path,
) -> None:
    audio_path = tmp_path / "voice.ogg"
    audio_path.write_bytes(b"audio")
    auth_path = tmp_path / "auth.json"
    auth_path.write_text(
        '{"tokens":{"accessToken":"access-token","accountId":"account-123"}}',
        encoding="utf-8",
    )
    session = _FakeSession(_FakeResponse(200, {"text": "hello codex"}))
    client = CodexSpeechToTextClient(
        session,  # type: ignore[arg-type]
        base_url="https://codex.example",
        auth_path=auth_path,
        config_path=tmp_path / "missing-config.toml",
        user_agent="Codex Desktop/0.122.0-alpha.1",
        client_id="app_EMoamEEZ73f0CkXaXp7hrann",
        client_version="0.122.0-alpha.1",
        client_os="linux",
        client_arch="arm64",
    )

    assert await client.transcribe(audio_path) == "hello codex"
    _, kwargs = session.calls[0]
    assert kwargs["headers"] == {
        "Authorization": "Bearer access-token",
        "chatgpt-account-id": "account-123",
        "User-Agent": "Codex Desktop/0.122.0-alpha.1",
        "x-openai-client-id": "app_EMoamEEZ73f0CkXaXp7hrann",
        "x-openai-client-version": "0.122.0-alpha.1",
        "x-openai-client-os": "linux",
        "x-openai-client-arch": "arm64",
        "x-openai-client-user-agent": "Codex Desktop/0.122.0-alpha.1",
    }
    assert session.calls[0][0][0] == "https://codex.example/transcribe"


@pytest.mark.asyncio
async def test_codex_speech_client_accepts_snake_case_runtime_auth(
    tmp_path: Path,
) -> None:
    audio_path = tmp_path / "voice.ogg"
    audio_path.write_bytes(b"audio")
    auth_path = tmp_path / "auth.json"
    auth_path.write_text(
        '{"tokens":{"access_token":"access-token","account_id":"account-123"}}',
        encoding="utf-8",
    )
    session = _FakeSession(_FakeResponse(200, {"text": "hello codex"}))
    client = CodexSpeechToTextClient(
        session,  # type: ignore[arg-type]
        base_url="https://codex.example",
        auth_path=auth_path,
        config_path=tmp_path / "missing-config.toml",
        user_agent="Codex Desktop/0.122.0-alpha.1",
        client_id="app_EMoamEEZ73f0CkXaXp7hrann",
        client_version="0.122.0-alpha.1",
        client_os="linux",
        client_arch="arm64",
    )

    assert await client.transcribe(audio_path) == "hello codex"
    _, kwargs = session.calls[0]
    assert kwargs["headers"] == {
        "Authorization": "Bearer access-token",
        "chatgpt-account-id": "account-123",
        "User-Agent": "Codex Desktop/0.122.0-alpha.1",
        "x-openai-client-id": "app_EMoamEEZ73f0CkXaXp7hrann",
        "x-openai-client-version": "0.122.0-alpha.1",
        "x-openai-client-os": "linux",
        "x-openai-client-arch": "arm64",
        "x-openai-client-user-agent": "Codex Desktop/0.122.0-alpha.1",
    }


@pytest.mark.asyncio
async def test_codex_speech_client_defaults_to_runtime_chatgpt_backend_url(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    audio_path = tmp_path / "voice.ogg"
    audio_path.write_bytes(b"audio")
    auth_path = tmp_path / "auth.json"
    auth_path.write_text(
        '{"tokens":{"accessToken":"access-token","accountId":"account-123"}}',
        encoding="utf-8",
    )
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        'chatgpt_base_url = "https://chat.openai.com"\n',
        encoding="utf-8",
    )
    stdlib_transcribe = _FakeStdlibTranscribe()
    monkeypatch.setattr(
        speech_client_module,
        "_post_transcribe_with_blocking_stdlib",
        stdlib_transcribe,
    )
    session = _FakeSession(_FakeResponse(200, {"text": "unused"}))
    client = CodexSpeechToTextClient(
        session,  # type: ignore[arg-type]
        base_url=None,
        auth_path=auth_path,
        config_path=config_path,
    )

    assert await client.transcribe(audio_path) == "hello codex"
    assert session.calls == []
    assert (
        stdlib_transcribe.calls[0]["url"]
        == "https://chat.openai.com/backend-api/transcribe"
    )


@pytest.mark.asyncio
async def test_codex_speech_client_uses_default_chatgpt_backend_url(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    audio_path = tmp_path / "voice.ogg"
    audio_path.write_bytes(b"audio")
    auth_path = tmp_path / "auth.json"
    auth_path.write_text(
        '{"tokens":{"accessToken":"access-token"}}',
        encoding="utf-8",
    )
    stdlib_transcribe = _FakeStdlibTranscribe()
    monkeypatch.setattr(
        speech_client_module,
        "_post_transcribe_with_blocking_stdlib",
        stdlib_transcribe,
    )
    session = _FakeSession(_FakeResponse(200, {"text": "unused"}))
    client = CodexSpeechToTextClient(
        session,  # type: ignore[arg-type]
        base_url=None,
        auth_path=auth_path,
        config_path=tmp_path / "missing-config.toml",
    )

    assert await client.transcribe(audio_path) == "hello codex"
    assert session.calls == []
    assert (
        stdlib_transcribe.calls[0]["url"]
        == "https://chatgpt.com/backend-api/transcribe"
    )


@pytest.mark.asyncio
async def test_codex_speech_client_uses_prompt_for_language_hint(
    tmp_path: Path,
) -> None:
    audio_path = tmp_path / "voice.ogg"
    audio_path.write_bytes(b"audio")
    auth_path = tmp_path / "auth.json"
    auth_path.write_text(
        '{"tokens":{"accessToken":"access-token"}}',
        encoding="utf-8",
    )
    session = _FakeSession(_FakeResponse(200, {"text": "hello codex"}))
    client = CodexSpeechToTextClient(
        session,  # type: ignore[arg-type]
        base_url="https://codex.example",
        auth_path=auth_path,
        config_path=tmp_path / "missing-config.toml",
        language_hint="de",
        user_agent="Codex Desktop/0.122.0-alpha.1",
        client_id="app_EMoamEEZ73f0CkXaXp7hrann",
        client_version="0.122.0-alpha.1",
        client_os="linux",
        client_arch="arm64",
    )

    await client.transcribe(audio_path)

    _, kwargs = session.calls[0]
    assert kwargs["headers"] == {
        "Authorization": "Bearer access-token",
        "User-Agent": "Codex Desktop/0.122.0-alpha.1",
        "x-openai-client-id": "app_EMoamEEZ73f0CkXaXp7hrann",
        "x-openai-client-version": "0.122.0-alpha.1",
        "x-openai-client-os": "linux",
        "x-openai-client-arch": "arm64",
        "x-openai-client-user-agent": "Codex Desktop/0.122.0-alpha.1",
    }
    form = kwargs["data"]
    fields = getattr(form, "_fields")
    prompt_field = next(field for field in fields if field[0]["name"] == "prompt")
    assert prompt_field[2] == "Language: de"


@pytest.mark.asyncio
async def test_codex_speech_client_includes_x_codex_headers_from_config_and_override(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    audio_path = tmp_path / "voice.ogg"
    audio_path.write_bytes(b"audio")
    auth_path = tmp_path / "auth.json"
    auth_path.write_text(
        '{"tokens":{"accessToken":"access-token","accountId":"account-123"}}',
        encoding="utf-8",
    )
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        "[features]\nmemories = true\nprevent_idle_sleep = true\nresponses_websockets_v2 = false\n",
        encoding="utf-8",
    )
    monkeypatch.setenv(
        "CODEX_INTERNAL_TURN_METADATA_OVERRIDE",
        '{"turn_id":"","sandbox":"workspace-write"}',
    )
    session = _FakeSession(_FakeResponse(200, {"text": "hello codex"}))
    client = CodexSpeechToTextClient(
        session,  # type: ignore[arg-type]
        base_url="https://codex.example",
        auth_path=auth_path,
        config_path=config_path,
        user_agent="Codex Desktop/0.122.0-alpha.1",
        client_id="app_EMoamEEZ73f0CkXaXp7hrann",
        client_version="0.122.0-alpha.1",
        client_os="linux",
        client_arch="arm64",
    )

    assert await client.transcribe(audio_path) == "hello codex"
    _, kwargs = session.calls[0]
    assert kwargs["headers"] == {
        "Authorization": "Bearer access-token",
        "chatgpt-account-id": "account-123",
        "User-Agent": "Codex Desktop/0.122.0-alpha.1",
        "x-openai-client-id": "app_EMoamEEZ73f0CkXaXp7hrann",
        "x-openai-client-version": "0.122.0-alpha.1",
        "x-openai-client-os": "linux",
        "x-openai-client-arch": "arm64",
        "x-openai-client-user-agent": "Codex Desktop/0.122.0-alpha.1",
        "x-codex-beta-features": "memories,prevent_idle_sleep",
        "x-codex-turn-metadata": '{"turn_id":"","sandbox":"workspace-write"}',
    }


@pytest.mark.asyncio
async def test_codex_speech_client_refreshes_runtime_auth_before_transcribe(
    tmp_path: Path,
) -> None:
    audio_path = tmp_path / "voice.ogg"
    audio_path.write_bytes(b"audio")
    auth_path = tmp_path / "auth.json"
    auth_path.write_text(
        json.dumps(
            {
                "tokens": {
                    "access_token": "stale-access-token",
                    "refresh_token": "refresh-token",
                    "account_id": "account-123",
                },
                "last_refresh": "2026-04-01T00:00:00Z",
            }
        ),
        encoding="utf-8",
    )
    refreshed_id_token = _encode_jwt(
        {
            "https://api.openai.com/auth": {
                "chatgpt_account_id": "account-456",
            }
        }
    )
    session = _FakeSession(
        [
            _FakeResponse(
                200,
                {
                    "access_token": "fresh-access-token",
                    "refresh_token": "fresh-refresh-token",
                    "id_token": refreshed_id_token,
                },
            ),
            _FakeResponse(200, {"text": "hello refreshed"}),
        ]
    )
    client = CodexSpeechToTextClient(
        session,  # type: ignore[arg-type]
        base_url="https://codex.example",
        auth_path=auth_path,
        config_path=tmp_path / "missing-config.toml",
        user_agent="Codex Desktop/0.122.0-alpha.1",
        client_id="app_EMoamEEZ73f0CkXaXp7hrann",
        client_version="0.122.0-alpha.1",
        client_os="linux",
        client_arch="arm64",
    )

    assert await client.transcribe(audio_path) == "hello refreshed"
    assert session.calls[0][0][0] == "https://auth.openai.com/oauth/token"
    assert session.calls[0][1]["json"] == {
        "grant_type": "refresh_token",
        "client_id": "app_EMoamEEZ73f0CkXaXp7hrann",
        "refresh_token": "refresh-token",
        "scope": "openid profile email",
    }
    assert session.calls[1][0][0] == "https://codex.example/transcribe"
    assert session.calls[1][1]["headers"] == {
        "Authorization": "Bearer fresh-access-token",
        "chatgpt-account-id": "account-456",
        "User-Agent": "Codex Desktop/0.122.0-alpha.1",
        "x-openai-client-id": "app_EMoamEEZ73f0CkXaXp7hrann",
        "x-openai-client-version": "0.122.0-alpha.1",
        "x-openai-client-os": "linux",
        "x-openai-client-arch": "arm64",
        "x-openai-client-user-agent": "Codex Desktop/0.122.0-alpha.1",
    }
    refreshed_auth = json.loads(auth_path.read_text(encoding="utf-8"))
    assert refreshed_auth["tokens"]["access_token"] == "fresh-access-token"
    assert refreshed_auth["tokens"]["refresh_token"] == "fresh-refresh-token"
    assert refreshed_auth["tokens"]["account_id"] == "account-456"
    assert refreshed_auth["last_refresh"]


@pytest.mark.asyncio
async def test_codex_speech_client_refreshes_and_retries_after_401(
    tmp_path: Path,
) -> None:
    audio_path = tmp_path / "voice.ogg"
    audio_path.write_bytes(b"audio")
    auth_path = tmp_path / "auth.json"
    auth_path.write_text(
        json.dumps(
            {
                "tokens": {
                    "access_token": "stale-access-token",
                    "refresh_token": "refresh-token",
                    "account_id": "account-123",
                },
                "last_refresh": "2999-01-01T00:00:00Z",
            }
        ),
        encoding="utf-8",
    )
    refreshed_id_token = _encode_jwt(
        {
            "https://api.openai.com/auth": {
                "chatgpt_account_id": "account-456",
            }
        }
    )
    session = _FakeSession(
        [
            _FakeResponse(401, {"error": {"message": "expired"}}),
            _FakeResponse(
                200,
                {
                    "access_token": "fresh-access-token",
                    "refresh_token": "fresh-refresh-token",
                    "id_token": refreshed_id_token,
                },
            ),
            _FakeResponse(200, {"text": "hello retried"}),
        ]
    )
    client = CodexSpeechToTextClient(
        session,  # type: ignore[arg-type]
        base_url="https://codex.example",
        auth_path=auth_path,
        user_agent="Codex Desktop/0.122.0-alpha.1",
        client_id="app_EMoamEEZ73f0CkXaXp7hrann",
        client_version="0.122.0-alpha.1",
        client_os="linux",
        client_arch="arm64",
    )

    assert await client.transcribe(audio_path) == "hello retried"
    assert session.calls[0][0][0] == "https://codex.example/transcribe"
    assert session.calls[1][0][0] == "https://auth.openai.com/oauth/token"
    assert session.calls[2][0][0] == "https://codex.example/transcribe"
    assert (
        session.calls[2][1]["headers"]["Authorization"] == "Bearer fresh-access-token"
    )


@pytest.mark.asyncio
async def test_codex_speech_client_raises_for_missing_runtime_auth(
    tmp_path: Path,
) -> None:
    audio_path = tmp_path / "voice.ogg"
    audio_path.write_bytes(b"audio")
    client = CodexSpeechToTextClient(
        _FakeSession(_FakeResponse(200, {"text": "unused"})),  # type: ignore[arg-type]
        base_url="https://codex.example",
        auth_path=tmp_path / "missing-auth.json",
    )

    with pytest.raises(SpeechToTextError, match="auth"):
        await client.transcribe(audio_path)


@pytest.mark.asyncio
async def test_codex_speech_client_raises_for_error_payload(tmp_path: Path) -> None:
    audio_path = tmp_path / "voice.ogg"
    audio_path.write_bytes(b"audio")
    auth_path = tmp_path / "auth.json"
    auth_path.write_text(
        '{"tokens":{"accessToken":"access-token"}}',
        encoding="utf-8",
    )
    client = CodexSpeechToTextClient(
        _FakeSession(_FakeResponse(401, {"error": {"message": "Unauthorized"}})),  # type: ignore[arg-type]
        base_url="https://codex.example",
        auth_path=auth_path,
    )

    with pytest.raises(SpeechToTextError, match="Unauthorized"):
        await client.transcribe(audio_path)


@pytest.mark.asyncio
async def test_codex_speech_client_surfaces_plain_text_error_payload(
    tmp_path: Path,
) -> None:
    audio_path = tmp_path / "voice.ogg"
    audio_path.write_bytes(b"audio")
    auth_path = tmp_path / "auth.json"
    auth_path.write_text(
        '{"tokens":{"accessToken":"access-token"}}',
        encoding="utf-8",
    )
    client = CodexSpeechToTextClient(
        _FakeSession(
            _FakeResponse(
                405,
                "Request method must be `GET`",
                raise_on_json=True,
            )
        ),  # type: ignore[arg-type]
        base_url="https://codex.example",
        auth_path=auth_path,
    )

    with pytest.raises(SpeechToTextError, match="Request method must be `GET`"):
        await client.transcribe(audio_path)
