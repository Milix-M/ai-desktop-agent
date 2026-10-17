"""閾値ルーティング（確率→3値判定）。

完了確認は高確信で前進・低確信で回復・中間でLLM検証に回す。
回復方針は確信度が足りなければLLMに回す。
"""

from __future__ import annotations

import os

from ai_desktop_agent.agent.decision.types import VerifyGrade, VerifyRoute

#: 既定閾値（docs/research/clef.mdの「0.9以上自動実行/0.6-0.9保留/未満上申」）
VERIFY_HIGH_DEFAULT = 0.9
VERIFY_LOW_DEFAULT = 0.6
RECOVER_MIN_DEFAULT = 0.6


def load_thresholds() -> tuple[float, float, float]:
    """環境変数から閾値を読む（DECISION_VERIFY_HIGH/LOW, DECISION_RECOVER_MIN）。"""

    def _f(name: str, default: float) -> float:
        try:
            return float(os.environ.get(name, str(default)))
        except ValueError:
            return default

    return (
        _f("DECISION_VERIFY_HIGH", VERIFY_HIGH_DEFAULT),
        _f("DECISION_VERIFY_LOW", VERIFY_LOW_DEFAULT),
        _f("DECISION_RECOVER_MIN", RECOVER_MIN_DEFAULT),
    )


def route_verify(prob_true: float, grade: VerifyGrade, *, high: float, low: float) -> VerifyRoute:
    """完了確認の確率＋度合いを3値に振り分ける（処理順3番目）。

    - achieved: 達成確率が高く度合いも完全 → そのまま前進
    - not_achieved: 達成確率が低いか度合いが未達 → 回復へ
    - escalate: 中間 → LLMに再確認
    """
    prob_true = max(0.0, min(1.0, prob_true))
    if grade == "none" or prob_true < low:
        return "not_achieved"
    if prob_true >= high and grade == "full":
        return "achieved"
    return "escalate"


def route_recovery(top_prob: float, *, minimum: float) -> bool:
    """回復方針の最有力確率が採用可能かを返す。FalseならLLMへ回す。"""
    return top_prob >= minimum


def strategy_to_recoverable(strategy: str) -> bool:
    """戦略名→継続可否。give_upだけが継続不能。"""
    return strategy != "give_up"
