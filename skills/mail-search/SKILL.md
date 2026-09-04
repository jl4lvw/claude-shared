---
name: mail-search
description: Thunderbirdローカルmbox(terashita@等の既知4アカウント)を横断して、相手先・件名・本文でメールを検索し、見つかったメールの添付ファイルを取得する標準手順。格納フォルダが未知の初見の相手先でも使える。900.ClaudeCode/mail-searchを使う。
trigger: 過去にやり取りしたメールを探したいとき、特定の相手先からのメールや添付ファイルを探して何かに使いたいとき
---

<!-- SKILL_VERSION: 2026-09-05_000447 -->

# mail-search — Thunderbirdメール横断検索

作業フォルダ: `C:\ClaudeCode\900.ClaudeCode\mail-search`(相対パスは全てここ基準)。

## できること・できないこと(重要)

**このスキルの範囲は「検索→添付取得」まで**。見つけたメールをどう使うか(転送する、
返信する、内容を要約する等)は毎回内容が変わるため、このスキルでは型にはめない
——見つけた`SearchHit`を使ってタスクごとに判断すること。「検索→転送」のような
特定パターンが今後繰り返されると分かった時点で、改めてその処理も含めたスキル化を
検討する(3回同じパターンが出たら抽象化、が目安)。

## 生まれた経緯

062.委託販売精算(しのびや案件)向けに作った`thunderbird_mail.py`は、案件専用フォルダの
中に埋もれていたため、別の相手先(例: JRサービスネット)を検索する新セッションが
この存在に気づけず、探索ロジックを一から再現しようとして罠(下記参照)を再び踏んだ
(2026-09-04)。汎用部分を`900.ClaudeCode/mail-search/`へ切り出し、スキル化することで
「メールを探して」という依頼が来たときに確実にこの手順を使わせる。

062.委託販売精算/scripts/thunderbird_mail.pyは現在このモジュールへの委譲レイヤーに
なっている(後方互換維持)。

---

## 使い方

### 1. 既知アカウントの確認

```python
import sys
sys.path.insert(0, r"C:\ClaudeCode\900.ClaudeCode\mail-search\scripts")
from accounts import ACCOUNTS

for a in ACCOUNTS:
    print(a.address, "-", a.label)
```

4アカウント(2026-09-04時点、根拠は長期記憶`reference_thunderbird_terashita_accounts.md`):
terashita@(本人メイン)・fw-shopmaster@・fw-terashita@(転送用)・ai-agent-terashita@(053用)。
どのアカウントに探している相手先のメールがあるか不明な場合、`accounts`省略で
全アカウントを対象にできる(下記)。

### 2. 検索する

```python
from search import SearchQuery, search, summarize_hit

hits = search(SearchQuery(
    from_contains="jr-servicenet",  # 差出人アドレス・表示名の部分一致(推奨)
    # subject_contains="発注",       # 件名の部分一致。単独では使わない(下記の罠参照)
    limit=10,                        # 見つかった件数だけ確認し、最初の1件を鵜呑みにしない
))
for h in hits:
    print(summarize_hit(h))
```

`SearchQuery`は`from_contains`/`subject_contains`/`body_contains`のいずれか1つ以上が必須。
`folder_hints`(既知のフォルダパス、下記参照)を省略すると**全アカウント×全フォルダを
横断検索する**(実測: terashitaアカウント85フォルダ・3.8GB規模でも1秒未満で完了。
性能面の心配は基本的に不要)。

**🔴 罠(2026-09-04発見)**: `subject_contains`だけの広い検索は、無関係なメール
(プロモーション・自動配信等)に偶然同じキーワードが入っていてヒットすることがある
(実例: 「棚卸」で検索したら`zaico`という別サービスの販促メールが先にヒットした)。
**`from_contains`(差出人アドレス・ドメイン)を必ず条件に加える**か、
`limit`を上げて複数候補を確認してから判断すること。`limit=1`の結果を無条件に
正しいメールだと信じない。

### 3. 格納フォルダが分かっている場合は絞り込む(高速化・任意)

委託先ごとに毎回決まったフォルダに届く相手先(例: 062.委託販売精算のしのびや案件)は、
`folder_hints`で先にそのフォルダだけを探すと速い(全文検索より確実でもある):

```python
hits = search(SearchQuery(
    subject_contains="棚卸",
    accounts=(terashita_account,),  # accounts.find_account("terashita@...")等で取得
    folder_hints=(("00C.販売先", "32_しのびや（天保山）"),),
))
```

### 4. 添付ファイルを取得する

```python
from pathlib import Path
from attachments import extract_attachments

saved_paths = extract_attachments(hits[0].message, Path("一時保存先"))
```

添付ファイル名は日本語を保持したまま、Windows禁止文字・パストラバーサル・NTFS
Alternate Data Stream・予約デバイス名を防ぐようサニタイズ済み(cgd Lv3/Lv5レビュー
で固めた方式、062.委託販売精算と同じ)。同名衝突時は連番を付けて両方保存する。

---

## 罠(必ず踏まえること)

**1. 振り分け専用の親フォルダ(`.sbd`のみ・平ファイルなし)**

Thunderbirdのフォルダ階層で、直下にメッセージを持たない「振り分け専用の親フォルダ」
(例: `00A.仕入`/`00C.販売先`)は、同名の平ファイルを持たず`<name>.sbd`ディレクトリの
みで存在する。単純に「`.sbd`拡張子は無視」という探索ロジックだと、この種の親フォルダ
とその配下が丸ごと探索から漏れる。`folders.py`の`find_folder()`/`walk_folders()`は
これを踏まえて実装済みなので、自分で新しくフォルダ探索ロジックを書き直さないこと。

**2. 振り分け専用フォルダ名の視覚的装飾(2026-09-04発見)**

`00A`〜`00E`のカテゴリフォルダは、実際のフォルダ名の末尾に**黒四角"■"が10個**
付いている(例: `"00C.販売先 ■■■■■■■■■■"`)。これはThunderbird側の視覚的な
装飾と見られ、`shinobiya_tenpozan.py`の`MAIL_FOLDER_PATH`のような人間が読みやすい
値(装飾なし)では厳密一致が失敗する。`find_folder()`は完全一致→前方一致の順で
フォールバックするため、装飾を意識せず素直な名前を指定してよい。

**3. 大容量mboxファイル**

terashitaのメインINBOXは960MB規模(7,948通、2026-09-04時点)。`mbox_reader.py`の
`iter_messages()`は1行ずつ読み進めるストリーミング方式で、1通分のバイト列だけを
メモリに保持する。`mailbox.mbox()`や`read_bytes()`で全文読み込みする実装を
自分で書き直さないこと(大容量アカウントでメモリを圧迫する)。

**4. 文字コードの罠**

件名・本文のcharsetラベルが実体と食い違う実例が複数ある: `windows-31j`/`x-sjis`は
`cp932`として、`gb2312`は(中国語圏メーラーが実体はgb18030範囲の漢字を含むのに
誤ラベルすることがあるため)`gb18030`として読み替える。`mbox_reader.py`の
`normalize_charset()`/`decode_mime_header()`が対応済み。

---

## テスト

`900.ClaudeCode/mail-search/tests/`に合成データでのテストがある(pytest)。
`folders.py`の`.sbd`探索・装飾名フォールバックは特に壊れやすい箇所なので、
このロジックを変更したら必ずテストを通してから使うこと:

```bash
cd "C:/ClaudeCode/900.ClaudeCode/mail-search" && python -m pytest tests/ -q
```

062.委託販売精算側にも、実データ(しのびやフォルダ)に対する回帰テストが1件ある
(`062.委託販売精算/tests/test_thunderbird_mail.py::test_find_folder_resolves_decorated_shinobiya_folder`)。

---

## 関連ファイル

- `scripts/accounts.py` — 既知アカウント一覧(4件)
- `scripts/mbox_reader.py` — mbox読取(ストリーミング)・文字コード正規化・見出しデコード
- `scripts/folders.py` — IMAP Modified UTF-7デコード・フォルダ探索(`.sbd`罠+装飾名対応)
- `scripts/attachments.py` — 添付ファイルのサニタイズ・保存
- `scripts/search.py` — アカウント×フォルダ横断検索の本体
- 長期記憶: `reference_thunderbird_terashita_accounts.md`(4アカウント構成・大容量mboxの注意)
- 委譲元(後方互換レイヤー): `062.委託販売精算/scripts/thunderbird_mail.py`
