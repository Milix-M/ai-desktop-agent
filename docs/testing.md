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

`--durations=20`は時間のかかったテストを表示します。

## カバレッジと結果を確認する

行と分岐のカバレッジを測定します。どちらも100%を目標とし、未実行の失敗・中断・再送の経路を確認します。

未実行の箇所は到達可能性を確認し、検証できる経路のテストを追加します。構造上到達しない箇所は対象と理由を次の表に記録します。

| 対象 | 到達しない理由 |
| --- | --- |
| [VNCClient.type_text](../src/ai_desktop_agent/vm/vnc_client.py)の末尾の`else` | 組み込み`str`型では、非ASCIIは貼り付けへ分岐し、ASCII制御文字は先に拒否します。残る文字は改行・タブ・記号・印字可能文字として処理するため、この分岐には到達しません。ASCII全128文字を照合しました |
| [VmPool._creation](../src/ai_desktop_agent/server/vm_pool.py)の再取得後の所有ラベル不一致 | 所有ラベルを確認した対象を同じIDで再取得します。[コンテナのラベルは存続中に変更できない](https://docs.docker.com/engine/manage-resources/labels/)ため、正常なDocker APIでは所有ラベルの不一致は起きません。異常応答を拒否する確認処理として残します |
| [VmCreationService._close](../src/ai_desktop_agent/server/vm_creations.py)の終了時に停止用の`None`を取り出す分岐 | 停止用の値は、残る要求を取り出すループの終了後に1回だけ追加します。`close()`を再度呼んでも同じ終了タスクを待つため、ループ内では取り出しません |
| [ActionExecutor._dispatch](../src/ai_desktop_agent/actions/executor.py)のどの操作種別にも一致しない経路 | `Action`は構築時に`ActionType`の値だけを受け付け、実行処理は全操作種別を扱います。正常に構築した`Action`では、どの分岐にも一致せずに終了する経路に到達しません。列挙値と分岐の一致をASTで照合しました |
| [Actionのクリック説明](../src/ai_desktop_agent/actions/primitives.py)で座標を不明とする分岐 | 標準プロバイダはローカルのJSON辞書を使い、必須の座標を検査してから説明を生成します。その間に非同期の待機や外部コールバックはないため、座標キーは失われません。共有辞書の並行変更や独自の辞書・文字列型はこの前提に含みません |
| [Screenshot.crop_with_meta](../src/ai_desktop_agent/vm/screenshot.py)の画像再読込時の例外捕捉 | 標準の切り出し処理は同じ不変のPNGバイト列を先に復号しています。不正な画像はそこで失敗するため、標準のPillowと正常な実行資源では直後のヘッダー読込だけが失敗する経路はありません。復号処理の差し替えやメモリ不足などへの防御として残します |
| [VmPool._lifecycle_targets](../src/ai_desktop_agent/server/vm_pool.py)で固定環境の中継名がない分岐 | 対象となる`vm`・`desktop`・`android`には、定数の対応表ですべて空でない中継名を定義しています。対応表を外部から変更しない通常の設定では、名前がない分岐には進みません |
| [通知接続の切断例外と非同期コールバック](../src/ai_desktop_agent/server/app.py)の分岐 | 登録する通知コールバックはすべて同期処理で`None`を返します。標準のUvicorn・Starletteでは切断をメッセージとして受け取り、その時点で受信ループを終えます。送信時の切断例外は別の処理で捕捉するため、外側の切断例外捕捉とコールバック結果の非同期待機には進みません。独自実装への備えとして残します |
| [lifespan](../src/ai_desktop_agent/server/app.py)で終了対象と共有サービスが異なる分岐 | 標準の単一起動では、作成サービスは共有プールを受け取り、同じサービスを終了まで保持します。設定から途中で差し替える経路はないため、終了時の同一性確認は一致します。独自の起動処理やサービスの差し替えに備えて残します |
| [Androidのタスク使用確認](../src/ai_desktop_agent/server/app.py)で登録済みセッションの属性取得が失敗する分岐と旧形式の稼働判定 | 標準のセッションの属性は文字列の環境IDと通常のタスク状態です。初期設定の確認中は新規投入が排他処理を待ち、稼働開始時には一覧へ登録するため、旧形式の参照だけで稼働を検出しません。ワーカーから完了済みタスクの参照を読む間に削除される経路は到達可能なので、別に検査します。独自のセッションや登録一覧の外部変更はこの前提に含みません |
| [タスク投入](../src/ai_desktop_agent/server/app.py)と[評価の後始末](../src/ai_desktop_agent/evaluation/desktop.py)でセッションに実行タスクがない分岐 | 標準の新規セッションは、検査済みのHTTP指示または組み込みの評価課題で開始します。開始処理は最初の非同期待機より前に実行タスクを割り当てるため、通常の失敗・取消後もタスクへの参照は残ります。独自の生成処理やタスク割当時の実行資源の故障に備えて残します |
| [TaskSession._ensure_closed](../src/ai_desktop_agent/server/session.py)の予備の解放処理 | 標準の開始経路は終了処理を待ち、接続の解放処理は待機前に解放済みの印を付けます。開始前の取消には専用の処理があるため、解放済みでない終了タスクは残りません。独自の状態変更や実行資源の故障に備える処理は残します |
| [QmpClient._read_message](../src/ai_desktop_agent/vm/qmp.py)と[QgaClient._read_message](../src/ai_desktop_agent/vm/qga.py)の改行位置が受信上限を超える分岐 | [socket.recv(n)の受信量はnバイト以下](https://docs.python.org/3/library/socket.html#socket.socket.recv)です。バッファ全体を上限+1バイトまでしか受信しないため、改行の位置は最大でも上限と同じです。受信上限を処理中に変えない通常のソケットでは到達しません |
| [OpenAICompatProvider._call](../src/ai_desktop_agent/agent/llm/openai_compat_provider.py)の再試行ループを使い切って暗黙に終了する経路 | 最大3回の各試行は成功時に結果を返し、最終試行の失敗は応答解析・スキーマ非対応・通信エラーの各処理で例外になります。最終試行から次へ進む経路がないため、ループ末尾には到達しません |
| [OpenAICompatProvider._record_usage](../src/ai_desktop_agent/agent/llm/openai_compat_provider.py)の例外捕捉 | 標準SDKのJSON応答を属性参照し、検査済みの整数を整数カウンターへ加算するため、通常の入力から例外は発生しません。SDKの差し替え・カウンターの外部改変・メモリ不足などへの防御として残します |
| [AndroidProvisioner.stop・_run](../src/ai_desktop_agent/server/android_provision.py)の通常終了と終了時の例外捕捉 | 同じイベントループ上では、巡回を止める処理はタスクを先に取消します。巡回中の通常の例外は内部で捕捉し、待機処理は取消を再送出するため、取消以外の終了やループ条件による終了には進みません。タスク・待機処理・ログの差し替えや実行環境の故障に備える処理は残します |
| [AndroidProvisioner._target](../src/ai_desktop_agent/server/android_provision.py)で保存領域の識別子が`None`になる分岐 | 標準の`VmPool`は空でないコンテナIDを検査して返し、初期設定もそのIDで保存領域を識別します。固定Androidのイメージがなければ同じIDを使い、イメージが壊れていれば例外になるため、この分岐には進みません。不正なIDを返す独自のプールはこの前提に含みません |
| [_is_black_screen](../src/ai_desktop_agent/server/session.py)で復号後の画素列が空になる分岐 | 標準のPNGキャプチャでは[幅・高さが1以上](https://www.w3.org/TR/png-3/#11IHDR)です。`L`形式へ変換した画素列は幅×高さバイトあるため空になりません。サイズ0のPNGはPillowが拒否し、例外処理へ進みます |
| [DesktopWorkspace](../frontend/src/app/page.tsx)の`subtasks`・`current_subtask_index`の既定値 | `createTask`と`getCurrentTask`は応答の`subtasks`を配列、`current_subtask_index`を非負の安全な整数として検査し、欠落・`null`を先に拒否します。検査後から表示用に読むまで応答を変更しないため、通常のAPI経路では`[]`や`0`への代替分岐に進みません |
| [useWebSocket](../frontend/src/hooks/useWebSocket.ts)の終了後に`connect`を中断する分岐 | 初回接続は同期実行し、以後は再接続タイマーだけが呼び出します。タイマーは切断通知の前に登録し、通知先が画面を閉じても終了処理で解除します。[解除済みタイマーはコールバックを実行しない](https://html.spec.whatwg.org/multipage/timers-and-user-prompts.html#timers)ため、通常のブラウザでは終了後に呼び出されません |
| [createVm](../frontend/src/lib/api.ts)の中断リスナー登録直後の再確認 | 処理開始時の中断確認から、この再確認まで非同期の待機や外部コールバックはありません。組み込みの文字列・ストレージ・乱数生成と[標準のAbortSignal](https://dom.spec.whatwg.org/#interface-abortsignal)を使う場合、この間に中断状態は変わりません。中断済みの要求の拒否と作成処理中の中断は別に検査します |
| [TaskSession._decide_action](../src/ai_desktop_agent/server/session.py)の再試行ループ後の例外 | 試行回数は固定値から2回または6回になります。成功時は結果を返し、再試行できない失敗や最終試行の失敗は例外になります。ループが正常に終了する経路がないため、末尾の予備の例外には到達しません |
| [DesktopWorkspace.handleSubmit](../frontend/src/app/page.tsx)で`Error`以外の失敗を文字列にする分岐 | 画面からの投入は空でない投入キーを渡します。`createTask`はキーがある場合の通信・応答解析の失敗を`Error`で返し、`ApiError`も`Error`を継承します。接続先変更の拒否も`Error`なので、通常の画面とHTTP経路ではこの分岐に進みません |
| [DesktopWorkspace.handleControl](../frontend/src/app/page.tsx)のVM・セッション未指定で終了する分岐 | 実際の`ControlPanel`はどちらかのIDがなければすべての操作ボタンを無効化します。[Reactは無効なボタンのクリック処理を呼ばず](https://github.com/facebook/react/blob/v19.3.1/packages/react-dom-bindings/src/events/getListener.js)、先に登録された処理も登録時のIDを保持するため、画面からIDなしで呼び出されません |
| [DesktopWorkspace.handleSubmit](../frontend/src/app/page.tsx)の`vm_id`の既定値 | 指示を投入できる画面では、選択中のVM IDが空になりません。環境一覧と選択先は[同じ更新として反映](https://react.dev/blog/2022/03/29/react-v18#new-feature-automatic-batching)し、ほかの選択処理も検査済みのIDを使うか元の選択を保ちます。`createTask`は指定したVMと応答の`vm_id`が一致しなければ拒否するため、通常の画面からの投入で元のVM IDへの代替分岐には進みません |
| [_task_snapshot](../src/ai_desktop_agent/server/app.py)の削除済み履歴を読み飛ばす分岐 | 削除と重なった履歴がなくなるまで先に読み直します。その後の一覧の組み立てには非同期の待機がなく、同じイベントループから削除記録が増えることもないため、後段の読み飛ばし処理には到達しません |
| [_resolve_vm](../src/ai_desktop_agent/server/app.py)の空のVM IDを既定値にする分岐 | 本番の呼び出し元はタスク投入と画面配信だけです。投入時は空のIDを先に既定VMへ解決し、配信先のURLには空でないIDが必要なため、HTTP・WebSocket経路ではこの分岐に進みません |

保存データのJSONは拡張項目も検査し、有限でない数値・不正なUnicode・重複キーを環境操作や復元の前に拒否します。Androidの完了記録では正常な拡張項目を更新後も保持します。

終了時の検査では別プロセスで作成記録の保存前・保存直後にイベントループを終了させ、所有権の保持、作成枠の解放、次の起動での1回だけの作成を確認します。不正な環境IDはタスクの停止やDocker操作の前に拒否することを検査します。

セッションの終了ではタスクの停止・履歴の保存・接続の解放を確認し、開始直前の停止と終了処理の取消も検査します。指示の型や文字コードが不正なら履歴に保存する前に拒否し、修正後に同じセッションで再実行できることを確認します。状態遷移の拒否後もゴール・計画・進捗が変わらないことを検査します。開始処理中の削除は409と`Retry-After`で拒否し、開始後の一時停止・削除と、履歴削除後に同じ投入キーで再実行しないことを確認します。

投入キーの割当後やタスク開始後は接続先の変更を拒否し、切替中は開始・投入キーの割当・別の切替を拒否します。切断に失敗した場合は元の接続を保持し、終了処理では進行中の切替を待って解放します。

画面を閉じた後の応答では追加要求を送らず、環境作成の取消では同じ受付記録で再試行できることを検査します。履歴の削除後は遅延通知や受付応答からタスクを復元せず、接続先の更新後は旧サーバーの削除結果を反映しません。`useWebSocket`の接続参照が変更されても、終了時はフックが開いた接続だけを閉じます。

テンプレート照合では比較する領域の片方だけが一様なら一致と判定しません。しきい値は0〜1の有限数、走査の間引き幅は正の整数に限ります。

ログはUvicornのアクセスログと後から追加した出力先で検査します。HTTPステータスの数値型を保ち、追加フィールド・例外・整形後の文字列に含まれる認証情報を伏せ字にすることを確認します。

回復操作が失敗した場合は同じ回復案の後続入力を止めます。操作上限・空の再計画・画面切断による終了も検査し、手動入力から復帰した成功記録には直前の操作の失敗理由を残しません。

画像付きのモデル要求ではJSONモードへの切替後も画像・解像度・セッションIDを保持します。画像変換の待ち中に別の要求を処理できることと、変換中の取消後に画像を送信しないことを検査します。画像が空・不正な場合やJPEG変換に失敗した場合は要求を送らず、要求回数にも加算しません。領域選択の幅・高さは小さな画像でも画像サイズを超えないことを検査します。

領域の切り出しでは1ピクセル幅の画像を保ち、縦横の実倍率で再選択・移動・ドラッグの座標を元画像へ戻すことを検査します。両軸の座標変換には`Screenshot.crop_with_geometry`を使います。従来の`crop_with_meta`が返す倍率は横方向だけです。

固定Androidは保存領域を読めない場合やext4ヘッダー・UUIDが不正な場合に初期設定を止め、完了記録を保持します。保存領域がない従来構成ではコンテナIDで識別します。初期設定の記録も検証し、環境ID・保存領域ID・日時が不正な場合は設定を進めず、記録を保持します。

KVM・実Androidなどの環境が必要な検査は、不足する環境と未検証の範囲を記載します。

```bash
uv run --frozen pytest -m 'not desktop_integration' --cov=ai_desktop_agent --cov-branch --cov-report=xml --cov-report=term-missing
npm --prefix frontend run test:coverage
```

Pythonの結果は`coverage.xml`、画面の結果は`frontend/coverage/`に保存します。PythonではCLIなどの子プロセス、画面ではテストから読み込まれない実装も測定対象です。CIでも両方を測定し、`coverage-report`と`frontend-coverage`に保存します。

操作は画面や保存成果物まで確認します。成果物による検査は[OSWorld](https://github.com/xlang-ai/OSWorld/blob/main/desktop_env/evaluators/metrics/general.py)を参考にしています。スクリプトの文字列だけを調べる検査より、実行時の引数や入力結果を確認し、失敗時にキーや接続を解放できるか検査してください。

再試行の単体テストでは待機を記録するモックを使って待機秒数と回数を検査します。並行処理やタイムアウトそのものを検査するテストには、同じ省略を適用しないでください。

## 実デスクトップを確認する

ローカルのLinux Docker Engine（API 1.48以降）とDocker Compose 2.33.1以降が必要です。ブラウザからの手動入力の検査には、ホストのChromiumまたはGoogle Chromeも使います。ホストの一時ディレクトリや`/var/run/docker.sock`をマウントするため、リモートのDockerデーモンは対象外です。

テストでは一時コンテナ、ネットワーク、ボリュームを作成します。マウントの検査には`SYS_ADMIN`権限とAppArmor制限の解除も使うため、開発用のDocker環境で実行してください。LLMのAPIキーやホストのKVMは使いません。

```bash
docker build -t ai-desktop-agent-desktop-smoke desktop
docker build -t ai-desktop-agent-backend-smoke .
docker build -t ai-desktop-agent-qmp-smoke vm
docker build -t ai-desktop-agent-qga-smoke -f tests/integration/qga.Dockerfile .
docker build -t ai-desktop-agent-frontend-smoke frontend
RUN_DESKTOP_SMOKE=1 uv run --frozen pytest -m desktop_integration tests/integration/
```

5種類すべてのイメージを作成してください。`RUN_DESKTOP_SMOKE=1`がない場合、この検査はスキップされます。ブラウザがなければ手動入力のブラウザ検査もスキップされます。CIではブラウザの存在を先に確認します。通常の単体テストの成功だけでは実デスクトップを検証したことにはなりません。

Firefoxの描画、VNC入力、環境間の通信遮断、復旧、保存ファイルの保持などを確認します。手動入力は実ブラウザからnoVNCを通して入力し、待機中だけ文字が届くことと、AIの呼び出しが止まっていることを検査します。実LLMの判断精度、KVM上のゲスト起動、実Androidの動作は対象外です。モデルの比較は[定型作業を評価する](desktop-evaluation.md)を参照してください。

コンテナ内のPythonも測定する場合は、ホストとバックエンドイメージのCPUアーキテクチャ・Python ABIを揃え、未使用の保存先を指定します。

```bash
coverage_root="$(mktemp -d)"
RUN_DESKTOP_SMOKE=1 DESKTOP_CONTAINER_COVERAGE_DIR="$coverage_root/container" \
  uv run --frozen pytest -m desktop_integration tests/integration/
```

イメージ内と手元のPythonソースの一致を確認してから測定します。結果は指定先の`container.coverage`と`container-coverage.json`に保存し、元の測定データも`raw/`に残します。ホストの測定結果には自動合算しません。強制終了の検査では終了方法を変えず、保存できなかった実行は測定結果に含めません。CIではホストとコンテナの結果を合算し、`desktop-coverage`に保存します。

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

`develop`へのpush、`develop`宛てのPR、毎週火曜06:17（JST）の定期実行で[CI](../.github/workflows/ci.yml)が実行されます。定期実行は全検査が対象です。

| 変更内容 | 実行する検査 |
| --- | --- |
| `README.md`、`android/README.md`、`docs/`内のMarkdownだけ | Pythonのlint・整形、Pythonとnpmの実行時依存の脆弱性監査 |
| それ以外を含む変更、または変更ファイルが空 | 上記に加え、バックエンドのテストとカバレッジ、画面のlint・型・テスト・ビルド、実デスクトップ検査、本番5イメージのOS脆弱性監査 |

CI設定や未知のファイルも全検査の対象です。変更範囲の取得に失敗した場合は、最終チェックも失敗します。`All checks passed`は、文書だけの変更で予定どおり省略した検査以外のスキップ・失敗・キャンセルを成功扱いにしません。

uvとnpmのダウンロードキャッシュ、本番5種類と検査用ゲストエージェントのDockerビルドキャッシュを再利用します。本番イメージはUTCの日付が変わるとOSパッケージの更新から再ビルドし、同じ日付では更新済みの層を再利用します。キャッシュがなければ通常どおり取得・ビルドします。実デスクトップ検査はCompose 2.33.1で実行します。

全検査の実行結果から、`os-audit`のartifactを30日間、その他を7日間ダウンロードできます。`os-audit`には本番5イメージのOS脆弱性監査結果を保存します。`coverage-report`はバックエンドテストの成功時に保存します。`desktop-evaluation`には評価の画面や保存成果物が含まれ、途中で失敗した場合も生成できた範囲のアップロードを試みます。

## 複数人・複数エージェントで作業する

作業ごとにcloneまたはworktreeを分け、`.venv`と`node_modules`も各作業ディレクトリで準備します。依存の更新、生成ファイル、整形処理が他の作業に混ざるのを防げます。

pytestを並行して実行する場合は、`--basetemp`に作業固有の一時ディレクトリを指定してください。指定先はpytestが削除・再作成するため、保存したいファイルのある場所や他の実行と同じ場所は指定しないでください。

実デスクトップのテストではDockerを共有します。同時実行する場合は、`DESKTOP_SMOKE_IMAGE`・`BACKEND_SMOKE_IMAGE`・`QMP_SMOKE_IMAGE`・`QGA_SMOKE_IMAGE`・`FRONTEND_SMOKE_IMAGE`に作業固有のタグを指定し、そのタグでビルドしてください。他の作業のコンテナやキャッシュを削除する`docker system prune`などは使わないでください。
