"""番外の評価器（導入前の精度測定器）。

本番ループ外で1回だけ動かす。data/tasks/*.jsonを正解集団に変換し、
決定モデルの正解率＋確率分布＋高確信帯の誤りを見る。
高確信で外すようなら閾値運用不可→見送りが結論になる。

注意：現行のタスク記録には画面画像が残らないため、画像つき検証の
完全な再現はできない。記録由来ラベルは弱ラベル（weak=True）とし、
目視確認したJSONLラベル（--labels）で上書きできる。
"""

from __future__ import annotations

import asyncio
import json
import math
from pathlib import Path
from typing import Any

from ai_desktop_agent.agent.decision.provider import (
    CLEF_FLASH_PRICE_PER_MTOKENS,
    DecisionProvider,
)
from ai_desktop_agent.agent.decision.routing import is_probability, load_thresholds
from ai_desktop_agent.agent.decision.types import EvalMetrics, RecoverCase, VerifyCase
from ai_desktop_agent.agent.llm.types import RecoveryStrategy
from ai_desktop_agent.server.store import TaskRecord

#: Clef-flash先行評価の推奨に従い、既定はflash単価で試算する
DEFAULT_DECISION_PRICE = CLEF_FLASH_PRICE_PER_MTOKENS
#: 決定モデル1回あたりの想定入力トークン（state短文＋質問。実測で置換）
DEFAULT_DECISION_TOKENS = 800.0


def load_task_records(tasks_dir: str | Path) -> list[dict[str, Any]]:
    """data/tasks配下のJSONを読み、壊れた物は飛ばす。"""
    records = []
    for path in sorted(Path(tasks_dir).glob("*.json")):
        try:
            records.append(TaskRecord.from_file(path).to_dict())
        except (ValueError, TypeError, KeyError, AttributeError, OSError, RecursionError):
            continue
    return records


def load_manual_labels(path: str | Path | None) -> dict[str, dict[str, Any]]:
    """目視確認ラベル（JSONL: {"id":..., "label":...}）を読む。"""
    if not path:
        return {}
    out = {}
    for number, line in enumerate(Path(path).read_text(encoding="utf-8").splitlines(), 1):
        line = line.strip()
        if not line:
            continue
        try:
            item = json.loads(line)
        except (ValueError, RecursionError) as error:
            raise ValueError(f"目視ラベルの{number}行目はJSONではありません") from error
        if (
            not isinstance(item, dict)
            or not isinstance(item.get("id"), str)
            or not item["id"].strip()
            or not (
                type(item.get("label")) is bool
                or isinstance(item.get("label"), str)
                and item["label"] in RecoveryStrategy
            )
        ):
            raise ValueError(f"目視ラベルの{number}行目のidまたはlabelが不正です")
        if item["id"] in out:
            raise ValueError(f"目視ラベルの{number}行目でidが重複しています")
        out[item["id"]] = item
    return out


def build_verify_cases(
    records: list[dict[str, Any]], manual: dict[str, dict[str, Any]] | None = None
) -> list[VerifyCase]:
    """完了確認ケースを作る。subtask_complete宣言をサブタスク順に対応付ける。"""
    manual = manual or {}
    cases = []
    for rec in records:
        task_id = str(rec.get("id", "unknown"))
        subtasks = rec.get("subtasks", []) or []
        actions = rec.get("actions", []) or []
        ok_task = rec.get("success") is True
        # 完了宣言の出現順にサブタスクを割り当てる
        complete_idx = 0
        for i, a in enumerate(actions):
            if not isinstance(a, dict) or a.get("action_type") != "subtask_complete":
                continue
            sub = subtasks[complete_idx] if complete_idx < len(subtasks) else {}
            case_id = f"{task_id}:verify:{complete_idx}"
            expected = str(sub.get("expected_outcome", "") or "")
            evidence = str(a.get("reasoning", "") or "")
            label_item = manual.get(case_id)
            if label_item is not None:
                if type(label_item.get("label")) is not bool:
                    raise ValueError(f"{case_id}のlabelはtrueまたはfalseで指定してください")
                label, weak = label_item["label"], False
            else:
                # 弱ラベル：全体成功なら達成、失敗なら当該宣言以降の失敗有無で推定
                later_fail = any(
                    not b.get("success", True) for b in actions[i:] if isinstance(b, dict)
                )
                label, weak = (ok_task and not later_fail), True
            hint = a.get("confidence", 1.0)
            cases.append(
                VerifyCase(
                    id=case_id,
                    expected_outcome=expected,
                    evidence_text=evidence,
                    label=label,
                    weak=weak,
                    confidence_hint=hint if is_probability(hint) else 0.0,
                )
            )
            complete_idx += 1
    return cases


def build_recover_cases(
    records: list[dict[str, Any]], manual: dict[str, dict[str, Any]] | None = None
) -> list[RecoverCase]:
    """回復方針ケースを作る。失敗記録のある操作を抽出する。"""
    manual = manual or {}
    cases = []
    for rec in records:
        task_id = str(rec.get("id", "unknown"))
        subtasks = rec.get("subtasks", []) or []
        actions = rec.get("actions", []) or []
        idx = rec.get("current_subtask_index", 0)
        sub_desc = str(
            (subtasks[idx] or {}).get("description", "")
            if type(idx) is int and 0 <= idx < len(subtasks)
            else ""
        )
        n = 0
        for a in actions:
            if not isinstance(a, dict) or a.get("success") is not False:
                continue
            case_id = f"{task_id}:recover:{n}"
            label_item = manual.get(case_id)
            if label_item is not None:
                label = label_item.get("label")
                if not isinstance(label, str) or label not in RecoveryStrategy:
                    raise ValueError(f"{case_id}のlabelは回復戦略の名前で指定してください")
            else:
                label = "wait_and_retry"
            cases.append(
                RecoverCase(
                    id=case_id,
                    error_message=str(a.get("error_message", "") or ""),
                    subtask_description=sub_desc,
                    history_text=str(a.get("reasoning", "") or ""),
                    retry_count=0,
                    label=label,
                    weak=label_item is None,
                )
            )
            n += 1
    return cases


async def evaluate(
    provider: DecisionProvider,
    verify_cases: list[VerifyCase],
    recover_cases: list[RecoverCase],
    *,
    llm_input_price: float = 2.5,
    llm_output_price: float = 10.0,
    avg_llm_prompt_tokens: float = 4000.0,
    avg_llm_completion_tokens: float = 300.0,
    decision_price: float = DEFAULT_DECISION_PRICE,
    decision_tokens: float = DEFAULT_DECISION_TOKENS,
) -> EvalMetrics:
    """決定モデルを全ケースに適用し、精度＋コスト試算を返す。"""
    costs = {
        "llm_input_price": llm_input_price,
        "llm_output_price": llm_output_price,
        "avg_llm_prompt_tokens": avg_llm_prompt_tokens,
        "avg_llm_completion_tokens": avg_llm_completion_tokens,
        "decision_price": decision_price,
        "decision_tokens": decision_tokens,
    }
    for name, value in costs.items():
        try:
            valid = type(value) in (int, float) and math.isfinite(value) and value >= 0
        except OverflowError:
            valid = False
        if not valid:
            raise ValueError(f"{name}は有限の0以上の数で指定してください")
    calls = len(verify_cases) + len(recover_cases)
    try:
        llm_call_cost = (
            avg_llm_prompt_tokens * llm_input_price + avg_llm_completion_tokens * llm_output_price
        ) / 1_000_000
        decision_call_cost = decision_tokens * decision_price / 1_000_000
        finite = all(
            math.isfinite(value)
            for value in (
                llm_call_cost,
                decision_call_cost,
                calls * llm_call_cost,
                calls * decision_call_cost,
            )
        )
    except OverflowError:
        finite = False
    if not finite:
        raise ValueError(
            "費用の計算結果が数値の範囲を超えました。単価とトークン数を確認してください"
        )
    m = EvalMetrics()
    high, _, _ = load_thresholds()
    m.verify_total = len(verify_cases)
    m.verify_weak = sum(case.weak for case in verify_cases)
    for case in verify_cases:
        try:
            r = await provider.verify_completion(
                case.expected_outcome, case.evidence_text, case.confidence_hint
            )
            if (
                (r.success is not None and type(r.success) is not bool)
                or not is_probability(r.prob)
                or r.grade not in ("full", "partial", "none")
            ):
                raise ValueError("完了判定の応答が不正です")
        except Exception:
            m.verify_escalations += 1
            continue
        if r.success is None:
            m.verify_escalations += 1
            continue
        if r.success == case.label:
            m.verify_correct += 1
        elif (r.prob if r.success else 1 - r.prob) >= high:
            m.verify_high_conf_errors += 1
    m.recover_total = len(recover_cases)
    m.recover_weak = sum(case.weak for case in recover_cases)
    for case in recover_cases:
        try:
            r = await provider.choose_recovery(
                case.error_message, case.subtask_description, case.history_text, case.retry_count
            )
            if (
                not isinstance(r.strategy, str)
                or r.strategy not in RecoveryStrategy
                or type(r.escalate) is not bool
                or not is_probability(r.prob)
                or type(r.recoverable) is not bool
                or r.recoverable != (r.strategy != RecoveryStrategy.GIVE_UP)
            ):
                raise ValueError("回復判定の応答が不正です")
        except Exception:
            m.recover_escalations += 1
            continue
        if r.escalate:
            m.recover_escalations += 1
            continue
        if r.strategy == case.label:
            m.recover_correct += 1
    # コスト試算：置換対象のLLM呼び出し回数×平均単価と決定モデル費用の比較
    m.est_llm_cost_without_clef = calls * llm_call_cost
    m.est_decision_cost = calls * decision_call_cost
    m.est_llm_remaining_cost = (m.verify_escalations + m.recover_escalations) * llm_call_cost
    return m


def threshold_recommendation(m: EvalMetrics) -> str:
    """閾値運用の可否を一文で返す。"""
    if m.verify_total == 0 and m.recover_total == 0:
        return "not-usable: 評価ケースが0件のため判断不可。運用記録の蓄積か目視ラベルが必要"
    if m.verify_high_conf_errors > 0:
        return (
            f"not-usable: 高確信帯の誤りが{m.verify_high_conf_errors}件あるため閾値運用は不可。"
            "Clefへの切替か追加学習を検討"
        )
    weak = m.verify_weak + m.recover_weak
    if weak:
        return f"needs-review: {weak}件は履歴からの推定ラベルです。目視ラベルで再評価してください"
    if m.verify_accuracy >= 0.85 and m.recover_accuracy >= 0.7:
        return (
            f"usable: 検証精度{m.verify_accuracy:.2f}・"
            f"回復精度{m.recover_accuracy:.2f}で評価上の基準を満たします。実タスクで再評価してください"
        )
    return (
        f"needs-review: 検証精度{m.verify_accuracy:.2f}・回復精度{m.recover_accuracy:.2f}。"
        "中間帯のLLM退避率が高い場合は閾値の再調整が必要"
    )


def run_offline(
    tasks_dir: str | Path,
    provider: DecisionProvider,
    labels_path: str | Path | None = None,
    **cost_kwargs: Any,
) -> tuple[EvalMetrics, list[VerifyCase], list[RecoverCase]]:
    """同期用の簡易実行口（CLI・テスト用）。"""
    records = load_task_records(tasks_dir)
    manual = load_manual_labels(labels_path)
    verify_cases = build_verify_cases(records, manual)
    recover_cases = build_recover_cases(records, manual)
    metrics = asyncio.run(evaluate(provider, verify_cases, recover_cases, **cost_kwargs))
    return metrics, verify_cases, recover_cases
