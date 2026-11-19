# アーキテクチャ詳細設計

本書は実装の実態に合わせた設計書である。コードを読む際の地図として使う。主要モジュールはsrc/ai_desktop_agent/配下のserverとagentとactionsとvm、frontend/src/、vm/のゲストコンテナ定義とする。

## 目次

1. [システム概要](#システム概要)
2. [バックエンド](#バックエンド)
3. [状態機械と実行フロー](#状態機械と実行フロー)
4. [意思決定層](#意思決定層)
5. [観測](#観測)
6. [アクション実行](#アクション実行)
7. [VM操作](#vm操作)
8. [フロントエンド](#フロントエンド)
9. [安全性](#安全性)
10. [制限と今後](#制限と今後)
11. [図の管理](#図の管理)

図は本文中のMermaidブロックで管理する。別ファイルや画像生成は不要とする。GitHubとVS CodeのMarkdownプレビューでそのまま表示できる。

## システム概要

自然言語の指示で仮想マシンのGUIをAIが直接操作する。利用者はブラウザから指示を出し、AIがVMを操作する様子をnoVNCビューアで見ることができる。

```mermaid
flowchart LR
    user["ユーザー<br/>(ブラウザ)"]
    subgraph host["Docker ホスト"]
        direction LR
        fe["frontend<br/>Next.js :3000"]
        be["backend<br/>FastAPI :8081"]
        desk["desktop<br/>Xfce+TigerVNC<br/>(container限定)"]
        vm["vm<br/>QEMU/KVM<br/>KDE / 4vCPU・4GB"]
        and["android<br/>Android-x86<br/>(kvm限定・手動起動)"]
        vm2["vm-id / desk-id / and-id<br/>専用コンテナ"]
        data["data/<br/>tasks/*.json<br/>vms.json"]
        docker["Docker<br/>デーモン"]
    end
    user -->|指示・視聴<br/>HTTP :3000| fe
    user -->|認証付き REST / WS<br/>状態通知・画面配信 :8081| be
    be -->|VNC操作（KVMなし時）<br/>desktop:5900| desk
    be -->|VNC操作（KVMあり時）<br/>vm:5900| vm
    be -->|VNC操作<br/>android:5900| and
    be -->|履歴保存<br/>data/tasks/*.json| data
    be -->|VM管理<br/>Docker socket| docker
    docker -->|コンテナ払い出し<br/>qcow2フルコピー<br/>動的コンテナはKVM不要| vm2
```

| 名称 | 中身 | ホスト公開ポート | 役割 |
|---|---|---|---|
| frontend | Next.js | 3000 | チャットUIとnoVNCビューアとVM管理 |
| backend | FastAPI | 8081 | 認証、API、状態通知、VNC転送、エージェント実行、履歴保存 |
| desktop | XfceとTigerVNC | なし | containerプロファイルの作業環境 |
| vm | QEMUとKVM | なし | KDEの作業環境。kvmプロファイルに属する |
| android | Android-x86とQEMU | なし | kvmプロファイルの手動起動。VNC操作は通常VMと同じ |

動的環境は台数分の専用コンテナを使い、ホストのポートを払い出さない。QEMU系はkvmプロファイル、desktop系はcontainerプロファイルに属する。up.shはx86_64、/dev/kvm、ゲストファイルが揃う場合にvm系、それ以外はdesktop系を起動する。初期起動の作業環境は1つで、androidは含まない。USE_KVM=falseはdesktopを選び、trueで条件が不足すれば起動前に止まる。

| 送信元 | 送信先 | 経路 |
|---|---|---|
| ブラウザ | backendのAPIとWS | ホストの:8081 |
| ブラウザ | 画面配信 | :8081/vnc/{id}。認証後にバックエンドがVNCへ転送する |
| backend | desktopのVNC | desktop:5900。KVMなし時でDocker内部ネットワーク |
| backend | VMのVNC | vm:5900。KVM時で動的VMはvm-ID:5900 |
| backend | AndroidのVNC | android:5900。動的はand-ID:5900 |
| backend | AndroidのADB | Docker内部の名前:5555 |
| backend | Docker | /var/run/docker.sock。VM作成と再起動用 |

LLM_PROVIDER、LLM_MODEL、各種APIキーでLLMを設定する。USE_KVMの既定値はauto、ALLOW_TCG_VMはfalse、VM_MEMORYは4096、VM_CPUSは4、DATA_DIRは./dataとなる。up.shはComposeが解決したUSE_KVMを読み、接続先と判定結果をバックエンドにも渡す。VNC_HOSTの既定はComposeではdesktop、バックエンド単体ではlocalhostとなる。VM_MEMORYとVM_CPUSの固定値はComposeで定め、動的払い出しでは環境変数を使う。

### ネットワーク

管理用のdefaultネットワークにはフロントエンドとバックエンドを置く。各作業環境はproject_env_IDという名前の専用bridgeを使い、バックエンドだけが各bridgeにも接続する。固定環境のbridgeはCompose、動的環境はEnvironmentNetworksが作る。新しい作業環境にはNET_RAWを与えない。

コンテナの起動点はserver/__main__.pyとなる。管理用IPとサブネットをDockerから確認し、そのIPだけでAPIを待ち受ける。ManagementNetworkMiddlewareも実際の送信元IPを確認し、管理用ネットワーク以外を拒否する。転送ヘッダーは信頼しない。管理経路のgw_priorityは1、専用bridgeは0とし、追加接続で既定経路を変えない。Docker API 1.48以降とCompose 2.33.1以降を必要とする。

起動時、環境一覧の取得時、タスク投入時に所属と専用ネットワークを確認する。旧環境は新しい接続先を確保してから共有ネットワークを外す。保存済みのisolated_networkは、旧環境の発見とバックエンド再作成後の再接続にも使う。移行失敗では処理を止め、別プロジェクトや未知のコンテナを切断しない。専用bridgeは環境削除時に回収する。

## バックエンド

src/ai_desktop_agent/server/app.pyがAPIとWebSocketを提供する。

### 認証

server/auth.pyが接続キーとブラウザセッションを管理する。AGENT_ACCESS_TOKENがなければdata/auth/access-keyを0600で生成する。POST /auth/loginでキーを照合し、12時間有効のHttpOnly・SameSite=Strict Cookieを発行する。HTTPSではSecureも付ける。ブラウザの保存領域やURLにはキーを残さない。

GET /auth/statusとGET /health、POST /auth/login、POST /auth/logout以外は認証を必須とする。APIクライアントはAuthorization: Bearerを使える。POST /auth/logoutは要求に付いたCookieのセッションを取り消す。Cookieがない場合やセッションが失効済みの場合もCookieを消して200を返す。セッションの失効時には開いているWebSocketも閉じる。セッションはメモリ内で管理し、バックエンド再起動後は再ログインする。利用者ごとの権限分離は未対応となる。

フロントはログイン・ログアウトを直列に送り、401では現在のCookieを確認する。同時確認はまとめ、5秒で打ち切る。認証変更を挟んだ確認は読み取り直し、古い401や初回確認・旧接続の結果でログイン状態を上書きしない。元の操作の再送は利用者が行う。

ログインとHTTP・WebSocketのBearer認証はキー照合の失敗数を共有する。直近60秒に10回失敗すると、照合前にHTTP 429とRetry-Afterを返し、WebSocketも接続前に拒否する。待ち時間は失敗時刻から計算し、拒否された試行では延長しない。Cookieで認証済みの操作とログアウトは継続できる。失敗時刻だけを最大10件メモリに保持し、キー照合の成功またはバックエンド再起動で消す。複数バックエンド間の共有は未対応となる。

認証変更を識別する乱数をlocalStorageで共有し、別タブの変更も照合する。保存領域が使えない場合は同じタブ内の順序で照合する。

画面配信は/vnc/{id}から所属プロジェクトの環境へ接続する。接続先はVM情報から決め、任意のホストやポートを受け付けない。バイナリ転送にはサイズと送信待ち時間の上限を設け、切断時にTCP接続も閉じる。

### エンドポイント

| 方法 | パス | 用途 |
|---|---|---|
| GET | /health | 生存確認 |
| POST | /tasks | タスク投入。instructionとvm_idとallow_vm_restartを指定できる。replace_running=falseで実行中の環境への投入を拒否する |
| GET | /tasks | 履歴一覧。新しい順に返す |
| GET | /tasks/{id} | タスク詳細。操作履歴と推論、利用者向けの結果（result）を含む |
| DELETE | /tasks/{id} | 履歴削除。実行中なら止めてから消す |
| GET | /tasks/current | 最新タスク状態。vm_idで環境を選べる |
| POST | /tasks/current/{pause,resume,stop} | タスク制御 |
| GET | /vms | VM一覧 |
| POST | /vms | VM作成。Idempotency-Keyを付けると元の受付結果を待つ。キーなしは要求ごとに新規作成する |
| POST | /vm-creations | 環境作成の受付。Idempotency-Key必須。受付IDを返す |
| GET | /vm-creations/{id} | 作成の受付状態と結果を取得する |
| DELETE | /vms/{id} | VM削除。上のタスク停止とコンテナ削除を行い、動的はディスクも消す |
| POST | /vms/{id}/stop | VMとコンテナの停止。固定と動的に使え、上のタスク停止と中継連動を含む |
| POST | /vms/{id}/start | VMとコンテナの起動。固定と動的に使え、中継連動を含む。QEMU系はKVM必須 |
| GET | /vm/status | 既定VMの状態。DockerとQMPで確認する |
| POST | /vm/restart | 既定VMの再起動。そのVMの実行中タスクを停止する |
| WS | /ws | 状態と操作ログの配信 |

HTTP本文は256 KiB以内とし、読み取り開始から5秒以内に受信する。保護対象APIは認証を先に確認する。エンドポイントを呼ぶ前に本文を受信し、Content-Lengthがなくても実測する。サイズ超過は413、受信時間切れは408、長さの不正は400を返し、HTTP/1では接続を閉じる。受信中の切断でもエンドポイントを呼ばない。この5秒にはエンドポイントの実行時間を含めない。WebSocketには既存の転送制限を使う。

標準起動では認証と実行状態を同じプロセスで扱うため、プロセスを1つに固定する。WEB_CONCURRENCYでは増やさない。WebSocketの受信は1メッセージ1 MiB、受信待ち行列は4件までとする。

### セッション管理

readiness.pyはrunningかつヘルスチェックがhealthyまたは未設定の環境を投入先とする。起動準備中・異常・不明の環境を既定の選択から外し、明示した環境への投入は409で拒否する。画面配信はrunningであれば接続でき、準備中も起動の様子を確認できる。

1タスクは1つのTaskSessionに対応し、_sessionsで管理する。_active_sessionは後方互換のための最新1件とする。vm_id省略時は稼働中のdesktopを優先する。WebSocketは接続時に全環境のsnapshotを送り、state・action・error・completeも配信する。イベントにはsession_idとvm_id、開始順のsession_order、状態更新にはrevisionも付ける。フロントは別タスクの古い通知を開始順で除外し、同じタスク内ではrevisionを比較する。接続ごとの送信キューを使い、遅い閲覧者からタスクの実行を切り離す。

状態配信は送受信のどちらかが終わると残る処理を取り消し、通知先の登録を外してから接続を閉じる。送信失敗・滞留は1013、ping以外の入力は1003で切断する。切断要求の待ち時間は5秒までとし、ASGIの取消中も解放処理を保護する。

画面の初期復元と履歴取得は、後から選んだ環境や履歴を上書きしない。環境一覧の定期更新でも、削除済み環境の履歴の閲覧を保つ。環境IDのない旧履歴は、最初の環境一覧が後から届いても表示を保つ。

snapshotは履歴を読んでいる間のセッション開始と履歴削除を追跡する。終了して実行一覧から外れたセッションも結果に含める。同じ環境の候補は開始順で選び、読み取り中に削除した履歴は結果から除く。選んだ履歴が削除された場合は読み直し、残る最新の履歴を返す。取得が終わったら追跡情報を解放し、取消・失敗でも残さない。

フロントは再接続時にsnapshotが確認した開始順の範囲で削除済みタスクを残る履歴へ切り替える。削除前の開始順も保持し、削除済みタスクの遅い通知で表示や操作を戻さない。

バックエンドは起動ごとにserver_idを生成し、TaskStatusとsnapshot・状態・操作・エラー・完了の通知へ付ける。フロントは起動が変わると開始順と状態番号の比較をやり直し、状態を同期して環境一覧・履歴を再取得する。同じ起動への再接続では比較情報を保つ。旧起動の応答と通知は除外する。HTTP要求の待機中に起動の切り替えを確認した場合、その応答で起動識別子を切り替えない。選択中の履歴は再取得し、入力中の指示は消さない。

pause・resume・stopは任意のsession_idクエリで対象を照合する。不一致や対象の消失は409とし、操作しない。Web UIは選択した環境とライブタスクのIDを指定する。タスクIDを確認できない間は操作を無効にする。409後は最新状態を取得する。取得が遅れても、後から選んだ環境や新しいタスクの状態を上書きしない。再開時のmanual_input_idとallow_screen_shareも照合する。session_id省略時は従来どおり最新の対象を操作する。

VNC接続・セッション生成中の中断では、生成処理の終了を待って取得済みの接続を解放する。セッション生成後はモデル接続も閉じ、解放が終わるまで次の投入を待たせる。

request_user_inputはTaskSessionで処理し、awaiting_userの間は画面取得とモデル呼び出しを止める。状態通知とsnapshotにmanual_inputのid・reasonを付ける。POST /tasks/current/resumeはmanual_input_idとallow_screen_share=trueが一致する場合だけ再開する。入力値は受け取らない。同じ環境へのタスク投入も待機中は拒否する。

### タスク投入の再送

POST /tasksは任意のIdempotency-Keyを受け付ける。キーはASCIIの英数字・ハイフン・アンダースコアを16〜128文字で指定する。同じキーと内容の再送では受付済みタスクの状態をreplayed=trueで返す。同じキーで指示・vm_id・allow_vm_restart・replace_runningのいずれかが異なる場合と受付処理中は409、受付後に履歴が削除されていれば410を返す。キーを省略した投入は毎回新しいタスクを作る。

server/submissions.pyとTaskStoreはキーと要求内容のハッシュで照合し、data/submissionsに受付記録を保存する。原文の指示やキーは受付記録に含めない。同じキーの初回保存はファイルロックで保護し、HTTP要求が中断されても保存完了までロックを保持する。タスク記録と受付記録を保存してからモデルを呼び出す。履歴を削除しても受付記録を残し、バックエンド再起動後の再送にも使う。受付記録や対応する履歴の破損は503として止める。

バックエンドは1プロセスでの運用を対象とし、複数プロセスでのVM操作の排他は未対応とする。

環境の作成は台数制限を含めて直列化し、停止・起動・再起動・削除・専用ネットワークへの接続は環境ごとに排他する。ディスクの複製中も状態の取得と別環境の操作を進められる。要求の中断時も処理の終了まで排他を保つ。

作成要求は専用ワーカーで処理し、実行中と待ちを合わせて8件まで受け付ける。超過は429とRetry-Afterを返す。要求を中断しても処理の終了まで受付枠を保ち、成功・失敗・中断の後に枠を戻す。

### 環境作成の再送

ブラウザはPOST /vm-creationsで受付IDを取得し、GET /vm-creations/{id}でqueued・running・succeeded・failedを確認する。受付キーのハッシュ・名前・種類をdata/vm-creationsに保存する。同じキーで同じ内容を再送すると元の受付を返し、内容の変更は409を返す。完了後や環境削除後も同じキーで再作成しない。新しく作る場合は別のキーを使う。succeededは払い出しの完了で、利用可否はGET /vmsのstatus・healthで確認する。

作成処理の前にrunningを保存する。起動時はqueuedを再開し、runningは対象IDの環境を照合する。環境が存在すれば作成済みとし、なければその作成に属する資源を削除して中断を記録する。正常終了時は未開始を失敗とし、開始済みの処理と記録の完了を待つ。記録の破損や保存失敗は503を返す。

ブラウザは受付キーをタブ内のsessionStorageに保持し、再読み込み後も結果を確認する。応答不明ではキーを残し、結果確定後に消す。ブラウザ保存が使えない場合はページ内だけで保持する。照会は1回10秒、全体10分までとし、画面を閉じても受付済みの作成は取り消さない。

### 実行制限

agent/budget.pyが実行制限を管理する。起動時に環境変数を検証し、TaskSession.runでタスクごとのContextVarを設定する。モデルへのHTTP送信前と使用量の受信時、画面操作前に上限を確認する。再試行と決定モデルも同じ予算を使う。OpenAI SDKの自動再試行は無効にし、送信ごとに回数を数える。

期限切れではディスプレイに停止を要求してからタスクを取り消し、実行中のスレッドを待って接続を解放する。上限の例外はLLMへの退避やエラー回復に変換しない。execution_limits・execution_usage・stop_reason・error_messageをTaskRecordに保存し、履歴の取得後も停止理由と使用量を表示する。推定費用は設定単価で算出し、請求額とは区別する。

### 永続化

data/tasks/タスクID.jsonに1タスク1ファイルで保存し、状態遷移とアクション実行のたびに更新する。TaskRecordは指示、状態、成否、操作履歴、判断理由、確信度、サブタスク、進捗index、vm_id、Goal、トークンと呼出回数を持つ。決定モデルのdecision_callsとdecision_escalationsも記録する。起動時は全件を確認し、実行中だった記録をinterruptedに変える。

server/persistence.pyが一時ファイルのfsync、os.replace、親ディレクトリのfsyncで保存する。新規ファイルは0600とする。保存先に書き込めなければ起動・タスク投入を止め、実行中の保存失敗でも以後の操作を打ち切る。保存できない場合も画面には失敗を通知する。

履歴の入出力は別スレッドで行い、同じセッションの保存を直列化する。タスク投入には初回保存後に応答する。停止時も進行中の保存を待ってから接続を解放する。

開始順はロックを取得して採番し、最初の状態通知より前にdata/task-order.jsonへ保存する。履歴にもsession_orderを保存し、時計が巻き戻っても開始順で最新のタスクを選ぶ。開始順のない旧履歴は0として扱い、旧履歴同士は更新時刻で並べる。開始順ファイルがなければ履歴の最大値から再開し、破損時は上書きせず新しいタスクを止める。

履歴一覧と環境ごとの最新状態は、全件の操作履歴を保持せずに集計する。ただし一覧の取得では全ファイルを読む。履歴の型と数値、IDとファイル名の一致を検証し、不正な履歴は読み飛ばして書き換えない。

### KVM判定

server/kvm.pyはUSE_KVM明示値、バックエンドの/dev/kvm、Dockerの順に判定する。Dockerでは同じComposeプロジェクトのvmに/dev/kvmが割り当てられていることを確認する。停止後の起動にも使うため稼働状態は問わず、ホストのデバイス消失はDockerの起動エラーとなる。KVMなしでのQEMU系の作成・起動・再起動はAPI 409で拒否し、ALLOW_TCG_VM=trueでのみTCGを許可する。

### VM制御

server/vm_control.pyのVmControllerが既定VMの状態取得と再起動を担う。状態にQMPのquery-status結果をqmp_statusとして添える。明示名を指定しない場合、所属プロジェクトのCompose命名に一致するVMを一覧と共通の基準で優先する。

VM_CONTAINER_NAMEで別環境を指定したときも、実際の再起動対象のタスクとAndroid初期設定を止める。再起動前に対象の環境IDが変わったときは503で拒否する。

Dockerの照会失敗は503を返し、キャッシュの稼働状態で代用しない。対象の消失はnot_foundとする。再起動の応答が失われた場合は503を返す。既に実行済みの場合があるため自動で再実行せず、再操作前に状態を確認する。

QMPは要求IDに合う返答を使い、通知と不明なIDの返答を読み飛ばす。接続処理と各操作の待ち時間を制限し、1応答は4 MiBまでとする。通信失敗・JSON不正・応答サイズ超過時は接続を閉じ、再接続してから使う。

### VMプール

server/vm_pool.pyのVmPoolが環境の一覧・作成・起動・停止・再起動・削除を担う。固定desktopはid=desktop、managed=Falseで検出する。default_vmは稼働中のdesktopを優先し、なければ稼働中の先頭を返す。

固定環境の一覧・識別・専用ネットワークの準備には同じ基準で選んだ本体を使う。停止・削除では同じプロジェクトの同じサービスの本体と対応する中継をまとめて扱う。動的環境で同じ環境IDの本体が複数ある場合は503で拒否する。

コンテナの自動検索は一覧のラベルとネットワークで所属を絞ってから詳細を取得する。旧環境は詳細取得後も所属を確認する。一覧から詳細取得までに消えた対象は除外し、環境一覧ではその他の詳細取得失敗を503で返す。ホーム用の補助コンテナの親が稼働中か、ボリュームが使用中かは一覧情報で確認する。

Android初期設定は準備前後でコンテナIDを照合し、接続先と保存領域の識別子を同じ本体に結び付ける。ADB子プロセスの起動前と実行中に対象を確認し、変更や照会失敗では完了を記録しない。実行中の照会は別ワーカーで行い、応答待ちでも子プロセスの中断を妨げない。進行中の照会が終わるまで次の操作へ引き渡さない。

APIのタスク開始と起動・停止・削除・デバッグ用再起動は環境IDごとに直列化する。接続・初回保存・中断後の後始末が終わるまで同じ環境の次の操作を待たせ、別環境の操作は進める。既定環境は待機後に利用可否を再確認する。開始順は登録前に確保し、遅れて登録された古いタスクで最新タスクを置き換えない。

POST /vmsのkindはqemuが既定で、containerとandroidも選べる。各環境は専用コンテナで動く。QEMU系は通常KVMを必要とし、TCGの明示許可時は/dev/kvmを要求しない。containerはKVM不要となる。動的IDの接頭辞はvm、desk、andで、その後に16進数を付ける。動的環境は全種別の合計で8台までとする。

環境情報はdata/vms.jsonに保存し、kindは一覧にも返す。旧APIのvnc_port・ws_port・adb_portは0を返し、画面接続には/vnc/{id}を使う。固定vm・desktop・androidもDELETE /vmsで削除できる。ホストパスはバックエンド自身の/app/dataマウント元から逆算する。

## 状態機械と実行フロー

### 状態と遷移

有限状態機械とする。agent/state.pyとagent/loop.pyで定義し、AgentLoopが有効遷移のみを通す。不正遷移はInvalidTransitionErrorとする。有効遷移の基準は_VALID_TRANSITIONSでagent/state.py:29に置く。

```mermaid
stateDiagram-v2
    direction LR
    [*] --> IDLE
    IDLE --> UNDERSTANDING : start()
    UNDERSTANDING --> PLANNING : 理解完了
    UNDERSTANDING --> FAILED : 理解失敗
    PLANNING --> EXECUTING : 計画ready
    PLANNING --> FAILED : 分解失敗
    EXECUTING --> WAITING : アクション実行
    EXECUTING --> EXECUTING : 継続 (自己遷移)
    EXECUTING --> PAUSED : 一時停止
    EXECUTING --> AWAITING_USER : 手動入力を要求
    EXECUTING --> FAILED : fail_now() 上限・例外・取消
    EXECUTING --> RECOVERING : 即時回復 (稀)
    EXECUTING --> COMPLETED : 即時完了 (稀)
    WAITING --> VERIFYING : 待機完了
    WAITING --> RECOVERING : タイムアウト
    WAITING --> FAILED : 取消
    VERIFYING --> EXECUTING : 継続
    VERIFYING --> COMPLETED : 全サブタスク完了
    VERIFYING --> RECOVERING : 検証失敗
    VERIFYING --> FAILED : 上限超過
    RECOVERING --> EXECUTING : リトライ
    RECOVERING --> PLANNING : 再計画
    RECOVERING --> FAILED : 回復不能
    RECOVERING --> AWAITING_USER : 手動入力を要求
    AWAITING_USER --> EXECUTING : 画面送信を許可して再開
    AWAITING_USER --> RECOVERING : 回復操作へ復帰
    AWAITING_USER --> FAILED : 停止・期限切れ
    PAUSED --> EXECUTING : 再開
    PAUSED --> IDLE : 中断
    PAUSED --> FAILED : 停止
    COMPLETED --> IDLE : reset
    COMPLETED --> FAILED : 完了記録の保存失敗
    FAILED --> IDLE : reset

    note right of VERIFYING
        クリックは画面変化を検証
        完了宣言は画像つきLLM検証
    end note

    note right of RECOVERING
        エラー情報を次判断へ引継ぎ
        (_pending_error)
    end note

    note right of PAUSED
        pause() は EXECUTING からのみ
        interrupted は状態ではなく
        永続化上のマーク
    end note
```

interruptedはバックエンド再起動後の永続化上の目印で、状態機械の状態ではない。pauseはEXECUTINGから行う。手動入力を終えると要求前のEXECUTINGかRECOVERINGへ戻る。上限超過・例外・取消ではFAILEDに移る。一時停止中と手動入力中の停止もFAILEDとして保存し、完了を通知する。

### 実行フロー

TaskSessionの実行順序はserver/session.py:runに置く。

```mermaid
sequenceDiagram
    actor U as ユーザー
    participant FE as frontend
    participant BE as backend (TaskSession)
    participant LLM as LLM
    participant VM as VM
    U->>FE: 自然言語の指示
    FE->>BE: POST /tasks
    activate BE
    BE->>LLM: understand_instruction()
    LLM-->>BE: Goal（意図・対象・制約）
    BE->>LLM: decompose_task()
    LLM-->>BE: サブタスク列
    loop サブタスク消化
        BE->>VM: スクリーンショット取得
        VM-->>BE: 生画像
        BE->>BE: グリッド重畳＋黒画面ウェイク
        BE->>LLM: decide_next_action()（最新の画像＋直近の操作履歴＋作業メモ＋エラー情報）
        LLM-->>BE: Action＋期待効果＋確信度＋メモの更新
        alt vm_restart要求
            BE->>BE: allow_vm_restart判定（未許可は却下して回復へ）
            BE->>VM: VM再起動＋60秒待機（許可時のみ）
        else 操作なし完了宣言
            BE->>LLM: 却下して再問い合わせ（最大2回）
        else 低確信度・連打
            BE->>LLM: region_select→拡大画像で再判断
        else 同一操作3連続・画面無変化
            BE->>BE: waitに置換
        end
        BE->>VM: マウス・キー実行（vncdotool）
        BE->>VM: 画面安定待ち（視覚系は wait_for_still）
        alt クリック系
            BE->>VM: 変化の有無をハッシュ比較（無変化はRECOVERING）
        else 完了宣言
            BE->>LLM: 画像つき達成検証（未達は上限まで回復）
        else 機械的失敗
            BE->>BE: RECOVERINGへ（_pending_error引継ぎ）
        end
        BE->>FE: WSで状態・操作ログをプッシュ
    end
    BE->>FE: 完了通知＋履歴更新
    BE->>BE: data/tasks/*.jsonに保存
    deactivate BE
```

処理は次の順序で進む。まずUNDERSTANDINGでunderstand_instructionにより指示をGoalに変える。Goalは意図と対象アプリと制約とする。失敗時は専用遷移を呼ばず、汎用例外処理でFAILED化する。次にPLANNINGでdecompose_taskによりサブタスク列に分ける。サブタスクはIDと説明と期待結果を持つ。続いてEXECUTINGからWAITINGを経てVERIFYINGに戻る循環に入る。_execute_phaseが担う。画面取得では重畳付き画像を使う。真っ黒ならShiftキーを送って復帰させ、再取得する。前回と同一画面なら無変化の旨をLLMに渡す。操作なし完了宣言は最大2回まで却下して問い直す。MAX_EMPTY_COMPLETE_REFUSALSと_is_empty_completeで判定する。低確信度クリックと同一座標連打はregion_select拡大フローへ回す。画面が変わらず同一アクションが3回続く場合はwaitに置き換える。画面が変わる連続操作は実行する。vm_restartは専用フローで処理する。座標は画面内に収めてから実行する。視覚系操作後は画面安定待ちを行う。wait_for_stillを使う。ステップ上限200を超えるとfail_nowで打ち切る。MAX_ACTIONS_PER_TASKで定める。

VERIFYINGでは_verify_phaseが機械的失敗をRECOVERINGへ回す。エラー情報は次判断へ引き継ぐ。クリック系は画面変化の有無をハッシュ比較する。無変化は空振りとしてRECOVERINGへ回す。subtask_completeは期待結果の達成を画像付きでLLM検証する。_verify_subtask_phaseで行う。未達は上限まで回復し、超過でfail_nowとする。上限はmax_retriesで定める。

RECOVERINGでは最新のエラーから回復計画を作る。回復操作を実行して記録し、理由を次の判断へ渡す。再計画では完了済みのサブタスクを残して残作業を分解する。再試行はmax_retries、再計画はタスク内3回、全操作は200回までとする。回復中のVM再起動にも許可確認を適用する。

### VM再起動

vm_restartはallow_vm_restartが真の場合だけ実行する。未許可なら却下して回復フローへ戻る。ディスクの初期化は行わず、再起動後はRESTART_SETTLE_SECONDSの60秒待ってから画面を確認する。

## 意思決定層

判断は生成系LLMと決定モデルの分担を方針とする。生成の座標と文章と計画はLLM、定型判断の成否と戦略選択は決定モデルに寄せる。

### LLMプロバイダ

agent/llm配下に置く。factory.pyのcreate_llm_providerが全提供元をOpenAICompatProviderに束ねる。対応はopenaiとanthropicとopenrouterとopencodeのZen、opencode-goのGo、ollama、mockとする。既定モデル例はgpt-4oとdeepseek-v4.1-flashとする。LLM_PROVIDERとLLM_MODELと各種APIキーで切り替える。

openai_compat_provider.pyの仕様は次の通りとする。画像は同一解像度JPEGのq80とdetail highで送る。座標系を保ちつつ軽くするためである。出力形式はStructured Outputのresponse_format json_schemaで強制する。非対応モデルでは通常JSONモードへ自動切替する。_structured_output退避で行う。不正な引数は_sanitize_paramsで整え、画面内収めは_clamp_paramsで行う。必須項目の欠落はActionの検証で誤りとする。OpenCode系にはUser-Agentのai-desktop-agent/0.1.0を付け、タスクIDをx-opencode-sessionで送る。session_idに対応する。呼出ごとのトークン使用量は_record_usageで累積し、タスク記録に保存する。max_tokensは1500に絞る。出力は小さいJSONのみのためである。

システムプロンプト方針は座標グリッドの読み方、2段階クリック、アプリ起動はキーボード優先、不足アプリは端末で導入、subtask_completeは操作後のみとする。画面文字はLLMが画像から直接読む。

### 画面をまたぐ作業メモ

たとえば依頼書から「ペンの訂正額は950円」と読み取ってCalcに転記するとき、画面を切り替えると依頼書は見えなくなる。そのため、エージェントは読み取った値と出典をタスク専用のメモ（`Goal.working_note`）に入れ、Calcの画面と一緒に次の操作判断へ渡す。アプリを切り替えても操作を決めるモデルは同じである。

```mermaid
sequenceDiagram
    participant S as タスクセッション
    participant M as 操作を決めるモデル
    S->>M: 依頼書の画面・指示・現在のサブタスク・直近の操作
    M-->>S: Calcへ切り替える操作と「依頼書：ペンは950円」のメモ
    S->>S: メモを保持して画面を切り替える
    S->>M: Calcの画面・指示・現在のサブタスク・直近の操作・メモ
    M-->>S: 金額を入力する操作
```

`OpenAICompatProvider.decide_next_action` は毎回、最新の画像、ゴールと制約、現在のサブタスク、直近10件までの操作履歴、現在のメモを組み合わせる。履歴には操作の種類・引数・成否が入り、メモには次の画面でも使う値が入る。過去の画像や会話全文を毎回送ると操作が続くほど入力が増えるため、引き継ぐ情報をこの範囲に絞っている。

モデルは操作とメモを同じ応答で返す。通常画面でも拡大画面でも、`TaskSession` は空のメモを受け取ると前の内容を保ち、値があればメモ全体を置き換える。最大400文字で、更新時には引き続き必要な値も含めるようモデルに指示する。新しいタスクは空のメモから始まる。読み取りを誤ったり値を残し損ねたりした場合は、元の画面で再確認する必要がある。

### 決定モデルの組込

実装済みとする。agent/decisionに置き、受け付ける型はnoulの成否とchoiceの戦略選択とscoreの評価の3型とする。System One互換APIで受け、提供元はDECISION_PROVIDERで切り替える。noneは従来動作で既定とし、mockは外部通信なしの簡易判定、clefはWorkers AI直結のClefとclef-flash、clef-openrouterはOpenRouter Decisions API経由のClef系を使う。clef-openrouterのみLLM本体と別のAPIキー（CLEF_API_KEY優先、なければOPENROUTER_API_KEY）を使える。既定モデルはclef-flashとする。中間帯と失敗時はLLMに退避するため、誤判定より退避を優先する。利用実績はdecision_callsとdecision_escalationsで記録する。

| 番号 | 組込点 | 置換対象 | 期待効果 |
|---|---|---|---|
| 1 | 回復戦略の選択 | _recover_phaseのLLM呼出 | 数百msで戦略確定。文章生成が不要な分類に適する |
| 2 | サブタスク達成検証 | _verify_subtask_phaseの画像付きLLM検証 | 高確信で前進し、低確信で回復し、中間でLLM検証へ回す。確率閾値で設計する |
| 3 | 評価ハーネス | なし。新設済み | 保存済みタスク記録を正解集団とし、精度と分布を測る。agent/decision/eval.pyとscripts/eval_clef.pyを使う。推奨順序はClef-flashからClefとする |

置換しないものはdecide_next_actionの座標とJSON生成、タスク分解、画面文字の読解とする。判断は決定モデル、実行内容の生成はLLMの分担を保つ。ベンチマークは提供元申告のため、自タスクでの精度と待ち時間と単価で選ぶ。利用可否の検討内容はdocs/research/clef.mdに残す。

## 観測

LLMに渡すのは生画像ではない。座標ヒントを重ねた画像と文字情報とする。

画面取得の値オブジェクトはvm/screenshot.pyに置く。with_overlayとcrop_with_metaを持つ。重畳描画はvm/overlay.pyに置く。50pxグリッドと100pxごとの太線、上端と左端の座標数字、カーソル十字、外周枠を描く。文字は12ptとする。ズーム画像には25pxグリッドを使う。領域ズームはcrop_region_with_metaで行い、2倍拡大画像と実倍率を返す。逆変換はabsolute = origin + zoomed_coord / actual_scaleで行う。_handle_region_zoomが担う。入れ子は深さ1までとする。

画面文字の読解はLLMが画像から直接行う。OCR機構（vm/ocr.py・WAIT_FOR_TEXT）は実画面ABで有益信号がなく削除した。

カーソル情報はVNCClientのcursor_positionとhas_custom_cursorを使う。cursor_positionは通信層の値を優先し、取れなければ内部追跡値を返す。重畳の十字には内部追跡の_cursor_xと_cursor_yを直接使う。

照合はvm/matcher.pyに置く。PILのみの正規化相互相関とし、単色はSAD併用、粗密2段階とする。executor.locateから利用できる。テンプレート供給元は今後の課題とする。

黒画面ウェイクはDPMS消灯などで全面真っ黒を検出したら起こして再取得する。平均輝度2.0未満を黒とみなし、最大1回まで行う。_is_black_screenで判定する。Linux系はShiftキーを送る。Android端末のandroidとand始まりは公式手順に沿い、ESCからMenuの1秒超保持と中央タップを行う。VNCClient.wake_androidが担う。実機の深い休止では戻らない場合がある。このためAndroidの休止対策は初期設定での無効化を本命とする。内容は休止最大と起床状態維持ONと画面保護無効とし、ADBのshell命令のみで行う。画面施錠の無効化命令は含まない。VNCタップ列は使わない。到達不能時は警告記録に留め、次回に再試行する。

無変化検出は前回のハッシュ比較で行い、画面が変わっていない旨を次判断に伝える。

## アクション実行

定義はactions/primitives.pyに置く。マウス7種とキー4種と待機3種、内部用のscreenshot、region_select、subtask_complete、vm_restartを持つ。クリック系は座標必須とする。allowed_paramsとrequired_paramsで検証する。

実行はactions/executor.pyに置き、Actionをbackend操作に変える。REGION_SELECTとVM_RESTARTはセッション側で処理する。このため実行器に届いた場合は警告のみ出す。REQUEST_USER_INPUTもセッションで処理し、実行器に届けば失敗とする。安定待ちのwait_for_stillを持つ。

VNC実装はvm/vnc_client.pyに置き、vncdotoolを包む。接続時はRFBの初期化まで待つ。vm/vnc_transport.pyのManagedVNCProxyは操作を直列化し、操作ごとのFutureで結果を返す。応答待ちの上限は10秒とし、期限切れや切断では待機中の操作と接続処理を取り消してTCP接続を閉じる。無効な入力のエラーやNoneの戻り値も、次の操作の結果に影響させない。

ASCIIのtype_textは1文字ずつキーイベントを送る。Shiftが必要な文字はShift修飾を使わず文字自体のX11 keysymを直接送る。Shift修飾はVNC経路で外れる実測があるためである。大文字AからZはそのまま通す。vncdotoolのKEYMAPに無い名前のbackspaceなどは起動時に補う。ドラッグはmouseMoveを段階的に送り、途中で停止要求を確認する。クリックは移動と整定待ち、50ms押下、解放の順に行う。ダブルクリックは同座標明示とする。組合せキーは20ms間隔とする。座標は画面内に収める。

Unicode入力はvm/clipboard.pyで[RFB Extended Clipboard](https://github.com/rfbproto/rfbproto/blob/master/rfbproto.rst#extended-clipboard-pseudo-encoding)を実装する。サーバーの対応を確認し、NotifyとShift+Insertで貼り付け、要求された入力文字列だけをUTF-8のProvideで返す。入力は64 KiB、受信メッセージは1 MiB、応答待ちは2秒を上限とし、待機中も停止を確認する。既存のクリップボードの取得要求と受信データの展開は行わない。QEMU・AndroidのUnicode入力は未対応となる。

## VM操作

### 実行環境の3種

desktopはdesktop配下に置き、XfceとTigerVNCをcontainerプロファイルで動かす。QEMUは不要となる。VNCはDocker内部のdesktop:5900で使い、画面配信は認証付きプロキシを通す。解像度は1280x800でVNC_GEOMETRYが指定する。FirefoxとLibreOfficeのWriterとCalc、xdotool、wmctrlを同梱する。LANG=C.UTF-8を既定とし、動的コンテナにも指定する。

vmはvm配下に置き、QEMUとKVMでKDEを動かす。kvmプロファイルに属し、起動条件と選択は構成の節に従う。up.shがバックエンドの接続先も切り替える。VmPool.default_vmはdesktop稼働時にはdesktopを優先する。

androidはAndroid-x86をQEMUで動かし、動的払い出しとandroidサービスで提供する。QEMUとKVM上でAndroid-x86のISOをLive起動する。VNC操作は通常VMと同一のため、VNCClientをそのまま使う。タップはクリック、スワイプはドラッグに対応付ける。KVM必須でkvmプロファイル配下とする。ISOはandroid/android-x86_64.isoに配置する。容量のためgit管理外とし、手順はandroid/README.mdに置く。休止しない設定は初回配備で入れる。AndroidProvisionerとandroid_adbで行う。内容は休止最大と起床状態維持ONと画面保護無効とする。ADBのshell命令のみとし、画面施錠の無効化命令は含まない。VNCタップ列は使わない。seed同梱なしで空dataから設定し、定期的なキー送信はしない。到達不能時は警告記録に留め、次回に再試行する。QEMUのADB転送のhostfwdは既定ONとする。詳細はandroid/README.mdに置く。

### KVM自動切替と起動制限

USE_KVMは未設定時にautoとする。起動前にゲスト4ファイルを確認し、Composeのマウントも存在しないファイルを自動作成しない。QEMUのentrypointも必要なファイルを確認する。TCGはALLOW_TCG_VM=trueでのみ許可する。エージェントのvm_restartが制限環境で要求された場合は失敗として_pending_errorに渡し、別手段での継続を促す。

VmPoolはDocker経由で専用コンテナを払い出す。新しい環境に公開ポートや中継は作らない。起動時に同じプロジェクトの旧中継を撤去し、失敗した場合はバックエンドの起動を止める。既存環境のVNC公開設定は再作成するまで残る。QEMU VMは通常KVMを必要とし、コンテナは不要となる。

```mermaid
sequenceDiagram
    actor U as ユーザー
    participant FE as frontend
    participant POOL as backend (VmPool)
    participant DOCKER as Docker
    participant VM as vm-id / desk-id / and-id
    U->>FE: + VM追加 / + コンテナ追加 / + Android追加
    FE->>POOL: POST /vms {kind}
    activate POOL
    POOL->>DOCKER: base qcow2をフルコピー (cp --sparse=always、qemuのみ)
    DOCKER-->>POOL: /vm/overlays/id.qcow2
    POOL->>DOCKER: 専用コンテナ起動 (VNCはDocker内部の5900)
    POOL-->>FE: id, kind (status creating)
    deactivate POOL
    FE->>FE: 定期的に /vms を追跡 (一覧15秒/監視2〜5秒) healthyまで
    FE->>POOL: 認証付き WS /vnc/id
    POOL->>VM: VNCのバイナリ転送
    FE->>POOL: POST /tasks (vm_idつき)
    POOL->>VM: VNC接続・エージェント実行 (vm:5900 / vm-id:5900)
    VM-->>POOL: 画面・操作
    opt 再起動（任意）
        FE->>POOL: POST /vm/restart または agent vm_restart (許可制)
        POOL->>DOCKER: コンテナ再起動 (実行中タスク停止つき)
        POOL->>POOL: 60秒待機して続行 (RESTART_SETTLE_SECONDS)
    end
    FE->>POOL: DELETE /vms/id
    activate POOL
    POOL->>POOL: 上タスク停止
    POOL->>DOCKER: コンテナ停止・削除
    POOL->>DOCKER: ディスク削除 (qemuのみ)
    POOL-->>FE: deleted
    deactivate POOL
    Note right of POOL: 固定環境も自動検出<br/>環境情報は data/vms.json に保存<br/>QEMU系は通常KVMを必要とする<br/>kind=container はKVM不要
```

### QEMU起動構成

vm/entrypoint.shに置く。

```text
qemu-system-x86_64 -enable-kvm -cpu host -smp $VM_CPUS -m $VM_MEMORY
  -kernel $VM_KERNEL -initrd $VM_INITRD -append "$CMDLINE"
  -drive file=$VM_IMAGE,if=virtio,format=qcow2
  -vnc 0.0.0.0:$VNC_DISPLAY -device virtio-net,netdev=net0 -netdev user,id=net0
  -serial stdio -display none
  -chardev socket,path=$QMP_SOCK,... -mon chardev=...,mode=control
  -chardev socket,... -device virtio-serial-pci -device virtserialport,...,name=org.qemu.guest_agent.0
```

KVMとhost CPU、4vCPUと4GBを既定とする。固定値はcomposeで定める。マシン型は変えない。NIC名のens3がnetplanに固定のためである。USE_KVM=auto時は/dev/kvmの有無でKVMとTCGを自動選択する。TCG側は既定で起動拒否し、ALLOW_TCG_VM=trueでのみ許可する。許可時のTCGは-cpu qemu64と-smp 1に切り替わる。保存はvirtio、通信はslirpのuser-mode NAT、VNC直結、シリアルは起動記録用とする。QMPとguest-agent用ソケットは/vm/sockets/配下とし、QMP_SOCKとQGA_SOCK環境変数でVMごとに変えられる。

標準起動ではprepare-boot.shが各qcow2を一時的なrawファイルへ変換し、元ディスクをマウントせずに起動ファイルを取得する。コピーのext4を検査し、/lib/modulesに対応するカーネルとinitrdのうち、両方が揃う最新バージョンを選ぶ。ディスク内の/etc/ai-desktop-agent/cmdlineを使い、旧画像でこのファイルがない場合はroot=/dev/vdaの既定値を使う。

準備は300秒で中断を要求し、終了しなければ5秒後に強制終了する。失敗時はQEMUを起動せず、元ディスクを保持する。実行ロックは準備からQEMUの終了まで保持する。VM_KERNELとVM_INITRDの両方を明示した場合は指定したファイルを使い、CMDLINE_FILEも上書きに使える。

### ゲスト画像構築

vm/build-vm-image.shで行う。debootstrapでUbuntu 24.04とKDE PlasmaとSDDMを構築し、qcow2化する。再構築時の必須項目は次の通りとする。利用者agentのNOPASSWD sudo、SDDM自動ログインでセッション自動検出、画面施錠無効と電源管理の休止と画面OFF無効、ログイン時DPMS無効化のdisable-dpms.desktopでXのDPMS 600秒消灯が残るため、netplanでens3をDHCP化でNetworkManager単独ではunmanagedになるため、/etc/resolv.confをQEMU内蔵DNSの10.0.2.3直書きと/etc/hostsに自ホスト名、ディスク下限15GBでKDEとsnap導入で3から4GBでは枯渇するため、qemu-guest-agentとxdotoolとwmctrlを同梱とする。注意点としてUbuntu 24.04のfirefoxパッケージはsnap移行置物である。エージェントがsudo snap install firefoxで導入する運用とし、通信が必要である。

構築ごとに専用の作業領域を作り、同じ出力先での同時構築を拒否する。4ファイルが完成してから出力を更新し、更新エラーやINT・TERMによる中断では旧ファイルへの復元を試みる。マウント解除や復元に失敗した場合は作業領域を残し、保存先を表示して異常終了する。

テンプレートの更新は既存ディスクに反映しない。構築出力全体の電源断からの復旧は未対応とする。

### ゲスト内通信

QEMU slirpは10.0.2.0/24、入口は.2、DNSは.3とする。過去のつまずきはNIC DOWN、NM unmanaged、resolv.confのstub迷子、ディスク枯渇とする。いずれも上記設定で解消済みとする。再発時はip -brief addrとgetent hostsから切り分ける。

### 保存方式

固定desktopと新しい動的コンテナはproject_home_IDという専用ボリュームに/home/agentを保存する。WorkspaceVolumesは所属・用途・localドライバーを照合し、動的環境の削除時だけボリュームも回収する。旧版の動的コンテナは更新で再作成せず、ホームの自動移行もしない。

scripts/up.shはビルド後、workspace_migrationの別コンテナで更新ロックを取得する。通常のバックエンドと選択した固定環境を止め、固定desktopのホームを退避する。HomeBackupsは0600のtarとSHA-256付き情報を保存し、通常ファイル・ディレクトリ・リンクを検査する。旧ホームはネットワークのない専用コンテナでボリュームへ展開し、ルートの所有者とモードも復元して読み戻す。照合後にComposeで再作成し、完了までロックを保持する。失敗時は残っている元のコンテナの再開を試み、バックアップを残す。旧版の専用bridgeは既知の接続先を停止してからCompose用ラベルも補う。

vm/desktop.qcow2は読み取り専用テンプレートとする。固定VMは初回だけvm/instances/default.qcow2へ複製し、再起動で上書きしない。新しい動的VMはvm/overlays/環境ID/disk.qcow2へ独立したコピーを作り、その環境のフォルダだけをマウントする。起動ファイルはディスク名に.bootを付けたフォルダへ取得する。動的ディスクはバックエンドの/vm/overlaysにも共有マウントし、コンテナの削除成功後にフォルダごと削除する。旧形式のvm/overlays/環境ID.qcow2の削除も扱う。固定VMの削除では専用ディスクを残す。

data/vms.jsonはファイルロックで同時更新を保護し、原子的に保存する。破損した情報は上書きせずエラーを返す。作成前に割当情報を保存し、作成失敗時はその環境のコンテナとディスクの回収を試みる。

### 監視と管理

追加QEMUの複製用コンテナはprepare-boot.shの存在を先に検査し、未対応なら作成を止める。複製は300秒で中断を要求し、終了しなければ5秒後に強制終了する。所属と用途のラベルを付け、待ち時間切れなどの失敗でも回収対象にする。複製用コンテナは環境一覧とタスクの接続先から除く。

ヘルス確認はVNCポート疎通でComposeが行う。QMPのvm/qmp.pyはquery-statusなどを行い、/vm/statusのqmp_statusに反映する。guest agentのvm/qga.pyはguest-ping、guest-get-osinfo、guest-execを提供し、診断と検証に使う。画面配信はserver/vnc_proxy.pyが担い、ブラウザはVncViewer.tsxのnoVNCで表示する。VmControllerは既定VMの状態と再起動、VmPoolは各環境の管理を担う。

QGAは接続時に同期し、要求IDに合う返答を使う。runの待ち時間には開始要求の送信と結果取得を含める。期限切れでもゲスト内の処理は継続する場合がある。例外にPIDが残っていれば、再接続して状態を確認する。

応答は4 MiBまでとする。取得できた標準出力と標準エラーは各16 KiBを上限とし、超過やゲスト側の省略はQgaOutputLimitErrorで通知する。例外には保持できた出力と終了コードを残す。

ContainerHostnameEndpointはTwistedのホスト名変換を拡張し、Docker形式のASCII接続名に含まれるアンダースコアを許可する。通常の名前・IPアドレス・Unicode名の変換、名前解決、IPv4/IPv6の接続試行はTwistedを使う。VNCClientはIPv6アドレスをvncdotoolへ角括弧付きで渡す。中断時は名前解決後の接続待ちも取り消し、中断後に名前解決が完了しても接続を始めない。

## フロントエンド

Next.jsのApp Routerを使う。InstructionInputで指示と再起動許可を受け、StatusPanelとLogPanelで進捗を示す。VmTabsで環境を選ぶ。VncViewerは選択した稼働中の環境だけに接続し、切断時にRFBを作り直す。入力の送信失敗時は指示を残し、日本語変換中のEnterでは送信しない。タスク投入の応答待ちは30秒までとし、期限切れでもタスクは継続する場合がある。

デバッグ系はCollapsibleSectionで折り畳む。側面幅は280から720pxで変更し、localStorageに保存する。状態・ログ・進捗はVM単位で保つ。WebSocketはpingで疎通を確認し、再接続時にsnapshotで状態を同期する。履歴の操作記録はGET /tasks/{id}で取得する。

VMControlsは状態を1回10秒まで取得し、応答後に次の確認を予約する。再起動の応答後に始めた確認だけで完了を判断する。準備完了・異常・停止で確認を終え、再起動の応答がなくても10分で確認を打ち切る。期限後や画面を閉じた後に届く再起動の応答は表示へ反映しない。

## 安全性

安全性と未実装の対策は[safety.md](safety.md)にまとめる。バックエンドはDockerソケットを使うため、バックエンド侵害時までホストの隔離を保証する構成ではない。利用者ごとの権限分離と外部公開の検証は未対応とする。

privacy.pyは登録済みのキーをモデルのテキストと保存・配信する記録から除く。ログのメッセージ・例外・最終出力も置き換え、VNCなどのプロトコルのDEBUGログは省く。画像の自動マスキングは行わない。

## 制限と今後

テンプレート照合の供給元のアイコンDBなどが未整備である。決定モデルのClefとSystemOneは実装済みとし、既定は無効とする。意思決定層の表の通り、評価ハーネスで測ってから回復戦略と達成検証の順で有効化する。複数VMのUIはタブ切替と単一ビューアのみとする。同時監視グリッドなどは未対応とする。モデル依存のゆらぎの空応答と非JSONは再試行と代替で吸収しているが、応答の安定したモデル選定が最も効く。

## 図の管理

図は本書のMermaidブロックを基準とする。画像ファイルの生成と管理はしない。状態遷移を変えたらagent/state.pyの_VALID_TRANSITIONSとagent/loop.pyを先に直し、状態機械のMermaidに合わせる。図だけを先に変えないこと。順序を変えたらserver/session.pyとserver/vm_pool.pyとserver/app.pyの順で裏取りする。
