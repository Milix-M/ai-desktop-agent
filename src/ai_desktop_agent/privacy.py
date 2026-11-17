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
_registered: set[str] = set()
_lock = threading.Lock()
_credential_name = re.compile(
    r"(?:^|_)(?:API_KEY|API_TOKEN|ACCESS_TOKEN|SECRET_ACCESS_KEY|SECRET_KEY|SECRET|PASSWORD|TOKEN)$"
)
_userinfo = re.compile(r"(https?://)[^/\s:@]+:[^/\s@]+@", re.IGNORECASE)
_bearer = re.compile(r"\b(Bearer|Basic)\s+[A-Za-z0-9+/_.=~-]{8,}", re.IGNORECASE)
_query = re.compile(r"([?&](?:api[_-]?key|access_token|token|secret|password)=)[^&\s\"']+", re.I)
_cookie = re.compile(r"(ai_desktop_agent_session=)[A-Za-z0-9_-]{8,}")


def protect(value: str) -> None:
    if len(value) >= 8:
        with _lock:
            _registered.add(value)


def unprotect(value: str) -> None:
    with _lock:
        _registered.discard(value)


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


def redact(text: str) -> str:
    with _lock:
        values = set(_registered)
    values.update(
        value
        for name, value in os.environ.items()
        if _credential_name.search(name) and len(value) >= 8
    )
    for name, value in os.environ.items():
        if name.endswith("_URL"):
            values.update(_url_values(value))
    pattern = _pattern(tuple(sorted(values)))
    if pattern is not None:
        text = pattern.sub(lambda _: REDACTED, text)
    text = _userinfo.sub(lambda m: m[1] + REDACTED + "@", text)
    text = _bearer.sub(lambda m: m[1] + " " + REDACTED, text)
    text = _query.sub(lambda m: m[1] + REDACTED, text)
    return _cookie.sub(lambda m: m[1] + REDACTED, text)


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


class _RedactingFormatter(logging.Formatter):
    def __init__(self, original):
        super().__init__()
        self.original = original or logging.Formatter()

    def format(self, record):
        return redact(self.original.format(record))


def install_log_redaction() -> None:
    for handler in logging.getLogger().handlers:
        if not isinstance(handler.formatter, _RedactingFormatter):
            handler.setFormatter(_RedactingFormatter(handler.formatter))
    previous = logging.getLogRecordFactory()
    if getattr(previous, "_agent_redaction", False):
        return

    def factory(*args, **kwargs):
        record = previous(*args, **kwargs)
        if (
            record.name.startswith(("vncdotool", "websockets", "uvicorn"))
            and record.levelno <= logging.DEBUG
        ):
            record.msg = "Protocol debug details omitted"
        else:
            try:
                record.msg = redact(record.getMessage())
            except (TypeError, ValueError):
                record.msg = "Unable to format log message"
        record.args = ()
        if record.exc_info:
            record.exc_text = redact("".join(traceback.format_exception(*record.exc_info)))
            record.exc_info = None
        if record.stack_info:
            record.stack_info = redact(record.stack_info)
        return record

    factory._agent_redaction = True
    logging.setLogRecordFactory(factory)
