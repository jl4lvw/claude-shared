---
name: rakuten-line-reserve
description: 楽天LINE(LINE 公式アカウント「制服のフジ楽天市場店」)の配信予約を手配するスキル。担当者が LINE「楽天メルマガ配信」で「【楽天LINE】作成しました」と依頼した下書きを LINE Official Account Manager で確かめ、承認の画面で承認を受けてから、端末 A の自動操作で「配信予約」を押し、予約になったことを確かめて LINE で知らせる。「楽天LINE」「楽天LINEの予約」「LINE配信予約」「/rakuten-line-reserve」で使う。
---

# /rakuten-line-reserve — 楽天LINE の配信予約(依頼の確認 → 承認 → 予約 → 確認 → 知らせ)

2026-10-24 運用者決定で作成。**LINE で完了を知らせたところまでで完了**。

```
① 依頼を読む       LINE「楽天メルマガ配信」の「【○○配信希望】の【楽天LINE】作成しました」(090 の受信記録)
② 下書きを読む     LINE Official Account Manager の「下書き」一覧(読むだけ)→ ID・配信日時・配信先・本文の先頭
③ 照合だけ実行     lineoa_reserve check(何も押さない)
④ 申請             lineoa_reserve enqueue → 見張り役が承認の画面で承認を求める
⑤ 予約(自動)      承認されたら見張り役が run → 照合 →「配信予約」→ 詳細画面で「配信予約中」を確かめる
⑥ 知らせ           /line-notify の手順で「楽天メルマガ配信」へ(承認の画面で承認を受けてから)
```

## 🚨 絶対ルール

1. **押すのは「配信予約」とその確認だけ。** 今すぐ配信・テスト配信・下書き保存・削除・編集内容の変更はしない(道具が通信ごと止める)。
2. **承認は承認の画面だけ**(チャットで承認を求めない)。Claude が Chrome で「配信予約」を押すことはしない(自動操作は承認された run だけ)。
3. **下書きの中身(本文・画像・配信日時・配信先)は直さない。** 照合が合わなければ止めて、違いを担当者に伝わる形で報告する(直すのは担当者)。
4. 配信先は「すべての友だち」だけ扱う(絞り込みは未対応・止まる)。配信日時まで 10 分を切っていたら押さない。
5. **ログインは運用者だけ。** 切れていたら `needs_human` で止まるので、`login` の窓で運用者にログインしてもらう(Claude はパスワードを入れない)。
6. 失敗は自動で出し直さない。原因を報告してユーザーに聞く。
7. 対象のアカウントは「制服のフジ楽天市場店」(`@072pxwvf`)だけ(`lineoa_reserve/config.py` の `ACCOUNTS`)。Yahoo!店・本店の LINE は対象外。

## 手順

### ① 依頼を読む
090 の受信記録を**読み取り専用**で開き、「楽天メルマガ配信」グループの直近の発言から【楽天LINE】の依頼を探す
(`090.LINE業務指示エージェント/data/line_agent.db` の `line_messages`。グループ ID は会話に書かない。`received_at` はミリ秒)。
配信希望の日時(例「本日19時30分配信希望」)を控える。【Yahoo! LINE】は別アカウントなので対象外。

### ② 下書きを読む(Claude in Chrome・読むだけ)
`https://manager.line.biz/account/@072pxwvf/broadcast` →「下書き」→ 依頼の時刻ごろに更新された行(更新者は詳細画面の「最終更新者」)。
詳細画面 `…/broadcast/<ID>` で ステータス(下書き)・配信日時・ターゲット を、編集画面 `…/broadcast/edit/<ID>` で本文(テキストの吹き出し)を読む。**編集画面では何も押さない**(開いただけでは保存されない)。
- 本文の先頭(text_head)は、**絵文字を含まない 10 文字以上**を選ぶ(絵文字は画像で表示され、照合で見つからないことがある)。
- 配信日時が依頼の希望と違う・空(今すぐ配信)なら、止めて担当者に確認(直さない)。
- 今月の無料メッセージ数(一覧の上)も控える。友だち数(約 406 人)を足して 5,000 を超えるなら報告する。

### ③ 照合だけ実行(何も押さない)
```
cd C:\ClaudeCode\900.ClaudeCode
python -m lineoa_reserve.cli check <ID> --send-at "YYYY-MM-DD HH:MM" --text-head "<本文の先頭>"
```
- `ok` … 次へ。`mismatch` … 違い(`diffs`)を報告して止める。`needs_human` … ⑤の「ログイン」へ。

### ④ 申請
```
python -m lineoa_reserve.cli enqueue <ID> --send-at "YYYY-MM-DD HH:MM" --text-head "<本文の先頭>" --purpose "<日付>の楽天LINE(<内容>)を<時刻>に配信予約するため"
```
- 見張り役が承認の画面で承認を求める。結果は `python -m lineoa_reserve.cli show <申請番号>` が `ok` / `failed` / `mismatch` / `needs_human` / `not_approved` になるまで裏で待つ(30 秒間隔のループを `run_in_background`)。
- 運用者には「承認の画面で承認してください。結果は自動で見ています」とだけ伝える。

### ⑤ 結果の確認
- `ok`: 押した後に詳細画面を読み直し、ステータスに「予約」が入り、配信日時が申請どおり。`shot` に画面の写し。
- `failed`: `detail` と `guard`(止めた・通した通信)をそのまま報告。**初回の実行で、画面の読み込みに必要な書き込みを止めて失敗した可能性がある**ときは、`guard` の block を見て運用者に相談する(勝手に許可を広げない)。
- 実行の後、Chrome で「配信予約中」の一覧も読んで、その ID・日時があることを確かめる(読むだけ)。
- **ログイン**(初回・切れたとき): `python -m lineoa_reserve.cli login` で専用ブラウザ(`a_browser_data\.profile_lineoa`)が開く。運用者が LINE ヤフー Business ID でログインし、アカウントリストが出たら自動で閉じる(15 分待つ)。ふだんの Chrome のログインとは別。

### ⑥ LINE で知らせる(`/line-notify` の手順 1〜4)
本文の形(メルマガの案 3 に合わせる)。URL は書かない(`--url` が末尾に足す):
```
◆楽天LINE 配信予約完了

10/24(土) 19:30 に配信されます。
すべての友だち 約406人
```
- 画像(`make_status_image.py`): 見出し「楽天LINE 配信予約完了」、表に 配信日時 / 配信先(人数)/ 本文の先頭。
- `request-approval --group 楽天メルマガ配信 --purpose "<日付>の楽天LINEの配信予約が済んだことを担当者に知らせるため"` → `wait-approval`(裏で)→ `send --send`。
- 19:00〜翌 9:00 は道具がキューに積む(終了コード 20)。その場合も「翌朝 9:00 に送ります」と書いて完了扱い。

## 道具
- `900.ClaudeCode/lineoa_reserve/`(config・models(照合)・guard(通信の見張り)・page(画面操作)・cli)。テスト `lineoa_reserve/tests`。
- 見張り役の対象 `lineoa`(`a_browser_watch/targets.py` の `_lineoa_summary`)。対象を足したら見張り役の再起動が要る。

## 観察記録(2026-10-24)
| 項目 | 内容 |
|---|---|
| 編集画面のボタン | 上下に「下書き保存」「テスト配信」「配信予約」 |
| 配信先 | radio `recipientRange`(1 つ目=すべての友だち) |
| 配信日時 | radio `radio`(1 つ目=今すぐ配信・2 つ目=予約)・`datepicker`(YYYY/MM/DD)・`timepicker`(HH:MM)。担当者が予約の日時まで入れて下書き保存している |
| 一覧のタブ | 配信予約中 / 下書き / 配信済み / 配信エラー |
| 画面を開くのに要る POST | `api/bots/<アカウント>/switch`(アカウントの切り替え)・`broadcastReach`(人数の見積もり)・`messageAnnotation/preview/…`(プレビュー)。止めると「Something went wrong」。見張りはこの 3 種類だけ常に通し、ほかの書き込みは「配信予約」を押す直前の 30 秒だけ通す |
| 未確認 | 「配信予約」を押した後の確認の小窓の文言・ボタン(初回の実行で確かめて、ここに書き足す) |
