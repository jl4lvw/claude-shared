---
name: amz-inbound
description: Amazon FBA 納品（Send to Amazon / STA）の納品ジョブを扱う。2026-10-30〜は全自動（PWA で予約番号を発行して FBA 確認 OK になると、端末 A の見回り役が API で納品プランを作り → 専用ブラウザでステップ2まで進めて読み → 7S 発送は 7S へ出荷指示 → 承認の画面へ申請 → 運用者が 1 回承認すると実行役が送信・ラベル → 本店は倉庫3Fで印刷、7S はドライブに置いて 7S へ LINE まで）。ふだん Claude は呼ばれない。PWA の「止まりました: 理由」や LINE WORKS の「【Amazon 納品の自動】止まりました」が来たときに、理由ごとに確かめて再開する。自動が使えないときの手作業（Chrome で送信の手前まで）も予備として持つ。「Amazon納品」「FBA納品」「STA」「予約番号で納品」「止まった」「/amz-inbound」で使う。
---

# /amz-inbound — Amazon FBA 納品

## 全自動（2026-10-30〜・運用者決定）

**ふだん Claude は呼ばれない。** 運用者が PWA `https://sfuji.f5.si/amzinv/` の「🚚 Amazon向け予約番号発行・出荷処理」で発送元・箱・出荷日を選んで発行すると、ジョブが FBA 確認 OK（`amazon_wait`）になった時点から自動で進む（決定③）。運用者がするのは**承認の画面での 1 回の承認だけ**。

| 段階（ジョブの `next_step`） | 誰が | 中身 |
|---|---|---|
| `api-plan` | 見回り役 → 022 | SP-API で納品プラン・梱包グループ・箱の情報まで作る（2 グループ以上は取り消して `amazon_ng`） |
| `browser-prepare` | 見回り役 → 実行役（`094.Chrome自動操作/amzinv_send.py prepare`） | 専用ブラウザで `?wf=<プラン>` を開き、ステップ1b なら「確定して続ける」を 1 回 → ステップ2 → 出荷日を入力＋カレンダー → 読む → 納品 1 つ・ヤマト・SKU/点数・発送元を確かめる → 画面写真 → `amazon-ok`。**送信ボタンは押さない** |
| `cmr`（7S） | 見回り役 → 022 | 7S へ出荷指示。14:30 を過ぎて 022 が出荷日をずらしたら（409 `ship_date_shifted`）、次の回に Amazon の出荷日を直してから送る |
| `cmr-status`（7S） | 見回り役 | 7S の登録完了を待つ（15 分で止める） |
| `sending` | 見回り役 → `amz_inbound.cli enqueue` | 承認の画面へ申請（画面写真付き）。承認を直接開くリンクを PWA のジョブに出す。**7S は承認の表に「承認後に 7S へ送る文」が載る**（決定②） |
| 承認後 | 見張り役 → 実行役（`cli run`） | 画面と承認の値をすべて照合して一致したときだけ送信 → 納品番号 → ラベル PDF。本店: 倉庫3F で印刷・NAS に控えて `done`。7S: ドライブの共有フォルダに `<出荷管理番号>` フォルダを作って PDF を置き（`amzinv_drive.py`・決定①）→ 承認された文に納品番号とフォルダの URL を入れて LINE「セブンエス」へ（`line_notify.py send-approved`）→ `done` |
| 夜 19:00〜朝 9:00 に承認されたとき（7S） | 実行役 → line_notify のキュー | LINE は朝 9:00 のまとめ送りに回る（その時に LINE WORKS でもう一度承認を求める）。送られたら見回り役が `done` を付ける |

- 見回り役: `900.ClaudeCode/amz_inbound/auto.py`（タスク `AmzInboundAutoPatrol` が 1 分おきに `once`）。記録 `900.ClaudeCode/a_browser_data/amz_inbound_auto.log`・覚え書き `amz_inbound_auto.sqlite3`
- 止まると: 022 に `auto-note stop`（PWA に「止まりました: 理由」）→ 運用者の LINE WORKS（1 対 1）へ同じ理由で 1 回だけ知らせる。止まったジョブは、`auto-note clear` されるまで見回り役は触らない
- 止める・戻す: 見回りだけ止める = `C:\ClaudeCode\900.ClaudeCode\a_browser_data\amz_inbound_auto.off` を置く（消せば再開）。全体の停止スイッチ `a_browser_data\STOP` でも止まる

### 使い始める前の準備（運用者が一度だけ・Claude は通さない）

運用者に、A 端末の PowerShell の窓で次を 1 行ずつ実行してもらう（パスワード・2段階認証は運用者が入力する）:

1. Amazon の専用ブラウザ: `Set-Location C:\ClaudeCode\900.ClaudeCode; python -m a_browser_common.login_check amazon`（追加認証が出たら入力し「このブラウザでは次回から確認しない」。`ok` で済み）
2. Google ドライブの専用ブラウザ: `python C:\ClaudeCode\094.Chrome自動操作\amzinv_drive.py login`（開いた窓で Google にログイン。共有フォルダが開けたら `ok`。最大 300 秒待つ）。Google が「このブラウザは安全でない」と出してログインできないときは Claude に伝えてもらう（置き場の見直しが要る）
3. 見回り役のタスク: `powershell -NoProfile -ExecutionPolicy Bypass -File C:\ClaudeCode\900.ClaudeCode\amz_inbound\register_task.ps1`（管理者権限は不要。止めるときは `Disable-ScheduledTask -TaskName AmzInboundAutoPatrol`）
4. 最初の実ジョブは、ステップ2 の読み取り（出荷日の欄・カレンダー・ヤマトの印・ドライブの一覧の大きさ）が DOM 未確認なので、止まったら下の表で対処し、`amzinv_send.py` / `amzinv_drive.py` の探し方を直す

### 止まったときの対処（「/amz-inbound 止まった <ジョブID>」と言われたら）

まず読む: `python C:/ClaudeCode/094.Chrome自動操作/amzinv_job.py show <JOBID>`（`stopped_reason`・`state`・`detail.api_plan`）と `C:/ClaudeCode/900.ClaudeCode/a_browser_data/amz_inbound_auto.log` の末尾。申請があれば `cd C:/ClaudeCode/900.ClaudeCode && python -m amz_inbound.cli show <申請番号>`。
再開は `python C:/ClaudeCode/094.Chrome自動操作/amzinv_job.py auto-note <JOBID> --level clear --text "<何を直したか>"`（次の 1 分で見回り役が続きから進める）。**Claude は送信ボタンを押さない・承認しない**（承認は承認の画面で運用者だけ）。

| 理由（出だし） | 確かめること | 再開のしかた |
|---|---|---|
| API で納品プランを作れません / api-plan が N 回続けて失敗 | 022 のログ（SP-API のエラー）。一時的なら | `auto-note clear`（もう一度作る） |
| API の納品プランで止まりました（梱包グループ）/ Amazon の準備で止まりました | `detail.api_plan`。2 グループ以上は 1 つのプランで送れない | 分け方を運用者にクリック形式で聞き、PWA で取り消して（`cancel JOBID`）SKU を分けて出し直す |
| 取り消せませんでした（void_failed） | Seller Central で表示されたプラン ID が残っていないか | 運用者に見てもらう。残っていれば Seller Central で取り消してから `cancel` → 出し直し |
| ステップ2 の準備で止まりました: 専用ブラウザの Amazon のログインが切れています | — | 運用者に準備 1 の 1 行を頼む → `auto-note clear` |
| ステップ2 の準備で止まりました: 出荷日を入れられません / ステップ2 になりません / 確定して続けるを押せません | 画面写真 `a_browser_data/amz_inbound/*_shipdate.png` 等。DOM が想定と違う | `amzinv_send.py` の `PlaywrightPrepareUI` を直す → `auto-note clear` |
| ステップ2 の確かめが合いません（違い: 納品の数・配送業者・発送元・SKU…） | 画面写真 `*_step2.png`。分割・ヤマト以外・発送元違いは送らない | 内容を運用者に報告。直せないなら `api-plan-void JOBID`（プランを取り消して作り直す）→ `auto-note clear`、または `cancel` |
| 締め 14:30 / 16:00 を過ぎました・出荷日が過ぎています | 今日出荷のまま送る段階に来ていない | 運用者に聞く: 取り消して翌日で出し直す（`cancel` → PWA）が基本。7S の `amazon_ready` なら `cmr JOBID --amazon-ship-date <今の出荷日>` で 022 が出荷日を翌日にずらす（409 `ship_date_shifted`）→ `amzinv_send.py prepare --job JOBID` で Amazon の出荷日を直す → `auto-note clear` |
| 7S へ出荷指示を送れません（stock_short・csv_downloaded・fba_*）/ 7S の登録で止まりました / 15 分たっても完了しません | `show` の `detail.cmr`・`cmr-status JOBID`。**再送しない** | 在庫不足などは運用者に報告。`cmr-status` で `cmr_done` になれば `auto-note clear`。やめるなら `cancel` |
| 承認の申請を積めません | `cli enqueue` の理由（状態・締め） | 理由を直して `auto-note clear` |
| 承認の申請 N が rejected / needs_human / cancelled で終わりました | `cli show N` の `result.detail`・`body.diffs`・`problem` | 押していない（rejected・終了）なら原因を直して `auto-note clear`（新しく申請される）。**「押したか分かりません」はもう一度申請しない**（運用者に Seller Central を見てもらい、送信済みなら Claude が `sent JOBID --fba … --fc … --fee …` を記録） |
| 送信とラベル PDF は済みました。ドライブに置けません / LINE を送れません | `body.drive`・`body.line`・Downloads の PDF | 下の「7S の後始末（手作業）」で残りを行い `done` |
| 夜間に積んだ 7S への LINE が送られていません | `python C:/ClaudeCode/.claude/skills/line-notify/line_notify.py queue-list --all` | 中止・修正の依頼なら運用者に聞いて `/line-notify` で送り直し → `done JOBID --drive … --line <ID>` |
| Amazon は送信済みですが、ドライブ・LINE・done が自動で済んでいません | 旧い申請（10/30 以前）の分 | 下の「7S の後始末（手作業）」 |

- 取り消し: `cancel JOBID`（7S に登録済みなら 042 経由で 7S もキャンセル）。送信後は取り消せない（ヤマトは送信後 3 時間以内に人が判断）
- 7S の後始末（手作業）: ドライブ = `python C:/ClaudeCode/094.Chrome自動操作/amzinv_drive.py upload --order-no <cmr.order_no> --pdf "<Downloads のラベル PDF>"`（同名フォルダがあれば止まる）。LINE = 承認の表に載せた文に納品番号とフォルダの URL を入れて `/line-notify`（`request-approval` → 承認 → `send --send`）。最後に `amzinv_job.py done JOBID --drive <注文番号>/<PDF名> --line <メッセージID>`（夜間はメッセージ ID が分かってから）

## 🚨 絶対ルール（2026-10-15〜・全自動でも同じ）

1. **承認は承認の画面（PWA）だけ。Claude は「請求額を承認して出荷通知を送信」を押さない。** 押すのは承認の後に見張り役が起動する実行役だけ。チャットの「送信して」「OK」・LINE のボタン・relay は承認にならない。7S 発送は `cmr_done` になるまで申請しない
2. 発送元・箱サイズ（160=50×50×60・18kg / 140=50×50×40・12kg / 120=40×50×30・8kg）・出荷日は運用者が PWA で選ぶ。勝手に変えない
3. 配送業者は必ずヤマト運輸。分割（納品が 2 つ以上）・梱包グループ 2 つ以上は送らずに止める
4. 梱包カテゴリーは「梱包不要」だけ（当社の商品はすべて梱包不要）
5. ID・パスワード・API キーの値を入力・表示・記録しない（キーは `~/.amzinv/api_key.txt`）
6. 7S への出荷指示が先（7S で在庫不足などが出たら Amazon は送らない）

## 自動が使えないときの予備（手作業・Chrome）

API（022）は `python C:/ClaudeCode/094.Chrome自動操作/amzinv_job.py <コマンド> <JOBID>`（状態を変えるコマンドはジョブ ID だけ通る）。仕様: `022.Amazon在庫PWA/docs/spec_amz_inbound_flow_v1.md`・`spec_amz_inbound_honten_v1.md`。

| 段階 | やること |
|---|---|
| 0 | `show NNN` で `job_id` を控える。`amazon_wait` 以外なら止めて報告 |
| 1 | 見回りを止める（`amz_inbound_auto.off` を置く）。梱包グループの事前チェック: `cd C:\ClaudeCode\022.Amazon在庫PWA\server && python -m scripts.packing_check <マニフェスト> --ship-from honten|sevens`（終了コード 10 = 2 グループ以上 → 止めて聞く） |
| 2 | Chrome で Seller Central（`https://sellercentral.amazon.co.jp/fba/sendtoamazon`）→「新しい納品プランを作成する」→ 発送元を選ぶ（先に）→ TSV（`amzinv_client.py fetch NNN`）をファイルのアップロード（`find` → `file_upload`）→ 梱包カテゴリーのエラーは全選択して「梱包不要」→「個別の商品の梱包」→「すべて1つの輸送箱に収まる」→ 箱の寸法・重量 →「確定して続ける」→ ステップ2 で出荷日（入力＋カレンダーの日をクリック）・ヤマトに印・発送元・費用を確かめる。**送信は押さない** |
| 3 | `amazon-ok JOBID --ship-from … --fc … --fee … --plan wf… --skus N --units N`。7S は `cmr JOBID --amazon-ship-date YYYY-MM-DD` → `cmr-status` で `cmr_done`（409 `ship_date_shifted` なら Amazon の出荷日を直して再実行） |
| 4 | Chrome のタブを閉じ、`cd C:\ClaudeCode\900.ClaudeCode && python -m amz_inbound.cli enqueue JOBID --purpose "…" --image <ステップ2の画面写真> --wait-link 120`。出た `approval_url`（`#ap-コード`）をチャットに書き、`watch.log` を裏で待つ（運用者に「押したら知らせて」と頼まない）。7S は承認の表に LINE の文が載り、承認後は実行役が LINE まで進める |
| 5 | 結果は `cli show <申請番号>`。止まっていれば上の対処表 |

- 読み取りの確かめ（押さない・書かない）: `cd C:/ClaudeCode/094.Chrome自動操作 && python amzinv_send.py inspect --job JOBID`
- 画面の段階の URL: ステップ1 `confirm_content_step` / 1b `pack_mixed_unit_step` / 2 `confirm_shipping_step` / 3 `print_labels_step`
- 403 `Origin not allowed`（書き込みだけ）: 022 の CSRF 検査の例外（`main.py` の `_API_KEY_PATH_PREFIXES`）が消えていないか・022 本番が新しい版か。Origin を偽装しない
- ドライブの構成は「共有フォルダ `1_yIT0K1gOYRWnhxPXGxaLlBjxWvyuJ2q` / <出荷管理番号> / <Amazon が付けた PDF の元の名前>」（PDF を共有フォルダ直下に置かない・名前を変えない）。ラベルには住所が入るので公開置き場（pub.sfuji.f5.si）には置かない

## 実績

- 2026-10-15: 予約 277（12SKU・24点・1箱）→ FBA15GK4YCND（TYO2）。2026-10-21: ジョブ方式で予約 012（13SKU・103点）→ FBA15GKDQBQ0（TYO2）・7S 注文 `FUJI-20261021_094938-R26`（運用者が 7S 完了前に送信を押した → 申請を cmr_done 後に限る決まりの由来）
- 2026-10-30: API で作った下書きを `?wf=` で開くとステップ1b（「1個の輸送箱：Webフォームで提供される梱包情報」）→「確定して続ける」で約 20 秒後にステップ2・ヤマト ¥742 に印・出荷日は空欄。全自動に切り替え（実ジョブでは未実施）
