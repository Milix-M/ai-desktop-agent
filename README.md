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

アプリ全体を **Docker Compose** で完結させる。VMもDockerコンテナ内で動作する。

```
┌── Docker Compose ────────────────────────────────────────┐
│                                                           │
│  ┌──────────────┐  ┌─────────────┐  ┌───────────────┐  │
│  │  frontend    │  │  backend     │  │  websockify   │  │
│  │  (Next.js)   │  │  (FastAPI)   │  │  (VNC→WS中継) │  │
│  │  :3000       │  │  :8080       │  │  :6080→vm:5900│  │
│  └──────────────┘  └──────┬───────┘  └───────┬───────┘  │
│                           │                   │          │
│                           │  ┌────────────────┘          │
│                           │  │ Docker 内部ネットワーク     │
│                           ▼  ▼                           │
│                    ┌──────────────┐                      │
│                    │  vm          │  ← /dev/kvm マウント  │
│                    │  QEMU/KVM    │                      │
│                    │  :5900       │                      │
│                    └──────────────┘                      │
└──────────────────────────────────────────────────────────┘
```

| レイヤー | 場所 | 役割 |
|---------|------|------|
| frontend (Next.js) | Dockerコンテナ | チャットUI + noVNCビューア |
| backend (FastAPI) | Dockerコンテナ | 指示受付、エージェント制御 |
| websockify | Dockerコンテナ | VNC→WebSocket中継 |
| vm (QEMU/KVM) | Dockerコンテナ | AIが操作する隔離環境。`/dev/kvm` をマウント |

ブラウザ → `localhost:3000`（frontend）。frontend→backend (`:8080`)、websockify→vm (`vm:5900`)、backend→vm (`vm:5900`) はすべてDocker内部ネットワークで通信。

## 技術スタック

### 仮想マシン

**QEMU/KVM** を採用。ホストの `/dev/kvm` を Docker コンテナにマウントし、コンテナ内でVMを起動する。

**要件**: ホストが KVM をサポートし、`/dev/kvm` が利用可能であること。

| OS | KVM対応 | 備考 |
|----|---------|------|
| Linux | ✅ ネイティブ | 最速 |
| Windows 11 | ✅ WSL2内で利用可能 | WSL2 + Docker Desktop で `/dev/kvm` が使える |
| macOS | ❌ 非対応 | Docker DesktopのLinux VMがネストKVMをサポートしない |


### Docker によるアプリ配備

アプリ全体（vm + backend + frontend + websockify）を1つの `docker-compose.yml` で完結させる。VMは `/dev/kvm` をマウントした専用コンテナ内でQEMU/KVMを起動する。

**動作環境**:

| OS | 要件 | VM動作 | 備考 |
|----|------|--------|------|
| Linux | QEMU + KVM + Docker | Docker内KVM | ネイティブ動作、最速 |
| Windows 11 | WSL2 + KVM有効化 + Docker Desktop | Docker内KVM | BIOSで仮想化有効 |


### LLM / AI モデル

特定のプロバイダに依存せず、**LLMプロバイダ抽象化レイヤー**を設けて複数のAPIに対応する。

| プロバイダ | モデル例 |
|-----------|---------|
| Anthropic | Claude (Computer Use) |
| OpenAI | GPT-4o, GPT-4.1 |
| Google | Gemini |
| ローカル (Ollama) | Llama, Qwen 等 |
| OpenAI互換 (vLLM) | 任意 |

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

```\nai-desktop-agent/
├── pyproject.toml
├── README.md
├── docker-compose.yml       # Docker Compose 構成
├── Dockerfile               # backend コンテナ定義
├── .dockerignore
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
│       │   ├── vnc_client.py    # VNC接続と制御
│       │   └── screenshot.py    # 画面キャプチャ + OCR
│       └── actions/
│           ├── __init__.py
│           ├── primitives.py    # 基本アクション定義
│           └── executor.py      # アクション実行 (vncdotool)
├── vm/                          # VMコンテナ用ビルドコンテキスト
│   ├── Dockerfile               # QEMU/KVMコンテナ
│   └── entrypoint.sh            # QEMU起動スクリプト
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

- [ ] QEMU VMの基本管理（Dockerコンテナ内で起動/停止）
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
