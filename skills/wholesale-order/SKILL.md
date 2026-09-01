---
name: wholesale-order
description: 卸売・外部チャネル注文(産経デジタル・ふるさと納税等)をCSV/データから読み込み、GoQ受注管理システムへ登録し、出荷完了報告メールを作るまでの標準手順。052.卸売注文GoQ統合を使う。新しい取引先が増えても同じ手順を使い回す。
trigger: 産経・ふるさと納税など卸売/外部チャネルの新規注文を処理するとき
---

<!-- SKILL_VERSION: 2026-08-25_000000 -->

# wholesale-order — 卸売注文 → GoQ登録 → 出荷報告

作業フォルダ: `C:\ClaudeCode\052.卸売注文GoQ統合`（以下すべて相対パス）。
このスキルは**手順の固定化**が目的。取引先(チャネル)が増えても、パーサーだけ
差し替えれば combine/送信/検証/報告の下流はそのまま使い回せる設計。

## 全体の流れ（この順を必ず守る）

```
1. 新着チェック   intake取込
2. パース         orders/*.json (正規化注文)
3. 同梱統合       combine_shipments.py（送り先が完全一致するものだけ）
4. 検証・プレビュー submit_orders.py --payload
5. ユーザー承認   （本番書込みは毎回必ず確認する。自動化しない）
6. 送信           submit_orders.py --send <ファイルを明示>
7. 照合           GoQを読み直して一致確認（ローカル記録を信用しない）
8. 出荷完了報告   メール送信済みフォルダの実履歴と突き合わせてから作る
```

---

## 1. 新着チェック

産経デジタルは「たよれーるどこでもキャビネット」経由で自動同期される:

```bash
ls "C:/Users/user/どこでもキャビネット/SEIFUKU_FUJI/"
```

日付フォルダの中身と `intake/` を突き合わせ、**まだ intake/ に無いファイルだけ**コピーする。
他チャネル(ふるさと納税等)は受け取り経路が別なので、取引先ごとの受領方法をここに追記していく。

```bash
cp "C:/Users/user/どこでもキャビネット/SEIFUKU_FUJI/<日付>/<ファイル名>.csv" "intake/"
```

## 2. パース（チャネルごとに専用パーサー）

- 産経デジタル: `python parsers/sankei_shukka.py "intake/<ファイル名>.csv"`
  - `parsers/excluded_products.json`（他社混入の商品コード除外）と
    `parsers/carrier_map.json`（配送会社・メール便条件）を自動適用する
  - 出力は「+ 件」「除外(他社) 件」「スキップ 件」の内訳が出る。**除外0件でも油断しない**
    （新しい商品コードが増えると除外リストに載っていない可能性がある。商品名を必ず目で見る）
- ZenPlus（メール受注・2026-08-25追加）: `python parsers/zenplus_order.py`
  - 発注元は`fw-shopmaster@seifukunofuji.com`の`5.Zenplus`フォルダ（Thunderbird mboxを直接パース）
  - 届け先は毎回ZenPlus倉庫（個人宅直送ではない）。**住所末尾の受入コード（例`(OFiM12)`）は
    addr1に含めて印字する**（倉庫側の要件・同時に同梱誤爆の防止にもなる）
  - 配送は判断せず**全件クリックポスト固定**で登録（サイズ振り分けはGoQ登録後にユーザーが行う）
  - `--order-id <番号>` で特定の注文だけ処理可能
  - **新チャネル導入時は必ず「過去メールの中に既に手動対応済みのものがないか」を確認する**
    （ZenPlus初回導入時、4月〜8月の過去4件が実は手動で完了済みと判明。そのまま登録すると
    二重登録になるところだった。完了済みと確認できたものは`orders/external_done/`へ退避し、
    ステータスを`external_done`にして以後パーサーが再検出しないようにする）
- ふるさと納税/シフトプラス（メール受注・2026-08-25追加）: `python parsers/furusato_shiftplus.py`
  - 発注元は`terashita@seifukunofuji.com`の`00C.販売先/シフトプラス`フォルダ
    （シフトプラス株式会社＝呉市ふるさと納税の代行事務局、mboxを直接パース）
  - **送り状は配送業者が別途紙で持参するため、GoQからは発行しない**。GoQ登録は
    「朝のピッキングリストに1件載せる」ためだけの目的（Sankei/ZenPlusとは根本的に別の使い方）
  - メールに寄附者の氏名・住所は無い。**ship_to は制服のフジ本店のダミー住所**を使う。
    **氏名は「ふるさと納税」固定**（2026-08-25追加決定）。047の登録フローは注文者欄・
    届け先欄の両方を同じship_to辞書から埋めるため（`ORDERER_FIELDS`）、nameを変えるだけで
    注文者氏名・送り先氏名の両方に反映される。配送会社も形式的に佐川急便固定（実際には
    発行されないので実害なし）
  - **メール1通（複数商品行）をまとめて1つの受注として登録する**（寄附者ごとに分けない）
  - `--date YYYY-MM-DD`（JST基準）で特定の日のメールだけ処理可能
  - **既知の罠**: 行フォーマットは`(コード)商品名 コード N件`だが、先頭の括弧内コードと
    末尾のコードが食い違う行が実在した（2026-08-10分）。**常に先頭の括弧内コードを正とする**
    （ユーザー確定）。末尾コードは行の終端を見つける目印としてのみ使い、値には使わない
  - **既知の罠2**: 「◯月◯日以降」の絞り込みをUTC基準の日付境界で行うと、JSTでは対象日付の
    メールを取りこぼす（2026-08-04 11:45 JST着信のメールがUTC基準では08-04早朝でカットオフの
    外に出ていた）。**日付の絞り込みは必ずJSTへ変換してから比較する**
- **メールボックスを直接パースするチャネル（ZenPlus・シフトプラス等）は`tools/channel_baseline.py`の
  基準日ガードを必ず組み込む**（2026-08-25追加・必須）。理由: `furusato_shiftplus.py`を日付指定
  なしで実行し、2年分のメール履歴から21件の注文JSONを誤って一括生成する事故が実際に発生した
  （ZenPlusでも同種の事故が起きかけた）。`orders/+sent/+external_done/`の存在チェックだけでは、
  「チャネル導入前から溜まっている過去メール」を新着と誤認するのを防げない。
  - 新チャネル導入時、無引数の一括チェックは**基準日が未設定だと`BaselineNotSet`で拒否**される
    （`channel_baseline.require_baseline(channel)`）
  - 導入時に「過去メールのどこまでが既に対応済みか」をユーザーに確認し、
    `channel_baseline.set_baseline(channel, 'YYYY-MM-DD', note, set_at)` で一度だけ設定する
  - `--date`/`--order-id`など**特定の対象を明示指定**したときは基準日を経由しない
    （ユーザーが個別に指名した対象は「一括チェックで誤検出」ではないため）
  - 産経（CSVをdococabフォルダから拾う方式）は元々retention窓が短く同種のリスクが低いが、
    将来同じ手口のチャネルを追加するなら同様のガードを検討する
- 新チャネルを追加するとき: `parsers/<チャネル名>.py` を新設し、**`tools/order_model.py` の
  正規化スキーマ**（`ship_to` 9項目・`items[].code/name/qty/wholesale_unit_price`・
  `carrier`・`shipping_kind`・`order_no_token`・`memo`）に合わせて `orders/*.json` を吐けば、
  3以降の手順（combine/送信/検証/報告）はそのまま使い回せる。単価は**必ず0円固定**
  （`to_queue_entry()` が強制するので手で書く必要はない。卸単価は `wholesale_unit_price` に保持）

## 3. 同梱統合（送り先が完全一致するときだけ）

```bash
python tools/combine_shipments.py --dry-run   # まず確認（副作用なし）
python tools/combine_shipments.py             # 対象があれば実施
```

同梱時は配送会社を**必ず佐川急便に固定**する（メール便/クリックポストは単品前提のため崩れる）。

## 4. 検証・プレビュー（送信前に必ず中身を見る）

```bash
python tools/submit_orders.py --payload
```

047へ実際に送るペイロード全文が出る。**単価0円・宛先・商品コード・配送会社/種別**を目視確認する。

## 5. ユーザー承認（毎回・自動化しない）

本番のGoQ受注登録は書込み操作。プレビュー内容を提示し、**明示の承認を得てから**次へ進む。
承認が得られたら文脈台帳に記録する:

```bash
python C:/ClaudeCode/.claude/hooks/ctx_cli.py add <SID> OK "052本番登録承認: <対象を具体的に>"
```

## 6. 送信（対象ファイルを明示。省略不可の安全策）

```bash
python tools/submit_orders.py --send orders/<ファイル1>.json orders/<ファイル2>.json
```

`--send` はファイル省略を許さない（一括全部送信を誤操作で防ぐ設計）。
送信中に中断が起きた場合は `--reconcile` で状態を確定させる（`status=sending` のまま残さない）。

## 7. 照合（ローカルの「送信できた」を信用せず、GoQを読み直す）

```bash
python -c "
from tools import goq_lookup
infos, errors = goq_lookup.fetch_goq_info(['SANKEI-xxxxx', ...])
print(infos, errors)
"
```

`goq_lookup.fetch_goq_info()` は読み取り専用。宛先・配送会社・配送種別が意図通りか確認する。
**GoQのstatus値**は開発済みの語彙: `新規受付`→`振分中`→`作業中N`→`処理済`（正常完了）が基本ルート。
`N滞留中！` は要注意アラート（送り状は出ているのにステータスが進んでいない詰まりのサイン）。

### 出荷済みなのにステータスが進んでいないものを見つけたら

全登録済み注文のstatusを一括で読み、`処理済` 以外かつ tracking_no が付いているものを洗い出す
（`振分中` でtracking_no無しは単に「まだ出荷していないだけ」なので対象外）。
ステータスを「処理済」へ**手動で変更する自動化コード**は用意してある
（`047.GoQ送り状再印刷/goq/reissue_slip.py` の `_change_status()`、読み戻し確認込み・
別用途で実績あり）が、**GoQへの書込みなので実行前に必ずユーザー確認を取る**。
過去のセッションでは「やはり手動でやります」と選ばれたこともある — 自動化コードがあっても
使うかどうかは毎回ユーザー判断に委ねる。

## 8. 出荷完了報告メール（このスキルで最も事故りやすい工程）

### 8-1. 「未報告」はローカルの `reported_at` フラグだけで判断しない

**実例(2026-08-25)**: `sent/*.json` の `reported_at` が付いていても、実際には一度も
メール送信されていない注文が3件見つかった（レポートCSVを生成しただけで、そのメール自体が
下書きのまま送信されずに終わっていたため。ツールはCSV生成時点でreported_atを付けてしまう
仕様で、実際の送信完了とは連動していない）。

**必ず実施する**: Thunderbirdの送信済みフォルダ（mbox）を直接パースし、過去に**実際に
送られたCSV添付**の中身（受注番号列）を全部読み出して、それと `sent/*.json` の
`sankei.order_no`（または各チャネルの実際の受注番号）を突き合わせる。ここで「未送信」と
判明したものだけが真の未報告。false positiveがあれば該当ファイルの `reported_at` /
`reported_in` / `reported_snapshot` を削除してから報告生成コマンドを再実行する。

mboxの場所（Thunderbird）: `C:/Users/user/AppData/Roaming/Thunderbird/Profiles/<profile>/ImapMail/<server>/Sent`
(`\nFrom ` で分割してメッセージ単位にし、`email.message_from_bytes()` でヘッダ/添付CSVを読む)

### 8-2. 同梱注文の自動レポートを鵜呑みにしない

同梱注文（combine_shipments.py で1梱包にまとめたもの）は、インシデント対応などで
**一部の商品だけ後から別便になった**ケースがありうる。この場合、レポート生成ツールは
同梱注文全体に単一の送り状番号を割り当てて分割表示するため、実際とズレる。
**同梱注文を報告する前に、含まれる各サブ注文のトークン単体でも `goq_lookup` に照会し、
それぞれ本当に同じ送り状番号で正しいか確認する。**

```bash
python tools/make_sankei_report.py           # 既定=未報告分のみ（GoQから最新情報を取得して生成・自動でreported_atを付ける)
python tools/make_sankei_report.py --all      # 全件（reported_atは付けない・確認用）
```

### 8-3. メール下書きはEML方式のみ（`-compose` 直叩き禁止）

`thunderbird.exe -compose "to='...',body='...'"` は本文のURLデコードが不安定で、
生のエンコード文字列が残ったり、アカウント既定署名と二重表示になったりする実例が複数回発生
（自分の環境・TK運用者の環境の両方で再現・断念済み）。**必ずEMLを生成してThunderbirdで開き、
ユーザーがCtrl+E（新しいメッセージとして編集）で下書きに変換する方式を使う**:

```python
from email.message import EmailMessage
from email.policy import SMTP
import subprocess

msg = EmailMessage(policy=SMTP)
msg["From"] = "寺下貴之 <terashita@seifukunofuji.com>"
msg["To"] = "..."
msg["Subject"] = "..."
msg.set_content(body_text, charset="utf-8")   # 署名込みの完成形をそのまま書く
msg.add_attachment(csv_bytes, maintype="text", subtype="csv", filename="....csv")

eml_path.write_bytes(bytes(msg))   # write_text ではなくバイト書き込み
subprocess.Popen([r"C:\Program Files\Mozilla Thunderbird\thunderbird.exe", "-file", str(eml_path)])
```

- **AI作成の断り書き（「※本メールはAIが作成しています」等）は入れない**（ユーザー方針・2026-08-25）
- 本文の署名はアカウント既定の自動署名と重複しないよう、EML側にフルで書き切る
  （EML方式は「新しいメッセージ」ではなく既存メッセージの編集として開くため、
  Thunderbirdの自動署名機能が介入しない。だからこそ安定する）
- **送信はユーザーが手動で行う。Claudeは下書きを開くところまで。**

---

## 関連ファイル

- 詳細な仕様・過去のインシデント記録: `052.卸売注文GoQ統合/README.md`
- 正規化スキーマ・検証規則: `052.卸売注文GoQ統合/tools/order_model.py`
- GoQ読み取り専用照会: `052.卸売注文GoQ統合/tools/goq_lookup.py`
- 同時実行の排他制御: `052.卸売注文GoQ統合/tools/pipeline_lock.py`（combine/submitは自動でロックする）
- 長期記憶: `project_052_sankei_goq_wholesale.md` / `reference_thunderbird_compose_via_eml.md` /
  `feedback_no_ai_disclaimer_in_emails.md`（`~/.claude/projects/C--ClaudeCode/memory/`）
