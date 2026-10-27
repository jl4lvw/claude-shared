---
name: mall-product-update
description: 既存商品の掲載内容を、023マスタ・楽天・Eストア・Yahoo!ショッピングの4か所へそろえて変更する標準手順。(A) 商品名・説明・写真の変更(例: 「ケース入り」販売終了)、(B) バリエーション(サイズ・色)の追加(例: Tシャツに S と 4L を追加)。現状の取得→変更前後の比較アーティファクト→承認→Codex確認→023→楽天→Eストア→Yahoo!の順に書き込み→各段階を読み戻し→在庫(GoQ)→Yahoo!店頭反映。「商品名を変更」「写真を削除」「説明を直して」「サイズを追加」「S と 4L も追加」などで起動。新規登録は /register-product。
---

<!-- SKILL_VERSION: 2026-10-21_132958 -->

# mall-product-update — 4 か所の商品掲載を、そろえて変更する

作業フォルダ: `C:\ClaudeCode\023.商品マスタDB`(`tools/` に実例の道具)。
実績: **G0964**(部隊識別コイン・ケース入り販売終了=A)/ **G2242**(Tシャツ S・4L 追加=B)。2026-10-16。

**どのモールも「楽天が正本」ではない。** 023 マスタ → 楽天 → Eストア → Yahoo! の順に、1 か所ずつ書いて、毎回**読み戻して**確認する。
最後に**LINE で完了通知**(別スキル `/line-notify`・画像つき)。

## 絶対に守ること(過去の事故から)

1. **書き込む前に、変更前後を並べたアーティファクトを見せて、承認をもらう。** 承認前に書かない
2. **書き込む前に Codex(medium)に漏れ確認**。`codex exec -s read-only -c model_reasoning_effort=medium - < prompt.txt` を**自分で**実行(Workflow に任せない)。指摘は直してから実行
3. **実行前の現状を `archive/` に日時つきで保存**(上書きしない)。保存が無いと書き込めない作りにする
4. **道具は既定が「確認のみ」、`--apply` で書く。** 段階ごとに: 事前確認(現状が想定どおりか)→ 書き込み → **読み戻し**(変えたもの・変えていないもの)。確認が1つでも外れたら**止まる**(警告で通さない)
5. **途中で止まっても同じコマンドで再開できる**作りにする(終わった部分は検証して飛ばす)
6. 使ってはいけない近道(次の「落とし穴」)

## 落とし穴

| 対象 | 罠 | 正しいやり方 |
|---|---|---|
| 023 → 楽天 | `POST /api/rakuten/push/{親}` は子の `products.stock`(多くは 0)を楽天の在庫として送り、**楽天の実在庫が 0 になる** | 楽天には `rakuten_items.patch_item(mn, {...必要なキーだけ})` で限定 PATCH(`title` / `images` / `productDescription`)。送る前に現状と一致するか確認、送った後に読み戻す |
| 楽天 images | `images` は**配列ごと置き換え**。写真を消すときは残す分を現状から作り直して送る。alt も忘れずに直す(「ケース入り」は「り」付きを先に消す) | 読み戻しで「並び」「alt」「他の項目が不変」を確認 |
| Yahoo! editItem | **送らなかった項目(説明・納期・オプション・サブコード等)を空で上書き** | 商品名・カテゴリ・価格・納期・送料・公開状態を**Yahoo!の現在値(getItem)のまま**送る。読み戻しで「説明以外の全項目が不変」を確認 |
| Yahoo! 写真削除 | editItem では消えない | `deleteItemImage`(POST・`seller_id` と `image_id`)。ID は `seifukunofuji_g0964_3` の形(2022年5月以降の登録画像は `_i_日時` 形式のことがある。getItem の `LibImageN` の URL で確認)。**削除は「反映待ち」**で、店頭は `reservePublish` が別途要る |
| Yahoo! サブコード追加 | editItem で options/subcodes を送っても、**新サブコードは getItem に出ない**(サイトは正常でも出ない) | 続けて `setStock`(新サブコードに数量 0)を呼ぶと現れる。既存サブコードの数量は変わらない |
| Yahoo! の店頭 | `reservePublish` は**ストア全体**が対象 | 実行前に `get_publish_summary(publish_id=0)` で未反映の件数を確認(自分の分だけか)。店頭ページは反映後 **数分**遅れて変わる(5分ほど待って再確認) |
| Yahoo! の snapshot | 023 の `yahoo_snapshot_variants` が古いままだと、後の `api-sync` が**古い8件で editItem して新サブコードを消す** | 追加後に snapshot(12件・`options_text`・`sub_code_field`・説明)を実態に更新 |
| Eストア 軸の再登録 | `PUT /variation`(軸)で選択肢を足すと、枝番号が消える恐れ | 全組み合わせの `variation-attributes`(`branch_code`・`item_price`)を再PUT。**既存の軸の順(例: ネイビー→ブラック)は維持**。読み戻しで既存の数量・価格・枝番号が不変なことを確認(**変わっていたら警告でなく停止**) |
| Eストア 説明 | 説明の PUT は指定キーだけ更新 | `main_description`(+中身のある `sub_description1`)だけ送る。改行は `\r\n`。読み戻しで他キー不変 |
| 023 add_children | 子の追加(`POST /api/products/{親}/children`)は楽天に variant を足し**在庫 0**で登録し、**GoQ再連携を自動で予約する**(途中でも走る) | 全モールの反映が終わったら、`POST /api/queue/goq-resync` をもう一度打つ |
| 税込 | お客様向けの価格は常に税込。Eストアの登録価格は 楽天÷1.1(3,190円 → 2,900円) | 資料・通知は税込で統一 |
| 在庫 | 023 の `stock` は GoQ の実在庫と一致しない | 在庫は **GoQ で設定**(下の「在庫」)。`estore update stock` や 023 の stock は使わない |

## 手順 A: 商品名・説明・写真の変更(例: G0964「ケース入り」販売終了)

**楽天の写真を新しい写真に差し替えるときは `/product-photo` の手順 6**
(`rakuten_photo_replace.py` の spec → 承認の画面 `rakuten_photo` → 本店・Yahoo! へ展開。2026-10-27)。承認は承認の画面(PWA)でだけ取る。
**今の写真を外すだけ**の変更は、今の差し替えの道具では申請できない(1 枚目が新しい写真であることが必須)。
下の G0964 の実例のように直接書き込まず、運用者に相談する(道具を外すだけにも対応させるか決めてから)。

1. **現状を取る(読み取りのみ)**: 023(`/api/products/export`・`/api/products/{sku}/images`)、楽天(`rakuten_items.get_item`)、Eストア(`/items/{code}/basic`・`/images`・説明 `_fetch_description_pc`・店頭 `http://fuji.ue.shopserve.jp/SHOP/{code}.html`)、Yahoo!(`yahoo_shopping_api.get_item`)
   - 写真は**実物を見る**(縮小画像を並べる)。写真の中の文字・写り方で判断する。ランキング画像の中の小さな画面など、判断が分かれるものは**残す案にして確認**する
2. **変更前後のアーティファクト**を作る(モールごとの商品名・説明・写真・alt)。キャンペーン表記(例「10/17まで ポイント5倍」)は残す。関連商品欄の別商品の文言は触らない
3. 承認 → 道具を作る(下の実例を雛形に)→ ドライラン → Codex確認 → 順に `--apply`
4. 023 では画像削除前に**元ファイルを archive へ退避**。画像削除は `estore_image_map` を無効化する(後続の自動処理が削除済みを再送しない)
5. 完了後 `/line-notify` で通知

実例: `tools/g0964_remove_case.py`(`pm` / `rakuten` / `estore` / `yahoo`)。

## 手順 B: バリエーション(サイズ・色)の追加(例: G2242 に S・4L)

1. **現状と命名規則を確認**: 子 SKU(`G2242-BK-3L` 形式・色コード BK/NV 等は `estore_register.COLOR_CODE_MAP`)、楽天 `merchantDefinedSkuId`、Eストア枝番号、Yahoo! サブコードが**同じ名前**になっているか。既存の対応が想定どおりか**事前に全件照合**する
2. **聞くこと(アーティファクト前に)**: 追加する色×サイズ、**寸法(胸囲・着丈・肩幅)**、価格(既存と同額か)、**在庫数**。在庫数は最初に聞く
3. **アーティファクト**: 追加する SKU の表(価格は楽天/Eストア/Yahoo!別)、説明の寸法欄の前後、書き込み順と安全策
4. 承認 → 道具 → ドライラン → Codex確認 → 順に実行:
   1. **023 + 楽天**: `POST /api/products/{親}/children`(`If-Match: 親version`・`sync_to_rakuten: true`)。既存の価格・在庫の不変、サイズ軸の並び(S→M→L→LL→3L→4L)、新 variant の価格・在庫0を読み戻す
   2. **楽天の説明**: `patch_item(mn, {"productDescription": {"pc":…, "sp":…}})`。寸法欄は M の前に S、3L の後に 4L の行を足す。023 の `rakuten_description` も同じ形に
   3. **Eストア**: 軸 → 全組み合わせの枝番号・価格 → 説明(上の落とし穴)
   4. **Yahoo!**: editItem(現在値のまま+説明の寸法行+options+subcodes)→ `setStock` で新サブコード 0 → getItem で 12 件・既存数量不変 → 023 snapshot 更新
5. **GoQ**: `POST /api/queue/goq-resync` → 完了を待つ(判定は遅れて出る) → `POST /api/goq-browser/search {"item_code": "G2242"}`(読み取り)で行を取り、**色・サイズ・商品コードが一致する行だけ**を確認表にする
6. **確認表を見せて承認** → 各行に `POST /api/queue/set-quantity {"item_code","quantity","row_ids":[行ID]}`。結果の `changed_fields == ["stock[行ID]"]` と数量を確認。**楽天・Eストア・Yahoo!の3か所で数量を読み戻す**
7. **Yahoo! 店頭反映**: 未反映件数を確認 → `reserve_publish(mode=1)` → 未反映が 0 になる/反映履歴の時刻 → 店頭ページで寸法行(数分待つ)
8. `/line-notify` で通知

実例: `tools/g2242_add_sizes.py`(`backup` / `pm` / `rakuten-desc` / `estore` / `yahoo` / `verify`)。

## 書き込みの前提(023 の API)

- ベース `http://127.0.0.1:8290`、ヘッダ `X-API-Key`(`023.商品マスタDB/.env` の `INTER_PWA_API_KEY`)・`X-Actor`。書き込みはループバックから PIN 不要。**キーを表示・記録しない**
- 外向きの名前(`*.sfuji.f5.si`)は社内 LAN から引けない。ローカルの API・Caddy は 127.0.0.1 で直接叩く

## 通知

完了したら `/line-notify`。画像には「何をしたか・反映先・変更前後・写真・注意点」を入れる。LINE のグループ名は寺下さんに確認する(例: 楽天メルマガ配信)。
**グループへ送る前に、寺下さんの1対1トークで承認をもらう**(2026-10-20〜 LINE WORKS の承認トーク)(`/line-notify` の手順3)。承認前に送らない。
`request-approval` には `--purpose` を付ける。例: `--purpose "楽天・Yahoo の商品ページの写真と説明を直したことを楽天メルマガ担当に知らせるため"`
- **purpose(目的)は必須。何のための操作か(誰に何を知らせる・何を補充する等)を 6〜120 字で書く。承認の画面・LINE WORKS・電話の音声案内で運用者に示す。電話の文は外部の音声サービス(Microsoft)へ送るので、人名・住所・電話番号・メールアドレス・お客様名は書かない**(2026-10-21 運用者決定。`request-approval --purpose` が無い・短い・長いと承認を求めずに止まる)

## 関連

- 新規登録: `/register-product` / 在庫の設定: `/goq-stock-set` / Amazon: `/amz-register`
- 道具の雛形: `023.商品マスタDB/tools/g0964_remove_case.py` と `g2242_add_sizes.py`(商品固有の定数が埋め込み。**別の商品では、定数・命名・事前確認を読み直して作り替える**。そのまま使い回さない)
- 道具は商品ごとに作る運用(汎用化はしていない)。汎用化するなら、事前確認と読み戻しの項目を先に決める
