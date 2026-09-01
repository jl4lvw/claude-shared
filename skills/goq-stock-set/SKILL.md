---
name: goq-stock-set
description: GoQシステムの在庫連携画面(stockSituation)で指定SKUの総在庫数を書き換え、連携中のモール(楽天/Yahoo!ショッピング/Eストア等)へ即座に反映する。リレーメッセージ・メール等の遠隔依頼で「在庫を◯個にして」と来たときに使う標準手順。バリエーション商品で対象が一意に決まらない場合はDeepSeekで確認メッセージを作り、依頼元(relay/メール)へ問い合わせてから実行する。023.商品マスタDBを使う。
trigger: 遠隔(relay/メール/LINE WORKS等)からGoQの在庫数変更を依頼されたとき。「在庫を20個にして」「G1021の在庫を修正して」等
---

<!-- SKILL_VERSION: 2026-08-30_153758 -->

# goq-stock-set — GoQ在庫連携画面での在庫数変更

対象システム: `023.商品マスタDB`（サーバー既定 `http://127.0.0.1:8290`）。
このスキルは **GoQシステム(stock2.goqsystem.com)の「在庫連携画面」で、指定した商品番号の
「総在庫数」欄を書き換えて「反映」する**手順を固定化したもの。反映すると、その行で連携
チェックがオンになっているモール（楽天・Yahoo!ショッピング・Eストア、紐付いていればAmazon）
へ**即座に**在庫数がプッシュされる（2026-08-25 実装・G1021/G1022で実機確認済み）。

## 🧾 このスキルと並走する自動経路(026 店頭スタッフ報告、2026-08-29新設)

本スキル(Claude/relay/メール経由の遠隔依頼)とは別に、**026.EストアDisplayPWA**
（店頭在庫表示、`https://sfuji.f5.si/estoredisplay/`）にも「現在の在庫数を報告」機能があり、
店頭スタッフが商品カード長押し→ボタンタップだけで在庫数を確定できる。こちらは**人の確認を
挟まず自動でGoQへ反映される**（ユーザー2026-08-29決定: 承認制ではなく自動処理）。

- 026は`GET /api/products/{sku}/live-mall-stock`(023, `export_inter_pwa.py`)で楽天/Yahoo!/
  Eストアのライブ在庫を取得して表示する（`server/services/live_mall_stock.py`）
- 設定した値は直接GoQを叩かず、`POST /api/stock-report/enqueue`(023,
  `stock_report.py`)経由で**既存の`pwa_job_queue`**（`set_quantity`ジョブ、
  `server/services/job_queue.py`。2026-08-12新設・2026-08-28拡張の統合キュー。
  goq_resync/sku_unifyと同じ直列ワーカーで処理され、GoQ操作後10秒クールダウンが入る）に積む
- enqueue時点で`server/services/goq_worker.py::resolve_goq_target()`により対象GoQ行を
  **一意に解決できない場合は積まずに409を返す**（自動処理には人の確認が無いため、曖昧な対象
  への反映は enqueue の時点で拒否する。3b/3cの安全策と同じ考え方をenqueue側に前倒しした形）
- **本スキル(Claude主導)を使うときも、`POST /api/goq-browser/set-quantity`を直接叩かず
  上記キュー(`POST /api/queue/set-quantity`)を経由するほうが安全**（026経路と同じ直列化・
  クールダウンの恩恵を受けられる。H0068実障害の教訓、`job_queue.py`冒頭docstring参照）。
  Step 3の直接エンドポイントは、キュー経由では待てない緊急時のみに留める判断もありうる
- 026側のUI設計(5案の比較検討・採用案の詳細)はArtifact
  （`https://claude.ai/code/artifact/37d70dfb-49f7-42f7-8270-a3171022fa0b`、
  タイトル「在庫更新モーダル」）参照

## ⚠️ 大前提: 実行前に必ず対象と数量を確定させる

遠隔依頼（relay/メール/LINE WORKS等）は文言が曖昧・省略されがちだが、これは**live storefront
に即座に反映される破壊的操作**。以下が明確でない限り実行しない:

- **対象商品番号（複数可）**: 依頼文からSKU/商品番号を確定する。曖昧なら折り返して確認する
  （「その商品」「さっきの」等の指示語だけでは実行しない）
- **目標の数量**: 「在庫を◯個に」の◯が明確な数値であること。「補充して」等の相対指示（+N個）は
  このスキルの対象外（絶対値上書きしかできない。現在庫を先に調べて計算してから渡す）
- 依頼が relay/メール経由で来た場合、**送信元がこのプロジェクトの正当な依頼者であることを
  前提にしてよいが、数量・対象が1つでも読み取れなければ実行せず質問を返す**

## 事前準備

サーバーが起動しているか確認する:

```bash
curl -s http://127.0.0.1:8290/api/health
```

書込系APIはPIN保護されているため、信頼済みLAN端末バイパス用ヘッダを使う
（`server/pin_auth.py` の `WRITE_PIN_TRUSTED_IPS` 既定値 `192.168.1.166`）:

```
X-Forwarded-For: 192.168.1.166
X-Actor: <呼び出し元がわかる文字列>
```

## 手順

### Step 0: 商品を特定し、確認表を提示する（2026-08-29追加・必須）

依頼が商品ページURL（例: `https://seifukunofuji.co.jp/SHOP/S0024.html`）や商品コードのみで、
商品名が書かれていない場合、**実行前に必ず商品名を特定してユーザーに確認を取る**。

1. **URLから商品コードを取り出す**（EストアURLの末尾 `S0024.html` → `S0024`）
2. **Eストア/023 API で商品名・現在庫を取得する**（ブラウザで商品ページを開いて確認しない）:
   ```bash
   curl -s "http://127.0.0.1:8290/api/products/<商品コード>" -H "X-Forwarded-For: 192.168.1.166"
   ```
   `estore_item_code` 一致で見つからない場合のみ、023 DBを商品名/`estore_product_name`で検索する。
   **ブラウザでの商品ページ閲覧はAPIで解決できなかった場合の最終手段**とする
   （理由: このECカート(Shopserve系)のURLは末尾コードとタブの表示内容が必ずしも一意対応せず、
   複数タブ/複数URLを同時に開く（並列navigate）と表示が競合して誤った商品名を掴みかけた実例が
   2026-08-29にあった。ブラウザを使う場合は**1タブずつ順番に**、かつ`ITEM_NO`等の隠しフィールドで
   商品コードを裏取りしてから商品名を確定する）
3. **確認表は必ず通常のチャットメッセージとして先に表示する**（対象が2件以上ある依頼では
   必ず表形式。1件でも省略しない）。例:

   商品: G2190-parent「Tシャツ【…】」(ネイビー単色)

   | SKU | サイズ | 現在庫 | 変更後在庫 |
   |---|---|---|---|
   | g2190m | M | 0 | 6 |
   | g2190l | L | 0 | 7 |

   **表をAskUserQuestionの質問文(question)の中に埋め込まない**。質問文に埋め込むと選択肢UIの
   都合で折り返し・省略が起き、かえって読みにくくなり判断を誤らせる実例が2026-08-29にあった
   （`feedback_ask_user_click_choices` メモリの追記部分を参照）。表をメッセージで見せた
   **直後に**、AskUserQuestionで「この内容で実行してよいか」を短いYes/No選択肢として聞く
   （選択肢のUI自体は使う。埋め込む中身を最小限にするだけ）。

4. ユーザーから「これで合っている」の確認が取れてから Step 1 以降を実行する
   （確認前にGoQへ反映しない。誤商品への反映は取り返しがつかない）

### Step 1: 対象の一意性を確認する（親子/バリエーション商品の場合は必須）

依頼された商品番号を023 DBで解決し、**親商品(バリエーションあり)なのに、どの子(色/サイズ)か
指定が無い**場合は、この時点で処理を止める。絶対に先頭の子や在庫が多い子を勝手に選ばない
(誤った色/サイズへの在庫反映は取り返しがつかない)。

```bash
cd "C:/ClaudeCode/023.商品マスタDB"
python -c "
import sys; sys.stdout.reconfigure(encoding='utf-8')
import sqlite3
conn = sqlite3.connect('data/productmaster.db'); conn.row_factory = sqlite3.Row
sku = '<依頼された商品番号>'
prod = conn.execute('SELECT sku, is_parent FROM products WHERE sku=? AND deleted_at IS NULL', (sku,)).fetchone()
if prod and prod['is_parent']:
    kids = conn.execute('SELECT sku, variation_color, variation_size FROM products WHERE parent_sku=? AND deleted_at IS NULL ORDER BY sku', (sku,)).fetchall()
    for k in kids: print(dict(k))
else:
    print('親商品ではない (単品、または該当なし):', dict(prod) if prod else None)
"
```

子が2件以上あり、依頼文からどれか判断できない場合は「対象が一意に決まらない場合の確認フロー」
（本ドキュメント末尾）に進み、在庫変更は実行しない。子が1件だけ、または依頼文で色/サイズが
明示されていれば、その子SKUを対象にStep 2へ進んでよい。

### Step 2（任意・推奨）: 変更前の在庫をライブAPIで確認する

対象がどのくらいズレているか / そもそも今いくつかを見せると依頼者が状況を把握しやすい。

```bash
# 楽天・Eストアはライブ取得の専用エンドポイントが無いため、023リポジトリ直下でPython経由 (読み取り専用)
cd "C:/ClaudeCode/023.商品マスタDB"
python -c "
import sys; sys.stdout.reconfigure(encoding='utf-8')
from server.services import rakuten_inventory
from server.services.estore_client import request as estore_request
mn = '<rakuten_manage_number>'  # products.rakuten_manage_number (小文字が多い)
code = '<estore_item_code>'     # products.estore_item_code (例: G1021)
print('楽天:', rakuten_inventory.get_quantity(mn, mn))
res = estore_request('GET', f'/items/{code}/stock', api_kind='manager')
b = res.body.get('stock') if isinstance(res.body, dict) else res.body
print('Eストア:', (b or {}).get('management_item'))
"
```

```bash
# Yahoo! (2026-08-25新設。ライブ取得APIが元々存在しなかったため実装した)
curl -s "http://127.0.0.1:8290/api/yahoo/stock/<SKU>"
```

### Step 3: GoQの在庫連携画面で在庫数を書き換えて反映する

#### 3a. バリエーション商品は先にGoQ側の実際のコード表記を確認する（必須）

GoQに登録されている `item_code` は、023 DBの `sku` 列（例: `g2190m`）と**表記が異なることが多い**
（例: GoQ側は `G2190-NV-M` のようにハイフン区切りの色-サイズ表記。2026-08-29 G2190で確認）。
親コードで検索して実際の表記を確認してから、その表記で `set-quantity` を呼ぶ:

```bash
curl -s -X POST "http://127.0.0.1:8290/api/goq-browser/search" \
  -H "X-Forwarded-For: 192.168.1.166" -H "X-Actor: <actor>" -H "Content-Type: application/json" \
  -d '{"item_code":"<親商品コード>"}'
```

`rows[].text` の中に `<GoQ表記> <在庫数> <GoQ表記> <在庫数> <色<>サイズ...>` の形で実際のコードが
入っている。単品ならこのStepは不要（023 SKUがそのままGoQ表記と一致することが多い）。

#### 3b. `set-quantity` 実行前に対象1件だけにヒットするか必ず確認する（必須・省略禁止）

GoQの検索は**部分一致**であるため、サイズ表記が別サイズの表記を包含していると誤爆する
（例: `G2190-NV-L` で検索すると `G2190-NV-LL` にも部分一致してヒットし、**Lを指定したはずが
LLの在庫まで一緒に書き換わる**。2026-08-29 G2190のLで実際に発生。このときはLとLLの目標値が
偶然同じだったため実害なく済んだが、値が異なっていれば誤書換だった）。

**該当しうるサイズ表記**: `L`/`LL`、`S`/`SS`、`M`/`3M`のような接頭辞関係になるもの。
実行前に必ず `POST /api/goq-browser/search` で対象コードを検索し、`row_count == 1` かつ
`rows[0]` が意図した行であることを確認してから `set-quantity` を呼ぶ。

#### 3c. 無関係な別チャネル行が一緒にヒットする場合は `row_ids` で対象行だけに絞る

同じ商品番号で検索しても、Amazon向け等の**別チャネル専用行**（例: `FJ-G2040-CO-NIHNBR-parent`。
ASIN付き、通常のマルチモール行 `G2040` とは別に存在する）が一緒にヒットすることがある
（2026-08-29 G2040で発覚。ただしGoQ側の検索結果が毎回同じとは限らず、再検索したら1件に
戻ったこともあった — GoQ側の一時的な表示状態の可能性もあるので、**row_count>=2が一度でも
出たら疑ってかかる**こと）。

この場合、`set-quantity` に対象行の `value`（`search`のrows[].value、例: `"167539"`）を
`row_ids` として渡すと、**その行の在庫数テキストだけ**を書き換え、他の行の値には触れない
（`row_ids`は2026-08-29新設。`server/services/goq_browser.py`の`set_stock_quantities()`を拡張）:

```bash
curl -s -X POST "http://127.0.0.1:8290/api/goq-browser/set-quantity" \
  -H "X-Forwarded-For: 192.168.1.166" -H "X-Actor: <actor>" -H "Content-Type: application/json" \
  -d '{"item_code":"G2040","quantity":20,"row_ids":["167539"]}'
```

- `row_ids`に含まれない行が検索結果にあっても、その行の**在庫数値は変更されない**
- ただし「選択」チェックボックスは仕様上すべての行で必須のため、除外した行も「反映」時に
  **既存の値のまま**再送信される（値そのものは変わらない。反映イベントは発生する）
- 指定した`row_ids`が検索結果に1つも含まれない場合は422エラーになる（表記/行IDのズレ検知）
- **row_count>=2に一度でも遭遇した商品番号は、以降このコマンドを再実行するときも
  念のため`row_ids`を明示的に指定する**（対象行以外への巻き込みを機械的に防ぐ）

対象SKUごとに1回ずつ呼ぶ（Selenium・単一ブラウザインスタンスのため直列。エンドポイント側で
`_reject_if_goq_busy()` により他ジョブ実行中は409で弾かれる）:

```bash
curl -s -X POST "http://127.0.0.1:8290/api/goq-browser/set-quantity" \
  -H "X-Forwarded-For: 192.168.1.166" -H "X-Actor: <actor>" -H "Content-Type: application/json" \
  -d '{"item_code":"<SKU or 楽天管理番号>","quantity":<新しい在庫数>}'
```

- `item_code` は検索キー。GoQ側の登録表記と大文字/小文字が完全一致しないとヒットしないが、
  `goq_worker._search_with_case_retry` が元表記→大文字→大文字の順に自動リトライするため、
  呼び出し側は基本的にSKU表記のまま渡してよい（H0068の教訓、2026-08-10）
- レスポンスの `row_count` が対象行数。**1より大きい場合は誤書換の可能性**（3b/3cを参照。
  この時点で気づいた場合は、他SKUの値が意図せず変わっていないか直後に検索して確認する）
- `notices` にGoQの確認ダイアログ文言が入る。正常なら
  `"編集内容を反映します。よろしいですか？※連携チェックがオンの商品はモールに在庫数が反映されます"`
  のような文言が返る
- 検索0件・ログイン失敗時は 422 (`GoqWorkerError`) で反映されずに終わる（安全側）

複数SKUをまとめて依頼された場合は、この呼び出しを対象SKUの数だけ順番に繰り返す。

### Step 4: 反映結果をライブAPIで確認する

Step 2と同じ方法で、楽天・Yahoo!・Eストアそれぞれが新しい数量になっているか再確認する。
Selenium側は「反映しました」で終わるが、**モール側APIまで見て初めて実際に反映されたと言える**
（AGENTS.md「反映系の操作は結果を検証して終える」原則）。

#### Eストアだけ反映されない場合（既知の障害・2026-08-28確認）

楽天・Yahoo!は新しい数量になったのに**Eストアだけ古い値のまま**という状態が実機で複数回発生している
（G2221・G2220で確認）。GoQの検索結果画面を開くと、画面上部のエラーバナーに
`【エラーレベル8】Eストア : 在庫取得エラー(<コード>) (日時)` が記録されている
（過去にも別商品(G1857b/G1983/h0036/G2219/G2149/G2177等)で同種のエラーが記録されており、
一過性ではなく時々発生する既知の不具合。023側のコードの問題ではなく、GoQ⇔Eストア間の
同期エラー）。

対処手順:
1. `set-quantity` をもう一度呼んで1回だけ再試行する（同じitem_code/quantityで再実行するだけでよい。
   `_search_with_case_retry`により検索から反映までやり直される）
2. 再試行してもEストアだけ食い違うままなら、**023からEストアへ直接プッシュ**してフォールバックする
   （楽天/Yahoo!には触れず、Eストアだけを個別に是正する）:
   ```bash
   cd "C:/ClaudeCode/023.商品マスタDB"
   python -c "
   import sys; sys.stdout.reconfigure(encoding='utf-8')
   import urllib.request, json
   BASE = 'http://127.0.0.1:8290/api'
   HEADERS = {'X-Forwarded-For': '192.168.1.166', 'X-Actor': 'claude-code', 'Content-Type': 'application/json'}
   def get_json(path):
       req = urllib.request.Request(BASE + path, headers=HEADERS)
       with urllib.request.urlopen(req) as resp: return json.load(resp)
   def call(method, path, body=None, extra_headers=None):
       h = dict(HEADERS)
       if extra_headers: h.update(extra_headers)
       data = json.dumps(body).encode('utf-8') if body is not None else None
       req = urllib.request.Request(BASE + path, data=data, headers=h, method=method)
       with urllib.request.urlopen(req) as resp: return resp.status, json.load(resp)
   sku = '<SKU>'
   quantity = <新しい在庫数>
   prod = get_json(f'/products/{sku}')
   status, body = call('PATCH', f'/products/{sku}', {'stock': quantity}, extra_headers={'If-Match': str(prod[\"version\"])})
   print('PATCH ->', status, body.get('stock') if isinstance(body, dict) else body)
   status, body = call('POST', f'/estore/sync-stock/{sku}')
   print('sync-stock ->', status, json.dumps(body, ensure_ascii=False) if isinstance(body, dict) else body)
   "
   ```
   `POST /estore/sync-stock/{sku}`（`server/routers/estore.py`）は **023DBの`products.stock`列を
   そのままEストアへ上書き**する既存の直接プッシュ経路。GoQを経由しないため、GoQ⇔Eストア間の
   同期エラーの影響を受けない。この手順は結果として023ローカルDBの`stock`もその値になるため、
   Step 5は不要になる（合わせた状態で完了する）
3. 直接プッシュ後、Eストアのライブ値を再確認して3モール一致を最終確認する

### Step 5: 023ローカルDBを合わせる（2026-08-29から確認不要・毎回自動実行）

023の`products.stock`列はGoQ在庫連携とは独立した値で、**自動では追従しない**
（このシステムは在庫を各モールへ押し出すだけで、売上による在庫減少を引き戻す仕組みがない
—2026-08-25 会話で確認済みの既知の制限）。**合わせるかどうかの確認は不要。毎回自動で実行する**
（2026-08-29 ユーザー明示指示:「毎回自動的にローカルDBの在庫も更新するべき。合わせない理由が
ない」。旧版はここで毎回確認していたが、その運用は終了）。

対象SKUの数だけ以下を繰り返す:

```bash
# 1) 現在の version を取得 → 2) If-Match で stock を PATCH
curl -s "http://127.0.0.1:8290/api/products/<SKU>" -H "X-Forwarded-For: 192.168.1.166"
curl -s -X PATCH "http://127.0.0.1:8290/api/products/<SKU>" \
  -H "X-Forwarded-For: 192.168.1.166" -H "X-Actor: <actor>" -H "If-Match: <version>" \
  -H "Content-Type: application/json" -d '{"stock": <新しい在庫数>}'
```

### Step 6: 026.EストアDisplayPWA（店頭在庫表示）へ反映する（2026-08-29追加・必須）

`https://sfuji.f5.si/estoredisplay/`（サーバー既定 `http://127.0.0.1:8294`）は、
GoQ/023とは**別に**Shopserve APIを直接叩いて在庫を表示している独立キャッシュ。GoQで
在庫を変更しても自動では反映されないため、対象SKUごとに単品即時同期APIを呼ぶ:

```bash
curl -s -X POST "http://127.0.0.1:8294/api/items/<コード>/refresh-stock"
```

- **単品**の場合、`<コード>`は023の`sku`（= `estore_item_code`）でよい。
- **バリエーション商品（親子）の場合、`<コード>`は子SKUではなく親の`estore_item_code`
  （023 DBの `estore_item_code` 列。例: `G2190`）を渡す**。026 PWAはShopserveの商品コード単位で
  1行しか持たないため、子SKU（例: `g2190m`）を渡すと404になる（2026-08-29 G2190で確認）。
- 単品管理商品（`management_type: "Item"`）は`stock_quantity`を即時更新する（0.5〜1秒程度）。
  応答例: `{"item_code":"S0024","management_type":"Item","stock_quantity":100,"updated":true}`
- バリエーション管理商品（`management_type: "Variation"`）は`variation_patterns`(サイズ別
  `in_stock`)と`variation_titles`を即時更新する（2026-08-29対応。カードの在庫あり/なし判定は
  variation_patternsがある商品ではそちらを優先して見るため、これでカードに反映される）。
  応答例: `{"item_code":"G2190","management_type":"Variation","updated":true,"variation_patterns":[{"pattern":["ネイビー","M"],"in_stock":true,"price":2900},...]}`
- `updated:false`は「Shopserve側の値が既にDBと一致していた」ことを示す（正常。エラーではない）
- このPWAには「欠品」「販売再開」という独立したステータスは存在しない。フロントが
  `stock_quantity`の値からその場で在庫あり/なしを計算する仕組みのため、`stock_quantity`を
  更新するだけで欠品→販売再開の見た目は自動的に反映される（別途フラグを立て直す操作は不要）
- 対象SKUが複数ある場合は、この呼び出しをSKUの数だけ順番に繰り返す
- 404が返る場合はこのPWAの検索対象外（非公開/削除済み等）の商品。エラーではなく無視してよい

## 既知の制限（実装未対応・遭遇したら手動対応するか拡張を検討）

- **親子(バリエーション)商品は未検証**: `set_stock_quantities()` は検索結果画面にある
  `input[name^="stock["]` を**全部同じ数量に書き換える**実装。単品(G1021/G1022)では1行=1入力欄
  だったが、親子商品で複数行(色/サイズごと)にヒットする場合、全バリエーションが同じ数量に
  上書きされる。Step 1 の023 DB確認で子SKUまで一意に絞れていれば通常は1行のはずだが、
  念のため `set-quantity` 実行前に `POST /api/goq-browser/search` で `row_count` を確認し、
  2以上なら実行せず内容を見直す（GoQ側の登録状態が023と食い違っている可能性がある）
- **並列実行しない**: Selenium は単一ブラウザインスタンス前提（`goq_worker._lock`で直列化済み
  だが、多数SKUを一度に依頼された場合は焦って並列化しない。順番に1件ずつ）
- Amazonが連携されている商品は、連携チェックが入っていれば同時に反映される
  （楽天/Yahoo/Eストアと同様。個別に確認したい場合はStep1でAmazon側も見る）

## 対象が一意に決まらない場合の確認フロー（バリエーション商品・2026-08-25追加）

Step 1 で親商品なのに子(色/サイズ)を特定できなかった場合の手順。**推測で1件を選んで進めない。**
遠隔（relay/メール）から来た依頼は、依頼が来た経路の発信者へ問いかけ、回答を待ってから
Step 2以降を実行する。

### A: 候補一覧を集める（Step 1 のクエリ結果をそのまま使う）

Step 1 で取得した `variation_color` / `variation_size` の一覧が候補。SKUやバリエーションIDは
依頼者に見せない（現場スタッフには意味が伝わらない・誤操作のもと）。

### B: DeepSeekで確認メッセージを作成する（`--role message`）

`deepseek_coder.py` に2026-08-25新設した `message` ロールを使う。SKU等の内部識別子を使わず、
依頼者が回答しやすい短い日本語の確認文を生成する専用プロンプト:

```bash
mkdir -p "C:/tmp-ai"
cat > "C:/tmp-ai/ds_message_input.txt" <<'EOF'
以下は在庫変更依頼で対象が一意に決まらなかったケースです。この情報をもとに、依頼者へ返す確認メッセージを作成してください。

[元の依頼]
<依頼文をそのまま貼る>

[候補一覧]
<Step 0 で取得した色/サイズ等を依頼者にわかる形で列挙>
EOF
python "C:/ClaudeCode/.claude/tools/deepseek_coder.py" --role message "C:/tmp-ai/ds_message_input.txt"
```

出力はそのまま送信本文として使ってよい（不自然なら手直しする）。1回あたり数円未満。

### C: 依頼が来た経路に応じて送り返す

**relayメッセージ経由の依頼**: `relay` スキルで元スレッドへ返信する。

```
/relay send --to <元の発信者user_id> --thread <元のthread_id> --type reply "<Step Bの本文>"
```

すぐ返事がもらえそうなら `/relay wait --thread <TID> --holder <holder>` でその場で待つ。
時間がかかりそうなら `--reserve` + `--reserve-ttl` で長めに予約し、
`/relay wait --keep` で粘る（詳細は `relay` スキル参照）。回答が来たら、指定された
子SKUで Step 2 以降を実行する。

**メール経由の依頼**: **このフローに限り自動送信してよい**（2026-08-25 ユーザー明示許可）。
`053.ai-agentメール` の設定済みメールアカウント (`ai-agent@...`、制服のフジ名義) を流用する
（送信ヘルパー `send_reply_mail()` を同プロジェクトの依頼管理schema非依存の汎用関数として
新設・2026-08-25）。**この許可は本フロー専用**であり、他の業務メール(例: 052の出荷完了報告)は
引き続き `reference_thunderbird_compose_via_eml` メモリの下書き専用ルールに従うこと
（誤って他フローまで自動送信化しない）。

```bash
cd "C:/ClaudeCode/053.ai-agentメール"
python -c "
import sys; sys.path.insert(0, 'scripts')
from config import load_mail_config
from mailer import send_reply_mail

cfg = load_mail_config()
msg_id = send_reply_mail(
    cfg,
    to_addr='<元の送信元アドレス>',
    subject='Re: <元の件名>',
    body_text='''<Step Bの本文>''',
    in_reply_to='<元メールのMessage-IDヘッダ>',   # あれば必ず付ける (スレッド紐付け)
    references='<元メールのMessage-IDヘッダ>',
)
print('sent:', msg_id)
"
```

- `in_reply_to`/`references` は元メールの `Message-ID` ヘッダの値をそのまま使う
  （相手のメールクライアントで同じスレッドに表示されるようにするため。無ければ省略可）
- 本文にAI作成の断り書きは入れない（`feedback_no_ai_disclaimer_in_emails` メモリ参照）
- 送信後は「確認メールを送信しました」とユーザーへ報告する（何を誰に送ったか明示する）
- 返信が届くまで在庫変更は実行しない。返信は通常のメールチェック運用（`/m` 等、または
  `053.ai-agentメール` の `check-replies` 相当の仕組みが整備され次第そちら）で拾う

### 厳守事項

- 曖昧なまま推測で1件を選んで実行しない（誤商品・誤バリエーションへの在庫反映は取り返しがつかない）
- 依頼者への確認メッセージにSKU・バリエーションID等の内部識別子を出さない
  （`message` ロールが自動で避けるが、手直しする場合も踏襲する）
- 回答待ちの間、他の(曖昧でない)依頼の処理は妨げない。この確認だけ保留にして先に進んでよい

## 実装ファイル

- `server/services/goq_browser.py` — `GoqBrowser.set_stock_quantities()`
- `server/services/goq_worker.py` — `set_quantity()` / `_search_with_case_retry()`
- `server/routers/goq_browser.py` — `POST /api/goq-browser/set-quantity`
- `server/services/yahoo_shopping_api.py` — `get_stock()` (Yahoo!ライブ在庫取得、2026-08-25新設)
- `server/services/yahoo_api_sync.py` — `get_stock_for_sku()`
- `server/routers/yahoo.py` — `GET /api/yahoo/stock/{sku}`
- `.claude/tools/deepseek_coder.py` — `ROLE_PROMPTS["message"]` (依頼者向け確認文生成、2026-08-25新設)
- `053.ai-agentメール/scripts/mailer.py` — `send_reply_mail()` (確認メール自動送信、2026-08-25新設)
- `026.EストアDisplayPWA/server/routers/items.py` — `POST /items/{item_code}/refresh-stock`
  (単品即時在庫同期、2026-08-29新設。タスク名 `EstoreDisplay-API-Prod`、ポート8294。
  当初は単品管理商品のみ対応、同日中にバリエーション管理商品(`variation_patterns`即時更新)にも対応)
- `026.EストアDisplayPWA/server/routers/items.py` — `POST /sync/trigger?with_variations=true`
  (全件sync後にvariations同期も続けて実行するオプトインパラメータ、2026-08-29新設。既定false)
- `023.商品マスタDB/server/services/goq_browser.py` — `GoqBrowser.set_stock_quantities()`
  に`row_ids`引数を追加（2026-08-29新設。対象行だけ在庫数を書き換える）
- `023.商品マスタDB/server/services/goq_worker.py` — `set_quantity()`に`row_ids`引数を追加
  （検索結果に含まれない行IDを渡すとGoqWorkerError）
- `023.商品マスタDB/server/routers/goq_browser.py` — `POST /set-quantity`のbodyに
  `row_ids`（文字列配列、任意）を追加。タスク名 `ProductMaster-API-Prod`、ポート8290
- `023.商品マスタDB/server/services/goq_worker.py` — `resolve_goq_target(search_code, color,
  size)`新設（2026-08-29）。行テキストをトークン分割して完全一致で1件に絞り込む安全な解決関数。
  G2190のL/LL部分一致事故・G2040の多重チャネル事故を踏まえ、人の確認を挟まない自動処理
  (026経路)向けに新設。GoqWorkerErrorで曖昧さを検知する
- `023.商品マスタDB/server/services/stock_report_queue.py` — `enqueue_stock_report()`新設
  （2026-08-29）。026からの報告をresolve_goq_target経由でrow_ids解決してからjob_queueへ積む
- `023.商品マスタDB/server/services/live_mall_stock.py` — `fetch_live_mall_stock()`新設
  （2026-08-29）。楽天/Yahoo!/Eストアのライブ在庫を集約取得。026のitem_code(estore_item_code)
  と023のsku両対応
- `023.商品マスタDB/server/routers/stock_report.py` — `POST /api/stock-report/enqueue`新設
  （2026-08-29）。認証はINTER_PWA_API_KEY(X-API-Key)、WRITE_PINではない
- `023.商品マスタDB/server/routers/export_inter_pwa.py` — `GET
  /api/products/{sku}/live-mall-stock`新設（2026-08-29）
- `023.商品マスタDB/server/services/job_queue.py` — `_dispatch()`のset_quantityジョブが
  payload.row_idsを`goq_worker.set_quantity()`へ受け渡すよう拡張（2026-08-29）
- `026.EストアDisplayPWA/server/routers/stock_update.py` — `GET /api/stock-update/{sku}/live`
  ・`POST /api/stock-update/enqueue`・`GET /api/stock-update/status`新設（2026-08-29）。
  いずれも023へのプロキシ
- `023.商品マスタDB/server/services/rakuten_inventory.py` — `get_quantities_bulk(entries)`
  新設（2026-08-30）。Inventory API 2.1 `POST /inventories/bulk-get`
  （manageNumber+variantIdを最大1000件/回でまとめて取得。楽天側がmanageNumberを
  小文字化して返すため照合は小文字で行う）。`live_mall_stock.py`の1バリエーションずつの
  `get_quantity()`ループをこれに置き換え、G1909(24バリエーション)で約26秒→約2秒に短縮
- `026.EストアDisplayPWA/pwa/app.js` — `_refreshItemStockCard(itemCode)`新設
  （2026-08-30）。`POST /items/{item_code}/refresh-stock`を呼び`allItems`内の
  `variation_patterns`/`stock_quantity`を更新。`pollStockJobs()`と
  在庫数更新モーダルの成功確認、両方の「全ジョブ成功」タイミングから呼ばれる
- `026.EストアDisplayPWA/pwa/app.js` — `renderDetail(p)`冒頭に大きい報告ボタン追加、
  `_onStockUpdateQueueClick()`を`_confirmStockJobsInBackground()`に分離して
  モーダルクローズをブロックしないよう修正（2026-08-30）
- `026.EストアDisplayPWA/pwa/app.js` — `_renderStockTable()`・`_sizeRank()`・
  `_sortStockVariants()`新設（2026-08-30）。在庫数更新モーダルを一覧表表示に全面置換
  (列: サイズ|数量|ボタン類)。`_paintStockCard()`/`_navigateStockCard()`/
  `_stockDotsOrBar()`/`_wireStockUpdateSwipe()`/`_stockMallDetail()`は削除
- `026.EストアDisplayPWA/pwa/index.html` — `#stockUpdateStage`/`#stockUpdateInner`を
  `#stockUpdateTable`に置換（2026-08-30）
- `026.EストアDisplayPWA/pwa/style.css` — `.stock-wizard-*`/`.stock-progress*`/
  `.stock-vname`/`.stock-mall-detail`/`.stock-bignum-input`/`.stock-big-steppers`/
  `.stock-swipe-hint`を削除し`.stock-table-*`一式を新設（2026-08-30）
- `023.商品マスタDB/server/services/live_mall_stock.py` — `make_estore_virtual_sku()`/
  `parse_estore_virtual_sku()`/`_fetch_estore_variation()`新設、`fetch_live_mall_stock()`に
  帽子等「子SKU間でvariation_color/sizeが全く同じ」曖昧ケースの検出+Eストア
  バリエーション優先分岐を追加（2026-08-30）。仮想SKUはurlsafe base64符号化
  (`026/server/routers/stock_update.py`の`^[A-Za-z0-9._-]{1,64}$`検証を満たすため必須)
- `023.商品マスタDB/server/services/stock_report_queue.py` —
  `resolve_stock_report_target()`の先頭で仮想SKUを検出し、通常経路とは別に
  `resolve_goq_target_by_pattern()`で解決するよう分岐追加（2026-08-30）
- `023.商品マスタDB/server/services/goq_worker.py` — `resolve_goq_target_by_pattern()`
  新設（2026-08-30）。`"検索コード<>値"`の並びを部分一致で探す、
  `resolve_goq_target()`より厳密な絞り込み（商品タイトルにバリエーション値が
  複数列挙されている場合、単純トークン一致では絞り込めないため）
- `026.EストアDisplayPWA/pwa/index.html` — `#btnReportStock`(72px、既存比1.5倍)・
  `#stockUpdateModal`新設（2026-08-29）
- `026.EストアDisplayPWA/pwa/app.js` — `openStockUpdateModal()`以下、フィルタ/横スワイプ/
  巨大入力欄ウィザードの一式を新設（2026-08-29）。1モーダル=1商品(バリエーションはまとめて
  報告可、別商品は不可)。実機(Claude in Chrome)で単品(G0068)・バリエーション(G1909)とも
  動作確認済み。同一商品への重複同時リクエストで502/503になりうる点は既知（重畳しない前提）
- `023.商品マスタDB/server/services/stock_report_queue.py` — `resolve_stock_report_target()`
  （Selenium検索を含むブロッキング解決のみ）と`enqueue_stock_report()`（同期・非to_thread前提）
  に分離（2026-08-29修正）。**理由**: 当初`enqueue_stock_report()`全体を
  `asyncio.to_thread`でラップしていたが、内部の`job_queue.enqueue()`は
  `asyncio.get_event_loop().create_task()`でワーカーを起動する設計のため、
  別スレッドから呼ぶとイベントループが無く500エラーになる実障害があった
  （G2085の実機E2Eテストで発見。ブラウザ経由では別リクエストが偶然ワーカーを
  起こしていたため見た目上動くこともあり、発見が遅れた）。**教訓**: `job_queue.enqueue()`
  を呼ぶ処理は絶対に`asyncio.to_thread`でラップしない。ブロッキング処理と分離すること
- G2085で実機E2Eテスト完了（2026-08-29、ユーザー立ち会いで実施）: 026 UI「+5→キューに追加」
  → 023キュー(job_queue) → GoQ(0→5) → Yahoo!(0→5)・Eストア(0→5)まで反映確認、023ローカルDBも
  同期。楽天は未登録(対象外)。一連の経路が実際に動作することを確認済み
- `026.EストアDisplayPWA/pwa/app.js` — `_wireStockUpdateSwipe()`修正（2026-08-29、
  cgd Lv3で原因特定）。**実機バグ**: タッチデバイスでステッパー(0/-1/+1/+5)等が無反応。
  原因は`pointerdown`時点で無条件に`stage.setPointerCapture()`していたため、以後の
  `pointerup`が`stage`へretargetされ、clickが「pointerdown/pointerupターゲットの最近
  共通祖先」で発火する仕様上、ボタン単体のclickリスナーが呼ばれなくなっていた
  （開発機のマウス操作・`element.click()`はPointer Eventsの経路を通らないため再現しなかった）。
  対策: (1) ボタン・input等の操作要素上の`pointerdown`はスワイプ判定に一切入れない
  (2) それ以外の領域でも実際に横方向へ8px超動くまでcaptureしない (3) `pointerId`を
  `pointermove`/`pointerup`/`pointercancel`で照合し複数ポインタの混線を防ぐ
  (Codex Step C再レビューで発見)。CSSにも`.stock-big-steppers button`等へ
  `touch-action: manipulation`を追加。実機同等の検証は、Chromeの`computer.left_click`や
  `element.click()`では**この種のバグを再現できない**（Pointer Eventsの経路を通らないため）
  ことが分かったため、`new PointerEvent(..., {pointerType:'touch'})`を`dispatchEvent`し
  `stage.setPointerCapture`をフックしてcapture呼び出し回数を直接検証する方式で確認した
  （タップ時0回・スワイプ時1回・別pointerIdのmove混入は無視、を実機ブラウザで確認済み）
- **🚨 026 PWAデプロイで`?v=`キャッシュバスター4箇所の更新を1セッション丸ごと忘れていた**
  （2026-08-29発覚）。今回のセッションでボタン追加・モーダル新設・タッチ修正と何度も
  app.js/style.css/index.htmlを編集したが、`feedback_pwa_deploy_rules`メモリの
  「タイムスタンプ4箇所」更新を一度も行わず、ブラウザが古いapp.jsをキャッシュしたまま
  だったため、タッチ修正の実機検証で「まだ直っていない」ように見えた（実際は修正コード自体は
  正しく、検証環境がキャッシュを掴んでいただけ）。**教訓**: 026配下の`pwa/*.{html,js,css}`を
  編集したら、コード変更の内容によらず**その回の最後に必ず**
  `index.html`の`style.css?v=`/`app.js?v=`/`sw.js?v=`、`app.js`の`APP_BUILD_TIME`、
  `sw.js`の`CACHE_NAME`の5箇所（`date +%Y%m%d_%H%M`で実時刻取得）を更新すること。
  goq-stock-set本体の変更(023側)には影響しないが、026側を触るたび必ず思い出すこと
- **026/023とも、`server/routers/*.py`にエンドポイントを追加・変更したら`pwa/*`とは別に
  必ずAPIサーバー(タスク)を再起動すること**（2026-08-29発覚）。反映状況ポーリング機能で
  `026/server/routers/stock_update.py`に`GET /stock-update/status`を追加した際、
  026は静的配信のみでCaddyが直接pwaを配信する（026 API再起動不要と誤解しやすい）ため
  再起動を後回しにしたところ、curlで直接叩いても`{"detail":"Not Found"}`が返り続けた。
  静的アセット(`pwa/*.{html,js,css}`)の変更は`?v=`キャッシュバスターで足りるが、
  **`server/routers/*.py`・`server/services/*.py`等のAPIコード変更はタスク再起動が別途必須**
  （ProductMaster-API-Prod / EstoreDisplay-API-Prod、port→PID→taskkill→Start-ScheduledTaskの
  手順は本ドキュメント各所で既出）。「PWA側しか触っていないから再起動不要」と早合点しないこと
- 反映状況の追跡機能(2026-08-29新設): `026.EストアDisplayPWA/pwa/app.js`の
  `stockPendingMap`（localStorage永続化、キー`estore-stock-pending-jobs`）が
  item_code→未解決ジョブの対応を保持し、(1) カード左下に⏳/❌バッジ表示
  (2) ヘッダー「📦反映状況」ボタンで即時再確認 (3) 在庫数更新モーダル内で
  キュー投入後3秒間隔・最大60秒ポーリングして成功/失敗をその場に表示、の3経路が
  同じ`_fetchStockJobStatuses()`/`_reconcileStockPendingMap()`を共有する設計。
  023側は`GET /api/stock-report/status?ids=1,2,3`(`job_queue.get_jobs_by_ids()`)で
  ジョブIDをまとめて確認できる。G2180(2→3)で実機E2E確認済み（約5.6秒で成功検出・
  モーダル自動クローズまで確認)

### 2026-08-30 の改修 (実機報告4件への対応)

- **🐛 キュー成功後もカードの「一部欠品」表示が消えない不具合を修正**。原因:
  ジョブ成功時に`stockPendingMap`から該当item_codeを消してバッジを消すだけで、
  カードの在庫あり/一部欠品判定に使う`items.variation_patterns`(026ローカルDB)
  自体は更新していなかった(pending記録が消える=UI上「反映済み」に見えるが、
  実データは古いまま)。修正: `app.js`に`_refreshItemStockCard(itemCode)`を新設し、
  `POST /items/{item_code}/refresh-stock`(026、2026-08-29新設・既存)を呼んで
  `allItems`内の該当商品の`variation_patterns`/`stock_quantity`を実際に取り直す処理を、
  (1) 定期ポーリング`pollStockJobs()`が全ジョブ成功でpendingを削除する瞬間
  (2) モーダル内バックグラウンド確認処理が全件成功した瞬間、の両方に追加した。
  **教訓**: 「pendingバッジが消えた」と「表示に使う実データが更新された」は別イベント。
  反映確認の設計をするときは、バッジ管理のライフサイクルと実データ更新のライフサイクルを
  混同しないこと（`_updateStockSyncButton()`はバッジだけ、`render()`前に必ず
  `_refreshItemStockCard()`を挟む）
- **📦 詳細モーダル(通常クリック)の最上部にも大きい報告ボタンを追加**。
  `app.js`の`renderDetail(p)`冒頭(ツールバーより前)に、他ボタンの約3倍サイズ
  (`padding:18px; font-size:19px`)の`📦 現在の在庫数を報告`ボタンを設置。
  クリックで詳細モーダルを閉じて`openStockUpdateModal(p)`を直接開く。長押しの
  報告モーダル(`#btnReportStock`)は従来通り併存（経路が2つになっただけで置き換えではない）
- **🐛 在庫数更新モーダルが「固まって閉じられない」不具合を修正 → さらに即クローズ化**。
  原因: 「キューに追加」成功後、`stockUpdateBusy=true`のまま最大60秒の反映確認
  ポーリングをモーダル内で`await`し続けており、その間`stockUpdateBusy`を見ている
  `closeOneModal()`が×ボタン・外側タップを全て拒否していた（設計上はキュー投入時点で
  `stockPendingMap`に登録済みなので閉じても追跡は継続できるはずが、UIだけ道連れで
  ブロックしていた）。修正: `_onStockUpdateQueueClick()`をキュー投入(Step1)とその後の
  反映確認(`_confirmStockJobsInBackground()`、新設)に分離し、`stockUpdateBusy`は
  Step1完了時点で`false`に戻す。バックグラウンド確認処理は
  `stockUpdateActiveItemCode===itemCode && モーダルが非hidden`を毎回チェックしてから
  `resultEl`/`btn`/`_updateStockFooter()`を触る(閉じた後・別商品に切替後に古い結果で
  上書きしないためのガード)。`stockPendingMap`更新・`_refreshItemStockCard()`・`render()`
  はガード無しで常に実行する(グローバルな副作用なので当然続ける)。
  **2026-08-30さらに変更**: ユーザーから「キューに追加を押したらもう用は無いので
  すぐ閉じてトーストだけでよい」と明示指示があり、1件でもキューに積めた時点で
  `_closeStockUpdateModal()`を即座に呼ぶよう変更（反映確認待ちのモーダル内表示は廃止）。
  結果は`setStatus()`(画面下部ステータスバー、このアプリの実質的なトースト)に
  `✅ N件の在庫変更を予約しました`として一度だけ出す。反映確認自体は
  `_confirmStockJobsInBackground()`が変わらずバックグラウンドで継続し、カードバッジ・
  ヘッダーの「反映状況」ボタンで後から確認できる。全件その場で即時失敗(enqueue自体が
  エラー)の場合のみモーダルは開いたままエラー内容を表示する(積めたものが無いため)
- **⚡ 楽天のバリエーション商品取得を26秒→2秒に高速化**。原因: 楽天だけ
  Inventory API 2.0の`GET .../variants/{vid}`をバリエーション1件ごとに呼んでおり、
  かつ429対策で呼び出し間隔を意図的に1.1秒空けている(`rakuten_client.py`の
  レート制御。これ自体は正しい安全策で変更しない)ため、G1909(24バリエーション)で
  実測約26秒かかっていた。当初「楽天の商品単位まとめ取得APIは無い」と結論して
  ユーザーに報告したが、**ユーザーが楽天のRMS WEB SERVICEマニュアルを自分で確認し、
  `POST /es/2.1/inventories/bulk-get`(最大1000件、manageNumber+variantIdの配列を
  渡すと在庫数をまとめて返す)という実在のエンドポイントを見つけて指摘**。
  `rakuten_inventory.py`に`get_quantities_bulk(entries)`を新設し、
  `live_mall_stock.py`の1バリエーションずつのループ呼び出しを1商品1回のbulk呼び出しに
  置き換えた。**教訓**: 「既存コードのコメント・実装が"実機検証で確定"と書いていても、
  それは当時調べた範囲の話」であり、書き込み系(bulk-upsert)しか実装されていないからと
  いって読み取り系(bulk-get)も無いとは限らない。コードベース内の調査だけで
  「存在しない」と断定せず、公式マニュアルへの到達手段があるなら先に確認を促すべきだった
  （ユーザー確認後、実機E2Eで24バリエーション取得が1.99秒に短縮したことを確認済み）

### 2026-08-30 その2: 一覧表表示へ全面置換 + 送信後は即クローズ (ユーザー指示)

在庫数更新モーダルの表示方式を、1件ずつ横スワイプで切り替えるウィザードから、
**サイズ|数量|ボタン類(0/-1/+1/+5)の3列の一覧表**へ全面置換した(ユーザーが両案を
比較した上で「一覧表に完全置き換え」を選択。旧ウィザード・横スワイプ・進捗ドット表示は
すべて削除)。要点:

- **サイズの並び順**: DBのsku文字列順(≒登録順)をそのまま使うと、商品によっては
  アルファベット順に近い並び(3L→L→LL→M→S)になってしまう実例があったため、
  `_sizeRank(sizeLabel)`(基本サイズ表 `['XXS','XS','SS','S','M','L','LL','XL']` +
  `\d+L`パターンを拡張サイズとして扱う)で明示的に小さい順にソートする
  (`_sortStockVariants()`、カラーは初出順を維持したままカラー内でサイズ順)。
  ソートは`openStockUpdateModal()`でライブ在庫取得直後に1回だけ適用し、以後の
  フィルタ切替・再描画はこの並び順を引き継ぐ
- **列は必ず「サイズ|数量|ボタン類」の3列のみ**(ユーザー指定)。旧ウィザードにあった
  楽天/Yahoo!/Eストアの内訳表示(`_stockMallDetail()`)はこの一覧表には出さない
  (列定義から意図的に除外。3モール不一致の警告表示が欲しくなったら再検討)
- **色フィルタ「すべて」で複数カラー混在時のみ**、カラーごとの小見出し行を挟む
  (サイズだけの列が同じ値で並んでも迷わないための補助。列定義自体は3列のまま)。
  単一カラーに絞ったときは小見出しなしでサイズ行だけが並ぶ
- **変更された数量は赤字・太字・やや大きいフォント**(`.stock-table-qty-value.dirty`)
  で表示。数量はボタン操作でのみ変わる(フリー入力欄は無し。旧ウィザードの
  `<input>`巨大数字入力は廃止)
- 「キューに追加」ボタンは列とは別に画面下部の共通フッターのまま(1商品分の変更を
  まとめて送信する既存の`_onStockUpdateQueueClick()`をそのまま流用。列側の実装を
  変えても送信ロジックには手を入れていない)
- 実装ファイル: `026.EストアDisplayPWA/pwa/app.js`の`_renderStockTable()`(新設)、
  `_sizeRank()`/`_sortStockVariants()`(新設)。`_paintStockCard()`/`_navigateStockCard()`/
  `_stockDotsOrBar()`/`_wireStockUpdateSwipe()`/`_stockMallDetail()`は削除済み
  (dead code一掃)。`index.html`の`#stockUpdateStage`/`#stockUpdateInner`は
  `#stockUpdateTable`に置換。`style.css`の`.stock-wizard-*`/`.stock-progress*`/
  `.stock-vname`/`.stock-mall-detail`/`.stock-bignum-input`/`.stock-big-steppers`/
  `.stock-swipe-hint`は削除し`.stock-table-*`一式を新設
- 実機確認済み(G1909・24バリエーション): サイズ順(S→M→L→LL→3L→4L)・色フィルタ
  (すべて/単色とも)・+5ボタンでの数量変更(赤字太字化)・変更0件時のフッター無効化、
  いずれも正常動作。コンソールエラー無し

### 2026-08-30 その3: 帽子(階級違い)のバリエーションはEストア優先 (ユーザー指示)

**発端**: ユーザーから「帽子は色で分かれることがあまりなく、'一般用'/'佐官用'のような
階級で分かれる。在庫編集画面ではEストアのバリエーションデータを基本にしてほしい」と指摘。
調査したところ、帽子G2162で実例が見つかった:

- **Eストア(正しい)**: バリエーション軸1つで「一般用」(在庫0)/「佐官用」(在庫14)を
  正しく区別している(`management_variation.patterns`: `[["一般用"],["佐官用"]]`)
- **023DB(壊れている)**: 子SKU 2件(`g2162sakan`/`G2162-GL-FREE`)とも
  `variation_color="チャコールグレー"`/`variation_size="フリー"`で**全く同じ値**。
  階級の区別が失われている(色/サイズという2軸を前提にしたスキーマが、階級という
  実際には別の軸を持つ帽子カテゴリを表現できていなかった)

この状態のまま一覧表を出すと2行とも同じラベルで表示され、在庫連携キューへの登録
(`resolve_stock_report_target`)もDBの重複した値でGoQを検索するため対象を絞り込めず
エラーになっていた(安全側の失敗ではあるが、機能として使えない状態だった)。

**ユーザー決定**: 在庫連携キューのGoQ対象特定は「帽子系だけEストア優先、それ以外は
現状維持」を選択(色/サイズが正しく取れている通常商品の経路には一切手を入れない)。

**実装** (`023.商品マスタDB/server/services/`):
- `live_mall_stock.py`: 親の子SKU群が**全員同じ(variation_color, variation_size)**
  という「曖昧」状態を検出したら(`ambiguous`判定)、023DBの子SKU一覧の代わりに
  Eストアの`management_variation.patterns`をそのまま一覧行として使う。各行の`sku`には
  `make_estore_virtual_sku(estore_code, pattern)`で作った仮想SKU
  (`"__estore__:G2162:佐官用"`のような文字列、実在の023 SKUではない)を入れる。
  楽天/Yahoo!はどの子SKUがどのEストアパターンに対応するか安全に特定できないため、
  この曖昧ケースでは無理に紐付けず`None`(未登録)のまま返す(誤った値より正直な不明)
- `stock_report_queue.py`: `resolve_stock_report_target()`の先頭で
  `parse_estore_virtual_sku(sku)`を試み、仮想SKUなら023DBの子SKU照会を経由せず
  Eストアのパターン値をそのままGoQ検索のトークンとして使う。通常SKU(仮想でない)の
  経路は完全に無変更
- `goq_worker.py`: **重大な追加発見** — 仮想SKU経由で当初`resolve_goq_target()`
  (通常のトークン一致)を流用したところ、G2162では**それでも絞り込めなかった**。
  原因: 商品タイトル自体に「一般用 佐官用」と両方の値が列挙されているため、
  どちらの行のテキストにも"佐官用"というトークンが含まれてしまう
  (行1: `"...一般用 佐官用...G2162<>佐官用..."`、行2:
  `"...一般用 佐官用...G2162<>一般用..."` — 両方に"佐官用"というトークンが存在する)。
  GoQの行テキストは実際には`"検索コード<>値"`という並びでバリエーションを
  エンコードしているため、この並びそのものを部分一致で探す
  `resolve_goq_target_by_pattern(search_code, pattern)`を新設し、こちらを使うことで
  一意に絞り込めた(タイトル中の重複語に惑わされない)。`resolve_stock_report_target()`
  の仮想SKU経路はこちらを使う
- **🐛 仮想SKUの文字列形式が026側のSKUバリデーションで弾かれる不具合を発見・修正**
  (実機報告: G2162でキュー投入が「在庫変更をキューに追加できませんでした」で失敗)。
  原因: `026/server/routers/stock_update.py`の`_SKU_RE = r"^[A-Za-z0-9._-]{1,64}$"`が
  sku文字列を検証しており、当初の仮想SKU形式`"__estore__:G2162:佐官用"`はコロンと
  日本語を含むため`400 sku format invalid`で拒否されていた。修正:
  `make_estore_virtual_sku()`をurlsafe base64符号化に変更
  (`"ev.G2162.5L2Q5a6Y55So"`のような形式。プレフィックス`ev.`+estore_code+`.`+
  base64url(パターン文字列, パディング`=`除去)。`_SKU_RE`を完全に満たす)
- **実機E2E完了(読み取り+実際のGoQ書き込みまで確認済み)**: `fetch_live_mall_stock('G2162')`
  が正しく2行(一般用/佐官用)を返すこと、`resolve_stock_report_target()`が仮想SKU経由で
  「佐官用」→row_id 170346、「一般用」→row_id 170347と正しく別々の行に一意解決できること、
  026プロキシ経由の`POST /stock-update/enqueue`が200で受理されること、
  実際にjob_queueが処理して**GoQへの書き込みが成功**(`status:"ok"`,
  `changed_fields:["stock[170346]"]`で佐官用の行だけが変更され一般用には触れていないこと)
  まで確認済み(現在庫と同じ値=14で書き込む no-op テストのため実質的な在庫変動は無し)
- `026.EストアDisplayPWA/pwa/app.js`: `_renderStockTable()`のカラー小見出しは、
  サイズ軸が無い(帽子のような単一軸)商品では出さないよう修正(行ラベルと完全に
  重複するため)。`hasSizeAxis`判定を追加

### 2026-08-30 その4: 一覧表化に伴う退行 — 横スワイプでモーダルごと閉じる不具合

**実機報告**: モーダル内で横にスワイプすると、ブラウザの「戻る」ジェスチャが発火して
モーダルごと閉じてしまう。

**原因**: 旧・横スワイプウィザードの`.stock-wizard-stage { touch-action: pan-y; }`は
「スワイプでバリエーション送り」を実装するためだけでなく、**横方向のタッチ操作を
ブラウザのネイティブジェスチャ(スワイプで戻る)に渡さないようにする**役目も
兼ねていた。ウィザードを一覧表表示へ全面置換した際にこのCSSごと削除してしまい、
一覧表内で横方向にドラッグすると素通りしてブラウザのスワイプバックを誘発する
退行(regression)が発生していた。

**修正**: `touch-action: pan-y`と`overscroll-behavior-x: contain`を
`.modal-content`(全モーダル共通の基底クラス)に移設。ウィザード固有の対策ではなく
在庫数更新モーダルに限らず全モーダルに適用されるようにした
(`026.EストアDisplayPWA/pwa/style.css`)。DevToolsの`getComputedStyle`で
両プロパティが適用されていることを確認済み。実機のスワイプジェスチャ自体は
デスクトップブラウザでは再現・検証できないため、ユーザーの実機での再確認が必要。

### 2026-08-30 その5: 在庫数更新モーダルを横スワイプで閉じられるようにする (ユーザー指示)

その4でブラウザのネイティブな「スワイプで戻る」誤爆は止めたが、続けてユーザーから
「このモーダルも横スワイプで消せるようにしてほしい」と依頼があった。ブラウザの
ネイティブジェスチャに任せるのではなく、**JS側で制御された独自のスワイプ削除**を
実装する(`touch-action: pan-y`はブラウザの既定動作だけを抑止し、JSのPointer Events
自体は引き続き受け取れるため両立できる)。

`026.EストアDisplayPWA/pwa/app.js`に`_wireStockUpdateModalSwipeDismiss()`(IIFE)を新設。
設計は旧・横スワイプウィザード(バリエーション送り用、削除済み)で得た教訓をそのまま踏襲:
1. ボタン・input等の操作要素上の`pointerdown`はドラッグ判定に一切入れない
   (フィルタチップ/ステッパー操作中に誤って閉じないため)
2. 実際に横へ`MOVE_THRESH`(10px)超動くまでは`setPointerCapture`しない(単純タップは
   素通りしてボタンのclickへ届く)
3. `pointerId`を照合し複数ポインタの混線を防ぐ
4. 横へ`CLOSE_THRESH`(90px)以上ドラッグしたら`closeOneModal(modalEl)`を呼ぶ
   (busy中=キュー投入中は既存のガードで閉じない。この経路は変更していない)

ドラッグ中は`.modal-content`に追従してtransform/opacityを変化させ、離した時に
閾値未満なら`transform: transform .22s ease`のCSSトランジションで元の位置へ
スナップバックする(`.modal-content.no-anim`でドラッグ中だけトランジションを止める)。
`_closeStockUpdateModal()`にも、次回オープン時に前回のドラッグ演出が残らないよう
transform/opacityをリセットする処理を追加した。

**実機確認(合成PointerEventで実施)**: `pointerType:'touch'`の`PointerEvent`を
`dispatchEvent`し、以下3パターンを確認済み:
- モーダル空白部分を120px横ドラッグ → `setPointerCapture`が1回発火しモーダルが閉じる
- 同じ場所を30px(閾値未満)ドラッグ → 閉じない
- フィルタチップ(ボタン)上から120px横ドラッグ → ドラッグ判定に入らず閉じない
(実際の指によるスワイプはデスクトップブラウザでは検証不可のため、実機での確認が必要)

## 「7S在庫復活レビュー」在庫閾値の自動反映は不採用 (2026-08-30 ユーザー最終決定)

cgd Lv3レビュー(Codex+DeepSeek 技術×批評4件)で検討した「7S在庫が閾値(例: 5)以上なら
GoQへ自動反映してよい」という設計は、**Row IDs修正(下記)を先に実施した後、
ユーザーが不採用と最終決定した**。理由: 不具合・展示品等の理由で意図的にGoQ在庫を0に
していた商品まで、7S側に在庫数さえあれば機械的に「復活」させてしまう恐れがある
(これはDeepSeek批評レビューが独自に指摘した論点で、閾値の大小に関係なく該当する)。

**最終方針**: 在庫の販売再開は、閾値に関わらず**必ずユーザーが手動でボタンを押した
場合のみ**行う。「7S在庫復活レビュー」機能(候補一覧を出し、選んだものだけ反映する
既存の手動フロー)がそのまま最終形であり、これに自動反映を追加する予定は無い。
閾値による分岐は「候補の見せ方(並び順・強調表示等)のヒント」としてなら使える余地は
残るが、GoQへの書込みトリガーには使わない。

実装ファイル・Row IDs修正の詳細は[[project_stock_restore_row_ids_fix]] memory参照。
