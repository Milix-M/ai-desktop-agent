# テストとCI

バックエンドと画面のテストは、DockerやLLMのAPIキーなしで実行できます。実デスクトップの検査にはDockerを使います。コマンドはリポジトリのルートで実行してください。

## 開発環境を準備する

Python 3.12以降、uv、Node.js 24が必要です。CIと同じPython 3.12を使い、ロックファイルのバージョンで依存をインストールします。

```bash
uv sync --frozen --python 3.12
npm --prefix frontend ci
```

## 変更した箇所を確認する

バックエンドはファイルとテスト名、画面はファイルで対象を絞れます。

```bash
uv run --frozen pytest tests/test_model_response_validation.py -k confidence
npm --prefix frontend test -- src/components/InstructionInput.test.tsx
```

画面を継続して編集する場合は、`npm --prefix frontend run test:watch -- src/components/InstructionInput.test.tsx` で変更のたびに再実行できます。全体を確認するには次を実行します。

```bash
uv run --frozen ruff check src/ tests/ scripts/ci_scope.py
uv run --frozen ruff format --check src/ tests/ scripts/ci_scope.py
uv run --frozen pytest -m 'not desktop_integration' --durations=20
npm --prefix frontend run lint
npm --prefix frontend run typecheck
npm --prefix frontend test
npm --prefix frontend run build
```

## カバレッジと結果を確認する

行と分岐のカバレッジはどちらも100%を目標とします。未実行の箇所は到達可能性を確認し、失敗・中断・再送も検査します。

```bash
uv run --frozen pytest -m 'not desktop_integration' --cov=ai_desktop_agent --cov-branch --cov-report=xml --cov-report=term-missing
npm --prefix frontend run test:coverage
```

Pythonの結果は `coverage.xml`、画面の結果は `frontend/coverage/` に保存します。Pythonの子プロセスと、画面のテストが読み込まない実装も測定対象です。CIでは `coverage-report` と `frontend-coverage` に保存します。

標準の実行経路で通らない分岐は、入力検証、処理順、ライブラリの仕様から理由を確認してください。

| 処理 | 標準の経路で分岐に入らない理由 |
| --- | --- |
| VNCの文字入力、操作の振り分け、クリック座標 | ASCII全128文字は入力検証後に各処理へ分岐します。`ActionType` の全値に実行分岐があり、必須座標も説明の生成前に確認します |
| 画像の読み出しと黒画面の判定 | 切り出し前に同じPNGを復号します。[PNGの幅・高さは1以上](https://www.w3.org/TR/png-3/#11IHDR)で、正常な画像から画素が空になることはありません |
| QMP・QGAの改行位置 | [socket.recv(n)](https://docs.python.org/3/library/socket.html#socket.socket.recv) が返すバイト数とバッファ容量から位置の上限が決まります |
| Docker環境の識別 | [所有ラベルはコンテナの存続中に変更できません](https://docs.docker.com/engine/manage-resources/labels/)が、矛盾した応答は拒否します。固定環境の中継名はすべて定数で定義します |
| タスクと作成サービスの終了 | 新しいタスクには開始時に実行タスクを割り当て、終了時は接続の解放を待ちます。作成ワーカーの停止用の値は未処理要求を取り出した後に追加します |
| Android初期設定の対象識別 | 標準のプールは空でないコンテナIDを返します。固定ディスクがない場合もそのIDを使い、ディスクが破損している場合は初期設定を止めます |
| モデル呼び出しの試行ループ | 成功時は結果を返し、試行不能な失敗と最終試行の失敗は例外になります |
| 画面と状態通知の分岐 | IDや状態はHTTP応答の検証前後で区別します。操作ボタンは対象IDが揃うまで無効となり、接続の終了時には再接続タイマーを解除します |

操作は画面や保存成果物まで確認します。再試行の回数や待機秒数はモックで検査でき、並行処理やタイムアウト自体を確認するときは実際の待機を使います。KVMや実Androidが必要な場合は、用意できなかった環境と未検証の範囲を記録してください。[実装済みの検査と残る確認](production-readiness.md)を参照できます。

## 実デスクトップを確認する

ローカルのLinux Docker Engine（API 1.48以降）とDocker Compose 2.34.0以降が必要です。ブラウザの手動入力を検査するには、ホストにChromiumまたはGoogle Chromeも用意します。

テストはローカルのDockerソケットと一時ディレクトリをマウントし、一時コンテナ・ネットワーク・ボリュームを作成します。マウントの検査では `SYS_ADMIN` とAppArmor制限の解除を使うため、開発用Docker環境で実行してください。リモートDockerは対象外です。

```bash
docker build -t ai-desktop-agent-desktop-smoke desktop
docker build -t ai-desktop-agent-backend-smoke .
docker build -t ai-desktop-agent-qmp-smoke vm
docker build -t ai-desktop-agent-qga-smoke -f tests/integration/qga.Dockerfile .
docker build -t ai-desktop-agent-frontend-smoke frontend
RUN_DESKTOP_SMOKE=1 uv run --frozen pytest -m desktop_integration tests/integration/
```

実行には5種類すべてのイメージが必要です。`RUN_DESKTOP_SMOKE=1` がなければ実デスクトップ検査を、ブラウザがなければ手動入力のブラウザ検査をスキップします。CIはブラウザを確認し、対応下限のCompose 2.34.0を使います。

Firefoxの描画、VNC入力後のファイル内容（日本語・絵文字・長文）、手動入力からの再開、環境間の通信遮断、バックエンド再作成後の復旧、更新後のファイル保持とバックアップの復元を確認します。実LLMの判断精度、KVM上のゲスト起動、実Androidの動作は対象外です。[モデルを比較する手順](desktop-evaluation.md)を参照してください。

コンテナ内のPythonも測る場合は、ホストとバックエンドイメージのCPUアーキテクチャ・Python ABI・ソースを揃え、未使用の保存先を指定します。

```bash
coverage_root="$(mktemp -d)"
RUN_DESKTOP_SMOKE=1 DESKTOP_CONTAINER_COVERAGE_DIR="$coverage_root/container" \
  uv run --frozen pytest -m desktop_integration tests/integration/
```

指定先に `container.coverage`、`container-coverage.json`、測定データの `raw/` が残ります。強制終了で保存できなかった実行は含みません。ローカルではホストの結果と自動合算せず、CIでは合算した結果を `desktop-coverage` に保存します。

## KVMでゲストの起動とデータの保持を確認する

`/dev/kvm` を使える x86_64 の Linux テスト機に、Docker Compose、uv、sudo を用意します。Ubuntu の検査はホストにイメージ構築用のパッケージをインストールします。Android の検査には `adb` と `mkfs.ext4` も必要です。リポジトリのルートから、確認するゲストのコマンドを実行してください。

```bash
./scripts/check-kvm-guest.sh linux
./scripts/check-kvm-guest.sh android
```

Ubuntu はテンプレートを構築し、KDE の起動と Firefox のバージョンを確認します。Android は Android-x86 9.0-r2 の ISO を取得して SHA-256 を照合し、ホーム画面と初期設定を調べます。どちらも VNC で画面を取得し、ゲストにファイルを書き込みます。同じディスクでコンテナを再作成し、ファイルを読み戻せると成功です。失敗時はログに表示された作業ディレクトリでディスクと起動ファイルを確認できます。

ゲストや起動スクリプトに関わる変更を含む PR では、[Guest boot](../.github/workflows/guest-boot.yml) が両方の検査を実行します。

## 依存の脆弱性を確認する

次のBashコマンドで、ロックファイルにある本番用の依存を監査できます。

```bash
audit_requirements="$(mktemp)"
uv export --frozen --no-dev --no-emit-project --output-file "$audit_requirements"
uv run --frozen pip-audit --strict --require-hashes --disable-pip --requirement "$audit_requirements"
rm -f "$audit_requirements"
npm --prefix frontend audit --omit=dev
```

## CIの実行範囲を確認する

[CI](../.github/workflows/ci.yml) は `develop` へのpush、`develop` 宛てのPR、毎週火曜06:17（JST）に実行します。

| 対象 | 検査 |
| --- | --- |
| `README.md`、`android/README.md`、`docs/` 内のMarkdownだけ | Pythonのlint・整形と、Python・npmの本番依存監査 |
| それ以外の変更を含む場合、変更ファイルが空、定期実行 | 上記に加え、バックエンドのテスト・カバレッジ、画面のlint・型・テスト・ビルド、実デスクトップ検査、本番5イメージのOS監査 |

`All checks passed` は文書だけの変更で予定どおり省略した検査を許容し、変更範囲の取得失敗や必要な検査の失敗・キャンセル・スキップを失敗として扱います。成果物の保存期間は `os-audit` が30日、その他が7日です。`desktop-evaluation` には評価画面や保存成果物が含まれます。

## 複数人・複数エージェントで作業する

作業ごとにcloneまたはworktreeを分け、`.venv` と `node_modules` を各ディレクトリに用意します。pytestを同時に実行する場合は、`--basetemp` に実行ごとに異なる一時ディレクトリを指定してください。pytestは指定先を削除・再作成します。

Dockerを共有して実デスクトップ検査を同時に実行する場合は、`DESKTOP_SMOKE_IMAGE`、`BACKEND_SMOKE_IMAGE`、`QMP_SMOKE_IMAGE`、`QGA_SMOKE_IMAGE`、`FRONTEND_SMOKE_IMAGE` に作業固有のタグを指定し、同じタグでイメージをビルドします。
