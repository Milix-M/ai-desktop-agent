"""OpenAI 互換 API 用 LLM プロバイダ実装。

OpenAI SDK を使用し、OpenAI / OpenRouter / Ollama / vLLM など
OpenAI Chat Completions 互換エンドポイント全般に対応する。

Structured Output (response_format) で LLM の出力を強制し、
JSON パースエラーを根本的に防止する。

座標精度を向上させるため、スクリーンショットには以下の視覚的ヒントが
重畳されている（VNCClient.capture_screen にて自動付与）：
  - 50px 間隔のグリッド線（100px ごとに太線）
  - 上端・左端の座標マーカー数字
  - 緑色のカーソル位置十字（現在のマウス位置）
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
    RecoveryStrategy,
    UnderstandingResult,
    VerificationResult,
)
from ai_desktop_agent.agent.state import ActionRecord, Goal, Subtask
from ai_desktop_agent.vm.screenshot import Screenshot

logger = logging.getLogger(__name__)

# ========================================================================
# システムプロンプト — 画面の見方と操作のルール
# ========================================================================

_SYSTEM_PROMPT = """あなたは Linux デスクトップ（KDE Plasma, 1024x768）を遠隔操作する
AI エージェントです。
ユーザーの指示に従い、GUI 操作を自律実行します。

# 画面の見方

スクリーンショットには以下の視覚的補助が描画されています：
- **赤いグリッド線**: 50px 間隔の細線（100px ごとにやや太い線）
- **座標マーカー**: 上端に X 座標、左端に Y 座標の数字（100px 間隔）
  - 例: 上端に「300」、左端に「200」とあれば、その交点付近が (300, 200)
- **緑の十字**: 現在のマウスカーソル位置
- **赤枠**: 画面の外周

# 座標の読み取り方

1. まず画面上端の数字を見て、ターゲットの X 座標を読む
2. 左端の数字を見て、ターゲットの Y 座標を読む
3. グリッド線を基準に、マーカーの間を比例補間する
4. 精度を上げたい場合は region_select で拡大表示を要求する

座標は左上が (0, 0)、右方向が +x、下方向が +y です。

# 2段階精密クリック戦略（重要）

ボタンや小さな UI をクリックする際は、以下の 2 段階を使い分けてください：

**【直接クリック】（確信度 0.8 以上の場合のみ）**
- 大きなボタン、ウィンドウタイトルバー、デスクトップアイコンなど
- 座標マーカーから自信を持って位置が特定できる場合
- 例: 上端「400」付近、左端「300」付近のボタン → left_click {x: 400, y: 300}

**【region_select → 精密クリック】（確信度が低い場合）**
- 小さいボタン、テキスト入力欄、チェックボックス、メニュー項目
- 座標マーカーだけでは自信がない場合
- ターゲット周辺を含む領域（100〜300px 四方）を region_select で要求
- 返される拡大画像で正確な座標を読んでから left_click する

# 操作可能なアクション

- mouse_move: {x: int, y: int} — カーソル移動
- left_click: {x: int, y: int} — 左クリック
- right_click: {x: int, y: int} — 右クリック
- double_click: {x: int, y: int} — ダブルクリック
- drag: {start_x: int, start_y: int, end_x: int, end_y: int} — ドラッグ
- scroll: {direction: "up"|"down", amount: int} — スクロール
- type: {text: str} — テキスト入力
- key_press: {key: str} — キー押下（enter, escape, tab 等）
- key_combo: {keys: [str]} — 複合キー（["ctrl", "c"] 等）
- wait: {seconds: float} — 待機
- screenshot: {} — 画面再撮影
- region_select: {x: int, y: int, width: int, height: int} — 領域拡大を要求
- subtask_complete: {} — 現在のサブタスク完了

# 重要なルール

1. **画面を見てから判断する**：推測でクリックしない。必ず座標マーカーを確認する。
2. **小さいターゲットは region_select を使う**：確信度 0.7 未満なら拡大表示を要求する。
3. **アクション後は画面変化を待つ**：クリック後は 0.5〜1.0 秒 wait する。
4. **失敗したら別の方法を試す**：同じ座標を連続クリックしない。
5. **confidence は正直に**：自信がないのに 0.9 以上を付けない。

画面解像度: 1024x768。座標は左上が (0,0)、右方向が +x、下方向が +y です。"""

# ========================================================================
# Structured Output JSON Schemas
# ========================================================================

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
    "region_select",
    "subtask_complete",
]

_SCHEMA_ACTION = {
    "name": "action_decision",
    "schema": {
        "oneOf": [
            {
                "type": "object",
                "properties": {
                    "action_type": {"const": "left_click"},
                    "params": {
                        "type": "object",
                        "properties": {"x": {"type": "integer"}, "y": {"type": "integer"}},
                        "required": ["x", "y"],
                        "additionalProperties": False,
                    },
                    "expected_effect": {"type": "string"},
                    "confidence": {"type": "number", "minimum": 0.0, "maximum": 1.0},
                    "reasoning": {"type": "string"},
                },
                "required": ["action_type", "params", "expected_effect", "confidence", "reasoning"],
                "additionalProperties": False,
            },
            {
                "type": "object",
                "properties": {
                    "action_type": {"const": "right_click"},
                    "params": {
                        "type": "object",
                        "properties": {"x": {"type": "integer"}, "y": {"type": "integer"}},
                        "required": ["x", "y"],
                        "additionalProperties": False,
                    },
                    "expected_effect": {"type": "string"},
                    "confidence": {"type": "number", "minimum": 0.0, "maximum": 1.0},
                    "reasoning": {"type": "string"},
                },
                "required": ["action_type", "params", "expected_effect", "confidence", "reasoning"],
                "additionalProperties": False,
            },
            {
                "type": "object",
                "properties": {
                    "action_type": {"const": "double_click"},
                    "params": {
                        "type": "object",
                        "properties": {"x": {"type": "integer"}, "y": {"type": "integer"}},
                        "required": ["x", "y"],
                        "additionalProperties": False,
                    },
                    "expected_effect": {"type": "string"},
                    "confidence": {"type": "number", "minimum": 0.0, "maximum": 1.0},
                    "reasoning": {"type": "string"},
                },
                "required": ["action_type", "params", "expected_effect", "confidence", "reasoning"],
                "additionalProperties": False,
            },
            {
                "type": "object",
                "properties": {
                    "action_type": {"const": "mouse_move"},
                    "params": {
                        "type": "object",
                        "properties": {"x": {"type": "integer"}, "y": {"type": "integer"}},
                        "required": ["x", "y"],
                        "additionalProperties": False,
                    },
                    "expected_effect": {"type": "string"},
                    "confidence": {"type": "number", "minimum": 0.0, "maximum": 1.0},
                    "reasoning": {"type": "string"},
                },
                "required": ["action_type", "params", "expected_effect", "confidence", "reasoning"],
                "additionalProperties": False,
            },
            {
                "type": "object",
                "properties": {
                    "action_type": {"const": "drag"},
                    "params": {
                        "type": "object",
                        "properties": {
                            "start_x": {"type": "integer"},
                            "start_y": {"type": "integer"},
                            "end_x": {"type": "integer"},
                            "end_y": {"type": "integer"},
                        },
                        "required": ["start_x", "start_y", "end_x", "end_y"],
                        "additionalProperties": False,
                    },
                    "expected_effect": {"type": "string"},
                    "confidence": {"type": "number", "minimum": 0.0, "maximum": 1.0},
                    "reasoning": {"type": "string"},
                },
                "required": ["action_type", "params", "expected_effect", "confidence", "reasoning"],
                "additionalProperties": False,
            },
            {
                "type": "object",
                "properties": {
                    "action_type": {"const": "scroll"},
                    "params": {
                        "type": "object",
                        "properties": {
                            "direction": {"type": "string", "enum": ["up", "down"]},
                            "amount": {"type": "integer"},
                        },
                        "required": ["direction", "amount"],
                        "additionalProperties": False,
                    },
                    "expected_effect": {"type": "string"},
                    "confidence": {"type": "number", "minimum": 0.0, "maximum": 1.0},
                    "reasoning": {"type": "string"},
                },
                "required": ["action_type", "params", "expected_effect", "confidence", "reasoning"],
                "additionalProperties": False,
            },
            {
                "type": "object",
                "properties": {
                    "action_type": {"const": "type"},
                    "params": {
                        "type": "object",
                        "properties": {"text": {"type": "string"}},
                        "required": ["text"],
                        "additionalProperties": False,
                    },
                    "expected_effect": {"type": "string"},
                    "confidence": {"type": "number", "minimum": 0.0, "maximum": 1.0},
                    "reasoning": {"type": "string"},
                },
                "required": ["action_type", "params", "expected_effect", "confidence", "reasoning"],
                "additionalProperties": False,
            },
            {
                "type": "object",
                "properties": {
                    "action_type": {"const": "key_press"},
                    "params": {
                        "type": "object",
                        "properties": {"key": {"type": "string"}},
                        "required": ["key"],
                        "additionalProperties": False,
                    },
                    "expected_effect": {"type": "string"},
                    "confidence": {"type": "number", "minimum": 0.0, "maximum": 1.0},
                    "reasoning": {"type": "string"},
                },
                "required": ["action_type", "params", "expected_effect", "confidence", "reasoning"],
                "additionalProperties": False,
            },
            {
                "type": "object",
                "properties": {
                    "action_type": {"const": "key_combo"},
                    "params": {
                        "type": "object",
                        "properties": {"keys": {"type": "array", "items": {"type": "string"}}},
                        "required": ["keys"],
                        "additionalProperties": False,
                    },
                    "expected_effect": {"type": "string"},
                    "confidence": {"type": "number", "minimum": 0.0, "maximum": 1.0},
                    "reasoning": {"type": "string"},
                },
                "required": ["action_type", "params", "expected_effect", "confidence", "reasoning"],
                "additionalProperties": False,
            },
            {
                "type": "object",
                "properties": {
                    "action_type": {"const": "wait"},
                    "params": {
                        "type": "object",
                        "properties": {"seconds": {"type": "number"}},
                        "required": ["seconds"],
                        "additionalProperties": False,
                    },
                    "expected_effect": {"type": "string"},
                    "confidence": {"type": "number", "minimum": 0.0, "maximum": 1.0},
                    "reasoning": {"type": "string"},
                },
                "required": ["action_type", "params", "expected_effect", "confidence", "reasoning"],
                "additionalProperties": False,
            },
            {
                "type": "object",
                "properties": {
                    "action_type": {"const": "region_select"},
                    "params": {
                        "type": "object",
                        "properties": {
                            "x": {"type": "integer"},
                            "y": {"type": "integer"},
                            "width": {"type": "integer"},
                            "height": {"type": "integer"},
                        },
                        "required": ["x", "y", "width", "height"],
                        "additionalProperties": False,
                    },
                    "expected_effect": {"type": "string"},
                    "confidence": {"type": "number", "minimum": 0.0, "maximum": 1.0},
                    "reasoning": {"type": "string"},
                },
                "required": ["action_type", "params", "expected_effect", "confidence", "reasoning"],
                "additionalProperties": False,
            },
            {
                "type": "object",
                "properties": {
                    "action_type": {"const": "screenshot"},
                    "params": {
                        "type": "object",
                        "properties": {},
                        "required": [],
                        "additionalProperties": False,
                    },
                    "expected_effect": {"type": "string"},
                    "confidence": {"type": "number", "minimum": 0.0, "maximum": 1.0},
                    "reasoning": {"type": "string"},
                },
                "required": ["action_type", "params", "expected_effect", "confidence", "reasoning"],
                "additionalProperties": False,
            },
            {
                "type": "object",
                "properties": {
                    "action_type": {"const": "subtask_complete"},
                    "params": {
                        "type": "object",
                        "properties": {},
                        "required": [],
                        "additionalProperties": False,
                    },
                    "expected_effect": {"type": "string"},
                    "confidence": {"type": "number", "minimum": 0.0, "maximum": 1.0},
                    "reasoning": {"type": "string"},
                },
                "required": ["action_type", "params", "expected_effect", "confidence", "reasoning"],
                "additionalProperties": False,
            },
        ]
    },
}


_SCHEMA_UNDERSTAND = {
    "name": "understand_instruction",
    "strict": True,
    "schema": {
        "type": "object",
        "properties": {
            "intent": {
                "type": "string",
                "enum": [
                    "open_application",
                    "close_application",
                    "web_browsing",
                    "text_editing",
                    "file_management",
                    "system_operation",
                    "spreadsheet_creation",
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
    "schema": {
        "type": "object",
        "properties": {
            "strategy": {
                "type": "string",
                "enum": ["wait_and_retry", "alternative_approach", "replan_subtask", "give_up"],
            },
            "actions": {
                "type": "array",
                "items": {
                    "oneOf": [
                        {
                            "type": "object",
                            "properties": {
                                "action_type": {"const": "left_click"},
                                "params": {
                                    "type": "object",
                                    "properties": {
                                        "x": {"type": "integer"},
                                        "y": {"type": "integer"},
                                    },
                                    "required": ["x", "y"],
                                    "additionalProperties": False,
                                },
                            },
                            "required": ["action_type", "params"],
                            "additionalProperties": False,
                        },
                        {
                            "type": "object",
                            "properties": {
                                "action_type": {"const": "right_click"},
                                "params": {
                                    "type": "object",
                                    "properties": {
                                        "x": {"type": "integer"},
                                        "y": {"type": "integer"},
                                    },
                                    "required": ["x", "y"],
                                    "additionalProperties": False,
                                },
                            },
                            "required": ["action_type", "params"],
                            "additionalProperties": False,
                        },
                        {
                            "type": "object",
                            "properties": {
                                "action_type": {"const": "double_click"},
                                "params": {
                                    "type": "object",
                                    "properties": {
                                        "x": {"type": "integer"},
                                        "y": {"type": "integer"},
                                    },
                                    "required": ["x", "y"],
                                    "additionalProperties": False,
                                },
                            },
                            "required": ["action_type", "params"],
                            "additionalProperties": False,
                        },
                        {
                            "type": "object",
                            "properties": {
                                "action_type": {"const": "mouse_move"},
                                "params": {
                                    "type": "object",
                                    "properties": {
                                        "x": {"type": "integer"},
                                        "y": {"type": "integer"},
                                    },
                                    "required": ["x", "y"],
                                    "additionalProperties": False,
                                },
                            },
                            "required": ["action_type", "params"],
                            "additionalProperties": False,
                        },
                        {
                            "type": "object",
                            "properties": {
                                "action_type": {"const": "drag"},
                                "params": {
                                    "type": "object",
                                    "properties": {
                                        "start_x": {"type": "integer"},
                                        "start_y": {"type": "integer"},
                                        "end_x": {"type": "integer"},
                                        "end_y": {"type": "integer"},
                                    },
                                    "required": ["start_x", "start_y", "end_x", "end_y"],
                                    "additionalProperties": False,
                                },
                            },
                            "required": ["action_type", "params"],
                            "additionalProperties": False,
                        },
                        {
                            "type": "object",
                            "properties": {
                                "action_type": {"const": "scroll"},
                                "params": {
                                    "type": "object",
                                    "properties": {
                                        "direction": {"type": "string", "enum": ["up", "down"]},
                                        "amount": {"type": "integer"},
                                    },
                                    "required": ["direction", "amount"],
                                    "additionalProperties": False,
                                },
                            },
                            "required": ["action_type", "params"],
                            "additionalProperties": False,
                        },
                        {
                            "type": "object",
                            "properties": {
                                "action_type": {"const": "type"},
                                "params": {
                                    "type": "object",
                                    "properties": {"text": {"type": "string"}},
                                    "required": ["text"],
                                    "additionalProperties": False,
                                },
                            },
                            "required": ["action_type", "params"],
                            "additionalProperties": False,
                        },
                        {
                            "type": "object",
                            "properties": {
                                "action_type": {"const": "key_press"},
                                "params": {
                                    "type": "object",
                                    "properties": {"key": {"type": "string"}},
                                    "required": ["key"],
                                    "additionalProperties": False,
                                },
                            },
                            "required": ["action_type", "params"],
                            "additionalProperties": False,
                        },
                        {
                            "type": "object",
                            "properties": {
                                "action_type": {"const": "key_combo"},
                                "params": {
                                    "type": "object",
                                    "properties": {
                                        "keys": {"type": "array", "items": {"type": "string"}}
                                    },
                                    "required": ["keys"],
                                    "additionalProperties": False,
                                },
                            },
                            "required": ["action_type", "params"],
                            "additionalProperties": False,
                        },
                        {
                            "type": "object",
                            "properties": {
                                "action_type": {"const": "wait"},
                                "params": {
                                    "type": "object",
                                    "properties": {"seconds": {"type": "number"}},
                                    "required": ["seconds"],
                                    "additionalProperties": False,
                                },
                            },
                            "required": ["action_type", "params"],
                            "additionalProperties": False,
                        },
                        {
                            "type": "object",
                            "properties": {
                                "action_type": {"const": "region_select"},
                                "params": {
                                    "type": "object",
                                    "properties": {
                                        "x": {"type": "integer"},
                                        "y": {"type": "integer"},
                                        "width": {"type": "integer"},
                                        "height": {"type": "integer"},
                                    },
                                    "required": ["x", "y", "width", "height"],
                                    "additionalProperties": False,
                                },
                            },
                            "required": ["action_type", "params"],
                            "additionalProperties": False,
                        },
                        {
                            "type": "object",
                            "properties": {
                                "action_type": {"const": "screenshot"},
                                "params": {
                                    "type": "object",
                                    "properties": {},
                                    "required": [],
                                    "additionalProperties": False,
                                },
                            },
                            "required": ["action_type", "params"],
                            "additionalProperties": False,
                        },
                        {
                            "type": "object",
                            "properties": {
                                "action_type": {"const": "subtask_complete"},
                                "params": {
                                    "type": "object",
                                    "properties": {},
                                    "required": [],
                                    "additionalProperties": False,
                                },
                            },
                            "required": ["action_type", "params"],
                            "additionalProperties": False,
                        },
                    ]
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

    # ── 指示理解 ─────────────────────────────────

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

    # ── タスク分解 ───────────────────────────────

    async def decompose_task(self, goal: Goal, subtask_count: int) -> DecompositionResult:
        from ai_desktop_agent.agent.state import Subtask

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

    # ── アクション決定（コア！）───────────────────

    async def decide_next_action(
        self,
        goal: Goal,
        current_subtask: Subtask,
        action_history: list[ActionRecord],
        screenshot: Screenshot,
        error_context: ErrorContext | None = None,
        *,
        is_zoomed: bool = False,
        zoom_origin: tuple[int, int] | None = None,
    ) -> ActionDecision:
        """現在の画面とコンテキストから次に実行すべきアクションを決定する。

        Args:
            goal: ユーザーのゴール。
            current_subtask: 現在のサブタスク。
            action_history: 操作履歴。
            screenshot: 現在の画面（オーバーレイ付き）。
            error_context: エラー回復中の場合のエラー情報。
            is_zoomed: このスクリーンショットが拡大表示かどうか。
            zoom_origin: 拡大表示の場合、元画面での左上座標 (x, y)。
        """
        history_text = self._format_action_history(action_history[-10:])

        error_block = ""
        if error_context:
            error_block = f"""
【エラー情報】
失敗したアクション: {error_context.action.action_type.value} {error_context.action.params}
エラーメッセージ: {error_context.error_message}
再試行回数: {error_context.retry_count}
"""

        zoom_block = ""
        if is_zoomed and zoom_origin:
            zx, zy = zoom_origin
            zoom_block = f"""
【拡大表示モード】
この画像は元の画面の領域 ({zx}, {zy}) を起点とする拡大表示です。
画像内の座標は領域内の相対座標です。
たとえば画像内の (50, 30) は元画面の ({zx + 50}, {zy + 30}) に相当します。
精密なクリック座標を画像から直接読み取ってください。
"""

        prompt = f"""現在のサブタスクに対して、次に実行すべき1つのアクションを決定してください。
{zoom_block}
【ゴール】{goal.description}
【意図】{goal.intent}
【対象アプリ】{goal.target_application or "なし"}

【現在のサブタスク】
ID: {current_subtask.id}
説明: {current_subtask.description}
期待結果: {current_subtask.expected_outcome}
{error_block}
【直前の操作履歴】
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

    # ── 結果検証 ─────────────────────────────────

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

    # ── エラー回復 ───────────────────────────────

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

【直前の操作履歴】
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
            strategy=RecoveryStrategy(data.get("strategy", "wait_and_retry")),
            actions=recovery_actions,
            reasoning=data.get("reasoning", ""),
            recoverable=bool(data.get("recoverable", True)),
        )

    # ── 内部 ──────────────────────────────────────

    async def _call(
        self,
        prompt: str,
        json_schema: dict[str, Any] | None = None,
        image_bytes: bytes | None = None,
    ) -> dict[str, Any]:
        """OpenAI API を呼び出し、JSON 応答を返す。"""
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
        """操作履歴をLLM向けテキストに整形。"""
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
