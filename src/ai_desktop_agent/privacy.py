"""Keep configured credentials out of model text, records, and application logs."""

import base64
import json
import logging
import os
import re
import threading
import traceback
from functools import lru_cache
from urllib.parse import parse_qsl, quote, quote_plus, unquote, urlsplit

REDACTED = "[REDACTED]"
_registered: frozenset[str] = frozenset()
_lock = threading.Lock()
_credential_name = re.compile(
    r"(?:^|_)(?:API_KEY|API_TOKEN|ACCESS_TOKEN|SECRET_ACCESS_KEY|SECRET_KEY|SECRET|PASSWORD|TOKEN)$"
)
_userinfo = re.compile(r"(https?://)[^/\s:@]+:[^/\s@]+@", re.IGNORECASE)
_bearer = re.compile(r"\b(Bearer|Basic)\s+[A-Za-z0-9+/_.=~-]{8,}", re.IGNORECASE)
_query = re.compile(r"([?&](?:api[_-]?key|access_token|token|secret|password)=)[^&\s\"']+", re.I)
_cookie = re.compile(r"(ai_desktop_agent_session=)[A-Za-z0-9_-]{8,}")


def protect(value: str) -> None:
    global _registered
    if len(value) >= 8:
        with _lock:
            if value not in _registered:
                _registered = _registered | {value}


def unprotect(value: str) -> None:
    global _registered
    with _lock:
        _registered = _registered - {value}


def _url_values(value: str) -> list[str]:
    try:
        parsed = urlsplit(value)
        values = [unquote(parsed.password)] if parsed.password else []
        values.extend(
            v
            for k, v in parse_qsl(parsed.query)
            if k.lower()
            in {"key", "api_key", "api-key", "token", "access_token", "password", "secret"}
        )
        return [v for v in values if len(v) >= 8]
    except ValueError:
        return []


def protect_url(value: str) -> None:
    for item in _url_values(value):
        protect(item)


@lru_cache(maxsize=16)
def _pattern(values: tuple[str, ...]):
    variants = set()
    for value in values:
        variants.update(
            (
                value,
                json.dumps(value, ensure_ascii=True)[1:-1],
                json.dumps(value, ensure_ascii=False)[1:-1],
                repr(value)[1:-1],
                quote(value, safe="", errors="surrogatepass"),
                quote_plus(value, safe="", errors="surrogatepass"),
                base64.b64encode(value.encode("utf-8", errors="surrogatepass")).decode(),
            )
        )
    return (
        re.compile("|".join(re.escape(v) for v in sorted(variants, key=len, reverse=True)))
        if variants
        else None
    )


@lru_cache(maxsize=16)
def _credential_pattern(registered, environment):
    values = set(registered)
    for name, value in environment:
        if _credential_name.search(name) and len(value) >= 8:
            values.add(value)
        if name.endswith("_URL"):
            values.update(_url_values(value))
    return _pattern(tuple(sorted(values)))


def _current_pattern():
    with _lock:
        registered = _registered
    environment = tuple(
        (name, value)
        for name, value in os.environ.items()
        if _credential_name.search(name) or name.endswith("_URL")
    )
    return _credential_pattern(registered, environment)


def _redact_with_pattern(text, pattern):
    if pattern is not None:
        text = pattern.sub(lambda _: REDACTED, text)
    text = _userinfo.sub(lambda m: m[1] + REDACTED + "@", text)
    text = _bearer.sub(lambda m: m[1] + " " + REDACTED, text)
    text = _query.sub(lambda m: m[1] + REDACTED, text)
    return _cookie.sub(lambda m: m[1] + REDACTED, text)


def redact(text: str) -> str:
    return _redact_with_pattern(text, _current_pattern())


def redact_data(value, *, skip_images=False):
    if isinstance(value, str):
        if skip_images and value.startswith("data:image/"):
            return value
        return redact(value)
    if isinstance(value, dict):
        return {key: redact_data(item, skip_images=skip_images) for key, item in value.items()}
    if isinstance(value, list):
        return [redact_data(item, skip_images=skip_images) for item in value]
    return value


class _RedactedLogMessage(str):
    # LogRecord.getMessage calls str(msg) before applying %. Keep the formatting
    # result redacted even for sinks that call getMessage without a formatter.
    def __str__(self):
        return self

    def __mod__(self, values):
        return redact(super().__mod__(values))


class _RedactingFormatter(logging.Formatter):
    def __init__(self, original):
        super().__init__()
        self.original = original or logging.Formatter()

    def format(self, record):
        try:
            # Logger.makeRecord adds extra fields after the record factory runs.
            _redact_log_record(record)
            rendered = self.original.format(record)
            # A formatter can assemble a credential from individually safe parts.
            record.message = redact(record.getMessage())
            return redact(rendered)
        except Exception:
            # Formatting failures must not expose raw arguments through handleError
            # or interrupt the application when a handler propagates exceptions.
            record.msg = record.message = "Unable to format log message"
            record.args = ()
            record.exc_info = record.exc_text = record.stack_info = None
            return record.msg


def _redact_log_value(value, pattern):
    if isinstance(value, str):
        return _redact_with_pattern(value, pattern)
    if isinstance(value, dict):
        return {key: _redact_log_value(item, pattern) for key, item in value.items()}
    if isinstance(value, tuple):
        return tuple(_redact_log_value(item, pattern) for item in value)
    if isinstance(value, list):
        return [_redact_log_value(item, pattern) for item in value]
    text = str(value)
    safe = _redact_with_pattern(text, pattern)
    if safe != text or _redact_with_pattern(repr(value), pattern) != repr(value):
        return safe
    return value


def _redact_log_record(record):
    try:
        pattern = _current_pattern()
        if record.exc_info:
            record.exc_text = _redact_with_pattern(
                "".join(traceback.format_exception(*record.exc_info)), pattern
            )
            record.exc_info = None
        for name, value in vars(record).items():
            setattr(record, name, _redact_log_value(value, pattern))
        record.msg = _RedactedLogMessage(_redact_with_pattern(str(record.msg), pattern))
        record.message = record.getMessage()
    except Exception:
        record.msg = record.message = "Unable to format log message"
        record.args = ()
        record.exc_info = record.exc_text = record.stack_info = None
    return True


def _wrap_log_handlers(logger):
    while logger is not None:
        for handler in logger.handlers:
            # Some structured sinks serialize records without calling format().
            handler.addFilter(_redact_log_record)
            if not isinstance(handler.formatter, _RedactingFormatter):
                handler.setFormatter(_RedactingFormatter(handler.formatter))
        if not logger.propagate:
            break
        logger = logger.parent


def install_log_redaction() -> None:
    _wrap_log_handlers(logging.getLogger())
    previous = logging.getLogRecordFactory()
    if getattr(previous, "_agent_redaction", False):
        return

    def factory(*args, **kwargs):
        record = previous(*args, **kwargs)
        # Uvicorn and plugins may install their handlers after application setup.
        _wrap_log_handlers(logging.getLogger(record.name))
        if (
            record.name.startswith(("vncdotool", "websockets", "uvicorn"))
            and record.levelno <= logging.DEBUG
        ):
            record.msg = "Protocol debug details omitted"
            record.args = ()
        _redact_log_record(record)
        return record

    factory._agent_redaction = True
    logging.setLogRecordFactory(factory)
