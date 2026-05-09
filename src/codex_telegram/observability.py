"""Structured logging configuration and helpers."""

from __future__ import annotations

from collections.abc import Callable, Iterator, Mapping, MutableMapping, Sequence
from contextlib import contextmanager
import logging
import os
import sys
import threading
import traceback
from typing import Any

import structlog
from structlog.typing import EventDict

Processor = Callable[
    [Any, str, MutableMapping[str, Any]],
    Mapping[str, Any] | str | bytes | bytearray | tuple[Any, ...],
]

_RESERVED_LOG_KEYS = {
    "event",
    "timestamp",
    "level",
    "logger",
    "v",
    "k",
    "_record",
    "_from_structlog",
    "_codex_foreign_log",
    "exc_info",
    "stack",
}


def _drop_none(value: object) -> object:
    if isinstance(value, Mapping):
        return {
            key: cleaned
            for key, nested in value.items()
            if (cleaned := _drop_none(nested)) is not None
        }
    if isinstance(value, Sequence) and not isinstance(value, str | bytes | bytearray):
        return [
            cleaned for nested in value if (cleaned := _drop_none(nested)) is not None
        ]
    return value


def drop_none_log_fields(
    _logger: logging.Logger, _method_name: str, event_dict: EventDict
) -> EventDict:
    """Remove null values recursively so payloads stay compact and consistent."""
    cleaned = _drop_none(event_dict)
    return dict(cleaned) if isinstance(cleaned, Mapping) else event_dict


def _add_runtime_metadata(
    _logger: logging.Logger, _method_name: str, event_dict: EventDict
) -> EventDict:
    event_dict.setdefault("pid", os.getpid())
    event_dict.setdefault("threadid", threading.get_native_id())
    return event_dict


def _mark_foreign_log(
    _logger: logging.Logger, _method_name: str, event_dict: EventDict
) -> EventDict:
    """Mark stdlib-originated records before ProcessorFormatter drops metadata."""
    if event_dict.get("_from_structlog") is False:
        event_dict["_codex_foreign_log"] = True
    return event_dict


def _coerce_log_value(value: object) -> dict[str, object]:
    if value is None:
        return {}
    if isinstance(value, Mapping):
        return dict(value)
    return {"value": value}


def _serialize_exception(err: BaseException) -> dict[str, object]:
    return {
        "type": type(err).__name__,
        "module": type(err).__module__,
        "message": str(err),
        "frames": [
            {
                "file": frame.filename,
                "line": frame.lineno,
                "func": frame.name,
                "code": frame.line,
            }
            for frame in traceback.extract_tb(err.__traceback__)
        ],
    }


def _normalize_log_schema(
    _logger: logging.Logger, _method_name: str, event_dict: EventDict
) -> EventDict:
    had_structured_event = "k" in event_dict
    had_explicit_value = "v" in event_dict
    is_foreign_log = bool(event_dict.pop("_codex_foreign_log", False))
    event_name = event_dict.pop("k", None) or event_dict.pop("event", None) or "log"
    value = _coerce_log_value(event_dict.pop("v", None))

    exc_info = event_dict.pop("exc_info", None)
    if exc_info:
        current_err = (
            exc_info if isinstance(exc_info, BaseException) else sys.exc_info()[1]
        )
        if isinstance(current_err, BaseException):
            value.setdefault("error", _serialize_exception(current_err))

    stack = event_dict.pop("stack", None)
    if stack is not None:
        value.setdefault("stack", {"text": stack})

    if (
        not value
        and not had_structured_event
        and not had_explicit_value
        and isinstance(event_name, str)
    ):
        value["text"] = event_name
        if is_foreign_log:
            event_name = "stdlib_log"

    normalized: EventDict = {
        "ts": event_dict.pop("timestamp", None),
        "sev": str(event_dict.pop("level", _method_name)).upper(),
        "pid": event_dict.pop("pid", None),
        "threadid": event_dict.pop("threadid", None),
        "logger": event_dict.pop("logger", None),
        "k": str(event_name),
        "v": value,
    }

    for key, val in list(event_dict.items()):
        if key not in _RESERVED_LOG_KEYS:
            normalized[key] = val
    return normalized


def _log_event(
    logger: Any,
    level: str,
    event_name: str,
    *,
    v: Mapping[str, object] | None = None,
    **metadata: object,
) -> None:
    getattr(logger, level)(event_name, k=event_name, v=_coerce_log_value(v), **metadata)


def log_debug(
    logger: Any,
    event_name: str,
    *,
    v: Mapping[str, object] | None = None,
    **metadata: object,
) -> None:
    """Emit one structured debug log event."""
    _log_event(logger, "debug", event_name, v=v, **metadata)


def log_info(
    logger: Any,
    event_name: str,
    *,
    v: Mapping[str, object] | None = None,
    **metadata: object,
) -> None:
    """Emit one structured info log event."""
    _log_event(logger, "info", event_name, v=v, **metadata)


def log_warning(
    logger: Any,
    event_name: str,
    *,
    v: Mapping[str, object] | None = None,
    **metadata: object,
) -> None:
    """Emit one structured warning log event."""
    _log_event(logger, "warning", event_name, v=v, **metadata)


def log_error(
    logger: Any,
    event_name: str,
    *,
    v: Mapping[str, object] | None = None,
    **metadata: object,
) -> None:
    """Emit one structured error log event."""
    _log_event(logger, "error", event_name, v=v, **metadata)


def log_exception(
    logger: Any,
    event_name: str,
    *,
    err: BaseException | None = None,
    level: str = "error",
    v: Mapping[str, object] | None = None,
    **metadata: object,
) -> None:
    """Emit one structured exception log event."""
    current_err = err if err is not None else sys.exc_info()[1]
    value = _coerce_log_value(v)
    if isinstance(current_err, BaseException):
        value["error"] = _serialize_exception(current_err)
    _log_event(logger, level, event_name, v=value, **metadata)


def configure_logging(level_name: str = "INFO") -> None:
    """Configure structlog and stdlib logging for JSON output."""
    level = getattr(logging, level_name.upper(), logging.INFO)
    timestamper = structlog.processors.TimeStamper(fmt="iso", utc=True)
    shared_processors: list[Processor] = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_logger_name,
        structlog.stdlib.add_log_level,
        timestamper,
        _add_runtime_metadata,
        structlog.processors.StackInfoRenderer(),
    ]
    formatter = structlog.stdlib.ProcessorFormatter(
        foreign_pre_chain=shared_processors,
        processors=[
            _mark_foreign_log,
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            _normalize_log_schema,
            drop_none_log_fields,
            structlog.processors.JSONRenderer(),
        ],
    )

    root_logger = logging.getLogger()
    root_logger.handlers.clear()
    root_logger.setLevel(level)
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(formatter)
    root_logger.addHandler(handler)

    structlog.configure(
        processors=[
            *shared_processors,
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )


def get_logger(name: str):
    """Return a structlog logger."""
    return structlog.get_logger(name)


def bind_log_context(**fields: object) -> None:
    """Bind contextual fields to the current execution context."""
    structlog.contextvars.bind_contextvars(
        **{key: value for key, value in fields.items() if value is not None}
    )


def clear_log_context() -> None:
    """Clear all bound contextual fields."""
    structlog.contextvars.clear_contextvars()


@contextmanager
def log_context(**fields: object) -> Iterator[None]:
    """Temporarily bind contextual fields for nested log events."""
    with structlog.contextvars.bound_contextvars(
        **{key: value for key, value in fields.items() if value is not None}
    ):
        yield
