---
name: mail-ignore-check
description: 受信メール台帳の中から、今後は候補に出さなくてよい(除外ルールに加えるべき)広告・自動配信を探して提案し、運用者が承認したものだけを除外ルールへ反映する専用スキル。「除外するべきものはないか」「広告が多い」「ブラックリストの検討」「/mic」「/mail-ignore-check」で使う。MCの後に回す。
---

<!-- SKILL_VERSION: 2026-11-11_090000 -->

# mail-ignore-check(/mic)— 台帳から「今後除外するもの」を探す

作業フォルダ: `C:\ClaudeCode\900.ClaudeCode\mail-search\scripts`
別名: **`/mic`**(同じ手順。`.claude/skills/mic/SKILL.md` がこのファイルを指す)

台帳に出てきたメールのうち、**毎回候補に出てくる広告・自動配信・営業**を見つけ、除外ルール(`exclude_rules.json`)へ
加える提案を出す。**提案までは自動、反映は運用者が承認した提案だけ。** 台帳の「完了/不要」の記録は、運用者の指示があるときだけ変える。

## 手順

1. **対象を決める**(聞かずに決めて 1 行で宣言する)
   - 既定は「未対応」すべて。依頼が「台帳全体」「今日の分」「#430〜」なら状態を問わず番号で絞る:
     ```bash
     python -c "import sqlite3,inbox_ledger as L; c=sqlite3.connect(str(L.LEDGER_PATH)); print(*[r[0] for r in c.execute(\"select no from items where first_seen>=? order by no\",('2026-11-10',))])"
     ```
     (`first_seen` の日付を直す。出た番号を 2 の `--nos` に渡す)
2. **提案を出す(何も書かない)**
   ```bash
   python C:\ClaudeCode\900.ClaudeCode\mail-search\scripts\blacklist_propose.py propose [--nos 番号...]
   ```
   判定は `提案` / `要確認` / `提案しない`。規則案ごとに、過去の全受信へ当てた結果(当たる通数・保護で救われる通数・依頼らしい通数)が出る。
3. **自分でも一件ずつ見て補う**(ツールが拾わない広告がある)
   - 台帳の件名・差出人(`from_header`)・理由欄を読み、販促・セミナー・会員向けイベント・営業提案・ポイント進呈・規約更新を拾う
   - 過去履歴を確認する: 同じ差出人の他の台帳項目(完了済みの重要通知がないか)
     ```bash
     python -c "import sqlite3,inbox_ledger as L; c=sqlite3.connect(str(L.LEDGER_PATH)); print(c.execute(\"select no,status,subject from items where lower(from_header) like ? order by no\",('%差出人の一部%',)).fetchall())"
     ```
   - ツールの提案にない分は、規則案を自分で作る(下の「規則の書き方」)
4. **運用者に表で見せて、承認を待つ**(提案番号・差出人・件名・規則案・理由・当たる通数)。`要確認` と、迷って入れなかったものも理由つきで見せる。末行に `【対応待ち】`
5. **承認された分だけ反映する**
   - ツールの提案: `python blacklist_propose.py apply P1 P2 …`(ルール追加+台帳を不要に。`.bak_日時` を残す)
   - 自分で作った規則: `exclude_rules.json` の `rules` へ追記。**書き込み前に `.bak_YYYYMMDD_HHMMSS` を残し、元の改行を保つ**(バイト列で読み書き。整形し直さない)。`id` は `ad-YYYYMMDD-NN`
6. **効果を確かめる**: `python triage.py --days 2` を実行し、追加した規則が想定の件数だけ当たっているか(多すぎないか)を見て報告する。

## 規則の書き方(原則・exclude_rules.json の note と同じ)

- 『対応不要と断定できる』ものだけ。**迷ったら入れない(残す側が安全)**
- **共有サービス(Amazon・メルカリ・Yahoo!・楽天・Google 等)は差出人だけで切らない。** `subject_contains` と AND にする
  (例: `store-news@amazon.co.jp` は出品者向け重要通知も送るので、件名「おすすめの商品」と AND)
- 配信専用のサブドメイン(`mc.`・`mail.`・`news@` など)は差出人単位でよい
- **官公庁・学校・自治体・公益団体(`.go.jp` `.ac.jp` `.ed.jp` `.lg.jp` `.or.jp`)は保護リストが先に効く。** 配信専用のものを除外するときは、
  除外ルールに加えて、**`protect` の該当項目の `unless` に同じ条件を足す**(足さないと除外されない)
- 取引先・お客様・店からの連絡(問い合わせの再連絡・返信・注文・請求・入金・発送・受賞や障害の通知)は除外しない。
  当方が返信した履歴のある相手も除外しない
- 契約サービスの定期報告(SEO 順位報告など)・システムの規約更新・メンテナンス案内は、広告と言い切れないので `要確認` に回す

## 守ること

- **承認前に `exclude_rules.json` を書かない。** 提案の段階では何も変わらない
- 台帳の完了・不要は運用者の指示があるときだけ(広告を不要にするかも、提案に添えて聞く)
- コミットや `/g-ul` は指示があるときだけ(除外ルールは `900.ClaudeCode/mail-search/` にあり、`.claude` のミラー対象ではない)
- テスト: `python -m pytest C:\ClaudeCode\900.ClaudeCode\mail-search\tests\test_blacklist_propose.py -q`(ツールを直したときだけ)
- 置き場はこの 1 か所だけ。他の PC へは `/g-ul` → `/g-dl`

## 関連

- 提案ツールの詳細・判定表: `mail-blacklist` スキル(同じ `blacklist_propose.py` を使う。本スキルはその上に「全件を自分でも見る・保護リストの unless・効果確認」を足した専用の入口)
- 除外の仕組み全般: `mail-triage` スキル
- 経緯: 2026-11-10 の MC 後、運用者が「広告が多い。除外リストを検討して」と依頼 → 6 件を追加(ad-20261110-01〜06)。この手作業を型にした
