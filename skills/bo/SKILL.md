---
name: bo
description: 在庫管理アプリ BoxQR（https://boxqr.sfuji.f5.si）の発注予約 API から発注予定数を取得し、7S・FBA の在庫と並べて、Tシャツ等の発注（TOMS/CAB など）を進めるスキル。予定数は信頼して数量を変えない。読み取りは自由、API への書き込み（claim・ordered など）は運用者の承認後だけ。「/bo」「発注予定」「BoxQRの予定」「注文予定を取得」などで起動。
---

<!-- SKILL_VERSION: 2026-10-06_bo_v1 -->

# /bo — BoxQR の発注予定から発注を進める

在庫管理アプリ（BoxQR）に入力された「発注予定」を API で読み、在庫を並べて表示し、運用者と手配内容を決めて、RC への依頼または直接手配へつなぐ。
実装: `.claude/skills/bo/scripts/bo_api.py`（テスト: `test_bo_api.py`）。API 仕様は下の「API の要点」。

## 基本ルール（運用者決定 2026-10-06）

- **予定数は疑わない**。API で取得した予定数をそのまま発注数量にする（調整は運用者が言ったときだけ）。25の倍数でなくても止めない（参考表示のみ）。
- **在庫は 7S と FBA（販売可・入荷中）だけを並べる**。本店と GoQ 枠は見ない。（通常の発注では「在庫は3箇所」が絶対ルールだが、`/bo` では運用者が上記に限定した。）
- **読み取りは自由、書き込み（claim・ordered・failed・release・cancelled・reopen）は運用者の承認後だけ**。`bo_api.py` の書き込みは既定が dry-run で、`--yes` を付けたときだけ送る。`--yes` は承認を得た直後にだけ付ける。承認を得たら文脈台帳に `OK`（対象の予定IDつき）を追記し、実行後に `drop` して `VERIFY` に付け替える。
- **API キーはファイルから読む**。`C:\Users\jl4lv\.boxqr\order_api_key.txt`（キーだけ1行。プロジェクトの外）。環境変数 `BOXQR_KEY_FILE` でパスを変えられる。**キーの値を画面・ログ・記録・メモリ・台帳に書かない**。保存・確認は `scripts/bo_setkey.py`（キーを非表示で入力して保存。`--verify` で有効確認、`--status` で状況確認。値は表示しない。保存先がプロジェクト内なら拒否し、保存後にアクセス権を本人だけに絞る）。キーが無いときは `python …/bo_setkey.py --verify` を運用者に案内する。有効確認は `bo_api.py verify-key`（存在しない予定IDを release するだけで、何も変更しない）。2026-10-06 に運用者の指示で保存済み。
- 履歴区別のため、書き込みには `X-Agent: TK-order-AI` を付ける（スクリプトが自動で付ける）。
- **スキルの連鎖は禁止**。`/bo` から `/tshirt-order` などを自動で呼ばない。同じスクリプトを直接使うか、「この先は tshirt-order で」と案内する。

## 手順

### Step 1: 予定の取得と一覧

```bash
python C:/ClaudeCode/900.ClaudeCode/.claude/skills/bo/scripts/bo_api.py table
```

- `pending`（発注前）が対象。`--status active` で ordering（他の AI が処理中）も確認できる。ordering の予定は触らない。
- 出力: 品番ごとに「商品名・色・サイズ別の 7S / FBA販売可 / FBA入荷中 / 在庫計 / 予定数 / 予定ID」と、台帳の直近の発注。
- そのまま運用者に見せる（表にして。商品名を必ず併記）。品番が複数でも1回で出す。
- 予定が0件なら「予定はありません」と伝えて終わる。

### Step 2: 聞き取り（品番ごと・番号の選択肢で）

次の順に確認する（既に運用者が言ったことは聞き返さない）:

1. **数量**: 予定どおりでよいか（既定は予定どおり）。
2. **手配先**: TOMS（→アドプロセス）／CAB（→アドプロセス）／その他。
3. **素材・カラー**: 型番（例: 00300-ACT）とカラー。**記録があっても毎回確認**する（素材の誤記録の事故あり）。
4. **納期**: 具体的な日付にする（「2週間後」＝今日＋14日）。**送信日から納期まで10日以上**。
5. **手配方法**: RC（長張さん端末）へ依頼するか、私が直接（CSV→発注書→メール）か。
6. **位置・色数**: 既製の再依頼として「前回と同じ」と書く。ただし**商品ページの全写真を見て、袖・脇・左胸などのプリントがあれば自己判断で足さず、運用者に位置を確認**する（例: G1440 は左袖にプリントがあった）。印刷仕様を写真から決めない。

商品ページ: `https://seifukunofuji.co.jp/SHOP/G####.html`。写真は `https://image1.shopserve.jp/seifukunofuji.co.jp/pic-labo/g####_*.jpg`（ページから取得する。`/timg/` はサムネなので外す）。

### Step 3: 手配の実行

- **RC へ依頼**: `017.Tシャツなどの注文/rc_request_sheet.py` の `append_request(order)` → `notify_rc(req_id, subject)`。`image1` は必須（商品写真のURL）。relay の宛先IDは `RC`。作業内容欄に「数量は BoxQR の発注予定どおり（予定ID …）」と書く。サイズ表の数量は予定どおり。
- **直接手配**: `/tshirt-order` の手順（CSV→発注書PDF→history.csv→Sheets→product_db.json→メール下書き→運用者が送信→check_sent.py）に従う。メール送信は運用者が手動（Thunderbird の画面自動操作は廃止）。

### Step 4: BoxQR への書き戻し（運用者の承認後）

どのタイミングで何を書き込むか、**その都度運用者に確認する**。標準は次のとおり。

| 場面 | コマンド（まず dry-run で内容を見せ、承認後に `--yes`） |
|---|---|
| 直接手配を始める前（二重発注防止） | `bo_api.py claim --ids 4,5,6 [--ttl 3600]` |
| 発注できた（注文番号が分かった） | `bo_api.py ordered --id 4 --order-ref 注文番号 [--ordered-qty N]` |
| 発注できなかった | `bo_api.py failed --ids 4 --note 理由` |
| やめた・手配前に戻す | `bo_api.py release --ids 4`（claim した本人だけ） |
| 取り消し／やり直し | `cancelled` ／ `reopen` |

- RC に依頼した場合、`ordered` は **RC の完了報告（受付番号）のあと**。それまで予定は pending のままになる（その旨を運用者に伝える）。
- **応答の `skipped` を必ず確認**する（`qty_changed`・`claimed_by_other`・`status_…`・`active_exists`）。`bo_api.py` は skipped があると異常終了し、changed に入った行だけが反映されている。
- claim は既定30分で自動的に pending に戻る。時間がかかるなら `--ttl` を長めに。
- 失敗（401: キーが違う／503: サーバー側未設定／409: 数量・状態の不一致／404: IDなし）はそのまま運用者に伝える。

### Step 5: 記録

発注後は tshirt-order と同じ記録（history.csv・Sheets・product_db.json・last_mail）を行う。RC 依頼なら、完了報告が来てから記録する。台帳（ctx）には依頼した REQ 番号と、BoxQR の予定IDの対応を `STATE` で残す。

## API の要点（`https://boxqr.sfuji.f5.si`）

- `GET /api/v1/order-plans?status=pending|ordering|ordered|failed|cancelled|active|all&product_code=&since=` — 認証なし
- `GET /api/v1/order-plans/events` — 変更履歴
- `GET /api/v1/products/{code}` — 商品名・7S/FBA/GoQ 在庫・台帳の発注状況（認証なし）
- 書き込み（`X-API-Key` 必須）: `POST /api/v1/order-plans/claim|release|ordered|failed|cancelled|reopen`、`PATCH /api/v1/order-plans/{id}`
- 状態遷移: pending →claim→ ordering →ordered / failed、release か期限切れで pending に戻る。同じ SKU の生きている予定（pending か ordering）は1件だけ。
- 仕様書（OpenAPI）: `https://boxqr.sfuji.f5.si/openapi.json`

## 実績（参考）

2026-10-06: G1440（L13/LL10/5L2）・G1691（M5/L10/LL10）を、TOMS 00300-ACT・納期10/20 で RC へ依頼（REQ-023・REQ-024）。G1440 は写真に左袖プリントがあり、運用者が「背中＋左袖」と指定した。

## 関連

- `tshirt-order`（発注の全フロー）・`relay`（RC への通知）・`mail-send`
- メモリ: `feedback_order_mail_rules` / `feedback_material_always_confirm` / `feedback_no_thunderbird_gui_automation` / `reference_order_form_catalog`
- 発注書の様式: `017.Tシャツなどの注文/発注書フォーマット集/README.md`
