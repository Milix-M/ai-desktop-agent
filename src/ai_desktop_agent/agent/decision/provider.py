"""決定モデルプロバイダ（Clef本体＋オフライン用モック）。

- MockDecisionProvider: 外部通信なしの決定的ヒューリスティック。
  評価器の基準線と単体テスト用。本物のClef精度を示すものではない。
- WorkersAiClefProvider: Cloudflare Workers AI直結でClef/clef-flashを呼ぶ。
- OpenRouterClefProvider: OpenRouter Decisions API経由でClef/clef-flashを呼ぶ。
  LLM本体とは別のAPIキー (CLEF_API_KEY優先) を使える。
  要求形式はSystem One互換（state＋型付きquestions）を前提にする。
  応答の取り回しは提供元の形式差を吸収できるよう複数形に対応し、
  解釈できなければ例外にしてLLM側に退避させる（誤判定より退避を優先）。
"""

from __future__ import annotations

import logging
import os
import re
from abc import ABC, abstractmethod
from typing import Any

from ai_desktop_agent.agent.decision import routing
from ai_desktop_agent.agent.decision.types import DecisionRecovery, DecisionVerify

logger = logging.getLogger(__name__)

#: Clef公称単価（USD/入力100万トークン。提供元申告）
CLEF_PRICE_PER_MTOKENS = 0.24
CLEF_FLASH_PRICE_PER_MTOKENS = 0.09

#: Workers AI直結用の既定モデル（RESTパス形式）
WORKERS_AI_CLEF_MODEL_DEFAULT = "@cf/cloudflare/clef-flash"
#: OpenRouter Decisions API用の既定モデル（OpenRouterモデルID形式）
OPENROUTER_CLEF_MODEL_DEFAULT = "cloudflare/clef-flash"
#: OpenRouter Decisions APIのエンドポイント（chat/completionsではない）
OPENROUTER_DECISIONS_URL = "https://openrouter.ai/api/alpha/decisions"

#: 画像制約（docs/research/clef.mdより）
MAX_IMAGES_PER_REQUEST = 4
MAX_IMAGE_MIB = 4.0

_WORD_RE = re.compile(r"[0-9A-Za-zぁ-んァ-ン一-鿿]+", re.UNICODE)


def _tokens(text: str) -> set[str]:
    return {t.lower() for t in _WORD_RE.findall(text or "") if len(t) >= 2}


def _normalize_clef_model(model: str, *, for_openrouter: bool) -> str:
    """CLEF_MODELの表記ゆれを吸収する（@cf/有無）。

    Workers AI直結は ``@cf/cloudflare/...`` 形式、
    OpenRouterは ``cloudflare/...`` 形式を要求するため相互変換する。
    """
    text = (model or "").strip()
    if for_openrouter and text.startswith("@cf/"):
        return text[len("@cf/") :]
    if not for_openrouter and text.startswith("cloudflare/"):
        return "@cf/" + text
    return text


def _openrouter_state(text: str, image_bytes: bytes | None) -> str | list[dict[str, Any] | str]:
    """Decisions API用のstateを作る。画像ありはテキスト＋image_urlパートの配列。"""
    urls = _encode_images(image_bytes)[:MAX_IMAGES_PER_REQUEST]
    if not urls:
        return text
    parts: list[dict[str, Any] | str] = [text]
    for url in urls:
        parts.append({"type": "image_url", "image_url": {"url": url}})
    return parts


class DecisionProvider(ABC):
    """決定モデルの抽象基底。完了確認と回復方針だけを担う。"""

    def __init__(self) -> None:
        self.call_count = 0

    @property
    @abstractmethod
    def provider_name(self) -> str: ...

    @property
    @abstractmethod
    def model_name(self) -> str: ...

    @abstractmethod
    async def verify_completion(
        self,
        expected_outcome: str,
        evidence_text: str,
        confidence_hint: float = 1.0,
        image_bytes: bytes | None = None,
    ) -> DecisionVerify:
        """完了確認（処理順3番目）。noul達成可否＋score度合い相当を返す。"""

    @abstractmethod
    async def choose_recovery(
        self,
        error_message: str,
        subtask_description: str,
        history_text: str = "",
        retry_count: int = 0,
    ) -> DecisionRecovery:
        """回復方針決定（処理順4番目）。choice戦略選択相当を返す。"""


class MockDecisionProvider(DecisionProvider):
    """決定的な簡易判定。通信なし。精度の基準線用。"""

    def __init__(self, model: str = "mock-decision") -> None:
        super().__init__()
        self._model = model

    @property
    def provider_name(self) -> str:
        return "mock-decision"

    @property
    def model_name(self) -> str:
        return self._model

    async def verify_completion(
        self,
        expected_outcome: str,
        evidence_text: str,
        confidence_hint: float = 1.0,
        image_bytes: bytes | None = None,
    ) -> DecisionVerify:
        self.call_count += 1
        want, have = _tokens(expected_outcome), _tokens(evidence_text)
        if expected_outcome.strip() and expected_outcome.strip() in (evidence_text or ""):
            base, grade = 0.93, "full"
        elif want and want & have:
            overlap = len(want & have) / max(1, len(want))
            base, grade = (0.93, "full") if overlap >= 0.6 else (0.72, "partial")
        else:
            base, grade = 0.25, "none"
        hint = max(0.0, min(1.0, confidence_hint))
        prob = round(base * (0.7 + 0.3 * hint), 4)
        high, low, _ = routing.load_thresholds()
        route = routing.route_verify(prob, grade, high=high, low=low)  # type: ignore[arg-type]
        success = {"achieved": True, "not_achieved": False, "escalate": None}[route]
        return DecisionVerify(
            success=success,
            prob=prob,
            grade=grade,  # type: ignore[arg-type]
            reasoning=f"mock判定: 一致度={grade} 確率={prob} 根拠=記録テキストの語彙一致",
        )

    async def choose_recovery(
        self,
        error_message: str,
        subtask_description: str,
        history_text: str = "",
        retry_count: int = 0,
    ) -> DecisionRecovery:
        self.call_count += 1
        msg = error_message or ""
        if retry_count >= 3 or any(k in msg for k in ("上限", "打ち切", "中断", "give up")):
            strategy, prob = "give_up", 0.7
        elif any(k in msg for k in ("変化", "変わ", "無変化")):
            strategy, prob = "alternative_approach", 0.8
        elif any(k in msg for k in ("未達", "期待結果", "確認できません", "検証")):
            strategy, prob = "replan_subtask", 0.75
        elif any(k in msg for k in ("待", "wait", "起動中", "時間", "安定")):
            strategy, prob = "wait_and_retry", 0.8
        else:
            strategy, prob = "wait_and_retry", 0.55
        _, _, minimum = routing.load_thresholds()
        escalate = not routing.route_recovery(prob, minimum=minimum)
        return DecisionRecovery(
            strategy=strategy,
            recoverable=routing.strategy_to_recoverable(strategy),
            prob=prob,
            reasoning=f"mock判定: 誤り文言からの対応付け 確率={prob}",
            escalate=escalate,
        )


class WorkersAiClefProvider(DecisionProvider):
    """Cloudflare Workers AI配置のClef/clef-flashを呼ぶ提供元。"""

    def __init__(
        self,
        account_id: str | None = None,
        api_token: str | None = None,
        model: str | None = None,
        timeout: float = 30.0,
    ) -> None:
        super().__init__()
        self._account_id = account_id or os.environ.get("CLOUDFLARE_ACCOUNT_ID", "")
        self._api_token = api_token or os.environ.get("CLOUDFLARE_API_TOKEN", "")
        raw_model = model or os.environ.get("CLEF_MODEL", WORKERS_AI_CLEF_MODEL_DEFAULT)
        self._model = _normalize_clef_model(raw_model, for_openrouter=False)
        self._timeout = timeout
        if not self._account_id or not self._api_token:
            raise ValueError("CLOUDFLARE_ACCOUNT_ID と CLOUDFLARE_API_TOKEN が必要です")

    @property
    def provider_name(self) -> str:
        return "clef"

    @property
    def model_name(self) -> str:
        return self._model

    def _endpoint(self) -> str:
        return (
            f"https://api.cloudflare.com/client/v4/accounts/{self._account_id}/ai/run/{self._model}"
        )

    # ── 要求組み立て ─────────────────────────────

    def build_verify_payload(
        self,
        expected_outcome: str,
        evidence_text: str,
        image_bytes: bytes | None = None,
    ) -> dict[str, Any]:
        """完了確認1件分のSystem One互換要求を作る（noul＋scoreの2問）。"""
        state = (
            f"【サブタスクの期待結果】\n{expected_outcome}\n\n"
            f"【直前の操作の判断理由】\n{evidence_text}\n"
            "重要情報は前方に置く（後方切捨て仕様のため）。"
        )
        questions: list[dict[str, Any]] = [
            {
                "id": "verify_achieved",
                "type": "noul",
                "question": "サブタスクの期待結果は達成されたか。はい/いいえで答える。",
            },
            {
                "id": "verify_grade",
                "type": "score",
                "question": "達成の度合いを順序付き基準表で採点する。",
                "rubric": ["完全達成", "部分達成", "未達"],
            },
        ]
        payload: dict[str, Any] = {"state": state, "questions": questions}
        images = _encode_images(image_bytes)
        if images:
            payload["images"] = images
        return payload

    def build_recover_payload(
        self,
        error_message: str,
        subtask_description: str,
        history_text: str = "",
        retry_count: int = 0,
    ) -> dict[str, Any]:
        """回復方針1件分のSystem One互換要求を作る（choiceの1問）。"""
        state = (
            f"【現在のサブタスク】\n{subtask_description}\n\n"
            f"【失敗内容】\n{error_message}\n再試行回数: {retry_count}\n\n"
            f"【直前の操作履歴】\n{history_text}\n"
        )
        return {
            "state": state,
            "questions": [
                {
                    "id": "recover_strategy",
                    "type": "choice",
                    "question": "次に取る回復戦略を1つ選ぶ。",
                    "options": [
                        "wait_and_retry",
                        "alternative_approach",
                        "replan_subtask",
                        "give_up",
                    ],
                }
            ],
        }

    # ── 実行 ─────────────────────────────────────

    async def _post(self, payload: dict[str, Any]) -> dict[str, Any]:
        import httpx

        async with httpx.AsyncClient(timeout=self._timeout) as client:
            resp = await client.post(
                self._endpoint(),
                headers={"Authorization": f"Bearer {self._api_token}"},
                json=payload,
            )
            resp.raise_for_status()
            data = resp.json()
        # Workers AIは {"result": {...}} 包みが通例。素の形も許す。
        if isinstance(data, dict) and isinstance(data.get("result"), dict):
            return data["result"]
        if isinstance(data, dict):
            return data
        raise ValueError(f"決定モデルの応答形式が不正: {type(data)}")

    async def verify_completion(
        self,
        expected_outcome: str,
        evidence_text: str,
        confidence_hint: float = 1.0,
        image_bytes: bytes | None = None,
    ) -> DecisionVerify:
        self.call_count += 1
        result = await self._post(
            self.build_verify_payload(expected_outcome, evidence_text, image_bytes)
        )
        prob = _extract_prob(result, ("verify_achieved", "noul", "prob_true", "p_yes"))
        grade = _extract_grade(result)
        high, low, _ = routing.load_thresholds()
        route = routing.route_verify(prob, grade, high=high, low=low)
        success = {"achieved": True, "not_achieved": False, "escalate": None}[route]
        return DecisionVerify(
            success=success,
            prob=prob,
            grade=grade,
            reasoning=f"clef判定: 確率={prob} 度合い={grade}",
        )

    async def choose_recovery(
        self,
        error_message: str,
        subtask_description: str,
        history_text: str = "",
        retry_count: int = 0,
    ) -> DecisionRecovery:
        self.call_count += 1
        result = await self._post(
            self.build_recover_payload(
                error_message, subtask_description, history_text, retry_count
            )
        )
        strategy, prob = _extract_choice(result)
        _, _, minimum = routing.load_thresholds()
        return DecisionRecovery(
            strategy=strategy,
            recoverable=routing.strategy_to_recoverable(strategy),
            prob=prob,
            reasoning=f"clef判定: 戦略={strategy} 確率={prob}",
            escalate=not routing.route_recovery(prob, minimum=minimum),
        )


class OpenRouterClefProvider(DecisionProvider):
    """OpenRouter Decisions API経由でClef/clef-flashを呼ぶ提供元。

    ``POST https://openrouter.ai/api/alpha/decisions`` に
    ``{model, state, questions}``（questionsはID key の辞書形式）を送る。
    chat/completionsエンドポイントでは決定モデルは使えないため専用実装とする。
    LLM本体とは別のAPIキーを使える（CLEF_API_KEY優先、なければOPENROUTER_API_KEY）。
    """

    def __init__(
        self,
        api_key: str | None = None,
        model: str | None = None,
        timeout: float = 30.0,
        base_url: str | None = None,
    ) -> None:
        super().__init__()
        self._api_key = (
            api_key or os.environ.get("CLEF_API_KEY") or os.environ.get("OPENROUTER_API_KEY", "")
        )
        raw_model = model or os.environ.get("CLEF_MODEL", OPENROUTER_CLEF_MODEL_DEFAULT)
        self._model = _normalize_clef_model(raw_model, for_openrouter=True)
        self._base_url = base_url or OPENROUTER_DECISIONS_URL
        self._timeout = timeout
        if not self._api_key:
            raise ValueError("CLEF_API_KEY（または OPENROUTER_API_KEY）が必要です")

    @property
    def provider_name(self) -> str:
        return "clef-openrouter"

    @property
    def model_name(self) -> str:
        return self._model

    def _endpoint(self) -> str:
        return self._base_url

    # ── 要求組み立て ─────────────────────────────

    def build_verify_payload(
        self,
        expected_outcome: str,
        evidence_text: str,
        image_bytes: bytes | None = None,
    ) -> dict[str, Any]:
        """完了確認1件分のDecisions API要求を作る（noul＋scoreの2問）。"""
        state_text = (
            f"【サブタスクの期待結果】\n{expected_outcome}\n\n"
            f"【直前の操作の判断理由】\n{evidence_text}\n"
            "重要情報は前方に置く（後方切捨て仕様のため）。"
        )
        return {
            "model": self._model,
            "state": _openrouter_state(state_text, image_bytes),
            "questions": {
                "verify_achieved": {
                    "type": "noul",
                    "instructions": "サブタスクの期待結果は達成されたか。はい/いいえで答える。",
                    "criteria": {
                        "true": "期待結果が画面や記録で確認できる",
                        "false": "期待結果が確認できない",
                    },
                },
                "verify_grade": {
                    "type": "score",
                    "instructions": "達成の度合いを順序付き基準表で採点する。",
                    "criteria": ["完全達成", "部分達成", "未達"],
                },
            },
        }

    def build_recover_payload(
        self,
        error_message: str,
        subtask_description: str,
        history_text: str = "",
        retry_count: int = 0,
    ) -> dict[str, Any]:
        """回復方針1件分のDecisions API要求を作る（choiceの1問）。"""
        state_text = (
            f"【現在のサブタスク】\n{subtask_description}\n\n"
            f"【失敗内容】\n{error_message}\n再試行回数: {retry_count}\n\n"
            f"【直前の操作履歴】\n{history_text}\n"
        )
        return {
            "model": self._model,
            "state": state_text,
            "questions": {
                "recover_strategy": {
                    "type": "choice",
                    "instructions": "次に取る回復戦略を1つ選ぶ。",
                    "criteria": {
                        "wait_and_retry": "待機して同じ操作を再試行する",
                        "alternative_approach": "別の手段で同じ目的を達成する",
                        "replan_subtask": "サブタスクを立て直す",
                        "give_up": "継続不能として打ち切る",
                    },
                }
            },
        }

    # ── 実行 ─────────────────────────────────────

    async def _post(self, payload: dict[str, Any]) -> dict[str, Any]:
        import httpx

        async with httpx.AsyncClient(timeout=self._timeout) as client:
            resp = await client.post(
                self._endpoint(),
                headers={"Authorization": f"Bearer {self._api_key}"},
                json=payload,
            )
            resp.raise_for_status()
            data = resp.json()
        # Decisions APIは {"answers": {...}, "usage": {...}} を返す。answers辞書を渡す。
        if isinstance(data, dict) and isinstance(data.get("answers"), dict):
            return data["answers"]
        if isinstance(data, dict):
            return data
        raise ValueError(f"決定モデルの応答形式が不正: {type(data)}")

    async def verify_completion(
        self,
        expected_outcome: str,
        evidence_text: str,
        confidence_hint: float = 1.0,
        image_bytes: bytes | None = None,
    ) -> DecisionVerify:
        self.call_count += 1
        result = await self._post(
            self.build_verify_payload(expected_outcome, evidence_text, image_bytes)
        )
        prob = _extract_prob(result, ("verify_achieved", "noul", "prob_true", "p_yes"))
        grade = _extract_grade(result)
        high, low, _ = routing.load_thresholds()
        route = routing.route_verify(prob, grade, high=high, low=low)
        success = {"achieved": True, "not_achieved": False, "escalate": None}[route]
        return DecisionVerify(
            success=success,
            prob=prob,
            grade=grade,
            reasoning=f"clef判定: 確率={prob} 度合い={grade}",
        )

    async def choose_recovery(
        self,
        error_message: str,
        subtask_description: str,
        history_text: str = "",
        retry_count: int = 0,
    ) -> DecisionRecovery:
        self.call_count += 1
        result = await self._post(
            self.build_recover_payload(
                error_message, subtask_description, history_text, retry_count
            )
        )
        strategy, prob = _extract_choice(result)
        _, _, minimum = routing.load_thresholds()
        return DecisionRecovery(
            strategy=strategy,
            recoverable=routing.strategy_to_recoverable(strategy),
            prob=prob,
            reasoning=f"clef判定: 戦略={strategy} 確率={prob}",
            escalate=not routing.route_recovery(prob, minimum=minimum),
        )


# ── 応答解釈（形式差の吸収） ────────────────────────


def _extract_prob(result: dict[str, Any], keys: tuple[str, ...]) -> float:
    """noul相当の達成確率を探す。見つからなければ例外（LLM退避用）。"""
    # 1. 質問ID直下の数値
    for key in keys:
        node = result.get(key)
        if isinstance(node, (int, float)):
            return max(0.0, min(1.0, float(node)))
        if isinstance(node, dict):
            for sub in ("prob_true", "p_yes", "noul", "probability", "prob", "score"):
                if isinstance(node.get(sub), (int, float)):
                    return max(0.0, min(1.0, float(node[sub])))
    # 2. questions配列形式
    questions = result.get("questions")
    if isinstance(questions, list):
        for q in questions:
            if isinstance(q, dict) and q.get("id") in keys:
                for sub in ("prob_true", "p_yes", "noul", "probability", "prob"):
                    if isinstance(q.get(sub), (int, float)):
                        return max(0.0, min(1.0, float(q[sub])))
    # 3. answers配列形式
    answers = result.get("answers")
    if isinstance(answers, list):
        for a in answers:
            if isinstance(a, dict) and a.get("id") in keys:
                for sub in ("prob_true", "p_yes", "noul", "probability", "prob"):
                    if isinstance(a.get(sub), (int, float)):
                        return max(0.0, min(1.0, float(a[sub])))
    raise ValueError("達成確率を応答から解釈できません")


def _numeric_dist(dist: dict[Any, Any]) -> dict[str, float]:
    """分布辞書から数値のみを抜く（文字列混入でのfloat変換死を防ぐ）。"""
    out: dict[str, float] = {}
    for k, v in dist.items():
        if isinstance(v, (int, float)):
            out[str(k)] = float(v)
    return out


def _best_key(dist: dict[Any, Any]) -> str | None:
    nums = _numeric_dist(dist)
    if not nums:
        return None
    return max(nums.items(), key=lambda kv: kv[1])[0]


def _grade_from_node(node: Any) -> str | None:
    """score相当ノードから度合いを推定する。Decisions APIの回答形にも対応。"""
    if isinstance(node, str):
        return _grade_from_text(node)
    if isinstance(node, dict):
        for label_key in ("label", "choice", "option", "value"):
            text = node.get(label_key)
            if isinstance(text, str):
                grade = _grade_from_text(text)
                if grade:
                    return grade
        legend = node.get("legend")
        for dist_key in ("probabilities", "probs", "scores"):
            dist = node.get(dist_key)
            if isinstance(dist, dict) and dist:
                best = _best_key(dist)
                if best is None:
                    continue
                # 連番keyの場合はlegendで見出しに戻す（実応答: {"0":完全達成,...}）
                label = best
                if isinstance(legend, dict) and str(best) in legend:
                    label = str(legend[str(best)])
                grade = _grade_from_text(label)
                if grade:
                    return grade
    return None


def _extract_grade(result: dict[str, Any]) -> str:
    for key in ("verify_grade", "grade", "top_rubric", "top"):
        grade = _grade_from_node(result.get(key))
        if grade:
            return grade
    # score分布から最有力を推定（数値のみ対象。回答ノード自体は上で処理済み）
    for key in ("verify_grade", "scores", "score"):
        node = result.get(key)
        if isinstance(node, dict) and node:
            if any(k in node for k in ("type", "probabilities", "legend")):
                continue
            best = _best_key(node)
            if best is None:
                continue
            grade = _grade_from_text(best)
            if grade:
                return grade
    # 度合い不明でも確率はある場合：中間扱いのため部分に倒す
    return "partial"


def _grade_from_text(text: str):
    if any(k in text for k in ("完全", "full", "達成済")):
        return "full"
    if any(k in text for k in ("部分", "partial")):
        return "partial"
    if any(k in text for k in ("未達", "none", "未達成", "失敗")):
        return "none"
    return None


def _extract_choice(result: dict[str, Any]) -> tuple[str, float]:
    valid = {"wait_and_retry", "alternative_approach", "replan_subtask", "give_up"}
    for key in ("recover_strategy", "strategy", "choice", "top_option"):
        node = result.get(key)
        if isinstance(node, str) and node in valid:
            return node, 0.7
        if isinstance(node, dict):
            label = (
                node.get("option") or node.get("label") or node.get("value") or node.get("choice")
            )
            dist = node.get("probabilities") or node.get("probs")
            prob = node.get("prob", node.get("probability", node.get("confidence", None)))
            if prob is None and isinstance(dist, dict) and label in dist:
                prob = dist[label]
            if not isinstance(prob, (int, float)):
                prob = 0.7
            if label in valid:
                return label, max(0.0, min(1.0, float(prob)))
    probs = result.get("probs") or result.get("probabilities") or result.get("options")
    if isinstance(probs, dict):
        known = _numeric_dist({k: v for k, v in probs.items() if k in valid})
        if known:
            best = max(known.items(), key=lambda kv: kv[1])
            return best[0], max(0.0, min(1.0, best[1]))
    raise ValueError("回復戦略を応答から解釈できません")


def _encode_images(image_bytes: bytes | None) -> list[str]:
    """画像をJPEG data URL化する。制限超過時は送らない（本文13MiB対策）。"""
    if not image_bytes:
        return []
    if len(image_bytes) > MAX_IMAGE_MIB * 1024 * 1024:
        logger.warning("決定モデル用の画像が大きすぎるため画像なしで送ります")
        return []
    try:
        import io

        from PIL import Image

        with Image.open(io.BytesIO(image_bytes)) as img:
            buf = io.BytesIO()
            img.convert("RGB").save(buf, format="JPEG", quality=80)
            raw = buf.getvalue()
    except Exception:
        raw = image_bytes
    import base64 as _b64

    return [f"data:image/jpeg;base64,{_b64.b64encode(raw).decode('ascii')}"]


def create_decision_provider_from_env() -> DecisionProvider | None:
    """環境変数から決定モデル提供元を作る。未設定/無効ならNone（従来動作）。"""
    kind = (os.environ.get("DECISION_PROVIDER") or "none").lower()
    if kind in ("", "none", "off", "false"):
        return None
    if kind in ("mock", "mock-decision"):
        return MockDecisionProvider()
    if kind in ("clef", "workers-ai", "cloudflare"):
        return WorkersAiClefProvider()
    if kind in ("clef-openrouter", "openrouter", "decisions"):
        return OpenRouterClefProvider()
    logger.warning("未知のDECISION_PROVIDER=%s のため決定モデルなしで動作", kind)
    return None
