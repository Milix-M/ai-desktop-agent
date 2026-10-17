# Android実行環境

QEMUとKVM上でAndroid-x86を起動する。VNC操作は通常VMと同一のため、VNCClientをそのまま使う。クリックはタップ、ドラッグはスワイプに対応付ける。

## ISOの取得

ISO自体は約1GBのためリポジトリに含めない。android/android-x86_64.isoに配置する。iso拡張子はgit管理外とする。

```bash
# 例: Android-x86 9.0-r2
curl -L -o android/android-x86_64.iso \
  https://archive.org/download/android-x86_64-9.0-r2/android-x86_64-9.0-r2.iso
```

## 起動用カーネルの抽出

直起動用のkernel約7MBとinitrd.img約1MBをISOから取り出すと、grub待ち約60秒を省ける。なければISO起動に切り替わる。いずれもgit管理外とする。実測のGUI到達は約2から3分である。

```bash
python3 scripts/extract-android-kernel.py
```

## 起動手順

```bash
./scripts/up.sh   # 初期起動はvmのみでandroidは含まない。必要なら手動で追加する
docker compose --profile kvm up -d android websockify-android
```

固定サービスはandroidのVNC 5590番、websockify-androidの6070番とする。動的払い出しはPOST /vmsにkind androidを指定する。KVM必須とする。起動直後はホーム画面が出る。セットアップウィザードはSETUPWIZARD=0で無人通過する。isolinuxのnosetupと同一とする。VNC 1024x768で画面を見られることを確認済みとする。初回設定はbackendのADB投入で入り、手動タップは要らない。永続dataありでは再起動後も保たれる。

## 永続データの扱い

Live起動は/dataがtmpfsのため、再起動で設定が消える。設定はタイムゾーンとランチャー既定などとする。ext4イメージを用意するとゲストの/dataとして載り、設定が残る。存在しなければ起動時に空作成する。mkfs.ext4で行い、e2fsprogs同梱済みとする。動的払い出しはコンテナ内に自動作成する。使い捨てのため共有しない。

```bash
./scripts/mk-android-data.sh [サイズMB, 既定1024]   # 手動作成で通常は不要
```

## 初期設定

backend常駐のAndroidProvisionerが未設定端末をADBのshell命令で初期設定する。LLM不使用とする。VNCタップ列は使わない。空data.imgから起動し、同梱シードなしで機械的操作のみで設定する。対象は再起動を超えて/dataに残るものとする。

- ホーム選択はset-home-activityでLauncherに固定する(9.0実機名。Pixel名QuickstepActivityは不可)
- タイムゾーンはAsia/Tokyoとし、ホストJSTと一致する
- ネットワークはEthernet既定(VIRT_WIFI=0)。eth0のまま残しEthernetTrackerがDHCPで10.0.2.15を自動取得する。Wi-Fi接続は必須としない(9.0の `cmd wifi` はshell権限で使えず、wlan0自体が無い構成のため)。Ethernet疎通があればwifi失敗でも成功扱いとする
- スリープ抑止はSleep最大値とスクリーンセーバー無効、Stay awake維持、画面ロックなしとする。OS標準設定のみとし、定期キー送信はしない
- F-Droidはadb installで導入し、以降はドロワー常駐とする(初回は120秒タイムアウト)

手順はADBのみとする。server/android_adb.pyがsettings put系とsvc系とcmd系で設定する。到達不能時は警告記録に留め、次回に再試行する。ゲストadbdのTCP listenはinitrdパッチ(scripts/patch-android-initrd.py + ADB_TCP=1)がdefault.propに追記して開ける。upするだけでホームと日本時間とネットとストア付きで起動する。無効化はANDROID_PROVISION=0とする。実行中タスクがある環境は避けて実行する。

仕組みはentrypointがdata.img不在時に空作成し、mkfs.ext4でシード同梱なし、-driveでvirtio接続、DATA=vdaを-appendに付与する。initrdのmount_dataが処理する。設定内容はADB投入で入れる。確認記録の2026-10-17では空data起動でADB device化・eth0自動取得・Home Launcher固定・日本時間・F-Droid導入を確認した。

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

QEMUのuser-netでゲスト5555番をコンテナのANDROID_ADB_PORT既定5555番に転送する。ANDROID_ADB_FORWARD=1既定とし、無効化は0とする。hostfwdは0.0.0.0 bind必須(127.0.0.1ではコンテナloのみでbackend不通)。backendはandroid:5555固定、and始まり:5555動的で届く。動的払い出しはホスト側5570番台も払い出す。開発者の手動adb用とする。ゲストadbdのTCP listenはinitrdパッチ+ADB_TCP=1で開ける。`adb connect` は到達不能でもrc=0を返す実測があるため出力判定(connected to)で行う。

### 寝たら起こす

抑止が間に合わない場合に備え、エージェントは黒画面検出時に公式手順準拠のESCとMenu 1秒超ホールドと中央タップを送る。TaskSession._capture_screenshotがvm_idのandroidとand始まりで切替、実送信はVNCClient.wake_androidとする。実機の深いスリープ3例では復帰しなかった。設定の節が本命で、本フォールバックはキーガード段階向けの最善努力と位置づける。

手動で緩和したい場合のLive起動のため再起動ごと再設定は次の通りとする。

1. SettingsからDisplay、Sleepを最長30分に
2. SettingsからSystem、Developer options、Stay awakeをONに。開発者向け項目が無ければAbout tabletのBuild numberを7回タップ

参考はhttps://www.android-x86.org/documentation/wake_machine.htmlとする。

### 背景記録

2026-10-12からの観測事実として固定androidを約23時間放置したところVNCが全面真っ黒になった。Linux VM用のShiftでは復帰しない。公式docによれば復帰キーはESCとMenuと矢印の1秒超ホールドのみとする。放置時間の計測はホストスリープで水増しされる点に注意する。プロセス年齢は覚醒時間のみ積算し、psのetimeと壁時刻差で判別できる。

2026-10-13の起動経路知見としてカーネル直起動の成否は条件依存で揺れる。ANDROID_MEMORY=4096では3例連続でISO検出後に進行しなかった。VNC真っ黒でadbd到達なしとする。2048ではセットアップウィザード到達を確認し、2から3分とする。既定は2GBとした。直起動が黒画面のままの場合は、到着がスリープ後既定タイムアウト約1から2分の可能性を先に疑うこと。androidboot.hardware=android_x86付与は起動ロゴ停止のため禁止で従来通りとする。既定-appendはisolinux.cfgと完全同一に保つこと。

## 観測

- 閲覧はfrontendの3001番の本リポジトリ上書き設定から環境タブでAndroid選択、またはnoVNC直結6070番、VNC直結5590番とする
- ログはdocker logs ai-desktop-agent-backend-1 | grep -i provisionとする。投入の開始と完了と未完が出る。対象がいない間は静かである
