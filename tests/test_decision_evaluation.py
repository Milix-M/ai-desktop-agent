"""Evaluation must preserve ground truth and never promote guessed labels to evidence."""

import json
import subprocess
import sys
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from ai_desktop_agent.agent.decision.eval import (
    build_recover_cases,
    build_verify_cases,
    evaluate,
    load_manual_labels,
    load_task_records,
    run_offline,
    threshold_recommendation,
)
from ai_desktop_agent.agent.decision.provider import MockDecisionProvider
from ai_desktop_agent.agent.decision.types import (
    DecisionRecovery,
    DecisionVerify,
    EvalMetrics,
    RecoverCase,
    VerifyCase,
)
from ai_desktop_agent.server.store import StoredAction, TaskRecord

ROOT = Path(__file__).resolve().parents[1]


def task(**changes):
    record = TaskRecord(
        id="report",
        instruction="保存した表を確認する",
        state="completed",
        success=True,
        subtasks=[{"id": "sum", "description": "合計を確認する", "expected_outcome": "sum 42"}],
        actions=[
            StoredAction(
                "subtask_complete", {}, "完了を申告", True, reasoning="sum 42", confidence=0.0
            )
        ],
    ).to_dict()
    record.update(changes)
    return record


def write_task(directory, record):
    path = directory / (record["id"] + ".json")
    path.write_text(json.dumps(record), encoding="utf-8")
    return path


def test_zero_confidence_and_an_explicit_false_label_survive_the_file_roundtrip(tmp_path):
    write_task(tmp_path, task())
    labels = tmp_path / "labels.jsonl"
    labels.write_text('{"id":"report:verify:0","label":false}\n', encoding="utf-8")
    cases = build_verify_cases(load_task_records(tmp_path), load_manual_labels(labels))
    assert len(cases) == 1
    assert cases[0].confidence_hint == 0.0
    assert cases[0].label is False and cases[0].weak is False


@pytest.mark.parametrize("invalid", ["false", "true", 0, 1, None, [], {}])
def test_a_manual_verification_label_cannot_be_coerced_to_a_boolean(invalid):
    with pytest.raises(ValueError, match="label"):
        build_verify_cases([task()], {"report:verify:0": {"label": invalid}})


@pytest.mark.parametrize("invalid", [float("nan"), float("inf"), -0.1, 1.1, "0.8", None, True])
def test_untrustworthy_confidence_is_not_upgraded_to_certainty(invalid):
    record = task()
    record["actions"][0]["confidence"] = invalid
    assert build_verify_cases([record])[0].confidence_hint == 0.0


def test_legacy_history_without_confidence_retains_its_default_and_unknown_subtask():
    record = task(subtasks=[])
    del record["actions"][0]["confidence"]
    record["actions"].insert(0, {"action_type": "wait", "success": True})
    result = build_verify_cases([record])
    assert result[0].confidence_hint == 1.0
    assert result[0].expected_outcome == ""
    assert result[0].label is True and result[0].weak is True


def test_failures_after_a_completion_declaration_are_only_weak_negative_labels():
    record = task()
    record["actions"].append({"action_type": "left_click", "success": False})
    assert build_verify_cases([record])[0].label is False
    assert build_verify_cases([record])[0].weak is True


@pytest.mark.parametrize(
    "contents", ["not json", "null", "[]", "42", '{"id":"broken","actions":[null]}', "\ud800"]
)
def test_corrupt_history_is_skipped_without_losing_the_valid_record(tmp_path, contents):
    write_task(tmp_path, task())
    (tmp_path / "broken.json").write_bytes(contents.encode("utf-8", errors="surrogatepass"))
    assert [r["id"] for r in load_task_records(tmp_path)] == ["report"]


def test_excessively_nested_or_misidentified_history_is_not_evaluated(tmp_path):
    (tmp_path / "nested.json").write_text("[" * 2000 + "0" + "]" * 2000)
    write_task(tmp_path, task()).rename(tmp_path / "wrong-id.json")
    assert load_task_records(tmp_path) == []


@pytest.mark.parametrize("value", [float("nan"), float("inf"), {"nested": [0] * 2}])
def test_history_metadata_uses_the_same_finite_and_depth_limits_as_the_task_api(tmp_path, value):
    if isinstance(value, dict):
        for _ in range(70):
            value = {"nested": value}
    write_task(tmp_path, task(goal={"untrusted": value}))
    assert load_task_records(tmp_path) == []


@pytest.mark.parametrize(
    "line",
    [
        "not json",
        "null",
        "[]",
        '{"id":1,"label":false}',
        '{"id":"","label":false}',
        '{"id":"report:verify:0"}',
        '{"id":"report:verify:0","label":"false"}',
    ],
)
def test_invalid_manual_labels_fail_before_any_model_request(tmp_path, line):
    labels = tmp_path / "labels.jsonl"
    labels.write_text("\n" + line + "\n")
    provider = MockDecisionProvider()
    with pytest.raises(ValueError, match="2"):
        run_offline(tmp_path, provider, labels)
    assert provider.call_count == 0


def test_duplicate_manual_labels_cannot_silently_replace_ground_truth(tmp_path):
    labels = tmp_path / "labels.jsonl"
    labels.write_text(
        '{"id":"report:verify:0","label":true}\n{"id":"report:verify:0","label":false}\n'
    )
    with pytest.raises(ValueError, match="2"):
        load_manual_labels(labels)


def test_an_omitted_or_blank_label_file_means_only_weak_labels(tmp_path):
    assert load_manual_labels(None) == {}
    path = tmp_path / "labels.jsonl"
    path.write_text(" \n\n")
    assert load_manual_labels(path) == {}


@pytest.mark.parametrize(
    "label", ["wait_and_retry", "alternative_approach", "replan_subtask", "give_up"]
)
def test_recovery_ground_truth_is_preserved(label):
    record = task()
    record["actions"] = [None, {"success": True}, {"success": False, "error_message": "no change"}]
    result = build_recover_cases([record], {"report:recover:0": {"label": label}})
    assert len(result) == 1
    assert result[0].label == label and result[0].weak is False
    assert result[0].error_message == "no change"
    assert result[0].subtask_description == "合計を確認する"


@pytest.mark.parametrize("invalid", ["unknown", "", None, False, {}, []])
def test_an_invalid_recovery_label_is_not_changed_to_retry(invalid):
    record = task(actions=[{"success": False}])
    with pytest.raises(ValueError, match="label"):
        build_recover_cases([record], {"report:recover:0": {"label": invalid}})


@pytest.mark.parametrize("index", [-1, 20, "0", None])
def test_a_bad_subtask_index_never_selects_an_unrelated_last_subtask(index):
    result = build_recover_cases([task(current_subtask_index=index, actions=[{"success": False}])])
    assert result[0].subtask_description == ""
    assert result[0].weak is True


@pytest.mark.parametrize("success,prob,label", [(True, 0.99, False), (False, 0.01, True)])
async def test_confident_errors_are_counted_for_both_positive_and_negative_predictions(
    success, prob, label
):
    provider = MockDecisionProvider()
    provider.verify_completion = AsyncMock(
        return_value=DecisionVerify(success, prob, "full" if success else "none")
    )
    result = await evaluate(provider, [VerifyCase("v", "sum", "shown", label, weak=False)], [])
    assert result.verify_high_conf_errors == 1
    assert threshold_recommendation(result).startswith("not-usable:")


async def test_guessed_labels_alone_can_never_approve_operational_thresholds():
    provider = MockDecisionProvider()
    provider.verify_completion = AsyncMock(return_value=DecisionVerify(True, 0.99, "full"))
    provider.choose_recovery = AsyncMock(return_value=DecisionRecovery("give_up", False, 0.99))
    result = await evaluate(
        provider,
        [VerifyCase("v", "sum", "sum", True)],
        [RecoverCase("r", "limit", "stop", label="give_up")],
    )
    assert result.verify_accuracy == result.recover_accuracy == 1.0
    assert result.verify_weak == result.recover_weak == 1
    assert threshold_recommendation(result).startswith("needs-review:")


async def test_escalations_and_errors_remain_in_counts_and_costs():
    provider = MockDecisionProvider()
    provider.verify_completion = AsyncMock(
        side_effect=[
            RuntimeError("provider unavailable"),
            DecisionVerify(None, 0.7, "partial"),
            DecisionVerify(True, 0.99, "full"),
            DecisionVerify(False, 0.4, "none"),
        ]
    )
    provider.choose_recovery = AsyncMock(
        side_effect=[
            RuntimeError("provider unavailable"),
            DecisionRecovery("wait_and_retry", True, 0.3, escalate=True),
            DecisionRecovery("wait_and_retry", True, 0.9),
            DecisionRecovery("give_up", False, 0.9),
        ]
    )
    metrics = await evaluate(
        provider,
        [VerifyCase(str(i), "expected", "evidence", True, weak=False) for i in range(4)],
        [RecoverCase(str(i), "error", "task", weak=False) for i in range(4)],
        llm_input_price=2,
        llm_output_price=4,
        avg_llm_prompt_tokens=1000,
        avg_llm_completion_tokens=500,
        decision_price=0.1,
        decision_tokens=100,
    )
    assert (metrics.verify_total, metrics.verify_correct, metrics.verify_escalations) == (4, 1, 2)
    assert (metrics.recover_total, metrics.recover_correct, metrics.recover_escalations) == (
        4,
        1,
        2,
    )
    assert metrics.verify_accuracy == metrics.recover_accuracy == 0.5
    assert metrics.est_llm_cost_without_clef == pytest.approx(0.032)
    assert metrics.est_llm_remaining_cost == pytest.approx(0.016)
    assert metrics.est_total_with_clef == pytest.approx(0.01608)
    assert threshold_recommendation(metrics).startswith("needs-review:")


@pytest.mark.parametrize(
    "name",
    [
        "llm_input_price",
        "llm_output_price",
        "avg_llm_prompt_tokens",
        "avg_llm_completion_tokens",
        "decision_price",
        "decision_tokens",
    ],
)
@pytest.mark.parametrize("invalid", [-1, float("nan"), float("inf"), True, 10**400])
async def test_invalid_cost_settings_are_rejected_before_inference(name, invalid):
    provider = MockDecisionProvider()
    with pytest.raises(ValueError, match=name):
        await evaluate(provider, [], [], **{name: invalid})
    assert provider.call_count == 0


@pytest.mark.parametrize("price,tokens", [(1e308, 1e308), (10**300, 10**300)])
async def test_cost_overflow_is_rejected_before_inference_instead_of_writing_infinity(
    price, tokens
):
    provider = MockDecisionProvider()
    with pytest.raises(ValueError, match="費用"):
        await evaluate(
            provider,
            [VerifyCase("v", "sum", "sum", True)],
            [],
            llm_input_price=price,
            avg_llm_prompt_tokens=tokens,
        )
    assert provider.call_count == 0


def test_empty_dataset_reports_no_evidence_instead_of_a_success(tmp_path):
    metrics, verify, recover = run_offline(tmp_path, MockDecisionProvider())
    assert verify == recover == []
    assert metrics.verify_accuracy == metrics.recover_accuracy == 0
    assert threshold_recommendation(metrics).startswith("not-usable:")


def test_cli_cost_options_are_actually_applied_to_the_saved_report(tmp_path):
    write_task(tmp_path, task())
    report = tmp_path / "metrics.json"
    process = subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts/eval_clef.py"),
            "--tasks-dir",
            str(tmp_path),
            "--provider",
            "mock",
            "--llm-input-price",
            "100",
            "--llm-output-price",
            "200",
            "--out",
            str(report),
        ],
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert process.returncode == 0, process.stderr
    saved = json.loads(report.read_text())
    assert saved["est_without"] == pytest.approx(0.46)
    assert saved["verify_total"] == 1


def test_strong_label_results_report_a_candidate_without_claiming_production_readiness():
    metrics = EvalMetrics(verify_total=10, verify_correct=10, recover_total=10, recover_correct=10)
    verdict = threshold_recommendation(metrics)
    assert "1.00" in verdict and "実タスク" in verdict


@pytest.mark.parametrize(
    "reply",
    [
        None,
        DecisionVerify("false", 0.9, "full"),
        DecisionVerify(True, float("nan"), "full"),
        DecisionVerify(True, 0.9, "unknown"),
    ],
)
async def test_malformed_verify_responses_are_escalated_instead_of_being_scored(reply):
    provider = MockDecisionProvider()
    provider.verify_completion = AsyncMock(return_value=reply)
    metrics = await evaluate(provider, [VerifyCase("v", "expected", "evidence", True)], [])
    assert metrics.verify_total == metrics.verify_escalations == 1
    assert metrics.verify_correct == metrics.verify_high_conf_errors == 0


@pytest.mark.parametrize(
    "reply",
    [
        None,
        DecisionRecovery(None, True, 0.9),
        DecisionRecovery("unknown", True, 0.9),
        DecisionRecovery("give_up", True, 0.9),
        DecisionRecovery("give_up", False, float("inf")),
        DecisionRecovery("give_up", False, 0.9, escalate="false"),
        DecisionRecovery("give_up", "false", 0.9),
    ],
)
async def test_malformed_recovery_responses_do_not_count_as_correct_predictions(reply):
    provider = MockDecisionProvider()
    provider.choose_recovery = AsyncMock(return_value=reply)
    metrics = await evaluate(provider, [], [RecoverCase("r", "error", "task", label="give_up")])
    assert metrics.recover_total == metrics.recover_escalations == 1
    assert metrics.recover_correct == 0


def test_offline_evaluation_uses_both_manual_label_types_and_applies_zero_prices(tmp_path):
    record = task()
    record["actions"][0]["confidence"] = 1.0
    record["actions"].insert(
        0,
        {
            "action_type": "wait",
            "params": {"seconds": 1},
            "description": "画面を待つ",
            "success": False,
            "error_message": "wait for startup",
        },
    )
    write_task(tmp_path, record)
    labels = tmp_path / "labels.jsonl"
    labels.write_text(
        '{"id":"report:verify:0","label":true}\n'
        '{"id":"report:recover:0","label":"wait_and_retry"}\n'
    )
    metrics, verify, recover = run_offline(
        tmp_path,
        MockDecisionProvider(),
        labels,
        llm_input_price=0,
        llm_output_price=0,
        decision_price=0,
    )
    assert len(verify) == len(recover) == 1
    assert metrics.verify_weak == metrics.recover_weak == 0
    assert metrics.verify_correct == metrics.recover_correct == 1
    assert metrics.est_llm_cost_without_clef == metrics.est_total_with_clef == 0.0


@pytest.mark.parametrize(
    "arguments, error",
    [
        (["--labels", "missing-labels.jsonl"], "評価データ"),
        (["--llm-input-price", "nan"], "評価の設定"),
    ],
)
def test_cli_explains_invalid_data_or_prices_without_a_traceback(tmp_path, arguments, error):
    result = subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts/eval_clef.py"),
            "--tasks-dir",
            str(tmp_path),
            "--provider",
            "mock",
            *arguments,
        ],
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 2
    assert error in result.stderr and "Traceback" not in result.stderr
