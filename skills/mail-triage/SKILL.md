---
name: mail-triage
description: 期間内の受信メールを全件列挙し、除外ルール(exclude_rules.json)に一致したものを落として「まだ対応していない可能性のあるメール」だけを残す手順。キーワードで拾うのではなく引き算で絞るため、ルールに無い新しい形のメールも必ず候補に残る。900.ClaudeCode/mail-search/scripts/triage.py を使う。
trigger: 「返信が必要なメールはないか」「対応漏れ・見落としがないか」「ここ数日のメールを確認して」のように、特定のメールを探すのではなく網羅的に確認したいとき
---

<!-- SKILL_VERSION: 2026-09-08_085647 -->

# mail-triage — 受信メールのトリアージ(除外方式)

作業フォルダ: `C:\ClaudeCode\900.ClaudeCode\mail-search`

## 🔴 このスキルの存在理由(必ず読む)

**キーワードで「対応が必要そうなメール」を拾ってはいけない。**

2026-09-08、「Amazon の注文キャンセルのメールが届いていませんか？」に対して
`"Amazon キャンセル"` / `"キャンセル されました"` で検索し **「0件・見つかりません」と
誤報告した**。実際には4件届いていた。件名は「注文**キャンセルリクエスト**」
「返金が開始されました」で、本文冒頭は HTML の CSS 定義に占められており、
推測したキーワードがどこにも存在しなかった。同じ3日間で、キーワード方式で見えたのは
23件、実際の受信は **1,476件**だった。

拾う方式は、こちらの推測が外れた瞬間に**エラーもなく静かに0件**を返す。だから向きを逆にする:

```
期間内を全件列挙 → 除外ルールに当たったものを落とす → 残りが候補
```

ルールに無い新しい形のメールは「除外されない」＝**必ず候補に残る**。これが要点。

## ❌ 禁止

- `search.py` / `mail_index.py` を直接 import した**その場限りのスクリプトを書かない**。
  集計も絞り込みも `triage.py` に入っている(下記)。一時スクリプトは毎回書き方が変わり、
  結果が揺らぐ。実際に上の誤報告はそれで起きた
- 除外ルールに**差出人ドメイン単位の粗い条件を入れない**。メルカリShops も Amazon も、
  同じ no-reply アドレスから「問い合わせ」「発送依頼」「キャンセル」を送ってくる。
  件名との AND で絞ること
- 迷ったルールを入れない。**残す側が安全**(取りこぼしはゼロに、ノイズは後から削れる)

## 手順

### 1. 候補を出す

```bash
cd "C:/ClaudeCode/900.ClaudeCode/mail-search/scripts" && python triage.py --days 3
```

- `--days N`(既定3) / `--since 2026-09-01 --until 2026-09-05`
- 出力は「要対応候補」→「日付不明」→「除外内訳」→「未使用ルール」の順

### 2. 中を掘る(必要なとき)

```bash
python triage.py --days 3 --grep キャンセル --grep 返金   # 候補の中を絞る(OR)
python triage.py --days 3 --group-by sender               # 差出人別に集計
python triage.py --days 3 --group-by subject              # 件名パターン別(数字はNに正規化)
python triage.py --days 3 --show-excluded                 # 何がどのルールで落ちたか
python triage.py --days 3 --json                          # 後段処理へ渡す
```

`--grep` は**除外を適用した後**の候補に効く。順序が逆だとキーワード検索に退化する。

### 3. 報告する

Claude が判断してよいのは**ここだけ**:

- 残った候補の優先順位づけ(顧客からの問い合わせ > 取引先の依頼 > 営業メール)
- 内容の要約と、次に取るべきアクションの提示

件名だけで判断せず、`body_head` を読む。HTML メールは本文冒頭が CSS で埋まることが
あるので、その場合は件名と差出人で判断する。

### 4. ルールを育てる

候補が多すぎるときは `--group-by sender` で件数の多い塊を見つけ、**ユーザーに確認してから**
`900.ClaudeCode/mail-search/exclude_rules.json` に追記する。除外は業務判断なので勝手に決めない。

```json
{
  "id": "provider-purpose",
  "reason": "なぜ対応不要と言えるか",
  "added": "YYYY-MM-DD",
  "match": { "from_contains": "...", "subject_contains": "..." }
}
```

- `match` 内は AND、ルール同士は OR。値は小文字化した部分一致
- 使えるキー: `from_` `to_` `subject_` `body_` `folder_` `account_` × `_contains` / `_equals`
- 追加したら**必ず除外内訳を確認**する。1ルールで全体の50%以上を消すと警告が出る
- キー名を間違えたルールは起動時に「使えていないルール」として警告される。
  `tests/test_triage.py::test_production_rules_are_all_usable` でも落ちる

## /mail-search との使い分け

| 目的 | 使うもの |
|---|---|
| **特定の**相手・案件のメールや添付を探す | `/mail-search`(キーワード検索でよい) |
| **対応漏れが無いか**を網羅的に確認する | **このスキル** |

「〜のメール来てない？」は一見 mail-search だが、**「無いことを確認したい」なら
このスキル**。キーワード検索の0件は「無い」の証明にならない。

## 前提と限界

- **送信済みメールはインデックスに入っていない**。`accounts.py` の収集起点が `INBOX.sbd`
  のため構造的に対象外。よって「自分が返信済み＝対応完了」の自動判定は現状できない。
  候補に出たものが対応済みかは人が判断する(実現するには収集範囲の拡張から)
- 既読/未読は見ていない(既読でも未対応はあるため、2026-09-08ユーザー判断)
- Date ヘッダが読めないメールは期間で落とさず「日付不明」として必ず表示する
- GoQ の在庫数通知は除外済み。**別途これ専用のスクリプトを作る方針**(2026-09-08ユーザー決定)

## テスト

```bash
cd "C:/ClaudeCode/900.ClaudeCode/mail-search" && python -m pytest tests/ -q
```

`tests/test_triage.py` はルール評価(AND条件・大文字小文字・不正キー検出)と、
本番 `exclude_rules.json` の妥当性を固定している。ルールを追加したらここを通すこと。

## 関連ファイル

- `900.ClaudeCode/mail-search/scripts/triage.py` — 本体
- `900.ClaudeCode/mail-search/exclude_rules.json` — 除外ルール
- 長期記憶: `feedback_mail_triage_exclusion_not_keyword.md` / `project_mail_search_fts5_index.md`
- 関連スキル: `/mail-search`(探す用途)
