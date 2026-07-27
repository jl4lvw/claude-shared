---
name: fbm-sync
description: 022.Amazon在庫PWA の FBA→FBM 7S在庫同期スキル。FBA在庫0かつ7S(AirLogi)在庫ありのSKUをFBM化し、既にFBMのSKUの在庫数を7S在庫に同期する。真の新規転換はAmazon自身のBUYABLE=False判定を主ガードとし(2026-07-13方針転換、stranding_riskは非ブロック・監査用記録のみ)、7S在庫>0+削除チャンネルAMAZON_JP限定と組み合わせる。混在チャンネル修復のみ従来通りstranding_risk<=0必須+手動判断。自動実行は二層: 日次フル(UTC19:15、数量更新+転換+混在needs_review+FBA在庫復活検知通知)と30分フォールバックtick(毎時:15/:45、quick syncで検知→新規転換のみ、FBA在庫切れから最悪約35分でFBM販売再開)。「標準FBA出荷指定」(fba_pinned)は表示専用フラグ(2026-07-15確定)でfbm-syncの動作には一切影響しない。
trigger: ユーザーが /fbm-sync を実行、または「FBM同期して」「7Sに合わせて」「FBA在庫0をFBMに」等と発言したとき
---

# fbm-sync — FBA→FBM 7S在庫同期スキル

## 概要

022.Amazon在庫PWA (`C:/ClaudeCode/022.Amazon在庫PWA/`) の商品について、以下を自動で行う:

1. **FBA在庫が0、かつ 7S(AirLogi)在庫がある SKU** → FBM(自社出荷)に変更し、数量を7S在庫に合わせる
2. **既にFBMになっている SKU** → 数量を7S在庫の最新値に同期(7S在庫が0になった場合も0に反映)

対象は **全商品(product_type不問)**。実FBA在庫が残っている SKU (FBA在庫>0) には一切触らない。

## 差分ゲート (無駄なAPIアクセスを避ける)

`inventory_items.fbm_sync_applied_qty` 列に「前回このスキルが実際にAmazonへ適用した数量」を保持する。
**新規FBA→FBM転換**の SKU は、現在の7S在庫 == fbm_sync_applied_qty なら Amazon への PATCH を完全に
スキップする (SP-API呼び出し自体を行わない)。

**既存FBM管理下** の SKU は v36 (2026-07-11〜) で仕様変更あり。下記「売り越し防止」参照。

**注意**: 定期SP-API同期 (`sync_worker.py`) の `is_fba` フラグは「一度 True になると False に戻さない」
仕様があり、FBM化後もカタログ上 `AMAZON_JP` チャネルが残るため `is_fba` が勝手に `True` に巻き戻ることがある。
このため対象判定には `is_fba` を使わず、独立列 `fbm_sync_applied_qty` を真実源にしている
(巻き戻りが起きても実際の出品状態には影響なく、次回実行時に不要な再patchが1回起きるだけ)。

## 売り越し防止 (v36, 2026-07-11〜)

**背景 (ユーザー指摘)**: Amazonで売れたがまだ7S倉庫から出荷していない間に、旧実装は生の7S在庫数を
そのまま Amazon へ上書き送信していた。Amazon 側は売れると自動でライブ在庫数
(`fulfillmentAvailability`。こちらが送信した `attributes.fulfillment_availability` とは別の、
実際に購入可能な数の真実源) を減らすが、これを生値で上書きすると「売れた分」が復活してしまい、
二重販売(売り越し)のリスクがあった。

**対策**: 既存FBM管理下のSKUは、実行のたびに Amazon の SP-API (`getListingsItem`,
`includedData=fulfillmentAvailability`) でライブ在庫数を読み、以下で送信数量を決める:

```
純増分   = max(0, 今回の7S在庫 - 前回チェック時の7S在庫)   ※出荷による減少は無視 (既に売れた分の消化のため)
desired = min(今回の7S在庫, max(0, ライブ在庫数 + 純増分))
```

- 生の7S在庫をそのまま送らない。「前回チェック以降に倉庫へ純粋に増えた分」だけをライブ在庫数に加算する
- `desired == ライブ在庫数` なら Amazon 側は既に正しい値 → PATCH せず記録 (`fbm_sync_applied_qty` /
  `fbm_sync_last_s7`) だけ更新する
- **新規FBA→FBM転換**の SKU はライブ確認の対象外 (これまでMFN出品として売られていなかったため売り越し
  リスクがなく、旧来どおり7S在庫をそのまま送信する)

**トレードオフ**: 既存FBM管理下のSKUは実行のたびに必ず1回 `getListingsItem` を呼ぶ (以前は差分なしなら
API呼び出し自体を省略できたが、ライブ在庫確認が安全性の要なので省略できなくなった)。件数が多い場合は
実行時間が伸びる。

## チャンネル残骸の自動修復 (2026-07-11 実機検証で追加)

**発見した問題**: 一度でもFBA(AMAZON_JP等)だったSKUをFBM化しても、旧実装のPATCH
(`op: "replace"`) は `fulfillment_channel_code` を**セレクタ**として扱うため、DEFAULTチャンネルを
追加するだけで元のAMAZON_JP(・多国展開品はAMAZON_SA/NA/SG/AU等)を削除できていなかった
([Amazon公式Issue](https://github.com/amzn/selling-partner-api-models/issues/2061)で確認)。
結果、Amazon側のライブ配信がFBA側チャンネルを優先し、`fulfillmentAvailability`(ライブ実在庫)に
DEFAULTが一切出てこない状態になっていた(実機検証で対象228件中220件がこの状態と判明)。

**対策**: PATCH時に `attributes.fulfillment_availability` から現在ついている非DEFAULTチャンネルを
検出し、`op: "delete"` で明示的に削除 + `op: "replace"` でDEFAULTを設定、を**同一PATCH内**で同時に送る。

```json
{"productType": "...", "patches": [
  {"op": "delete", "path": "/attributes/fulfillment_availability",
   "value": [{"fulfillment_channel_code": "AMAZON_JP"}, ...]},
  {"op": "replace", "path": "/attributes/fulfillment_availability",
   "value": [{"fulfillment_channel_code": "DEFAULT", "quantity": <desired>, "lead_time_to_ship_max_days": 3}]}
]}
```

- **ライブ反映には10〜15分程度かかる**(即座ではない。実機検証で複数SKU確認済み)
- 新規FBA→FBM転換のSKUも同時にチャンネル修復する (将来同じ問題を抱えないよう予防)
- 実行結果の `detail` に `(削除チャンネル: AMAZON_JP,...)` と記録される
- **(2026-07-11 更新)** 「ライブ在庫数が読み取れない」未追跡の既存FBM (is_fbaフラグ由来では
  なくライブAPIのチャンネル構成で発覚したケース) は、生の7S在庫を送るフォールバックを廃止し
  **スキップ**するよう変更した。詳細は次の「is_fbaフラグに頼らないライブ判定」セクション参照。

## 新規FBA→FBM転換の安全条件 (2026-07-11 ストランデッド在庫事故を受けて追加)

**発生した事故**: 上記チャンネル修復機能により、新規FBA→FBM転換で `fulfillable_qty<=0` だけを
条件にしていたSKU (6件) が、実際にはFBA倉庫に reserved (注文引当中/FC処理中/FC間転送中) 在庫が
残っている状態で AMAZON_JP チャンネルを削除されてしまった。AmazonのFBAとMFN(DEFAULT)は排他的
チャンネルのため、これは倉庫の実在庫が「販売可能な出品に紐付かない」**ストランデッド在庫化**の
リスクを生む (保管料が発生し続け、30日以内に対応しないとAmazonが返送/廃棄しうる)。
ユーザー報告 (ASIN B0CN41LLGB, fulfillable=1・reserved=13・total=14) を受けて、SP-APIで直接
在庫状況を確認 → Lv7 5者レビュー (Codex med+high/Gemini/DS/Qwen) で原因確定 → 6件とも
FBAチャンネルを復元 (delete DEFAULT + replace AMAZON_JP) しライブ反映を確認済み。

**対策 (2段階のAND条件、いずれもLv7 5者収束)**:

1. **`total_qty<=0` を必須条件に追加** — `fulfillable_qty` だけでなく
   `total_qty` (= fulfillable + reserved + inbound + unfulfillable) もゼロでなければ、
   FBA倉庫に在庫ライフサイクルが残っていると判定し新規転換の対象外にする
   (`_fbm_sync_candidates()` の候補抽出時、および `_run_fbm_sync_body()` のPATCH直前に
   DBを再読込して二重にガード)。

2. **Amazon自身のBUYABLE判定を追加ゲートにする** (ユーザー提案) —
   ユーザーの指摘: 「在庫数を自前で計算するより、FBAで停止中(在庫切れ)になっているものを
   Amazon自身の判断に準じて抽出したほうが間違いない」。実機検証で `getListingsItem` の
   `summaries[].status` 配列に `BUYABLE` が含まれるかを確認:
   ```
   FUJI-G1504-DR-LL (問題のSKU, fulfillable=1,reserved=13) → BUYABLE=True (誤変換だった実例)
   FUJI-G1415-...   (誤変換6件の1つ, fulfillable=0,reserved=2) → BUYABLE=True (これも誤変換!)
   ```
   `fulfillable_qty=0` でも Amazon がまだ「購入可能」と判定しているケースが実在することを確認。
   ただし公式ドキュメントに「BUYABLE欠如は在庫不足以外 (出品停止/ブロック/オファー不備等) でも
   起こりうる」とあるため、**BUYABLE欠如だけを転換トリガーにはしない** (5者全員が反対)。
   `total_qty<=0` を主ガードとして維持したまま、「Amazonがまだ購入可能と判定しているSKUには
   絶対に触らない」追加の安全弁として使う。
   - 実装は `_run_fbm_sync_body()` のPATCH直前、既に取得済みの `summaries` (追加API呼び出し
     不要) を `_listing_has_buyable_status()` で判定
   - `BUYABLE` あり → 転換しない (Amazonがまだ買えると判定している)
   - `BUYABLE` 判定不能 (summaries空・status欠落等) → 転換しない (安全側にスキップ)
   - `BUYABLE` なし かつ `total_qty<=0` の場合のみ転換
   - 対象は真の「FBA→FBM転換」(`requires_fba_to_fbm`) のみ。未管理だが元々FBMのSKUや
     既存FBM管理下 (v36売り越し防止) には適用しない (適用するとBUYABLE判定に振り回されて
     関係ない理由で同期が止まる別問題を作るため)

## is_fbaフラグに頼らないライブ判定 (2026-07-11 追加)

**発見した問題**: DBの `is_fba` フラグは「一度Trueになると定期同期でFalseに戻さない」既知の
巻き戻りバグがあり信頼できない。実例 (`FUJI-G0688-3L`): `is_fba=True` (未管理) のSKUが、
実際には既にDEFAULT(FBM)チャンネルでライブ運用中 (在庫6) だった。本物のFBA在庫ゼロSKU
(ライブAPIでAMAZON_JPチャンネルのみと確認できたもの) を13件サンプルしたところ全件で
BUYABLE=Falseと正しく一致しており、根本原因は `is_fba` DBフラグの信頼性だと判明した。

**対策**: `_run_fbm_sync_body()` で `it.attributes.get("fulfillment_availability")`
(PATCH直前に取得済み、追加API呼び出し不要) から実際のチャンネル構成を読み取り、
`effective_has_default` (DEFAULTチャンネルが既に存在するか) で分岐し直す。DB由来の
`c["is_new_conversion"]` は実行時の分岐には使わない (使うと label/ガードが食い違う
integrationバグになる。Codex high 指摘)。

- `effective_has_default=True` (ライブで既にFBM) → is_fbaフラグの値に関わらずv36
  売り越し防止ロジックを適用。未追跡 (`fbm_sync_applied_qty is None`) でも
  `last_s7 is None → last_s7=s7` フォールバックがそのまま機能し、初回は
  `min(7S在庫, ライブ在庫)` に丸められる (安全側。次回以降は正しく純増分を検知する)
- `effective_has_default=True` かつライブDEFAULT数量が読めない場合 → **スキップ**
  (未追跡SKUに「生の7S在庫を送る」フォールバックを適用すると、追跡していないMFN販売
  履歴を無視して売り越しリスクを再導入するため。旧仕様から変更)
- `effective_has_default=False` (ライブで非DEFAULTのみ) → 真の新規転換候補
- スキップ時も含め全経路で `is_fba=False` / `fulfillment_channel="DEFAULT"` をDB補正
  する (Gemini/DS/Qwen収束。巻き戻りバグの根治)

## pendingCustomerOrderQuantity の除外 (Lv4 cgd, 2026-07-11 追加)

**問題提起 (ユーザー)**: `total_qty<=0` ガードは reserved の中身を区別しないため、
「注文引当済みだが支払い未了で高確率で出荷される」1個の在庫のために、SKUが長期間
「FBA在庫0で停止中、FBMは7S在庫あるのに転換もされない」という二重の販売機会損失を
生むケースが実例 (`FUJI-G1827-SUB100Y-DR-BK-M`, reserved_customer_orders_qty=1) で
確認された。

**調査結果**: 「既に確定した注文は出品を非アクティブ化しても通常通り出荷される」という
Amazon公式フォーラムの情報を確認。また `pendingCustomerOrderQuantity` はFBA倉庫
(Amazon側) の在庫を消費する一方、FBM転換後の新規販売は7S(自社倉庫)在庫から行われる
**別の在庫プール**なので、両者は競合しない。Lv4 cgd (Gemini→Claude→DS+Qwen並列advisor)
で3者ともこの除外案 (A案) に収束した。

**対策**: `_stranding_risk_qty()` ヘルパーで「チャンネル変更でストランデッド化しうる
数量」を `fulfillable + fcProcessing + fcTransfers + inbound + unfulfillable` として
算出し (`reserved_customer_orders_qty` を明示的に除外)、これが0以下ならFBAチャンネル
削除 (新規転換・混在チャンネル修復のいずれも) を許可する。

**鮮度の注意 (重要)**: DBの `reserved_*` 列は周期同期 (`sync_worker.py`, 既定30分間隔)
由来だが、実機で **9時間以上ズレるケース** を確認した (原因未特定、要監視)。そのため
PATCH直前は必ず `sp_api_client.fetch_live_inventory_for_sku(sku)` でライブ再照会する
(DBは信用しない)。この呼び出しは非DEFAULTチャンネルを削除する可能性がある2分岐
(真の新規転換 / 既にDEFAULT混在) でのみ発生し、レート制限は `inventories:get_sku`
キーで 0.5秒間隔 (FBA Inventory API 既定の 2 req/s) を守る。

## 日次自動実行 + Discord通知 (Lv4 cgd, 2026-07-11 追加、同日方針改訂)

**背景 (ユーザー要望)**: 手動トリガーのみだと実行を忘れる。当初は「チャンネル削除を伴う
操作は一律自動化しない」方針だったが、シミュレーションで **「FBA在庫が0になったSKUが
永久に自動転換されない (自動フォールバック不在)」** ギャップが判明し、ユーザー指示
「これではだめ、自動フォールバック必要」により同日中に方針改訂 (Lv7 5者レビュー済み)。

**方針**: 実行時にライブAPIで自動判別し、以下の3分類で動く。

- **数量更新 (自動実行)**: ライブAPIで既にDEFAULT単独と確認できたSKUの、
  v36売り越し防止ロジックによる数量更新。チャンネル削除は発生しない。
- **新規FBA→FBM転換 (自動実行、2026-07-11改訂で追加、2026-07-13再改訂)**:
  ライブでDEFAULT無しの真のFBA。以下の**3条件**を全部通過したものだけ実行し、
  実行したSKUはDiscordに全件列挙する:
  1. Amazon自身の判定が `BUYABLE=False` (True/判定不能はブロック) — **主ガード**
  2. 7S在庫 > 0
  3. 削除対象チャンネルが `AMAZON_JP` のみ (未知チャンネル混入は needs_review。
     2026-07-13よりsafe_only限定を撤廃し常時適用)
  **`stranding_risk<=0` は2026-07-13よりブロック条件から除外** (下記「BUYABLE最優先化」参照)。
- **混在チャンネル修復 (自動実行しない)**: DEFAULT+非DEFAULT併存の異常状態。
  `status="needs_review"` として記録するだけに留め、Amazon APIへの書き込み・追加の
  ライブ照会は行わない (この状態にはBUYABLEガードが無く、原因不明の異常のため人間の目を残す)。

**stranding_risk の fail-open 封鎖 (Codex Lv7 🔴, 2026-07-11)**: `_stranding_risk_qty` は
内訳合計だけでなく `max(内訳合計, total_qty - reserved_customer_orders_qty)` を返す。
inventoryDetails 欠落や未パース区分 (researchingQuantity等) で内訳が0でも、totalQuantity に
実在庫が残っていれば転換をブロックする。

**実装**:
- `_run_fbm_sync_body(rid, safe_only=True)`: `is_mixed_channel_case`
  (= `effective_has_default and bool(non_default)`) のみ即 `needs_review`。
  新規転換分岐では旧実装にあった `applied_qty == desired` スキップを撤廃
  (ライブでDEFAULT不在が確定している時点でPATCH必須。Seller Central側で手動FBA化
  された場合にDB値が古いまま永久スキップするバグだった。Codex med + DS 収束🟠)
- `sync_worker.py` の `start_scheduler()` に `fbm-sync-daily` ジョブを登録
  (cron, UTC 19:15 = JST 04:15、1日1回)
  - 実行時刻は **airlogi-sync (毎時0,30分) と衝突しないよう15分ずらしている**
    (fbm-syncは7S在庫=AirlogiStock.stock_qtyを数量根拠にするため、同時刻だと
    更新前の古い7S在庫を読みうる。Codex Lv7 収束指摘で確定)
  - ブートストラップ実行なし (サーバー再起動のたびに走ると開発中に予期しない実行になるため)
- `fbm_sync_daily_tick()`: 日次実行のエントリポイント。他の出荷方法変更ジョブ
  (手動 `/fbm-sync/run` や `/fulfillment/bulk`) が実行中ならスキップ (ジョブ単位の
  排他ロック `_try_start_job` を尊重)。スキップされた場合も無音にせずDiscord通知する
  (Codex Lv7 指摘: 無音スキップだと「今日の自動実行が走らなかった」ことに気づけない)
- 完了後 `_notify_fbm_sync_daily_result()` で結果をDiscordに通知
  (数量更新件数/変化なし件数/**新規FBA→FBM転換のSKU全件列挙**/手動確認が必要な
  件数とSKU例/**失敗の理由別内訳**。Lv7 4者収束指摘で強化、2026-07-11)
  (`006.secretary/scripts/discord_notify.py` の `notify()` を再利用。Webhook未設定でも
  例外を投げず本体処理には影響しない)
- **結果通知はJST 09:00に遅延送信** (2026-07-12 ユーザー指摘「人間宛て通知が深夜に
  届く」対応): 実行はJST 04:15のまま (業務開始前に在庫を整える)、結果は
  `server/fbm_sync_daily_notice_pending.txt` に積み、`fbm-sync-notice-flush` ジョブ
  (UTC 00:00 = JST 09:00) が送信して削除。ファイル経由なのでサーバー再起動をまたいでも
  通知は消えない。排他スキップ通知も同様に9時送信。
  **即時通知のまま**: 30分tickの転換通知・quick sync障害アラート (実際の変更/障害の報告のため)
- `POST /fulfillment/fbm-sync/run?safe_only=true` で手動テスト可能 (方針改訂後の実機検証
  2026-07-11: 232件中、混在1件のみ`needs_review`・真のFBA候補5件はガード到達後に
  3件stranding_risk>0 / 2件BUYABLE=True で正しくブロック・Amazon書き込みゼロを確認)

**既知の制約**: `safe_only=true` でも「fbm-sync未追跡だが元々DEFAULT単独のSKU」は
自動更新対象に含まれる (既存仕様「既にFBMのSKUの在庫数を7S在庫に同期する」の一部。
Codex Lv7 指摘、意図的な仕様として維持)。

## 30分フォールバックtick (2026-07-11 追加、Lv7 5者レビュー済み)

**背景 (ユーザー要件)**: 日次だけだとFBA在庫切れから転換まで最大24時間。
「**1時間程度でFBMにフォールバックしたい**」との指示で高頻度化 (30分ごとをユーザー選択)。

**構成 (日次との二層)**:
- 日次フル実行 (JST 04:15): 全候補の数量更新+転換+混在needs_review+サマリ通知 (従来どおり)
- フォールバックtick (毎時 :15/:45 + jitter 60秒): FBA在庫切れ→FBM転換**だけ**に特化

**tickの流れ** (`fbm_sync_fallback_tick`, routers/fulfillment.py):
1. UTC 19:00-19:24 は日次実行に譲って無条件スキップ (job mutexの取り合いで日次が
   負けて丸1日スキップされる事故を防ぐ優先制御)
2. quick sync (`trigger_sync(manual=True, kind="quick")`, FBA Inventory APIのみ約5秒) を
   起動し SyncRun 行ポーリングで完了待ち (最大90秒) → fba_stock を最新化
   (auto-sync 30分周期を待たずに在庫切れを検知するため)
   - 別の同期が実行中 (`already in progress`) なら、その完了を最大120秒待って続行
     (fullが走っているならDBはむしろ新しくなる)
   - 起動失敗/タイムアウト/failed は既存DBで続行 (安全性はライブ照会ガードが担保)。
     ただし**連続6回 (約3時間) 失敗で1回だけDiscord通知**、復旧時も通知
     (`_record_quick_sync_result`。検知が古いDB頼みになっている事実の可視化)
3. 候補を `is_new_conversion AND is_fba_raw` に絞る (通常10件前後)
   - **重要 (Codex Lv7 med+high 収束🔴)**: `is_new_conversion` (=未管理) だけだと
     「未管理だが元々FBM」も拾い、既存FBMの数量更新が30分ごとに回ってしまう。
     `is_fba_raw` も要求して真の転換候補に限定。さらに実行時、ライブで
     `effective_has_default=True` (is_fba stale) なら skipped で日次に委ねる
4. safe_only=True で実行 — 3条件 (BUYABLE=False主ガード / 7S>0 / AMAZON_JP限定) は
   日次と完全共通 (`_run_fbm_sync_body` 共用、2026-07-13よりstranding_riskは非ブロック)
5. Discord通知は**実際にFBM転換した時だけ** (SKU全件列挙、**各行末尾に商品名付き**
   `SKU: FBM化 (在庫N)... | 商品名`。2026-07-12 ユーザー指示「SKUだけでは何の商品か
   分からない」対応。`_product_names_for` がDBの product_name_user_override 優先で引く。
   日次通知の転換リストも同様)。ガードによるfailed・needs_reviewは30分ごとに
   同じ内容が再発するため通知せず日次サマリに任せる。排他スキップも通知しない (ログのみ)

**転換までの所要時間**: FBA在庫が0になってから最悪約35分・平均約20分で販売再開
(次のtickでquick syncが検知→ガード通過→PATCH→Amazon伝播10-15分)。

**FBA補充運用との共存 (v37 manual_fba_at、2026-07-11)**: FBA補充の操作順
「①PWAでFBAに切替→②納品プラン作成→③発送」の①〜②間は、FBA在庫0・inbound0・
BUYABLE=False で真の在庫切れと区別できず、tickが30分でFBMに引き戻してしまう。
対策: PWA一括切替のFBA方向で `manual_fba_at` に時刻を記録し、**24時間** (ユーザー選択)
は自動フォールバック候補から除外する。②で納品プランができれば inbound が
stranding_risk ガードに引き継ぎ、猶予切れ後もFBA在庫0のままなら通常の自動転換に戻る。
**制限**: Seller Central 側で直接FBAに切り替えた場合は manual_fba_at が記録されない
(PWA経由の切替のみ保護)。FBA復帰は必ずPWAの一括切替で行うこと。
**手動 `/fbm-sync/run` はこの猶予を無視する** (2026-07-12 Lv7再監査、ユーザー判断:
運用者が明示的に確認・実行したい時に候補から漏れないように。`_fbm_sync_candidates(db,
respect_manual_fba_grace=...)` / `_run_fbm_sync(..., is_automated=...)` で制御。
日次tick・30分tickは `is_automated=True` で猶予を尊重、手動実行系は既定Falseで無視)。

**手動テスト**: `POST /fulfillment/fbm-sync/fallback-run` (Originヘッダ必須) →
`GET /fulfillment/bulk/progress/{request_id}` で確認。

**関連修正 (2026-07-11 Lv7、tickと同時に実施)**:
- `sync_worker.trigger_sync`: `_run_lock` acquire後に `_create_run_row` (SQLite書き込み)
  が例外を投げるとロックが永久リークし全同期が停止するバグを try/except release で封鎖
  (Codex med+high 収束🔴。database is locked の実績があり、tickで露出が48倍/日になるため)
- fallback cron に jitter=60秒: auto-sync (位相不定30分interval) との位相ロック飢餓
  (fullが毎回起動拒否され続ける) を防止

**Lv7全体再監査 (2026-07-12)**: 複数回の追加修正が積み重なった状態で通し監査を実施
(Codex med+high、Gemini、DS、Qwen)。🔴重大指摘なし (現行の単一プロセス構成では危険な
即時転換経路なし)。DS/Qwenの「manual_fba_atのtimezone不整合」「stranding_risk負値
バグ」は実DB確認・コード検証の結果いずれも誤検知 (本コードベースはnaive UTC datetime
で一貫、`max(breakdown, total_based)` は負値を自動的に無視する)。実指摘として採用・
修正した5件:
1. 手動 `/fbm-sync/run` が manual_fba_at 猶予に巻き込まれる問題 (上記参照)
2. `/fbm-sync/preview` の `new_fba_to_fbm` 集計を実行時の絞り込み
   (`is_new_conversion AND is_fba_raw`) と一致させた (以前は `is_new_conversion` のみで
   「未管理だが元々FBM」も誤って新規転換件数に含まれていた)
3. `_JOB_LOCK`/`_JOB_STATE`/`_QS_FAIL_STREAK` 等プロセス内グローバル状態への
   単一プロセス前提コメント追加 (`--workers>1` にすると排他が崩れる旨を明記)
4. 古くなっていたコメント・docstringの更新 (`is_new_conversion` のみ→
   `AND is_fba_raw AND manual_fba_at猶予外` を反映)
5. `_queue_daily_notice` を tmpファイル+`os.replace` でアトミック書き込み化

## BUYABLE最優先化 (2026-07-13、方針転換)

**背景 (ユーザー明示指示)**: 実例 FJ-G2019-HA-SAFARI-MEI-L は `stranding_risk_qty=1`
(内訳: `reserved_fc_processing_qty=1`、FC内処理中で近く`fulfillable`に戻りうる残存在庫)
により、旧ガードで転換が延々ブロックされ続けていた。一方7S在庫は25あり、Amazon自身も
`BUYABLE=False` (販売停止) と判定済み。つまり「潤沢なFBM在庫があるのに、処理中の
1個のためだけに何日も販売機会を失う」状態になっていた。

ユーザーへ「stranding_riskガードを外すと7/11事故と同型のリスクを再導入する」懸念を
説明した上で確認したところ、回答:「1が何だろうと関係ない。一番重要な事実は『FBA
販売停止である』こと。FBMに多数の在庫があるのに、多くの販売機会を逃している。
重大事故発生中」。→ 機会損失をより重大なリスクと判断し、方針転換。

**訂正 (2026-07-13 ユーザー指摘で判明)**: 上記「7/11事故と同型」という説明は不正確
だった。7/11事故の実例記録 (上記「新規FBA→FBM転換の安全条件」セクション) を再確認
すると、誤変換された6件のうち FUJI-G1504-DR-LL / FUJI-G1415-... は **BUYABLE=True
(=Amazonが「まだ購入可能」と判定、販売中で停止していない)** の状態で誤って転換
されていた。当時のロジックは `fulfillable_qty<=0` という在庫数のみを判断基準にし、
BUYABLE判定を一切見ていなかったことが直接の原因。つまり **7/11事故の再発防止は
「BUYABLE=Falseを主ガードとして必須にすること」で既に達成されている** (今回の
変更でも維持・変更なし)。stranding_risk (今回ブロック条件から外した方) が守って
いたのは性質の異なる別のリスク: 「Amazonが停止と判定していても、FC処理中/転送中の
在庫がわずかに残っている場合、それが将来fulfillableに戻った時に出品が無く孤立する」
という規模の小さい二次的リスクであり、7/11事故 (販売中の出品を誤って止める) とは
別物。

**変更内容 (真の新規転換分岐 `else` のみ。混在チャンネル修復は対象外・変更なし)**:
- `stranding_risk<=0` をブロック条件から**除外**。値は `rec["stranding_risk_ignored"]`
  に記録し、成功時detailに `[参考:FBA残存N を無視して転換(BUYABLE=False優先)]` と表示
- 残るガードは **BUYABLE=False (主ガード、7/11事故の再発防止はこれで担保) + 7S在庫>0
  + 削除対象チャンネルAMAZON_JP限定**
- Codex Lv7 high指摘🟠で追加修正: AMAZON_JP限定ガードは元々`safe_only`時のみ有効
  だったが、stranding_riskを外したことで「唯一の残存防波堤」になったため、
  safe_only の値に関わらず常に適用するよう変更 (手動実行でも未知チャンネル混入は
  needs_reviewに回る)

**リスクの明示的な受容**: 「処理中/転送中の残存在庫がわずかに残ったままチャンネル
削除する」ケースが起こりうる (7/11事故とは別種・より小規模なリスク)。ユーザーが
機会損失側のリスクをより重く見て明示的に許容した判断であり、
再発防止ガードではなく**運用ポリシーの選択**。

**Lv7定時動作再監査 (2026-07-13、5者)**: BUYABLE最優先化を含む日次tick/30分tick
全体を通し監査。DS/Qwenの「ジョブ名が違うため排他されない」「stranding_riskが
Noneのまま比較される」等は実コード確認 (`_JOB_STATE["active"]`は単一共有フラグ、
真の新規転換分岐は到達時点でstranding_riskが必ず有効なint) で誤検知と判明。
Codex med+high/Gemini収束の実指摘3件を修正:
1. **「stranding_riskは監査用のみ」と言いながら、取得APIの失敗だけは転換を
   ブロックする矛盾**。混在チャンネル修復分岐 (stranding_riskが唯一のガード)
   は引き続きブロック、真の新規転換分岐は取得失敗時 `stranding_risk_unavailable`
   として続行するよう分離
2. **朝9時の通知送信がDiscord API障害で失敗しても pending ファイルが削除される
   矛盾** (「サーバー再起動をまたいでも消えない」設計と矛盾)。`_send_discord_notify`
   がbool を返すようにし、送信成功時のみ pending を削除するよう修正
3. 古い「3重ガード/4重ガード/total_qty<=0」コメントを実装と整合する記述に更新

**Codex high 🔴 (ユーザー確認済み・現状維持)**: stranding_riskの中には
`fulfillable_qty` (今すぐ出荷可能な確定FBA在庫) も含まれており、これも無視して
いる点を指摘。ユーザーに確認したところ「完全に例外なし (fulfillable_qtyも含めて
無視する)」と明示的に再確認・現状維持を選択。

## FBA在庫復活の検知通知 (2026-07-13 追加)

**背景**: fbm-syncは一方向 (FBA→FBM) 専用で、FBM→FBA復帰ロジックは存在しない。
Amazon側もFBA倉庫への在庫補充だけではチャンネル設定 (DEFAULT/AMAZON_JP) を勝手に
変えないため、ユーザーがFBAに在庫を補充しても**自動ではFBA出荷に戻らずFBM出荷の
まま**になる。補充されたFBA在庫はストランデッド状態 (誰にも使われない) で放置され続け、
これまで検知・通知する手段がなかった (シミュレーション質問で判明した盲点)。

**実装**: 日次実行の `effective_has_default` 分岐 (純粋DEFAULT、混在チャンネルは
既存のstranding_risk判定と役割が被るため対象外) で、DB上のFBA実在庫
(`c["fba_stock"]` = `InventoryItem.fulfillable_qty`、追加API呼び出しなし) が
0より大きいSKUを検知し、`rec["stranded_fba_qty"]` に記録する。**PATCHは一切行わず、
既存の処理 (v36数量更新) はそのまま継続する** (副作用ゼロの純粋な観測)。
日次通知に「📦↩️ FBA在庫が復活しているのにFBM出荷のまま」セクションとして
SKU全件+商品名+FBA在庫数を列挙する。needs_reviewと同様、解消するまで毎日
再検出・再通知される。30分フォールバックtickは対象SKU (既存FBM管理下) を
そもそも候補に含めないため対象外 (日次のみ)。

## 使い方

### 1. プレビュー (API呼び出しなし・安全)

```
GET http://127.0.0.1:8090/fulfillment/fbm-sync/preview
```

対象件数の内訳を返す。実行前に必ずこれで規模を確認する。

```powershell
Invoke-RestMethod -Uri "http://127.0.0.1:8090/fulfillment/fbm-sync/preview" -Headers @{ "Origin"="http://localhost" }
```

レスポンス例 (v36〜):
```json
{
  "total_candidates": 215,
  "new_fba_to_fbm": 3,
  "new_fba_to_fbm_will_patch": 3,
  "existing_fbm_will_read_live": 212,
  "note": "既存FBM管理下のSKUは実行時にAmazonのライブ在庫数を読んで売り越し防止の再計算を行うため、実際に更新される件数はこのプレビューでは確定しません (existing_fbm_will_read_live はライブ読み取り対象の件数で、そのうちAmazon側が既に正しい値なら書き込みなしでスキップされます)。"
}
```

**v35以前との違い**: 既存FBM管理下 (`existing_fbm_will_read_live`) は実行時に必ずライブAPI読み取りが
入るため、「実際にPATCHされる件数」はこのプレビューだけでは確定しない (実行結果の `results[]` で確認する)。

### 2. 実行

```
POST http://127.0.0.1:8090/fulfillment/fbm-sync/run
```

即座に `{"request_id": "..."}` を返し、バックグラウンドスレッドで処理を開始する。

```powershell
$start = Invoke-RestMethod -Uri "http://127.0.0.1:8090/fulfillment/fbm-sync/run" -Method Post -Headers @{ "Origin"="http://localhost" }
$rid = $start.request_id
```

### 3. 進捗確認 (ポーリング)

```
GET http://127.0.0.1:8090/fulfillment/bulk/progress/{request_id}
```

`bulk` API と同じ進捗エンドポイントを共有する。`done: true` になるまで1秒間隔などでポーリングし、
`results[]` に SKU 単位の `status` (`ok` / `skipped` / `failed`) と `detail` が入る。

```powershell
do {
  Start-Sleep -Seconds 2
  $p = Invoke-RestMethod -Uri "http://127.0.0.1:8090/fulfillment/bulk/progress/$rid" -Headers @{ "Origin"="http://localhost" }
} while (-not $p.done)
$p.results | Group-Object status | Select Name, Count
```

## 実行前の確認事項 (Claude Code が実行を代行する場合)

- 事前に必ず `GET /fbm-sync/preview` で件数を確認し、ユーザーに規模を報告してから実行する
  (`existing_fbm_will_read_live` が数百件を超える場合は特に、実行時間が伸びる旨を明示する)
- ライブ Amazon 出品への一括変更である旨を明示する
- FBA→FBM は **チャネル表示の切替であって在庫移動ではない** (FBA在庫は倉庫に残る、FBM→FBA でも現物は自動移動しない) ことを毎回申し添える
- バックエンドが起動していない場合は `AmzInvAPIServer` タスクの状態を確認する (管理者権限不要、[[reference_pwa_task_restart_no_admin_needed]] 参照)

## 実装箇所

- バックエンド: `022.Amazon在庫PWA/server/routers/fulfillment.py`
  - `_fbm_sync_candidates()` — 対象SKU算出 (DB内で完結、API呼び出しなし)
  - `_run_fbm_sync()` / `_run_fbm_sync_body()` — 実行ワーカー (バックグラウンドスレッド)。
    既存FBM管理下は `listings_client.extract_live_channel_qty()` でライブ在庫数を抽出し、
    売り越し防止の desired 計算を行う
  - `GET /fulfillment/fbm-sync/preview` / `POST /fulfillment/fbm-sync/run`
- `022.Amazon在庫PWA/server/listings_client.py`
  - `ListingItem.fulfillment_availability_live` — `includedData=fulfillmentAvailability` のレスポンス
  - `extract_live_channel_qty()` — 指定チャネル (既定 `DEFAULT`) の quantity を取り出すヘルパー
- DB: `inventory_items.fbm_sync_applied_qty` / `fbm_sync_at` / `fbm_sync_last_s7` (v36で追加, Integer, NULL可)
  - migration: `server/db.py` の `_m35_add_fbm_sync_columns` (user_version=35) / `_m36_add_fbm_sync_last_s7` (user_version=36)
- PWA手動操作 (選択SKU個別のFBA/FBM切替UI): 選択モードの「🚚 出荷方法を変更」ボタン
  (`POST /fulfillment/bulk` — こちらは fbm-sync とは別の、選択SKU限定・都度指定の切替。
  fbm-sync は全SKU横断・自動同期という違いがある)

## 標準FBA出荷指定 (fba_pinned、2026-07-14 追加、2026-07-15 方針訂正)

**背景 (ユーザー要件)**: 商品バリエーション全てをFBM(自社出荷)にすると、その商品の
Amazonプライムマークが一切消える。売れ筋SKUだけは常にFBA出荷を維持したいため、
「このSKUはFBAに預けておくべき」というフラグを導入した。

**用途の確認 (2026-07-14→15 二段階でユーザー訂正)**:
1. 当初「fbm-syncからの保護 (恒久除外)」と実装したが、実際の意図は**絞り込み用
   フラグ**だと判明 (2026-07-14訂正)。
2. さらに2026-07-15、「pinしていてもFBA在庫が0になったら自動的にFBMへフォール
   バックしてほしい。このフラグはあくまでユーザー向けの通知として使う」と明確化。
   これを受けて `_fbm_sync_candidates()` の `fba_pinned == False` 除外フィルタを
   **完全に撤廃**した。

**現在の仕様 (確定)**: `fba_pinned=True` は fbm-sync の候補抽出・実行ロジックに
**一切影響しない**。pinned SKUも他のSKUと全く同じ条件で日次tick・30分フォール
バックtick・手動 `/fbm-sync/run` の対象になり、FBA在庫が0になれば通常どおり
自動でFBM化される。フラグの役割は**表示専用**:
- 一覧の絞り込み (pinned SKUだけを見て、FBA在庫が閾値を下回っていないか監視する)
- 行の📌バッジ表示
- 「FBAに預けておくべき」という運用上の意図をチームで共有するメモ的な位置づけ

**実装 (v38 migration)**:
- `inventory_items.fba_pinned` (Boolean, 既定False)
- `POST /inventory/bulk-fba-pinned` (`bulk-sales-ended` と同型) — 複数SKU一括ON/OFF
- `PATCH /inventory/{sku}/fba-pinned` — 単SKUトグル (行「⋯」メニュー用、2026-07-14追加)
- `_fbm_sync_candidates()` は `fba_pinned` を一切参照しない (2026-07-15撤廃)
- PWA複数選択バーに「📌 標準FBA出荷指定」「📌 指定解除」ボタン
  (`_bulkFbaPinnedSelected`、`sales_ended` 同型パターン)
- 行「⋯」メニューにも「📌 標準FBA出荷指定にする/解除」(`toggle-fba-pinned`)
- 一覧絞り込みショートカット + `filter-select` に `fba_pinned` オプション
  (`_passesNonSearchFilter` に分岐、停止中(Inactive)も含めて表示 — 在庫切れで
  停止したpinned SKUこそ補充判断が必要なため `fba_only` と同じ扱い)
- 一覧に📌バッジ表示 (`fbaPinBadge`、channel-badgeの右)

**経緯メモ**: 2026-07-14のLv7 5者レビューで「恒久除外」設計に対する懸念が
複数出たが、その時点ではユーザーが「絞り込み用フラグ」への訂正を「fbm-syncからの
除外は維持したまま、意味合いだけ絞り込み用と捉え直す」形で確定させたため、
除外フィルタ自体は残していた。翌日2026-07-15に改めてユーザーから「pinしていても
自動フォールバックすべき」と明確な訂正があり、除外フィルタを完全撤廃した。
過去のLv7レビューでの却下判断 (Gemini「認証欠如」等) はこの訂正後も引き続き有効。

## 既知の制約

- FBA方向への変換はこのスキルでは行わない (FBA在庫0→FBM化と、既存FBMの在庫同期のみ)
- 一度FBM化したSKUがFBA側で再入荷した場合、自動でFBAに戻す機能はない
  (`fba_pinned` はfbm-syncの動作に影響しない表示専用フラグ。FBAへ戻すのは
  既存の「出荷方法を変更」ボタンで人間が手動で行う。
  FBAへ戻すのは既存の「出荷方法を変更」ボタンで人間が手動で行う)
- ~~FBM数量が7S在庫と一致していても、Amazon側で手動変更された場合は次回実行で検知できない~~
  → v36で解消。既存FBM管理下は毎回ライブ在庫数を読むため、Amazon側の実際の現在値を必ず確認する
- ライブ在庫数の取得に失敗したSKUはそのままスキップされる (未確認の値を送信しない安全側の挙動)。
  失敗が続く場合は `results[]` の `detail` (「ライブ在庫数取得不可のためスキップ」) で個別に原因調査が必要
