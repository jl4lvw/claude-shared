---
name: mail-send
description: メールを送る・返信する・転送するときの標準手順。アーティファクトで送信予定内容を表示→ユーザー承認→EML生成→Thunderbirdで下書き化→手動送信。社内への問合せ転送・添付ファイル(CSV等)にも対応。
trigger: 「メール送信」「メールを送って」「返信して」「転送」「転送して」「担当者へ回して」「いつもの3人に」等、メールを送る/返信する/転送する意図が出たとき。必ずこのスキルを呼んでから着手する
---

<!-- SKILL_VERSION: 2026-09-18_forward -->

# mail-send — メールの作成・確認・送信(共通手順)

メールを、**いきなりThunderbirdの下書きを作らず**、必ずアーティファクトでの表示→承認を
経てから送るための標準手順。**新規スレッド・返信・転送のすべてがこのスキルの対象**。

**このスキルが対象とするのは「Claudeが下書きを作り、最終送信は人間がThunderbirdで
手動で行う」経路のみ。**053.ai-agentメール等のAPI直接送信(`mailer.send_reply_mail`)は
対象外([[feedback_test_send_before_production_email]]参照、別のテスト送信ルールが適用される)。

---

## 🚫 絶対禁止（最初に読む）

| やってはいけないこと | 正しいやり方 |
|---|---|
| **勝手にメールを送信する** | 送信は必ずユーザーが手動。Claudeは下書きを開くまで |
| **アーティファクトを飛ばしてEMLを作る/Thunderbirdを開く** | 先にアーティファクト表示→明示承認を待つ |
| `thunderbird -compose "to=...,body=..."` の直叩き | `mail_draft.py`（内部で `-file`）を使う |
| `write_text()` でEMLを書く | `mail_draft.py`（内部で `write_bytes`）を使う |
| 元メール本文を持たないまま転送文を作る | 先に `/mail-search` で一次資料を取る（後述 Step 0） |

**「急いでいる」「内容は分かっている」は省略の理由にならない。**

---

## 📋 この手順の全体像（迷ったらこの順に実行）

```
0️⃣  返信・転送なら、まず元メールの本文を /mail-search で取得する
    そのうえで本文を組み立てる（転送の定型フォーマットは後述）
1️⃣  アーティファクトで送信予定内容を表示          ← 省略禁止
2️⃣  ユーザーの明示承認を待つ（「OK」「送って」等） ← 省略禁止
3️⃣  本文を .txt に書き、mail_draft.py でEML生成＋Thunderbird起動
    → ユーザーがCtrl+E→下書き化→手動送信。Claudeはここで止まる
4️⃣  (該当する場合)066.業務秘書等で「処理済み」マーク
```

**🔴 2026-09-16の事故**: 産経デジタルへの報告メールで 1️⃣ を飛ばしていきなりEMLを作り
Thunderbirdを開いてしまい、ユーザーに指摘された。
**🔴 2026-09-18の事故**: 転送依頼で 0️⃣ を飛ばし、元メール本文を持たないまま
プレースホルダー入りの転送文を作った。さらに `-compose` 直叩きで本文が空欄になり、
3回やり直した。**0️⃣ と 1️⃣ は飛ばさない。**

---

## Step 0️⃣ 返信・転送は、先に元メールの本文を取る（省略禁止）

**本文を持たないまま「[詳細は元メールを確認してください]」のようなプレースホルダーで
転送文を作らない。** それは転送になっていない。

```python
import sys
sys.path.insert(0, r"C:\ClaudeCode\900.ClaudeCode\mail-search\scripts")
sys.stdout.reconfigure(encoding="utf-8")
from search import SearchQuery, search, summarize_hit
from mbox_reader import extract_text_body

hits = search(SearchQuery(subject_contains="件名の一部", limit=5))
for h in hits:
    print(summarize_hit(h))
    print(extract_text_body(h.message))
```

### 🔴 罠: `mc`(mailcheck.py)が表示する差出人は、実際のFromとは限らない

ホームページの問い合わせフォーム経由のメールは、**実際のFromは
`wordpress@seifukunofuji.com`**（宛先は `fw-shopmaster@`）で、お客様のアドレスは
**本文の中に書かれている**。`mc` の一覧はそのお客様名を差出人として見せるため、
お客様のアドレスで `from_contains` 検索すると **0件になる**。

- 件名は `AskFromWebpage株式会社 制服のフジ "<お客様が入力した題名>"` の形
- 探すときは **`subject_contains` にお客様が入力した題名の一部**を使う
- 2026-09-18、`from_contains="sisaa"` で0件 → 見つからないと誤判断しかけた実例

**0件は「無い」の証明にならない**（[[mail-search]]の警告）。条件を変えて複数回試す。

---

## 📮 社内の定型宛先

| 呼び方 | 展開先 |
|---|---|
| **「いつもの3人」「担当者へ」「担当に回して」** | `fuji@seifukunofuji.com, kaneko@seifukunofuji.com, kentaro@seifukunofuji.com` |

`mail_draft.py --to "いつもの3人"` と書けば自動で展開される（宛先を手打ちしない）。
金子=kaneko、健太郎=kentaro、fuji=寺下本人の控え。

---

## 📨 転送（問合せメールを社内へ回す）の定型フォーマット

ホームページの問い合わせフォームから届いた相談を社内に回すときは、**元メールを丸ごと
引用せず、要点を整理したこの形**にする（実際に運用されている書式）。

**件名**: `【転送：問合せメール】<相談内容>（<お客様名>様）`

**本文**:
```
お疲れさまです。寺下です。

ホームページのお問い合わせフォームより、<相談内容>について
ご相談が届いています。ご対応をお願いします。

■ お客様
　<氏名> 様
　<メールアドレス> ／ <電話番号>
　<住所>
　（<所属・組織があれば>）

■ ご要望
　<本文の要点を2〜4行で。お客様の言葉を活かす>

■ 受信日
　<YYYY年M月D日（曜）HH:MM>
　※ <電話等の補足があれば>
　※ ホームページのお問い合わせフォーム経由。まだ返信していません。
```

- 住所・電話・郵便番号は**元メールに書かれている分だけ**書く（推測して補わない）
- 署名は入れない（Thunderbird側の既定署名と二重になる）
- 受信日時は元メールの `Date` ヘッダ（JST換算）を使う

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

## 3️⃣ EML生成＋Thunderbird起動は `mail_draft.py` を呼ぶだけ

**EMLの組み立てコードを毎回書かない。** このスキルのフォルダに専用CLIがある。
`EmailMessage(policy=SMTP)` / `write_bytes` / `-file` 起動 / 生成後の自己検証まで
全部入っている。

### 手順（2ステップ）

**(a) 本文を UTF-8 テキストファイルに書く**（Writeツール。スクラッチパッドでよい）

本文を引数で渡さないこと。日本語・改行をコマンドラインに載せると Windows で壊れる
([[reference_windows_shell_pitfalls_hub]])。

**(b) CLIを実行する**

```bash
python "C:\ClaudeCode\.claude\skills\mail-send\mail_draft.py" --to "いつもの3人" --subject "【転送：問合せメール】…（…様）" --body-file "<本文.txtの絶対パス>"
```

主なオプション:

| オプション | 用途 |
|---|---|
| `--to` | 宛先。`いつもの3人` と書くと社内3名に展開。カンマ区切りで直接指定も可 |
| `--subject` | 件名 |
| `--body-file` | 本文のUTF-8テキストファイル（必須） |
| `--cc` | Cc |
| `--attach a.csv b.pdf` | 添付ファイル（複数可） |
| `--out` | EML出力先（既定: `C:\ClaudeCode\.mail_drafts\<本文ファイル名>.eml`） |
| `--no-open` | Thunderbirdを起動せず生成だけ（動作確認用） |

実行すると生成したEMLを**読み直して本文・添付を検証**し、`[OK] 検証通過` が出てから
Thunderbirdが開く。`[NG]` が出たら本文の文字化け・添付欠落なので、送らずに原因を直す。

**添付ファイルが本来ユーザーへ渡す成果物でもある場合**(例: 出荷報告CSV)、メール添付とは
別に[[feedback_user_check_files_downloads_and_nas]]の二重コピー(Downloads + NAS
999.一時ファイル)も忘れずに行う。

### 送信後

1. Thunderbirdの作成ウインドウで**Ctrl+E**(新しいメッセージとして編集)を押し、下書き化
2. 内容を最終確認
3. **送信はユーザーが手動で行う。Claudeは下書きを開くところまで**
4. (該当する場合)066.業務秘書等の案件管理システムで「処理済み」マーク

---

## 4️⃣ ローカル記録の更新(該当する場合)

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

- `mail_draft.py`（このフォルダ） — EML生成＋Thunderbird起動の共通CLI。**メール送信で
  Pythonを書く必要があるのは本文ファイルの作成だけ**
- [[mail-search]] — Step 0️⃣ で元メール本文・添付を取るのに使う
- [[reference_thunderbird_compose_via_eml]] — `-compose`禁止・EML方式の根拠
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

**2026-09-18 更新（転送対応・再現性強化）**: 自衛隊からの盾デザイン相談を社内3名へ
転送する作業で、(1)元メール本文を取らずにプレースホルダーで作る (2)`-compose`直叩きで
本文が空欄になる (3)「いつもの3人」の宛先が分からず手が止まる、の3点で手戻りが発生。
ユーザー指示により「メール送信」「転送」の文言でこのスキルを必ず呼び、**毎回同じ結果に
なる**よう手順化した。EML組み立てを`mail_draft.py`に固定し、Step 0️⃣(一次資料の取得)・
社内定型宛先・転送フォーマットを明記。
