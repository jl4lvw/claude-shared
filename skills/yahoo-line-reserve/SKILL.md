---
name: yahoo-line-reserve
description: Yahoo!店 LINE(LINE 公式アカウント「制服のフジ Yahoo!店」)の配信予約を手配するスキル。担当者が LINE「楽天メルマガ配信」で「【Yahoo! LINE】作成しました」と依頼した下書きを LINE Official Account Manager で確かめ、カードのリンク・在庫・「楽天」の文字の有無を検査し、承認の画面で承認を受けてから端末 A の自動操作で「配信予約」を押し、予約になったことを確かめて LINE で知らせる。「Yahoo! LINE」「ヤフーのLINE」「Yahoo!店のLINE予約」「/yahoo-line-reserve」で使う。
---

# /yahoo-line-reserve — Yahoo!店 LINE の配信予約(依頼の確認 → 検査 → 承認 → 予約 → 確認 → 知らせ)

2026-10-24 運用者決定で作成。**LINE で完了を知らせたところまでで完了**。楽天の `/rakuten-line-reserve` と同じ道具(`900.ClaudeCode/lineoa_reserve`)を使い、違うのは **アカウント・カードの検査・「楽天」の禁止** だけ。

```
① 依頼を読む       LINE「楽天メルマガ配信」の「【○○配信希望】の【Yahoo! LINE】作成しました」(090 の受信記録)
② 下書きを読む     LINE Official Account Manager(@718yuycx)の「下書き」一覧(読むだけ)→ ID・配信日時・配信先・本文の先頭
②' カードの検査    lineoa_reserve cards → yahoo_check(リンク・商品名・在庫・「楽天」の文字)
③ 照合だけ実行     lineoa_reserve check --account @718yuycx(何も押さない)
④ 申請             lineoa_reserve enqueue --account @718yuycx → 見張り役が承認の画面で承認を求める
⑤ 予約(自動)      承認されたら見張り役が run → 照合 →「配信予約」→ 確認の小窓 →「配信」→ 詳細画面で「配信予約中」
⑥ 知らせ           lineoa_reserve.report → /line-notify(承認の画面で承認を受けてから「楽天メルマガ配信」へ)
```

## 🚨 絶対ルール

1. **Yahoo! の中で楽天の話を出すのは絶対禁止**(2026-10-24 運用者決定)。カードの文字・吹き出しの文・リンク先のページに「楽天」があれば**エラー**として止め、担当者に伝える。リンク先の商品ページに「楽天」があれば、その商品の Yahoo! の商品名・説明文を直す相談をする(直すのは承認の画面の承認後。道具は `023.商品マスタDB/tools/yahoo_rakuten_cleanup.py` と承認の入口 `yahoo_text_fix`)。
2. 押すのは「配信予約」とその確認の小窓の「配信」だけ。今すぐ配信・テスト配信・下書き保存・削除はしない(道具が通信ごと止める)。
3. 承認は承認の画面だけ。Claude が Chrome で「配信予約」を押すことはしない。Chrome で編集画面を開いたままにしない(「下書き保存」を押すと予約が下書きに戻る)。
4. 下書きの中身は直さない。照合が合わない・品切れ・リンク切れ・「楽天」があるときは止めて報告し、続けるかをユーザーに聞く(一部品切れは報告して続行を聞く)。
5. ログインは運用者だけ(専用ブラウザ `a_browser_data\.profile_lineoa` は楽天と共通。切れていたら `python -m lineoa_reserve.cli login`)。失敗は自動で出し直さない。

## 手順

### ① 依頼を読む
090 の受信記録を**読み取り専用**で開き、「楽天メルマガ配信」グループの直近の【Yahoo! LINE】の依頼を探す(`090.LINE業務指示エージェント/data/line_agent.db` の `line_messages`。グループ ID は会話に書かない)。配信希望の日時を控える。

### ② 下書きを読む(Claude in Chrome・読むだけ)
`https://manager.line.biz/account/@718yuycx/broadcast` →「下書き」タブ → 依頼の時刻ごろに更新された行。詳細画面 `…/broadcast/<ID>` で配信日時・ターゲット、一覧の上で友だち数と今月の無料メッセージ数(スタンダード 30,000 通)を控える。
- 本文の先頭(text_head)は、編集画面の「お知らせの文」(カードの altText。例「＼新商品／護衛艦ちょうかい＆水中処分隊Tシャツ・…」)から**絵文字を含まない 10 文字以上**。

### ②' カードの検査(必須)
```
cd C:\ClaudeCode\900.ClaudeCode
python -m lineoa_reserve.cli cards <ID> --account @718yuycx --out "000.tmp\lineoa_yahoo_<日付>"
python -m lineoa_reserve.yahoo_check "000.tmp\lineoa_yahoo_<日付>\cards.json" --json "000.tmp\lineoa_yahoo_<日付>\cards_check.json"
```
- 商品(`store.shopping.yahoo.co.jp/seifukunofuji/<コード>.html`): 023 の控え(`/api/yahoo/snapshot/by-code/<コード>`)で商品名、023 の在庫 API(`/api/yahoo/stock/<SKU>`・Yahoo! からその場で読む)で種類ごとの在庫。SKU は 023 の `products` で「<コード大文字>-parent」(色・サイズのある商品)か「<コード大文字>」(単品)。
- それ以外のリンク: 開けるか・ページ名。
- どちらも、カードの文字とリンク先のページに「楽天」が無いか。
- 終了コード: 0=問題なし / 1=警告(一部品切れ・非公開など)/ 2=エラー(全商品が販売不可・「楽天」がある)。2 は必ず止める。
- 下書き全体(吹き出しの文も含む)に「楽天」が無いかも確かめる(`cards` が保存する配信データ、または調査用に `broadcast.json` を出して数える)。
- 実例(10/25 配信): 商品 8 点・販売可能 8・「楽天」0・一部品切れ 1(G1893 ネックウォーマーのグレー大人用・子ども用)。運用者が「そのまま載せる」と判断。

### ③ 照合だけ / ④ 申請 / ⑤ 結果
```
python -m lineoa_reserve.cli check <ID> --account @718yuycx --send-at "YYYY-MM-DD HH:MM" --text-head "<本文の先頭>"
python -m lineoa_reserve.cli enqueue <ID> --account @718yuycx --send-at "YYYY-MM-DD HH:MM" --text-head "<本文の先頭>" --purpose "<日付>のYahoo!店LINE(<内容>)を<時刻>に配信予約するため" --checks-file "<観点の行を書いたファイル>"
python -m lineoa_reserve.cli show <申請番号>
```
- **`--checks-file` は必須**(2026-10-28 運用者指摘: 申請 462 が「どういった観点でチェックしたのかの報告が必要」で差し戻された)。確認した観点と結果を 1 行 1 観点で書いたファイル(UTF-8・20 行まで)を渡すと、承認の画面に「■ 確認した観点(検査の結果)」として出る。書く観点: 配信日時(照合の結果)・配信先・中身(吹き出しの数と種類)・リンク先(ページ名・開けるか)・品切れ・「楽天」の文字の有無(Yahoo!店)・予約後に「配信予約中」の一覧で確かめること。カード型でない下書き(画像にリンクを付けた形など)は `cards` がリンクを拾えないので、配信データを読んでリンクを数え、そう書く。
- 結果は `show` が `ok` / `failed` / `mismatch` / `needs_human` / `not_approved` になるまで裏で待つ(30 秒間隔のループを `run_in_background`)。
- `ok` のあと、Chrome で `…/@718yuycx/broadcast` の「配信予約中」に、その ID・日時で載っていることを読んで確かめる(読むだけ)。

### ⑥ LINE で知らせる
```
python -m lineoa_reserve.report --store yahoo --cards "000.tmp\lineoa_yahoo_<日付>\cards.json" --check "000.tmp\lineoa_yahoo_<日付>\cards_check.json" --send-at "YYYY-MM-DD HH:MM" --friends <友だち数> --reserved --out "000.tmp\lineoa_yahoo_<日付>\report"
```
- 本文(`body.txt`)は短く「詳しくは画像をご確認ください」。画像(`spec.json` → `make_status_image.py`)は ① 確認した観点(配信日時・配信先・商品リンク・品切れ・そのほかのリンク・**「楽天」の文字が無い**・予約中の一覧)② カードごとの確認(No・カード・リンク先(G番号)・在庫・結果)③ 気づいた点(一部品切れの種類など)。楽天の `/rakuten-line-reserve` と同じ形(楽天は `--store rakuten` で、「楽天」の行は出ない)。
- `line_notify.py publish` → `request-approval --group 楽天メルマガ配信 --purpose "<日付>のYahoo!店LINEの配信予約と確認結果を担当者に知らせるため"` → `wait-approval`(裏で)→ `send --send`。19:00〜翌 9:00 は道具がキューに積む(終了コード 20)= 翌朝 9:00 に送る。

## 道具
- `900.ClaudeCode/lineoa_reserve/`: cli(login / check / enqueue / run / show / cards)・yahoo_check(Yahoo! のカード検査)・report(知らせの本文と画像の仕様・楽天と共通)。テスト `lineoa_reserve/tests`。
- 見張り役の対象 `lineoa`(楽天・Yahoo!店共通。`a_browser_watch/targets.py` の `LINEOA_ACCOUNTS`)。アカウントを足したら見張り役の再起動が要る。

## 観察記録(2026-10-24)
| 項目 | 内容 |
|---|---|
| アカウント | 制服のフジ Yahoo!店 `@718yuycx`・友だち約 2,219 人・スタンダードプラン(無料 30,000 通/月) |
| 画面・小窓 | 楽天と同じ(編集画面の「配信予約」→ 小窓「メッセージを配信」の「配信」) |
| 依頼 | 楽天と同じ「楽天メルマガ配信」グループに【Yahoo! LINE】として来る |
| 「楽天」の実例 | カードのリンク先 G1726 の Yahoo! 商品名に「楽天ランキング1位★」があった → Yahoo!店 17 点を直した(申請 75・76) |
