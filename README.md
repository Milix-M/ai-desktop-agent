# AI Desktop Agent

自然言語の指示で仮想マシンのGUIをAIが直接操作するデスクトップ作業自動化アプリ。ユーザーはWebブラウザから指示を出し、AIがVMを操作する様子をリアルタイムで視聴できる。

## 概要

```
ユーザー (ブラウザ) → Web UI → バックエンド (FastAPI) → AIエージェント → VM (QEMU/VNC)
                              ↑                                |
                              └── ライブ画面配信 (noVNC) ←─────┘
```

ユーザーがWebのチャット画面から自然言語で指示を出すと、AIエージェントがVMのスクリーンショットを取得し、マルチモーダルLLMで状況を判断、マウス・キーボード操作を実行する。その様子は埋め込みnoVNCビューアを通じてリアルタイムで確認できる。

## アーキテクチャ

```
┌─────────────────────────────────────────────────────┐
│  ブラウザ (Web UI - Next.js)                         │
│  ┌───────────────────┐  ┌────────────────────────┐  │
│  │  指示入力パネル     │  │  noVNC (VM画面ライブ)   │  │
│  │  チャット/ログ      │  │  リアルタイム視聴       │  │
│  └───────────────────┘  └────────────────────────┘  │
└──────────────┬──────────────────────┬───────────────┘
               │ WebSocket            │ WebSocket (noVNC)
               ▼                      ▼
┌─────────────────────────────────────────────────────┐
│  バックエンドサーバー (Python / FastAPI)               │
│  ┌──────────────┐  ┌─────────────┐  ┌───────────┐  │
│  │  Chat API    │  │ Agent Loop  │  │websockify │  │
│  │  (指示受付)   │  │  (AI制御)   │  │(VNC→WS中継)│  │
│  └──────────────┘  └──────┬──────┘  └─────┬─────┘  │
└──────────────────────────┬─────────────────┬────────┘
                           │ VNCプロトコル    │
                           ▼                  ▼
┌─────────────────────────────────────────────────────┐
│  QEMU VM (Linuxデスクトップ)                         │
│  VNCサーバー :5900                                   │
│  (Ubuntu + Xfce などの軽量DE)                        │
└─────────────────────────────────────────────────────┘
```

## 技術スタック

### 仮想マシン

| 方式 | メリット | デメリット | 適性 |
|------|---------|-----------|------|
| **QEMU/KVM** | 高い隔離性、GPUパススルー可、枯れた技術 | やや重い、セットアップに知識必要 | ★★★ 本番向き |
| **Docker + Xvfb + x11vnc** | 軽量高速、コンテナ管理容易、イメージ配布が簡単 | 完全なVM隔離ではない、カーネル共有 | ★★★ 開発/CI向き |
| **VirtualBox** | GUI管理ツール充実、クロスプラットフォーム | ヘッドレス運用がやや面倒、VBoxManage依存 | ★★☆ 個人利用向き |
| **クラウドVM** (EC2/GCE等) | スケーラブル、GPU選択可能 | コスト、ネットワーク遅延 | ★★☆ 大規模向き |

**選定方針**: 第一候補は QEMU/KVM。開発環境では Docker+Xvfb の軽量構成も選択肢に入れる。将来的にはプラグイン方式でVMバックエンドを切り替え可能にする。

### LLM / AI モデル

特定のプロバイダに依存せず、**LLMプロバイダ抽象化レイヤー**を設けて複数のAPIに対応する。

```python
# プロバイダ抽象化のイメージ
class LLMProvider(Protocol):
    """LLMプロバイダの共通インターフェース"""
    async def decide_action(
        self,
        instruction: str,
        screenshot: bytes,
        action_history: list[Action],
        current_state: AgentState,
    ) -> AgentAction: ...

class AnthropicProvider(LLMProvider): ...     # Claude (Computer Use)
class OpenAIProvider(LLMProvider): ...         # GPT-4o / GPT-4.1
class GoogleProvider(LLMProvider): ...         # Gemini
class OllamaProvider(LLMProvider): ...         # ローカルモデル
class OpenAICompatibleProvider(LLMProvider): ... # vLLM, LiteLLM等
```

| プロバイダ | モデル例 | 特徴 |
|-----------|---------|------|
| Anthropic | Claude Opus/Sonnet | Computer Useネイティブ対応、座標出力精度が高い |
| OpenAI | GPT-4o / GPT-4.1 | 汎用性能高い、Vision API安定 |
| Google | Gemini 2.5 Pro | コンテキスト長が長い、マルチモーダル性能高い |
| ローカル (Ollama) | Llama 4, Qwen 等 | API費用ゼロ、プライバシー重視、精度は劣る |
| OpenAI互換 (vLLM) | 任意 | 自前GPUで任意モデルをホスティング |

### フロントエンド

- **Next.js** (App Router) を採用
  - チャットUI（指示入力 + 操作ログ表示）
  - noVNC埋め込みビューア（VM画面のリアルタイム視聴）
  - WebSocket接続でバックエンドとリアルタイム通信
  - VM管理パネル（起動/停止/再起動）

### バックエンド

- **FastAPI** + **WebSocket**
  - 指示受付API
  - エージェント制御用WebSocket
  - websockify連携（VNC→WS中継）
  - タスクキュー管理（バックグラウンドジョブ）

## エージェント設計

単純な「スクショ→LLM→操作→繰り返し」のループでは実際のデスクトップ操作は安定しない。堅牢な動作のために**多段階パイプライン**を採用する。

詳細は [`docs/architecture.md`](docs/architecture.md) を参照。

## プロジェクト構成

```
ai-desktop-agent/
├── pyproject.toml
├── README.md
├── docs/
│   └── architecture.md     # エージェント詳細設計
├── src/
│   └── ai_desktop_agent/
│       ├── __init__.py
│       ├── main.py              # エントリポイント
│       ├── config.py            # 設定管理
│       ├── server/
│       │   ├── __init__.py
│       │   ├── app.py           # FastAPIアプリケーション
│       │   ├── routes.py        # HTTP/WebSocketルート
│       │   └── static/          # フロントエンド資材
│       ├── agent/
│       │   ├── __init__.py
│       │   ├── loop.py          # メインエージェントループ（状態機械）
│       │   ├── planner.py       # タスク分解と計画立案
│       │   ├── executor.py      # アクション実行エンジン
│       │   ├── verifier.py      # 実行結果の検証
│       │   ├── recovery.py      # エラー回復戦略
│       │   ├── state.py         # エージェント状態管理
│       │   └── llm/
│       │       ├── __init__.py
│       │       ├── base.py      # LLMプロバイダ抽象インターフェース
│       │       ├── anthropic.py # Anthropic (Claude)
│       │       ├── openai.py    # OpenAI (GPT-4o)
│       │       ├── google.py    # Google (Gemini)
│       │       ├── ollama.py    # Ollama (ローカル)
│       │       └── openai_compat.py # OpenAI互換 (vLLM等)
│       ├── vm/
│       │   ├── __init__.py
│       │   ├── base.py          # VMバックエンド抽象化
│       │   ├── qemu.py          # QEMU/KVMバックエンド
│       │   ├── docker_xvfb.py   # Docker + Xvfb バックエンド
│       │   ├── vnc_client.py    # VNC接続と制御
│       │   └── screenshot.py    # 画面キャプチャ + OCR
│       └── actions/
│           ├── __init__.py
│           ├── primitives.py    # 基本アクション定義
│           └── executor.py      # アクション実行 (vncdotool)
├── frontend/                    # Next.js アプリケーション
│   ├── package.json
│   ├── next.config.js
│   ├── src/
│   │   ├── app/
│   │   │   ├── layout.tsx
│   │   │   ├── page.tsx        # メインダッシュボード
│   │   │   └── globals.css
│   │   ├── components/
│   │   │   ├── ChatPanel.tsx    # 指示入力 + ログ
│   │   │   ├── VMViewer.tsx     # noVNC埋め込み
│   │   │   └── VMControls.tsx   # VM管理パネル
│   │   └── lib/
│   │       └── websocket.ts    # WebSocketクライアント
│   └── public/
│       └── novnc/              # noVNC静的ファイル
└── scripts/
    ├── start_vm.sh
    └── setup_vm_image.sh
```

## 安全性設計

- **VM隔離**: AIはサンドボックスVM内で動作し、ホストに影響を与えない
- **アクションレート制限**: ループ暴走の防止（1秒あたり最大Nアクション）
- **ユーザー割り込み**: Web UIからいつでもエージェントを停止可能
- **操作ログ**: 全アクションを記録、チャットパネルで確認可能
- **アクションホワイトリスト**: 危険操作（`rm -rf`、`sudo`等）はデフォルトブロック、明示許可制

## ロードマップ

- [ ] QEMU VMの基本管理（起動/停止/スナップショット）
- [ ] Docker + Xvfb バックエンド（軽量開発用）
- [ ] VNC経由の画面キャプチャと操作実行
- [ ] LLMプロバイダ抽象化レイヤー（Anthropic / OpenAI / Gemini / Ollama）
- [ ] 多段階エージェントパイプライン（計画→実行→検証→回復）
- [ ] FastAPIバックエンド + WebSocket
- [ ] noVNC統合（ライブ視聴）
- [ ] Next.jsフロントエンド（チャット + ビューア）
- [ ] OCRによる画面テキスト抽出
- [ ] 操作履歴とログ機能
- [ ] エラーリカバリとリトライ戦略
- [ ] 複数VM対応
- [ ] 定型タスクのテンプレート機能

## ライセンス

MIT
