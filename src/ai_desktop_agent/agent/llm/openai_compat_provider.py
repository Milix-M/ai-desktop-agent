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
- wait: {seconds: float} — 待機（アプリ起動待ちには2〜5秒を使う）
- region_select: {x: int, y: int, width: int, height: int} — 領域拡大を要求
- subtask_complete: {} — 現在のサブタスク完了

※ 毎ターン最新の画面が自動で送られるため、画面再取得のためのアクションは不要。
   アプリの起動を待つ場合は wait を使う。

# 重要なルール

1. **画面を見てから判断する**：推測でクリックしない。必ず座標マーカーを確認する。
2. **小さいターゲットは region_select を使う**：確信度 0.7 未満なら拡大表示を要求する。
3. **アクション後は画面変化を待つ**：クリック後は 0.5〜1.0 秒 wait する。
4. **失敗したら別の方法を試す**：同じ座標を連続クリックしない。
5. **confidence は正直に**：自信がないのに 0.9 以上を付けない。
6. **subtask_complete は操作の後に**：そのサブタスクで1つも操作せずに完了宣言すると
   システムに却下される。まずクリック・入力・キー操作・待機のいずれかを実行し、
   期待結果が画面に現れたことを確認してから完了を宣言する。
7. **アプリ起動はキーボード優先**：タスクバーの小さいアイコンへの直接クリックは
   誤爆しやすいため最後の手段にする。第一選択は Alt+F2（ランナー）→
   アプリ名を type → Enter。ランナーが使えない場合のみアイコンを使う。
8. **アプリが無い場合は導入する**：起動を試みて見つからない・起動しない場合、
   Ctrl+Alt+T で端末を開き `which <アプリ名>` で確認する。無ければ
   `sudo apt install -y <pkg>` か `sudo snap install <snap>` で導入する
   （sudo はパスワード不要）。導入後は改めて起動し、画面で確認する。
   ネットワーク不可等で導入できない場合は、試した手順を reasoning に残した上で
   完了を宣言する（推測での完了宣言はしない）。

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
        default_headers: dict[str, str] | None = None,
        session_id: str | None = None,
    ) -> None:
        self._model = model
        self._max_tokens = max_tokens
        self._temperature = temperature
        # 会話単位ID（OpenCode Go の x-opencode-session 用）。
        # タスクごとに TaskSession が設定する。
        self.session_id = session_id

        api_key = api_key or os.environ.get("OPENAI_API_KEY") or "***"
        client_kwargs: dict[str, Any] = {"api_key": api_key}
        if base_url:
            client_kwargs["base_url"] = base_url
        if default_headers:
            client_kwargs["default_headers"] = default_headers
        self._client = AsyncOpenAI(**client_kwargs)
        # structured output (response_format) が使えるかどうか。
        # 非対応エラーが出たら False に倒し、通常JSONモードで続行する。
        self._structured_output = True

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
        zoom_scale: float = 1.0,
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
            zoom_scale: 拡大表示の場合の倍率。LLMには拡大後ピクセル座標で
                返させ、呼び出し側で ``origin + coord / scale`` に戻す。
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
この画像は元の画面の領域 ({zx}, {zy}) を {zoom_scale:.2f} 倍に拡大したものです。
画像には相対座標のグリッドが描画されています。ターゲットの位置を
この拡大画像上のピクセル座標（左上原点）でそのまま返してください。
元画面への逆変換（origin + coord / scale）はシステムが行います。
画像外の座標や負の座標は返さないでください。
"""

        screen_block = f"""
【画面情報】
解像度: {screenshot.width}x{screenshot.height}
有効な座標範囲: 0 <= x < {screenshot.width}, 0 <= y < {screenshot.height}
範囲外の座標は実行時にクランプされますが、精度が落ちるため範囲内に収めてください。
"""

        prompt = f"""現在のサブタスクに対して、次に実行すべき1つのアクションを決定してください。
{zoom_block}
{screen_block}
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
        params = self._sanitize_params(action_type, data.get("params", {}))
        params = self._clamp_params(action_type, params, screenshot)
        return ActionDecision(
            action=Action(action_type=action_type, params=params),
            expected_effect=data.get("expected_effect", ""),
            confidence=float(data.get("confidence", 0.5)),
            reasoning=data.get("reasoning", ""),
        )

    # ── 結果検証 ─────────────────────────────────

    async def verify_result(
        self,
        action: ActionDecision,
        expected_effect: str,
        screenshot: Screenshot | None = None,
        expected_outcome: str | None = None,
    ) -> VerificationResult:
        if screenshot is not None and expected_outcome:
            prompt = f"""サブタスクが達成されたか、添付の画面を見て判定してください。

【サブタスクの期待結果】
{expected_outcome}

【直前のアクション】
種別: {action.action.action_type.value}
パラメータ: {action.action.params}
理由: {action.reasoning}

期待結果が画面上で確認できれば success=true、できなければ false を返してください。
推測での true は禁止です。"""
        else:
            prompt = f"""アクションの実行結果を検証してください。

【実行したアクション】
種別: {action.action.action_type.value}
パラメータ: {action.action.params}

【期待された効果】
{expected_effect}

【LLMの判断理由】
{action.reasoning}"""
        data = await self._call(
            prompt, _SCHEMA_VERIFY, image_bytes=screenshot.image_bytes if screenshot else None
        )
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
        recovery_actions = []
        for a in raw_actions:
            try:
                at = ActionType(a.get("action_type", "wait"))
            except ValueError:
                continue
            params = self._sanitize_params(at, a.get("params", {}))
            try:
                recovery_actions.append(Action(action_type=at, params=params))
            except ValueError:
                logger.warning("回復アクションをスキップ（不正params）: %s", a)
                continue
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
        """OpenAI API を呼び出し、JSON 応答を返す。

        structured output (response_format) に対応していないモデルでは
        400系エラーになるため、その場合は通常JSONモード（スキーマ指示を
        プロンプトに追記＋応答テキストからJSON抽出）に自動フォールバックする。
        画像は同一解像度のJPEG（q80）に変換して送る。座標系は変わらないまま
        ペイロードを約1/4に削減し、応答速度とコストを改善する。
        """
        # メッセージ構築（座標精度のため detail: high で原解像度を維持）
        if image_bytes:
            b64 = base64.b64encode(self._to_jpeg(image_bytes)).decode("ascii")
            user_content: Any = [
                {"type": "text", "text": prompt},
                {
                    "type": "image_url",
                    "image_url": {
                        "url": f"data:image/jpeg;base64,{b64}",
                        "detail": "high",
                    },
                },
            ]
        else:
            user_content = prompt

        use_schema = bool(json_schema) and self._structured_output
        if json_schema and not use_schema:
            # 既に非対応と判明している場合は最初からJSON指示付きで呼ぶ
            user_content = self._with_json_instruction(user_content, json_schema)

        max_retries = 3
        empty_count = 0
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
                if use_schema:
                    kwargs["response_format"] = {
                        "type": "json_schema",
                        "json_schema": json_schema,
                    }
                if self.session_id:
                    kwargs["extra_headers"] = {"x-opencode-session": self.session_id}

                response = await self._client.chat.completions.create(**kwargs)
                text = response.choices[0].message.content or ""
                if not text:
                    empty_count += 1
                    raise ValueError("応答が空です")

                return self._parse_json(text)

            except (json.JSONDecodeError, ValueError) as e:
                logger.warning(
                    "API 応答のパースに失敗 (attempt %d/%d): %s",
                    attempt + 1,
                    max_retries,
                    e,
                )
                if attempt == max_retries - 1:
                    if empty_count >= max_retries:
                        raise ValueError(
                            "応答が空です（空応答が連続）。モデルが画像付き"
                            "structured outputに対応していない可能性があります。"
                            "LLM_MODEL の変更を検討してください"
                        ) from e
                    raise
            except Exception as e:
                if use_schema and self._is_unsupported_error(e):
                    logger.warning(
                        "structured output 非対応のため通常JSONモードに切替: %s",
                        e,
                    )
                    self._structured_output = False
                    use_schema = False
                    if json_schema is not None:
                        user_content = self._with_json_instruction(user_content, json_schema)
                    continue
                logger.exception("API 呼び出しエラー (attempt %d/%d)", attempt + 1, max_retries)
                if attempt == max_retries - 1:
                    raise
        return {}

    @staticmethod
    def _to_jpeg(image_bytes: bytes, quality: int = 80) -> bytes:
        """PNG画像を同一解像度のJPEGに変換する（座標系不変・軽量化）。

        変換に失敗したら元のバイト列をそのまま返す。
        """
        try:
            import io

            from PIL import Image

            with Image.open(io.BytesIO(image_bytes)) as img:
                buf = io.BytesIO()
                img.convert("RGB").save(buf, format="JPEG", quality=quality)
                return buf.getvalue()
        except Exception:
            logger.debug("JPEG変換に失敗、PNGのまま送信")
            return image_bytes

    @staticmethod
    def _is_unsupported_error(e: Exception) -> bool:
        """response_format 非対応を示すエラーかどうかを判定する。

        401（認証）や429（レート制限）はフォールバック対象外とし、
        response_format 付きリクエストでの400/404/422系のみ対象とする。
        """
        msg = str(e).lower()
        if getattr(e, "status_code", None) in (400, 404, 422):
            return True
        return "response_format" in msg or "json_schema" in msg

    @staticmethod
    def _with_json_instruction(user_content: Any, json_schema: dict[str, Any]) -> Any:
        """response_format の代わりにプロンプトへJSON指示を追記する。"""
        instruction = (
            "\n\n必ずJSONオブジェクトのみで返答してください"
            "（前後の説明文やコードフェンスは禁止）。"
            "以下のJSONスキーマに従うこと:\n" + json.dumps(json_schema, ensure_ascii=False)
        )
        if isinstance(user_content, str):
            return user_content + instruction
        if isinstance(user_content, list):
            updated = list(user_content)
            for i, part in enumerate(updated):
                if isinstance(part, dict) and part.get("type") == "text":
                    updated[i] = {**part, "text": part.get("text", "") + instruction}
                    break
            return updated
        return user_content

    @staticmethod
    def _parse_json(text: str) -> dict[str, Any]:
        """LLM応答テキストからJSONを抽出してパースする。

        通常JSONモードでは説明文が混ざることがあるため、
        全体→コードフェンス→先頭{〜末尾}の順で試す。
        """
        stripped = text.strip()
        first_error: json.JSONDecodeError | None = None
        try:
            return json.loads(stripped)  # type: ignore[no-any-return]
        except json.JSONDecodeError as e:
            first_error = e

        candidate = stripped
        if "```json" in candidate:
            start = candidate.index("```json") + 7
            end = candidate.index("```", start)
            candidate = candidate[start:end].strip()
        elif "```" in candidate:
            start = candidate.index("```") + 3
            end = candidate.index("```", start)
            candidate = candidate[start:end].strip()
        try:
            return json.loads(candidate)  # type: ignore[no-any-return]
        except json.JSONDecodeError:
            pass

        # 最後の手段：最初と最後の波括弧で切り出す
        start = stripped.find("{")
        end = stripped.rfind("}")
        if start != -1 and end > start:
            try:
                return json.loads(stripped[start : end + 1])  # type: ignore[no-any-return]
            except json.JSONDecodeError:
                pass
        raise first_error or ValueError(f"JSONを抽出できません: {text[:100]}")

    @staticmethod
    def _sanitize_params(action_type: ActionType, params: dict[str, Any]) -> dict[str, Any]:
        """LLMのゴミパラメータを除去する。

        structured output 非厳密なモデルが余計なキーを付けてくることがある。
        許可リストに無いキーは落とす。必須欠落は残し、後段の
        Action バリデーションに任せる。
        """
        from ai_desktop_agent.actions.primitives import allowed_params

        if not isinstance(params, dict):
            return {}
        allowed = allowed_params(action_type)
        return {k: v for k, v in params.items() if k in allowed}

    @staticmethod
    def _clamp_params(
        action_type: ActionType, params: dict[str, Any], screenshot: Screenshot
    ) -> dict[str, Any]:
        """LLMが返した座標を画面内にクランプする。

        範囲外座標は実行時にずれる原因になるため、ここで丸める。
        region_select の幅・高さは最低16pxを保証する。
        """
        if not isinstance(params, dict):
            return {}
        clamped = dict(params)
        w, h = screenshot.width, screenshot.height

        def _clamp_int(v: Any, lo: int, hi: int) -> int:
            try:
                iv = int(v)
            except (TypeError, ValueError):
                iv = lo
            return max(lo, min(iv, hi))

        if action_type in (
            ActionType.MOUSE_MOVE,
            ActionType.LEFT_CLICK,
            ActionType.RIGHT_CLICK,
            ActionType.MIDDLE_CLICK,
            ActionType.DOUBLE_CLICK,
        ):
            if "x" in clamped:
                clamped["x"] = _clamp_int(clamped["x"], 0, max(0, w - 1))
            if "y" in clamped:
                clamped["y"] = _clamp_int(clamped["y"], 0, max(0, h - 1))
        elif action_type == ActionType.DRAG:
            for k in ("start_x", "end_x"):
                if k in clamped:
                    clamped[k] = _clamp_int(clamped[k], 0, max(0, w - 1))
            for k in ("start_y", "end_y"):
                if k in clamped:
                    clamped[k] = _clamp_int(clamped[k], 0, max(0, h - 1))
        elif action_type == ActionType.REGION_SELECT:
            if "x" in clamped:
                clamped["x"] = _clamp_int(clamped["x"], 0, max(0, w - 1))
            if "y" in clamped:
                clamped["y"] = _clamp_int(clamped["y"], 0, max(0, h - 1))
            if "width" in clamped:
                clamped["width"] = max(16, min(int(clamped["width"]), w))
            if "height" in clamped:
                clamped["height"] = max(16, min(int(clamped["height"]), h))
        return clamped

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
