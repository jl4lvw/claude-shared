---
name: product-photo
description: 商品写真(Tシャツ・スポーツタオル・ワッペン)の仕上げから楽天の差し替えまでの標準手順。元写真の背景を白にする・正方形にする(Claude が作り、2回失敗したら Codex の gpt-6-astra に頼む)→ Tシャツの着用画像(最初から astra)→ トップ画像(白い背景の版・着用画像の版。共通の道具 023 tools/product_photo.py が座標と倍率を写真から測る)→ 一覧で確認 → 承認の画面(rakuten_photo)で楽天の写真を差し替え、本店・Yahoo! へ展開。新商品は写真まで作って /rakuten-new-item に渡す。「商品写真を作って」「写真を差し替えて」「着用イメージ」「着用画像」「トップ画像を作って」「背景を白に」「/product-photo」で使う。旧 /wear-image(Gemini を手で操作)は置き換え済み。
---

<!-- SKILL_VERSION: 2026-10-27_1500 -->

# product-photo — 商品写真の仕上げと差し替え

G1635(Tシャツ)・G2251/G1413(スポーツタオル)・G2247(ワッペン)の手作業を 1 本にまとめた手順(2026-10-27 運用者決定)。
**承認は承認の画面(PWA)でだけ取る。チャットで承認を求めない。** 楽天・モールへの書き込みは手順 6 の申請からだけ。

| 対象 | 作る写真(並び) |
|---|---|
| Tシャツ | トップ画像(着用画像の版)→ 白い背景のトップ画像 → 前の白い背景 → 胸・袖の寄り → 着用画像(後ろ・前)→ 型の共用画像(サイズ表)・楽天だけの写真 → 末尾に白抜き(G1635 の並び) |
| スポーツタオル | トップ画像 → 帯とマーク・帯の寄り・手でめくる・斜め全体・折った全体・生地の拡大(元写真しだい)→ 末尾に白抜き |
| ワッペン | トップ画像 → 寄り・横・手で持った・裏・斜め(元写真しだい)→ 末尾に白抜き |

並びは手本(Tシャツ G1591・G1699 / タオル G2251 / ワッペン G2247)に合わせ、最初に運用者へ確かめる。

## 0. 最初にまとめて聞く(途中で小出しに止めない)

`AskUserQuestion` と本文の表で、次を一度に聞く。後から聞くことが出たら、この一覧へ追記する。

| 項目 | 例 |
|---|---|
| G番号と商品名 | G1635(IPD2021 護衛艦かが記念Tシャツ)。G番号には必ず商品名を括弧で付ける |
| 元写真 | データ共有(`/img`)の番号・フォルダ |
| 作るもの | 写真一式 / 着用画像だけ / トップ画像だけ / 差し替えまで |
| トップ画像の文言 | Tシャツ: 題名 2 行・サイズ(S・M・L・LL・3L)・生地(ドライ生地)・着用画像の版の「護衛艦○○ / 記念の名前」・「S－3L」 / タオル: 艦名・「サイズ：約 110×35cm」 / ワッペン: 艦名・「サイズ：直径 10cm」。**文言は運用者が選ぶ**(案を 2〜3 出す) |
| 差し替えの並び | 今の楽天の写真のどれを残し・外すか(新商品なら不要) |

## 1. 材料を集める

- 作業フォルダ: `C:/ClaudeCode/900.ClaudeCode/a_browser_data/rakuten_new_item/<G番号>_photo/`(`source/`・`proc/`・`final/`・`check/`・`run_astra/`)
- 元写真はデータ共有から `get <番号> --variant orig` で原寸を取り、`source/` に置く(一覧用の小さい画像は使わない)
- 既存商品は今の写真を `cd C:/ClaudeCode/023.商品マスタDB && python tools/photo_inspect.py <G番号>` で読んで並べ、元の 1 枚目(文字・マークの配置の手本)も `source/` に置く
- 設定ファイル `photo.json` を作業フォルダに書く(下の「photo.json」)

## 2. 写真の仕上げ(Claude が作る。2 回失敗したら astra)

道具は `C:/ClaudeCode/023.商品マスタDB/tools/product_photo.py`(試験 `tests/test_product_photo.py`)。

```bash
cd C:/ClaudeCode/023.商品マスタDB
python tools/product_photo.py cutout <元写真> <作業フォルダ>/final/z_cutout.jpg   # 白い背景の白抜き(.png なら透明)
python tools/product_photo.py square <元写真> <作業フォルダ>/final/d_chest.jpg    # 正方形・商品を中心・1200 角(回転させない)
python tools/product_photo.py check <作業フォルダ>/final --out <作業フォルダ>/check [--navy]
```

- `cutout` は白い背景(撮影台・白い布)か、明るい無地の背景(縁の色に近い所を消す)の写真向け。**紺の布の背景で撮った写真**は先に `tools/navy_backdrop.py` の `to_white()` で白にする
- `check` は 1200 角・2MB 以内かを見て、`red_*.jpg`(消した背景を赤)・`edges_*.jpg`(四隅と上下の中央の拡大)・`sheet.jpg`(一覧)を作る。
  **赤の画像と縁の拡大を Read で必ず見る**。紺の布の背景を消した明るい商品は `--navy` で縁に残った紺の割合も見る(0 に近いこと)
- 写真の決まり(運用者決定・省略しない):
  - 背景はすべて消して白にする。商品は欠けさせない。縁の外に紺・灰色の細い帯を残さない
  - 切り抜きは画素ごとに背景を判定する。四角形で切るのは色で分けられないときだけ(使ったら報告に書く)。トップ画像と末尾の白抜きは必ず画素ごと
  - **正方形の元写真を回転させない**(四隅に白い三角が出る)。逆さ・横向きだけ 90 度単位で直す
  - 寄りの写真が切れるなら、切らずに白い余白で正方形にする。商品は写真の中心に置く
  - 明るさ・色味を全部の写真でそろえる。商品の色は元写真のまま
  - 形・色・明るさを変える処理だけにし、画像を作り出す AI で商品写真を描き直さない(プリントの文字が変わる)
- **失敗の数え方**: 運用者の指摘、または Claude が拡大して見つけた問題で作り直したら 1 回。**同じ写真で 2 回作り直しても直らなければ astra に頼む**(下の「astra に頼む」・雛形 `templates/task_photos.md`)。粘らない(G2250 で何十回も失敗した)

## 3. 着用画像(Tシャツ・最初から astra)

Claude には作れないので、最初から astra に頼む。雛形 `templates/task_wear.md` を埋める。

- **胸のマークは着た人の左胸 = 前から見て右**(luna は反対の胸に付けた)
- **袖のプリントは袖の外側の面**: 真後ろ・真正面からは袖の丸みに沿って回り込み、端の半分ほどが斜めに細く見えるだけ
- **袖のプリントは実物の比率**: 袖口寄りで、袖の長さの半分ほど。袖いっぱいに広げない
- 顔は写さない(あごから下)・短髪の男性・直立・明るい無地の背景・茶色系のチノパン
- 手本の姿勢 `G1635_photo/ref_wear/pose_back.jpg`・書き方 `G1635_photo/ref_wear/example_prompt_g1591.md` を `ref_wear/` に写して渡す
- 出来たら、元写真のプリントと見比べる(文字・胸の位置・袖の大きさと見え方)。おかしければ `templates/fix.md` で直しを頼む(1 回 4〜5 分)

## 4. トップ画像(Claude が道具で作る)

```bash
cd C:/ClaudeCode/023.商品マスタDB
python tools/product_photo.py top-white --config <作業フォルダ>/photo.json   # 白い背景の版(Tシャツ・タオル・ワッペン)
python tools/product_photo.py top-wear  --config <作業フォルダ>/photo.json   # 着用画像の版(Tシャツ)
```

- **Tシャツのトップ画像(1 枚目)は着用画像の版**(手本 楽天 G1591・G1699 の 1 枚目)。白い背景の版は 2 枚目に置く
- `top-wear` は背中の着用画像から Tシャツとプリントの位置を測り、**プリントが欠けず・左下の前の着用画像に重ならず・シャツの左端が左の文字の列より右**に来る一番大きい倍率を選ぶ(上限 1.35)。
  出力の `scale`・`print_on_canvas`・`warnings` を見て、気に入らなければ photo.json に `"scale"`・`"offset"` を書いて上書きする。
  上書きした値でもプリントが欠ける・艦名や前の着用画像に重なるときは、画像を出さずに止まる(理由が出る)
- プリントは近い文字・線をまとめて測り、一番大きいまとまり(背中の図柄)を基準に置く。袖のプリントなど離れたまとまりは、画面から欠けないかだけを見る(欠けると `warnings` に出る)
- 前の着用画像は輪郭で白抜きして左下に重ねる(四角いまま貼ると後ろの人の腕を隠す)。胸のマークと袖のある側(前から見て右)に寄せて切り出す
- 文言の誤字は Read で拡大して確かめる

## 5. 一覧を送って確認

- `check` を `final/` 全部に流し、`sheet.jpg` を `SendUserFile` で送る(並び順どおり)。赤の画像・縁の拡大は Claude が見る
- 運用者の指摘は手順 2〜4 へ戻す(失敗の数に入れる)
- **ここまでで写真の作業は終わり**(G1635 の決定)。新商品はここで `/rakuten-new-item` に渡す(spec の `images` に `final/` の写真を並び順で書く)

## 6. 楽天の写真を差し替える(既存商品)

1. `<作業フォルダ>/replace/spec.json` を書く(形は `tools/rakuten_photo_replace.py` の冒頭)。今の写真はすべて `keep` か `remove` に書く。1 枚目は新しいトップ画像・白抜きは末尾。楽天だけの写真(`/tj/`・`/ranking/`)は `keep`
2. 計画を作る(読み取りだけ):
   ```bash
   cd C:/ClaudeCode/023.商品マスタDB && python tools/rakuten_photo_replace.py build --spec <作業フォルダ>/replace/spec.json
   ```
3. Codex(medium)に、送る内容(並び・alt・外す写真・置き場のファイル名 20 バイト以内)の漏れを確かめさせる
4. 申請を積む(承認は見張り役が知らせ、運用者が承認の画面で行う。写真があるので電話では承認できない):
   ```bash
   cd C:/ClaudeCode/900.ClaudeCode && python -m rakuten_photo.cli enqueue <plan.json> --purpose "<G番号>(<商品名>)の楽天の写真を新しく作った写真に差し替え、本店・Yahoo! へ展開するため"
   ```
5. 結果を裏で待つ(運用者に「押したら知らせて」と頼まない)。実行した(`実行終了 status=…`)・実行せずに閉じた(`終了: 理由` = 却下・差し戻し・期限切れ)・`結果不明` のどれでも抜ける:
   ```bash
   until grep -qE "rakuten_photo id=<N> (実行終了|終了:|結果不明)" C:/ClaudeCode/900.ClaudeCode/a_browser_data/watch.log; do sleep 15; done; grep -E "rakuten_photo id=<N> " C:/ClaudeCode/900.ClaudeCode/a_browser_data/watch.log | tail -3
   ```
   `status=done` 以外(`needs_human`・`終了:`・`結果不明`)は、理由を読んで運用者に報告する。差し戻しは理由に沿って写真や spec を直し、申請し直す
6. 3 モールを読み直して一覧にする(`cd C:/ClaudeCode/023.商品マスタDB && python tools/photo_inspect.py <G番号>`)。並び・枚数・楽天だけの写真が本店・Yahoo! に写っていないことを確かめて報告する

## astra に頼む

```bash
cd <作業フォルダ>/run_astra
codex exec -m gpt-6-astra -c model_reasoning_effort=medium --sandbox workspace-write --skip-git-repo-check -C . -o codex_work/last.txt - < codex_work/TASK.md > codex_work/codex_out.txt 2>&1
```

- 1 回 20 分前後かかる(G1635 は写真一式と着用画像で 21 分 48 秒、直しは 4〜5 分)。**バックグラウンドで起動**し、終わりの通知を待つ
- 元写真・手本は `run_astra/` の外(`../source/`・`../ref_wear/`)に置き、依頼書で「読むだけ」と書く
- 依頼書に「作業フォルダの中では Python・PowerShell を確認なしで使ってよい」と書く(書かないと確認を求めて止まる)
- 着用画像の出来上がりの画像は `C:/Users/user/.codex/generated_images/<セッション>/` にもできる。`proc/` にあるものを使う
- 頼むなら astra(luna は速いが、胸のマークを反対に付けるなどの誤りが出る)

## photo.json

パスは photo.json のフォルダからの相対で書ける。`out_dir` に出す。`out` の省略時は Tシャツ: 着用画像の版 `a_main.jpg`・白い背景の版 `a_main_white.jpg` / タオル・ワッペン: `a_main.jpg`(2 つが同じ名前なら止まる)。

```json
{
  "g": "G1635",
  "kind": "tshirt",
  "out_dir": "final",
  "top_white": {
    "out": "h_main_white.jpg",
    "cutout": "final/z_cutout.jpg",
    "title": ["記念Tシャツ(IPD2021)", "護衛艦かが"],
    "sizes": ["S", "M", "L", "LL", "3L"],
    "fabric": "ドライ生地",
    "chest": "final/d_chest.jpg",
    "chest_label": "胸元プリント"
  },
  "top_wear": {
    "out": "a_main.jpg",
    "back": "final/f_wear_back.jpg",
    "front": "final/g_wear_front.jpg",
    "size_label": "S－3L",
    "name": ["護衛艦かが", "IPD2021"]
  }
}
```

| キー | 意味 |
|---|---|
| `kind` | `tshirt` / `towel` / `wappen` |
| `top_white.cutout` | 商品の白抜き(白い背景の JPEG か透明の PNG) |
| `top_white.title` | Tシャツ: 行の配列 / タオル・ワッペン: 艦名の文字列 |
| `top_white.label` | タオル・ワッペンの見出し(省略時 タオル `["綿 100%", "スポーツタオル"]`・ワッペン `["日本製", "ワッペン"]`) |
| `top_white.size_text` | タオル・ワッペンの右下(`サイズ：約 110×35cm`・`サイズ：直径 10cm`) |
| `top_white.chest` / `chest_circle` | Tシャツの右下の丸に使う胸の寄り。丸の位置は測る(`[cx, cy, r]` で上書き) |
| `top_wear.dry` / `dry_sub` | 青の「DRY」「吸汗速乾」(既定 true。ドライ生地でなければ false) |
| `top_wear.name` | 右下の白い明朝。`["護衛艦かが", "IPD2021"]` か `[["護衛艦かが", 92], ["IPD2021", 66]]` |
| `top_wear.scale` / `offset` / `max_scale` / `inset_side` | 倍率・位置の上書き / 倍率の上限(既定 1.35)/ 前の着用画像を寄せる側(既定 `right`) |

共通の素材(海上自衛隊のマーク)は `023.商品マスタDB/assets/product_photo/jmsdf_mark.png`。

## 関係するスキル・道具

- `/rakuten-new-item`(新商品の楽天登録。写真はこのスキルで作って渡す)・`/register-product`(本店・Yahoo!・092)・`/mall-product-update`(名前・説明の変更。写真はこのスキル)
- 差し替え: `023 tools/rakuten_photo_replace.py`・`900.ClaudeCode/rakuten_photo/cli.py`(見張り役の `rakuten_photo`)
- 紺の布の背景: `023 tools/navy_backdrop.py`
- 実績: **G1635**(IPD2021 護衛艦かが記念Tシャツ)2026-10-27 差し替え(申請 405・408。楽天 12 枚・本店 9 枚・Yahoo! 9 枚)。道具の倍率は手作業の 1.22 に対し 1.224
- 旧 `/wear-image`(Gemini を手で操作)は 2026-10-27 に置き換え、`.claude/_retired_skills/wear-image/` へ退避した
