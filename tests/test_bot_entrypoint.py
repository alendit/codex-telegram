from pathlib import Path

import pytest

from codex_telegram.adapters.speech_to_text import (
    CodexSpeechToTextClient,
    OpenAISpeechToTextClient,
)
from codex_telegram.config import SpeechToTextConfig, TelegramConfig
from codex_telegram.entrypoints.bot import build_speech_client


class _DummySession:
    pass


def test_build_speech_client_returns_openai_client() -> None:
    client = build_speech_client(
        _DummySession(),  # type: ignore[arg-type]
        SpeechToTextConfig(
            enabled=True,
            provider="openai",
            base_url="https://stt.example",
            api_key="secret",
            model="whisper-1",
            language_hint=None,
            request_timeout_seconds=30.0,
        ),
        TelegramConfig(
            bot_token="token",
            allow_from="*",
            enable_topic_sessions=False,
            typing_refresh_seconds=4.0,
            wait_notice_seconds=180.0,
            bridge_window_ttl_seconds=900.0,
            default_language="de",
        ),
    )

    assert isinstance(client, OpenAISpeechToTextClient)


def test_build_speech_client_returns_codex_client(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    codex_home = tmp_path / "codex-home"
    monkeypatch.setenv("CODEX_HOME", str(codex_home))

    client = build_speech_client(
        _DummySession(),  # type: ignore[arg-type]
        SpeechToTextConfig(
            enabled=True,
            provider="codex",
            base_url="https://codex.example",
            api_key="ignored",
            model="ignored-model",
            language_hint=None,
            request_timeout_seconds=30.0,
        ),
        TelegramConfig(
            bot_token="token",
            allow_from="*",
            enable_topic_sessions=False,
            typing_refresh_seconds=4.0,
            wait_notice_seconds=180.0,
            bridge_window_ttl_seconds=900.0,
            default_language="de",
        ),
    )

    assert isinstance(client, CodexSpeechToTextClient)


def test_build_speech_client_allows_codex_client_without_explicit_base_url(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    codex_home = tmp_path / "codex-home"
    codex_home.mkdir()
    monkeypatch.setenv("CODEX_HOME", str(codex_home))

    client = build_speech_client(
        _DummySession(),  # type: ignore[arg-type]
        SpeechToTextConfig(
            enabled=True,
            provider="codex",
            base_url=None,
            api_key="ignored",
            model="ignored-model",
            language_hint=None,
            request_timeout_seconds=30.0,
        ),
        TelegramConfig(
            bot_token="token",
            allow_from="*",
            enable_topic_sessions=False,
            typing_refresh_seconds=4.0,
            wait_notice_seconds=180.0,
            bridge_window_ttl_seconds=900.0,
            default_language="de",
        ),
    )

    assert isinstance(client, CodexSpeechToTextClient)


def test_build_speech_client_rejects_unknown_provider() -> None:
    with pytest.raises(ValueError, match="Unknown speech-to-text provider"):
        build_speech_client(
            _DummySession(),  # type: ignore[arg-type]
            SpeechToTextConfig(
                enabled=True,
                provider="bogus",
                base_url="https://stt.example",
                api_key=None,
                model="ignored-model",
                language_hint=None,
                request_timeout_seconds=30.0,
            ),
            TelegramConfig(
                bot_token="token",
                allow_from="*",
                enable_topic_sessions=False,
                typing_refresh_seconds=4.0,
                wait_notice_seconds=180.0,
                bridge_window_ttl_seconds=900.0,
                default_language="de",
            ),
        )
