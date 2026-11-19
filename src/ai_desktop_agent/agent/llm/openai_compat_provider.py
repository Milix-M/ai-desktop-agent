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

import asyncio
import base64
import json
import logging
import math
import os
import re
import time
from datetime import UTC
from email.utils import parsedate_to_datetime
from typing import Any

from openai import APIResponseValidationError, AsyncOpenAI

from ai_desktop_agent.actions.primitives import Action, ActionType
from ai_desktop_agent.agent.budget import TaskLimitError, model_request
from ai_desktop_agent.agent.llm.base import LLMProvider, ModelResponseError
from ai_desktop_agent.agent.llm.types import (
    ActionDecision,
    DecompositionResult,
    ErrorContext,
    RecoveryPlan,
    RecoveryStrategy,
    UnderstandingResult,
    VerificationResult,
)
from ai_desktop_agent.agent.output import parse_task_output
from ai_desktop_agent.agent.state import ActionRecord, Goal, Subtask
from ai_desktop_agent.privacy import (
    install_log_redaction,
    protect,
    protect_url,
    redact,
    redact_data,
)
from ai_desktop_agent.vm.async_io import run_display_call
from ai_desktop_agent.vm.screenshot import Screenshot

logger = logging.getLogger(__name__)

#: 操作対象プラットフォーム。linux（既定）/ android。
PLATFORM_LINUX = "linux"
PLATFORM_ANDROID = "android"
MAX_RETRY_WAIT_SECONDS = 60


def _planning_prompt(platform: str) -> str:
    return (
        f"{platform}のGUI操作を計画します。"
        "秘密情報は利用者の手動入力とし、VM再起動は許可が必要です。"
    )


def _system_prompt(platform: str = PLATFORM_LINUX) -> str:
    """プラットフォーム別のシステムプロンプトを返す。"""
    if platform == PLATFORM_ANDROID:
        return _COMMON_ACTION_PROMPT + _ANDROID_PROMPT
    return _SYSTEM_PROMPT


# ========================================================================
# システムプロンプト — 画面の見方と操作のルール
# ========================================================================

_COMMON_ACTION_PROMPT = """あなたは画面を遠隔操作する
AI エージェントです。
ユーザーの指示に従い、GUI 操作を自律実行します。

画面左上を原点とし、上端のX座標・左端のY座標とグリッド線から位置を読み取ってください。
緑の十字はカーソルです。確信度0.8以上なら直接クリックできます。
小さい対象や位置が不確かな対象は周囲100〜300pxをregion_selectし、拡大画面で選んでください。

# 操作可能なアクション

- mouse_move: {x: int, y: int} — カーソル移動
- left_click: {x: int, y: int} — 左クリック
- right_click: {x: int, y: int} — 右クリック
- double_click: {x: int, y: int} — ダブルクリック
- drag: {start_x: int, start_y: int, end_x: int, end_y: int} — ドラッグ
- scroll: {direction: "up"|"down", amount: int} — スクロール
- type: {text: str} — テキスト入力
  日本語などはクリップボードで貼り付ける。選択したテキストを置き換える場合は、
  選択後にBackspaceで削除してからtypeする。クリップボードの更新で選択が解除されるため。
- request_user_input: {reason: str} — パスワードや認証コードを利用者に入力してもらう。
  入力欄を選んでから要求する。待機中はAIへ画面を送らない。
  秘密の値を指示やtypeに含めず、利用者が画面を確認して再開するまで待つ。
- key_press: {key: str} — キー押下（enter, escape, tab 等）
- key_combo: {keys: [str]} — 複合キー（["ctrl", "c"] 等）
- wait: {seconds: float} — 待機（アプリ起動待ちには2〜5秒を使う）
- region_select: {x: int, y: int, width: int, height: int} — 領域拡大を要求
- vm_restart: {reason: str} — VM再起動（事前にユーザーの許可が必要。
  許可されていないタスクでは却下される。ディスクの初期化は行わない。
  再接続後の画面を確認し、保存済みの作業をやり直さずに続行する）
- subtask_complete: {summary, data_json} — 現在のサブタスク完了
  summary に利用者への結果を簡潔に書く。依頼された値は画面で確認した事実だけを書く。
  data_json は件数やIDなどを含むJSONオブジェクトを文字列で返す。不要なら空文字にする。

※ 毎ターン最新の画面が自動で送られるため、画面再取得のためのアクションは不要。
   アプリの起動を待つ場合は wait を使う。

# 重要なルール

1. **画面を見てから判断する**：推測でクリックしない。必ず座標マーカーを確認する。
2. **小さいターゲットは region_select を使う**：確信度 0.7 未満なら拡大表示を要求する。
3. 次の画面に操作結果が見えたら、そのまま次へ進む。読み込み中だけ wait する。
4. **失敗したら別の方法を試す**：同じ座標を連続クリックしない。
5. **confidence は正直に**：自信がないのに 0.9 以上を付けない。
6. **subtask_complete は操作の後に**：そのサブタスクで1つも操作せずに完了宣言すると
   システムに却下される。まずクリック・入力・キー操作・待機のいずれかを実行し、
   期待結果が画面に現れたことを確認してから完了を宣言する。
"""

_SYSTEM_PROMPT = (
    _COMMON_ACTION_PROMPT
    + """
Linux デスクトップの操作
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
)

_ANDROID_PROMPT = """
# 操作対象: Android 端末（Android 9, x86 タブレット, 1024x768）

このタスクの操作対象は Linux ではなく **Android 端末** です。
Android では以下の操作を使えません：

- 禁止: 端末アプリ・KRunner・Alt+F2・Ctrl+Alt+T・`which`・`apt`・`snap`
  （Android に端末・パッケージ管理・ランナーはありません）
- 禁止: タスクバーのランチャー検索・デスクトップという概念
  （下部のバーは Android のタスクバーであり、Linux のものとは別物です）

代わりに以下の Android 流儀で操作してください：

- **タップ**: `left_click` がそのままタップになる。アプリアイコン・ボタンを直接タップする
- **スワイプ・スクロール**: `drag` がスワイプ、`scroll` がスクロールになる
- **アプリ起動**: ホームのドロワー（アプリ一覧）アイコンをタップ→目的アプリをタップ。
  ホームにアイコンがあれば直接タップでよい
- **文字入力**: テキスト欄をタップしてから `type` を使う
  （ソフトウェアキーボードが出ても無視して `type` で入力できる）
- **戻る・ホーム**: `escape` キーは戻るボタン相当。下部ナビゲーションバーの
  ◀ ■ ● ボタンのタップでもよい
- **禁止: `home` / `menu` / `power` のキー押下**: QEMU経由ではAndroidに届かない。
  ホームに戻る等は必ず画面下部のナビゲーションバーボタンをタップする
  （◀: 約(340, 730)、●: 約(512, 730)、■: 約(680, 730)）
- **設定変更**: 設定アプリを開き、目的の項目をタップで辿る
  （例: 日付と時刻は 設定 → システム → 日付と時刻）
- **初回起動時の選択**: 「Select a Home app」と出たら好みのランチャー
  （Quickstep 推奨）をタップして `ALWAYS` をタップする
- **アプリが無い場合**: Play ストアは無い。導入は指示がある場合のみ
  ブラウザで APK を入手する手順に切り替え、勝手に `apt` 等を使わない
- **待機**: アプリ・画面の切り替わりには `wait`（2〜5秒）を使う"""

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
    "request_user_input",
    "key_press",
    "repeat_navigation",
    "key_combo",
    "wait",
    "region_select",
    "vm_restart",
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

_SCHEMA_INITIAL = {
    "name": "initial_plan",
    "strict": True,
    "schema": {
        **_SCHEMA_UNDERSTAND["schema"],
        "properties": {
            **_SCHEMA_UNDERSTAND["schema"]["properties"],
            "subtasks": _SCHEMA_DECOMPOSE["schema"]["properties"]["subtasks"],
        },
        "required": [*_SCHEMA_UNDERSTAND["schema"]["required"], "subtasks"],
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
                    "anyOf": [
                        {
                            "type": "object",
                            "properties": {
                                "action_type": {"type": "string", "const": "left_click"},
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
                                "action_type": {"type": "string", "const": "right_click"},
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
                                "action_type": {"type": "string", "const": "double_click"},
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
                                "action_type": {"type": "string", "const": "mouse_move"},
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
                                "action_type": {"type": "string", "const": "drag"},
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
                                "action_type": {"type": "string", "const": "scroll"},
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
                                "action_type": {"type": "string", "const": "type"},
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
                                "action_type": {"type": "string", "const": "key_press"},
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
                                "action_type": {"type": "string", "const": "key_combo"},
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
                                "action_type": {"type": "string", "const": "wait"},
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
                                "action_type": {"type": "string", "const": "region_select"},
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
                                "action_type": {"type": "string", "const": "vm_restart"},
                                "params": {
                                    "type": "object",
                                    "properties": {
                                        "reason": {"type": "string"},
                                    },
                                    "required": ["reason"],
                                    "additionalProperties": False,
                                },
                            },
                            "required": ["action_type", "params"],
                            "additionalProperties": False,
                        },
                        {
                            "type": "object",
                            "properties": {
                                "action_type": {"type": "string", "const": "subtask_complete"},
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


def _action_variant(name: str, params: dict) -> dict:
    return {
        "type": "object",
        "properties": {
            "action_type": {"type": "string", "const": name},
            "params": {
                "type": "object",
                "properties": params,
                "required": list(params),
                "additionalProperties": False,
            },
        },
        "required": ["action_type", "params"],
        "additionalProperties": False,
    }


_recovery_variants = _SCHEMA_RECOVER["schema"]["properties"]["actions"]["items"]["anyOf"]
_recovery_variants.append(
    _action_variant(
        "request_user_input", {"reason": {"type": "string", "minLength": 1, "maxLength": 1000}}
    )
)
_SCHEMA_ACTION = {
    "name": "action_decision",
    "schema": {
        "type": "object",
        "properties": {
            "action": {
                "anyOf": [
                    *(
                        variant
                        for variant in _recovery_variants
                        if variant["properties"]["action_type"]["const"] != "subtask_complete"
                    ),
                    _action_variant(
                        "subtask_complete",
                        {"summary": {"type": "string"}, "data_json": {"type": "string"}},
                    ),
                    _action_variant(
                        "repeat_navigation",
                        {
                            "key": {"type": "string", "enum": ["up", "down", "left", "right"]},
                            "count": {"type": "integer", "minimum": 2, "maximum": 5},
                        },
                    ),
                ]
            },
            "working_note": {"type": "string", "maxLength": 400},
            "expected_effect": {"type": "string"},
            "confidence": {"type": "number", "minimum": 0.0, "maximum": 1.0},
            "reasoning": {"type": "string"},
        },
        "required": ["action", "expected_effect", "confidence", "reasoning", "working_note"],
        "additionalProperties": False,
    },
}


_PLAN_VERIFICATION = (
    "完了判定には最後の画面1枚だけを使います。異なる画面が必要な成果は、"
    "それぞれを確認できる時点でサブタスクを分け、次の画面に移る前に完了させてください。"
    "同じ画面で確認できる操作はまとめてください。"
)


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
        max_tokens: int = 1500,
        temperature: float = 0.0,
        base_url: str | None = None,
        default_headers: dict[str, str] | None = None,
        session_id: str | None = None,
        timeout: float = 120.0,
    ) -> None:
        self._model = model
        self._max_tokens = max_tokens
        self._temperature = temperature
        # 会話単位ID（OpenCode Go の x-opencode-session 用）。
        # タスクごとに TaskSession が設定する。
        self.session_id = session_id
        # 累積トークン使用量（可観測性用）
        self.total_prompt_tokens = 0
        self.total_completion_tokens = 0
        self.call_count = 0

        api_key = api_key or os.environ.get("OPENAI_API_KEY") or "***"
        protect(api_key)
        if base_url:
            protect_url(base_url)
        for name, value in (default_headers or {}).items():
            if any(word in name.lower() for word in ("authorization", "key", "token")):
                protect(value)
                protect(value.removeprefix("Bearer ").removeprefix("Basic "))
        install_log_redaction()
        # Retries belong to _call so every real send is counted by the task budget.
        client_kwargs: dict[str, Any] = {"api_key": api_key, "timeout": timeout, "max_retries": 0}
        if base_url:
            client_kwargs["base_url"] = base_url
        if default_headers:
            client_kwargs["default_headers"] = default_headers
        self._client = AsyncOpenAI(**client_kwargs)
        # structured output (response_format) が使えるかどうか。
        # 非対応エラーが出たら False に倒し、通常JSONモードで続行する。
        self._structured_output = True
        self._completion_limit_only = (
            self._client.base_url.host == "api.openai.com" and model.startswith("gpt-6.")
        )

    @property
    def provider_name(self) -> str:
        return "openai_compat"

    @property
    def model_name(self) -> str:
        return self._model

    # ── 指示理解 ─────────────────────────────────

    async def aclose(self) -> None:
        await self._client.close()

    async def initial_plan(
        self, goal: Goal, platform: str
    ) -> tuple[UnderstandingResult, DecompositionResult]:
        prompt = (
            "指示の意図・対象アプリ・制約と、期待結果つきの操作計画を返してください。"
            "単一の成果物を一つのサブタスクにまとめ、開く・選択・入力・保存を分割しない。"
            "期待結果は画面で確認できる最終状態とし、維持すべき制約はconstraintsに残す。"
            "ユーザーが明示的に求めた検証は計画に含める。"
            f"{_PLAN_VERIFICATION}IDはstep_1から連番にしてください。\n" + goal.description
        )
        data = await self._call(prompt, _SCHEMA_INITIAL, system_prompt=_planning_prompt(platform))
        return (
            UnderstandingResult(
                data["intent"], data["target_application"], data["constraints"], data["reasoning"]
            ),
            self._decomposition(data, 0),
        )

    async def understand_instruction(
        self, goal: Goal, platform: str = PLATFORM_LINUX
    ) -> UnderstandingResult:
        prompt = f"""ユーザー指示を解析し、意図・対象アプリ・制約を抽出してください。

【ユーザー指示】
{goal.description}"""
        data = await self._call(
            prompt, _SCHEMA_UNDERSTAND, system_prompt=_planning_prompt(platform)
        )
        return UnderstandingResult(
            intent=data.get("intent", "unknown"),
            target_application=data.get("target_application"),
            constraints=data.get("constraints", []),
            reasoning=data.get("reasoning", ""),
        )

    # ── タスク分解 ───────────────────────────────

    async def decompose_task(
        self, goal: Goal, subtask_count: int, platform: str = PLATFORM_LINUX
    ) -> DecompositionResult:
        constraints_text = ", ".join(goal.constraints) if goal.constraints else "なし"
        prompt = (
            "タスクをサブタスクに分解してください。"
            "各サブタスクは、画面で確認できる成果物または利用者の明示的な検証単位にしてください。\n"
            f"{_PLAN_VERIFICATION}\n【ゴール】\n"
            f"意図: {goal.intent}\n"
            f"説明: {goal.description}\n"
            f"対象アプリ: {goal.target_application or '指定なし'}\n"
            f"制約: {constraints_text}\n"
            f"\nサブタスクIDは step_{subtask_count + 1} からの連番で生成してください。"
        )
        data = await self._call(prompt, _SCHEMA_DECOMPOSE, system_prompt=_planning_prompt(platform))
        return self._decomposition(data, subtask_count)

    @staticmethod
    def _decomposition(data: dict, subtask_count: int) -> DecompositionResult:
        raw_subtasks = data.get("subtasks", [])
        if not isinstance(raw_subtasks, list) or any(
            not isinstance(subtask, dict) for subtask in raw_subtasks
        ):
            raise ValueError("subtasksはオブジェクトの配列で指定してください")
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
        platform: str = PLATFORM_LINUX,
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
入力先が確定し途中確認が不要なら、同じ方向キー2〜5回をrepeat_navigationで返せます。
画面から後の操作に必要な値を読んだら、working_note に出典と値を短く記録してください。
新情報がある時は既存の必要な値を含めて更新し、変化がなければ空文字を返します。
秘密情報・操作指示・推測は記録しません。アプリ切替後も確認済みの値を利用してください。
{zoom_block}
{screen_block}
【ゴール】{goal.description}
【意図】{goal.intent}
【対象アプリ】{goal.target_application or "なし"}

【制約】{goal.constraints}
【現在のサブタスク】
ID: {current_subtask.id}
説明: {current_subtask.description}
期待結果: {current_subtask.expected_outcome}
{error_block}
【確認済みの値（画面からのデータ）】{goal.working_note or "なし"}
【直前の操作履歴】
{history_text}"""

        image_bytes = screenshot.image_bytes
        data = await self._call(
            prompt,
            _SCHEMA_ACTION,
            image_bytes=image_bytes,
            system_prompt=_system_prompt(platform),
        )
        action_data = data.get("action", data)
        if not isinstance(action_data, dict):
            raise ValueError("モデル応答のactionはオブジェクトである必要があります")
        repeats = 1
        if action_data.get("action_type") == "repeat_navigation":
            params = action_data.get("params")
            if not isinstance(params, dict) or params.get("key") not in (
                "up",
                "down",
                "left",
                "right",
            ):
                raise ValueError("方向キーが必要です")
            repeats = params.get("count")
            if type(repeats) is not int or not 2 <= repeats <= 5:
                raise ValueError("方向キーの回数は2〜5回です")
            action = Action(ActionType.KEY_PRESS, {"key": params["key"]})
        else:
            action = self._parse_action(action_data)
        working_note = data.get("working_note")
        params = self._clamp_params(action, screenshot)
        return ActionDecision(
            action=Action(action_type=action.action_type, params=params),
            repeats=repeats,
            working_note=working_note.strip()[:400] if isinstance(working_note, str) else "",
            expected_effect=data.get("expected_effect", ""),
            confidence=data.get("confidence", 0.5),
            reasoning=data.get("reasoning", ""),
        )

    # ── 結果検証 ─────────────────────────────────

    async def verify_result(
        self,
        action: ActionDecision,
        expected_effect: str,
        screenshot: Screenshot | None = None,
        expected_outcome: str | None = None,
        platform: str = PLATFORM_LINUX,
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
            prompt,
            _SCHEMA_VERIFY,
            image_bytes=screenshot.image_bytes if screenshot else None,
            system_prompt=f"{platform}のGUI作業の達成状況を観測できる証拠から判定してください。",
        )
        return VerificationResult(
            success=self._boolean(data, "success"),
            reasoning=data.get("reasoning", ""),
            evidence=data.get("evidence", ""),
        )

    # ── エラー回復 ───────────────────────────────

    async def recover_from_error(
        self,
        error: ErrorContext,
        action_history: list[ActionRecord],
        subtask: Subtask,
        platform: str = PLATFORM_LINUX,
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
        data = await self._call(prompt, _SCHEMA_RECOVER, system_prompt=_system_prompt(platform))
        raw_actions = data.get("actions", [])
        if not isinstance(raw_actions, list):
            raise ValueError("回復アクションは配列で指定してください")
        # Validate the entire plan before returning any actions. Skipping a failed focus/click
        # could otherwise type into a different window or submit an unintended form.
        recovery_actions = [self._parse_action(action) for action in raw_actions]
        return RecoveryPlan(
            strategy=RecoveryStrategy(data.get("strategy", "wait_and_retry")),
            actions=recovery_actions,
            reasoning=data.get("reasoning", ""),
            recoverable=self._boolean(data, "recoverable"),
        )

    @staticmethod
    def _boolean(data: dict, name: str) -> bool:
        value = data.get(name)
        if type(value) is not bool:
            raise ValueError(f"モデル応答の{name}は真偽値である必要があります")
        return value

    @classmethod
    def _parse_action(cls, data: dict) -> Action:
        if not isinstance(data, dict) or not isinstance(data.get("action_type"), str):
            raise ValueError("モデル応答に有効なaction_typeが必要です")
        action_type = ActionType(data["action_type"])
        return Action(action_type, cls._sanitize_params(action_type, data.get("params", {})))

    # ── 内部 ──────────────────────────────────────

    async def _call(
        self,
        prompt: str,
        json_schema: dict[str, Any] | None = None,
        image_bytes: bytes | None = None,
        system_prompt: str | None = None,
    ) -> dict[str, Any]:
        """OpenAI API を呼び出し、JSON 応答を返す。

        structured output (response_format) に対応していないモデルでは
        400系エラーになるため、その場合は通常JSONモード（スキーマ指示を
        プロンプトに追記＋応答テキストからJSON抽出）に自動フォールバックする。
        画像は同一解像度のJPEG（q80）に変換して送る。座標系は変わらないまま
        ペイロードを約1/4に削減し、応答速度とコストを改善する。
        """
        prompt = redact(prompt)
        system_prompt = redact(system_prompt or _SYSTEM_PROMPT)
        # メッセージ構築（座標精度のため detail: high で原解像度を維持）
        image_content = None
        if image_bytes is not None:
            encoded_image = await run_display_call(self._to_jpeg, image_bytes)
            b64 = base64.b64encode(encoded_image).decode("ascii")
            image_content = {
                "type": "image_url",
                "image_url": {
                    "url": f"data:image/jpeg;base64,{b64}",
                    "detail": "high",
                },
            }
        user_prompt = prompt

        use_schema = bool(json_schema) and self._structured_output
        if json_schema and not use_schema:
            # 既に非対応と判明している場合は最初からJSON指示付きで呼ぶ
            user_prompt = self._with_json_instruction(prompt, json_schema)

        max_retries = 3
        empty_count = 0
        for attempt in range(max_retries):
            try:
                kwargs: dict[str, Any] = {
                    "model": self._model,
                    "messages": [
                        {"role": "system", "content": system_prompt or _SYSTEM_PROMPT},
                        {
                            "role": "user",
                            "content": [
                                {"type": "text", "text": user_prompt},
                                image_content,
                            ]
                            if image_content is not None
                            else user_prompt,
                        },
                    ],
                }
                if self._completion_limit_only:
                    kwargs["max_completion_tokens"] = self._max_tokens
                else:
                    kwargs.update(max_tokens=self._max_tokens, temperature=self._temperature)
                if use_schema:
                    kwargs["response_format"] = {
                        "type": "json_schema",
                        "json_schema": json_schema,
                    }
                if self.session_id:
                    kwargs["extra_headers"] = {"x-opencode-session": self.session_id}

                async with model_request() as budget:
                    self.call_count += 1
                    try:
                        response = await self._client.chat.completions.create(**kwargs)
                    except (json.JSONDecodeError, APIResponseValidationError):
                        if budget is not None:
                            budget.record_usage(None)
                        raise
                    self._record_usage(response)
                    if budget is not None:
                        budget.record_usage(getattr(response, "usage", None))
                text = response.choices[0].message.content or ""
                if not text:
                    empty_count += 1
                    raise ValueError("応答が空です")

                return redact_data(self._parse_json(text))

            except TaskLimitError:
                raise
            except (json.JSONDecodeError, ValueError) as e:
                logger.warning(
                    "API 応答のパースに失敗 (attempt %d/%d): %s",
                    attempt + 1,
                    max_retries,
                    e,
                )
                if attempt == max_retries - 1:
                    if empty_count >= max_retries:
                        raise ModelResponseError(
                            "応答が空です（空応答が連続）。モデルが画像付き"
                            "structured outputに対応していない可能性があります。"
                            "LLM_MODEL の変更を検討してください"
                        ) from e
                    raise ModelResponseError(str(e)) from e
                # 一時不調の可能性があるため待ってから再試行する
                await asyncio.sleep(1.5 * (attempt + 1))
            except Exception as e:
                if use_schema and self._is_unsupported_error(e):
                    logger.warning(
                        "structured output 非対応のため通常JSONモードに切替: %s",
                        e,
                    )
                    self._structured_output = False
                    if attempt == max_retries - 1 or self._retry_directive(e) == "false":
                        raise
                    delay = self._retry_delay(e, 0)
                    if delay is None:
                        raise
                    use_schema = False
                    user_prompt = self._with_json_instruction(prompt, json_schema)
                    if delay:
                        await asyncio.sleep(delay)
                    continue
                logger.exception("API 呼び出しエラー (attempt %d/%d)", attempt + 1, max_retries)
                if not self._is_retryable_error(e) or attempt == max_retries - 1:
                    raise
                delay = self._retry_delay(e, 1.5 * (attempt + 1))
                if delay is None:
                    raise
                await asyncio.sleep(delay)

    @staticmethod
    def _to_jpeg(image_bytes: bytes, quality: int = 80) -> bytes:
        """PNG画像を同一解像度のJPEGに変換する（座標系不変・軽量化）。

        変換に失敗したら、不正な形式の画像を送信する前に拒否する。
        """
        try:
            import io

            from PIL import Image

            with Image.open(io.BytesIO(image_bytes)) as img:
                buf = io.BytesIO()
                img.convert("RGB").save(buf, format="JPEG", quality=quality)
                return buf.getvalue()
        except Exception as error:
            raise ValueError("画面画像をJPEGに変換できません") from error

    @staticmethod
    def _is_unsupported_error(e: Exception) -> bool:
        """response_format 非対応を示すエラーかどうかを判定する。

        401（認証）や429（レート制限）はフォールバック対象外とし、
        400/404/422で対象パラメータの非対応を確認できた場合だけ切り替える。
        """
        if getattr(e, "status_code", None) not in (400, 404, 422):
            return False
        param = getattr(e, "param", None)
        schema_params = ("response_format", "response_format.type", "response_format.json_schema")
        if param is not None and param not in schema_params:
            return False
        code = getattr(e, "code", None)
        if code == "invalid_json_schema":
            return False
        if param in schema_params and code in (
            "unsupported_parameter",
            "unsupported_value",
            "not_supported",
        ):
            return True
        body = getattr(e, "body", None)
        message = body.get("message") if isinstance(body, dict) else str(e)
        if not isinstance(message, str):
            return False
        feature = r"(?:\bresponse_format\b|\bjson_schema\b)"
        before = r"(?:does not support|doesn't support|unsupported|unrecognized|unknown)"
        between = r"(?:\s+(?:the|parameter|field|format|type|value))*[\s'\"`:.=-]*"
        after = (
            r"[\s'\"`:.()=-]*(?:(?:is|are)\s+)?"
            r"(?:not (?:supported|available|implemented)|unsupported)"
        )
        return re.search(f"{before}{between}{feature}|{feature}{after}", message, re.I) is not None

    @staticmethod
    def _retry_directive(error: Exception) -> str | None:
        headers = getattr(getattr(error, "response", None), "headers", {})
        return headers.get("x-should-retry")

    @classmethod
    def _is_retryable_error(cls, error: Exception) -> bool:
        directive = cls._retry_directive(error)
        if directive in ("true", "false"):
            return directive == "true"
        status = getattr(error, "status_code", None)
        return status is None or status in (408, 409, 429) or status >= 500

    @staticmethod
    def _retry_delay(error: Exception, default: float) -> float | None:
        """Return the requested wait, or stop rather than retry before a long cooldown."""
        headers = getattr(getattr(error, "response", None), "headers", {})
        for name, scale in (("retry-after-ms", 0.001), ("retry-after", 1)):
            try:
                delay = float(headers.get(name)) * scale
            except (TypeError, ValueError):
                continue
            if math.isfinite(delay) and delay >= 0:
                return delay if delay <= MAX_RETRY_WAIT_SECONDS else None
        try:
            date = parsedate_to_datetime(headers.get("retry-after"))
            if date.tzinfo is None:
                date = date.replace(tzinfo=UTC)
            delay = max(0, date.timestamp() - time.time())
        except (TypeError, ValueError, OverflowError):
            return default
        return delay if delay <= MAX_RETRY_WAIT_SECONDS else None

    def _record_usage(self, response: Any) -> None:
        """API応答のトークン使用量を累積する（可観測性用）。"""
        try:
            usage = getattr(response, "usage", None)
            if usage is None:
                return
            prompt = getattr(usage, "prompt_tokens", None)
            completion = getattr(usage, "completion_tokens", None)
            if any(
                isinstance(value, bool) or not isinstance(value, int) or value < 0
                for value in (prompt, completion)
            ):
                return
            self.total_prompt_tokens += prompt
            self.total_completion_tokens += completion
        except Exception:
            logger.debug("使用量記録に失敗", exc_info=True)

    @property
    def total_tokens(self) -> int:
        """累積トークン数の合計。"""
        return self.total_prompt_tokens + self.total_completion_tokens

    @staticmethod
    def _with_json_instruction(prompt: str, json_schema: dict[str, Any]) -> str:
        """response_format の代わりにプロンプトへJSON指示を追記する。"""
        instruction = (
            "\n\n必ずJSONオブジェクトのみで返答してください"
            "（前後の説明文やコードフェンスは禁止）。"
            "以下のJSONスキーマに従うこと:\n" + json.dumps(json_schema, ensure_ascii=False)
        )
        return prompt + instruction

    @staticmethod
    def _parse_json(text: str) -> dict[str, Any]:
        """LLM応答テキストからJSONオブジェクトを抽出してパースする。

        通常JSONモードでは説明文が混ざることがあるため、
        全体→コードフェンス→先頭{〜末尾}の順で試す。
        配列や文字列の内側からオブジェクトを取り出さない。
        重複キーと不正な数値も再試行可能なJSONDecodeErrorで拒否する。
        """
        stripped = text.strip()
        parse_error = json.JSONDecodeError("JSONオブジェクトを抽出できません", stripped, 0)

        candidates = [stripped]
        if "```json" in stripped:
            start = stripped.index("```json") + 7
            end = stripped.find("```", start)
            candidates.append(stripped[start : end if end != -1 else None].strip())
        elif "```" in stripped:
            start = stripped.index("```") + 3
            end = stripped.find("```", start)
            candidates.append(stripped[start : end if end != -1 else None].strip())
        # A quoted or array-wrapped response is data, not prose surrounding an object.
        openings = [stripped.find(marker) for marker in ("{", "[", '"')]
        start = min((index for index in openings if index != -1), default=-1)
        end = stripped.rfind("}")
        if start != -1 and stripped[start] == "{" and end > start:
            candidates.append(stripped[start : end + 1])

        def finite_float(value: str) -> float:
            number = float(value)
            if not math.isfinite(number):
                raise ValueError("モデル応答の数値は有限である必要があります")
            return number

        def reject_constant(value: str):
            raise ValueError("モデル応答にJSONで表現できない数値があります")

        def unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
            result = {}
            for key, value in pairs:
                if key in result:
                    raise ValueError("モデル応答に同じJSONキーが複数あります")
                result[key] = value
            return result

        for candidate in candidates:
            try:
                result = json.loads(
                    candidate,
                    parse_float=finite_float,
                    parse_constant=reject_constant,
                    object_pairs_hook=unique_object,
                )
            except json.JSONDecodeError as error:
                parse_error = error
                continue
            except ValueError as error:
                # Never rescue a semantically invalid response by dropping its context.
                raise json.JSONDecodeError(str(error), candidate, 0) from error
            if not isinstance(result, dict):
                raise json.JSONDecodeError("JSONオブジェクトではありません", candidate, 0)
            return result  # type: ignore[no-any-return]
        raise parse_error

    @staticmethod
    def _sanitize_params(action_type: ActionType, params: dict[str, Any]) -> dict[str, Any]:
        """LLMのゴミパラメータを除去する。

        structured output 非厳密なモデルが余計なキーを付けてくることがある。
        許可リストに無いキーは落とす。必須欠落は残し、後段の
        Action バリデーションに任せる。
        """
        from ai_desktop_agent.actions.primitives import allowed_params

        if not isinstance(params, dict):
            raise ValueError("モデル応答のparamsはオブジェクトである必要があります")
        if action_type == ActionType.SUBTASK_COMPLETE:
            output = parse_task_output(params.get("summary"), params.get("data_json"))
            if output is None:
                return {}
            return {
                "summary": output.summary,
                "data_json": (
                    json.dumps(output.data, ensure_ascii=False, separators=(",", ":"))
                    if output.data is not None
                    else ""
                ),
            }
        allowed = allowed_params(action_type)
        return {k: v for k, v in params.items() if k in allowed}

    @staticmethod
    def _clamp_params(action: Action, screenshot: Screenshot) -> dict[str, Any]:
        """検証済みのアクションの座標を画面内にクランプする。

        範囲外座標は実行時にずれる原因になるため、ここで丸める。
        region_select の幅・高さは、画像の大きさを上限に最低16pxへ広げる。
        """
        clamped = dict(action.params or {})
        action_type = action.action_type
        w, h = screenshot.width, screenshot.height

        if action_type in (
            ActionType.MOUSE_MOVE,
            ActionType.LEFT_CLICK,
            ActionType.RIGHT_CLICK,
            ActionType.MIDDLE_CLICK,
            ActionType.DOUBLE_CLICK,
        ):
            clamped["x"] = max(0, min(clamped["x"], w - 1))
            clamped["y"] = max(0, min(clamped["y"], h - 1))
        elif action_type == ActionType.DRAG:
            for k in ("start_x", "end_x"):
                clamped[k] = max(0, min(clamped[k], w - 1))
            for k in ("start_y", "end_y"):
                clamped[k] = max(0, min(clamped[k], h - 1))
        elif action_type == ActionType.REGION_SELECT:
            clamped["x"] = max(0, min(clamped["x"], w - 1))
            clamped["y"] = max(0, min(clamped["y"], h - 1))
            clamped["width"] = min(w, max(16, clamped["width"]))
            clamped["height"] = min(h, max(16, clamped["height"]))
        return clamped

    @staticmethod
    def _format_action_history(history: list[ActionRecord]) -> str:
        """操作履歴をLLM向けテキストに整形。"""
        if not history:
            return "（履歴なし）"
        lines = []
        for i, record in enumerate(history, 1):
            status = "✓" if record.success else "✗"
            params = record.action.params
            if record.action.action_type == ActionType.TYPE and len(params["text"]) > 80:
                params = {"text": f"（{len(params['text'])}文字）"}
            lines.append(f"{i}. [{status}] {record.action.action_type.value} {params}")
            if record.error_message:
                lines.append(f"   エラー: {record.error_message}")
        return "\n".join(lines)
