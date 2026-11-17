# AI Desktop Agent

自然言語の指示で、VMやコンテナ内のデスクトップをAIが操作する。ブラウザで画面を見ながら、指示・一時停止・停止・履歴の確認ができる。

## 起動する

Docker EngineとDocker Compose、画像入力に対応するLLMのAPIキーが必要になる。ローカルの1人利用を対象とする。

KVMがあるホストでは、先に `sudo ./vm/build-vm-image.sh` でゲストを用意する。`vm/desktop.qcow2`、`vm/vmlinuz`、`vm/initrd.img`、`vm/cmdline.txt` が必要になる。このスクリプトはホストにパッケージを導入し、ゲストイメージを作成する。

```bash
cp .env.example .env
# .envにLLM_PROVIDERと対応するAPIキーを設定する
./scripts/up.sh
```

[http://localhost:3000](http://localhost:3000) を開く。KVMがある場合はQEMU VM、ない場合はXfceのコンテナが起動する。Androidは初期起動に含まない。

| 接続先 | ポート |
| --- | --- |
| 操作画面 | 3000 |
| API・WebSocket | 8081 |
| 画面配信 | コンテナは6081、QEMUは6080 |
| VNC | コンテナは5901、QEMUは5900 |

公開ポートはホストのループバックに限定している。別ホストや別ポートからの接続は、Originの許可設定も必要になる。

## 指示を出す

環境のタブを選び、指示を入力して［実行］を選ぶ。Enterで送信、Shift+Enterで改行する。日本語変換中のEnterでは送信しない。

同じ環境への後発タスクは先行タスクを停止する。別の環境では並列実行できる。1タスクは回復操作を含めて200アクションまでで、エラー回復にも再試行上限がある。

［VM再起動を許可する］を選ぶと、AIの判断で環境を再起動できる。再起動はディスクを初期化しない。VNCと状態通知は切断後に再接続し、各環境の状態を同期する。

タブの停止・起動は固定環境と追加した環境の両方に使える。動的環境を削除すると、その環境のディスクも削除する。履歴の削除は環境のディスクを消さない。

## LLMを選ぶ

`.env` の `LLM_PROVIDER`、`LLM_MODEL`、対応するキーで切り替える。モデルは画像入力に対応するものを選ぶ。

| LLM_PROVIDER | キー |
| --- | --- |
| `openai` | `OPENAI_API_KEY` |
| `anthropic` | `ANTHROPIC_API_KEY` |
| `openrouter` | `OPENROUTER_API_KEY` |
| `opencode` | `OPENCODE_API_KEY` |
| `opencode-go` | `OPENCODE_GO_API_KEY` |
| `ollama` | 不要 |

Ollamaの既定接続先は `http://localhost:11434/v1` で、バックエンドコンテナ内のlocalhostを指す。ホスト上のOllamaへつなぐ設定は別途必要になる。

## 履歴を保存する

タスクと操作記録は `data/tasks/`、追加環境の情報は `data/vms.json` に保存する。バックエンド再起動時に残っていた実行中タスクは中断扱いになる。保存先のバックアップには入力内容が含まれるため、取り扱いに注意する。

VNC経由の文字入力はASCIIに限る。日本語の指示を理解しても、アプリ内に日本語の本文を入力する経路は未対応となる。

## 開発時に確認する

```bash
uv sync --frozen
uv run pytest -q
uv run ruff check src/ tests/
uv run ruff format --check src/ tests/
cd frontend
npm ci
npm test
npm run lint
npm run typecheck
npm run build
```

実デスクトップの確認はリポジトリのルートで実行する。APIキーは使わず、Firefoxの描画とVNC入力後のファイル内容を検査する。LLMの判断精度を測るテストではない。

```bash
docker build -t ai-desktop-agent-desktop-smoke desktop
RUN_DESKTOP_SMOKE=1 uv run pytest -m desktop_integration tests/integration/
```

CIでも実デスクトップを検査する。通常の単体テストではこの検査をスキップする。

## 詳細

- [構成とAPI](docs/architecture.md)
- [安全性と利用範囲](docs/safety.md)
- [本番利用に向けた確認事項](docs/production-readiness.md)
- [Androidの準備](android/README.md)
- [決定モデルの評価](docs/research/clef.md)

ライセンスはMIT。
