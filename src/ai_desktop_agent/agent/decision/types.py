"""決定モデル (Clef / SystemOne) の型定義。

生成系LLMと違い、自由文を返さず確率付きの構造化判定だけを返す。
呼び出し名だけでは処理内容が分からないという指摘に応え、
各型に日本語の処理説明を持たせる。
"""

from __future__ import annotations

import dataclasses
from typing import Literal

# 完了確認の度合い（scoreの順序付き基準表に対応）
VerifyGrade = Literal["full", "partial", "none"]
#: 日本語表示：full=完全達成 / partial=部分達成 / none=未達
VERIFY_GRADE_LABELS: dict[str, str] = {
    "full": "完全達成",
    "partial": "部分達成",
    "none": "未達",
}

# 完了確認の3値判定結果
VerifyRoute = Literal["achieved", "not_achieved", "escalate"]
#: achieved=達成確定で前進 / not_achieved=未達確定で回復へ / escalate=中間でLLM再確認
VERIFY_ROUTE_LABELS: dict[str, str] = {
    "achieved": "達成確定で前進",
    "not_achieved": "未達確定で回復へ",
    "escalate": "中間のためLLMに再確認",
}


@dataclasses.dataclass
class DecisionVerify:
    """完了確認の判定結果（noul達成可否＋score度合いの統合）。"""

    success: bool | None  # True=達成 / False=未達 / None=中間でLLMへ回す
    prob: float  # 達成確率 0.0〜1.0
    grade: VerifyGrade  # 完全/部分/未達の最有力
    reasoning: str = ""  # 人間向けの判定根拠
    source: str = "decision"  # decision / llm


@dataclasses.dataclass
class DecisionRecovery:
    """回復方針の判定結果（choice戦略選択の統合）。"""

    strategy: str  # RecoveryStrategyの値（wait_and_retry等）
    recoverable: bool  # 継続可能か
    prob: float  # 最有力選択肢の確率 0.0〜1.0
    reasoning: str = ""
    escalate: bool = False  # Trueなら確信度不足でLLMへ回す


@dataclasses.dataclass
class VerifyCase:
    """評価用の完了確認ケース1件。"""

    id: str  # 由来タスクID:サブタスクID等
    expected_outcome: str  # サブタスクの期待結果
    evidence_text: str  # 判定根拠テキスト（操作理由等。画像の代替ではない）
    label: bool  # 正解ラベル True=達成
    weak: bool = True  # True=記録からの弱ラベル（要目視確認）
    confidence_hint: float = 1.0


@dataclasses.dataclass
class RecoverCase:
    """評価用の回復方針ケース1件。"""

    id: str
    error_message: str
    subtask_description: str
    history_text: str = ""
    retry_count: int = 0
    label: str = "wait_and_retry"  # 正解のRecoveryStrategy値
    weak: bool = True


@dataclasses.dataclass
class EvalMetrics:
    """評価結果の集計。"""

    verify_total: int = 0
    verify_correct: int = 0
    verify_escalations: int = 0
    verify_high_conf_errors: int = 0  # 確信度>=高閾値での誤り（閾値運用の可否材料）
    recover_total: int = 0
    recover_correct: int = 0
    recover_escalations: int = 0
    # コスト試算（USD）
    est_llm_cost_without_clef: float = 0.0
    est_decision_cost: float = 0.0
    est_llm_remaining_cost: float = 0.0

    @property
    def verify_accuracy(self) -> float:
        judged = self.verify_total - self.verify_escalations
        return (self.verify_correct / judged) if judged else 0.0

    @property
    def recover_accuracy(self) -> float:
        judged = self.recover_total - self.recover_escalations
        return (self.recover_correct / judged) if judged else 0.0

    @property
    def est_total_with_clef(self) -> float:
        return self.est_decision_cost + self.est_llm_remaining_cost
