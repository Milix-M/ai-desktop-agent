# Android実行環境

QEMUとKVM上でAndroid-x86を起動する。VNC操作は通常VMと同一のため、VNCClientをそのまま使う。クリックはタップ、ドラッグはスワイプに対応付ける。

## ISOの取得

ISO自体は約1GBのためリポジトリに含めない。android/android-x86_64.isoに配置する。iso拡張子はgit管理外とする。

```bash
# 例: Android-x86 9.0-r2
curl -L -o android/android-x86_64.iso \
  https://downloads.sourceforge.net/project/android-x86/Release%209.0/android-x86_64-9.0-r2.iso
printf '%s  %s\n' f7eb8fc56f29ad5432335dc054183acf086c539f3990f0b6e9ff58bd6df4604e android/android-x86_64.iso | sha256sum -c -
```

## 起動用カーネルの抽出

固定Androidの初回起動前に、カーネルと初期RAMディスクをISOから取り出す。ISOを更新したときも再実行する。

```bash
python3 scripts/extract-android-kernel.py
```

## 永続データを準備する

固定Androidを初めて使うときは、リポジトリのルートで次のコマンドを実行する。`android/android-data.img` に設定とファイルを保存する。既存のイメージを使う場合はこの手順を省く。

```bash
./scripts/mk-android-data.sh
```

旧版で `android/android-data.img` がディレクトリになっている場合は、**コンテナを再作成する前に**次の手順で保存内容を取り出す。先にAndroid上の作業を保存して終了する。

```bash
docker compose --profile kvm stop android
docker compose --profile kvm cp android:/android/data-local.img ./android/recovered-data.img
# コピーに成功し、ext4と表示されることを確認する
file ./android/recovered-data.img
rmdir ./android/android-data.img
mv -T ./android/recovered-data.img ./android/android-data.img
```

コピーに失敗した場合は旧コンテナとディレクトリを保持し、保存場所を確認する。

## 起動手順

```bash
./scripts/up.sh   # Androidは初期起動に含まない
docker compose --profile kvm build --pull \
  --build-arg "PACKAGE_REFRESH=$(date -u +%F)" android
docker compose --profile kvm up -d android
```

操作画面でログインし、androidのタブを選ぶ。画面配信はバックエンドの認証付き接続を使い、VNC・ADBをホストへ直接公開しない。動的払い出しはPOST /vmsにkind androidを指定する。KVM必須とする。起動直後はホーム画面が出る。セットアップウィザードはSETUPWIZARD=0で無人通過する。isolinuxのnosetupと同一とする。VNC 1024x768で画面を見られることを確認済みとする。初回設定はbackendのADB投入で入り、手動タップは要らない。永続dataありでは再起動後も保たれる。

## 初期設定

backend常駐のAndroidProvisionerが未設定端末をADBのshell命令で初期設定する。LLM不使用とする。VNCタップ列は使わない。空data.imgから起動し、同梱シードなしで機械的操作のみで設定する。対象は再起動を超えて/dataに残るものとする。

- ホーム選択はset-home-activityでLauncherに固定する(9.0実機名。Pixel名QuickstepActivityは不可)
- タイムゾーンはAsia/Tokyoとし、ホストJSTと一致する
- ネットワークはEthernet既定(VIRT_WIFI=0)。eth0のまま残しEthernetTrackerがDHCPで10.0.2.15を自動取得する。Wi-Fi接続は必須としない(9.0の `cmd wifi` はshell権限で使えず、wlan0自体が無い構成のため)。Ethernet疎通があればwifi失敗でも成功扱いとする
- スリープ抑止はSleep最大値とスクリーンセーバー無効、Stay awake維持とする。OS標準設定のみを使い、画面ロックの設定は変えず、定期キー送信もしない
- F-Droidはadb installで導入し、以降はドロワー常駐とする(初回は120秒タイムアウト)

設定命令はserver/android_adb.pyに置く。ADBへ接続できなければ次の巡回で再試行し、実行中のタスクがある環境は設定しない。ANDROID_PROVISION=0で自動設定を無効にする。

タスク開始や環境の停止・起動・削除では、その環境の初期設定を中断し、設定用のADBコマンドが終了してから操作を渡す。backendの終了時も設定スレッドの終了を待つ。端末で実行済みの変更は戻さず、未完の設定は次の巡回で再試行する。ADB接続待ちから設定・APK導入までの実行予算は20分とする。中断時は実行中の対象確認が終わるまで待つ。

固定のext4ディスクはUUIDで識別し、通常の書き込みでは初期設定を繰り返さない。固定ディスクを識別できなければ本体のコンテナIDを使い、動的環境もコンテナIDで識別する。識別子が変わると再設定する。初期設定の記録とADB接続には画面と同じ本体を使う。既定では設定記録をdata/android-provision.jsonに保存する。記録が破損するとbackendは初期設定を止め、元のファイルを残す。バックアップから記録を復元する。

設定開始時は接続先・コンテナID・保存領域の識別子をまとめて確認する。ADBコマンドの実行前と実行中にも対象を確認し、本体や保存領域の変更、照会失敗を検知すると中断する。同じ本体と保存領域を確認してから完了を記録する。

固定Androidは事前に用意した `android/android-data.img` を使う。ファイルがなければComposeは起動を止める。動的に追加したAndroidは環境ごとに専用のデータディスクを作る。どちらもゲストの `/dev/vda` を `/data` にマウントし、ADBで初期設定する。確認記録の2026-10-17では空data起動でADB device化・eth0自動取得・Home Launcher固定・日本時間・F-Droid導入を確認した。

## ネットワーク

VIRT_WIFI=0既定でeth0のまま残す。virt_wifi有効時はeth0がwifi_ethに改名されEthernet管理外となりIPv4不通・ADB offline停滞の実測あり(2026-10-17)。eth0ではEthernetTrackerがDHCPで10.0.2.15を自動取得し、dumpsys connectivityでCONNECTED/VALIDATEDを確認した。以降は/dataに保存され再起動後も保持される。

- 接続はEthernet DHCP自動取得とし、Wi-Fi接続は必須としない。ANDROID_VIRT_WIFI=1に限りVirtWifiを使うが、その場合はUI接続が必要なため対象外とする
- NICモデルは既定virtio-netのままとする。ANDROID_NICでe1000などに変えられる
- 確認記録の2026-10-17ではeth0自動取得後にgateway・8.8.8.8へのping疎通とF-Droid導入を確認した

## アプリストア

フリーのストアとしてF-Droidをadb installで導入する。ゲストにネットが無い前提でもインストールまで行える。

```bash
./scripts/fetch-apks.sh   # android/apks/F-Droid.apkを取得する。Aurora Storeなどもここに追加できる
```

仕組みはbackendが置場のapkをadb install -rで入れる。導入済みは成功扱いとする。確認記録の2026-10-14ではF-Droid 2.0.1をインストールして起動し、リポジトリ同期まで確認した。ネットが必要である。Aurora StoreはF-Droid内から入手できる。

## スリープ対策

対処は設定投入のみとし、定期キー送信はしない。VNCタップ列も使わない。検証は観測の節の手順で行う。

### 寝かせない設定

初回投入のserver/android_provision.pyとserver/android_adb.pyで設定する。空data.imgからADBのみで設定し、シード同梱は不要とする。設定は/dataに残るため再起動後も保たれる。

- ADBでsettings put system screen_off_timeout 2147483647、stay_on_while_plugged_in=7、svc power stayon true、スクリーンセーバー無効、タイムゾーンとする
- 到達不能時は警告記録に留め、次回に再試行する。到達にはゲスト側でTCP adbdを開けておく必要がある


注意として手動コンテナが固定androidと同一composeラベルを持つとプール一覧を別物と取り違える。android-nowiz実例があり、到達不能ホストへ送り続けて不発となる。固定宛接続はサービス名androidのcompose DNSに固定して回避する。UIとAPIの停止と起動と削除のPOST /vms/android/stopとstart、DELETE /vms/androidも同一ラベルの手動コンテナまで対象になる。手動コンテナを残したい場合は削除前にラベルを外すか別名管理にすること。

### ADB転送

QEMUのuser-netでゲスト5555番をコンテナのANDROID_ADB_PORT既定5555番に転送する。ANDROID_ADB_FORWARD=1既定とし、無効化は0とする。hostfwdは0.0.0.0 bind必須(127.0.0.1ではコンテナloのみでbackend不通)。backendはandroid:5555固定、and始まり:5555動的で届く。既定構成ではホストへADBポートを公開しない。ゲストadbdのTCP listenはinitrdパッチ+ADB_TCP=1で開ける。`adb connect` は到達不能でもrc=0を返す実測があるため出力判定(connected to)で行う。

### 寝たら起こす

抑止が間に合わない場合に備え、エージェントは黒画面検出時に公式手順準拠のESCとMenu 1秒超ホールドと中央タップを送る。TaskSession._capture_screenshotがvm_idのandroidとand始まりで切替、実送信はVNCClient.wake_androidとする。実機の深いスリープ3例では復帰しなかった。設定の節が本命で、本フォールバックはキーガード段階向けの最善努力と位置づける。

端末で設定を変更するには、次の手順を使う。

1. SettingsからDisplay、Sleepを最長30分に
2. SettingsからSystem、Developer options、Stay awakeをONに。開発者向け項目が無ければAbout tabletのBuild numberを7回タップ

参考はhttps://www.android-x86.org/documentation/wake_machine.htmlとする。

### 背景記録

2026-10-12からの観測事実として固定androidを約23時間放置したところVNCが全面真っ黒になった。Linux VM用のShiftでは復帰しない。公式docによれば復帰キーはESCとMenuと矢印の1秒超ホールドのみとする。放置時間の計測はホストスリープで水増しされる点に注意する。プロセス年齢は覚醒時間のみ積算し、psのetimeと壁時刻差で判別できる。

2026-10-13の起動経路知見としてカーネル直起動の成否は条件依存で揺れる。ANDROID_MEMORY=4096では3例連続でISO検出後に進行しなかった。VNC真っ黒でadbd到達なしとする。2048ではセットアップウィザード到達を確認し、2から3分とする。既定は2GBとした。直起動が黒画面のままの場合は、到着がスリープ後既定タイムアウト約1から2分の可能性を先に疑うこと。androidboot.hardware=android_x86付与は起動ロゴ停止のため禁止で従来通りとする。既定-appendはisolinux.cfgと完全同一に保つこと。

## 観測

- 閲覧はhttp://localhost:3000の操作画面へログインし、Androidの環境を選ぶ
- ログはdocker compose logs backendで確認する。初期設定の開始・完了・未完・中断と失敗を記録する
