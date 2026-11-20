"""Legacy gateway formats and offline routing must retain conservative behavior."""

import httpx
import pytest

from ai_desktop_agent.agent.decision.provider import (
    MockDecisionProvider,
    OpenRouterClefProvider,
    WorkersAiClefProvider,
    create_decision_provider_from_env,
)
from tests.test_decision_transport import provider  # noqa: F401
from tests.test_decision_transport import transport as transport


def answer(transport, body):
    transport(lambda _: httpx.Response(200, json=body))
    return provider("openrouter", base_url="https://gateway.test/decisions")


@pytest.mark.parametrize("field", ["prob_true", "p_yes", "noul", "probability", "prob", "score"])
async def test_legacy_probability_fields_still_verify_explicit_evidence(transport, field):
    decision = answer(transport, {"verify_achieved": {field: 0.95}, "grade": "full"})
    result = await decision.verify_completion("saved", "saved document")
    assert result.success is True and result.prob == 0.95


async def test_legacy_question_arrays_skip_unrelated_entries(transport):
    decision = answer(
        transport,
        {
            "questions": [
                None,
                {"id": "other", "prob": 0.01},
                {"id": "verify_achieved", "prob": 0.95},
            ],
            "top_rubric": {"value": "完全達成"},
        },
    )
    assert (await decision.verify_completion("saved", "saved document")).success is True


@pytest.mark.parametrize(
    "body",
    [
        {"questions": [None, {"id": "other", "prob": 0.95}]},
        {"questions": [{"id": "verify_achieved"}]},
        {"questions": {"verify_achieved": 0.95}},
        {"verify_achieved": {"unknown": 0.95}},
        {"verify_achieved": {"prob_true": "invalid", "prob": 0.99}},
    ],
)
async def test_incomplete_legacy_probability_cannot_be_guessed(transport, body):
    decision = answer(transport, {**body, "grade": "full"})
    with pytest.raises(ValueError):
        await decision.verify_completion("saved", "saved document")


@pytest.mark.parametrize(
    "grade",
    [
        {"label": "FULL"},
        {"choice": "完全達成"},
        {"option": " full "},
        {"value": "達成済"},
        {"scores": {"full": 0.95, "none": 0.05}},
    ],
)
async def test_legacy_grade_nodes_still_recognize_exact_full_labels(transport, grade):
    decision = answer(transport, {"verify_achieved": 0.95, "verify_grade": grade})
    assert (await decision.verify_completion("saved", "saved document")).success is True


@pytest.mark.parametrize(
    "body,expected",
    [
        ({"verify_grade": {"full": 0.95, "none": 0.05}}, True),
        ({"scores": {"full": 0.95, "none": 0.05}}, True),
        ({"score": {"full": 0.95, "none": 0.05}}, True),
        ({"verify_grade": {"full": 0.5, "none": 0.5}}, None),
        ({"top": {"value": "未達成"}}, False),
        ({"verify_grade": "部分達成"}, None),
        ({"verify_grade": {"label": "unrecognized"}}, None),
        ({"verify_grade": {"unknown": 1.0}}, None),
        ({"verify_grade": {"probabilities": {"unknown": 1.0}}}, None),
        ({"verify_grade": {"type": "score", "score": 0.5}}, None),
        ({}, None),
    ],
)
async def test_legacy_grade_distributions_never_turn_unknown_levels_into_full(
    transport, body, expected
):
    decision = answer(transport, {"verify_achieved": 0.95, **body})
    result = await decision.verify_completion("saved", "saved document")
    assert result.success is expected


@pytest.mark.parametrize("distribution", [None, [], {}])
async def test_malformed_grade_distributions_are_not_empty_evidence(transport, distribution):
    decision = answer(
        transport, {"verify_achieved": 0.95, "verify_grade": {"probabilities": distribution}}
    )
    with pytest.raises(ValueError):
        await decision.verify_completion("saved", "saved document")


@pytest.mark.parametrize(
    "field,label",
    [
        ("recover_strategy", "option"),
        ("strategy", "label"),
        ("choice", "value"),
        ("top_option", "choice"),
    ],
)
async def test_legacy_recovery_labels_require_an_explicit_probability(transport, field, label):
    decision = answer(transport, {field: {label: "wait_and_retry", "probability": 0.9}})
    result = await decision.choose_recovery("not ready", "save")
    assert result.strategy == "wait_and_retry" and result.prob == 0.9 and not result.escalate


@pytest.mark.parametrize("field", ["probs", "probabilities", "options"])
async def test_legacy_recovery_distributions_choose_the_unique_top_option(transport, field):
    decision = answer(transport, {field: {"wait_and_retry": 0.1, "give_up": 0.9}})
    result = await decision.choose_recovery("cannot continue", "save")
    assert result.strategy == "give_up" and result.prob == 0.9 and not result.recoverable


@pytest.mark.parametrize(
    "body",
    [
        {"options": {}},
        {"options": {"unknown": 1.0}},
        {"options": {"wait_and_retry": 0.5, "give_up": 0.5}},
        {"recover_strategy": {"option": "unknown", "prob": 0.99}},
        {"recover_strategy": {"option": "wait_and_retry", "probs": {}}},
    ],
)
async def test_legacy_recovery_never_invents_an_option_or_breaks_a_tie(transport, body):
    with pytest.raises(ValueError):
        await answer(transport, body).choose_recovery("not ready", "save")


@pytest.mark.parametrize(
    "setting,expected",
    [
        ("off", None),
        ("false", None),
        ("mock", MockDecisionProvider),
        ("mock-decision", MockDecisionProvider),
        ("workers-ai", WorkersAiClefProvider),
        ("cloudflare", WorkersAiClefProvider),
        ("decisions", OpenRouterClefProvider),
        ("not-a-provider", None),
    ],
)
def test_factory_aliases_select_only_the_requested_provider(monkeypatch, setting, expected):
    monkeypatch.setenv("DECISION_PROVIDER", setting)
    monkeypatch.setenv("CLOUDFLARE_ACCOUNT_ID", "factory-account")
    monkeypatch.setenv("CLOUDFLARE_API_TOKEN", "factory-token")
    monkeypatch.setenv("CLEF_API_KEY", "factory-token")
    monkeypatch.delenv("CLEF_MODEL", raising=False)
    decision = create_decision_provider_from_env()
    if expected is None:
        assert decision is None
    else:
        assert isinstance(decision, expected)
        assert decision.provider_name in {"mock-decision", "clef", "clef-openrouter"}
        assert decision.model_name in {
            "mock-decision",
            "@cf/cloudflare/clef-flash",
            "cloudflare/clef-flash",
        }


@pytest.mark.parametrize("missing", ["CLOUDFLARE_ACCOUNT_ID", "CLOUDFLARE_API_TOKEN"])
def test_workers_configuration_requires_both_credentials(monkeypatch, missing):
    monkeypatch.setenv("CLOUDFLARE_ACCOUNT_ID", "factory-account")
    monkeypatch.setenv("CLOUDFLARE_API_TOKEN", "factory-token")
    monkeypatch.delenv(missing)
    with pytest.raises(ValueError):
        WorkersAiClefProvider()


async def test_mock_partial_evidence_escalates_and_malformed_thresholds_use_defaults(monkeypatch):
    for name in ("DECISION_VERIFY_HIGH", "DECISION_VERIFY_LOW", "DECISION_RECOVER_MIN"):
        monkeypatch.setenv(name, "invalid")
    decision = MockDecisionProvider()
    result = await decision.verify_completion("save report", "save document")
    assert result.success is None and result.grade == "partial"
    assert (await decision.verify_completion("save report", "save report")).success is True
    assert decision.call_count == 2


@pytest.mark.parametrize(
    "error,retries,strategy",
    [
        ("検証に失敗", 0, "replan_subtask"),
        ("起動中のため待つ", 0, "wait_and_retry"),
        ("unknown error", 3, "give_up"),
    ],
)
async def test_mock_recovery_preserves_the_failure_and_retry_policy(error, retries, strategy):
    result = await MockDecisionProvider().choose_recovery(
        error, "save document", retry_count=retries
    )
    assert result.strategy == strategy and not result.escalate
    assert result.recoverable is (strategy != "give_up")
