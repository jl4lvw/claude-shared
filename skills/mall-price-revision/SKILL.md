---
name: mall-price-revision
description: 自社ネット(Eストア・楽天・Yahoo!ショッピング)の価格改定の標準手順。価格改定のお知らせPDFを受け取り、3モールの現在値を実測して計画を作り、アーティファクトで承認を得て、Eストア→楽天・Yahoo!の順に1商品ずつ更新し、読み戻して確認し、「制服のフジ情報共有」へ報告(画像つき)するまで。2026-10-20(11/1改定・17品番)に確立。「価格改定」「モールの価格を変える」「価格改定のお知らせ」「Eストア 楽天 Yahoo 価格」で使う。卸売先(JRサービスネット広島)への価格改定依頼書は /price-revision-jr(別物)。
---
<!-- SKILL_VERSION: 2026-10-20_180000 -->

# mall-price-revision — 自社ネット3モールの価格改定

作業場所: `C:\ClaudeCode\023.商品マスタDB`(道具は `tools/`、計画は `data/price_plan_YYYYMM/`)。
**2026-10-20 に 11/1 改定(巾着4・Tシャツ・フェイスタオル・キャップ4・ジャケット・ウィンドブレーカー3ほか17品番)を一通り流して確立。** 以後、毎月同じ形で行う。

## 全体の流れ

```
1 PDF(価格改定のお知らせ)を読む → 品番・新価格の表を作る
2 計画を作る: mall_price_plan.py(読み取り専用。3モールの現在値を実測)
3 食い違いを洗い出して運用者に聞く(AskUserQuestion)→ アーティファクトで承認
4 書込前の最終確認(下の「書込前チェック」)
5 Eストア → 楽天・Yahoo! の順に、1商品ずつ更新(読み戻しつき)→ Yahoo! 店頭反映
6 023 の estore_price を揃える → 3モールを読み直して全件一致を確認
7 /line-notify で「制服のフジ情報共有」へ報告(承認トーク経由)
8 記録(メモリ・README)と、公開した画像の片づけ
```

## 守ること

- **価格の基準 = Eストア登録値(税抜入力)。楽天・Yahoo! = その ×1.1(円未満四捨五入)。** PDF の「新価格」は Eストア登録値(旧価格が 023 の `estore_price` と一致することで確かめる。2026-10-20 確認)。
  特価・均一価格の商品は例外のことがある → 食い違えば運用者に聞く
- **お客様に見える価格は税込で扱う。** 報告・資料では税抜と混ぜない([[feedback_docs_unify_tax_included_no_mixed_tax]])
- **PDF にない品番を勝手に足さない。** 品番のない行(例: 「キャップ」)は、JAN で在庫管理PWAの `master.json`(`inventory-replenishment-pwa/master.json`)を引き、023 の候補を出して**運用者に選んでもらう**。複数選択になることがある(10/20 は G0316・G0060・G0100・G0061 の4つ)
- **023 の `products.price` は根拠にしない**(楽天のスナップショットで、セール価格の残骸を含む)。現在値は必ず 3モールを実測する → `mall_price_plan.py` がやる
- **書込は運用者の承認の後だけ。** アーティファクトで一覧を見せ、「実行して」「OK」をもらう
- **在庫に触らない。** 楽天は PATCH で standardPrice/referencePrice だけ(023 の push は在庫を0にするので使わない)。Yahoo! は updateItems で price だけ(editItem は未送信項目を消すので使わない)
- 外部へ反映する操作は、**コマンドの成否ではなく反映後の状態**を確かめて終える(最後に必ず 3モールを読み直す)

## 1. PDF を読む

PDF はたいてい `\\192.168.1.50\共有スペース\寺下\999.一時ファイル\` にある。Read ツールは poppler が無く PDF を読めないので、**PyMuPDF で本文を抽出**する(`python -I -c "import fitz; ..."`・作業コピーはスクラッチ領域に取る)。
行の並びは「JAN・品番・商品名・新価格・旧価格」。**転記した表を運用者に見せ、旧価格が 023 の現在値と合うか**を後で計画で確かめる。

PDF のクセ(10/20):
- 品番のない行がある(キャップ・フェイスタオル)。JAN から商品を探す。フェイスタオルは JAN→「水兵フェイスタオル」→ G1280
- **品番が違うのに JAN が同じ行がある**(フライトジャケット3種)。**正しい**(価格が同じ商品は JAN を共通にしている。運用者に確認済み)。転記ミスと疑わない

## 2. 計画を作る(読み取り専用)

```bash
cd C:\ClaudeCode\023.商品マスタDB
python tools/mall_price_plan.py --out data/price_plan_YYYYMM --set G0675=880 G1895=3300 ...   # 品番=新Eストア価格
```

- 3モールの現在値を読み、`plan.json`(現在値・新価格)と、Eストア選択肢つき商品用の `estore_<旧>_<新>.json` を作る。一覧に「Eストア済/楽天済/Yahoo済」「Eストア未登録」などの状態が出る
- **plan.json は改定の「前」の現在値を持つ。** 更新後に作り直すと旧価格が失われる(報告画像の「旧→新」に使う)。**更新が済むまで上書きしない**
- 楽天のバリエーション商品は variant ごとの価格を持つ。不揃いなら一覧に出る

### 食い違いの典型(10/20 に出たもの)と扱い

| 食い違い | 10/20 の決定 |
|---|---|
| 楽天だけ 847円より低い(特価の残り: 677/762/726) | 通常の x1.1 に**そろえる** |
| 改定済みのはずなのに楽天・Yahoo! がずれている(G0884 が 19,150 で他3品は 19,360) | x1.1 にそろえる |
| Eストアだけ改定済みで楽天が旧価格(G0192・G0434) | 楽天だけ直す |
| 3モールとも改定済み(G1890・G1956・G1996) | **触らない**。報告に「変更なし」と書く |
| Eストア未登録の商品(G0191) | 楽天・Yahoo! だけ更新。Eストアは運用者が後日登録 |

食い違いが出たら **AskUserQuestion(クリック形式)で聞く**。表は本文に貼ってから聞く(質問ボックスだけで終えない)。

## 3. アーティファクトで承認をもらう

商品ごとに「Eストア・楽天・Yahoo! の 旧→新」「単品か親子か」「触らない商品」「確認してほしい点」を一覧にして `Artifact` で出す(10/20 の形: 要約タイル+表+注意+実行順序)。**運用者の「実行して」「OK」を待つ。**「OKです、ただし〜」のように条件がつくときは、条件(例: この商品は触らない)を実行計画に反映してから進める。

## 4. 書込前チェック

1. **Codex(medium)へ漏れ確認**([[feedback_pre_write_codex_review]]・厳守)。送る内容の差分(plan.json の抜粋とスクリプトの更新部分)を `/cgd` Lv1 に渡す。**10/20 はこれを飛ばして進めた**(ドライランと1商品の試験実行で代えた。結果は問題なかったが、ルール違反。次回は必ず実行する)
2. **ドライランが全件「更新する/済」で通る**こと(下の各スクリプトの既定)
3. `.hq/board` を確認し、他セッションが同じ商品・同じファイルを触っていないこと
4. 文脈台帳に承認を記録する(`ctx_cli.py add <SID> OK 価格改定 YYYY-MM 3モール実行承認`・実行後に `drop` して `VERIFY` へ)

## 5. 更新する(1商品ずつ・読み戻しつき)

```bash
cd C:\ClaudeCode\023.商品マスタDB
# (a) Eストア 単品(plan.json の kind=single)
python tools/mall_price_estore_single.py --plan data/price_plan_YYYYMM/plan.json            # ドライラン
python tools/mall_price_estore_single.py --plan data/price_plan_YYYYMM/plan.json --apply
# (b) Eストア 選択肢つき(1商品ずつ。--apply は --item 必須)
python tools/estore_variation_price.py --plan data/price_plan_YYYYMM/estore_770_880.json --item G0675 --apply
#     在庫0・非表示の商品は --allow-hidden を付ける(価格だけ直す。G0061 で使用)
# (c) 023 の estore_price(親行)を新価格に揃える(下の SQL)
# (d) 楽天・Yahoo!(単品・親子とも)。まず1商品で試す
python tools/mall_price_rakuten_yahoo.py --plan data/price_plan_YYYYMM/plan.json --sku G0675 --apply
python tools/mall_price_rakuten_yahoo.py --plan data/price_plan_YYYYMM/plan.json --pending-ok 1 --apply   # 残り全部+Yahoo!店頭反映
```

- `--pending-ok N`: **先に `--sku` で試した商品が Yahoo! で未反映のまま残っている数**。これが合っていないと店頭反映を行わない(他の人の未反映を巻き込まないため)
- 事前チェックで想定外(計画と違う現在値・Yahoo! にセール価格・子コードに個別価格など)があれば**何も書かずに止まる**。「済」の商品は飛ばす(途中からの再開可)
- 楽天: 更新後に在庫が 0 に落ちていないこと、価格以外の項目のハッシュが不変なことを確認してから次へ進む
- Yahoo!: 価格以外のフィールドが不変なこと、子コード数が不変なことを確認
- (c) 023 の `estore_price`(`xxxx-parent` 行・Eストア登録商品のみ):
  `UPDATE products SET estore_price=?, version=version+1, updated_at=? WHERE sku=? AND version=?` と `audit_log` への記録(10/20 はその場の Python で実施。単品は (a) が自動で揃える)。
  **Eストアに未登録の商品(G0191)は estore_price が古いまま** → Eストア登録時に合わせて直す

## 6. 全件を読み直す

もう一度 `mall_price_plan.py --out <別フォルダ> --set …` を流すと、**全商品が「Eストア済/楽天済/Yahoo済」になる**はず(Eストア未登録は除く)。`mall_price_rakuten_yahoo.py` のドライランで Yahoo! の「未反映」が 0 になっていれば店頭反映も済んでいる。確認用のフォルダは終わったら消す。

## 7. 「制服のフジ情報共有」へ報告(/line-notify を別に呼ぶ)

```bash
python .claude/skills/mall-price-revision/make_report_spec.py <改定前のplan.json> <spec.json> --date "2026年11月1日" --updated "10/20 更新" [--names names.json]
python .claude/skills/line-notify/make_status_image.py <spec.json> <out.png>     # 開いて目視(商品名・数字)
```

- 文面は短く(10/20 の形): `【価格改定】11/1(日)から、下記商品の価格を改定しました。` / `Eストア・楽天・Yahoo!ショッピングとも、○/○に更新済みです。` / 改定した品番数と品目のまとめ / 未登録などの注記 / URL
- 以降は `/line-notify` の手順どおり: publish → **運用者がスマホで URL を開けると確認してから**承認依頼 → LINE WORKS で承認 → send --send
- 画像は Downloads と NAS の `999.一時ファイル` にもコピーする。公開画像は不要になったら `public-share/` から削除

## 8. 記録

- メモリ(project)に「今回の改定: 日付・品番数・決定事項・食い違い」を残す(2026-10-20 の分は [[project_mall_price_revision_20261101]])
- 毎回の改定で出た**新しい罠・決定**はこの SKILL.md の表に足す

## 罠(10/20 に踏んだもの・見つけたもの)

| 罠 | 対処 |
|---|---|
| Read ツールで PDF が読めない(poppler 無し) | PyMuPDF(fitz)で抽出 |
| 楽天 variant の価格が不揃い・特価の残り | 計画の一覧で気づく。x1.1 にそろえるか運用者に聞く |
| `yahoo_shopping_api.get_item` が読み取り専用の DB 接続で失敗(トークン更新が書く) | 通常接続で開く(商品データには書かない) |
| `sqlite3.Row` を使う関数に素の接続を渡すと `tuple indices must be integers` | `row_factory = sqlite3.Row` を付ける |
| Eストアの単品は `variation-attributes` が 404(選択肢なし) | 404 は単品として扱う。**未登録(basic も 404)と区別する**(plan 側で `kind=none`) |
| Eストア「選択肢つき」商品の `item_price` を変えると基本価格が最安の行に連動する | `estore_variation_price.py` が読み戻しで確認する |
| Eストアの選択肢更新は在庫 1 以上でないと止まる | 在庫0・非表示は `--allow-hidden` |
| Yahoo! 店頭反映は**ストア全体**が対象 | 他の未反映があれば行わない(`--pending-ok` で自分の分だけ許す) |
| 楽天・Yahoo! の 023 `price` は旧セール価格(「6/10まで10%OFF」等)が残っている | 更新時に新価格へ揃える。表示名のセール文言は別件(直さない) |

## 今月(2026-10-20)の実績

17品番(PDF 14行+キャップを4品番に展開)。Eストア 10品番・楽天 14品番・Yahoo! 12品番を更新、3品番は改定済みで変更なし。更新後に3モールを読み直して全件一致、Yahoo! 未反映 0 を確認。
楽天は更新前後で在庫が全品不変。未了: G0191 の Eストア登録(運用者が後日)と、そのときの 023 `estore_price` の更新。

## 検証した範囲と、していない範囲

- **検証済み**: 10/20 の実データでの一巡。`mall_price_plan.py` は更新後の状態で「全て済」と判定することを確認。純粋関数は pytest(`023.商品マスタDB/tests/test_mall_price_plan.py`)
- **未検証**: ①Yahoo! の子コードに個別価格がある商品(`subcode_price` 方式・事前チェックで止まる)②セール価格が設定された商品(止まる)③`--allow-hidden` を楽天側にも必要とする商品 ④Amazon・メルカリなど3モール以外の価格(対象外)⑤Eストア登録済みで variation-attributes の価格が行ごとに違う商品(plan は `row_prices` を持つが、グルーピングは基本価格基準)
