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
│  │  :3000       │  │  :8081       │  │  :6080→vm:5900│  │
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

ブラウザ → `localhost:3000`（frontend）。frontend→backend (`:8081`)、websockify→vm (`vm:5900`)、backend→vm (`vm:5900`) はすべてDocker内部ネットワークで通信。

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

**KVM有効化**: `.env` で `USE_KVM=true` にする（`docker-compose.yml` の `vm` は `/dev/kvm` をマウント済み）。無効時（デフォルト `false`）はTCGソフトウェアエミュレーションで動作するが低速。ホストに `/dev/kvm` がない環境では `devices` の2行を削除すること。

**デバッグ: VM作り直し**: 操作UIのサイドバー「VM管理（デバッグ）」から `VM作り直し` ボタンでVMコンテナを再起動できる（ゲストOSごとクリーンブート、実行中タスクは停止）。backendがDockerソケット（`/var/run/docker.sock` マウント）経由で操作する。API直叩きの場合: `POST /vm/restart`、`GET /vm/status`。


### LLM / AI モデル

特定のプロバイダに依存せず、**LLMプロバイダ抽象化レイヤー**を設けて複数のAPIに対応する。

| プロバイダ | モデル例 |
|-----------|---------|
| Anthropic | Claude (Computer Use) |
| OpenAI | GPT-4o, GPT-4.1 |
| Google | Gemini |
| ローカル (Ollama) | Llama, Qwen 等 |
| OpenAI互換 (vLLM) | 任意 |
| OpenCode Zen | DeepSeek / GPT / Claude / Gemini 等（`chat/completions`互換モデル） |
| OpenCode Go | 月額制のopenモデル群（`chat/completions`互換モデル。独自UA付き） |

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
  - タスク履歴の永続化（`data/tasks/` にJSON保存、`GET /tasks` で履歴取得。再起動で中断扱い）

## エージェント設計

単純な「スクショ→LLM→操作→繰り返し」のループでは実際のデスクトップ操作は安定しない。堅牢な動作のために**多段階パイプライン**を採用する。

詳細は [`docs/architecture.md`](docs/architecture.md) を参照。

## プロジェクト構成

```
ai-desktop-agent/
├── pyproject.toml
├── README.md
├── docker-compose.yml       # Docker Compose 構成（vm/backend/frontend/websockify）
├── docker-compose.override.yml  # ローカル用上書き（任意・git管理外）
├── Dockerfile               # backend コンテナ定義
├── docs/
│   └── architecture.md     # エージェント詳細設計
├── src/
│   └── ai_desktop_agent/
│       ├── main.py              # エントリポイント
│       ├── server/
│       │   ├── app.py           # FastAPIアプリ・REST/WSルート
│       │   ├── session.py       # タスク実行セッション（状態機械の駆動）
│       │   ├── store.py         # タスク履歴の永続化（data/tasks）
│       │   └── vm_control.py    # VMコンテナ管理（Docker経由の再起動）
│       ├── agent/
│       │   ├── loop.py          # 状態機械の遷移管理
│       │   ├── state.py         # Goal/Subtask/履歴の定義
│       │   └── llm/
│       │       ├── base.py      # LLMプロバイダ抽象インターフェース
│       │       ├── factory.py   # プロバイダ生成（openai/anthropic/openrouter/opencode/ollama/mock）
│       │       ├── openai_compat_provider.py # OpenAI互換API実装（画像つき判断の中核）
│       │       ├── types.py     # 決定・検証・回復の型定義
│       │       └── mock.py      # テスト用モック
│       ├── vm/
│       │   ├── base.py          # 表示バックエンド抽象化
│       │   ├── vnc_client.py    # VNC接続と制御（vncdotool）
│       │   ├── screenshot.py    # 画面キャプチャ値オブジェクト
│       │   ├── overlay.py       # 座標グリッド重畳・領域ズーム
│       │   └── fake.py          # テスト用フェイク
│       └── actions/
│           ├── primitives.py    # アクション定義・バリデーション
│           └── executor.py      # アクション実行エンジン
├── vm/                          # VMコンテナ用ビルドコンテキスト
│   ├── Dockerfile               # QEMU/KVMコンテナ
│   ├── entrypoint.sh            # QEMU起動スクリプト
│   ├── build-vm-image.sh        # ゲストOSイメージ構築
│   ├── desktop.qcow2            # ゲストディスク（生成物・git管理外）
│   ├── vmlinuz / initrd.img / cmdline.txt  # ゲストカーネル一式（生成物）
├── frontend/                    # Next.js アプリケーション
│   ├── package.json
│   ├── src/
│   │   ├── app/
│   │   │   ├── layout.tsx
│   │   │   ├── page.tsx        # メインダッシュボード（状態復元つき）
│   │   │   └── globals.css
│   │   ├── components/
│   │   │   ├── InstructionInput.tsx  # 指示入力
│   │   │   ├── LogPanel.tsx          # 操作ログ
│   │   │   ├── StatusPanel.tsx       # エージェント状態
│   │   │   ├── StatusBar.tsx         # VNC/VM状態バー
│   │   │   ├── ControlPanel.tsx      # 一時停止/再開/停止
│   │   │   ├── VMControls.tsx        # VM管理（デバッグ用作り直し）
│   │   │   ├── TaskHistory.tsx       # タスク履歴
│   │   │   └── VncViewer.tsx         # noVNC埋め込み＋状態表示
│   │   ├── hooks/
│   │   │   └── useWebSocket.ts       # WebSocketクライアント
│   │   └── lib/
│   │       ├── api.ts                # backend APIクライアント
│   │       └── types.ts
├── websockify/                  # VNC→WebSocket中継コンテキスト
└── data/tasks/                      # タスク履歴の保存先（git管理外）
```

## 使い方

### 起動

```bash
cp .env.example .env   # APIキー・USE_KVMを設定
docker compose up -d --build
```

* 操作UI: `http://localhost:3000`
* backend: `http://localhost:8081`（`/health`で確認）
* 3000番が他プロセスと競合する場合は `docker-compose.override.yml` でずらす

### 主なAPI

| メソッド | パス | 用途 |
|---|---|---|
| GET | `/health` | 生存確認 |
| POST | `/tasks` | タスク投入（`{"instruction": "..."}`） |
| GET | `/tasks` | 履歴一覧 |
| GET | `/tasks/{id}` | タスク詳細（操作履歴つき） |
| GET | `/tasks/current` | 最新タスク状態（リロード後の復元用） |
| POST | `/tasks/current/{pause,resume,stop}` | タスク制御 |
| GET | `/vm/status` | VMコンテナ状態 |
| POST | `/vm/restart` | VM作り直し（再起動） |
| WS | `/ws` | 状態・操作ログのプッシュ配信 |

### 開発

```bash
uv run pytest -q          # backendテスト
uv run ruff check src tests
cd frontend && npm test   # frontendテスト（vitest）
```

## 安全性設計

- **VM隔離**: AIはサンドボックスVM内で動作し、ホストに影響を与えない
- **ステップ上限**: 1タスク200アクションで打ち切り（トークン燃費対策）
- **ユーザー割り込み**: Web UIからいつでも一時停止・停止可能
- **操作ログ**: 全アクション＋LLMの判断理由を記録・永続化し、履歴から確認可能
- **画面ブランク対策**: ゲストのDPMS無効化＋真っ黒検出時の自動ウェイク
- 未実装: アクションレート制限、危険操作のホワイトリスト（予定）

## ロードマップ

- [x] QEMU VMの基本管理（Dockerコンテナ内で起動/停止）
- [x] VNC経由の画面キャプチャと操作実行
- [x] LLMプロバイダ抽象化レイヤー（OpenAI互換でAnthropic / OpenAI / Gemini / Ollama / OpenCode Zen対応）
- [x] 多段階エージェントパイプライン（計画→実行→検証→回復）
- [x] FastAPIバックエンド + WebSocket
- [x] noVNC統合（ライブ視聴）
- [x] Next.jsフロントエンド（チャット + ビューア）
- [x] 操作履歴とログ機能（永続化つき）
- [x] エラーリカバリとリトライ戦略
- [ ] OCRによる画面テキスト抽出（現在はVLMの読解に依存）
- [ ] 複数VM対応
- [ ] 定型タスクのテンプレート機能

## ライセンス

MIT
