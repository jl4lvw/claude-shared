---
name: goq-stock-set
description: GoQシステムの在庫連携画面(stockSituation)で指定SKUの総在庫数を書き換え、連携中のモール(楽天/Yahoo!ショッピング/Eストア等)へ即座に反映する。リレーメッセージ・メール等の遠隔依頼で「在庫を◯個にして」と来たときに使う標準手順。バリエーション商品で対象が一意に決まらない場合はDeepSeekで確認メッセージを作り、依頼元(relay/メール)へ問い合わせてから実行する。023.商品マスタDBを使う。
trigger: 遠隔(relay/メール/LINE WORKS等)からGoQの在庫数変更を依頼されたとき。「在庫を20個にして」「G1021の在庫を修正して」等
---

<!-- SKILL_VERSION: 2026-08-25_194500 -->

# goq-stock-set — GoQ在庫連携画面での在庫数変更

対象システム: `023.商品マスタDB`（サーバー既定 `http://127.0.0.1:8290`）。
このスキルは **GoQシステム(stock2.goqsystem.com)の「在庫連携画面」で、指定した商品番号の
「総在庫数」欄を書き換えて「反映」する**手順を固定化したもの。反映すると、その行で連携
チェックがオンになっているモール（楽天・Yahoo!ショッピング・Eストア、紐付いていればAmazon）
へ**即座に**在庫数がプッシュされる（2026-08-25 実装・G1021/G1022で実機確認済み）。

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

### Step 0: 対象の一意性を確認する（親子/バリエーション商品の場合は必須）

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
明示されていれば、その子SKUを対象にStep 1へ進んでよい。

### Step 1（任意・推奨）: 変更前の在庫をライブAPIで確認する

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

### Step 2: GoQの在庫連携画面で在庫数を書き換えて反映する

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
- レスポンスの `row_count` が対象行数。**1より大きい場合は要注意**（下記「既知の制限」参照）
- `notices` にGoQの確認ダイアログ文言が入る。正常なら
  `"編集内容を反映します。よろしいですか？※連携チェックがオンの商品はモールに在庫数が反映されます"`
  のような文言が返る
- 検索0件・ログイン失敗時は 422 (`GoqWorkerError`) で反映されずに終わる（安全側）

複数SKUをまとめて依頼された場合は、この呼び出しを対象SKUの数だけ順番に繰り返す。

### Step 3: 反映結果をライブAPIで確認する

Step 1と同じ方法で、楽天・Yahoo!・Eストアそれぞれが新しい数量になっているか再確認する。
Selenium側は「反映しました」で終わるが、**モール側APIまで見て初めて実際に反映されたと言える**
（AGENTS.md「反映系の操作は結果を検証して終える」原則）。

### Step 4: 023ローカルDBを合わせるか確認する

023の`products.stock`列はGoQ在庫連携とは独立した値で、**自動では追従しない**
（このシステムは在庫を各モールへ押し出すだけで、売上による在庫減少を引き戻す仕組みがない
—2026-08-25 会話で確認済みの既知の制限）。ローカルDBも合わせるかどうかは**毎回ユーザーに確認する**
（黙って合わせない。前回セッションでは「合わせますか？」と聞いてから実施した）。

合わせる場合:

```bash
# 1) 現在の version を取得 → 2) If-Match で stock を PATCH
curl -s "http://127.0.0.1:8290/api/products/<SKU>" -H "X-Forwarded-For: 192.168.1.166"
curl -s -X PATCH "http://127.0.0.1:8290/api/products/<SKU>" \
  -H "X-Forwarded-For: 192.168.1.166" -H "X-Actor: <actor>" -H "If-Match: <version>" \
  -H "Content-Type: application/json" -d '{"stock": <新しい在庫数>}'
```

## 既知の制限（実装未対応・遭遇したら手動対応するか拡張を検討）

- **親子(バリエーション)商品は未検証**: `set_stock_quantities()` は検索結果画面にある
  `input[name^="stock["]` を**全部同じ数量に書き換える**実装。単品(G1021/G1022)では1行=1入力欄
  だったが、親子商品で複数行(色/サイズごと)にヒットする場合、全バリエーションが同じ数量に
  上書きされる。Step 0 の023 DB確認で子SKUまで一意に絞れていれば通常は1行のはずだが、
  念のため `set-quantity` 実行前に `POST /api/goq-browser/search` で `row_count` を確認し、
  2以上なら実行せず内容を見直す（GoQ側の登録状態が023と食い違っている可能性がある）
- **並列実行しない**: Selenium は単一ブラウザインスタンス前提（`goq_worker._lock`で直列化済み
  だが、多数SKUを一度に依頼された場合は焦って並列化しない。順番に1件ずつ）
- Amazonが連携されている商品は、連携チェックが入っていれば同時に反映される
  （楽天/Yahoo/Eストアと同様。個別に確認したい場合はStep1でAmazon側も見る）

## 対象が一意に決まらない場合の確認フロー（バリエーション商品・2026-08-25追加）

Step 0 で親商品なのに子(色/サイズ)を特定できなかった場合の手順。**推測で1件を選んで進めない。**
遠隔（relay/メール）から来た依頼は、依頼が来た経路の発信者へ問いかけ、回答を待ってから
Step 1以降を実行する。

### A: 候補一覧を集める（Step 0 のクエリ結果をそのまま使う）

Step 0 で取得した `variation_color` / `variation_size` の一覧が候補。SKUやバリエーションIDは
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
子SKUで Step 1 以降を実行する。

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
