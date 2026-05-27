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

アプリ全体を **Docker Compose** で完結させる。VMもDockerコンテナ内で動作する（Linux/WSL2のみ。macOSではネストKVMが非対応のためホスト側QEMUを併用）。

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

QEMU/KVMは **WSL2 上で動作する**（Windows 11 は WSL2 の KVM を正式サポート）。macOS は QEMU + HVF で代用可能。

| 方式 | メリット | デメリット | 適性 |
|------|---------|-----------|------|
| **QEMU/KVM** (Linux/WSL2) | 高い隔離性、GPUパススルー可、枯れた技術 | やや重い、セットアップに知識必要 | ★★★ 本番向き |
| **QEMU/HVF** (macOS) | Apple Siliconで高速、Mac標準搭載 | x86_64エミュレーションは遅い | ★★☆ macOS向き |
| **Docker + Xvfb + x11vnc** | 軽量高速、コンテナ管理容易、イメージ配布が簡単 | 完全なVM隔離ではない、カーネル共有 | ★★★ 開発/CI向き |
| **VirtualBox** | GUI管理ツール充実、クロスプラットフォーム | ヘッドレス運用がやや面倒、VBoxManage依存 | ★★☆ 個人利用向き |
| **クラウドVM** (EC2/GCE等) | スケーラブル、GPU選択可能 | コスト、ネットワーク遅延 | ★★☆ 大規模向き |

**選定方針**: 第一候補は QEMU/KVM。Windows 環境では WSL2 内で KVM が利用可能（Windows 11 Pro/Enterprise で `/dev/kvm` 有効）。開発環境では Docker+Xvfb の軽量構成も選択肢に入れる。将来的にはプラグイン方式でVMバックエンドを切り替え可能にする。

### Docker によるアプリ配備

アプリ全体（vm + backend + frontend + websockify）を1つの `docker-compose.yml` で完結させる。VMは `/dev/kvm` をマウントした専用コンテナ内でQEMU/KVMを起動する。

```yaml
# docker-compose.yml (予定)
services:
  vm:
    build:
      context: ./vm
      dockerfile: Dockerfile
    devices:
      - /dev/kvm:/dev/kvm          # KVMアクセラレーション
    ports:
      - "5900:5900"                 # VNC (内部通信用)
    volumes:
      - vm_data:/vm
    environment:
      - VM_IMAGE=/vm/desktop.qcow2
      - VM_MEMORY=4096
      - VM_VNC_PORT=5900
    restart: unless-stopped
    healthcheck:
      test: ["CMD", "bash", "-c", "echo | ncat localhost 5900"]
      interval: 10s
      retries: 5

  backend:
    build: .
    ports: ["8080:8080"]
    environment:
      - VNC_HOST=vm                 # Docker内部ネットワークでVMに接続
      - VNC_PORT=5900
      - LLM_PROVIDER=${LLM_PROVIDER:-anthropic}
      - LLM_MODEL=${LLM_MODEL:-claude-sonnet-4-20250514}
      - ANTHROPIC_API_KEY=${ANTHROPIC_API_KEY}
      - OPENAI_API_KEY=${OPENAI_API_KEY:-}
    volumes:
      - ./data:/app/data
    depends_on:
      vm:
        condition: service_healthy
    restart: unless-stopped

  frontend:
    build: ./frontend
    ports: ["3000:3000"]
    environment:
      - NEXT_PUBLIC_BACKEND_URL=http://localhost:8080
      - NEXT_PUBLIC_WEBSOCKIFY_URL=http://localhost:6080
    depends_on: [backend]

  websockify:
    image: ghcr.io/novnc/websockify:latest
    command: vm:5900               # Docker内部ネットワーク経由でVMに接続
    ports: ["6080:5900"]

volumes:
  vm_data:
```

**動作環境**:

| OS | 要件 | VM動作 | 備考 |
|----|------|--------|------|
| Linux | QEMU + KVM + Docker | Docker内KVM | ネイティブ動作、最速 |
| Windows 11 | WSL2 + KVM有効化 + Docker Desktop | Docker内KVM | `wsl --install` でWSL2導入、BIOSで仮想化有効 |
| macOS | QEMU + HVF + Docker Desktop | **ホスト側QEMU** | ネストKVM非対応のためvmコンテナはスキップ、代わりにホスト側でVM起動 |

**macOS 用のフォールバック**:
```yaml
# docker-compose.mac.yml (macOS向けオーバーライド)
services:
  vm:
    profiles: ["linux"]            # macOSでは無効化

  backend:
    environment:
      - VNC_HOST=host.docker.internal  # ホスト側QEMUに接続

  websockify:
    command: host.docker.internal:5900
```
```bash
# macOS
./scripts/start_vm.sh --accel hvf   # ホスト側でVM起動
docker compose -f docker-compose.yml -f docker-compose.mac.yml up -d
```

**WSL2 の KVM 有効化**（Windows 11）:
```powershell
# Windows側
wsl --install -d Ubuntu-24.04
wsl --set-default-version 2
# WSL2内でKVM利用可能か確認
ls -la /dev/kvm  # 存在すればOK
```

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
│       │   ├── qemu.py          # QEMU/KVMバックエンド
│       │   ├── docker_xvfb.py   # Docker + Xvfb バックエンド
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
