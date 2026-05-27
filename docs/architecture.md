# アーキテクチャ詳細設計

## 目次

1. [エージェント状態機械](#エージェント状態機械)
2. [多段階パイプライン](#多段階パイプライン)
3. [アクション定義](#アクション定義)
4. [観測モデル](#観測モデル)
5. [LLMプロバイダ抽象化](#llmプロバイダ抽象化)
6. [エラー回復戦略](#エラー回復戦略)
7. [安全性設計](#安全性設計)

## エージェント状態機械

エージェントは単純なループではなく、**有限状態機械**として動作する。各状態でやるべきことが明確に分離されており、異常時は適切な回復状態に遷移する。

```
                    ┌─────────────┐
                    │    IDLE     │  ◄── 指示待ち
                    └──────┬──────┘
                           │ ユーザー指示受信
                           ▼
                    ┌─────────────┐
                    │ UNDERSTAND  │  指示の意図解析
                    └──────┬──────┘
                           │
                           ▼
                    ┌─────────────┐
              ┌────►│  PLANNING   │  タスク分解・計画立案
              │     └──────┬──────┘
              │            │
              │            ▼
              │     ┌─────────────┐
              │     │  EXECUTING  │  アクション実行
              │     └──────┬──────┘
              │            │
              │            ▼
              │     ┌─────────────┐
              │     │  WAITING    │  UI変化待ち
              │     └──────┬──────┘
              │            │
              │            ▼
              │     ┌─────────────┐
              │ ┌───│  VERIFYING  │  結果検証
              │ │   └──────┬──────┘
              │ │          │ success
              │ │          ▼
              │ │   ┌─────────────┐
              │ │   │  COMPLETED  │  全サブタスク完了
              │ │   └─────────────┘
              │ │
              │ │   verification failed
              │ │          │
              │ │          ▼
              │ │   ┌─────────────┐
              │ └───│  RECOVERING │  エラー回復
              │     └──────┬──────┘
              │            │ recovery possible → replan/retry
              │            │
              │            │ unrecoverable
              │            ▼
              │     ┌─────────────┐
              │     │   FAILED    │  回復不能
              └─────┴─────────────┘
              (RECOVERING→PLANNING で再計画)
```

### 状態一覧

| 状態 | 説明 | 遷移先 |
|------|------|--------|
| `IDLE` | 指示待ち状態 | → UNDERSTANDING |
| `UNDERSTANDING` | ユーザー指示を解析、意図と制約を抽出 | → PLANNING |
| `PLANNING` | タスクをサブタスクに分解、アクション計画を生成 | → EXECUTING |
| `EXECUTING` | 計画に従いアクションを1つずつ実行 | → WAITING |
| `WAITING` | アクション後のUI変化を待機（ロード中など） | → VERIFYING |
| `VERIFYING` | アクションが期待通りの効果を持ったか検証 | → EXECUTING (継続) / RECOVERING (失敗) / COMPLETED (終了) |
| `RECOVERING` | エラーから回復を試みる | → PLANNING (再計画) / EXECUTING (リトライ) / FAILED |
| `PAUSED` | ユーザーが一時停止 | → EXECUTING (再開) |
| `COMPLETED` | タスク正常完了 | → IDLE |
| `FAILED` | タスク遂行不能 | → IDLE |

### 割り込み

- **ユーザー停止**: どの状態からでも PAUSED → 指示により IDLE へ
- **タイムアウト**: 各状態に最大滞在時間を設定、超過時は RECOVERING → FAILED

## 多段階パイプライン

### Phase 1: 指示理解 (UNDERSTANDING)

入力：ユーザーの自然言語指示
出力：構造化されたゴール定義

```python
@dataclass
class Goal:
    """LLMによって抽出された構造化ゴール"""
    description: str                    # "Excelで売上レポートを作成して"
    intent: str                         # "スプレッドシート作成"
    target_application: str | None      # "LibreOffice Calc"
    constraints: list[str]              # ["A列に日付", "B列に金額"]
    expected_output: str                # "/home/user/report.ods に保存"
    deadline_seconds: int | None        # タイムアウト
    environment_vars: dict[str, str]    # 必要な環境変数
```

LLMプロンプトでは、指示から以下の情報を抽出させる：
- 何をしたいのか（意図）
- どのアプリを使うべきか
- どのような制約があるか
- 完了条件は何か

### Phase 2: タスク分解 (PLANNING)

入力：Goal
出力：サブタスクのリスト

```python
@dataclass
class Subtask:
    id: str
    description: str                    # "LibreOffice Calcを起動する"
    preconditions: list[str]            # 前提条件 ["デスクトップが表示されている"]
    expected_outcome: str               # "Calcの空のスプレッドシートが表示されている"
    max_retries: int = 3
    timeout_seconds: int = 30
    actions: list[Action]               # 実行すべきアクション列（EXECUTINGでLLMが都度生成）
```

**サブタスク分解の例**:

指示: 「売上データのスプレッドシートを作成し、/home/user/report.ods に保存して」

```
Subtask 1: アプリ起動
  → LibreOffice Calcを開く

Subtask 2: ヘッダー入力
  → A1に「日付」、B1に「商品名」、C1に「金額」を入力

Subtask 3: データ入力
  → A2:C5 にサンプルデータを入力

Subtask 4: 書式設定
  → ヘッダー行を太字に、金額列を通貨形式に

Subtask 5: 保存
  → 名前を付けて保存 → /home/user/report.ods
```

### Phase 3-5: 実行・待機・検証 (EXECUTING → WAITING → VERIFYING)

各サブタスクに対して、LLMが都度1つのアクションを提案し、それを実行→待機→検証のサイクルで進める。

```
for each subtask:
    while subtask not complete:
        # 1. 現在の画面を観測
        observation = await capture_observation(vnc)

        # 2. LLMに次のアクションを決定させる
        action = await llm.decide_next_action(
            goal=goal,
            current_subtask=subtask,
            observation=observation,
            action_history=context.action_history[-10:],  # 直近10アクション
        )
        # LLMは以下を返す:
        #   - action_type + params
        #   - expected_effect（何が起こるはずか）
        #   - confidence（0.0〜1.0）

        # 3. アクションを実行
        await execute_action(vnc, action)
        context.action_history.append(action)

        # 4. 待機（UI変化を待つ）
        await asyncio.sleep(0.5)  # 最小待機
        await wait_for_settle(vnc, timeout=3.0)  # 画面変化が落ち着くまで

        # 5. 検証
        new_observation = await capture_observation(vnc)
        result = await verify_effect(action, observation, new_observation)

        if result.is_success:
            # サブタスク完了判定
            if await check_subtask_complete(new_observation, subtask):
                break  # 次のサブタスクへ
        else:
            # 回復フェーズへ
            recovery_result = await recover(action, result, context)
            if not recovery_result.recoverable:
                raise TaskFailedError(subtask, result)
```

### アクション決定のLLMプロンプト設計

LLMに送るコンテキスト：

```
【システム】あなたはLinuxデスクトップを操作するAIエージェントです。

【現在のゴール】{goal.description}
【現在のサブタスク】{subtask.description}
【期待する結果】{subtask.expected_outcome}

【現在の画面】
→ 添付のスクリーンショットを参照
→ OCRテキスト:
{observation.ocr_text}
→ アクティブウィンドウ: {observation.active_window}
→ カーソル位置: {observation.cursor_position}

【直近の操作履歴】
{action_history_last_10}

【指示】
次に実行すべき1つのアクションを提案してください。
以下のJSON形式で返答してください:

{
  "action_type": "left_click | type | key_combo | scroll | wait | screenshot | subtask_complete",
  "params": { ... },                  // アクションに応じたパラメータ
  "expected_effect": "...",           // このアクションで何が起こるか（検証に使用）
  "confidence": 0.0-1.0,              // このアクションへの確信度
  "reasoning": "..."                  // なぜこのアクションを選んだか
}
```

## アクション定義

### 基本アクション

```python
class ActionType(StrEnum):
    # === マウス操作 ===
    MOUSE_MOVE = "mouse_move"         # カーソル移動
    LEFT_CLICK = "left_click"         # 左クリック
    RIGHT_CLICK = "right_click"       # 右クリック
    DOUBLE_CLICK = "double_click"     # ダブルクリック
    MIDDLE_CLICK = "middle_click"     # 中クリック
    DRAG = "drag"                     # ドラッグ (from → to)
    SCROLL = "scroll"                 # スクロール (direction, amount)

    # === キーボード操作 ===
    TYPE = "type"                     # 文字列入力
    KEY_PRESS = "key_press"           # 単一キー押下
    KEY_COMBO = "key_combo"           # 複合キー (Ctrl+C等)
    KEY_HOLD = "key_hold"             # キー長押し

    # === 待機 ===
    WAIT = "wait"                     # 指定秒数待機
    WAIT_FOR_TEXT = "wait_for_text"   # 特定テキストがOCRで検出されるまで待機
    WAIT_FOR_STILL = "wait_for_still" # 画面変化が収まるまで待機

    # === 観測（LLM判断用） ===
    SCREENSHOT = "screenshot"         # 高解像度スクリーンショット取得

    # === メタ ===
    SUBTASK_COMPLETE = "subtask_complete" # サブタスク完了宣言
```

### アクションパラメータ

```python
@dataclass
class Action:
    action_type: ActionType
    params: dict  # 型ごとに異なる

    # マウス操作のparams
    # MOUSE_MOVE:  {"x": int, "y": int}
    # LEFT_CLICK:  {"x": int, "y": int} | {}  (省略時は現在位置)
    # DRAG:        {"start": [x,y], "end": [x,y]}
    # SCROLL:      {"direction": "up"|"down", "amount": int}

    # キーボード操作のparams
    # TYPE:        {"text": str}
    # KEY_PRESS:   {"key": str}  例: "enter", "escape", "tab"
    # KEY_COMBO:   {"keys": [str]}  例: ["ctrl", "c"]
    # KEY_HOLD:    {"key": str, "duration_ms": int}

    # 待機のparams
    # WAIT:              {"seconds": float}
    # WAIT_FOR_TEXT:     {"text": str, "timeout": float}
    # WAIT_FOR_STILL:    {"timeout": float, "threshold": float}
```

### アクション実行時の注意点

1. **座標系**: 左上原点 (0,0)、右方向+x、下方向+y。画面解像度はVMの設定に依存。
2. **クリック前のカーソル移動**: 明示的に `MOUSE_MOVE` → `LEFT_CLICK` の2段階が安全。
3. **日本語入力**: VM側のIME状態を考慮。英数字以外の入力時は `TYPE` の前にIME ON/OFFが必要。
4. **キーコンボ**: 修飾キーは押下順を保証（押下: Ctrl→C、解放: C→Ctrl）。

## 観測モデル

エージェントがVMの状態を把握するための多層的な観測システム。

```python
@dataclass
class Observation:
    # 視覚
    screenshot: bytes                          # 画面全体のPNG画像
    screenshot_resized: bytes                  # LLM送信用にリサイズ（1024px幅以下）

    # テキスト (OCR)
    ocr_full_text: str                         # 画面全体のOCR結果
    ocr_blocks: list[OCRBlock]                 # ブロック単位（位置情報付き）

    # ウィンドウ情報 (xdotool/wmctrl)
    active_window_title: str | None            # アクティブウィンドウのタイトル
    active_window_geometry: Rect | None        # アクティブウィンドウの位置とサイズ
    all_windows: list[WindowInfo]              # 全ウィンドウ情報

    # カーソル
    cursor_position: tuple[int, int] | None    # 現在のカーソル位置

    # 差分
    changed_regions: list[Rect]                # 前回観測からの変化領域

    # メタ
    timestamp: float                           # 観測時刻
    frame_number: int                          # 観測シーケンス番号

@dataclass
class OCRBlock:
    text: str
    bbox: Rect        # (x1, y1, x2, y2)
    confidence: float  # OCR信頼度

@dataclass
class WindowInfo:
    title: str
    pid: int
    geometry: Rect
    is_active: bool

@dataclass
class Rect:
    x: int
    y: int
    width: int
    height: int
```

### 観測の流れ

```
1. VNCフレームバッファ取得 (PIL Image)
2. 画像差分検出（前回スクショと比較 → changed_regions）
3. OCR実行（Tesseract/EasyOCR → 全テキスト + ブロック情報）
4. ウィンドウ情報取得（VM内で xdotool/wmctrl を実行）
5. カーソル位置取得（VNCプロトコルから）
6. LLM送信用にリサイズ（長辺1024px、圧縮率80% JPEG）
```

## LLMプロバイダ抽象化

### インターフェース

```python
class LLMProvider(ABC):
    """全LLMプロバイダが実装すべき共通インターフェース"""

    @abstractmethod
    async def decide_next_action(
        self,
        goal: Goal,
        subtask: Subtask,
        observation: Observation,
        action_history: list[ActionRecord],
        error_context: ErrorContext | None,
    ) -> ActionDecision:
        """現在の状態から次のアクションを決定する"""
        ...

    @abstractmethod
    async def decompose_task(
        self,
        goal: Goal,
        observation: Observation,
    ) -> list[Subtask]:
        """指示をサブタスクに分解する"""
        ...

    @abstractmethod
    async def verify_result(
        self,
        action: Action,
        expected_effect: str,
        before: Observation,
        after: Observation,
    ) -> VerificationResult:
        """アクションの結果を検証する"""
        ...

    @abstractmethod
    async def recover_from_error(
        self,
        error: ErrorRecord,
        observation: Observation,
        action_history: list[ActionRecord],
    ) -> RecoveryPlan:
        """エラーからの回復計画を生成する"""
        ...

class ActionDecision(NamedTuple):
    action: Action
    expected_effect: str
    confidence: float       # 0.0〜1.0
    reasoning: str          # 判断理由（ログ用）
```

### プロバイダ実装

| クラス | プロバイダ | モデル例 | 備考 |
|--------|----------|---------|------|
| `AnthropicProvider` | Anthropic | `claude-sonnet-4-20250514` | Computer Use ツールネイティブ |
| `OpenAIProvider` | OpenAI | `gpt-4o`, `gpt-4.1` | Vision API + JSON mode |
| `GoogleProvider` | Google | `gemini-2.5-pro` | 長コンテキスト、マルチモーダル |
| `OllamaProvider` | Ollama (ローカル) | `llama3.2-vision`, `qwen2.5-vl` | API費用ゼロ、低レイテンシ |
| `OpenAICompatProvider` | OpenAI互換 | 任意 (vLLM, LiteLLM等) | 自前GPUで任意モデル |

### プロバイダ選択

```python
# 環境変数または設定ファイルで指定
provider = LLMProviderFactory.create(
    provider_type="anthropic",        # or "openai", "google", "ollama", "openai_compat"
    model="claude-sonnet-4-20250514",
    api_key=os.environ["ANTHROPIC_API_KEY"],
    # プロバイダ固有のオプション
    max_tokens=4096,
    temperature=0.0,                  # 決定論的な動作を優先
)
```

### Anthropic Computer Use 統合

AnthropicのComputer Use機能を使う場合、ツール定義を利用する：

```python
# Anthropicのツール定義（computer_use_20250514）
tools = [
    {
        "type": "computer_20250514",
        "name": "computer",
        "display_width_px": 1280,
        "display_height_px": 720,
        "display_number": 0,
    },
    {
        "type": "text_editor_20250514",
        "name": "str_replace_editor",
    },
    {
        "type": "bash_20250514",
        "name": "bash",
    },
]
```

Computer Use APIを使う場合、スクリーンショットのエンコードやツール呼び出しのループ処理がプロバイダ内部にカプセル化される。

## エラー回復戦略

### 回復戦略の種類

```python
class RecoveryStrategy(StrEnum):
    WAIT_AND_RETRY = "wait_and_retry"         # 待ってから同じアクションを再実行
    SCROLL_AND_RETRY = "scroll_and_retry"     # スクロールしてから再実行（対象が画面外の可能性）
    CLOSE_DIALOG = "close_dialog"             # 予期しないダイアログを閉じる
    ALT_WINDOW = "alt_window"                 # Alt+Tabでウィンドウ切り替え
    REFRESH = "refresh"                       # F5やCtrl+Rでリフレッシュ
    ALTERNATIVE_APPROACH = "alternative"      # 別のUI経路で同じ目標を達成
    REPLAN_SUBTASK = "replan"                 # サブタスク全体を再計画
    ASK_USER = "ask_user"                     # ユーザーに判断を仰ぐ
    GIVE_UP = "give_up"                       # 回復不能、失敗としてマーク
```

### 回復フロー

```python
async def recover(
    action: Action,
    error: VerificationError,
    context: AgentContext,
    observation: Observation,
) -> RecoveryResult:
    """エラーからの回復を試みる"""

    # 1. リトライ回数チェック
    retry_key = f"{context.current_subtask.id}:{action.action_type}"
    retries = context.retry_counts.get(retry_key, 0)

    if retries >= context.current_subtask.max_retries:
        # 最大リトライ回数超過 → ユーザーに確認
        return await escalate_to_user(context, error)

    context.retry_counts[retry_key] = retries + 1

    # 2. エラー種別に応じた回復戦略を選択
    strategy = classify_error(error, observation)

    # 3. 単純な回復は決定的に処理
    if strategy == RecoveryStrategy.WAIT_AND_RETRY:
        await asyncio.sleep(2.0)
        return RecoveryResult(retry=True, action=action)  # 同じアクションを再実行

    if strategy == RecoveryStrategy.CLOSE_DIALOG:
        await vnc.key_press("escape")
        await asyncio.sleep(1.0)
        return RecoveryResult(retry=True, action=action)

    # 4. 複雑な回復はLLMに判断させる
    if strategy in (RecoveryStrategy.ALTERNATIVE_APPROACH,
                    RecoveryStrategy.REPLAN_SUBTASK):
        recovery_plan = await llm.recover_from_error(
            error=error,
            observation=observation,
            action_history=context.action_history,
        )
        return RecoveryResult(
            retry=True,
            recovery_actions=recovery_plan.actions,
        )

    # 5. それでもダメならユーザーに
    return await escalate_to_user(context, error)
```

### エラークラス分類

```python
def classify_error(error: VerificationError, obs: Observation) -> RecoveryStrategy:
    """エラーの種類に応じて回復戦略を選ぶ"""

    # 要素が見つからない → 画面外かも → スクロール
    if isinstance(error, ElementNotFoundError):
        return RecoveryStrategy.SCROLL_AND_RETRY

    # 予期しないダイアログ → 閉じる
    if isinstance(error, UnexpectedDialogError):
        return RecoveryStrategy.CLOSE_DIALOG

    # 想定と違うウィンドウがアクティブ → Alt+Tab
    if isinstance(error, WrongWindowError):
        return RecoveryStrategy.ALT_WINDOW

    # UIが変わった → LLMに判断させる
    if isinstance(error, UIStateChangedError):
        return RecoveryStrategy.REPLAN_SUBTASK

    # タイムアウト → リトライ
    if isinstance(error, TimeoutError):
        return RecoveryStrategy.WAIT_AND_RETRY

    # 分類不能 → デフォルトはLLM判断
    return RecoveryStrategy.ALTERNATIVE_APPROACH
```

## 安全性設計

### アクションホワイトリスト / ブラックリスト

```python
# デフォルトでブロックされる危険操作
BLOCKED_PATTERNS = [
    # コマンド実行
    {"type": "type", "pattern": r"rm\s+-rf"},
    {"type": "type", "pattern": r"sudo\s"},
    {"type": "type", "pattern": r">\s*/dev/"},
    {"type": "type", "pattern": r"mkfs\."},
    {"type": "type", "pattern": r"dd\s+if="},
    {"type": "type", "pattern": r":\(\)\s*\{",       # fork bomb

    # キーコンボ
    {"type": "key_combo", "keys": ["ctrl", "alt", "f1"]},  # TTY切替
    {"type": "key_combo", "keys": ["ctrl", "alt", "delete"]},

    # ネットワーク操作の制限（オプション）
    # {"type": "type", "pattern": r"curl|wget"},  # 必要に応じて
]
```

### レート制限

```python
@dataclass
class RateLimiter:
    max_actions_per_second: float = 2.0     # 1秒あたり最大2アクション
    max_actions_per_task: int = 200         # 1タスクあたり最大200アクション
    max_task_duration_seconds: int = 600    # 1タスク最大10分
    min_interval_between_actions: float = 0.2  # アクション間最小間隔
```

### ユーザー制御

- **緊急停止**: Web UIの「停止」ボタンで即時中断。VMにSIGSTOPは送らず、実行中のアクションのみキャンセル
- **一時停止/再開**: PAUSED状態でVMはそのまま維持
- **ステップ実行**: 1アクションずつ手動で進めるデバッグモード
- **操作ログの全記録**: 全アクション、全スクリーンショット、LLMの判断理由を保存

### VM隔離

- QEMUは `-sandbox on` オプションでseccomp分離を有効化
- VMにはホストファイルシステムをマウントしない（共有フォルダなし）
- ネットワークはNAT（ホストからの外向きのみ許可）
- VMイメージはスナップショットから起動し、終了時に破棄（イミュータブル運用）

## Docker 配備設計

### コンテナ構成

アプリ本体（backend + frontend + websockify）は Docker Compose で起動する。VM（QEMU/KVM）はホスト側で動作する。これにより:

- アプリの配布と起動が `docker compose up` で完結する
- VMとアプリが独立しているため、VMの再起動がアプリに影響しない
- Windows (WSL2) / Linux / macOS のどの環境でも同じ構成で動作する

```
┌── Docker Compose ────────────────────────────────────────┐
│                                                           │
│  ┌────────────────┐  ┌──────────────┐  ┌──────────────┐ │
│  │  frontend      │  │  backend      │  │  websockify  │ │
│  │  Next.js       │──│  FastAPI      │  │  VNC→WS中継  │ │
│  │  :3000         │  │  :8080        │  │  :6080→:5900 │ │
│  │  (Static →     │  │               │──│               │ │
│  │   backend:8080)│  │               │  │               │ │
│  └────────────────┘  └──────┬────────┘  └──────┬────────┘ │
│                             │                   │          │
└─────────────────────────────┼───────────────────┼──────────┘
                              │ host.docker.      │ host.docker.
                              │ internal:5900     │ internal:5900
                              ▼                    ▼
┌── ホスト ─────────────────────────────────────────────────┐
│  QEMU/KVM プロセス                                         │
│  VM (Ubuntu Desktop) :5900                                 │
└───────────────────────────────────────────────────────────┘
```

### docker-compose.yml

```yaml
version: "3.9"

services:
  backend:
    build:
      context: .
      dockerfile: Dockerfile
    ports:
      - "8080:8080"
    environment:
      - VNC_HOST=${VNC_HOST:-host.docker.internal}
      - VNC_PORT=${VNC_PORT:-5900}
      - LLM_PROVIDER=${LLM_PROVIDER:-anthropic}
      - LLM_MODEL=${LLM_MODEL:-claude-sonnet-4-20250514}
      - ANTHROPIC_API_KEY=${ANTHROPIC_API_KEY}
      - OPENAI_API_KEY=${OPENAI_API_KEY:-}
      - GOOGLE_API_KEY=${GOOGLE_API_KEY:-}
    volumes:
      - ./data:/app/data
      - /var/run/docker.sock:/var/run/docker.sock  # オプション: VM管理
    restart: unless-stopped

  frontend:
    build:
      context: ./frontend
      dockerfile: Dockerfile
    ports:
      - "3000:3000"
    environment:
      - NEXT_PUBLIC_BACKEND_URL=http://localhost:8080
      - NEXT_PUBLIC_WEBSOCKIFY_URL=http://localhost:6080
    depends_on:
      - backend
    restart: unless-stopped

  websockify:
    image: ghcr.io/novnc/websockify:latest
    command: ${VNC_PORT:-5900}
    ports:
      - "6080:5900"  # ブラウザは 6080 に接続し内部で 5900 → VNC
    restart: unless-stopped
```

### 環境ごとの起動方法

#### Linux

```bash
# VM起動 (ホスト側)
./scripts/start_vm.sh

# アプリ起動 (Docker)
docker compose up -d

# ブラウザで http://localhost:3000
```

#### Windows 11 (WSL2)

```powershell
# WSL2 インストール（初回のみ）
wsl --install -d Ubuntu-24.04
wsl --set-default-version 2

# WSL2 内でKVM確認
wsl ls -la /dev/kvm

# WSL2 内で VM 起動
wsl bash scripts/start_vm.sh

# Docker Desktop で起動
docker compose up -d
# → http://localhost:3000
```

#### macOS

```bash
# QEMU + HVF で VM 起動
./scripts/start_vm.sh --accel hvf

# Docker Desktop で起動
docker compose up -d
# → http://localhost:3000
```

### コンテナ間通信

| From | To | 経路 |
|------|-----|------|
| frontend (ブラウザ) | backend API | `localhost:8080` (ポート公開) |
| frontend (ブラウザ) | websockify | `localhost:6080` (ポート公開) |
| backend | VM (VNC) | `host.docker.internal:5900` (ホストネットワーク) |
| websockify | VM (VNC) | `host.docker.internal:5900` (ホストネットワーク) |

### Dockerfile (backend)

```dockerfile
FROM python:3.12-slim

WORKDIR /app

# uv で依存解決
COPY pyproject.toml uv.lock* ./
RUN pip install uv && uv sync --frozen

COPY src/ ./src/

EXPOSE 8080
CMD ["uv", "run", "python", "-m", "ai_desktop_agent"]
```

### Dockerfile (frontend)

```dockerfile
FROM node:22-alpine AS builder
WORKDIR /app
COPY package.json package-lock.json* ./
RUN npm ci
COPY . .
RUN npm run build

FROM node:22-alpine AS runner
WORKDIR /app
COPY --from=builder /app/.next ./.next
COPY --from=builder /app/public ./public
COPY --from=builder /app/node_modules ./node_modules
COPY --from=builder /app/package.json ./
EXPOSE 3000
CMD ["npm", "start"]
```

### 設計上の判断

- **VMはDocker外**: QEMU/KVMをコンテナ内で動かすには privileged modeが必要。VMを分離することでコンテナの特権昇格を回避し、セキュリティを向上
- **host.docker.internal**: DockerコンテナからホストのVNCに接続する標準的な方法。Linuxでは `extra_hosts` で明示指定も可
- **websockifyをコンテナに含める**: noVNC + websockifyはDockerイメージが公開されているため、自前ビルド不要
