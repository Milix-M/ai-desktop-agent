import base64
import json
import logging
import uuid
from urllib.parse import quote

import httpx
import pytest
from openai import AsyncOpenAI

from ai_desktop_agent.actions.executor import ActionExecutor
from ai_desktop_agent.actions.primitives import Action, ActionType
from ai_desktop_agent.agent.llm.openai_compat_provider import OpenAICompatProvider
from ai_desktop_agent.privacy import REDACTED, install_log_redaction, protect, redact, redact_data
from ai_desktop_agent.server.auth import AccessControl
from ai_desktop_agent.server.store import StoredAction, TaskRecord, TaskStore
from ai_desktop_agent.vm.fake import FakeDisplayBackend


def test_env_credentials_and_escaped_variants_are_redacted(monkeypatch):
    secret = "secret-" + uuid.uuid4().hex + '\n日本語/"'
    monkeypatch.setenv("OPENAI_API_KEY", secret)
    for text in (
        secret,
        json.dumps(secret)[1:-1],
        repr(secret)[1:-1],
        quote(secret, safe=""),
        base64.b64encode(secret.encode()).decode(),
    ):
        assert redact("prefix " + text + " suffix") == "prefix " + REDACTED + " suffix"
    assert redact_data({"text": secret, "count": 3, "list": [secret]}) == {
        "text": REDACTED,
        "count": 3,
        "list": [REDACTED],
    }


def test_generated_access_key_and_browser_cookie_are_protected():
    access = AccessControl("operator-" + uuid.uuid4().hex)
    cookie = access.new_session()
    assert redact(access._key.decode()) == REDACTED
    assert redact(cookie) == REDACTED
    assert access.valid_key(access._key.decode())


async def test_factory_does_not_log_a_credential_from_an_explicit_url(caplog):
    from ai_desktop_agent.agent.llm.factory import create_llm_provider

    secret = "gateway-key-" + uuid.uuid4().hex
    with caplog.at_level(logging.INFO):
        provider = create_llm_provider(
            provider="openai",
            api_key="configured-api-key",
            base_url="https://example.test/v1?key=" + secret,
        )
    await provider.aclose()
    assert secret not in caplog.text


def test_url_credentials_headers_and_nested_tokens_are_not_exposed(monkeypatch):
    password = uuid.uuid4().hex
    token = uuid.uuid4().hex
    monkeypatch.setenv(
        "LLM_BASE_URL", f"https://operator:{password}@example.test/v1?api_key={token}"
    )
    for value in (
        password,
        token,
        f"https://operator:{password}@example.test/v1?token={token}",
        "Bearer " + token,
        "Basic " + token,
    ):
        assert password not in redact(value) and token not in redact(value)
    assert redact("https://example.test/v1?model=local") == "https://example.test/v1?model=local"


def test_log_records_arguments_and_tracebacks_do_not_retain_a_known_key(caplog, monkeypatch):
    secret = "log-key-" + uuid.uuid4().hex
    monkeypatch.setenv("OPENAI_API_KEY", secret)
    install_log_redaction()
    logger = logging.getLogger("privacy-check")
    with caplog.at_level(logging.INFO):
        logger.info("credential=%s", secret)
        try:
            raise ValueError("API failure: " + secret)
        except ValueError:
            logger.exception("provider failed %s", secret)
    assert secret not in caplog.text
    assert all(secret not in repr(record.__dict__) for record in caplog.records)
    assert "ValueError" in caplog.text and "privacy-check" in caplog.text


def test_structured_fields_are_redacted_after_formatting(caplog, monkeypatch):
    secret = "structured-key-" + uuid.uuid4().hex
    monkeypatch.setenv("OPENAI_API_KEY", secret)
    original = caplog.handler.formatter
    try:
        caplog.handler.setFormatter(logging.Formatter("%(message)s %(credential)s"))
        install_log_redaction()
        with caplog.at_level(logging.INFO):
            logging.getLogger("privacy-extra").info("request", extra={"credential": secret})
        assert secret not in caplog.text and REDACTED in caplog.text
    finally:
        caplog.handler.setFormatter(original)


def test_malformed_log_arguments_cannot_interrupt_task_execution(caplog):
    install_log_redaction()
    with caplog.at_level(logging.INFO):
        logging.getLogger("privacy-check").info("invalid format %s %s", "argument")
    assert "Unable to format log message" in caplog.text


@pytest.mark.parametrize("name", ["vncdotool.client", "websockets.server", "uvicorn.error"])
def test_vnc_debug_logs_cannot_reconstruct_typed_characters(caplog, name):
    install_log_redaction()
    with caplog.at_level(logging.DEBUG):
        logging.getLogger(name).debug("keyPress %s", [112, 97, 115, 115])
    assert "keyPress" not in caplog.text and "112" not in caplog.text


def test_revoked_cookie_is_still_masked_in_cookie_headers():
    access = AccessControl("operator-" + uuid.uuid4().hex)
    cookie = access.new_session()
    access.revoke(cookie)
    assert (
        redact("Cookie: ai_desktop_agent_session=" + cookie)
        == "Cookie: ai_desktop_agent_session=" + REDACTED
    )


async def test_executor_logs_never_preview_an_unknown_typed_value(caplog):
    text = "private-user-input-" + uuid.uuid4().hex
    with caplog.at_level(logging.INFO):
        assert await ActionExecutor(FakeDisplayBackend()).execute(
            Action(ActionType.TYPE, {"text": text})
        )
    assert text not in caplog.text and text[:20] not in caplog.text


def test_store_scrubs_old_records_atomically_and_preserves_corrupt_files(tmp_path, monkeypatch):
    secret = "stored-key-" + uuid.uuid4().hex
    store = TaskStore(tmp_path)
    record = TaskRecord(
        "old-task",
        "instruction " + secret,
        "completed",
        actions=[StoredAction("type", {"text": secret}, secret, True, reasoning=secret)],
        goal={"description": secret},
    )
    path = tmp_path / "tasks" / "old-task.json"
    path.write_text(json.dumps(record.to_dict()))
    bad = tmp_path / "tasks" / "corrupt.json"
    bad.write_text("not json")
    backup = tmp_path / "tasks" / "old-task.backup.json"
    backup.write_bytes(path.read_bytes())
    backup_bytes = backup.read_bytes()
    monkeypatch.setenv("ANTHROPIC_API_KEY", secret)
    assert secret not in json.dumps(store.load("old-task").to_dict())
    assert store.redact_existing() == 1
    assert secret not in path.read_text() and bad.read_text() == "not json"
    assert backup.read_bytes() == backup_bytes
    assert path.stat().st_mode & 0o777 == 0o600
    assert store.redact_existing() == 0
    store.save(record)
    assert secret not in path.read_text()


async def test_model_text_is_scrubbed_but_authentication_header_remains_valid():
    secret = "model-key-" + uuid.uuid4().hex
    observed = []

    def response(request):
        observed.append(request)
        return httpx.Response(
            200,
            json={
                "id": "test",
                "object": "chat.completion",
                "created": 0,
                "model": "local",
                "choices": [
                    {
                        "index": 0,
                        "message": {
                            "role": "assistant",
                            "content": json.dumps({"reasoning": secret}),
                        },
                        "finish_reason": "stop",
                    }
                ],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
            },
        )

    provider = OpenAICompatProvider(api_key=secret)
    await provider._client.close()
    provider._client = AsyncOpenAI(
        api_key=secret,
        base_url="https://example.test/v1",
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(response)),
    )
    try:
        result = await provider._call("Do not send " + secret)
        assert secret not in observed[0].content.decode()
        assert observed[0].headers["authorization"] == "Bearer " + secret
        assert result == {"reasoning": REDACTED}
    finally:
        await provider.aclose()


def test_image_payloads_are_opaque_and_not_modified_as_text():
    value = "image-token-" + uuid.uuid4().hex
    protect(value)
    data = "data:image/png;base64," + value
    assert redact_data({"prompt": value, "images": [data]}, skip_images=True) == {
        "prompt": REDACTED,
        "images": [data],
    }


def test_invalid_unicode_in_a_configured_key_cannot_break_logging(monkeypatch):
    secret = "invalid-key-\udcff" + uuid.uuid4().hex
    monkeypatch.setenv("OPENAI_API_KEY", secret)
    assert redact(secret) == REDACTED


@pytest.mark.parametrize("kind", ["workers", "openrouter"])
async def test_decision_models_receive_redacted_text_with_valid_authentication(kind, monkeypatch):
    from ai_desktop_agent.agent.decision.provider import (
        OpenRouterClefProvider,
        WorkersAiClefProvider,
    )

    secret = "decision-key-" + uuid.uuid4().hex
    observed = []
    original_client = httpx.AsyncClient

    def response(request):
        observed.append(request)
        return httpx.Response(200, json={"ok": True})

    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda **kwargs: original_client(transport=httpx.MockTransport(response), **kwargs),
    )
    provider = (
        WorkersAiClefProvider(account_id="account", api_token=secret)
        if kind == "workers"
        else OpenRouterClefProvider(api_key=secret)
    )
    assert await provider._post({"state": {"text": secret}, "questions": {}}) == {"ok": True}
    assert secret not in observed[0].content.decode()
    assert observed[0].headers["authorization"] == "Bearer " + secret
