# AI Desktop Agent

![AI Desktop Agentのプロダクトイメージ](docs/images/readme-hero.png)

自然言語の指示で、AIがVMやコンテナ内のデスクトップを操作する。ブラウザで画面を見ながら、指示を送り、実行を一時停止・停止し、履歴を確認できる。

## 起動する

Docker Engine（API 1.48以降）、Docker Compose 2.34.0以降、画像入力に対応するLLMが必要になる。アプリはローカルでの1人利用を想定している。操作画面とAPIはホスト内から接続し、インターネットには公開しない。

1. リポジトリを取得し、設定ファイルを用意する。

   ```bash
   git clone https://github.com/Milix-M/ai-desktop-agent.git
   cd ai-desktop-agent
   (umask 077; cp -n .env.example .env)
   realpath .env
   ```
2. `.env` に `LLM_PROVIDER` と、必要な場合は対応するAPIキーを設定する。利用できる提供元は[LLMを選ぶ](#llmを選ぶ)を参照する。
3. リポジトリのルートで起動する。

   ```bash
   ./scripts/up.sh
   ```
4. 次のコマンドで接続キーを確認し、[操作画面](http://localhost:3000)に入力する。`.env` に `AGENT_ACCESS_TOKEN` を設定した場合は、その値を使う。

   ```bash
   docker compose exec backend cat /app/data/auth/access-key
   ```

既定では、x86_64ホストに `/dev/kvm` とVMテンプレートが揃っていればQEMU VM、それ以外はXfceコンテナが起動する。QEMU VMを用意するには、手順3の前に `sudo ./vm/build-vm-image.sh` を実行する。このスクリプトはホストにパッケージを導入し、`vm/desktop.qcow2` を作成する。`USE_KVM=false` でコンテナを選べる。`USE_KVM=true` のときは条件が揃わなければ起動を止める。[Androidは別途準備する](android/README.md)。

## 指示を出す

環境のタブを選び、指示を入力して［実行］を選ぶ。Enterで送信し、Shift+Enterで改行する。日本語変換中のEnterでは送信しない。［準備中］［接続異常］の環境には指示を送れない。

同じ環境で次の指示を実行すると、先に実行していたタスクが止まる。［手動入力待ち］の間は新しい指示を受け付けない。別の環境では並行して実行できる。ログアウト後もタスクは続く。終了させるには、先に［停止］を選ぶ。

通信エラーで受付結果が分からないときは、同じ指示・環境・再起動許可で［再送］を選ぶ。受付済みなら同じタスクの状態を返す。混雑時は画面に表示された秒数を待ってから再送する。履歴の削除後にやり直すときは［新しいタスクとして実行］を選ぶ。

パスワードや認証コードを使うときは、指示に値を書かず、AIに手動入力を求めるよう指示する。［手動入力待ち］になったら接続先を確認してデスクトップへ直接入力し、秘密情報を画面から隠して［画面を送信して再開］を選ぶ。[画面送信と履歴の扱い](docs/safety.md#秘密情報を入力する)を参照する。

［VM再起動を許可する］を選ぶと、AIが作業環境を再起動できる。再起動後もディスクは保持する。動的環境の削除ではディスクやホーム用ボリュームも消えるため、必要なファイルを先に保存する。

## 日本語を入力する

更新済みの固定desktopと、更新後に追加したコンテナで日本語や絵文字を入力できる。QEMU・AndroidのUnicode入力は未対応となる。貼り付け時は作業環境のクリップボードを置き換え、1回につきUTF-8で64 KiBまで送る。改行やタブも文字として貼り付けるため、端末では貼り付け後にEnterで実行する。

文字の選択がクリップボード更新で解除される場合がある。選択した文字を置き換えるときは、Backspaceで削除してから入力する。貼り付けが失敗した場合は、デスクトップで入力結果を確認してから次の指示を出す。

## LLMを選ぶ

`.env` の `LLM_PROVIDER`、`LLM_MODEL`、対応するキーで切り替える。モデルは画像入力に対応するものを選ぶ。

| `LLM_PROVIDER` | キー |
| --- | --- |
| `openai` | `OPENAI_API_KEY` |
| `anthropic` | `ANTHROPIC_API_KEY` |
| `openrouter` | `OPENROUTER_API_KEY` |
| `opencode` | `OPENCODE_API_KEY` |
| `opencode-go` | `OPENCODE_GO_API_KEY` |
| `ollama` | 不要 |

Ollamaを使う場合は、Ollamaを動かす環境で `ollama pull qwen2.5vl:3b` を実行し、アプリの `.env` に `LLM_PROVIDER=ollama` を設定する。別の画像対応モデルを使う場合は、取得したモデル名を `LLM_MODEL` に指定する。

`OLLAMA_BASE_URL` には、バックエンドから接続できるOllamaのURLを `/v1` まで指定する。たとえば同じCompose管理ネットワークに `ollama` というサービスがある場合は `http://ollama:11434/v1` を使う。省略時の `http://localhost:11434/v1` はバックエンド自身を指す。

## 実行上限を変える

次の上限は `.env` に正数で指定する。

| 設定 | 既定の上限 |
| --- | --- |
| `TASK_MAX_SECONDS` | 1800秒 |
| `TASK_REQUEST_TIMEOUT_SECONDS` | 1送信の応答待ち120秒 |
| `TASK_SUBTASK_TIMEOUT_SECONDS` | サブタスクごとに60秒 |
| `TASK_MAX_REQUESTS` | 500送信 |
| `TASK_MAX_TOKENS` | 入出力合計100000トークン |

推定費用を制限するには、`TASK_MAX_COST_USD` と、入出力それぞれ100万トークン当たりのUSD単価 `TASK_INPUT_USD_PER_MILLION`、`TASK_OUTPUT_USD_PER_MILLION` を指定する。複数のモデルを使う場合は、使用する各モデルの単価を下回らない値を指定する。使用量は応答後に加算するため、トークンと推定費用は1送信分超過する場合がある。推定費用と実際の請求額は異なる場合がある。[上限に達した場合の挙動](docs/safety.md#実行と停止)を参照する。

## 更新する

アプリ内の作業を保存してから `./scripts/up.sh` を実行する。更新中はバックエンドと選択した固定環境を停止する。固定desktopでは `/home/agent` をバックアップし、旧コンテナのホームを専用ボリュームへ移す。`docker compose up` を直接実行すると、この移行処理は走らない。[バックアップの対象と復元手順](docs/workspace-backups.md)を参照する。

## コンテナデスクトップをgVisorで動かす

[gVisorのDocker設定手順](https://gvisor.dev/docs/user_guide/quick_start/docker/)に従い、Dockerホストに `runsc` を登録する。作業を保存してから、`.env` に `DESKTOP_RUNTIME=runsc` を設定して `./scripts/up.sh` を実行する。固定環境をコンテナにする場合は `USE_KVM=false` も設定する。

設定は固定desktopの再作成時と、追加コンテナの作成時に適用する。作成済みの追加環境は作成時のランタイムで動く。省略時はDockerホストの既定を使い、標準ランタイムを明示するには `DESKTOP_RUNTIME=runc` を指定する。未登録の `runsc` を指定すると、起動や追加作成を止める。管理バックエンドは引き続きDockerソケットを使う。

## 詳細

- [安全性と利用範囲](docs/safety.md)
- [デスクトップのホームを保存・復元する](docs/workspace-backups.md)
- [MCP・Webhookでタスク結果を受け取る](docs/task-integrations.md)
- [構成・API・データの保存先](docs/architecture.md)
- [テストとCI](docs/testing.md)
- [定型作業を評価する](docs/desktop-evaluation.md)
- [本番利用に向けた確認事項](docs/production-readiness.md)
- [決定モデルの評価](docs/research/clef.md)

ライセンスはMIT。
