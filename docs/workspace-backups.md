# デスクトップのホームを保存・復元する

コンテナ環境の `/home/agent` を保存する。対象は通常ファイル、ディレクトリ、所有者、アクセス権、リンクとなる。ホーム外のファイル、リンク先の外部ファイル、アプリで保存していない作業、QEMU・Androidのディスクは対象外となる。

## 更新時の保存

アプリ内の作業を保存し、`./scripts/up.sh` で更新する。固定desktopを停止してホームをバックアップし、旧コンテナから専用ボリュームへの移行を照合してから再作成する。失敗時は元のコンテナの再開を試みる。直接の `docker compose up` はこの処理を通らない。

固定desktopと新しく追加するコンテナ環境は、環境ごとのボリュームにホームを保存する。旧版で追加した動的環境は自動移行しない。動的環境を削除するとホーム用ボリュームも消えるため、必要なファイルは先に保存する。`docker compose down -v` でもボリュームを削除する。

バックアップは `data/backups/プロジェクト名/` にtarとJSONの組で保存し、ファイルの権限は0600となる。バックアップは自動削除しない。ホストの故障に備えて両方を別の保存先にコピーする。秘密情報を含む場合があるため、コピー先のアクセスを制限する。

1回の上限は既定で20 GiB・200000項目とする。`.env` の `WORKSPACE_BACKUP_MAX_BYTES` と `WORKSPACE_BACKUP_MAX_FILES` に正数を指定して変更できる。上限超過と保存先の容量不足では、元のコンテナを置き換えず更新を止める。

## 手動で保存する

対象環境のタスクを止め、アプリ内の作業を保存してから実行する。`desktop` は固定環境のIDで、動的コンテナはその環境のIDに置き換える。稼働中の環境は保存中だけ一時停止する。

```bash
docker compose run --rm -T --no-deps --entrypoint /app/.venv/bin/python backend \
  -m ai_desktop_agent.server.workspace_migration backup desktop
```

成功するとバックアップIDを表示する。tarとJSONを一組で保管する。

保存後の再開に失敗した場合も、表示された「保存済みバックアップ」のIDとtar・JSONを保管する。一時停止したままのコンテナは名前を指定して `docker unpause` で再開する。

## 固定desktopを復元する

復元するとホームの現在の内容が置き換わる。対象は `./scripts/up.sh` でホームを移行済みの固定desktopとなる。`.env` の `USE_KVM=false` を確認し、タスクを止めてアプリ内の作業を保存する。次のコマンドで、復元前のバックアップを作る。

```bash
docker compose --profile container stop backend desktop && \
docker compose run --rm -T --no-deps --entrypoint /app/.venv/bin/python backend \
  -m ai_desktop_agent.server.workspace_migration backup desktop
```

バックアップIDが表示されたことを確認してから、次を実行する。`BACKUP_ID` は戻したい時点のIDに置き換える。コンテナだけを削除してボリュームを未使用にし、検証済みのバックアップを復元する。復元処理でも置き換え前のホームを保存する。

```bash
docker compose --profile container rm -f desktop && \
docker compose run --rm -T --no-deps --entrypoint /app/.venv/bin/python backend \
  -m ai_desktop_agent.server.workspace_migration restore BACKUP_ID && \
./scripts/up.sh
```

復元に失敗した場合は、保存したtar・JSONとホーム用ボリュームを削除せず、エラーを確認する。使用中のボリューム、所属の異なる保存先、破損したバックアップには復元しない。
