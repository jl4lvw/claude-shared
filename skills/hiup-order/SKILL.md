---
name: hiup-order
description: 合同会社ハイアップ(呉市光町・コンパス得意先23665)から届く卸売の発注書PDF(海自・海軍グッズ)を、GoQの新規受注(単価0円・手渡し・送り状なし)へ登録する専用手順。データ共有(076)のPDF→052のhiup_order.pyで品名を紐付け表(hiup_products.json)のGoQコードへ→承認の画面(wholesale_goq)で承認→登録→GoQで照合。商品が準備できたら、掛率(通常60%・特50/特55を実績で確認)→コンパスへ売上登録(承認の画面 compass_sales)→読み戻して納品書PDF→LINE「ハイアップ注文」で連絡→標準プリンターで印刷まで。「ハイアップの注文」「ハイアップの注文商品が準備できました」「ハイアップから発注書」「/hiup-order」で使う。江田島海自カレーをハイアップへ発注する方向(/tshirt-order の HIUP_CURRY)とは逆向きなので混同しない。
trigger: ハイアップ(合同会社ハイアップ・若狭様)から卸売の発注書が届いたとき
---

<!-- SKILL_VERSION: 2026-10-26_1600 -->

# hiup-order — ハイアップの発注書 → GoQ 新規受注

作業フォルダ: `C:\ClaudeCode\052.卸売注文GoQ統合`(以下この相対パス)。2026-10-24 の初回注文(データ共有 63 番)で作った。

## 前提(2026-10-24 運用者決定)

| 項目 | 決まり |
|---|---|
| 取引先 | 合同会社ハイアップ(担当 若狭様)。コンパス得意先 **23665**・呉市光町15-2(〒737-0831) |
| 渡し方 | **手渡し**。GoQ の配送会社は形だけ佐川急便、**送り状は発行しない**。電話欄は記録が無いので制服のフジ本店(0823-21-7731) |
| 単価 | **GoQ は 0 円**(052 が強制)。請求は別途コンパスで売上登録。過去実績は「税込上代の 60% を税抜単価」で1回ごとに伝票 |
| ひとことメモ | `23665:都度コンパス売上登録が必要 / ハイアップ発注書 YYYY/MM/DD 若狭様 / 手渡し(送り状は発行しない)`(パーサーが作る) |
| 受注番号 | `HIUP-<発注日>-<PDFのsha256先頭6桁>`。同じ PDF からは同じ番号(二重登録しない) |
| 承認 | **承認の画面(PWA)だけ**。チャットやアーティファクトで承認を求めない。対象名 `wholesale_goq` |

## 手順(止まってよいのは 3 の「新しい品名」と 5 の承認待ちだけ)

1. **PDF を取り出す**: `/img` で番号を確かめて取り出し、`intake_hiup/` に置く(`intake/` は産経の自動取込の場所なので使わない)
   ```bash
   cd C:/ClaudeCode/052.卸売注文GoQ統合
   python C:/ClaudeCode/076.写真共有PWA/tools/photoshare.py get <番号> --out intake_hiup --mark-used
   ```
   取り出した PDF は必ず Read で目でも見る(表の数量・合計)。
2. **読み取り(書き込みなし)**:
   ```bash
   python parsers/hiup_order.py "intake_hiup/<ファイル>.pdf" --dry-run
   ```
   合計が合わない・発注者がハイアップでない・表が読めない → 終了コード 2 で止まる。PDF の様式が変わった可能性があるので運用者に見せる。
3. **紐付け表に無い品名があると終了コード 3 で止まる**(推測で紐付けない)。候補は次で探して、表(数・根拠つき)を本文に書いてから AskUserQuestion で選んでもらう:
   - `023.商品マスタDB/data/productmaster.db` の `products`(商品名 LIKE)
   - `036.ピッキングリスト出力/data/picking.db` の `product_master`(ピッキングで出る名前・保管場所)と `code_map`(GoQ のコードが何に解決されるか)
   - ネット未出品の店頭品は品番が無いことがある → コンパスの売上(`/conphas-search --mode sales --kw <半角カナ>`)で実在を確かめ、運用者に仮コードを決めてもらう(例 `FUNA-PLACUP-GN`)
   - **仮コードは SKUDB にも行を足す**(足さないとピッキングリストで所在が出ない)。書く口は 042 の `POST http://127.0.0.1:8303/api/skudb/ops/add` だけ(CSV を直接いじらない)。
     行は A列=品名・B列とE列=仮コード・F列=所在(既存の所在から運用者に選んでもらう)。まず `commit:false` でエラー・警告・照合のずれが 0 件か確かめ、
     その後 `commit:true` で保存。保存直後は 036 が「書き出された直後」として約 20 秒読めない。読めたら `picking_logic.resolve_sku` で名前と所在が出るか確かめる
     (2026-10-24 FUNA-PLACUP-GN/WH を 12_F2-2階 で登録・履歴 4ba4db5b0b5e654b)
   - 色違い・枚数違いに注意(タンブラー 零戦 グリーン=G0768/灰色=G1283、カレー皿は 1枚単位 `CG0849-CRRYSARA-Sx1`)

   決まったら `parsers/hiup_products.json` に 1 行足す(`pdf_name` は発注書の表記そのまま。空白・全角半角は無視して照合される)。
4. **注文 JSON を作って確認**:
   ```bash
   python parsers/hiup_order.py "intake_hiup/<ファイル>.pdf"          # orders/HIUP-....json
   cd C:/ClaudeCode/900.ClaudeCode
   python -m wholesale_goq.cli check ../052.卸売注文GoQ統合/orders/HIUP-....json
   ```
5. **承認の画面へ申請**(本文に明細表を貼ってから):
   ```bash
   cd C:/ClaudeCode/900.ClaudeCode
   python -m wholesale_goq.cli enqueue ../052.卸売注文GoQ統合/orders/HIUP-....json --purpose "ハイアップのM/Dの発注書をGoQに入れてピッキングに載せるため"
   ```
   見張り役が LINE WORKS に知らせとリンクを送る。運用者には「LINE WORKS の知らせのリンクから承認の画面で」と伝え、
   **`a_browser_data/watch.log` の「wholesale_goq id=N 実行終了」を自分で待つ**(運用者に「押したら教えて」と頼まない)。
   承認後は見張り役が `wholesale_goq.cli run` を起動し、注文 JSON が承認時と同じ(ハッシュ一致)ときだけ `tools/submit_orders.py --send` で登録する。
6. **結果を確かめる**: `python -m wholesale_goq.cli show <id>` が `done` で、`sent/HIUP-....json` の `result.created_oid` があること。
   さらに GoQ を受注番号で読み直し、**明細(コードごとの「0円 x N個」)が注文 JSON と全行一致するか**を確かめる(読み取りのみ):
   ```bash
   cd C:/ClaudeCode/052.卸売注文GoQ統合
   python -X utf8 tools/goq_find_rows.py HIUP-....
   ```
   `goq_lookup.fetch_goq_info` の `items` は**常に 0 の固定値**で明細の数ではない(2026-10-24 に取り違えかけた)。明細は上のツールで見る。
   初回実績: 2026-10-24 `HIUP-20261024-ca2593` = 管理番号 115130(115-130)・新規受付・9行 220個・0円を確認。
   失敗・結果不明のときは、受注番号で GoQ を検索して有無を確かめてから報告する(二重登録しないため、積み直す前に必ず)。
7. **報告**: 受注番号・管理番号(oid)・明細を短く。

## 商品が準備できたあと(納品・請求・連絡・印刷。2026-10-26 初回実績)

運用者から「ハイアップの注文商品が準備できました」と来たら、8〜13 を**止まらず最後まで**続ける。止まってよいのは、運用者への質問(掛率など)と承認の画面での承認待ちだけ。

8. **掛率を決める**: ハイアップの掛率は**一律 60% ではない**(10/26 運用者指摘)。通常=税込上代の 60%・特価品は【特50】=50%・【特55】=55%。
   コンパスの売上(UMS・得意先23665)の過去実績を商品ごとに引く(履歴 `reports/hiup_conphas_sales_history.jsonl` は
   `conphas_search.py --mode sales --customer 23665 --from 2024-01-01 --json` で作り直せる)。実績のある品は「実績あり」、無い品は掛率を運用者に聞く
   (まとめて 1 回で聞く。運用者が「不明なものは全て 60%」と答えた実績あり)。結果を `parsers/hiup_pricing.json` に足す
   (税込上代・掛率・`jisseki`・根拠の伝票)。**単価はすべて税込(内税)**。税抜として税を上乗せしない(10/26 に誤って 10% 多い納品書を作った)。
9. **コンパスへ売上を登録**: `013.CONPHAS-PWA/server/post_hiup_YYYYMMDD.py`(`post_hiup_20261026.py` を写す)を作り、確認のみで全行の一致を確かめる。
   承認は**承認の画面だけ**(チャットで承認コードを求めない):
   ```bash
   cd C:/ClaudeCode/900.ClaudeCode
   python -m compass_sales.cli enqueue post_hiup_YYYYMMDD.py --purpose "ハイアップの○/○発注分をコンパスの売上伝票に登録し、納品書を作るため"
   ```
   `a_browser_data/watch.log` の `compass_sales id=N 実行終了` を自分で待つ。商品コードはコンパスの商品マスタにある品はそのコード
   (過去の実績コードを優先)、無い品は同名・同価格のマスタ品を当てて承認の画面で確認してもらう(名前が違うものは注意書きを添える)。
   キーは `sales-hiup`(得意先23665だけ・担当者11・内税・商品コード指定)。詳細は `/approved-write`。
10. **別の接続で読み戻して照合**: UMS(得意先 `0000023665`・日付・伝票番号)の全項目が確認内容と一致すること・伝票番号の重複が無いこと。
11. **納品書をコンパスの内容から作る**(金額入り・税込・A4 1 枚):
    ```bash
    cd C:/ClaudeCode/052.卸売注文GoQ統合
    PYTHONUTF8=1 "C:/Users/user/AppData/Local/Programs/Python/Python312/python.exe" tools/hiup_delivery_note.py sent/HIUP-....json --date YYYY-MM-DD --from-compass <伝票番号>
    ```
    数量・単価・税込・税が単価表の計算と全行一致しなければ止まる。出来た PDF は Downloads と `\\192.168.1.50\共有スペース\寺下\999.一時ファイル` にコピーし、画像にして目で確かめる。
12. **LINE で連絡**(グループ「ハイアップ注文」・`/line-notify`): 納品書の PNG(150dpi)を `line_notify.py publish --prefix hiup` で公開し、
    `request-approval`(目的の例 `--purpose "ハイアップの注文商品が準備できたことと納品書を知らせ、引き取りに来てもらうため"`)→ `wait-approval`(承認の画面)→ 承認されたら `send --send`。
    文面はあいさつ・名乗りなし。例: 「ご注文の商品が準備できました。納品書はこちらです。/ 都合の良い時にお引き取りをお願いいたします。」(10/26 運用者指定)
13. **納品書を標準プリンターで印刷**(LINE 送信のあと・運用者指示 10/26): `cd "C:/ClaudeCode/017.Tシャツなどの注文" && python print_fax_copy.py "<納品書PDF>"`。
    プリンター名は聞かない・報告に標準プリンター名を書く(10/26 は EPSON PX-S887(普通紙))。LINE の送信に失敗・未承認なら印刷しない。

## 注意

- 紐付け表にない品名を、名前が似ているからと勝手に既存コードへ寄せない(2026-10-24: プラコップはネット未出品で品番が無かった)
- `orders/` の HIUP-*.json は産経の自動登録の対象外(`auto` 付きだけが自動で送られる)。手で `submit_orders.py --send` を直接叩かない(承認の画面を通す)
- 見張り役の承認対象を足した・変えたときは見張り役の再起動が要る(`taskkill /F /PID <run_watch.pyw の PID>` → `Start-ScheduledTask A_BrowserWatch`。再起動前に `requests` の queued/running が無いことを確かめる)
- 実装: `052.卸売注文GoQ統合/parsers/hiup_order.py`・`parsers/hiup_products.json`・`tests/test_hiup_order.py`・`tools/goq_find_rows.py`(明細の照合) / `900.ClaudeCode/wholesale_goq/cli.py` / `a_browser_watch/targets.py` の `wholesale_goq`(テスト `a_browser_watch/tests/test_watch_wholesale_goq.py`)
- 他の取引先を同じ形で足すときは、`parsers/<取引先>.py` で 052 の注文 JSON を作れば、5 以降(wholesale_goq)はそのまま使える
