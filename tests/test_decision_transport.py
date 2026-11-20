"""Check the real provider boundary against Cloudflare's published Clef schema.

Reference: https://developers.cloudflare.com/workers-ai/models/clef/schema-input.json
Response: https://developers.cloudflare.com/workers-ai/models/clef/schema-output.json
"""

import asyncio
import base64
import json
from io import BytesIO
from unittest.mock import AsyncMock, Mock

import httpx
import pytest
from PIL import Image

from ai_desktop_agent.actions.primitives import Action, ActionType
from ai_desktop_agent.agent.budget import ExecutionLimits, TaskBudget
from ai_desktop_agent.agent.decision.provider import OpenRouterClefProvider, WorkersAiClefProvider
from ai_desktop_agent.agent.llm.mock import MockLLMProvider
from ai_desktop_agent.agent.llm.types import ErrorContext, RecoveryPlan, RecoveryStrategy
from ai_desktop_agent.agent.state import AgentState, Goal, Subtask
from ai_desktop_agent.server.session import TaskSession
from ai_desktop_agent.server.store import TaskStore
from ai_desktop_agent.vm.fake import FakeDisplayBackend


@pytest.fixture
def transport(monkeypatch):
    original = httpx.AsyncClient.__init__
    observed = []
    opened = []

    def install(handler):
        def handle(request):
            observed.append(request)
            return handler(request)

        def client(instance, **kwargs):
            kwargs["transport"] = httpx.MockTransport(handle)
            original(instance, **kwargs)
            opened.append(instance)

        monkeypatch.setattr(httpx.AsyncClient, "__init__", client)
        return observed

    install.opened = opened
    return install


def provider(kind, **kwargs):
    if kind == "workers":
        return WorkersAiClefProvider(account_id="local-account", api_token="local-token", **kwargs)
    return OpenRouterClefProvider(api_key="local-token", **kwargs)


def verify_answer(prob=0.98, grade="完全達成"):
    levels = ["未達", "部分達成", "完全達成"]
    return {
        "verify_achieved": {"type": "noul", "noul": prob},
        "verify_grade": {
            "type": "score",
            "score": levels.index(grade),
            "legend": {str(i): label for i, label in enumerate(levels)},
            "probabilities": {str(i): float(label == grade) for i, label in enumerate(levels)},
            "confidence": 1.0,
        },
    }


def response(kind, answers, *, wrapped=True):
    body = {
        "model": "clef-flash",
        "answers": answers,
        "usage": {"input_tokens": 7, "output_tokens": 0},
    }
    # Use text so malformed numeric literals can reach the response parser too.
    return httpx.Response(
        200,
        text=json.dumps(
            {"success": True, "result": body} if kind == "workers" and wrapped else body
        ),
        headers={"Content-Type": "application/json"},
    )


@pytest.mark.parametrize("kind", ["workers", "openrouter"])
@pytest.mark.parametrize("phase", ["verify", "recover"])
async def test_the_wire_contract_uses_typed_questions_and_accounts_for_both_phases(
    transport, kind, phase
):
    def serve(request):
        payload = json.loads(request.content)
        assert payload["model"] == ("clef-flash" if kind == "workers" else "cloudflare/clef-flash")
        assert isinstance(payload["questions"], dict)
        for question in payload["questions"].values():
            assert question["instructions"]
        if phase == "verify":
            assert payload["questions"]["verify_grade"]["criteria"] == [
                "未達",
                "部分達成",
                "完全達成",
            ]
            assert "文書を保存" in payload["state"]
            answers = verify_answer()
        else:
            assert set(payload["questions"]["recover_strategy"]["criteria"]) == {
                "wait_and_retry",
                "alternative_approach",
                "replan_subtask",
                "give_up",
            }
            assert "再試行回数: 2" in payload["state"]
            answers = {
                "recover_strategy": {
                    "type": "choice",
                    "choice": "wait_and_retry",
                    "probabilities": {"wait_and_retry": 0.85, "give_up": 0.15},
                    "confidence": 0.7,
                }
            }
        return response(kind, answers)

    observed = transport(serve)
    decision = provider(kind)
    budget = TaskBudget(ExecutionLimits())
    async with budget.activate(lambda: None):
        if phase == "verify":
            result = await decision.verify_completion("文書を保存", "保存済みの画面")
            assert result.success is True and result.grade == "full" and result.prob == 0.98
        else:
            result = await decision.choose_recovery("応答を待機", "文書を保存", retry_count=2)
            assert result.strategy == "wait_and_retry" and result.prob == 0.85
            assert result.escalate is False and result.recoverable is True
    assert len(observed) == decision.call_count == budget.requests == 1
    assert budget.input_tokens == 7 and budget.output_tokens == 0 and budget.usage_complete
    assert observed[0].headers["authorization"] == "Bearer local-token"
    assert all(client.is_closed for client in transport.opened)
    assert str(observed[0].url) == (
        "https://api.cloudflare.com/client/v4/accounts/local-account/ai/run/@cf/cloudflare/clef-flash"
        if kind == "workers"
        else "https://openrouter.ai/api/alpha/decisions"
    )


@pytest.mark.parametrize("kind", ["workers", "openrouter"])
async def test_choice_probability_is_not_the_models_derived_confidence(transport, kind):
    transport(
        lambda _: response(
            kind,
            {
                "recover_strategy": {
                    "type": "choice",
                    "choice": "wait_and_retry",
                    "confidence": 0.99,
                    "probabilities": {"wait_and_retry": 0.55, "give_up": 0.45},
                }
            },
        )
    )
    result = await provider(kind).choose_recovery("一時的な失敗", "保存する")
    assert result.prob == 0.55 and result.escalate is True


@pytest.mark.parametrize("kind", ["workers", "openrouter"])
async def test_unknown_negative_grade_text_cannot_authorize_completion(transport, kind):
    transport(
        lambda _: response(
            kind,
            {
                "verify_achieved": {"noul": 0.99},
                "verify_grade": "not fully achieved",
            },
        )
    )
    result = await provider(kind).verify_completion("保存される", "画面を確認")
    assert result.success is None and result.grade == "partial"


@pytest.mark.parametrize("kind", ["workers", "openrouter"])
async def test_recovery_without_an_option_probability_defers_to_the_main_model(transport, kind):
    transport(
        lambda _: response(
            kind,
            {
                "recover_strategy": {
                    "type": "choice",
                    "choice": "give_up",
                    "confidence": 0.99,
                }
            },
        )
    )
    decision = provider(kind)
    llm = MockLLMProvider()
    llm.recover_from_error = AsyncMock(wraps=llm.recover_from_error)
    session = TaskSession(llm=llm, display=FakeDisplayBackend(), decision=decision)
    session.loop.start(Goal("保存する"))
    session.loop.understanding_done()
    session.loop.plan_ready([Subtask("save", "文書を保存する")])
    action = Action(ActionType.SUBTASK_COMPLETE)
    session.loop.record_action(action, False)
    session.loop.action_executed()
    session.loop.wait_complete()
    session.loop.verify_failed()
    session._pending_error = ErrorContext(action, "保存を確認できない", 0)
    try:
        await session._recover_phase()
        llm.recover_from_error.assert_awaited_once()
        assert session.decision_escalations == 1
    finally:
        await session.aclose()


@pytest.mark.parametrize("kind", ["workers", "openrouter"])
@pytest.mark.parametrize(
    "distribution",
    [
        {"2": 0.99, "0": "unknown"},
        {"2": 0.99, "0": True},
        {"2": 0.99, "0": -0.1},
        {"2": 0.99, "0": 0.5},
        {"2": 0.99, "0": float("nan")},
    ],
)
async def test_invalid_grade_distributions_cannot_authorize_completion(
    transport, kind, distribution
):
    answers = verify_answer()
    answers["verify_grade"]["probabilities"] = distribution
    transport(lambda _: response(kind, answers))
    with pytest.raises(ValueError):
        await provider(kind).verify_completion("保存される", "画面を確認")


@pytest.mark.parametrize("kind", ["workers", "openrouter"])
async def test_a_tied_grade_cannot_become_full_completion(transport, kind):
    answers = verify_answer()
    answers["verify_grade"]["probabilities"] = {"2": 0.5, "0": 0.5}
    transport(lambda _: response(kind, answers))
    result = await provider(kind).verify_completion("保存される", "画面を確認")
    assert result.success is None and result.grade == "partial"


@pytest.mark.parametrize("kind", ["workers", "openrouter"])
@pytest.mark.parametrize(
    "distribution",
    [
        {"give_up": 1.0},
        {"wait_and_retry": 0.1, "give_up": 0.9},
        {"wait_and_retry": 0.5, "give_up": 0.5},
        {"wait_and_retry": 0.9, "unexpected": 0.1},
        {"wait_and_retry": 0.9, "give_up": "unknown"},
        {"wait_and_retry": 0.9, "give_up": 0.9},
    ],
)
async def test_inconsistent_choice_distributions_cannot_authorize_recovery(
    transport, kind, distribution
):
    transport(
        lambda _: response(
            kind,
            {
                "recover_strategy": {
                    "type": "choice",
                    "choice": "wait_and_retry",
                    "prob": 0.99,
                    "probabilities": distribution,
                }
            },
        )
    )
    with pytest.raises(ValueError):
        await provider(kind).choose_recovery("保存できない", "保存する")


@pytest.mark.parametrize("kind", ["workers", "openrouter"])
@pytest.mark.parametrize(
    "field,node",
    [
        ("verify_achieved", {"type": "score", "score": 0.99}),
        ("verify_achieved", {"type": "noul", "prob": 0.99}),
        ("verify_grade", {"type": "choice", "choice": "full"}),
        ("recover_strategy", {"type": "noul", "choice": "wait_and_retry", "prob": 0.99}),
        ("recover_strategy", {"type": "choice", "choice": "wait_and_retry", "prob": 0.99}),
    ],
)
async def test_an_answer_for_a_different_question_type_is_not_a_valid_decision(
    transport, kind, field, node
):
    answers = {**verify_answer(), field: node}
    transport(lambda _: response(kind, answers))
    with pytest.raises(ValueError):
        if field == "recover_strategy":
            await provider(kind).choose_recovery("保存できない", "保存する")
        else:
            await provider(kind).verify_completion("保存される", "画面を確認")


@pytest.mark.parametrize("kind", ["workers", "openrouter"])
@pytest.mark.parametrize("answers", [None, [], "invalid"])
async def test_a_broken_answers_envelope_cannot_fall_back_to_flat_success_fields(
    transport, kind, answers
):
    body = {"answers": answers, **verify_answer()}
    transport(lambda _: httpx.Response(200, json={"result": body} if kind == "workers" else body))
    with pytest.raises(ValueError):
        await provider(kind).verify_completion("保存される", "画面を確認")


@pytest.mark.parametrize("result", [None, [], "invalid"])
async def test_a_broken_workers_result_envelope_cannot_expose_flat_success(transport, result):
    transport(lambda _: httpx.Response(200, json={"result": result, **verify_answer()}))
    with pytest.raises(ValueError):
        await provider("workers").verify_completion("保存される", "画面を確認")


@pytest.mark.parametrize("kind", ["workers", "openrouter"])
async def test_a_typed_score_without_its_distribution_cannot_use_an_extra_full_label(
    transport, kind
):
    answers = verify_answer()
    answers["verify_grade"] = {"type": "score", "score": 0.99, "label": "full"}
    transport(lambda _: response(kind, answers))
    assert (await provider(kind).verify_completion("保存される", "画面を確認")).success is None


@pytest.mark.parametrize("kind", ["workers", "openrouter"])
async def test_an_extra_option_field_cannot_replace_the_canonical_choice(transport, kind):
    transport(
        lambda _: response(
            kind,
            {
                "recover_strategy": {
                    "type": "choice",
                    "choice": "wait_and_retry",
                    "option": "give_up",
                    "probabilities": {"wait_and_retry": 0.01, "give_up": 0.99},
                    "confidence": 0.98,
                }
            },
        )
    )
    with pytest.raises(ValueError):
        await provider(kind).choose_recovery("保存できない", "保存する")


async def test_a_workers_failure_flag_cannot_authorize_a_successful_result(transport):
    body = json.loads(response("workers", verify_answer()).text)
    body["success"] = False
    transport(lambda _: httpx.Response(200, json=body))
    with pytest.raises(ValueError):
        await provider("workers").verify_completion("保存される", "画面を確認")


@pytest.mark.parametrize("kind", ["workers", "openrouter"])
@pytest.mark.parametrize("failure", ["http", "network", "json", "timeout"])
async def test_failed_requests_close_the_client_and_preserve_the_original_error(
    transport, kind, failure
):
    errors = {
        "http": httpx.HTTPStatusError,
        "network": httpx.ConnectError,
        "json": ValueError,
        "timeout": httpx.ReadTimeout,
    }

    def serve(request):
        if failure == "http":
            return httpx.Response(503, text="temporarily unavailable")
        if failure == "json":
            return httpx.Response(200, text="truncated {")
        raise errors[failure]("transport failed", request=request)

    observed = transport(serve)
    decision = provider(kind)
    with pytest.raises(errors[failure]):
        await decision.verify_completion("保存される", "画面を確認")
    assert len(observed) == decision.call_count == 1
    assert transport.opened and all(client.is_closed for client in transport.opened)


@pytest.mark.parametrize("kind", ["workers", "openrouter"])
@pytest.mark.parametrize("body", [None, [], "not an answer", 1])
async def test_non_object_json_is_not_a_decision(transport, kind, body):
    transport(lambda _: httpx.Response(200, text=json.dumps(body)))
    with pytest.raises(ValueError):
        await provider(kind).verify_completion("保存される", "画面を確認")
    assert all(client.is_closed for client in transport.opened)


@pytest.mark.parametrize("kind", ["workers", "openrouter"])
@pytest.mark.parametrize(
    "image", [b"corrupted screenshot", b"x" * (4 * 1024 * 1024 + 1)], ids=["corrupt", "oversized"]
)
async def test_invalid_image_evidence_is_not_silently_removed_or_sent_as_fake_jpeg(
    transport, kind, image
):
    observed = transport(lambda _: response(kind, verify_answer()))
    with pytest.raises(ValueError):
        await provider(kind).verify_completion("保存される", "画面を確認", image_bytes=image)
    assert not observed and not transport.opened


@pytest.mark.parametrize("format", ["PNG", "TIFF"])
def test_a_small_compressed_image_cannot_expand_beyond_the_jpeg_request_limit(format):
    # A 16 MP checkerboard is a small PNG but an oversized JPEG at quality 80.
    pixels = (b"\xaa" * 500 + b"\x55" * 500) * 2000
    encoded = BytesIO()
    Image.frombytes("1", (4000, 4000), pixels).save(encoded, format=format)
    image = encoded.getvalue()
    assert len(image) < 4 * 1024 * 1024
    if format == "TIFF":
        with pytest.raises(ValueError, match="画像"):
            provider("workers").build_verify_payload("保存される", "画面を確認", image)
    else:
        payload = provider("workers").build_verify_payload("保存される", "画面を確認", image)
        url = payload["images"][0]
        assert url.startswith("data:image/png;base64,")
        assert base64.b64decode(url.split(",", 1)[1]) == image


def test_a_compressed_image_above_sixteen_megapixels_is_rejected_before_decoding(monkeypatch):
    encoded = BytesIO()
    Image.new("1", (16001, 1000)).save(encoded, format="PNG")
    decode = Mock(side_effect=AssertionError("decoded oversized image"))
    monkeypatch.setattr(Image.Image, "load", decode)
    with pytest.raises(ValueError, match="画像"):
        provider("workers").build_verify_payload("保存される", "画面を確認", encoded.getvalue())
    decode.assert_not_called()


@pytest.mark.parametrize("kind", ["workers", "openrouter"])
@pytest.mark.parametrize("phase", ["verify", "recover"])
@pytest.mark.parametrize("failure", ["http", "invalid-answer"])
async def test_failed_decision_attempts_remain_in_persisted_usage_when_the_llm_recovers(
    transport, kind, phase, failure, tmp_path
):
    observed = transport(lambda _: httpx.Response(503) if failure == "http" else response(kind, {}))
    llm = MockLLMProvider(
        recover_result=RecoveryPlan(RecoveryStrategy.ALTERNATIVE_APPROACH, [], "再確認する", True)
    )
    llm.verify_result = AsyncMock(wraps=llm.verify_result)
    llm.recover_from_error = AsyncMock(wraps=llm.recover_from_error)
    store = TaskStore(tmp_path)
    decision = provider(kind)
    session = TaskSession(llm=llm, display=FakeDisplayBackend(), decision=decision, store=store)
    session.loop.start(Goal("文書を保存する"))
    session.loop.understanding_done()
    session.loop.plan_ready([Subtask("save", "文書を保存する")])
    session.loop.record_action(
        Action(ActionType.SUBTASK_COMPLETE), phase == "verify", "保存できない"
    )
    session.loop.action_executed()
    session.loop.wait_complete()
    try:
        if phase == "verify":
            await session._verify_phase()
            llm.verify_result.assert_awaited_once()
            assert session.loop.state == AgentState.COMPLETED
        else:
            session.loop.verify_failed()
            await session._recover_phase()
            llm.recover_from_error.assert_awaited_once()
            assert session.loop.state == AgentState.EXECUTING
        record = store.load(session.id)
        assert len(observed) == decision.call_count == session.decision_calls == 1
        assert record.decision_calls == 1 and record.decision_escalations == 1
    finally:
        await session.aclose()


@pytest.mark.parametrize("kind", ["workers", "openrouter"])
@pytest.mark.parametrize("phase", ["verify", "recover"])
@pytest.mark.parametrize("outcome", ["timeout", "stop"])
async def test_interrupted_decision_requests_are_closed_counted_and_never_retried_by_the_llm(
    transport, kind, phase, outcome, tmp_path
):
    entered = asyncio.Event()

    async def serve(request):
        entered.set()
        await asyncio.Event().wait()

    observed = transport(serve)
    llm = MockLLMProvider()
    llm.verify_result = AsyncMock()
    llm.recover_from_error = AsyncMock()
    store = TaskStore(tmp_path)
    session = TaskSession(
        llm=llm,
        display=FakeDisplayBackend(),
        decision=provider(kind),
        store=store,
        limits=ExecutionLimits(
            max_seconds=5, request_timeout_seconds=0.03 if outcome == "timeout" else 2
        ),
    )

    async def run_phase(goal):
        session.loop.understanding_done()
        session.loop.plan_ready([Subtask("save", "文書を保存する")])
        session.loop.record_action(Action(ActionType.SUBTASK_COMPLETE), phase == "verify")
        session.loop.action_executed()
        session.loop.wait_complete()
        if phase == "verify":
            await session._verify_phase()
        else:
            session.loop.verify_failed()
            await session._recover_phase()

    session._run_task = run_phase
    task = asyncio.create_task(session.run("文書を保存する"))
    try:
        await asyncio.wait_for(entered.wait(), 2)
        if outcome == "stop":
            session.stop()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(asyncio.shield(task), 2)
        else:
            assert await asyncio.wait_for(asyncio.shield(task), 2) is False
        record = store.load(session.id)
        assert record.state == "failed" and record.success is False
        assert record.decision_calls == 1 and record.decision_escalations == 0
        assert record.execution_usage["requests"] == len(observed) == 1
        assert record.execution_usage["usage_complete"] is False
        assert record.stop_reason == ("request_timeout" if outcome == "timeout" else "cancelled")
        assert record.error_message
        llm.verify_result.assert_not_awaited()
        llm.recover_from_error.assert_not_awaited()
        assert all(client.is_closed for client in transport.opened)
        assert not session.is_running and not session.display.is_connected
    finally:
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
