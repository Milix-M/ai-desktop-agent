# Clef決定モデルの検討

- 出典はchot-incの解説記事とする。元出典はCloudflare BlogとDocsとHugging Faceとする。数値は提供元申告であり第三者再現なしとする
- 状態は実装済みとする。既定は無効であり、DECISION_PROVIDERで切り替える。組込計画の基準はdocs/architecture.mdの意思決定層の節とする。提供元はWorkers AI直結（clef）とOpenRouter Decisions API経由（clef-openrouter）の2系統とし、後者のみLLM本体と別のAPIキーを使える

応答の型と確率分布を検査し、不正な場合は実行制限の範囲でLLMに退避する。`decision_calls`には失敗・中断した試行も含む。

## Clefの概要

Cloudflareが公開した決定モデルである。Workers AI配置のcf/cloudflare/clefとclef-flash、重み公開を含む。位置づけは判断はClef、文章生成と実行はLLMとする。生成モデルの置換ではない。自由文を生成せず、入力stateと型付きquestionsに対し許可選択肢ごとの確率付き構造化データを返す。出力形式の揺らぎ対策は不要である。ベースモデルを凍結し、プレフィルの1回だけで選択肢を並列採点するため高速である。分類追加で再学習は不要とし、TypeSafe AIのJevと同じSystem One APIとの互換性がある。Brier損失で校正調整済みであり、確率の閾値設計を前提にする。例として0.9以上自動実行、0.6から0.9保留、未満上申とする。

## APIの型

1要求最大64質問とする。

- noulは肯定否定判定で肯定確率を返す
- choiceは定義済み選択肢から1つを選び、選択肢ごと確率と確信度を返す
- scoreは順序付き基準表に採点し確率重み付き点数を返す

## 公称値

提供元申告とする。

| 項目 | Clef | Clef-flash |
|---|---|---|
| 引数規模 | 27B | 9B |
| 文脈長 | 65,536 | 65,536 |
| 単価の入力100万トークン | $0.24 | $0.09 |
| 遅延中央値 | 209.3ms | 38.8ms |
| 遅延p95 | 238.6ms | 122.4ms |

どちらも画像入力に対応し、1要求最大4枚とする。評価ではJev Decision Index由来の10件中7件でClef系が最高と主張している。一方、課題により優劣は入れ替わる。例としてWhen2CallはJev勝ち、CLINC150とOOSはflash大敗とする。まずflashで評価するのが推奨されている。

## 制約

- 画像はPNGとJPEGとWebP埋め込みのみとし、外部URL不可とする。最大4枚、各4MiBと1600万画素以内、復号後合計8MiB、要求本文最大13MiBとする
- stateが長い場合はトークン上限で前方優先と後方切捨てになるため、重要情報は前方に置く
- 質問IDは英数と下線と空白とドットとハイフンのみ、最大100文字とする

## 適用可否

現状はagent/llm/base.pyのLLMProviderがdecide_next_actionとverify_resultとrecover_from_errorを提供し、server/session.pyがEXECUTINGからVERIFYINGを経てRECOVERINGへ駆動する。結論として座標と動作JSON生成はLLM必須でClefは置換不可とする。一方、検証と回復の判断部分は適合度が高い。混合構成が妥当である。

| 番号 | 組込点 | 置換対象 | 方式 |
|---|---|---|---|
| 1 | 回復戦略の選択 | _recover_phaseのLLM呼出 | choiceでRecoveryStrategyから選ぶ。flashの数十msで戦略確定する |
| 2 | 達成検証 | _verify_subtask_phaseの画像付きLLM検証 | noulの達成可否とscoreの完全と部分と未達を使う。高確信で前進し、低確信で回復し、中間でLLM検証へ回す |
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
