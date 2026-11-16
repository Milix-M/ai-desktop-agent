# AI Desktop Agent

自然言語の指示で、VMやコンテナ内のデスクトップをAIが操作する。ブラウザで画面を見ながら、指示・一時停止・停止・履歴の確認ができる。

## 起動する

Docker Engine（API 1.48以降）とDocker Compose 2.33.1以降、画像入力に対応するLLMのAPIキーが必要になる。ローカルの1人利用を対象とする。

QEMU VMを使う場合は、x86_64ホストの `/dev/kvm` とゲストファイルが必要になる。先に `sudo ./vm/build-vm-image.sh` を実行する。このスクリプトはホストにパッケージを導入し、`vm/desktop.qcow2`、`vm/vmlinuz`、`vm/initrd.img`、`vm/cmdline.txt` を作成する。

更新でコンテナが再作成される場合がある。作業環境内の必要なファイルは先に退避する。

```bash
cp .env.example .env
# .envにLLM_PROVIDERと対応するAPIキーを設定する
./scripts/up.sh
```

初回起動で接続キーを生成する。次のコマンドで確認し、[http://localhost:3000](http://localhost:3000) のログイン画面に入力する。`.env` の `AGENT_ACCESS_TOKEN` を指定した場合は、その値を使う。

```bash
docker compose exec backend cat /app/data/auth/access-key
```

KVMとゲストファイルが揃う場合はQEMU VM、それ以外はXfceのコンテナが起動する。`.env` の `USE_KVM=false` でコンテナを選べる。`true` で条件が不足する場合は起動前に止まる。Androidは初期起動に含まない。

| 接続先 | ポート |
| --- | --- |
| 操作画面 | 3000 |
| API・状態通知・画面配信 | 8081 |

公開ポートはホストのループバックに限定する。API・状態通知・画面配信は認証を必須とし、VNC・ADBのポートを直接公開しない。旧版の中継は起動時に同じプロジェクトのものだけ撤去する。

各作業環境に専用ネットワークを割り当て、管理APIと別環境への接続を制限する。外部サイトへの接続と、認証後の画面配信は利用できる。

ログインは12時間有効で、バックエンド再起動後は再ログインする。ログアウトは閲覧接続を切断するが、実行中タスクは止めない。停止する場合は先に［停止］を選ぶ。

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

## 実行上限を変える

上限は `.env` に正数で指定する。時間には一時停止中の待機も含み、モデルの再試行と決定モデルも送信回数・使用量に含む。

| 設定 | 既定の上限 |
| --- | --- |
| `TASK_MAX_SECONDS` | 1800秒 |
| `TASK_REQUEST_TIMEOUT_SECONDS` | 1送信の応答待ち120秒 |
| `TASK_MAX_REQUESTS` | 500送信 |
| `TASK_MAX_TOKENS` | 入出力合計100000トークン |

推定費用を制限する場合は、`TASK_MAX_COST_USD` と入力・出力100万トークン当たりのUSD単価 `TASK_INPUT_USD_PER_MILLION`、`TASK_OUTPUT_USD_PER_MILLION` を設定する。複数モデルを使う場合は、各モデルの単価を下回らない値を指定する。

上限に達すると失敗として終了し、履歴に理由と使用量を残す。使用量を返さないモデル応答でも停止する。実行中の操作は終了を待つ場合がある。

使用量は応答後に加算するため、トークンと推定費用は1送信分超過する場合がある。未報告の失敗分や料金体系の差により、推定費用と請求額は異なる場合がある。

## 履歴を保存する

タスクと操作記録は `data/tasks/`、追加環境の情報は `data/vms.json` に保存する。バックエンド再起動時に残っていた実行中タスクは中断扱いになる。保存先に書き込めなければ起動・タスク投入を止め、実行中の保存失敗でも操作を打ち切る。空き容量とアクセス権を確認する。

固定QEMU VMの作業は `vm/instances/default.qcow2`、動的VMは `vm/overlays/` に保存する。`vm/desktop.qcow2` は読み取り専用のテンプレートとなる。履歴とディスクのバックアップには入力内容が含まれるため、取り扱いに注意する。

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

実デスクトップの確認はリポジトリのルートで実行する。LLMのAPIキーは使わず、Firefoxの描画、VNC入力後のファイル内容、環境間の通信遮断、バックエンド再作成後の復旧を検査する。LLMの判断精度を測るテストではない。

```bash
docker build -t ai-desktop-agent-desktop-smoke desktop
docker build -t ai-desktop-agent-backend-smoke .
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
