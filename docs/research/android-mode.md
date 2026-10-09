# Android操作モード（Phase 0/1 実装済み。視聴は未対応）

- 実装状態: `AdbBackend`（`vm/adb_backend.py`）＋`VmPool` の `kind=android` 動的払い出し＋
  compose `android` サービス（`android` プロファイル）まで実装済み。
  ライブ視聴（noVNC相当）は未対応のため、以下は構成案の記録として残す。
- 目的: 現行 Linux（KDE / Xfce）＋VNC 構成を温存しつつ、Android 端末操作モードを追加する。
- 非目標: iOS 対応、実機接続、Play 課金アプリの自動購入。
- 前提: `VNCClient`（汎用 VNC）＋座標グリッド＋OCR＋`VmPool` のポート払い思想は流用する。

## 選択肢の比較

| 選択肢 | Docker動作 | KVM要否 | 画面取得 | 入力注入 | GMS/Play | 備考 |
|---|---|---|---|---|---|---|
| **Redroid**（本命A・軽量・既定候補） | ◎ container-native（`--privileged`＋binder/binderfs要。QEMUなし） | 不要 | △ネイティブVNCなし。`adb exec-out screencap -p` ポーリング or `scrcpy -s host:5555`（VNC化はkasmweb等のラッパーで付加） | ◎ `adb shell input tap/swipe/text/keyevent` / scrcpy経由 | ×素体なし。後付けは再ビルド（MindTheGapps）か社区済みイメージ。x86上のARMアプリはlibndk/houdini要 | container起動のみで最速。`androidboot.redroid_{width,height,dpi,fps}` で解像度固定可。adb無認証のため `127.0.0.1:5555` 限定公開 |
| **budtmo/docker-android**（本命B・流用最大） | ◎ `--device /dev/kvm`（Ubuntuホスト限定） | 要 | ◎ VNC `:5900`＋noVNC `:6080`（`WEB_VNC=true`）同梱 | ◎ VNC（現行流用）＋外部から `adb connect` | ×無料版は `google_apis` 系でPlayなし。Play StoreはPro版の予定項目 | VNC＋noVNC＋ADBがそろう唯一の選択肢。無料版は9.0–14.0（15以降はProのみ）。pull 2.5–3GB級・起動1–3分・RAM 4GB+ で重い |
| **Android-x86/Bliss OS on QEMU**（PoC用） | ◎ 現行 `vm` コンテナのゲスト差し替えだけ | 推奨 | ◎ QEMU `-vnc` そのまま（現行流用100%） | ○ クリック→タップ、ドラッグ→スワイプ代用 | △ Bliss系はPlay同梱あり | 縦画面回転・解像度固定が面倒、マルチタッチなし |
| **Cuttlefish** | △ Docker手順あり。激重 | 要 | △ WebRTC。VNCなし | △ WebRTC入力 or adb | × AOSP素体 | フレームワーク忠実度は最高だが自動化には過剰。非推奨 |
| **Waydroid** | × ホスト直結前提 | 不要 | △ wayvnc回り | △ compositor経由 or adb | △ 手動 | compose完結の方針に合わない。除外推奨 |
| **Anbox Cloud / Genymotion SaaS** | —（外部） | 不要 | △ WebRTC | △ ADB＋API | ○ | 商用課金・外部依存・VNC非互換のため不適 |

## Redroid vs budtmo/docker-android 深掘り（本PJ視点）

| 観点 | Redroid（本命A・既定候補） | budtmo/docker-android（本命B・必要時） |
|---|---|---|
| 方式 | container-native。QEMUなしでAndroidを直接起動 | SDK emulator（QEMU）をコンテナ内蔵 |
| ホスト要件 | Linux＋`binder_linux`（kernel 6.xはbinderfsマウント要）＋`--privileged`。KVM不要。ashmemは5.18+/Android 12+で不要（memfd） | Ubuntu＋`/dev/kvm`。現行 `USE_KVM` 切替と同型で判定流用可 |
| 画面取得 | `adb exec-out screencap -p` ポーリング or scrcpy。VNC素体なし→ `AdbBackend` 新規が必要 | VNC `:5900`＋noVNC `:6080` 同梱→ `VNCClient`・視聴系を修正なしで流用 |
| 入力注入 | `adb shell input tap/swipe/text/keyevent`（タップ確実、座標系単純） | VNC（click→タップ代用）＋adb。emulator側のスキン・回転あり |
| GMS/Play | 素体なし。再ビルド（MindTheGapps）か社区済みイメージ・スクリプトで後付け。x86でARMアプリを動かす場合はlibndk/houdini要 | 無料版（9.0–14.0）は `google_apis` 系でPlayなし。15以降・Play Store・headlessはPro（スポンサー）のみ |
| サイズ・起動 | 軽量・秒級（container起動のみ。完了判定は `sys.boot_completed` ポーリング） | pull 2.5–3GB級・emulatorコールドブート1–3分・RAM 4GB+ |
| 忠実度 | 実機相当の素直なAndroid。マルチタッチ・センサー系は弱い | 公式emulator。デバイススキン・回転・仮想センサー・録画あり |
| セキュリティ | adb無認証（`127.0.0.1:5555` 限定公開必須）＋`--privileged` の隔離弱化 | KVM隔離で現行 `vm` と同等。adbはコンテナ内に閉じる運用可 |
| 結論 | 既定はRedroid（軽量・高速・KVM不要）。Play必須の実アプリ検証だけbudtmo（無料版で届かなければProか社区Play系を別途検討）の二段構え。Phase案は維持 | — |

出典: `github.com/remote-android/redroid-doc`、`github.com/budtmo/docker-android`、Docker Hubレイヤ情報（2026年10月時点）。

## 現行アーキテクチャの流用可否

- ◎ 流用可: `screenshot.py` / `overlay.py` 座標グリッド / OCR / `session.py` 状態機械 /
  noVNC視聴 / `VmPool` のポート払い思想。
  `budtmo` / Android-x86 系なら `VNCClient` も修正なしで動く
  （`click`→タップ、`drag`→スワイプとして再利用）。
- △ 要拡張:
  - `DisplayBackend` に `AdbBackend` 追加（Redroid用。
    `capture=adb exec-out screencap -p`、`tap/swipe=input tap/swipe` に写像）。
  - `actions/primitives.py` に `tap` / `long_press` / `swipe` / `back` / `home` / `wake` 等の意味層。
  - オーバーレイのカーソル十字は無効化（ホバー概念なし）、縦画面用グリッド再調整。
  - IME は英字先行、日本語はクリップボード経由（後回し）。
  - 起動完了は `sys.boot_completed` ポーリングに変更（現行 `RESTART_SETTLE_SECONDS=60` では不足）。

## 段階導入案

- Phase 0（読取専用観測）: `android-redroid` サービス追加＋`AdbBackend`（captureのみ）。
  スクショ＋OCR＋グリッド表示だけ疎通する。
- Phase 1（タップ操作）: `adb shell input tap/swipe/keyevent` を executor に接続。
  `left_click`→tap、`drag`→swipe の互換マッピングで既存 LLM プロンプトをほぼ変えずに試す。
- Phase 2（視聴＋精度向上）: scrcpy→x11vnc 系で noVNC 相当視聴。
  `region_select` 拡大・`long_press`・`back/home` 追加。
- Phase 3（本格・要判断）: `budtmo/docker-android` 系を別プロファイルで追加。
  VNC現行流用＋Play系 image で実アプリ検証。既定は Redroid、必要時だけ emulator の二段構え。

## リスク

- 起動の重さ: emulator/Cuttlefish は常時起動に向かない。snapshot・プール・ディスク管理が必須。
- Google Play の有無: Playなしだと検証対象アプリの入手自体が課題（sideload 運用・署名・課金不可）。
- 操作精度: 小ボタン・ジェスチャー・WebView・スケルトンの誤認識。`wait_for_still` 等の再調整が必要。
- ホスト要件分岐: KVM / binder / `--privileged` が環境で異なる。
  現行の `USE_KVM` のみの切替から `ANDROID_BACKEND=redroid|emulator|x86` 切替＋起動可否チェックが必要。
- セキュリティ: adb ポートの公開範囲（localhost限定）、`--privileged` の隔離弱化、Play ログイン情報の扱い。

## 受入基準案

- 設定アプリの起動→Wi-Fi表示→メモ帳入力の3タスクを無人完走すること。
