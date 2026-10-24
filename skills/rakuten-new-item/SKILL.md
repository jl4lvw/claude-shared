---
name: rakuten-new-item
description: 楽天市場に新しい商品(G番号)を API で作る標準手順。似た既存商品(型)を写し、商品名・説明文(固有の部分)・写真・価格・色とサイズ・属性だけを入れ替えて、倉庫(非表示)・在庫0で作る。入力は最初にまとめて聞き、商品名と説明文は Claude が型の書き方に合わせて下書きし、承認は承認の画面(PWA)でだけ取る。画像フォルダは使用率90%を超えたら使わず「Tシャツ2」のような新しいフォルダを作る。公開は人が RMS で見た目を確かめてから行い、Eストア・Yahoo!・GoQ は別に /register-product で行う。「楽天に新商品を登録」「楽天に新しい商品を作って」「G2246を楽天に登録」「/rakuten-new-item」で使う。
---

<!-- SKILL_VERSION: 2026-10-24_123000 -->

# rakuten-new-item — 楽天に新しい商品を倉庫で作る

設計書: `C:\ClaudeCode\023.商品マスタDB\docs\rakuten_new_item_design.md`(決定事項・仕様書で確かめた事実・理由はここ)。

| 役割 | ファイル |
|---|---|
| 下見・計画・検査・実行・照合 | `023.商品マスタDB/tools/rakuten_new_item.py`(純粋な処理は `rakuten_new_item_build.py`) |
| 申請を積む・承認された申請だけを実行 | `900.ClaudeCode/rakuten_new_item/cli.py`(見張り役の対象 `rakuten_new_item`) |
| 作業フォルダ | `900.ClaudeCode/a_browser_data/rakuten_new_item/<品番>/`(spec.json・plan.json・state.json・sheet.png。git には入らない) |

**Claude がやること**: 最初にまとめて聞く → spec を書く → build → 内容を本文で見せる → Codex で送る内容を確認 → 申請を積む →
承認の結果を自分で見張る → 結果を報告。**承認はチャットやアーティファクトで求めない**(承認の画面だけ)。

## 絶対に守ること

1. **倉庫(非表示)・在庫0で作る。** 公開・在庫の設定はこのスキルではしない
2. **既にある商品は上書きしない。** 楽天の作成 API は「同じ番号があれば丸ごと置き換え」になる。ツールが作る直前に「無い」ことを確かめ、
   応答が 201 以外なら止める。**終了コード 5(既存を上書きした)が出たら、すぐ運用者に知らせる**(再実行しない)
3. 計画(plan.json)を承認の後に書き換えない(ハッシュが合わなくなり実行されない)。直すときは build し直して申請し直す
4. スキルの中から別のスキルを呼ばない。`/register-product`・`/line-notify` は運用者に頼まれてから、Claude が次の作業として呼ぶ
5. 試験のためだけの商品を作らない。楽天の商品を消さない(消すのは運用者が RMS で)

## Phase 0: 下見と、最初にまとめて聞く

1. 型の候補を Claude が 2〜3 つ選ぶ(同じ種類の最近の商品。例: 手ぬぐい → g2245、Tシャツ → g2242)。下見(読み取りだけ):
   ```bash
   cd C:/ClaudeCode/023.商品マスタDB
   python tools/rakuten_new_item.py preflight G2246 --template g2245 --template g2244 --images 15
   ```
   見るところ: `exists`(true なら新規の対象ではない)・ジャンルの必須属性(`genre.mandatory`)・型の属性と値・SKU の付け方
   (`variant_ids`・`sku_example`)・画像の置き場の判定(`folder_decision`)・型の固有の説明文(`own_description`)・
   `tagline`/`customization_options`/`sales_own`(あれば spec で指定が要る)
2. **AskUserQuestion と本文の表で、次を 1 回でまとめて聞く**(途中で聞き足したら、この表に足す):

   | 項目 | 聞き方 |
   |---|---|
   | 品番 | 指示どおり(G+4桁) |
   | 型 | 候補 2〜3(ジャンル・送料・属性が同じもの) |
   | 楽天の商品名 | 型と兄弟商品の付け方で Claude が 2〜3 案(バイト数つき。255 バイトまで。楽天は Shift_JIS 相当で数える) |
   | 価格(税込) | 楽天の価格。部隊識別帽の一般用など例外に注意(記憶 `project_mall_price_markup_10pct`) |
   | 色・サイズ | 無し/色/サイズ/色×サイズ。SKU 番号は `G2242-BK-3L` の形(色の略語 BK/NV/OD…)、SKU 管理番号は型の付け方(単品 `g2245`・色×サイズ `g2242black3l`) |
   | 写真 | データ共有(`/img`)の番号かフォルダ。並び順(1 枚目がメイン)。型の共用画像(サイズ表など)を使い回すか。切り抜くと端の商品が欠けそうな写真は「白い余白」にするか |
   | 商品の事実 | 何の商品か・由来・素材・大きさ・生産国など(説明文と属性に使う。推測で書かない) |
   | 属性 | 必須属性のうち型と違う値(シリーズ名・カラー・素材・原産国など) |
   | 店内カテゴリ | 付けるなら ID(099 の対応表 `099.ECサイトカテゴリー統一プロジェクト` から候補)。分からなければ付けない(人が RMS で) |

## Phase 1: spec を書いて build する

`900.ClaudeCode/a_browser_data/rakuten_new_item/<品番>/spec.json` に書く(パスは Windows 形式 `C:/...`):

```json
{
 "item_number": "G2246", "template": "g2245",
 "title": "自衛隊 てぬぐい 【 手ぬぐい ( … )】 …",
 "description_html": "陸上自衛隊「…」の手ぬぐいです。<br>\n<br>\n…<br>\n<br>\n大きさ：約86cm×34cm<br>\n素材：綿100%<br>\n生産国：日本<br>\n<br>\n-------------------------<br>\n<img src=\"https://www.rakuten.ne.jp/gold/seifukunofuji/banner/nekopos-ra.jpg\">\n<br>",
 "images": ["C:/…/1.jpg", {"src": "C:/…/2.jpg", "rotate": 180}, {"src": "C:/…/3.jpg", "fit": "pad"}],
 "reuse_images": ["/06978817/12092953/g2244_09.jpg"],
 "variants": [{"id": "g2246", "sku": "G2246", "price": 726}],
 "attributes": {"シリーズ名": "…", "原産国／製造国": "日本", "代表カラー": "-", "素材（生地・毛糸）": "綿・コットン",
                "本体横幅": {"values": ["86"], "unit": "cm"}, "本体縦幅": {"values": ["34"], "unit": "cm"}},
 "category_ids": []
}
```

- **説明文は商品固有の部分だけを書く。** 型の固有の部分(`own_description`)の構成をまねる。末尾の区切り線とネコポスの画像のように
  型にある決まった部分も、変えないならそのまま入れる。TJ の目印の部分(関連商品・カテゴリの道筋など)はツールが型から写す
  (外部の仕組みが翌朝までに新しい商品用に入れ直す)。スマホ用を変えるときだけ `description_sp_html`
- 色・サイズがあるとき: `"selectors": [{"key": "Key1", "displayName": "カラー", "values": ["ブラック", "ネイビー"]}, …]` と、
  各 SKU の `"selectors": {"Key1": "ブラック", "Key0": "M"}`・SKU ごとに違う属性 `"attributes": {"カラー": "ブラック", "代表カラー": "ブラック", "サイズ（S/M/L）": "M"}`
- **写真は build が楽天用に加工する**(設計書 9 節。元の写真は変えず、作業フォルダの `processed/` に置く):
  1200×1200 の正方形の JPEG・2,000,000 バイト以内・向きの情報で回転・付帯情報(位置情報など)を消す。
  - 正方形でない写真は**中央で切り抜く**(既定)。写真ごとに `"anchor": "top"|"bottom"|"left"|"right"` で寄せる位置、
    `"fit": "pad"` で切らずに白い余白を足す。`"rotate": 90|180|270`(時計回り)は向きの情報の後に当たる
  - 1200 より小さい写真は拡大する(承認の画面に「拡大」と出る)。すでに 1200×1200・2MB 以内の JPEG/PNG は作り直さず、付帯情報だけを消す
  - 写真はデータ共有から `PS get <番号> --variant orig`(元の大きさ。HEIC も読める)か `full` で取り出し、
    `900.ClaudeCode/a_browser_data/rakuten_new_item/<品番>/source/` に置く(一覧用の小さい画像は使わない)
- 型にキャッチコピー・JAN・商品オプションがあるときは `tagline` / 各 SKU の `jan` / `keep_customization_options` を指定(無いと止まる)
- 型の画像が古い置き場(rakuten01 の直下)にあるときは `"image_folder": "/06978817/<フォルダ>"` で置き場の系列を指定する

```bash
cd C:/ClaudeCode/023.商品マスタDB
python tools/rakuten_new_item.py build --spec C:/ClaudeCode/900.ClaudeCode/a_browser_data/rakuten_new_item/G2246/spec.json
```

終了コード 2(入力の誤り)なら `detail` を読んで spec を直す。通ったら出力の `summary` を**返答の本文にそのまま貼り**、
`sheet.png`(写真の一覧・**加工後の写真**)を **Read で必ず目で見る**(省略しない)。確かめること:
- 逆さ・横向きの写真が無いか(スマホの写真は、上下が逆のまま向きの情報が「正常」で届くことがあり、自動では直らない)→ `rotate` を付けて build し直す
- 切り抜きで商品が欠けていないか(見出しの「切抜」。summary に「※ … % を落としました」が出たものは特に)→ `anchor` か `"fit": "pad"` にして build し直す
- 拡大した写真(見出しの「拡大」)がぼやけすぎていないか → 元の大きい写真があれば差し替える
- summary の写真ごとの行(「元 4032×3024 → 中央で正方形に切り抜き → …」)も運用者向けの本文に含める

### 画像の置き場(2026-10-24 運用者決定)

- 型のフォルダ(例: Tシャツ)と番号付き(Tシャツ2・Tシャツ3…)のうち、いちばん大きい番号を候補にする
- **使用率が 90% を超えた(2,000 枚中 1,800 枚超え)フォルダは使わない。** 足すと 2,000 枚を超えるときも使わない
- 使えなければ次の番号のフォルダ(Tシャツ2 など)を作る。作ることは承認の画面に出る
- 承認の後に条件に当たるようになったら、実行は止まる(終了コード 4)。build し直して申請し直す

## Phase 2: 確認 → 申請 → 結果を見張る

1. **Codex(medium)に送る内容の漏れを確認してもらう**(書き込み API の決まり)。`plan.json` の `body`(説明文の TJ 部分は長いので
   固有部分だけでよい)と spec を渡し、「型から写した項目に新しい商品に合わないものが無いか・必須項目の漏れ」を聞く。
   指摘で直したら build し直す
2. 申請を積む(承認は見張り役が LINE WORKS で知らせ、運用者が承認の画面で行う。写真があるので電話では承認できない):
   ```bash
   cd C:/ClaudeCode/900.ClaudeCode
   python -m rakuten_new_item.cli enqueue --plan C:/ClaudeCode/900.ClaudeCode/a_browser_data/rakuten_new_item/G2246/plan.json --purpose "新商品を楽天に倉庫で作り、見た目を確かめてから公開するため"
   ```
   `purpose` は 6〜120 字。人名・住所・電話番号・お客様名は書かない(電話の音声案内に使われる)
3. 運用者には「LINE WORKS の知らせのリンクから承認の画面で承認してください」とだけ伝え、**結果は自分で見張る**(裏で待つ):
   ```bash
   until grep -qE "rakuten_new_item id=<N> 実行終了" C:/ClaudeCode/900.ClaudeCode/a_browser_data/watch.log; do sleep 15; done; grep -E "rakuten_new_item id=<N>" C:/ClaudeCode/900.ClaudeCode/a_browser_data/watch.log | tail -3
   ```
   終わったら `python -m rakuten_new_item.cli show <N>` で結果(`result`)を読む

| 結果 | 意味 | 次の一手 |
|---|---|---|
| done | 楽天に倉庫・在庫0で作った | Phase 3 |
| rejected(終了コード 2/3/4) | 書く前に止めた(入力・既にある・置き場が変わった) | `detail` を読み、直して build → 申請し直す |
| failed(終了コード 5) | **既存の商品を上書きした** | すぐ運用者に知らせる。RMS で確かめるまで何もしない |
| failed(その他) | 途中で失敗(写真・在庫・カテゴリ・照合) | `state.json` で済んだ所を確かめる。同じ申請は使えないので、計画は同じまま申請し直す(済んだ段は飛ばされる) |

## Phase 3: 報告(店の人が読める言葉で)

- 楽天に**倉庫(非表示)・在庫0**で作ったこと、品番・商品名・価格・SKU・写真の置き場(新しいフォルダを作ったならその名前)
- **人がやること**: RMS で見た目を確かめて公開する・在庫を入れる・(付けていなければ)店内カテゴリを付ける
- 続けて Eストア・Yahoo!・GoQ に広げるときは `/register-product`(楽天登録済みが前提)を別に頼んでもらう
- 照合の注意(`warnings`。例: 説明文が楽天側で整形された)があれば書く
- **最初の数点は翌日に TJ の部分が新しい商品用に入れ直されたかを確かめる**(`python tools/rakuten_new_item.py verify --plan …` と
  RMS の画面)。入れ直されていなければ運用者に相談する

## してはいけないこと

- `tools/rakuten_new_item.py apply` を Claude が直接実行しない(承認を確かめるのは 900 側の入口。直接呼ぶと承認なしで書ける)
- 途中で止まった計画を build し直さない(済んだ記録 `state.json` と合わなくなる)。直すなら運用者に相談
- 同じ品番を RMS でも作らない(作成 API には「作成だけ」が無く、確かめてから送るまでの約 1 秒の隙間が残る)

## 使い始める前の確認(初回だけ)

- 見張り役(タスク `A_BrowserWatch`)が対象 `rakuten_new_item` を読み込んでいること(対象を足した後に見張り役を再起動したか)。
  再起動していないと、申請しても承認を求めない

## 実装の事実(2026-10-24)

- 作成 API: `PUT /es/2.0/items/manage-numbers/{管理番号}`(新規 201・既存の上書き 204・送らない項目は消える)。自動再送しない
- 在庫は作成の後に InventoryAPI で ABSOLUTE 0 → 読み戻し。店内カテゴリは `PUT /es/2.0/categories/item-mappings/manage-numbers/{管理番号}`
- 必須属性は `GET /es/2.0/navigation/genres/{ジャンルID}/attributes` の MANDATORY。足りなければ build が止める
- 写真の加工: `023.商品マスタDB/tools/rakuten_image_prep.py`(試験 `tests/test_rakuten_image_prep.py` 38 件)
- 試験: `023.商品マスタDB/tests/test_rakuten_new_item.py`(36 件)・`900.ClaudeCode/rakuten_new_item/test_cli.py`(9 件)
- 応答が不明なときは、楽天の作成時刻と中身で自分が作ったかを確かめる。再開時は在庫・カテゴリの前に毎回同じ商品かを確かめる
  (Codex レビューの反映。詳細は設計書 8 節)
