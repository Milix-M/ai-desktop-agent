# Clef 決定モデルの利用可否検討

- 出典: https://chot-inc.com/columns/cloudflare-clef-decision-model
  （元出典は Cloudflare Blog / Docs / Hugging Face。数値は提供元申告であり第三者による再現なし）
- 状態: 検討のみ。未実装（`docs/architecture.md` の「意思決定層」節が組込計画の正本）

## Clef とは

- Cloudflare が公開した**決定モデル (decision model)**。
  Workers AI ホスト（`@cf/cloudflare/clef`、`clef-flash`）＋重み公開。
- 位置づけ: **判断は Clef、文章生成・実行は LLM**。生成モデルの代替ではない。
- 自由文を生成せず、入力 `state`＋型付き `questions` に対し許可選択肢ごとの
  **確率付き構造化データ**を返す。出力形式の揺らぎへの対策が不要。
- ベースモデルを凍結し、プレフィルの1パスだけで選択肢を並列採点するため高速。
- カテゴリ追加で再学習不要。TypeSafe AI `Jev` と同じ System One APIとの互換性がある。
- Brier損失でキャリブレーションを調整済みであり、確率の閾値設計を前提にする
  （例：`0.9以上自動実行 / 0.6–0.9保留 / 未満エスカレーション`）。

## API型（1リクエスト最大64質問）

- `noul`: はい/いいえ。「はい」確率を返す。
- `choice`: 定義済み選択肢から1つ。選択肢ごと確率＋確信度。
- `score`: 順序付きルーブリックに採点し確率重み付きスコア。

## 公称スペック（提供元申告）

| 項目 | Clef | Clef-flash |
|---|---|---|
| パラメータ | 27B | 9B |
| コンテキスト | 65,536 | 65,536 |
| 単価（入力100万トークン） | $0.24 | $0.09 |
| レイテンシ中央値 | 209.3ms | 38.8ms |
| レイテンシ p95 | 238.6ms | 122.4ms |

- どちらも画像入力に対応（1リクエスト最大4枚）。
- ベンチマークでは、Jev Decision Index 由来の10件中7件で Clef 系が最高と主張している。
  一方、タスクによって優劣は入れ替わる
  （例: `When2Call` は Jev 勝ち、`CLINC150+OOS` は flash 大敗）。
  まず flash で評価するのが推奨されている。

## 制約（見落とし注意）

- 画像は PNG/JPEG/WebP 埋め込みのみ、リモート URL 不可。
  最大4枚、各4MiB・1600万画素以内、デコード後合計8MiB、リクエスト本文最大13MiB。
- `state` が長い場合はトークン上限で前方優先・後方切捨てになるため、重要情報は前方に置く。
- 質問 ID は英数＋`_. -`、最大100文字。

## 本リポジトリへの適用可否

- 現状: `agent/llm/base.py` の `LLMProvider` が
  `decide_next_action` / `verify_result` / `recover_from_error` を提供し、
  `server/session.py` が EXECUTING→VERIFYING→RECOVERING を駆動する。
- 結論: **座標・アクション JSON 生成は LLM 必須で Clef は代替不可**。
  一方、**検証・回復の判断部分は適合度が高い**（ハイブリッド構成が妥当）。

| # | 組込点 | 置換対象 | 方式 |
|---|---|---|---|
| 1 | 回復戦略の選択 | `_recover_phase()` の LLM 呼出 | `choice`（選択肢＝`RecoveryStrategy`）。flash の数十msで戦略確定 |
| 2 | サブタスク達成検証 | `_verify_subtask_phase()` の画像つき LLM 検証 | `noul`（達成可否）＋`score`（完全/部分/未達）。高確信で前進・低確信で回復・中間で LLM 検証にエスカレーション |
| 3 | 評価ハーネス | なし（新設） | 永続化タスク記録を正解セット化し、Clef-flash→Clef の順で精度・分布を測定 |

## 導入手順案

1. 運用ログ（`data/tasks/`）から100–300件の正解ラベルを用意する。
2. `noul` / `choice` / `score` スキーマに翻訳し、flash で一括評価する
   （正解率＋確率分布。高確率帯の誤りが多ければ閾値運用は不可→Clef への切替や追加学習）。
3. `agent/llm/` に Workers AI REST 対応の provider を追加し、
   `decide_next_action` / `understand` / `decompose` は既存 LLM に委譲する
   デコレータ構成で、既存への影響を抑えて組み込む。
4. 自タスクでの精度×レイテンシ×単価で選定すること（提供元ベンチマークのみで決めない）。
