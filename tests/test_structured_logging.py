"""Actual formatter contracts and late handlers keep redacted log records usable."""

import io
import json
import logging
import os
import subprocess
import sys
import uuid
from contextlib import contextmanager
from pathlib import Path

import pytest
from uvicorn.logging import AccessFormatter

from ai_desktop_agent.privacy import REDACTED, install_log_redaction, protect, redact, unprotect


@contextmanager
def output(formatter, *, late=True):
    stream = io.StringIO()
    records = []

    class Capture(logging.StreamHandler):
        def emit(self, record):
            super().emit(record)
            records.append(record)

    logger = logging.getLogger("structured-check-" + uuid.uuid4().hex)
    logger.setLevel(logging.DEBUG)
    logger.propagate = False
    handler = Capture(stream)
    handler.setFormatter(formatter)
    if late:
        install_log_redaction()
    logger.addHandler(handler)
    if not late:
        install_log_redaction()
    try:
        yield logger, stream, records
    finally:
        logger.removeHandler(handler)
        handler.close()


@pytest.mark.parametrize("status", [200, 404])
@pytest.mark.parametrize("late", [False, True])
def test_access_formatter_keeps_its_five_arguments_and_masks_the_path(
    monkeypatch, capsys, status, late
):
    secret = "access-key-" + uuid.uuid4().hex
    monkeypatch.setenv("OPENAI_API_KEY", secret)
    formatter = AccessFormatter(
        '%(client_addr)s - "%(request_line)s" %(status_code)s', use_colors=False
    )
    with output(formatter, late=late) as (logger, stream, records):
        logger.info(
            '%s - "%s %s HTTP/%s" %d',
            "127.0.0.1:12345",
            "GET",
            "/health?model=" + secret,
            "1.1",
            status,
        )
    text = stream.getvalue()
    assert "Logging error" not in capsys.readouterr().err
    assert f'127.0.0.1:12345 - "GET /health?model={REDACTED} HTTP/1.1" {status}' in text
    assert secret not in text and secret not in repr(records[0].__dict__)
    assert len(records[0].args) == 5 and type(records[0].args[-1]) is int


@pytest.mark.parametrize("late", [False, True])
def test_nonroot_extra_fields_are_redacted_and_the_record_does_not_retain_them(monkeypatch, late):
    secret = "extra-key-" + uuid.uuid4().hex
    monkeypatch.setenv("OPENAI_API_KEY", secret)
    formatter = logging.Formatter("%(message)s %(credential)s %(payload)s")
    with output(formatter, late=late) as (logger, stream, records):
        logger.info("request", extra={"credential": secret, "payload": {"headers": [secret]}})
    assert REDACTED in stream.getvalue() and secret not in stream.getvalue()
    assert secret not in repr(records[0].__dict__)


@pytest.mark.parametrize("style", ["tuple", "mapping"])
def test_numeric_arguments_keep_their_types_for_message_and_structured_formatters(
    monkeypatch, style
):
    secret = "numeric-key-" + uuid.uuid4().hex
    monkeypatch.setenv("OPENAI_API_KEY", secret)
    with output(logging.Formatter("%(message)s")) as (logger, stream, records):
        if style == "tuple":
            logger.info("attempt=%04d credential=%s", 7, secret)
        else:
            logger.info("attempt=%(attempt)04d credential=%(key)s", {"attempt": 7, "key": secret})
    assert stream.getvalue().strip() == "attempt=0007 credential=" + REDACTED
    args = records[0].args
    assert type(args[0] if style == "tuple" else args["attempt"]) is int
    assert secret not in repr(records[0].__dict__)


def test_a_credential_assembled_during_formatting_is_masked_in_output_and_cached_message(
    monkeypatch,
):
    first, second = "split-credential-", uuid.uuid4().hex
    secret = first + second
    monkeypatch.setenv("OPENAI_API_KEY", secret)
    with output(logging.Formatter("%(message)s")) as (logger, stream, records):
        logger.info("%s%s", first, second)
    assert stream.getvalue().strip() == REDACTED
    assert records[0].getMessage() == REDACTED
    assert secret not in repr(records[0].__dict__)


def test_a_credential_assembled_from_extra_fields_is_masked_after_formatting(monkeypatch):
    first, second = "split-extra-", uuid.uuid4().hex
    secret = first + second
    monkeypatch.setenv("OPENAI_API_KEY", secret)
    with output(logging.Formatter("%(first)s%(second)s %(message)s")) as (logger, stream, _):
        logger.info("request", extra={"first": first, "second": second})
    assert stream.getvalue().strip() == REDACTED + " request"


def test_exception_arguments_are_scrubbed_without_retaining_the_exception_object(monkeypatch):
    secret = "exception-key-" + uuid.uuid4().hex
    monkeypatch.setenv("OPENAI_API_KEY", secret)
    with output(logging.Formatter("%(message)s")) as (logger, stream, records):
        logger.warning("provider rejected %s", ValueError(secret))
    assert REDACTED in stream.getvalue() and secret not in repr(records[0].__dict__)


def test_a_safe_string_representation_cannot_hide_a_credential_in_repr(monkeypatch):
    secret = "repr-key-" + uuid.uuid4().hex
    monkeypatch.setenv("OPENAI_API_KEY", secret)

    class CredentialHandle:
        def __str__(self):
            return "credential handle"

        def __repr__(self):
            return "credential=" + secret

    with output(logging.Formatter("%(message)s")) as (logger, stream, records):
        logger.info("provider=%r", CredentialHandle())
    assert secret not in stream.getvalue() and secret not in repr(records[0].__dict__)
    assert "provider=" in stream.getvalue()


def test_parent_handlers_added_after_setup_also_redact_extra_fields(monkeypatch):
    secret = "parent-key-" + uuid.uuid4().hex
    monkeypatch.setenv("OPENAI_API_KEY", secret)
    with output(logging.Formatter("%(message)s %(credential)s")) as (parent, stream, records):
        child = logging.getLogger(parent.name + ".requests")
        child.info("request", extra={"credential": secret})
    assert stream.getvalue().strip() == "request " + REDACTED
    assert len(records) == 1 and secret not in repr(records[0].__dict__)


def test_a_formatter_failure_does_not_dump_its_exception_or_raw_record_to_stderr(
    monkeypatch, capsys
):
    secret = "formatter-key-" + uuid.uuid4().hex
    monkeypatch.setenv("OPENAI_API_KEY", secret)

    class FailingFormatter(logging.Formatter):
        def format(self, record):
            raise ValueError("formatter failed " + secret)

    with output(FailingFormatter()) as (logger, stream, records):
        logger.info("request %d", 200)
    assert stream.getvalue().strip() == "Unable to format log message"
    assert records[0].message == "Unable to format log message"
    assert "Logging error" not in capsys.readouterr().err
    assert secret not in repr(records[0].__dict__)


@pytest.mark.parametrize("debug", [False, True])
def test_a_sink_that_does_not_call_a_formatter_receives_only_scrubbed_record_fields(
    monkeypatch, debug
):
    secret = "sink-key-" + uuid.uuid4().hex
    monkeypatch.setenv("OPENAI_API_KEY", secret)
    records = []

    class StructuredSink(logging.Handler):
        def emit(self, record):
            records.append(record)

    install_log_redaction()
    logger = logging.getLogger("uvicorn.error.structured-sink-" + uuid.uuid4().hex)
    logger.setLevel(logging.DEBUG)
    logger.propagate = False
    handler = StructuredSink()
    logger.addHandler(handler)
    try:
        if debug:
            try:
                raise ValueError("provider failed " + secret)
            except ValueError:
                logger.debug(
                    "keyPress %s", [112, 97, 115, 115], exc_info=True, extra={"credential": secret}
                )
        else:
            logger.info("status=%d", 200, extra={"credential": secret})
    finally:
        logger.removeHandler(handler)
        handler.close()
    assert len(records) == 1 and secret not in repr(records[0].__dict__)
    assert records[0].credential == REDACTED
    if debug:
        assert records[0].message == "Protocol debug details omitted" and records[0].args == ()
        assert records[0].exc_info is None and "ValueError" in records[0].exc_text
    else:
        assert records[0].message == "status=200" and records[0].args == (200,)


def test_a_previous_record_factory_still_supplies_its_structured_metadata():
    previous = logging.getLogRecordFactory()

    def traced(*args, **kwargs):
        record = previous(*args, **kwargs)
        record.trace_id = 42
        return record

    logging.setLogRecordFactory(traced)
    try:
        with output(logging.Formatter("%(trace_id)d %(message)s")) as (logger, stream, records):
            logger.info("work=%d", 3)
    finally:
        logging.setLogRecordFactory(previous)
    assert stream.getvalue().strip() == "42 work=3"
    assert records[0].trace_id == 42 and records[0].args == (3,)


def test_repeated_installation_keeps_one_delivery_and_a_useful_exception_trace(monkeypatch):
    secret = "trace-key-" + uuid.uuid4().hex
    monkeypatch.setenv("OPENAI_API_KEY", secret)
    with output(logging.Formatter("%(message)s")) as (logger, stream, records):
        install_log_redaction()
        install_log_redaction()
        try:
            raise OSError("failed " + secret)
        except OSError:
            logger.exception("request failed", stack_info=True)
    assert len(records) == 1 and "OSError" in stream.getvalue() and "Stack" in stream.getvalue()
    assert secret not in stream.getvalue() and secret not in repr(records[0].__dict__)


def test_repeated_registration_is_idempotent_and_unregistration_invalidates_cached_credentials():
    secret = "registered-key-" + uuid.uuid4().hex
    assert redact(secret) == secret
    try:
        protect(secret)
        protect(secret)
        assert redact(secret) == REDACTED
        unprotect(secret)
        assert redact(secret) == secret
    finally:
        unprotect(secret)


@pytest.mark.parametrize("kind", ["key", "url"])
def test_rotating_environment_credentials_cannot_reuse_an_obsolete_pattern(monkeypatch, kind):
    first, second = "first-key-" + uuid.uuid4().hex, "second-key-" + uuid.uuid4().hex
    name = "STRUCTURED_TEST_API_KEY" if kind == "key" else "STRUCTURED_TEST_URL"

    def value(secret):
        return secret if kind == "key" else "https://operator:" + secret + "@example.test/v1"

    monkeypatch.setenv(name, value(first))
    assert redact(first) == REDACTED
    monkeypatch.setenv(name, value(second))
    assert redact(second) == REDACTED and redact(first) == first
    monkeypatch.delenv(name)
    assert redact(second) == second


def test_an_unrelated_malformed_url_cannot_disable_credential_redaction(monkeypatch):
    secret = "url-error-key-" + uuid.uuid4().hex
    monkeypatch.setenv("STRUCTURED_TEST_URL", "https://[invalid")
    monkeypatch.setenv("STRUCTURED_TEST_API_KEY", secret)
    assert redact(secret) == REDACTED
    with output(logging.Formatter("%(message)s")) as (logger, stream, _):
        logger.info("connection failed %s", secret)
    assert stream.getvalue().strip() == "connection failed " + REDACTED


def test_a_fresh_process_without_configured_credentials_still_masks_bearer_headers():
    environment = {
        name: value
        for name, value in os.environ.items()
        if name.startswith(("COVERAGE_", "COV_CORE_"))
    }
    environment["PYTHONPATH"] = os.environ.get(
        "PYTHONPATH", str(Path(__file__).resolve().parents[1] / "src")
    )
    code = (
        "import json; from ai_desktop_agent.privacy import redact;"
        "print(json.dumps([redact('ordinary text'),redact('Bearer abcdefghijklmnop')]))"
    )
    result = subprocess.run(
        [sys.executable, "-c", code], env=environment, capture_output=True, text=True, check=True
    )
    assert json.loads(result.stdout) == ["ordinary text", "Bearer " + REDACTED]
