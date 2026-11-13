---
name: master-sync-check
description: 商品マスタ(023)が古いまま本店(Eストア)・Yahoo! へ古い/誤った情報を展開しないための点検と直し方。楽天を正として、マスタ・本店・Yahoo! の商品名・キャッチ・説明・Caption・スマホ用の追加説明・年号を突き合わせ、マスタを楽天の最新へ強制的に取り込み直してから、ずれた欄をそろえる。「マスタが古い」「年号が違う」「2026年のまま」「楽天と本店・Yahoo!で商品名が違う」「楽天に合わせて」「/master-sync-check」で使う。
---

# /master-sync-check — マスタを楽天の最新にしてから展開する

2026-11-10 運用者決定:「マスタは毎回最新版にして。強制的に」。受付 #60(G1674 三笠カレンダー)で、
楽天は 2027 年版なのに、マスタ・本店・Yahoo! の商品名・Yahoo! のキャッチコピー・Caption・スマホ用の追加説明が
2026 年のまま残った。楽天から写す経路が説明と写真しか無く、マスタを元に書く経路は楽天と照合していなかったため。

## 仕組み(どこで強制されるか)

| 経路 | 強制の場所 | 動き |
|---|---|---|
| 090「楽天の変更を反映」`sync_from_rakuten` | `090/api/mall_sync.py`(承認案)・`mall_sync_run.py`(実行の順) | 照合 → **マスタ取り込み** → 写真 → 説明 → **商品名ほか**。マスタだけ古くても承認案を出す |
| 本店の登録・更新(内容に触るとき) | `023/server/routers/estore.py`(`/register` `/register/start` `/update`) | 書く前に `master_refresh.gate`。楽天を読めなければ 409 |
| Yahoo! の更新・新規登録 | `023/server/routers/yahoo.py`(`/api-sync/{sku}` `/register-new` `/register-new/start`) | 同上 |
| 商品登録の流れ `/register-product` | 上の API を呼ぶので自動で通る | — |

在庫・価格・送料だけの更新は楽天を読まない(楽天 API の不調で在庫の同期まで止めないため)。**価格は取り込み直さない**(本店だけ別価格の商品がある)。

部品:
- `023.商品マスタDB/server/services/master_refresh.py` … 取り込み直し(`refresh`)・宣伝の括弧を外す(`clean_title`)・変わった部分の組(`diff_pairs`)と当て込み(`apply_pairs`)・残った年号(`stray_years`)・記録した組(`recent_pairs`。監査記録 `action='refresh_pairs'`)
- `023.商品マスタDB/tools/name_sync.py` … 本店の商品名・Yahoo! の商品名・キャッチコピー・Caption・SpAdditional の下見と書き込み(`inspect` / `run --steps pm,es,yh,yh_pub`)

## 手順

### 1. 下見(何も書かない)
```
cd C:\ClaudeCode\023.商品マスタDB
python -X utf8 tools/name_sync.py inspect G1674
```
- `master.changed`: マスタで楽天の最新に変わる列(商品名・キャッチ・説明・本店用の商品名)
- `master.pairs`: 楽天で変わった部分(例 `2026年→2027年`)
- `fields.<欄>`: 今の値(now)・当てた後(new)・自動で書けない理由(problems: 楽天に無い年号が残る・「楽天」の語)
- 報告の商品名の頭に商品コード、G番号には商品名を添える

### 2. 直す(承認は承認の画面で 1 回)
**090 の流れに乗せる**(承認・実行・読み戻し・Yahoo! 店頭反映・報告画像まで道具がそろっている):
- その商品の受付が 090 にあり、完了・未クローズなら、承認の画面の「AIに指示し直す」(`reinstruct`)で
  「商品名・キャッチほかも楽天の最新にそろえて」と指示する → 承認案に「商品名・キャッチほか」の節が出る
- 受付が無ければ、LINE で FujiWorks に「G#### 楽天に合わせて本店とYahoo!も更新して」と頼む(担当者か運用者)
- 承認されたら実行役が `マスタ → 写真 → 説明 → 商品名ほか` の順に進める。結果は 090 の受付で確かめる

承認案の「※ 〜は自動では直せません」の欄は、手で直す(/mall-product-update)。直したら 1 の下見で残りが無いことを確かめる。

### 3. 確かめる(事後・必須)
- もう一度 `name_sync.py inspect` を流し、`master.changed` が空・`fields` の now と new が同じであること
- 年号の残りは `fields.*.problems` が空であること
- Yahoo! は店頭反映まで数分かかる

## 価格と公開(2026-11-10 追加・厳守)
- **価格は楽天が正**。指示が無ければ 3 店とも楽天と同じ価格(税込)にする。価格が食い違ったまま展開・公開へ進めない(運用者「大事故」)
- 090 の「楽天の変更を反映」は 3 店の価格を読み、違えば承認案の一番上に価格の節が出る(実行の最初に書く)。価格を読めない商品(選択肢つき)は承認案を作らず要対応
- 単品の価格だけ直すとき: `900.ClaudeCode` で 023 `tools/mall_price_set.py plan --spec` → `python -m mall_price_set.cli enqueue <plan> --purpose ...`
- 公開(本店・楽天の倉庫外し・Yahoo! 公開)と 092 への登録: 023 `tools/mall_publish.py plan --spec {"items":[{"code":"G####"}]}` →
  `python -m mall_publish.cli enqueue <plan> --zaiko <{"items":["G####"],"prep":[["adjust","G####","数","--note","理由"]]}> --purpose ...`(3 店の価格がそろっていないと案にならない)
- 事後は 3 店の公開・価格・在庫を直接読み戻し、店頭のページが開けることまで確かめる(受付 #60 G1674 の実例: 申請 893 価格・894 公開+092 在庫 7)

## してはいけないこと
- マスタの在庫を楽天から取り込まない。帽子の例外(本店 3,520 円/楽天 3,980 円)は、運用者の指示があるときだけ価格を変えずに残す
- Caption・スマホ用の追加説明を楽天の説明で丸ごと上書きしない(1,666 件中 1,122 件が説明と別の文。11/10 集計)
- 「楽天」の語を Yahoo! に書かない(`clean_title` が「楽天ランキング N 位」を外す。当てた後に残れば自動で書かない)
- 承認をチャットで求めない(承認の画面だけ)

## 関連
- 090 の一覧表: `090.LINE業務指示エージェント/docs/capabilities.md`(楽天の変更の反映の行)
- 説明: `023/tools/desc_sync.py`・写真: `023/tools/photo_expand.py`
- 試験: 023 `tests/test_master_refresh.py`・090 `tests/test_mall_sync.py`
