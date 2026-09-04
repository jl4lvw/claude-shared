---
name: consignment-settlement
description: 委託販売先(現在はしのびや.com/SAMURAIMARKET天保山店)から届く月次棚卸表を精算し、社内マスタ(pickorder)との価格照合→CONPHAS向け売上登録CSV作成→客先提出PDF作成→ai-agent-terashita@からの返信メール送信(固定Cc・テスト送信必須・自動アーカイブ)までを行う専用手順。062.委託販売精算を使う。
trigger: しのびや.com等の委託先から棚卸表・販売数報告メールが届いたとき、月次の委託販売精算処理を行うとき
---

<!-- SKILL_VERSION: 2026-09-04_225359 -->

# consignment-settlement — 委託販売精算

作業フォルダ: `C:\ClaudeCode\062.委託販売精算`(相対パスは全てここ基準)。

現在サポートしている委託先は**しのびや.com(SAMURAIMARKET天保山店)のみ**。取引先固有の
情報(メール格納先・Excel列構成・顧客コード等)は `partners/shinobiya_tenpozan.py` に
分離してある。別の委託先が増えた場合は、同じ形の新しいpartnersモジュールを作る
(汎用部分は`scripts/`にあるので使い回せる)。

2026-09-04、しのびや.com天保山店8月分の精算を実際に一件流し、その過程で見つかった
罠・確定した規則をすべて反映済み。

---

## 全体の流れ

1. 新着の棚卸表メールを探す
2. 添付Excelを解析する
3. pickorder(社内商品マスタ)と価格を突き合わせる
4. **相違点は必ずユーザーに確認する**(黙って直さない)
5. CONPHAS向け売上登録CSVを作る
6. 客先提出用PDFを作る(アーティファクトで内容確認 → PDF化)
7. CSVをNASへ配置する(要ユーザー承認)
8. 返信メールを`ai-agent-terashita@`から送る(固定Cc・テスト送信必須・送信は自動でログに残る)

---

## 1. 新着メールを探す

`thunderbird_mail.py` を使う(`scripts/`)。委託先ごとのフォルダパスは
`partners/shinobiya_tenpozan.py` の `MAIL_FOLDER_PATH` にある。

```python
import sys
sys.path.insert(0, r"C:\ClaudeCode\062.委託販売精算\scripts")
sys.path.insert(0, r"C:\ClaudeCode\062.委託販売精算\partners")
import thunderbird_mail as tb
import shinobiya_tenpozan as partner

mbox_path = tb.find_folder(tb.TERASHITA_INBOX_SBD, partner.MAIL_FOLDER_PATH)
box = tb.list_messages(mbox_path)
# 最新メッセージ = box[max(box.keys())]。件名は tb.decode_mime_header(msg.get("subject"))
```

**罠(2026-09-04に実際にハマった)**: Thunderbirdのフォルダ階層で、直下にメッセージを
持たない「振り分け専用の親フォルダ」(例: `00A.仕入`/`00B.配送・通関`/`00C.販売先`)は、
同名の平ファイルを持たず `<name>.sbd` ディレクトリのみで存在する。単純に「`.sbd`拡張子は
無視」という探索ロジックだと、この種の親フォルダとその配下が丸ごと探索から漏れる。
`thunderbird_mail.find_folder()` はこれを踏まえて実装済みなので、自分で新しく
フォルダ探索ロジックを書き直さないこと。

2026-09-04追加: `thunderbird_mail.py`の汎用ロジック(フォルダ探索・mbox読取・添付抽出)は
`900.ClaudeCode/mail-search/scripts/`へ切り出し、このファイルは委譲レイヤーになった
(委託販売精算に限らない一般的なメール検索の需要があったため)。呼び出し方(`tb.find_folder(...)`等)
は変わらないので、既存の使い方をそのまま踏襲してよい。同時に、`00A`〜`00E`の振り分け専用
フォルダ名には視覚的装飾(末尾に"■"×10)が付いていることが判明し、厳密一致では見つからない
事故があったため、`find_folder()`は完全一致→前方一致フォールバックの2段構えに修正済み
(`MAIL_FOLDER_PATH`の値自体は装飾なしのままで問題ない)。

## 2. 添付Excelを解析する

```python
from pathlib import Path
attachments = tb.extract_attachments(msg, Path("一時保存先"))
rows = partner.parse_inventory_xlsx(attachments[0])  # xlsx = attachments[0]
```

**罠1**: 先方シートの「下代合計」列に数式が入っていない行がある(2026-09-04、
手ぬぐい大和夕日の実例)。`SheetRow.has_amount_formula`で判定できる。
`qty_sold > 0` なのに `has_amount_formula` が `False` の行は、シートの表示合計に
反映されていない可能性が高いので必ず個別に計算し直す。

**罠2**: 同一JANが複数の商品名で使い回されている行がある(コピペミスと思われる)。
`partner.find_duplicate_jans(rows)` で検出できる。同じ価格同士のペアなら実害はないが、
価格が違うペアは要注意(手順3参照)。

## 3. pickorderと価格を突き合わせる

```python
from pickorder_price_check import fetch_master, index_by_jan, compare_item, find_by_name

master = fetch_master()  # GET http://127.0.0.1:8087/gasapi?action=getMaster (読み取り専用・副作用なし)
master_by_jan = index_by_jan(master)

results = [
    compare_item(r.jan, r.name, r.ex_tax_price, r.qty_sold, master_by_jan)
    for r in rows if r.qty_sold > 0  # 今回の精算に関係する行だけでよい
]
```

`compare_item` の `status` は5種類:
- `matched` — 一致。何もしなくてよい
- `mismatched` — 不一致。手順4でユーザーに確認する
- `disabled` — pickorder側がdisabled(無効化商品)。判定不能として報告のみ
  (2026-09-04追加。以前は`disabled`フラグが未考慮で`zero_price`と誤判定されることがあった。
  cgd Lv3レビュー指摘対応)
- `zero_price` — pickorder側が有効だが0円(データ不備)。判定不能として報告のみ
- `not_found` — JANがpickorderに未登録。判定不能として報告のみ

**🔴 最重要の罠(2026-09-04、ミニフラッグ4商品の実例)**: `mismatched` が出ても、
それが本当に価格の誤りとは限らない。委託先シートの同一JAN使い回し(手順2の罠2)により、
**間違った商品と突き合わせてしまっているだけ**のことがある。`mismatched` の商品は、
先に `partner.find_duplicate_jans(rows)` でそのJANが使い回されていないか確認し、
使い回されていたら `find_by_name(master, "キーワード")` で商品名からも検索して
裏取りすること。JANだけで判定して「相違あり」と即断しない。

## 4. 相違点をユーザーに確認する(必須・省略禁止)

`mismatched` になった品目は、**シート記載の下代**と**pickorderから導出した下代**を
両方提示し、AskUserQuestionで確認してから採用する。ユーザーが独自に正しい価格を
教えてくれた場合はそれを優先する(pickorderの数字が絶対ではない。2026-09-04、
セーラーショルダーバッグは掛率0.7ではなくユーザー指定の実額で確定した実例あり)。

`zero_price` / `not_found` の品目は「判定不能」として報告するに留め、金額を
勝手に0円やpickorderの隣接値で埋めない。

## 5. CONPHAS向け売上登録CSVを作る

```python
from conphas_csv import SettlementItem, write_csv

items = [SettlementItem(name, qty, ex_tax_price) for ...]  # 確認済みの下代税抜を使う
csv_path, subtotal, tax, total = write_csv(
    year=2026, month=8,
    customer_code=partner.CUSTOMER_CODE, customer_name=partner.CUSTOMER_NAME,
    store_code=partner.STORE_CODE, items=items,
    out_dir=Path("スクラッチ領域"),  # まずここ。NASへは手順7で
)
```

規則の詳細・根拠は長期記憶 `reference_conphas_uriage_csv_format.md` を参照。
特に **CSV1行目(ヘッダ)の「日」は精算対象月の最終日**(月初の1ではない。
2026-09-04にユーザーから訂正を受けた)。`month_end_day()`が自動計算する。
消費税の端数処理は`013.CONPHAS-PWA/server/invoice.py`と同じROUND_HALF_UP。
商品名にカンマ・引用符・改行が入っていても`csv.writer`が自動エスケープする
(2026-09-04、cgd Lv3レビュー指摘対応)。数量は`int(round(qty))`で丸めて整数化する。

## 6. 客先提出用PDFを作る

```python
from settlement_html import Finding, LineItem, build_settlement_html
from settlement_pdf import render_html_to_pdf

html = build_settlement_html(
    title=f"{year}年{month}月 委託販売精算",
    recipient_eyebrow=partner.RECIPIENT_EYEBROW,
    period_label=f"{year}年{month}月分",
    report_received_label="...",
    issuer_name="株式会社制服のフジ",
    findings=[...],  # 手順4で確認が取れた相違点のみ(無関係なものは載せない)
    items=[LineItem(...) for ...],
    subtotal_ex_tax=subtotal, tax=tax, total_incl_tax=total,
    issue_date_label=f"発行日: {today}",
)
```

**🔴 客先提出物なので、`pickorder`・`CONPHAS`・社内コード・下書きメモ等の内部情報は
一切含めない**(2026-09-04にユーザーから明示指示。`settlement_html`は最初から
これらを含まない設計になっているので、追加の文言をここに継ぎ足さないこと)。

**必ずArtifactとして公開し、ユーザーに内容を確認してもらってからPDF化する**
(先走って直接PDF化しない。2026-09-04に一度先走って指摘を受けた)。承認が出たら
`render_html_to_pdf(html, out_path)` でPDF化し、`SendUserFile`で送付する。

## 7. CSVをNASへ配置する(要ユーザー承認)

```powershell
Get-ChildItem "\\192.168.1.50\共有スペース\001.win-conpath_Auto\売上登録CSV" -Filter "売上伝票_<顧客コード>_*"
```

まず衝突がないか確認し、`Copy-Item`で配置後、**必ずSHA256ハッシュを比較して
コピーの完全性を検証する**(コピー成功メッセージだけで終えない。
[[feedback_verify_result_not_action]])。これは実際に会計システムへの取込を
トリガーする操作なので、配置前に必ずユーザーの明示承認を得る。

## 8. 返信メールを送る(ai-agent-terashita@経由)

2026-09-04の実運用で、当初想定していたThunderbird EML下書き方式(terashita@本人アカウント)
から、**053.ai-agentメールの`mailer.send_reply_mail()`を使い、`ai-agent-terashita@`
アカウントから直接SMTP送信する方式**に変更が確定した。理由: 相違点の説明メールは
本人アカウントより専用アカウントの方が運用上ふさわしいとユーザーが判断したため。

```python
import sys
sys.path.insert(0, r"C:\ClaudeCode\053.ai-agentメール\scripts")
from dataclasses import replace
import mailer
from config import load_mail_config

cfg = replace(load_mail_config(), display_name="株式会社制服のフジ 寺下")  # 委託先ごとに調整可
```

### 8.1 固定Cc(必須・省略禁止・コードで強制)

`ai-agent-terashita@`から社外へ送るメールは、**委託先固有のCc(`partner.REPORT_CC`)に加えて、
以下2アドレスを必ずCcに含める**(2026-09-04ユーザー指定):

```
terashita@seifukunofuji.com
kawasaki@seifukunofuji.com
```

2026-09-04、この規則は`scripts/partner_mail.py`の`send_partner_reply()`が自動で
強制するようになった(cgd Lv3レビュー批評指摘対応: 「固定Cc・テスト送信・本番承認が
コードで強制されずSKILL.md上の約束のみで、人間が読み飛ばすと外部誤送信になる」)。
**`mailer.send_reply_mail()`を直接呼ばず、必ず`partner_mail.send_partner_reply()`を
経由すること**(手順8.4のコード例を参照)。委託先が増えても、この2アドレスは固定で外さない。

### 8.2 本文でのAI関与の扱い(アカウントによって真逆)

- `ai-agent-terashita@`から送る場合: 表示名が元々「AIエージェント」を含む(またはそれと
  分かるアドレス)なので、**本文の名乗りにAI関与を明記する**(例:「制服のフジ、寺下（扱い：
  AIエージェント）です。」)。2026-09-04にユーザーが明示指示。
- 通常のterashita@等の個人アカウントから送る場合: 引き続き**AI関与の断り書きは一切書かない**
  ([[feedback_no_ai_disclaimer_in_emails]])。
両者は矛盾しているように見えるが、「送信元アドレス自体でAI関与が分かるかどうか」で
使い分ける、というのがユーザーの意図。

### 8.3 送信前に必ずヘッダー込みのArtifactプレビューを見せる(必須・省略禁止)

本文だけでなく **From/To/Cc/件名を含めたメール全体のプレビュー**をArtifactとして
公開し、ユーザーの確認を得てから次へ進む(2026-09-04ユーザー指定)。Cc欄は
`partner_mail.build_cc()`で組み立てる(手順8.1の固定Ccと実際の送信時ロジックを
一致させるため、ここでも手でCc文字列を組み立てない)。

```python
sys.path.insert(0, r"C:\ClaudeCode\062.委託販売精算\scripts")
from email_preview import EmailPreview, build_email_preview_html
import partner_mail

html = build_email_preview_html(EmailPreview(
    from_addr=f"{cfg.display_name} <{cfg.address}>",
    to_addr=partner.REPORT_TO,
    cc_addr=partner_mail.build_cc(partner.REPORT_CC),
    subject=subject, body_text=body_text,
    attachments=[p.name for p in attachment_paths],
))
# → Write()でファイル化してArtifact publish
```

### 8.4 本番送信前に必ずテスト送信する(必須・省略禁止・コードで強制)

[[feedback_test_send_before_production_email]]の通り、本番の宛先(委託先)へ送る前に、
**必ず一度`terashita@seifukunofuji.com`宛だけにテスト送信**し、本文・署名・添付の
見え方をユーザーに確認してもらう。

2026-09-04、`partner_mail.py`が2つの関数でこの規則を強制するようになった:
`send_partner_test()`(常にterashita@宛・件名prefix付与・外部Cc除外)と
`send_partner_reply()`(本番送信・固定Cc自動付与)。**`send_partner_reply()`は
`send_partner_test()`の戻り値(`TestReceipt`)を必須引数として要求し、件名・本文・
添付ファイル(ファイル名・サイズ・sha256)が一致しない場合はValueErrorで送信を
拒否する**。`is_test`のようなbool一つの設計だと「本番分岐へ直接引数を渡せば
テスト未実施でも送れてしまう」抜け道が残るため(2026-09-04、cgd Lv5 Step Cレビューで
実際に指摘された)、テスト送信の実施そのものをオブジェクトの有無で強制する設計に
している。添付ファイルの一致まで見ているのは、件名・本文だけの検証では「テスト送信は
正しいPDFだったが本番送信で別ファイルを渡す」事故を防げないため(cgd Lv5 Step C2で
追加指摘された)。

```python
sys.path.insert(0, r"C:\ClaudeCode\062.委託販売精算\scripts")
import partner_mail

# 1. まずテスト送信(terashita@宛固定・件名に【テスト送信】が自動で付く)
test_receipt = partner_mail.send_partner_test(
    cfg, subject=subject, body_text=body_text,
    in_reply_to=original_message_id, references=original_message_id,
    attachments=attachment_paths,
    source_context="consignment-settlement:shinobiya_tenpozan",
    context_note=context_note,
)
# → ユーザーに内容を確認してもらう(AskUserQuestionで明示承認)

# 2. 承認が取れたら本番送信。test_receiptは1.と同じsubject/body_textでないとValueError
message_id = partner_mail.send_partner_reply(
    cfg, to_addr=partner.REPORT_TO, partner_cc=partner.REPORT_CC,
    subject=subject, body_text=body_text, test_receipt=test_receipt,
    in_reply_to=original_message_id, references=original_message_id,
    attachments=attachment_paths,
    source_context="consignment-settlement:shinobiya_tenpozan",
    context_note=context_note,
)
```

**この最終送信(2.)も、実行前に必ずユーザーの明示承認を得る**
(テストの承認と本番送信の承認は別)。

### 8.5 送信は自動でアーカイブされる(何もしなくてよいが、仕組みは理解しておく)

2026-09-04追加。`mailer.send_html_mail()`/`send_reply_mail()`は**送信成功後に自動で**
以下3つを行う(呼び出し側で個別に実装する必要はない):

1. IMAP AppendでSentフォルダへ複製(生SMTP送信はサーバー側Sentへ自動複製されないため。
   実機確認で0件だった)
2. `sent_log`テーブル(FTS5 trigram全文検索、033共有受信箱と同方式)へ記録
3. 添付ファイルを`053.ai-agentメール/sent_archive/`へ恒久コピー(呼び出し元がスクラッチ
   領域に置いていても、送信後は消えても大丈夫になる)

**呼び出し時は`source_context`/`context_note`を渡すこと**(必須ではないが、渡さないと
後で検索したときに経緯が分からなくなる)。このスキルからの送信では:

```python
mailer.send_reply_mail(
    cfg, to_addr=..., subject=..., body_text=..., attachments=...,
    source_context="consignment-settlement:shinobiya_tenpozan",
    context_note="2026年8月分精算、金額相違3件の訂正連絡",  # 内容を変えるたびに具体的に書く
)
```

過去の送信を検索するときは(request/staffスキーマの外からの送信も含め全件対象):

```bash
python "C:\ClaudeCode\053.ai-agentメール\scripts\mail_client.py" search-sent しのびや
python "C:\ClaudeCode\053.ai-agentメール\scripts\mail_client.py" show-sent <id>
```

現状はCLIのみ(閲覧用PWAは件数が増えてから検討、というユーザー判断。2026-09-04)。

---

## 関連ファイル

- 汎用ロジック: `scripts/conphas_csv.py`(CSV生成)、`scripts/pickorder_price_check.py`
  (価格照合)、`scripts/settlement_html.py`(客先提出資料のHTML生成)、
  `scripts/settlement_pdf.py`(HTML→PDF、Playwright使用)、`scripts/thunderbird_mail.py`
  (900.ClaudeCode/mail-search/への委譲レイヤー。Thunderbirdローカルmboxの検索・添付取得)、
  `scripts/email_preview.py`(送信前プレビューのHTML生成)、`scripts/partner_mail.py`
  (固定Cc・テスト送信規約をコードで強制する送信ラッパー。2026-09-04追加)
- 委託先固有情報: `partners/shinobiya_tenpozan.py`
- メール送信本体: `053.ai-agentメール/scripts/mailer.py`の`send_reply_mail()`
  (依頼管理schema非依存の汎用送信ヘルパー。添付・Cc対応済み。委託先への送信では
  直接呼ばず`scripts/partner_mail.py`経由にすること)
- テスト: `tests/`(pytest、pure logic部分をカバー)
- 長期記憶: `reference_conphas_uriage_csv_format.md`、
  `project_shinobiya_tenpozan_consignment.md`、
  `reference_thunderbird_terashita_accounts.md`、
  `reference_thunderbird_compose_via_eml.md`、
  `feedback_verify_result_not_action.md`、
  `feedback_test_send_before_production_email.md`、
  `feedback_no_ai_disclaimer_in_emails.md`(ai-agent-terashita@の例外を含む)、
  `project_053_ai_agent_mail.md`
  (`~/.claude/projects/C--ClaudeCode/memory/`)
