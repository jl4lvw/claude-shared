---
name: mercari-shops-stock
description: メルカリShops(制服のフジ)の在庫を、基準の在庫(Eストア)にそろえる標準手順。管理画面から商品データCSVを取り直し、Eストア在庫と突き合わせて差分CSVと承認用の一覧を作り、承認後にChromeでアップロードして、取り直したCSVで照合する。メルカリShopsはAPI連携ができないため、この手順が唯一の更新経路。029.メルカリショップス登録の stock_sync を使う。
trigger: 「メルカリの在庫を更新」「メルカリShopsの在庫をEストアにそろえて」「メルカリ在庫同期」「/mercari-shops-stock」。メルカリShopsの在庫を変える話が出たとき
---

<!-- SKILL_VERSION: 2026-10-23_1700 -->

# mercari-shops-stock — メルカリShops 在庫同期

対象: **制服のフジ メルカリ店**(`--store fuji`)。基準の在庫は Eストア(管理APIの読み取りのみ)。
コードは `029.メルカリショップス登録/stock_sync/`、店舗の設定と確定した対応表は `stores/fuji/`。

## 絶対に守ること

1. **承認の前にアップロードしない。** 承認の取り方は「承認用の一覧(アーティファクト)を見せ、チャットで『アップロードして』と返事をもらう」(2026-10-23 運用者決定)。
2. **ログインは運用者が行う。** メルカリ管理画面にはSMS認証がある。パスワード・確認番号は入力しない。表示が出たら運用者に頼んで待つ。
3. **ダウンロードは許可を得てから。** ファイル名(`product_data_日付.csv`)・取得元(管理画面)・大きさ(約2MB)を伝え、運用者の「よい」を得る。同じ作業の中の取り直し(照合用)は、最初に「更新前と更新後の2回取ります」と伝えておけば再確認は要らない。
4. **在庫は差分でしか更新できない。** 商品データを取り直してから、計画・承認・アップロードまでを続けて行う。時間を空けると、売れた分でずれる。承認待ちが長引いたら取り直して計画を作り直す。
5. **触らないもの**(コードが自動で除外する): 非公開の商品 / Eストアが99999999または900以上 / メルカリが99または900以上 / 対応先が未確定。
6. **最初の本番更新は、承認用の一覧の「大きな変化」を運用者と一緒に確認する。** 上限(`max_updates` 既定500件)を超える更新は、一覧の注意を読んだうえで明示的に承認をもらう。
7. **LINEなど他への報告は、頼まれたときだけ。** 承認なしに投稿しない。

## 手順

作業フォルダ(RUN)は毎回新しく作る: `exports/stock_sync/fuji/<日時>/`(下では `$RUN`)。

### 1. メルカリ管理画面を開く
`tabs_context_mcp` → 新しいタブで `https://mercari-shops.com/seller/shops/ZmyUKx762YtoZNfau4EbnK/products/update` を開く(`stores/fuji/store.json` の `mercari_admin_base`)。ログイン画面や電話番号の確認が出たら、運用者に頼んで完了を待つ。商品一覧が表示されたら次へ。

### 2. 最新の商品データを取り直す(ダウンロード)
1. 時刻を控える: `date '+%Y-%m-%d %H:%M:%S'`
2. 「商品データをダウンロード」を押す。**別タブで `…/products/download` が開く**。条件は指定せずに「作成」を押す。
3. 履歴の最上段が「完了」になるまで待つ(10〜20秒。ページを開き直して確認)。ページ末尾へは `End` キー(スクロール量は10まで)。
4. 最上段の「ダウンロード」を押す。
5. 作業フォルダへ写す:
```bash
cd "C:/ClaudeCode/029.メルカリショップス登録" && python scripts/stock_sync_cli.py find-csv --after "<控えた時刻>" --copy-to "exports/stock_sync/fuji/<日時>"
```
元のダウンロードは触らず、`product_before.csv` として複写される。見つからなければダウンロードの完了を待つ。

### 3. 計画を作る(Eストア在庫の取得を含む)
```bash
cd "C:/ClaudeCode/029.メルカリショップス登録" && python scripts/stock_sync_cli.py plan --store fuji --product-csv "exports/stock_sync/fuji/<日時>/product_before.csv" --fetch --out-dir "exports/stock_sync/fuji/<日時>"
```
- `--fetch` はEストアの在庫を読み取り(GET)で取得する(919商品で約15分。**`run_in_background` で実行**し、終わりの通知を待つ)。取得済みのキャッシュ(`stores/fuji/estore_cache.json`)は再利用されるが、**更新の計画には毎回 `--refresh` を付けて取り直す**。
- 出力: `approval.html`(承認用の一覧)・`stock_update_日付.csv`(差分CSV)・`..._REVERT.csv`(巻き戻し用)・`plan.csv`・`summary.json`。
- 終了コード: 0=正常 / 1=検算NGなど / 2=更新数が上限超過(更新CSVは作られない。一覧の注意を見せて、承認が出たら `--force-limit` を付けて同じコマンドを再実行する。このとき `--fetch --refresh` は付けずに、取得済みのキャッシュを使う=同じ内容で更新CSVだけ作る)。
- 数字は取得のたびに少し動く(同じ日の2回の取得で更新815件→816件)。承認した一覧と更新CSVの件数が一致することを、アップロードの前に確かめる。

### 4. 承認用の一覧を見せる
`approval.html` をスクラッチパッドへ複写し、Artifact で公開して開く(タイトルは既存のままでよい)。チャットには次を日本語で要約する: 更新SKU数・増やす/減らす個数・大きな変化の上位・上限超過の有無・**Eストア在庫の取得時刻**。そのうえで「アップロードしてよいか」を聞き、返事を待つ(返答の最後に `【対応待ち】`)。

### 5. 承認後: アップロード(Chrome)
1. `…/products/update` を開き、`find` で「CSVファイルのアップロード用 file input」を探す。
2. `file_upload` に、その ref と `$RUN` の差分CSV(`stock_update_日付.csv`)のフルパスを渡す。**ファイル選択ボタンは押さない。**
3. 表示が「アップロード」ボタンに変わったら、それを押す。確認画面(「CSVで商品を一括変更する」)が開く。
4. 確認画面で、商品の件数が一覧の商品数と一致すること、数行の増減数が計画どおりであることを**スクリーンショットで目視**する(`get_page_text` は列の順序が崩れるので判断に使わない)。
5. 「一括で変更する」→ ダイアログの「一括で変更する」を押す。「商品を一括変更しました」の表示で成功。**押したあとは取り消せない**(戻すときは巻き戻し用CSVを同じ手順でアップロードする)。

### 6. 照合
手順2をもう一度行い(控える時刻も取り直す)、取り直したCSVを作業フォルダへ `--name product_after.csv` で写す。
```bash
cd "C:/ClaudeCode/029.メルカリショップス登録" && python scripts/stock_sync_cli.py verify --store fuji --before "$RUN/product_before.csv" --after "$RUN/product_after.csv" --update-csv "$RUN/stock_update_日付.csv"
```
- 終了コード0=すべて目標の在庫。「対象外なのに在庫が変わった」は、更新中に売れた・入荷した分で正常。
- 1=未達や公開状態の変化がある。未達のSKUを運用者に報告し、巻き戻すか決めてもらう。

### 7. 片付けと報告
Chrome のタブを閉じる。結果(更新SKU数・増減の個数・照合の結果)を日本語で報告する。必要ならメモリ `project_029_mercari_shops_multistore_inventory` に追記する。

## 対応先が決まらないSKU

計画の「対応先が決まっていない公開SKU」は更新されない。人が確定するときは `stores/fuji/mapping.json` の `overrides` に書く(キーは `SKU_ID` または `"管理コード|種類"`)。
```json
{"overrides": {"PG1004-YMT-T4-MEBKM|M/ブラック": {"action": "map", "item": "G1004", "pattern": "ブラック/M"},
               "G0258free|フリー": {"action": "skip", "note": "Eストア未出品"}}}
```
`action: map` は Eストアの商品番号(`item`)と種類(`pattern`。単品は空)、`skip` は対象外にする。書き換え前に `.bak_日時` を残す。

## 事前確認と注意

- 事前にテスト: `cd "C:/ClaudeCode/029.メルカリショップス登録" && python -m pytest tests/test_stock_sync_*.py -q`
- 更新CSVは cp932・CRLF。Excelで開いて保存し直さない(文字コードが変わる)。
- 管理コードが空のSKUは対応づけできないため対象外。
- 履歴のCSVはアップロード後7日で消える。
- 関連: `029.メルカリショップス登録/README.md`(在庫同期の節)、`stores/fuji/store.json`(更新ルール)。
