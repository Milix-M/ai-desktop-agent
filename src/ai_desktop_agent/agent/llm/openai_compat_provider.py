"""OpenAI 互換 API 用 LLM プロバイダ実装。

OpenAI SDK を使用し、OpenAI / OpenRouter / Ollama / vLLM など
OpenAI Chat Completions 互換エンドポイント全般に対応する。

Structured Output (response_format) で LLM の出力を強制し、
JSON パースエラーを根本的に防止する。
"""

from __future__ import annotations

import base64
import json
import logging
import os
from typing import Any

from openai import AsyncOpenAI

from ai_desktop_agent.actions.primitives import Action, ActionType
from ai_desktop_agent.agent.llm.base import LLMProvider
from ai_desktop_agent.agent.llm.types import (
    ActionDecision,
    DecompositionResult,
    ErrorContext,
    RecoveryPlan,
    UnderstandingResult,
    VerificationResult,
)
from ai_desktop_agent.agent.state import ActionRecord, Goal, Subtask
from ai_desktop_agent.vm.screenshot import Screenshot

logger = logging.getLogger(__name__)

_SYSTEM_PROMPT = """\
あなたはLinuxデスクトップ（KDE Plasma）を操作するAIエージェントです。
ユーザーの指示に従い、GUI操作を自動実行します。

操作可能なアクション:
- mouse_move: カーソル移動 {x, y}
- left_click: 左クリック {x, y}
- right_click: 右クリック {x, y}
- double_click: ダブルクリック {x, y}
- drag: ドラッグ {start_x, start_y, end_x, end_y}
- scroll: スクロール {direction: "up"|"down", amount: int}
- type: テキスト入力 {text: str}
- key_press: キー押下 {key: str}
- key_combo: 複合キー {keys: [str]}
- wait: 待機 {seconds: float}
- screenshot: スクリーンショット取得 {}
- subtask_complete: サブタスク完了宣言 {}

画面解像度: 1024x768。座標は左上が (0,0)、右方向が +x、下方向が +y です。"""

# ── Structured Output JSON Schemas ─────────────────────
# OpenAI の response_format で出力を強制し、JSON パースエラーを防止する。
# ref: https://platform.openai.com/docs/guides/structured-outputs

_ACTION_TYPES = [
    "mouse_move",
    "left_click",
    "right_click",
    "double_click",
    "drag",
    "scroll",
    "type",
    "key_press",
    "key_combo",
    "wait",
    "screenshot",
    "subtask_complete",
]

_SCHEMA_UNDERSTAND = {
    "name": "understand_instruction",
    "strict": True,
    "schema": {
        "type": "object",
        "properties": {
            "intent": {
                "type": "string",
                "enum": [
                    "spreadsheet_creation",
                    "file_management",
                    "web_browsing",
                    "text_editing",
                    "system_operation",
                    "unknown",
                ],
            },
            "target_application": {
                "anyOf": [{"type": "string"}, {"type": "null"}],
            },
            "constraints": {
                "type": "array",
                "items": {"type": "string"},
            },
            "reasoning": {"type": "string"},
        },
        "required": ["intent", "target_application", "constraints", "reasoning"],
        "additionalProperties": False,
    },
}

_SCHEMA_DECOMPOSE = {
    "name": "decompose_task",
    "strict": True,
    "schema": {
        "type": "object",
        "properties": {
            "subtasks": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "id": {"type": "string"},
                        "description": {"type": "string"},
                        "expected_outcome": {"type": "string"},
                    },
                    "required": ["id", "description", "expected_outcome"],
                    "additionalProperties": False,
                },
            },
            "reasoning": {"type": "string"},
        },
        "required": ["subtasks", "reasoning"],
        "additionalProperties": False,
    },
}

_SCHEMA_ACTION = {
    "name": "action_decision",
    "strict": True,
    "schema": {
        "type": "object",
        "properties": {
            "action_type": {"type": "string", "enum": _ACTION_TYPES},
            "params": {"type": "object"},
            "expected_effect": {"type": "string"},
            "confidence": {"type": "number", "minimum": 0.0, "maximum": 1.0},
            "reasoning": {"type": "string"},
        },
        "required": [
            "action_type",
            "params",
            "expected_effect",
            "confidence",
            "reasoning",
        ],
        "additionalProperties": False,
    },
}

_SCHEMA_VERIFY = {
    "name": "verify_result",
    "strict": True,
    "schema": {
        "type": "object",
        "properties": {
            "success": {"type": "boolean"},
            "reasoning": {"type": "string"},
            "evidence": {"type": "string"},
        },
        "required": ["success", "reasoning", "evidence"],
        "additionalProperties": False,
    },
}

_SCHEMA_RECOVER = {
    "name": "recover_from_error",
    "strict": True,
    "schema": {
        "type": "object",
        "properties": {
            "strategy": {
                "type": "string",
                "enum": [
                    "wait_and_retry",
                    "alternative_approach",
                    "replan_subtask",
                    "give_up",
                ],
            },
            "actions": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "action_type": {"type": "string", "enum": _ACTION_TYPES},
                        "params": {"type": "object"},
                    },
                    "required": ["action_type", "params"],
                    "additionalProperties": False,
                },
            },
            "reasoning": {"type": "string"},
            "recoverable": {"type": "boolean"},
        },
        "required": ["strategy", "actions", "reasoning", "recoverable"],
        "additionalProperties": False,
    },
}


class OpenAICompatProvider(LLMProvider):
    """OpenAI 互換 API を使用する LLM プロバイダ。

    base_url でカスタムエンドポイント (OpenRouter / Ollama / vLLM) に接続。
    api_key と model は環境変数または引数で指定。
    Structured Output (response_format) で全メソッドの出力を強制。
    """

    def __init__(
        self,
        model: str = "gpt-4o",
        api_key: str | None = None,
        max_tokens: int = 4096,
        temperature: float = 0.0,
        base_url: str | None = None,
    ) -> None:
        self._model = model
        self._max_tokens = max_tokens
        self._temperature = temperature

        api_key = api_key or os.environ.get("OPENAI_API_KEY") or "***"
        client_kwargs: dict[str, Any] = {"api_key": api_key}
        if base_url:
            client_kwargs["base_url"] = base_url
        self._client = AsyncOpenAI(**client_kwargs)

    @property
    def provider_name(self) -> str:
        return "openai_compat"

    @property
    def model_name(self) -> str:
        return self._model

    # ── 指示理解 ───────────────────────────────────────

    async def understand_instruction(self, goal: Goal) -> UnderstandingResult:
        prompt = f"""ユーザー指示を解析し、意図・対象アプリ・制約を抽出してください。

【ユーザー指示】
{goal.description}"""
        data = await self._call(prompt, _SCHEMA_UNDERSTAND)
        return UnderstandingResult(
            intent=data.get("intent", "unknown"),
            target_application=data.get("target_application"),
            constraints=data.get("constraints", []),
            reasoning=data.get("reasoning", ""),
        )

    # ── タスク分解 ─────────────────────────────────────

    async def decompose_task(self, goal: Goal, subtask_count: int) -> DecompositionResult:
        constraints_text = ", ".join(goal.constraints) if goal.constraints else "なし"
        prompt = (
            "タスクをサブタスクに分解してください。"
            "各サブタスクは単一の操作単位にしてください。\n"
            f"\n【ゴール】\n"
            f"意図: {goal.intent}\n"
            f"説明: {goal.description}\n"
            f"対象アプリ: {goal.target_application or '指定なし'}\n"
            f"制約: {constraints_text}\n"
            f"\nサブタスクIDは step_{subtask_count + 1} からの連番で生成してください。"
        )
        data = await self._call(prompt, _SCHEMA_DECOMPOSE)
        raw_subtasks = data.get("subtasks", [])
        subtasks = [
            Subtask(
                id=st.get("id", f"step_{subtask_count + idx + 1}"),
                description=st.get("description", ""),
                expected_outcome=st.get("expected_outcome", ""),
            )
            for idx, st in enumerate(raw_subtasks)
        ]
        return DecompositionResult(subtasks=subtasks, reasoning=data.get("reasoning", ""))

    # ── アクション決定 ─────────────────────────────────

    async def decide_next_action(
        self,
        goal: Goal,
        current_subtask: Subtask,
        action_history: list[ActionRecord],
        screenshot: Screenshot,
        error_context: ErrorContext | None = None,
    ) -> ActionDecision:
        history_text = self._format_action_history(action_history[-10:])
        error_block = ""
        if error_context:
            error_block = f"""
【エラー情報】
失敗したアクション: {error_context.action.action_type.value} {error_context.action.params}
エラーメッセージ: {error_context.error_message}
再試行回数: {error_context.retry_count}
"""
        prompt = f"""現在のサブタスクに対して、次に実行すべき1つのアクションを決定してください。

【ゴール】{goal.description}
【意図】{goal.intent}
【対象アプリ】{goal.target_application or "なし"}

【現在のサブタスク】
ID: {current_subtask.id}
説明: {current_subtask.description}
期待結果: {current_subtask.expected_outcome}
{error_block}
【直近の操作履歴】
{history_text}"""
        image_bytes = screenshot.image_bytes
        data = await self._call(prompt, _SCHEMA_ACTION, image_bytes=image_bytes)
        action_type_str = data.get("action_type", "subtask_complete")
        try:
            action_type = ActionType(action_type_str)
        except ValueError:
            action_type = ActionType.SUBTASK_COMPLETE
        return ActionDecision(
            action=Action(action_type=action_type, params=data.get("params", {})),
            expected_effect=data.get("expected_effect", ""),
            confidence=float(data.get("confidence", 0.5)),
            reasoning=data.get("reasoning", ""),
        )

    # ── 結果検証 ───────────────────────────────────────

    async def verify_result(
        self, action: ActionDecision, expected_effect: str
    ) -> VerificationResult:
        prompt = f"""アクションの実行結果を検証してください。

【実行したアクション】
種別: {action.action.action_type.value}
パラメータ: {action.action.params}

【期待された効果】
{expected_effect}

【LLMの判断理由】
{action.reasoning}"""
        data = await self._call(prompt, _SCHEMA_VERIFY)
        return VerificationResult(
            success=bool(data.get("success", True)),
            reasoning=data.get("reasoning", ""),
            evidence=data.get("evidence", ""),
        )

    # ── エラー回復 ─────────────────────────────────────

    async def recover_from_error(
        self,
        error: ErrorContext,
        action_history: list[ActionRecord],
        subtask: Subtask,
    ) -> RecoveryPlan:
        history_text = self._format_action_history(action_history[-10:])
        prompt = f"""エラーからの回復計画を立案してください。

【現在のサブタスク】
ID: {subtask.id}
説明: {subtask.description}

【失敗したアクション】
種別: {error.action.action_type.value}
パラメータ: {error.action.params}
エラーメッセージ: {error.error_message}
再試行回数: {error.retry_count} / 最大 {subtask.max_retries}

【直近の操作履歴】
{history_text}"""
        data = await self._call(prompt, _SCHEMA_RECOVER)
        raw_actions = data.get("actions", [])
        recovery_actions = [
            Action(
                action_type=ActionType(a.get("action_type", "wait")),
                params=a.get("params", {}),
            )
            for a in raw_actions
        ]
        return RecoveryPlan(
            strategy=data.get("strategy", "wait_and_retry"),
            actions=recovery_actions,
            reasoning=data.get("reasoning", ""),
            recoverable=bool(data.get("recoverable", True)),
        )

    # ── 内部 ──────────────────────────────────────────

    async def _call(
        self,
        prompt: str,
        json_schema: dict[str, Any] | None = None,
        image_bytes: bytes | None = None,
    ) -> dict[str, Any]:
        """OpenAI API を呼び出し、JSON 応答を返す。

        json_schema 指定時は Structured Output で出力を強制。
        image_bytes 指定時は Vision API でマルチモーダルリクエスト。
        """
        # メッセージ構築
        if image_bytes:
            b64 = base64.b64encode(image_bytes).decode("ascii")
            user_content: Any = [
                {"type": "text", "text": prompt},
                {
                    "type": "image_url",
                    "image_url": {"url": f"data:image/png;base64,{b64}"},
                },
            ]
        else:
            user_content = prompt

        max_retries = 3
        for attempt in range(max_retries):
            try:
                kwargs: dict[str, Any] = {
                    "model": self._model,
                    "max_tokens": self._max_tokens,
                    "temperature": self._temperature,
                    "messages": [
                        {"role": "system", "content": _SYSTEM_PROMPT},
                        {"role": "user", "content": user_content},
                    ],
                }
                if json_schema:
                    kwargs["response_format"] = {
                        "type": "json_schema",
                        "json_schema": json_schema,
                    }

                response = await self._client.chat.completions.create(**kwargs)
                text = response.choices[0].message.content or ""
                if not text:
                    raise ValueError("応答が空です")

                # Structured Output 使用時は API が有効な JSON を保証する
                if json_schema:
                    return json.loads(text)  # type: ignore[no-any-return]
                return self._parse_json(text)

            except (json.JSONDecodeError, ValueError) as e:
                logger.warning(
                    "API 応答のパースに失敗 (attempt %d/%d): %s",
                    attempt + 1,
                    max_retries,
                    e,
                )
                if attempt == max_retries - 1:
                    raise
            except Exception:
                logger.exception("API 呼び出しエラー (attempt %d/%d)", attempt + 1, max_retries)
                if attempt == max_retries - 1:
                    raise
        return {}

    @staticmethod
    def _parse_json(text: str) -> dict[str, Any]:
        """LLM応答テキストからJSONを抽出してパースする（フォールバック用）。"""
        text = text.strip()
        if "```json" in text:
            start = text.index("```json") + 7
            end = text.index("```", start)
            text = text[start:end].strip()
        elif "```" in text:
            start = text.index("```") + 3
            end = text.index("```", start)
            text = text[start:end].strip()
        return json.loads(text)

    @staticmethod
    def _format_action_history(history: list[ActionRecord]) -> str:
        """アクション履歴をLLM向けテキストに整形。"""
        if not history:
            return "（履歴なし）"
        lines = []
        for i, record in enumerate(history, 1):
            status = "✓" if record.success else "✗"
            lines.append(
                f"{i}. [{status}] {record.action.action_type.value} {record.action.params}"
            )
            if record.error_message:
                lines.append(f"   エラー: {record.error_message}")
        return "\n".join(lines)
