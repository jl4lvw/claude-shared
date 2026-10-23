---
name: sku-rekey
description: 092(自前在庫連携PWA・zaiko.sfuji.f5.si)で「SKU未統一」と出る商品を、023 の SKU統一(3モールのSKU番号をそろえる)→ 023 と 092 の子SKUのキー(products.sku)を SKU番号(例 G0862-NV-S)へ付け替える → 092 に載っていなければ追加する、までの標準手順。付け替えは承認の画面(PWA)で承認されたときだけ実行する。「SKU統一して092に再登録」「SKU未統一を直して」「子SKUを付け替えて」「/sku-rekey G0862-parent」で使う。
---

<!-- SKILL_VERSION: 2026-10-23_223000 -->

# sku-rekey — SKU統一と 092 への再登録

2026-10-23 に G0862-parent(呉鎮守府Tシャツ・子10件)で確立した手順。

## 何をそろえるのか
- **モールのSKU番号**(楽天のシステム連携用SKU番号・Yahoo!のサブコード・Eストアの枝番号)= 023 の **SKU統一**で `G0862-NV-S` 形式にそろえる
- **023 の子のキー**(`products.sku`)= 数字(`15633`)や `G1845-r-sku00000001` のことがある。092 の台帳のキーも同じ値。
  これを SKU番号に付け替えると、092 の「SKU未統一」が消える(`shadow/web/catalog.py` の `unify_state`)
- 楽天の `rakuten_variant_id` と `parent_sku` は**変えない**(楽天 pull は variant_id で子を突き合わせるので、子が二重にならない)

## 道具
| 道具 | 役目 |
|---|---|
| 023 PWA の SKU統一 / `GET http://127.0.0.1:8290/api/skuunify/{親}/preview` | モールのSKU番号をそろえる・済んでいるか見る |
| `023.商品マスタDB/tools/rename_child_sku.py` | キーの付け替え本体。既定は確認だけ。`--apply --plan-hash` で書く。`--reverse 対応表.csv` で戻す |
| `900.ClaudeCode/sku_rename/cli.py` | 申請を積む(`enqueue`)。見張り役が承認の画面で承認を求め、承認されたら `run` を起動する |
| `.claude/skills/sku-rekey/verify.py` | あとの確認(読み取りだけ) |

## 手順

### 1. 現状を見る(読み取りだけ)
```
curl -s http://127.0.0.1:8290/api/skuunify/G0862-parent/preview
```
- `rakuten_change_count=0`・`yahoo.changed=false`・`estore.changed_count=0`・`errors=[]` なら、モールは統一済み → 手順3へ
- 親は `XXXX-parent` の形で指定する(素の `G0862` は 404)

### 2. モールのSKU統一(済んでいないときだけ)
023 PWA の SKU統一で行う(運用者の操作・承認)。既知の罠は必ず先に読む:
[[project_023_g1780_sku_unify_yahoo_stock_headline]](Yahoo在庫が023の古い数で上書き・Headline消失)/
[[project_023_g1845_sku_unify_20261019]](Eストアの表記違いは estore_map・Yahoo商品名が楽天名に変わる)/
[[project_023_sku_unify_yahoo_pending_refresh]](Yahooは reservePublish が別・snapshot refresh)。
終わったら手順1をもう一度見て、変更0件を確かめる。

### 3. 付け替えの確認(何も書かない)
```
cd C:\ClaudeCode\023.商品マスタDB
python tools/rename_child_sku.py --parent G0862-parent
```
旧→新の表・各表の行数・ハッシュが出る。`[NG]` があれば止まる(終了コード2)。主な止まり方:
- SKU番号が無い子がある / 新しいキーが他の行・他の商品のSKU番号と重なる
- 子SKUに画像・セット商品の構成がある(未対応)
- 023 に未処理のジョブ(子・親・商品コードが対象)/ 092 でモールへの反映・販売停止の送信が途中
- 092 に旧キーの大文字小文字違いがある / **GoQ にまだ商品が残っている**(092 の最新の重複チェックで判定)
- 3モールのSKU統一が済んでいない(プレビューと照合)

### 4. 申請を積む → 承認の画面で承認 → 自動で実行
```
cd C:\ClaudeCode\900.ClaudeCode
python -m sku_rename.cli enqueue G0862-parent --purpose "092で子SKUをシステム連携用SKU番号で扱うため"
```
- 承認はチャットで求めない。見張り役が LINE WORKS で知らせ、運用者が承認の画面で承認する
- 承認を待つ間は `900.ClaudeCode/a_browser_data/watch.log` を自分で見張り、`sku_rename id=N 実行終了 status=done rc=0` を待つ
  (rc=6 は「付け替えたが旧キーの行ができた」=要確認)
- 結果は `python -m sku_rename.cli show N`
- 実行の中身: 023・092 の DB を backup API で写す(`*.bak_rename_日時`)→ 092 のリース「cycle」を取る →
  対応表 `023.商品マスタDB/data/sku_rename_map_日時.csv` を書く → 092→023 の順に BEGIN IMMEDIATE → 両方を書き換えて検査 →
  023→092 の順にコミット → 023 をチェックポイント → 5秒後に旧キーが戻っていないか確かめる

### 5. あとの確認
```
python C:/ClaudeCode/.claude/skills/sku-rekey/verify.py G0862-parent
```
`OK` で完了。見るもの: 023 のキー=SKU番号・variant_id あり / 092 の台帳と自前連携 / 092 の未統一 0 /
書き先(楽天=管理番号+variant_id・Yahoo!=サブコード・Eストア=選択肢)/ 092 内部API が managed=true /
023 の切り替え済み判定(GoQ へ書かない判定)が新しいキーを返す。

### 6. 092 に載っていない商品だったとき(再登録)
付け替えのあと、092 の画面の「商品新規追加」(Ctrl+N / Alt+N)で親SKUを追加する。
GoQ 側でその商品を消す決まりは従来どおり(092 RUNBOOK「SKU を GoQ から自社へ切り替える」)。
すでに 092 に載っている商品は、付け替えで台帳の数・履歴・切り替え済みがそのまま新しいキーへ移るので、**外して登録し直さない**
(登録し直すと数と履歴が引き継がれない)。

## 戻し方
```
cd C:\ClaudeCode\023.商品マスタDB
python tools/rename_child_sku.py --reverse data/sku_rename_map_日時.csv               # 確認だけ
```
書き戻しも同じく承認が要る(今は `sku_rename.cli` は付け替えの向きだけ。戻すときは運用者に相談して `--apply --plan-hash`)。
最後の手段は `*.bak_rename_日時` の DB に戻す(その後に入った注文・修正は失われる)。

## 既知のこと(2026-10-23 時点)
- Eストアの「LL」が今朝の写しで見つからない(G0862 の選択肢は `LＬ(XL)`・023 は `LL`)。キーとは関係ない表記の違い。
  092 から Eストアへ LL の在庫を書くと失敗する恐れ → 別途 023 の値かEストアをそろえる
- 023 の切り替え済み判定は、商品コード(`G0862`・`g0862`)で聞くと空になる(親SKUが `G0862-parent` のため)。
  付け替えの前からの動き。GoQ から商品を消してあれば実害はない
- 023 の子SKU 3,389件のうち、キー=SKU番号は 59件だけ(10/23)。1件ずつ進める。まとめて流すときは対象一覧を出して承認を得る
- 新しい子SKU(新規登録)は最初から SKU番号のキーで作られるものがある(G2178 など)
