# docs/puml — PlantUML 図の管理

アーキテクチャ図の正本はここにある `.puml` ファイルである。
PNGは生成物であり、`docs/architecture.md` と `README.md` から参照する。

| ファイル | 内容 | 対応する本文 |
|---|---|---|
| `system-overview.puml` | システム構成（Dockerホスト全体） | `architecture.md` システム概要 |
| `agent-state.puml` | エージェント状態機械（`agent/state.py:29` の `_VALID_TRANSITIONS` が正本） | `architecture.md` エージェント状態機械と実行フロー |
| `task-sequence.puml` | タスク実行シーケンス（`server/session.py:run()`） | 同上 TaskSessionの実行フロー |
| `vm-lifecycle.puml` | 動的VM・コンテナライフサイクル（`server/vm_pool.py` の `VmPool`） | `architecture.md` VM操作アーキテクチャ |

## 再生成

```bash
./docs/render-puml.sh
```

- 日本語フォント（Noto Sans JP）を `plantuml/plantuml` コンテナに取り込んで描画する。
  各 `.puml` の `skinparam defaultFontName "Noto Sans JP"` を消さないこと。
- フォント候補は `render-puml.sh` の `FONT_CANDIDATES` を参照。
  WSLでは `/mnt/c/Windows/Fonts/NotoSansJP-VF.ttf` を使う。

## 運用ルール

- 図を変えたら `.puml` と `.png` をセットで更新する（`.png` だけの手修正はしない）。
- 状態遷移を変えたら `agent/state.py` の `_VALID_TRANSITIONS` と `agent/loop.py` を先に直し、
  `agent-state.puml` をそれに合わせる。図だけを先に変えないこと。
- シーケンスを変えたら `server/session.py`・`server/vm_pool.py`・`server/app.py` の順で裏取りする。
