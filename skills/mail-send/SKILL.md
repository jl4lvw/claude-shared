---
name: mail-send
description: 外部宛メール(新規スレッド・返信を問わず)を、アーティファクトで送信予定内容を表示→ユーザー承認→EML生成→Thunderbirdで下書き化→手動送信、の手順で安全に送る標準手順。添付ファイル(CSV等)にも対応。
trigger: 外部(取引先・顧客)宛にメールを新規作成・送信したいとき。返信メール特有の引用形式の詳細は[[mail-reply]]を参照
---

<!-- SKILL_VERSION: 2026-09-18_initial -->

# mail-send — 外部宛メールの作成・確認・送信(共通手順)

外部(取引先・顧客)宛のメールを、**いきなりThunderbirdの下書きを作らず**、必ず
アーティファクトでの表示→承認を経てから送信するための標準手順。新規スレッド
(社外への通知・報告等)・返信メールのどちらにも使う共通の土台。

**このスキルが対象とするのは「Claudeが下書きを作り、最終送信は人間がThunderbirdで
手動で行う」経路のみ。**053.ai-agentメール等のAPI直接送信(`mailer.send_reply_mail`)は
対象外([[feedback_test_send_before_production_email]]参照、別のテスト送信ルールが適用される)。

---

## 🎯 手順概要

```
1. メール本文を作成(新規スレッド or 返信。返信は必ず引用形式、[[mail-reply]]参照)
2. アーティファクト(HTML)で送信予定内容を表示(宛先・件名・本文・添付内容)
3. ユーザーが内容を確認(修正があれば Edit で差し替え→アーティファクト再表示)
4. ユーザーが明示的に承認するまで、EMLは絶対に作らない(「生成して」「OK」「送って」等)
5. 承認後にEMLファイルを生成(添付ファイルがあれば同梱)
6. Thunderbirdで開く(-file起動)
7. ユーザーがCtrl+Eで下書きに変換 → 内容再確認 → 手動で送信ボタンを押す
8. (該当する場合)066.業務秘書等の案件管理システムで「処理済み」マーク
```

**🔴 最重要(2026-09-16、実際にユーザーに指摘された事故)**: 産経デジタルへの新規
報告メールで、アーティファクト表示を飛ばしていきなりEMLを作りThunderbirdを開いてしまい、
「いきなりメールのThunderbirdの下書きを作ってはいけない。アーティファクトで送信予定の
メールを表示してください」と指摘された。**新規スレッドのメールでも、返信メールと同じく
必ずStep 2(アーティファクト表示)を経る。**「内容は事前に分かっている」「急いでいる」
といった理由で省略しない。

---

## 1️⃣ アーティファクトで送信予定内容を表示(必須・省略禁止)

### 表示すべき項目

- ⚠️ 発見した問題・補足事項(あれば。例: GoQ側のデータ不整合を訂正した経緯等)
- 宛先(To・Cc)
- 件名
- 添付ファイル名(あれば)
- 本文全文(monospace + pre-wrap)
- **添付がCSV等の構造化データなら、内容をテーブルでも表示する**(本文に埋め込まれた
  CSVバイト列を人間が目で追うのは非現実的なため)

### HTMLアーティファクトの最小構成

```html
<h1>送信予定メール(まだ送信していません)</h1>
<div class="card">
  <div class="field"><div class="k">宛先</div><div class="v">to1@example.com, to2@example.com</div></div>
  <div class="field"><div class="k">件名</div><div class="v">件名文字列</div></div>
  <div class="field"><div class="k">添付</div><div class="v">report.csv</div></div>
</div>
<div class="card"><pre class="body">本文全文</pre></div>
<div class="card">
  <table><tr><th>列1</th><th>列2</th></tr><tr><td>値</td><td>値</td></tr></table>
</div>
```

light/dark両対応のCSS変数([[artifact-design]]スキル参照)を使うこと。**「まだ送信して
いません」という見出しを必ず入れる**(下書き済みなのか送信済みなのか、ユーザーが
一目で分かるようにするため)。

### チェックポイント(送信前に必ず確認)

- [ ] 宛先・Ccは正しいか
- [ ] 敬語・表現は適切か
- [ ] URL・金額・商品名は正確か(商品名はG番号併記、[[feedback_always_annotate_g_number_with_product_name]])
- [ ] 返信の場合、引用文は正しい形式か([[mail-reply]]参照)
- [ ] **AI作成の断り書きが入っていないか**(禁止。[[feedback_no_ai_disclaimer_in_emails]])
- [ ] 添付ファイルの中身は正確か(生成直後に自分でも読み直す)

---

## 2️⃣ ユーザーの明示承認を待つ(省略禁止)

アーティファクトを表示したら、**「生成して」「OK」「送って」等の明示的な承認が
得られるまでEMLを作らない。** これは[[feedback_show_artifact_before_writing_records]]
と同じ思想(登録・送信のような不可逆に近い操作は、一覧提示→承認→実行の順を必ず守る)。

承認が得られたら文脈台帳に記録してよい(長時間セッションでの圧縮対策):
```bash
python C:/ClaudeCode/.claude/hooks/ctx_cli.py add <SID> OK "<宛先>へのメール送信承認: <件名>"
```

---

## 3️⃣ EMLファイル生成(添付ファイル対応・Windows文字コード罠に注意)

**`EmailMessage(policy=SMTP)`を使い、`write_bytes()`でバイト列のまま書き出す。**
`write_text()`は使わない(Windowsでは`\n`が`\r\n`に黙って変換され、`add_attachment`で
組み立てたMIME構造を壊すおそれがある。[[reference_windows_shell_pitfalls_hub]]の
newline罠と同じ系統の問題)。

```python
from email.message import EmailMessage
from email.policy import SMTP
from pathlib import Path

body = """宛先様

いつもお世話になっております。
制服のフジ　寺下です。

[本文]

以上、よろしくお願いいたします。

寺下貴之（てらした たかゆき）
terashita@seifukunofuji.com
〒737-0046 広島県呉市中通1丁目1番21号
(株)制服のフジ
TEL：0823-21-7731 / FAX：0823-25-0130 / Mob：080-1925-1031
"""

msg = EmailMessage(policy=SMTP)
msg["From"] = "寺下貴之 <terashita@seifukunofuji.com>"
msg["To"] = "to1@example.com, to2@example.com"
msg["Subject"] = "件名"
msg.set_content(body, charset="utf-8")

# 添付ファイルがある場合のみ
csv_bytes = Path(r"C:\path\to\report.csv").read_bytes()
msg.add_attachment(csv_bytes, maintype="text", subtype="csv", filename="report.csv")

eml_path = Path(r"C:\ClaudeCode\...\scratchpad_xxx.eml")
eml_path.write_bytes(bytes(msg))   # write_text ではなく write_bytes
print("written:", eml_path, eml_path.exists())
```

日本語パスを含むディレクトリへの直接編集はEditツールが失敗することがあるため
([[CLAUDE]]「注意事項」)、上記のようなPythonスクリプトを`C:/ClaudeCode/`直下
(または[[reference_windows_shell_pitfalls_hub]]の罠を避けるためスクラッチパッド)に
書いて`python`で実行する方式を使う。

**添付ファイルが本来ユーザーへ渡す成果物でもある場合**(例: 出荷報告CSV)、メール添付とは
別に[[feedback_user_check_files_downloads_and_nas]]の二重コピー(Downloads + NAS
999.一時ファイル)も忘れずに行う。

---

## 4️⃣ Thunderbirdで開く(-file起動、手動送信のみ)

```bash
powershell -Command "Start-Process 'C:\Program Files\Mozilla Thunderbird\thunderbird.exe' -ArgumentList '-file', '\"<EMLの絶対パス>\"'"
```

Pythonから直接起動する場合(`subprocess.Popen`、リスト形式でシェルクォート問題を回避):

```python
import subprocess
subprocess.Popen([r"C:\Program Files\Mozilla Thunderbird\thunderbird.exe", "-file", str(eml_path)])
```

`-compose "to='...',body='...'"`のような直叩きは**禁止**([[reference_thunderbird_compose_via_eml]]、
本文のURLデコードが不安定・署名と二重表示になる実例が複数回発生・断念済み)。**必ずEML経由**。

### 送信後

1. Thunderbirdの作成ウインドウで**Ctrl+E**(新しいメッセージとして編集)を押し、下書き化
2. 内容を最終確認
3. **送信はユーザーが手動で行う。Claudeは下書きを開くところまで**
4. (該当する場合)066.業務秘書等の案件管理システムで「処理済み」マーク

---

## 5️⃣ ローカル記録の更新(該当する場合)

添付CSVがローカルの「報告済み」フラグ管理と連動している場合(例: 052卸売の
`make_sankei_report.py`が`reported_at`を自動付与)、**CSV生成の時点で既にフラグが
付いていることがある**。この場合、ユーザーから「送った」と聞いた時点で追加の記録操作は
不要(CSV生成時点で付与済み)。フラグ管理の仕組みが無いメールの場合、案件管理システム
(066等)側で「処理済み」マークを行う。

---

## 複数メールを並行処理する場合

1. 全メールについてアーティファクト作成
2. 順番に確認・修正
3. 確認済みのものから承認を得てEML生成・Thunderbird起動
4. すべて送信完了した後に案件管理システムを一括更新

---

## 🔗 関連

- [[mail-reply]] — 返信メール特有の引用形式・案件管理システム連携の詳細
- [[feedback_email_reply_artifact_preview]] — このワークフローの根拠となったユーザー指摘
  (2026-09-16、052卸売の新規スレッドメールでアーティファクト表示を省略し指摘された実例)
- [[feedback_mail_writing_rules_hub]] — 謝罪・期限・AI断り書き・転送件名等、文面自体の規約ハブ
- [[feedback_no_ai_disclaimer_in_emails]] / [[feedback_always_annotate_g_number_with_product_name]]
- [[feedback_user_check_files_downloads_and_nas]] — 添付が成果物を兼ねる場合の二重コピー
- [[feedback_test_send_before_production_email]] — API直接送信経路(053等)の別ルール(本スキルの対象外)

---

**作成日**: 2026-09-18
**作成背景**: 052卸売の産経デジタル向け出荷報告メール(新規スレッド・CSV添付あり)を
複数回作成する中で確立した手順をスキル化。[[mail-reply]](返信専用・引用形式が主眼)とは
別に、新規スレッドメールも含む共通の土台として切り出した。
