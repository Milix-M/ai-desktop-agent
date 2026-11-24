# 定型作業を評価する

コンテナ内の4課題で、タスクの完了と保存成果物の一致を別々に検査する。初期状態と検査条件を固定する構成は[OSWorldの課題定義](https://github.com/xlang-ai/OSWorld/blob/main/evaluation_examples/README.md)を参考にした。

| 課題ID | 保存成果物の検査 |
| --- | --- |
| `text-edit` | Mousepadで編集した文章の一致 |
| `text-append` | 元の2行を保持し、指定の1行と末尾改行を追記 |
| `file-rename` | 新しい名前・内容の一致、元の名前の消失、別ファイルの保持 |
| `spreadsheet-total` | CalcのSUM式・計算結果・元のセルの保持 |

## 実行する

DockerとREADMEの開発環境が必要になる。各試行は専用ネットワーク付きの新しいコンテナで行い、CPU上限を2個分、メモリ上限を2 GiB、プロセスとスレッドの合計を1024までとする。スワップは使わない。評価条件を固定するため、`DESKTOP_*`の上限設定は使わない。決定モデルはnoneに固定する。操作前のファイル内容や名前が初期状態と一致しなければ、モデルを呼ばず準備エラーにする。

```bash
docker build -t ai-desktop-agent-desktop-eval desktop
uv run python scripts/eval_desktop.py --mode scripted --out data/evaluations/scripted-1
```

`scripted`の結果は決められたGUI操作による評価基盤の動作確認として扱う。モデルを比較する場合は、`.env`に`LLM_PROVIDER`・`LLM_MODEL`と対応するAPIキーを設定して実行する。Ollamaはキー不要となる。

```bash
uv run --env-file .env python scripts/eval_desktop.py --mode model --out data/evaluations/model-1
```

出力先は未作成のディレクトリを指定する。課題は`--cases text-edit file-rename`、繰り返しは`--repeats 3`で選ぶ。既定の上限は1試行300秒・60送信・入出力合計30000トークンで、`--max-seconds`・`--max-requests`・`--max-tokens`で変えられる。実際の上限は`report.json`の`limits_per_attempt`で確認する。費用上限はREADMEの`TASK_MAX_COST_USD`と単価設定を使う。

## 結果を読む

`report.json`にコード・課題・コンテナ画像の識別情報、モデル設定、試行ごとの結果と集計を保存する。タスク成功は、エージェントが完了し、成果物の検査も通った場合に数える。モデル呼び出しの失敗や時間切れもタスク失敗に含む。

成功率の分母は評価済み件数となる。比較するときは要求件数・評価済み件数・エラー件数も確認する。準備や検査基盤にエラーがあれば状態は `incomplete` となる。終了画面を取得できなかった場合も基盤エラーとして、成果物の検査結果と併記する。使用量には中断した試行も含み、単価と使用量が揃う場合は推定費用を表示する。

各試行のフォルダーに成果物・操作履歴・画面を保存する。途中の画面は64枚までとし、省いた枚数も記録する。出力には画面や入力内容が残るため、共有前に確認する。手動入力が必要になった試行は終了し、再開を待たない。

終了コードは全件成功が`0`、タスクまたは成果物の失敗が`1`、設定・基盤エラーが`2`、中断が`130`となる。中断時も結果を保存し、試行用のコンテナとネットワークの削除を試みる。
