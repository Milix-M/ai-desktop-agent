# テストとCI

バックエンドと画面のテストは、DockerやLLMのAPIキーなしで実行できます。実デスクトップの検査は別に実行します。コマンドは、指定がなければリポジトリのルートで実行してください。

## 開発環境を準備する

Python 3.12以降・uv・Node.js 24が必要です。CIと揃える場合はPython 3.12を使います。

```bash
uv sync --frozen --python 3.12
npm --prefix frontend ci
```

`--frozen`と`npm ci`はロックファイルの依存バージョンを使います。依存更新が目的でなければ、`uv.lock`と`frontend/package-lock.json`を更新する必要はありません。

## 変更した箇所を確認する

バックエンドはファイル単位、またはテスト名で絞れます。

```bash
uv run --frozen pytest tests/test_model_response_validation.py
uv run --frozen pytest tests/test_model_response_validation.py -k confidence
```

画面のテストはファイルを指定します。継続して編集する場合は監視モードを使えます。

```bash
npm --prefix frontend test -- src/components/InstructionInput.test.tsx
npm --prefix frontend run test:watch -- src/components/InstructionInput.test.tsx
```

全体を確認するときは次のコマンドを使います。

```bash
uv run --frozen ruff check src/ tests/ scripts/ci_scope.py
uv run --frozen ruff format --check src/ tests/ scripts/ci_scope.py
uv run --frozen pytest -m 'not desktop_integration' --durations=20
npm --prefix frontend run lint
npm --prefix frontend run typecheck
npm --prefix frontend test
npm --prefix frontend run build
```

`--durations=20`は時間のかかったテストを表示します。カバレッジも確認する場合は、pytestに`--cov=ai_desktop_agent --cov-report=xml --cov-report=term-missing`を追加します。`coverage.xml`が生成されます。

再試行の単体テストでは、待機を記録するモックを使って待機秒数と回数を検査します。並行処理やタイムアウトそのものを検査するテストには、同じ省略を適用しないでください。

## 実デスクトップを確認する

ローカルのLinux Docker Engine（API 1.48以降）とDocker Compose 2.33.1以降が必要です。ホストの一時ディレクトリや`/var/run/docker.sock`をマウントするため、リモートのDockerデーモンは対象外です。

テストでは一時コンテナ、ネットワーク、ボリュームを作成します。マウントの検査には`SYS_ADMIN`権限とAppArmor制限の解除も使うため、開発用のDocker環境で実行してください。LLMのAPIキーやホストのKVMは使いません。

```bash
docker build -t ai-desktop-agent-desktop-smoke desktop
docker build -t ai-desktop-agent-backend-smoke .
docker build -t ai-desktop-agent-qmp-smoke vm
docker build -t ai-desktop-agent-qga-smoke -f tests/integration/qga.Dockerfile .
RUN_DESKTOP_SMOKE=1 uv run --frozen pytest -m desktop_integration tests/integration/
```

4種類すべてのイメージを作成してください。`RUN_DESKTOP_SMOKE=1`がない場合、この検査はスキップされます。通常の単体テストの成功だけでは、実デスクトップを検証したことにはなりません。

Firefoxの描画、VNC入力、環境間の通信遮断、復旧、保存ファイルの保持などを確認します。実LLMの判断精度、KVM上のゲスト起動、実Androidの動作は対象外です。モデルの比較は[定型作業を評価する](desktop-evaluation.md)を参照してください。

## 依存の脆弱性を確認する

Pythonは実行時の依存をロックファイルから書き出して監査します。以下はBashで実行します。

```bash
audit_requirements="$(mktemp)"
uv export --frozen --no-dev --no-emit-project --output-file "$audit_requirements"
uv run --frozen pip-audit --strict --require-hashes --disable-pip --requirement "$audit_requirements"
rm -f "$audit_requirements"
npm --prefix frontend audit --omit=dev
```

## CIの実行範囲を確認する

`develop`へのpushと、`develop`宛てのPRで[CI](../.github/workflows/ci.yml)が実行されます。

| 変更内容 | 実行する検査 |
| --- | --- |
| `README.md`、`android/README.md`、`docs/`内のMarkdownだけ | Pythonのlint・整形、Pythonとnpmの実行時依存の脆弱性監査 |
| それ以外を含む変更、または変更ファイルが空 | 上記に加え、バックエンドのテストとカバレッジ、画面のlint・型・テスト・ビルド、実デスクトップ検査 |

CI設定や未知のファイルも全検査の対象です。変更範囲の取得に失敗した場合は、最終チェックも失敗します。`All checks passed`は、文書だけの変更で予定どおり省略した検査以外のスキップ・失敗・キャンセルを成功扱いにしません。

uvとnpmのダウンロードキャッシュ、4種類のDockerビルドキャッシュを再利用します。キャッシュがなければ通常どおり取得・ビルドします。実デスクトップ検査はCompose 2.33.1で実行します。

全検査の実行結果から、artifactを7日間ダウンロードできます。`coverage-report`はバックエンドテストの成功時に保存します。`desktop-evaluation`には評価の画面や保存成果物が含まれ、途中で失敗した場合も生成できた範囲のアップロードを試みます。

## 複数人・複数エージェントで作業する

作業ごとにcloneまたはworktreeを分け、`.venv`と`node_modules`も各作業ディレクトリで準備します。依存の更新、生成ファイル、整形処理が他の作業に混ざるのを防げます。

pytestを並行して実行する場合は、`--basetemp`に作業固有の一時ディレクトリを指定してください。指定先はpytestが削除・再作成するため、保存したいファイルのある場所や他の実行と同じ場所は指定しないでください。

実デスクトップのテストではDockerを共有します。同時実行する場合は、`DESKTOP_SMOKE_IMAGE`・`BACKEND_SMOKE_IMAGE`・`QMP_SMOKE_IMAGE`・`QGA_SMOKE_IMAGE`に作業固有のタグを指定し、そのタグでビルドしてください。他の作業のコンテナやキャッシュを削除する`docker system prune`などは使わないでください。
