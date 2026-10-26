---
name: rakuten-merumaga-reserve
description: 楽天メルマガの下書き(RMS R-Mail の送信一覧)を、運用者が内容を確認して OK と言ったあとに配信予約する(送信手続き)。配信枠の空きを読む → 配信日時と配信先を運用者が決める → 送信確認の画面で止めるお試し → 承認の画面で承認 → 道具が「送信を予約する」を 1 回だけ押す → 予約受付メールで確かめる → LINE で知らせる。「メルマガを予約して」「送信手続きをして」「配信予約」「/rakuten-merumaga-reserve」で使う。下書きを作るのは /rakuten-merumaga-flow。
---

# /rakuten-merumaga-reserve — RMS の配信予約(送信手続き)

2026-10-26 新設(受付 30 番で実機確認・申請 238 で予約成功)。道具は端末 A の `900.ClaudeCode/rakuten_login/rmail_reserve/`。

## いつ使うか

- `/rakuten-merumaga-flow` で下書きができ、**運用者が内容を確認して「送信手続きをして」と言ったときだけ**。
  運用者の指示が無ければ予約しない(「手配」は下書きまで。メモリ feedback_tehai_means_draft)。

## 絶対ルール

1. **配信枠の空きを調べてから、配信日時を最終決定する**(運用者指示 10/26)。日時と配信先は運用者が選ぶ(クリック形式で聞く)。
2. **本番の予約は承認の画面で承認を受けてから**(見張り役 a_browser_watch・対象 `rmail_reserve`)。チャットで承認を求めない。
3. 調査(予約しない読み取り・お試し)は承認なしで直接実行してよい(運用者決定 10/26)。`cli.py research ...`
4. 失敗・結果不明の予約は**自動で出し直さない**。`unknown` のときは RMS の送信済一覧と予約受付メールで確かめてから。
5. RMS のログインは前回の試行から 3 分空ける(道具が待つ)。

## 手順(C:\ClaudeCode\900.ClaudeCode\rakuten_login で)

### 1. 配信枠の空きを読む(承認なし)
```
python -X utf8 -m rmail_reserve.cli research slots
```
`mail_draft_data/reserve/<日時>_slots.json` の表(○空き / △混雑 / ×受付不可 / -過去・時間外)を本文に貼って見せる。
携帯メールは 9 時〜21 時だけ予約できる。月 12 回まで(メール送信の画面の「送信回数照会」)。

### 2. 配信日時・配信先を運用者に聞く(AskUserQuestion・1 回にまとめる)
- HTML: 「無料配信対象ユーザへ送る」(週 1 回無料・同じ週の 2 回目から有料)か、店舗のリスト
- 携帯: 店舗のリスト「全件検索」(mobile の search_cond_id 2623910)など
- 店舗のリストの番号はリスト条件選択の画面(`research wizard <種類> <ID>` の記録)で確かめる

### 3. 予約の中身を書く(JSON)
```json
[
 {"mail_type": "html", "rmail_id": "26794591", "list_id": "free", "slot": "2026-10-26 15:00", "subject": "<下書きの件名>"},
 {"mail_type": "mobile", "rmail_id": "26794593", "list_id": "2623910", "slot": "2026-10-26 15:00"}
]
```
置き場の例: `900.ClaudeCode/000.tmp/reserve_items_YYYYMMDD.json`。配信日時は毎時 0 分・今から 20 分より先。

### 4. お試し(送信確認の画面で止める・承認なし)
```
python -X utf8 -m rmail_reserve.cli research dryrun --items <JSON>
```
2 通とも `ok`(照合一致)を確かめる。送信人数を運用者への報告に使う。

### 5. 本番の予約を申請 → 承認 → 実行
```
python -X utf8 -m rmail_reserve.cli enqueue-reserve <JSON> --purpose "<目的 6〜120 字>"
```
見張り役が承認の画面へ出す。`a_browser_data/watch.log` の「rmail_reserve id=N 実行終了」を裏で待ち、
`python -X utf8 -m rmail_reserve.cli show N` で結果を読む。各通 `reserved` で、受付完了の画面の写し(`reserve_done_<ID>.png`)が残る。

### 6. 予約受付メールで確かめ、LINE で知らせる
`/rakuten-merumaga-flow` の ⑥⑦ のとおり(`reserve_notice.py wait ...` → 画像 → `/line-notify` の承認 → 送信)。

## 道具が押すもの・照合するもの(実機で確かめた一本道)

メール送信 → 種類の「次へ」→ 配信先 →「この条件でリストを作成する」(「送信リストを作成中」の画面は待つ)
→ 本文選択の**タブ**「送信一覧」を**押す**(説明文の [送信一覧] や直接開くと管理画面の一覧になり手順から外れる)
→ 下書きの「プレビュー」→ テスト送信先に運用者宛(mm@)を入れて「次の画面へ(テスト送信)」
→ 送信日時設定で**「予約送信」を選び直す(既定は即時送信)**→ 枠の「○」→「次の画面へ」
→ 送信確認で照合: 送信日時・配信方法=予約送信・種類・件名・指定条件の全文・下書き番号(隠し欄)
→ 全部合えば「送信を予約する」を 1 回。

歯止め(`guard.py`): R-Mail への POST は直前に許した 1 回だけ。別の指示が混ざった送信・同じ名前の欄が 2 つの送信は止める。

## ハマりどころ

- 予約するたびに mm@ へテストメールが 1 通届く(プレビューの次へがテスト送信を兼ねる)。
- 送信人数はリストを作るたびに少し変わる(お試しと本番で数人違う)。
- 承認がすぐ届くと、直前の調査のログインから 3 分たっていないことがある(道具が待つ)。
- 「手動の対応が必要」(needs_human・manual)は一時的なことがある。何も押していないので、読むだけの調べでログインを確かめてから出し直す。

## 関連
- 下書きを作る: `/rakuten-merumaga-flow`(①〜⑤)
- 道具の中身: `rmail_reserve/reserve.py`(本番)・`probe.py`(調査)・`guard.py`(歯止め)・`tests/`
