# Clef決定モデルの検討

Clefとの連携は実装済みで、既定では無効となる。`DECISION_PROVIDER` でWorkers AIの `clef` とOpenRouter Decisions APIの `clef-openrouter` を選ぶ。OpenRouter経由ではLLM本体と別のAPIキーを使える。[意思決定層の設計](../architecture.md#意思決定層)を参照する。

応答の型と確率分布を検査し、不正なら実行上限の範囲でLLMに切り替える。`decision_calls` には失敗・中断した試行も含む。

## Clefの概要

ClefはCloudflareが公開した決定モデルで、`state` と型付きの `questions` を受け取り、選択肢ごとの確率を構造化データで返す。TypeSafe AIのJevと同じSystem One APIに対応し、確率のしきい値を使って判断する。このアプリでは達成確認と回復戦略の選択に使い、計画・座標・操作内容の生成はLLMが担う。

## APIの型

1要求最大64質問とする。

- noulは肯定否定判定で肯定確率を返す
- choiceは定義済み選択肢から1つを選び、選択肢ごと確率と確信度を返す
- scoreは順序付き基準表に採点し確率重み付き点数を返す

## 公称値

Cloudflareの資料に基づく検討時の公称値で、第三者による再現結果は含まない。

| 項目 | Clef | Clef-flash |
|---|---|---|
| パラメータ数 | 27B | 9B |
| 文脈長 | 65,536 | 65,536 |
| 単価の入力100万トークン | $0.24 | $0.09 |
| 遅延中央値 | 209.3ms | 38.8ms |
| 遅延p95 | 238.6ms | 122.4ms |

どちらも画像入力に対応する。提供元の評価ではJev Decision Indexに由来する10課題のうち7課題でClef系が最高となった。When2CallではJevが上回り、CLINC150とOOSではClef-flashが大差で負けた。提供元はClef-flashから評価することを勧めている。

## 制約

- 画像はPNGとJPEGとWebP埋め込みのみとし、外部URL不可とする。最大4枚、各4MiBと1600万画素以内、復号後合計8MiB、要求本文最大13MiBとする
- stateが長い場合はトークン上限で前方優先と後方切捨てになるため、重要情報は前方に置く
- 質問IDは英数と下線と空白とドットとハイフンのみ、最大100文字とする

## 適用可否

| 番号 | 組込点 | 置換対象 | 方式 |
|---|---|---|---|
| 1 | 回復戦略の選択 | `_recover_phase` のLLM呼び出し | `choice` で `RecoveryStrategy` を選ぶ |
| 2 | 達成検証 | `_verify_subtask_phase` の画像付きLLM検証 | `noul` で達成の可否を、`score` で達成度を判断する。確信度が高ければ前進し、低ければ回復する。中間はLLMで再確認する |
| 3 | 評価手順 | scripts/eval_clef.py | 保存済みタスク記録に目視ラベルを付け、Clef-flashからClefの順で精度と分布を測る |

## 導入前に評価する

`CLOUDFLARE_ACCOUNT_ID`と`CLOUDFLARE_API_TOKEN`を`.env`に設定する。通信なしの確認には`--provider mock`を使う。

履歴から100〜300件を目安に正解表を用意し、目視ラベルを`labels.jsonl`へ保存する。次の`task-001`は履歴のIDに置き換える。番号は完了申告または失敗操作を0から数える。

```jsonl
{"id":"task-001:verify:0","label":false}
{"id":"task-001:recover:0","label":"wait_and_retry"}
```

検証ラベルはJSONの`true`か`false`とする。回復ラベルは`wait_and_retry`、`alternative_approach`、`replan_subtask`、`give_up`から選ぶ。不正なラベルやIDの重複は評価を止める。

```bash
uv run --env-file .env python scripts/eval_clef.py --tasks-dir data/tasks --provider clef --labels labels.jsonl --out data/clef-evaluation.json
```

目視ラベルがないケースは推定として数え、推定が残る評価は`needs-review`とする。正解率は退避したケースを除いて計算するため、総件数と退避件数も確認する。達成・未達のどちらでも高確信の誤りがあれば`not-usable`となる。

`--llm-input-price`と`--llm-output-price`は入力・出力100万トークン当たりのUSD単価で、推定費用に反映する。基準を満たしても、自課題の精度・遅延・費用を比較してから有効にする。
