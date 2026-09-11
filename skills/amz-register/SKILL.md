---
name: amz-register
description: 022.Amazon在庫PWAで新規商品(親+バリエーション子)をAmazonへ新規登録する標準手順。023商品マスタDB・Eストア公開ページから情報を集め、画像を選定・アップロードし、レビューアーティファクトで確認を取ってから、既存のbatch/preview→batch/submitパイプラインでAmazon側に登録する。G2200・G0673・G0942の実登録(2026-08-27〜09-01)で確立した手順。
trigger: 「Gxxxxを登録したい」「Amazonに新規出品したい」等、023マスタの商品番号(G####)をAmazonへ新規登録したいとき
---

<!-- SKILL_VERSION: 2026-09-11_140000 -->

# amz-register — Amazon新規商品登録

対象システム: `022.Amazon在庫PWA`(サーバー既定 `http://127.0.0.1:8090`)。
新規 parent + children を Amazon へ本登録するまでの標準フロー。**既存のテスト済みパイプライン
(`/register/images/upload`、`/register/batch/preview`、`/register/batch/submit`)を使う。
生スクリプトで SP-API を直接叩かない** — これらのエンドポイントは画像パイプライン(3段化
アップロード)・属性合成(`_inject_batch_common_attrs`等)・PIN認証・submission管理を
一括で担っており、車輪の再発明は不整合の元。

## 0. 起動時確認

- 対象G番号(1件または複数)をユーザーから受け取る
- サーバー起動確認: `curl -s -o /dev/null -w "%{http_code}\n" http://127.0.0.1:8090/docs` が200であること
- marketplace_id は `A1VC38T7YXB528`(config.settings.marketplace_id と一致確認)

---

## Phase 0: 情報収集

1. **023商品マスタDB**(`023.商品マスタDB/data/productmaster.db`, テーブル `products`)を
   対象G番号で検索(親行・子行・`description_main`等の説明カラム)。
   **CP932コンソール対策**: Bashで直接クエリせず、`sys.stdout.reconfigure(encoding="utf-8")`
   を先頭に入れたPythonスクリプトファイルを書いて実行する(AGENTS.md厳守事項)。
2. DBが薄い(価格・サイズ・説明が空)場合、**Eストア公開ページ**を直接閲覧して補完する:
   `https://seifukunofuji.co.jp/SHOP/{G番号}.html` — カラー・素材・サイズ表(cm)・価格・
   JANコード・サイズ別在庫状況が載っている。
3. **重複チェック**: `022/server/amzinv.db` の `inventory_items` を商品名キーワード・SKU
   パターン(G番号含む)で検索。**テキスト一致だけで「重複」と判定しない** — ヒットしたら
   必ず商品画像も見比べて実際に同一商品か確認する(ワッペン案件でテキストのみ判定が
   誤検知した実例あり)。

---

## Phase 1: 画像収集・選定

### Step 0: 楽天トップ画像の再取込み(必須・毎回)

ローカル `C:/ProductMaster/images/{G番号}-parent/` は `023.商品マスタDB` の楽天連携
パイプライン(`server/services/rakuten_image_import.py`)が書き込み先にしているフォルダ
であり、**楽天側の最新出品画像と自動では同期しない**(オンデマンド更新のみ)。作業開始前に
必ず最新化する:

```
POST http://127.0.0.1:{023のポート}/rakuten/refresh-images/{sku}
```

**注意(2026-09-01 ユーザー確認済み)**: このAPIは楽天側 `images[]` の**全件で全置換**する
仕様(トップ画像だけを選んで更新する機能はない)。sub_02/03のような独自追加の高解像度写真が
楽天側に存在しなければ、全置換で消える可能性がある。実行前にその旨をユーザーに一言断ってから
進めること。子SKU指定でも親SKUの画像として保存される(既存規約)。

### Step 1: ローカル画像フォルダの確認

`C:/ProductMaster/images/{G番号}-parent/`(main.jpg + sub_NN.jpg)の全カットについて、
実際のpxサイズをPILで確認する:

```python
from PIL import Image
im = Image.open(path)
print(im.size)  # Amazon要件: 長辺1000px以上
```

banner合成(サイズ表記・キャッチコピー入りの販促画像)・レビュー画面のスクリーンショット
なども混在し得る。**判定はするが、勝手に候補から除外しない**(2026-09-01 ユーザー指摘:
除外可否はユーザーが決める)。

**メイン画像は背景が純白(255,255,255)か必ず実測する**(2026-09-01追加)。Amazon規約は
メイン画像の背景を純白と規定している。周辺部をサンプリングして純白ピクセル率を確認する:

```python
import numpy as np
from PIL import Image
im = Image.open(path).convert("RGB")
arr = np.array(im)
# 外周8%帯をサンプリングし、(255,255,255)率を見る。100%未満なら要修正
```

純白でなければ**AI/ML系の背景除去は使わない**(過去の経験で精度が不安定というユーザー
判断)。決定論的なスクリプトで対応する: 明度が低い/彩度がある画素から商品のバウンディング
ボックスを検出し、その外側(背景)だけを四隅からのBFS連結領域として純白化する。
**単純な明度しきい値だけで塗ると、白い商品自体まで塗りつぶして質感が壊れる**
(2026-09-01 実際に発生: 白いバッグの画像で最初はしきい値だけの実装を使い、フラップの
織り目テクスチャが斑に消えた。商品バウンディングボックスをマージン付きで保護してから
再実行して解決)。参考実装: このスキル更新時のセッションで作成した
`whiten_bg.py`(商品検出→バウンディングボックス保護→四隅BFS純白化)を再利用または
同じロジックで都度書く。修正後は必ず元画像と並べて目視比較し、商品側に変化がないか確認する。

**メイン画像は必ずユーザー提供のファイル/URLを使う。候補グリッドから選ばせない**
(2026-09-09確立)。Rakuten側の候補画像は純白に近くても厳密には255,255,255でないことが
多く(実測 249〜254台がほとんど)、Amazon規約の純白要件を安全に満たせるのはユーザーが
別途加工した専用画像だけ。ユーザーには「メイン画像(純白背景・2000×2000px)をダウンロード
フォルダへ保存してください」と依頼し、保存されたファイルを四隅ピクセル値で確認してから使う。

**メイン画像は商品をフレームいっぱいに大きく、余白は最小限にする**(2026-09-01追加)。
`whiten_bg.py`で求めた商品バウンディングボックスを使い、アスペクト比を保ったまま
ボックスぎりぎりまでクロップ→正方形キャンバスにリサイズする(`crop_tight.py`)。
**余白の量は必ずユーザーに確認する**(自動で決めない) — 実際のやり取りでは
「ギリギリまで」という指示で余白比率3%(長辺の3%)を試したところ「小さすぎる」と
差し戻され、10%に変更して採用された。**変更後は必ずアップロード前にローカルで
生成しユーザーに見せる**(`SendUserFile`等)。承認を得てから
`/register/images/upload`へ反映し、VALIDATION_PREVIEWを再実行する。

**画像やペイロードを一つ直した後、次の変更に進む前には必ずユーザーの明示的な
「進めてよい」を待つ**(2026-09-01 実際に注意された: 前の修正の勢いのまま
VALIDATION_PREVIEW再実行やスキル更新まで自動で続けてしまい、「最終ゴーサインが
出てないのに勝手に進めてはいけません」と指摘された)。1つの確認事項が解決しても、
次のアクション(再アップロード・再検証・本登録)は都度個別に承認を得る。

### Step 2: 不足時の選択肢をユーザーに確認

1000px以上のカットが足りない場合、**自動で決めず**以下をAskUserQuestionで確認する:
- 高解像度版を別途提供してもらう(NAS共有パス等で受け取る。`\\192.168.1.50\...`形式)
- Eストア/ローカルの低解像度版(800px等)を**強制拡大**(画質補完なし、単純リサイズで
  1000px以上に引き伸ばすだけ)して使う — ユーザーが明示許可した場合のみ

### Step 3: 候補一覧アーティファクト(必須・毎回・省略禁止)

**商品登録のたびに、他画像(other_1〜6)の選定は必ずクリック選択式アーティファクトで行う。**
チャットで「どれにしますか」とテキストで聞いたり、Claudeが勝手に選んだりしてはいけない
(2026-09-11 ユーザーから明示指示: 「商品登録の時は、必ず他画像を選択するためのアーティファクトを
表示してください」)。候補が2枚以下で実質選択の余地が無い場合も、確認の意味でアーティファクトは
出す。候補が1枚しかない(または0枚)場合のみ、AskUserQuestionでその旨を確認すればよい。

**必ず以下のテンプレート+ビルドスクリプトを使う。手書きHTML/JS・Pythonの文字列埋め込みは
禁止**(付録「既知の落とし穴」参照 — ブレース二重化エスケープ事故が実際に起きた):

```bash
python C:/ClaudeCode/.claude/skills/amz-register/build_image_picker.py \
  --candidates <G番号>_candidates.json \
  --img-dir "C:/ProductMaster/images/<G番号 or G番号-parent>" \
  --output <G番号>_image_picker.html \
  --slug <g番号小文字> \
  --title-tag "<G番号> 画像候補(クリック選択)" \
  --eyebrow "Amazon出品 画像選定 · <G番号>" \
  --h1 "<G番号> 他画像を選択" \
  --max-select 6
```

候補JSON(`<G番号>_candidates.json`)は `{"name","label","url"}` の配列。`url` は
Rakuten CDN元画像(`build_image_url()`参照)を入れると、アーティファクト側で長押しコピーが
使える。**全候補**(除外候補も含む)を入れ、除外を勧めたい画像(レビュー画面・ランキング
バナー等、商品写真ではないもの)にはlabelで理由が分かるようにするが、候補からは外さない。

publish後は `grep -c '{{' out.html` が0であることを確認してから `Artifact` で公開する
(`capabilities: {"db": {}}` 必須)。

選定結果は `read_db`(`collection: <slug>`, `doc_id: "other_image_selection"`)で
「A, C, E, F, G, I」のような順序付きアルファベット列として受け取り、その順番を
`other_1`以降への割当順として扱う(=主観的にどの画像を目立たせたいかの意図を尊重)。

### Step 4: アップロード

```bash
curl -s -H "Origin: http://127.0.0.1:8090" \
  -F "parent_sku=<PARENT_SKU>" -F "variant_key=<parent|色キー>" -F "role=main" \
  -F "file=@<path>;type=image/jpeg" \
  http://127.0.0.1:8090/register/images/upload
```

- `variant_key`: variation_themeがSIZE単独なら全SKU共通で `"parent"`。COLOR軸がある場合は
  子ごとの色キー(英数字、customer向け表示名ではない)。
- `role`: `main`(1枚)/ `pt` + `display_slot`(1〜6) / `swatch`(variant_keyが"parent"以外の
  子には必須 — 無いと `POST /register/batch/preview` が422で拒否する)。
- **容量上限は main 1枚 + other_1〜6 の最大6枚(7枚ではない)**。`other_7`は存在しない
  (2026-09-01 実際に1枚オーバーして気づいた失敗例あり。事前に必ず計算しておく)。
- 同じ `(parent_sku, variant_key, role, display_slot)` に再アップロードすると新
  generationとして自動的に旧画像を置き換える(明示的な削除操作は不要)。

---

## Phase 2: 仕様確認(必ずユーザーに聞く。自動で決めない)

以下は「好みの選択」ではなく仕様判断のため、CLAUDE.mdの自動選択ルールの**例外**扱い。
毎回 AskUserQuestion で確認する:

### Step 0: item_name(商品名)候補の提示(2026-09-01追加・必須)

**item_nameを1案だけ作って提示しない。** 過去に登録済みの同カテゴリ・同シリーズの実際の
Amazon出品(例: 「かが」Tシャツなら G1846/G1635/T2401/G1715/G2200等)を
`inventory_items`(商品名キーワード検索)で洗い出し、その命名パターン(ブランド接頭辞の
有無、括弧の使い方、キーワード順、色・サイズの入れ方)を踏まえて**候補を3〜4案**作り、
AskUserQuestionで提示する(ユーザーは選択肢から選ぶか、「Other」で直接指示できる)。
1案だけ出して黙って確定させない(2026-09-01 実際に抜けていた手順)。

候補は「ブランド接頭辞あり/なし」「キーワード列挙型/文章型」「呉・広島等の地域語を
入れる/入れない」など、実在パターンの幅を意識して作ると選びやすい。

**候補提示時は必ず文字数を実測して併記する**(2026-09-01追加)。Amazon商品名は
**75文字制限**(2026-07-27施行、Mediaカテゴリ以外の全カテゴリ対象、SHIRT含む。
超過分はAmazonのAIが無断で自動書き換えする)。全角/半角の区別なくPythonの`len()`で
数えた文字数がそのまま上限とみなしてよい(Amazon公式もバイト数ではなく文字数ベースと
案内)。**サイズ接尾辞込みの最長ケース(`(NV,3L)`等)で計測すること** — サイズ違いで
文字数が変わるため、末尾が一番長くなる組み合わせで確認する。候補は75文字ギリギリではなく
余裕を持たせつつ、ユーザーが「もっとキーワードを詰めたい」と言えば70文字前後まで
攻めた案を作り直す(2026-09-01 実際にこの往復が発生した)。

### Step 1以降: その他の仕様確認

| 項目 | 備考 |
|---|---|
| Amazon価格 | Eストア価格とは別に設定されることが多い。DBの`price`と`estore_price`のどちらとも異なる場合がある。**同シリーズを続けて登録する場合でも毎回確認する**(前件と同額を無断で流用しない) |
| `item_package_weight` | DBに無いことが多い。同カテゴリ既存品(例: 他の「かが」Tシャツ)の値を**提案しつつ**確認する |
| サイズ展開 | 実在サイズのみか、4L/5Lなど023マスタに無いサイズも強制登録するか |
| 4L/5L等の推定値 | 同一生地・同一型の既存兄弟商品がないか実測値を照合してから提案する(例: 胸囲=身幅×2、着丈完全一致、肩幅一致で「同一型」と確認できたケースあり)。一致する兄弟が無ければユーザーに参考データを依頼する |
| SKU命名規則 | 接頭辞を含め、既存の確立ルール・実データと食い違わないか必ず確認する。「そうする予定だった気がする」的な記憶ベースの指示は、実データ(DB行・過去ペア)で裏取りしてから採否を決める |

---

## Phase 3: レビューアーティファクト(Amazon商品ページ・シミュレーション形式)

**属性を並べただけの表ではなく、実際のAmazon商品ページに近い見た目でシミュレーション
表示する**(2026-09-01 ユーザー要望・必須)。データ確定の途中経過(仕様確認中)は簡易な
属性カードで良いが、**VALIDATION_PREVIEWがissues 0件になり、本登録の可否をユーザーに
判断してもらう段階では必ず以下を満たすアーティファクトに作り直す**:

- メイン画像を大きく表示し、other画像をサムネイルギャラリーとして横に並べる(実際の
  Amazon商品ページのレイアウトを模す)
- item_name(商品名)をそのまま見出しとして表示(文字数も添える)
- 価格を表示
- `bullet_point`(箇条書き)を実際に表示される順番で列挙する(省略しない)
- `product_description`(商品説明文)を整形して表示する(`<br>`はそのまま改行として
  描画し、生のHTMLタグを見せない)
- **登録予定の全SKU(親+子)を表形式で必ず明記する**(サイズ・SKU名。省略しない —
  2026-09-01 ユーザー指摘: SKUの列挙を毎回必須にする)

このアーティファクトを提示したうえで、Phase 5 Step 1 の実行確認に進む。データが変わる
たびに同じURLへ再publishして更新する(新規アーティファクトを都度作らない)。

---

## Phase 4: ペイロード構築・Amazon検証

### Step 1: DRY_RUN → VALIDATION_PREVIEW

```
POST /register/batch/preview
{
  "parent_sku": "...", "product_type": "SHIRT"等, "variation_theme": "SIZE"等,
  "marketplace_id": "A1VC38T7YXB528",
  "parent_item_name": "...", "parent_extra_attributes": {"brand": "制服のフジ"},
  "children": [{"sku": "...", "price": ..., "item_name": "...", "color": "...", "size": "..."}],
  "preview_mode": "DRY_RUN" または "VALIDATION_PREVIEW",
  "parent_material": "DR"等(material_dict.py参照), "parent_description": "...",
  "parent_bullet_points": [...], "parent_item_package_weight_kg": ..., ...
}
```

まず `preview_mode: "DRY_RUN"` でpayload構成を目視確認 → 次に `"VALIDATION_PREVIEW"` で
実際にAmazonへ検証問合せ(listingは作成されない)。`validation_ok: true` かつ全item
`issues: []` になるまで繰り返す。

### Step 2: product_typeごとの必須属性の動的発見

`SHIRT`以外(HANDBAG等)の product_type では、VALIDATION_PREVIEW のエラー(code 90220
"XXXは必須ですが、入力されていません")で初めて必須属性が判明することが多い。以下の手順で
正しい属性キー・構造を特定する:

1. `GET /register/definitions/{product_type}` で簡易フラット化されたメタを取得し、
   エラーの日本語ラベルに近い `label` を探す
2. 見つからない/ネストが深い場合は `definitions_client.get_definitions_product_type()`
   の `schema_json`(生JSON Schema)を直接読み、エラーラベルと一致する `title` を持つ
   プロパティを探す(似た名前の別属性に要注意 — 実例: `item_display_dimensions`
   「品目の表示寸法」と `item_dimensions`「商品本体サイズ」は別物で、後者が
   `height`/`length`/`width` 3点必須)
3. 見つけた構造を `child_template_attributes`(全childに共通なら)または
   `parent_extra_attributes`/`extra_attributes` に反映し、再度VALIDATION_PREVIEWへ

### Step 3: 2回失敗したらcgd Lv3相談(2026-09-01 ユーザー決定)

同一エラーへの対処を**2回試みても issues が解消しない**場合、そのまま闇雲にリトライを
重ねない。`/cgd` の **Lv3**(Codex+DeepSeekの技術×批評「2社×2視点」4レビュー・実装なし・
review専用)でエラー内容・現在のpayload・schema抜粋をレビューしてもらい、その結果を踏まえて
次の修正を行う。

---

## Phase 5: 最終確認・本登録

1. Phase 3で作った商品ページ・シミュレーションアーティファクト(全SKU一覧を含む)を
   提示済みであることを確認したうえで、**実行してよいか明示確認**を取る
   (AskUserQuestion。「はい、実行」以外は待機)
2. `POST /register/batch/submit` — `X-Write-PIN` ヘッダが必須。PIN値は
   `config.settings.write_pin` をサーバー側スクリプトから直接読んで送信し、**チャット・
   ログ・ファイルのいずれにも値を出力しない**(pin_auth.py の設計方針どおり)
3. 送信後は `GET /register/batch/{batch_id}` をポーリングし `state: "finalized"` を確認
4. さらに **SP-API `get_listing_item` で実ASIN・statusを直接確認**する(`included_data=
   ("summaries","issues")`)。**finalized直後は子SKUのsummariesが空でも異常とは限らない**
   (新規ASINのインデックス反映に数分かかることがある、FUJI-G0865案件で確認済みの既知挙動)。
   親ASINが確認できていれば「反映待ち」として報告し、不要な再送信はしない。

---

## Phase 6: 事後処理 — LCL登録 + 7S(コマースロボ)商品登録(一体化・別工程)

**新規登録と同時にはできない** — どちらもFBA本登録(Phase 5)で確定する
`InventoryItem.asin`/`fnsku` に依存しており、フル同期(`POST /sync?kind=full`、
1-5分・**30分クールダウンあり**)がそのSKUを拾うまで実行できない。
`asin`はASIN確定後すぐに、`fnsku`もこの会社のアカウントでは概ね同じタイミングで
InventoryItemに反映される(2026-09-01実測: G0942は本登録から約1時間後のフル同期で
両方とも反映済みだった)。同期直後に両サブステップをまとめて実行してよい。

「コマースロボ」「7S」「AirLogi」「CMR」はすべて同一の外部倉庫/WMSシステムの別名称
(brand: Commerce Robo, `042.EC統合管理/server/cmr_api_client.py` 経由)。

### Step A: LCL(自社発送)兄弟SKUの追加

`lcl_provision.py`/`lcl_sync.py`(2026-08-28確立)による自社発送(LCL)兄弟SKUの追加。
LCL SKUは既存ASINへの「オファーのみ」登録(`productType="PRODUCT"`,
`requirements="LISTING_OFFER_ONLY"`, `merchant_suggested_asin`)。

- SKU命名: **FBA役は既存の(接頭辞なしの)SKUのまま**、新規追加するLCL役だけに
  ブランド接頭辞の直後・商品番号の直前に `-LCL-` を挿入する
  (例: `FUJI-G1655-...` → `FUJI-LCL-G1655-...`)。**`FUJI-FBA-`のような接頭辞は
  使わない**(既存20件の実データと不一致になる。記憶ベースの命名案は必ず実データで
  裏取りする)
- 手順: `lcl_provision.preview_batch()`(VALIDATION_PREVIEW) → 人間確認 →
  `register_batch()`(型で「previewOK以外は実行しない」を強制) →
  `register_pairs_batch()`(`lcl_sync_pair`へ登録、在庫同期の対象になる。SKUDB.csv
  非依存で独立して動く)
- `update_skudb_master_batch()`(倉庫ピッキング用Excelマスタへの反映)は**任意**。
  既存行が古いSKU形式のエイリアスしか持たず解決できないことがある
  (2026-09-01 G0942実例: SKUDB.csvのエイリアスが`G0942S`のような旧形式で、
  新規`FUJI-G0942-S`と一致せず`unresolved_row_key`でfail-fast・書き込みゼロ件に
  なった)。失敗しても他のサブステップには影響しない。
  **`unresolved_row_key`が出ても、このCSV自体を手で編集して解消しようとしない**
  (2026-09-01 ユーザー指摘)。`SKUDB.csv`は別PCのExcelマクロ(`GoQピッキングリスト
  生成_r1.xlsm`のExportToCSV)が定期的に上書きするファイルであり、こちら側からの
  編集は次回エクスポートで消える。エイリアス不一致はマスタ側データの根本的な
  修正が別途必要な問題として報告するに留め、都度その場で解決しようとしない
  (2026-09-01時点でユーザーが別途仕組みを検討中・保留)
- 対象にするかどうかは商品ごとに都度判断してよい(全商品に必須ではない)

### Step B: 7S(コマースロボ)商品マスタ登録(2026-09-01追加)

FBA用に払い出されたFNSKUバーコードを、**自社発送(LCL)分の7S倉庫在庫管理にも
そのまま流用する**(自社発送品にはFNSKUが発行されないため。同一バリエーションで
複数SKU(FBA用・LCL用)があっても、7S倉庫→FBA納品時にバーコード貼り替えが不要になる
運用上の理由)。この7S側への商品登録ステップが従来のスキルから抜けていたため追加した。

- 経路: `022.Amazon在庫PWA/server/routers/cart_7s.py`
  - `POST /cart/7s/register-match` — 対象SKUのFNSKUがコマースロボに未登録か確認
    (二重登録防止。登録済みと判明した分はローカルミラーを自動補正)
  - `POST /cart/7s/register-via-api` — 未登録分を送信(非同期。`items[]`に
    `sku`/`item_code`/`product_name`/`spec1`/`spec2`を渡す。barcodeはサーバー側が
    SKU→FNSKUを自動解決するため指定不要)
  - `GET /cart/7s/register-via-api/status/{send_log_id}` — 成否をポーリング確認
- `item_code`(7S品番): `{G/U4桁}-{色2文字}-{サイズ}` 形式。色2文字はAmazon SKU
  で使っている既存の2文字コード(BK=黒、NV=紺 等)をそのまま流用する。
  **機械的に導出でき、都度ユーザーに確認する必要はない**(既存の`airlogi_stock`
  実データ(例: `G1866-NV-S`)で裏取り済み)。同一G番号で素材違いがある場合は
  `-{素材}-`を挿入する(例: `G1854-DR-BK-S` / `G1854-COT-BK-S`)
- `product_name`(短縮商品名): **管理しやすい非常に短い名前にする**(Amazon
  item_nameをそのまま使わない)。実例(`G1866-NV-M` → 「Tシャツ 練習艦隊 2024年
  (NV, M)」)を参考に、**ベースとなる短い名前を候補として提示しユーザーに選ばせる**
  (Amazon item_nameの候補選定と同じ方式・Phase 2 Step 0参照)。1回選べば、
  全サイズ分は機械的に `ベース名(色, サイズ)` の形で組み立てる(サイズごとに
  何度も聞かない)
- `spec1`/`spec2`(商品規格1/2): **コード上は汎用の任意項目で固定の意味づけは無い**
  (2026-09-01時点でCSV版は常に空欄送信。G1854実例では素材区別にのみ使用)。
  ユーザー運用の慣習として `spec1`=素材・カラー(例:「ドライ・ネイビー」)、
  `spec2`=サイズ、を踏襲する(2026-09-01 ユーザー指示)。裏付けがコードに無い旨は
  一度ユーザーに確認してから採用したので、以後は聞き直さず適用してよい

---

## 付録: 既知の落とし穴

- **クリック選択式アーティファクトはテンプレート(`template_image_picker.html`+
  `build_image_picker.py`)以外の経路で作らない(2026-09-11実例)**。その場で
  Python文字列(f-string/`.format()`)にHTML/CSS/JSを埋め込んで手書きした際、
  `.format()`用に`{{`/`}}`と二重化したブレースが、実際には`.replace()`方式に
  切り替えたため単体の`{`/`}`に戻し忘れて残った。結果、CSS宣言・JSの関数定義が
  すべて壊れ、生成自体は成功(HTTP 200・画像も表示される)するのに**クリックしても
  何も起きない**という、見た目だけでは気づきにくい壊れ方をした。ユーザー指摘で
  発覚。教訓: **同じUIパーツを2回以上作るなら、必ず独立した`.html`テンプレート
  ファイル+ビルドスクリプトに切り出す**(Pythonの三重引用符文字列に長いHTML/JS
  を埋め込むと、この種のエスケープ事故が起きやすい)。生成後は
  `grep -c '{{' out.html`が0であることを機械的に確認する
- **同種商品の前回スクリプトを`sed`で一括複製しない(2026-09-09実例)**。前の商品の
  build/page-simスクリプトを`sed 's/g2195/g2221/g'`のように小文字置換だけで複製すると、
  `Path(r"C:/ProductMaster/images/G2195")`のような**大文字G番号を含む行が置換されず
  残る**。この時はプレビュー用アーティファクトが前商品(G2195)の画像を表示してしまい、
  ユーザーから「サブの画像が違う商品になっている」と指摘された(実際にAmazonへ送信した
  画像URLは正しく新SKU名前空間だったため実害はなかったが、レビュー画面の信頼性を損ねた)。
  対策: 新商品ごとに**都度Writeで新規に書く**か、置換後に必ず
  `grep -n "旧G番号" <生成先.py>`で大文字小文字を問わず取りこぼしがないか確認する
- **カラーコード(color属性のASCII表記)はその場で略語を推測しない(2026-09-11実例)**。
  ネイビーを`NVY`、ブラックを`BLK`と、英単語から自己流で略語を作って2回連続で
  ユーザーに訂正された(正しくは`NV`/`BK`)。022 `inventory_items`の実SKU162件を
  調べたところ、この業務では`NV`/`GL`/`BK`/`WH`/`OD`(2文字)と`GLD`/`SLV`(3文字)、
  低視認性(ロービジ)版は`LowNV`/`LowGL`/`LowOD`という実績のある略語体系が既にあり、
  単語から桁数を推測すると高確率で外れる。対策: 色名からcolor属性コードを決める前に
  必ず `python C:/ClaudeCode/.claude/skills/amz-register/lookup_color_code.py <色名>`
  を実行する。`color_codes.json`に無い新色は、その場で作らずユーザーに確認して
  `color_codes.json`へ追記してから使う(`--list`で登録済み一覧を確認できる)
- **COLLECTIBLE_COINS(記念コイン)はSHIRTと属性体系が全く別**(2026-09-09追加)。
  `item_diameter`/`material`/`mint_mark`/`denomination_unit`/`series_title`/
  `model_year`/`graded_by`/`grade_rating`等が必要で、`target_gender`/`department`/
  `age_range_description`/`care_instructions`/`fit_type`/`style`はSHIRT用の既定値が
  batchスキーマに残ったまま送ると無関係な属性が混入するので、
  `parent_target_gender`等を明示的に空文字で上書きする。既存の兄弟コインSKUがあれば
  `GET /register/template/{sibling_sku}`で属性一式をテンプレートとして取得でき、
  そのまま`child_template_attributes`に流用できる(価格・画像・item_name等の
  インスタンス固有値だけ除いて使う)。**親レベルでも`recommended_browse_nodes`/
  `supplier_declared_dg_hz_regulation`/`country_of_origin`が必須**(childだけに
  入れると親でERROR 90220になった実例あり)なので`parent_template_attributes`にも
  同じ値を重複して入れる。カラー(例: ゴールド/シルバー)はvariant_keyのASCII制約により
  英語表記(GLD/SLV等)で登録されることになるため、レビューアーティファクトで
  必ずその旨をユーザーに明記する
- **FABRIC_APPLIQUE_PATCH(ワッペン)は`batteries_required`/`list_price`が親・子
  両方で必須になることが多い**(2026-09-11実例、G2179で発覚)。`skip_offer`と共に
  `parent_template_attributes`/`child_template_attributes`の両方へ最初から入れておくと
  VALIDATION_PREVIEWの往復を1回減らせる。`list_price`(メーカー希望小売価格・税抜)は
  明示指示が無ければ `round(税込価格 / 1.1)` を暫定値としてレビューアーティファクトに
  明記し、ユーザー確認を仰ぐ
- **`.claude/`配下の編集は`/g-ul`まで完了させないと、他セッションの`/g-dl`や
  自動ミラー同期で静かに巻き戻る**(2026-09-11実例)。本SKILL.mdはこの事故で
  2026-09-01版まで戻り、上記の複数の教訓が一度消えて再構築する羽目になった。
  編集がある程度まとまったら都度`/g-ul`を実行し、claude-shared + originへ
  実際に反映されたことを確認する(feedback_claude_dir_changes_must_be_gul_or_mirror_reverts
  も参照)
- **画像枠は7枚ではなく最大7枚(main1+other6)** — other_7は存在しない
- **`merchant_suggested_asin` を兄弟テンプレから流用しない** — 新規SKUに他人の実ASINが
  紐付く事故になる(実際に危険な状態を作りかけた実例あり)。新規登録では空文字のまま
- **レガシー `size` 属性書き込みはAmazonに無視される**(WARNING 90000900で自動strip
  される)。実際に効くのは `shirt_size`(SHIRTの場合)の方
- **`variation_theme` は事前にPTD(`GET /register/definitions/{pt}`)のenumで存在確認**
  してから使う(例: `"SIZE"` は有効、複合軸は`"COLOR_NAME/SIZE_NAME"`等厳密な表記)
- **画像アップロードのcurlはローカルscratchpad配下のファイルを指定する** —
  `C:/ProductMaster/images/...` を直接 `-F file=@...` で渡すと環境によって
  `CURLE_READ_ERROR`(exit 26)になることがあった。一度scratchpadへコピーしてから
  アップロードすると安定する
- **サイズ表を商品説明に載せる場合、ユーザーが明示的に不要と言った注記文は削除する**
  (「※参考値です」等を機械的に付けない — 2026-08-27の実例で指摘された)
