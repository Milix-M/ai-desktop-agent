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
│  ブラウザ (Web UI)                                   │
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

| コンポーネント | 技術 | 役割 |
|-------------|------|------|
| 仮想マシン | QEMU/KVM | ローカルVM、VNCによる画面出力 |
| 画面配信 | noVNC + websockify | VNC→WebSocket変換、ブラウザでライブ表示 |
| バックエンド | FastAPI + WebSocket | 指示受付、エージェント統括 |
| AI操作 | vncdotool + LLM API | 画面キャプチャ → 判断 → 操作実行 |
| LLM | Claude / GPT-4o (マルチモーダル) | 画面理解 + 操作計画 |
| フロントエンド | プレーンJS（またはReact） | チャットUI + noVNCビューア埋め込み |

## エージェントループ

```python
async def agent_loop(instruction: str, vnc, llm):
    while not task_complete:
        # 1. VMのスクリーンショットを取得
        screenshot = vnc.capture_screen()

        # 2. マルチモーダルLLMに指示+画面+操作履歴を送信し判断
        action = await llm.decide(instruction, screenshot, action_history)

        # 3. VM上でアクション実行（クリック、入力、スクロール等）
        await vnc.execute_action(action)

        # 4. WebSocketでフロントエンドに進捗通知
        await websocket.broadcast({"status": action.description, "step": step_count})

        # 5. UIの変化を待つ
        await asyncio.sleep(1)
```

## プロジェクト構成

```
ai-desktop-agent/
├── pyproject.toml
├── README.md
├── docs/
│   └── architecture.md
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
│       │   ├── loop.py          # メインエージェントループ
│       │   ├── llm.py           # LLMクライアント (Claude/GPT-4o)
│       │   └── actions.py       # アクション定義と実行
│       └── vm/
│           ├── __init__.py
│           ├── manager.py       # QEMU VMのライフサイクル管理
│           ├── vnc_client.py    # VNC接続と制御
│           └── screenshot.py    # 画面キャプチャユーティリティ
├── frontend/
│   ├── index.html
│   ├── app.js
│   └── style.css
└── scripts/
    ├── start_vm.sh
    └── setup_vm_image.sh
```

## 設計上の重要な判断

### なぜ QEMU + VNC か？
- **隔離性**: AIはサンドボックスVM内で動作し、ホストに影響を与えない
- **VNCプロトコル**: 枯れた技術で画面キャプチャと入力注入の両方をサポート
- **noVNC**: ブラウザで完結するVNCクライアント。ユーザー側のインストール不要
- **ローカル完結**: クラウド依存なし、VM環境を完全制御可能

### なぜ noVNC でライブ視聴か？
- ユーザーはソフトウェアを追加インストールせずにAIの操作をリアルタイム視聴可能
- noVNC + websockify で VNC ↔ WebSocket 変換を実現
- 読み取り専用モードによりAI操作中のユーザー干渉を防止可能

### 安全性設計
- VM隔離によりAIがホストに影響を及ぼせない
- アクションレート制限（ループ暴走の防止）
- ユーザーはWeb UIからいつでもエージェントを停止可能
- 全アクションはログに記録されチャットパネルで確認可能

## クイックスタート

> 🚧 開発中

### 前提条件

- Python 3.12 以上
- QEMU/KVM
- VMイメージ（Ubuntu Desktop推奨）
- Claude または GPT-4o のAPIキー

### インストール

```bash
git clone https://github.com/Milix-M/ai-desktop-agent.git
cd ai-desktop-agent
uv sync
```

### 起動方法

```bash
# VMとWebサーバーを起動
uv run python -m ai_desktop_agent

# ブラウザで http://localhost:8080 にアクセス
```

## ロードマップ

- [ ] QEMU VMの基本管理（起動/停止）
- [ ] VNC経由の画面キャプチャと操作実行
- [ ] マルチモーダルLLMによるエージェントループ
- [ ] FastAPIバックエンド + WebSocket
- [ ] noVNC統合（ライブ視聴）
- [ ] Web UI（指示パネル + ビューア）
- [ ] 操作履歴とログ機能
- [ ] エラーリカバリとリトライ
- [ ] 複数VM対応
- [ ] 定型タスクのテンプレート機能

## ライセンス

MIT
