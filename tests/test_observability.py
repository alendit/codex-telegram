import json
import logging

from codex_telegram.observability import (
    clear_log_context,
    configure_logging,
    drop_none_log_fields,
    get_logger,
    log_context,
    log_exception,
    log_info,
)


def test_drop_none_log_fields_removes_null_values_recursively() -> None:
    event_dict = {
        "k": "telegram_message_received",
        "turn_id": None,
        "v": {
            "text_length": 12,
            "error": None,
            "nested": {
                "keep": "value",
                "drop": None,
            },
        },
    }

    cleaned = drop_none_log_fields(logging.getLogger(__name__), "info", event_dict)

    assert cleaned == {
        "k": "telegram_message_received",
        "v": {
            "text_length": 12,
            "nested": {"keep": "value"},
        },
    }


def test_configure_logging_emits_structured_json(capsys) -> None:
    configure_logging("INFO")
    logger = get_logger("test.observability")

    clear_log_context()
    with log_context(chat_key="chat:1"):
        log_info(
            logger,
            "telegram_message_received",
            turn_id="turn-1",
            v={"text_length": 4, "ignored": None},
        )
    clear_log_context()

    captured = capsys.readouterr().out.strip()
    record = json.loads(captured)

    assert record["k"] == "telegram_message_received"
    assert record["sev"] == "INFO"
    assert record["chat_key"] == "chat:1"
    assert record["turn_id"] == "turn-1"
    assert record["logger"] == "test.observability"
    assert isinstance(record["pid"], int)
    assert isinstance(record["threadid"], int)
    assert "ts" in record
    assert record["v"] == {"text_length": 4}
    assert "event" not in record
    assert "msg" not in record


def test_log_info_without_payload_keeps_empty_object(capsys) -> None:
    configure_logging("INFO")
    logger = get_logger("test.observability")

    log_info(logger, "codex_app_server_client_closing")

    captured = capsys.readouterr().out.strip()
    record = json.loads(captured)

    assert record["k"] == "codex_app_server_client_closing"
    assert record["v"] == {}


def test_configure_logging_handles_stdlib_records(capsys) -> None:
    configure_logging("INFO")
    logger = logging.getLogger("test.stdlib")

    logger.info("Connection established %s", "again")

    captured = capsys.readouterr().out.strip()
    record = json.loads(captured)

    assert record["k"] == "stdlib_log"
    assert record["sev"] == "INFO"
    assert record["logger"] == "test.stdlib"
    assert record["v"] == {"text": "Connection established again"}


def test_log_exception_emits_structured_error_payload(capsys) -> None:
    configure_logging("INFO")
    logger = get_logger("test.observability")

    try:
        raise ValueError("boom")
    except ValueError as err:
        log_exception(
            logger,
            "telegram_message_handling_failed",
            err=err,
            request_id=7,
            v={"phase": "dispatch"},
        )

    captured = capsys.readouterr().out.strip()
    record = json.loads(captured)

    assert record["k"] == "telegram_message_handling_failed"
    assert record["request_id"] == 7
    assert record["v"]["phase"] == "dispatch"
    assert record["v"]["error"]["type"] == "ValueError"
    assert record["v"]["error"]["module"] == "builtins"
    assert record["v"]["error"]["message"] == "boom"
    assert record["v"]["error"]["frames"]
