---
name: shipping-notify
description: 産経デジタル(3K)・シフトプラス(呉市ふるさと納税)・ZenPlusの3チャネルについて、GoQでの出荷状態(送り状発行済みか)を確認し、各チャネルへ出荷完了を通知するまでの標準手順。052.卸売注文GoQ統合を使う。登録(wholesale-order/furusato-shiftplus)より後の工程に特化。
trigger: 出荷状態の確認・出荷完了通知(産経/シフトプラス/ZenPlusのいずれか、または「何か通知していないものはないか」の横断確認)を行うとき
---

<!-- SKILL_VERSION: 2026-09-12_085915 -->

# shipping-notify — 出荷状態確認 → 出荷完了通知(3チャネル横断)

作業フォルダ: `C:\ClaudeCode\052.卸売注文GoQ統合`（相対パスは全てここ基準）。

このスキルは[[wholesale-order]]・[[furusato-shiftplus]]の**登録が終わった後**の工程
(出荷状態の確認・出荷完了を各チャネルへ知らせる)だけを横断的に扱う。3チャネルとも
「対象抽出→GoQ照会→通知→ローカル記録更新」という同じ骨格だが、通知の手段(メール/CSV
アップロード/API)がチャネルごとに全く違うため、混同しないようここに一本化する。

## チャネル対応表

| チャネル(略称) | 通知手段 | ツール | 対象抽出条件 |
|---|---|---|---|
| 産経デジタル(3K) | メール添付CSV(手動送信) | `tools/make_sankei_report.py` | `sent/SANKEI-*.json` の `reported_at` 無し |
| シフトプラス(Shift+、呉市ふるさと納税CSV経路) | 専用CSVを手動アップロード | `tools/export_shiftplus_shipped_csv.py` | `sent/FURUSATOCSV-*.json` の `shiftplus_reported_at` 無し |
| ZenPlus | API直接送信(PUT) | `tools/report_zenplus_shipped.py` | `sent/ZENPLUS-*.json` の `zenplus_reported_at` 無し |

シフトプラスの「まとめ登録」経路(`FURUSATO-<日付>.json`、送り状はGoQで発行しない)は
**対象外**(配送業者が紙伝票を持参するため、出荷完了通知という概念自体が無い)。

## 0. 「出荷状態を確認して」「未通知のものはないか」と言われたら

3チャネルすべてを横断でチェックする。各チャネルの「未報告分プレビュー」コマンドは
**副作用が無い**(reported_atを付けない)ので、まずこれだけ回して現状を報告してよい:

```bash
python tools/make_sankei_report.py --no-mark 2>&1 | tail -5
python tools/export_shiftplus_shipped_csv.py --out reports/_preview.csv 2>&1; rm -f reports/_preview.csv
python tools/report_zenplus_shipped.py --payload 2>&1
```

いずれも「対象がありません」「出力対象0件」なら、そのチャネルは通知漏れ無し。
候補が出たら、送り状発行状況(tracking_no)込みで一覧をユーザーに提示してから、
下記の各チャネル手順で実際に通知する。

## 1. 産経デジタル(3K)

詳細手順は[[wholesale-order]]の「8. 出荷完了報告メール」を参照(**このスキルの最重要参照先**)。
要点だけここに転記する:

```bash
python tools/make_sankei_report.py            # 既定=未報告分のみ・GoQ実読取→CSV生成→reported_atを付与
python tools/make_sankei_report.py --all       # 全件(確認用・reported_atは付けない)
python tools/make_sankei_report.py --no-mark   # 未報告分プレビュー(マークしない)
```

- 生成したCSVは**メール添付で先方へ送信**(EML方式、`-compose`直叩き禁止、AI作成の断り書きは入れない)
- **`reported_at`はCSV生成時点で付く。実際に送信されたかどうかとは連動しない**
  (2026-08-25実例: 3件がreported_at付きなのに実際は送信されず下書きのまま放置されていた)。
  真に未送信かは**Thunderbird送信済みフォルダの実履歴と突き合わせて**確認する
- 同梱注文(combine_shipments.pyでまとめたもの)は、サブ注文単位でも`goq_lookup`に個別照会して
  本当に同じ送り状番号か確認してから報告する

## 2. シフトプラス(Shift+、実住所CSV経路)

詳細手順は[[furusato-shiftplus]]の「6. シフトプラスへ出荷完了を通知する」を参照。

```bash
python tools/export_shiftplus_shipped_csv.py            # 未通知分のみ・CSV出力+マーク
python tools/export_shiftplus_shipped_csv.py --all       # 全件・マークしない(確認用)
```

- 列は `配送No/伝票番号/配送業者/出荷予定日`(CP932)。**「配送No」はシフトプラス側の
  「配送No.」の値**(例`20260005112`)であり、**GoQの受注番号ではない**(2026-09-02に
  実際にアップロード拒否された事故あり。`furusato_csv_csv.py`が`delivery_no`として
  item単位で保持している値をそのまま使う)
- 生成したCSVはシフトプラスの管理画面へ手動アップロード
- **出力ファイルは必ずNAS(`\\192.168.1.50\共有スペース\寺下\999.一時ファイル`)と
  `C:\Users\user\Downloads\`の両方へコピーする**(絶対厳守、判断せず無条件)

## 3. ZenPlus

```bash
python tools/report_zenplus_shipped.py --payload   # 送信内容をプレビュー(実送信なし)
python tools/report_zenplus_shipped.py --send       # 実際にAPIへPUTし、成功分をマーク
```

- 認証は`052.卸売注文GoQ統合/.env`の`ZENPLUS_API_KEY`(GUID形式)。無ければ
  ユーザーにストア設定画面での確認を依頼する(`.env`は`**/.env`でgit管理外)
- **`tools/zenplus_client.py`はUser-Agentにブラウザ相当の文字列を付けている。外さない**
  (2026-09-12実測: Cloudflareが既定の`Python-urllib`UAを`error code: 1010`でボット判定しブロックする)
- **レート制限は1秒1リクエスト**。`list_orders`/`get_order`を連続で呼ぶ場合は`time.sleep(1.2)`
  程度の間隔を空ける(`mark_shipped`は1回のPUTで複数件まとめて送れるので通常は問題にならない)
- 通信エラー・タイムアウト時は**自動再送しない**(ZenPlus側で実は成功している可能性があるため)。
  `report_zenplus_shipped.py`はエラー時にマークを一切行わず中断する設計になっている
- ファイル出力は無い(API直送信のため、NAS/Downloadsコピーの対象外)
- `delivery_company`はGoQの`carrier`値(「日本郵便」「佐川急便」等)をそのまま渡している。
  ZenPlus側のAPIマニュアルの例(「ヤマト運輸」「日本郵便」)と表記が一致することを確認済み

## 4. 共通の罠

### GoQのステータス・送り状番号を過信しない(最重要)

- **ステータスが「処理済」でも送り状番号(tracking_no)が空のことがある**
  ([[feedback_goq_status_can_advance_without_tracking]])。必ず`tracking_no`の有無で
  「本当に出荷済みか」を判定し、ステータス文言だけでは判断しない
- **逆に、送り状発行済みなのに`goq_lookup`(047の`reissue_slip.py --info-only`、
  `index_beta.php?stat=12`の一覧検索方式)がtracking_noを取得できないことがある**
  (2026-09-10発見。一覧から対象注文自体が外れると検知できない)。この場合はユーザーに
  GoQ画面を直接見て伝票番号を教えてもらい、それを使ってCSV/API送信の内容を組み立てる
  (`export_shiftplus_shipped_csv.py`等のGoQ照会をバイパスして手動で値を渡す一時対応)。
  **根本原因の調査・恒久修正は別タスクとして進行中**(未解決の場合あり。着手前に
  `.hq/board`や進行中タスクを確認する)

### 出力ファイルは必ず二重コピー

CSV/PDFを出力したら、**判断せず無条件に**以下2箇所へコピーする
([[feedback_user_check_files_downloads_and_nas]]):

1. `C:\Users\user\Downloads\`
2. `\\192.168.1.50\共有スペース\寺下\999.一時ファイル`(PowerShellの`Copy-Item`推奨)

### `reported_at`系フラグは`git stash`事故で消えることがある

**このリポジトリでは無引数の`git stash`を使わない**
([[feedback_no_broad_git_stash_this_repo]])。2026-09-11、別セッションの広域stashに
巻き込まれ、`shiftplus_reported_at`8件・`zenplus`関連の実装・経路Aのmessage_idが
すべてディスク上から消えた実例がある。**通知作業の直後に更新のはずのフラグが
消えていたら、まず`git stash list`を確認し、残っていれば`git diff stash@{0}^1 stash@{0}
-- <file>`で自分のハンクだけ確認してから`git apply`で個別復元する**(ファイル丸ごと
`git checkout stash@{0} -- <file>`は他セッションの変更まで巻き戻すので使わない)。

### 大量件数の通知は分割して確認する

一度に大量件数を無確認で送信・アップロードしない。件数が多い場合は一覧を先に提示し、
ユーザー確認を得てから実行する([[feedback_bulk_order_needs_csv_upload_not_one_by_one]]
と同じ思想。この方針はGoQ**登録**時のものだが、通知系の一括操作でも踏襲する)。

### GoQの`shipping_method`(配送種別テキスト)は信用しない

2026-09-16、佐川急便で配達完了済みの注文がGoQ上で`shipping_method`(配送種別)
「ゆうパケ」(日本郵便専用サービス)を返す実例が発生した(単独照会でも再現・こちらの
登録スクリプトのバグではなくGoQ本体側の不具合)。**本当に出荷されているか・どの
サービスで送ったかを確定させたい場合は、実際の配送会社の追跡ページで直接確認する**:
佐川急便=`047.GoQ送り状再印刷/server/sagawa_track.py`、
日本郵便=`001.Python/103.GoQ出荷処理サポート/carriers/japan_post.py`(どちらも
読み取り専用・既存モジュール)。産経デジタル向け報告の配送種別欄は、この事故を機に
「ゆうパケ」(carrier=日本郵便)/「宅配便」(それ以外)の2値のみへ簡略化した
(`carrier`は事故当日でも正しく読めていたため信頼できる値として採用。
詳細は[[project_052_sankei_goq_wholesale]]参照)。

### 出荷済み・未報告の放置は定時チェックで自動検知する(2026-09-16実装)

上記事故で、実際の発送(9/15)から報告(9/16)まで1日以上気づかれなかった。
`tools/check_unreported_shipped.py`がWindowsタスク「`SankeiUnreportedShippedCheck`」
経由で1日4回(9/13/16/19時)自動実行され、3チャネル横断で出荷済み・未報告が
6時間超放置されているとDiscord+LineWorksへアラートする(読み取り専用・実際の
報告メール送信はしない)。**ユーザーから「チェックして」と言われるのを待たなくても、
この仕組みが先に気づく設計になっている**ので、依頼を待つだけでなくアラートが来たら
即座に本スキルのStep 0以降を実行すること。

### 産経デジタルは17時締切のため16:30に自動送信までする(2026-09-18実装・本チャネル限定)

産経デジタルから「出荷管理報告は必ず17時までに」と要請があった。他チャネルと違い、
産経デジタルは**通知だけでなく自動送信まで行う**(ユーザーが明示的に人の確認なしの
完全自動送信を選択・承認済み)。`tools/auto_send_sankei_deadline.py`がWindowsタスク
「`SankeiDeadlineAutoSend1630`」経由で毎日16:30に自動実行され、未報告分があれば
`make_sankei_report.py --only-shipped`でCSV生成→Downloads/NASへコピー→
`053.ai-agentメール`のSMTP基盤で産経デジタルへ自動送信する。安全装置として
dryrun/self/productionの3段階(`047`の`sevens_notify_worker.py`と同じ設計)があり、
現在productionモードで稼働中。**このチャネル(産経デジタル)については、16:30以前に
手動で報告を済ませてあれば0件で何もしない(二重送信の心配は無い、`reported_at`で
自動的に除外される)**。シフトプラス・ZenPlusにはこの自動送信は無い(通知のみ)。

---

## 関連ファイル

- 登録(GoQへの受注登録)側の手順: [[wholesale-order]](産経・ZenPlus・シフトプラス共通の
  登録フロー)、[[furusato-shiftplus]](シフトプラス2経路の詳細)
- ツール本体: `tools/make_sankei_report.py`・`tools/export_shiftplus_shipped_csv.py`・
  `tools/report_zenplus_shipped.py`・`tools/zenplus_client.py`
- GoQ読み取り専用照会: `tools/goq_lookup.py`(`stat=12`一覧の既知の限界あり、上記参照)
- 長期記憶: `project_052_sankei_goq_wholesale.md`・`feedback_goq_status_can_advance_without_tracking.md`・
  `feedback_no_broad_git_stash_this_repo.md`・`feedback_user_check_files_downloads_and_nas.md`
  (`~/.claude/projects/C--ClaudeCode/memory/`)
