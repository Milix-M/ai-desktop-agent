#!/usr/bin/env python3
"""番外の評価器CLI（導入前の精度測定器）。

使い方:
    uv run python scripts/eval_clef.py --tasks-dir data/tasks --provider mock
    uv run python scripts/eval_clef.py --tasks-dir data/tasks --provider clef
    uv run python scripts/eval_clef.py --tasks-dir data/tasks --labels labels.jsonl
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from ai_desktop_agent.agent.decision.eval import (  # noqa: E402
    build_recover_cases,
    build_verify_cases,
    evaluate,
    load_manual_labels,
    load_task_records,
    threshold_recommendation,
)
from ai_desktop_agent.agent.decision.provider import (  # noqa: E402
    MockDecisionProvider,
    OpenRouterClefProvider,
    WorkersAiClefProvider,
)


def main() -> int:
    parser = argparse.ArgumentParser(description="Clef導入前の精度測定器")
    parser.add_argument("--tasks-dir", default="data/tasks")
    parser.add_argument("--provider", choices=["mock", "clef", "clef-openrouter"], default="mock")
    parser.add_argument("--labels", default=None, help="目視確認ラベルのJSONL")
    parser.add_argument("--out", default=None, help="結果JSONの保存先")
    parser.add_argument("--llm-input-price", type=float, default=2.5)
    parser.add_argument("--llm-output-price", type=float, default=10.0)
    args = parser.parse_args()

    records = load_task_records(args.tasks_dir)
    manual = load_manual_labels(args.labels)
    verify_cases = build_verify_cases(records, manual)
    recover_cases = build_recover_cases(records, manual)
    try:
        if args.provider == "mock":
            provider = MockDecisionProvider()
        elif args.provider == "clef":
            provider = WorkersAiClefProvider()
        else:
            provider = OpenRouterClefProvider()
    except ValueError as e:
        print(f"提供元の初期化に失敗: {e}", file=sys.stderr)
        return 2
    metrics = asyncio.run(evaluate(provider, verify_cases, recover_cases))

    verdict = threshold_recommendation(metrics)
    print(f"records={len(records)} verify={len(verify_cases)} recover={len(recover_cases)}")
    print(
        f"verify: 正解率={metrics.verify_accuracy:.2f} "
        f"(正={metrics.verify_correct}/{metrics.verify_total} 退避={metrics.verify_escalations} "
        f"高確信誤り={metrics.verify_high_conf_errors})"
    )
    print(
        f"recover: 正解率={metrics.recover_accuracy:.2f} "
        f"(正={metrics.recover_correct}/{metrics.recover_total} 退避={metrics.recover_escalations})"
    )
    print(
        f"cost: Clefなし=${metrics.est_llm_cost_without_clef:.4f} "
        f"Clefあり=${metrics.est_total_with_clef:.4f} "
        f"(決定=${metrics.est_decision_cost:.4f} 残LLM=${metrics.est_llm_remaining_cost:.4f})"
    )
    print(f"判定: {verdict}")
    weak = sum(1 for c in verify_cases + recover_cases if c.weak)
    if weak:
        print(f"注意: {weak}件は記録由来の弱ラベル。--labelsで目視ラベルを追加推奨")

    if args.out:
        Path(args.out).write_text(
            json.dumps(
                {
                    "records": len(records),
                    "verify_total": metrics.verify_total,
                    "verify_correct": metrics.verify_correct,
                    "verify_escalations": metrics.verify_escalations,
                    "verify_high_conf_errors": metrics.verify_high_conf_errors,
                    "verify_accuracy": metrics.verify_accuracy,
                    "recover_total": metrics.recover_total,
                    "recover_correct": metrics.recover_correct,
                    "recover_escalations": metrics.recover_escalations,
                    "recover_accuracy": metrics.recover_accuracy,
                    "est_without": metrics.est_llm_cost_without_clef,
                    "est_with": metrics.est_total_with_clef,
                    "verdict": verdict,
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
