# Android実行環境

x86_64ホストのKVMを使い、QEMU上でAndroid-x86を動かす。操作画面ではクリックがタップ、ドラッグがスワイプになる。画面配信には認証が必要となる。

## ISOを取得する

Android-x86のISO（約1 GB）を `android/android-x86_64.iso` に置く。次は9.0-r2を取得する例となる。ここからのコマンドはリポジトリのルートで実行する。

```bash
curl -L -o android/android-x86_64.iso \
  https://downloads.sourceforge.net/project/android-x86/Release%209.0/android-x86_64-9.0-r2.iso
printf '%s  %s\n' f7eb8fc56f29ad5432335dc054183acf086c539f3990f0b6e9ff58bd6df4604e android/android-x86_64.iso | sha256sum -c -
```

## 起動用ファイルを取り出す

ISOから `android/kernel` と `android/initrd.img` を取り出す。スクリプトは起動用ファイルの抽出に加え、自動初期設定に必要なADBを有効にする。ISOと抽出したファイルはGitの管理対象外となる。

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

## 起動する

[アプリの初期設定](../README.md#起動する)を済ませ、`.env` の `USE_KVM` が `false` なら `auto` に戻す。Androidは通常の起動対象に含まれないため、固定の `android` サービスを追加で起動する。固定環境では事前に用意したデータディスクを使う。

```bash
./scripts/up.sh
docker compose --profile kvm build --pull \
  --build-arg "PACKAGE_REFRESH=$(date -u +%F)" android
docker compose --profile kvm up -d android
```

[操作画面](http://localhost:3000)にログインし、Androidのタブを選ぶ。動的環境を追加するには、[QEMU VMの起動条件](../README.md#起動する)を満たした状態で `./scripts/up.sh` を実行してから［+ Android追加］を選ぶ。APIでは `POST /vms` に `kind: android` を指定する。

## データを保存する

カーネルから起動する構成では、ext4イメージをAndroidの `/data` として使い、再起動後も設定を保持する。固定環境のホスト側イメージは `android/android-data.img` となる。手動で作る場合、`./scripts/mk-android-data.sh` の引数でサイズをMB単位で指定できる。既定は1024 MB。動的環境のイメージはコンテナ内に自動作成する。ISOからのLive起動では `/data` が一時領域となり、再起動すると設定が消える。

## 初期設定を確認する

バックエンドは起動したAndroidをADBで検出し、ホームアプリ、タイムゾーン（`Asia/Tokyo`）、スリープ設定を適用する。通信には既定でEthernetのDHCPを使う。ADBに接続できない場合は次の巡回で再試行する。タスク開始や環境の停止・起動・削除があると設定を中断し、未完の設定を改めて試す。自動設定を無効にするには `ANDROID_PROVISION=0` を指定する。

旧版で初期設定を済ませた端末では、［設定］→［システム］→［日付と時刻］でタイムゾーンの自動設定をオフにし、日本（東京）を選ぶ。

設定記録は既定で `data/android-provision.json` に保存する。固定ディスクはUUIDで識別し、ディスクを使わない構成と動的環境ではコンテナIDを使う。識別子が変わると再設定する。設定記録の破損を検出すると、元のファイルを残して設定を止める。バックアップから記録を復元する。

## F-Droidを入れる

初期設定の前に `./scripts/fetch-apks.sh` で `android/apks/F-Droid.apk` を取得する。バックエンドが初期設定時にAPKをADBでインストールする。パッケージ一覧の同期にはAndroid側のネット接続が必要となる。

## 起動や画面表示を確認する

- 起動や初期設定が進まない場合は、操作画面と `docker compose logs backend` で、環境の状態と初期設定の開始・完了・中断・失敗を確認する。
- `ANDROID_VIRT_WIFI=1` でVirtWifiを使う場合は、Androidの画面で接続する。2026年10月17日の検証では、既定のEthernetでDHCP疎通を確認し、VirtWifi有効時にはIPv4に接続できずADBもオフラインになった。
- 黒画面を検出すると復帰を試みる。深いスリープからは復帰できない場合がある。手動で対処するには［Settings］から［Display］→［Sleep］を最長にし、［System］→［Developer options］で［Stay awake］を有効にする。［Developer options］が見つからなければ、［About tablet］で［Build number］を7回タップする。
- 固定環境と手動で作成したコンテナに同じComposeサービスのラベルを付けると、固定環境の起動・停止・削除が両方に及ぶ。手動コンテナは別のサービス名で管理する。

技術的な構成は[実行環境の設計](../docs/architecture.md#vm操作)を参照する。
