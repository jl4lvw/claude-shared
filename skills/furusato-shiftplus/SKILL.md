---
name: furusato-shiftplus
description: 呉市ふるさと納税(シフトプラス株式会社)の1,000円寄附品(水兵あられ等)受注をGoQへ登録し、出荷完了をシフトプラスへ通知するまでの専用手順。052.卸売注文GoQ統合を使う。wholesale-orderの上位互換ではなく、このチャネル固有の罠(切詰め・削除不可・列名完全一致)を凝縮したもの。
trigger: シフトプラス/呉市ふるさと納税の新規注文・出荷登録・出荷完了通知を扱うとき
---

<!-- SKILL_VERSION: 2026-09-07_000000 -->

# furusato-shiftplus — 呉市ふるさと納税(シフトプラス)専用スキル

作業フォルダ: `C:\ClaudeCode\052.卸売注文GoQ統合`（相対パスは全てここ基準）。
本スキルは[[wholesale-order]]スキルの一部だが、このチャネル固有の罠が多いため
独立させてある。**大枠の流れは`wholesale-order`と同じ**（新着チェック→パース→検証→
承認→送信→照合→報告）。ここではシフトプラス固有の差分だけを詳しく書く。

---

## 受注経路は2つある(取り違えない)

| | チャネルA(メール通知) | チャネルB(実住所CSV) |
|---|---|---|
| 届き方 | `terashita@seifukunofuji.com`の`00C.販売先/シフトプラス`フォルダのメール(「返礼品出荷依頼」) | `sell_YYYYMMDDHHMM.csv`(共有スペース`寺下\999.一時ファイル`に置かれる) |
| 住所 | **無い**(紙の伝票が配送業者経由で別便で届く) | **実際の寄附者住所が入っている** |
| GoQでの扱い | 送り状は**発行しない**(ピッキングリストに載せるためだけの登録)。ダミー住所(制服のフジ本店)・氏名「ふるさと納税」固定 | **実際に個別の送り状を発行する**(クリックポスト) |
| パーサー | `parsers/furusato_shiftplus.py` | `parsers/furusato_shiftplus_csv.py` |
| 受注番号 | `FURUSATO-<YYYYMMDD>`(メール1通=1受注にまとめる) | `FURUSATOCSV-<寄附受付No>`(1行=1受注、後述) |
| 基準日ガード | あり(`tools/channel_baseline.py`、チャネル名`furusato_shiftplus`) | 無し(CSVファイル単位のdedupで足りる。ただし後述の「1行=1受注」注意) |

以下、主に**チャネルB(実住所CSV)**の手順を詳しく書く。チャネルAは[[wholesale-order]]の
該当節を参照。

---

## 1. 新着CSVチェック

```bash
# PowerShellでNAS上のsell_*.csvを確認(UNCパスはBashのpython -cだと失敗するのでPowerShell必須)
Get-ChildItem "\\192.168.1.50\共有スペース\寺下\999.一時ファイル\" -Filter "sell_*.csv" | Sort-Object LastWriteTime
```

新しいファイルがあれば`intake/`へコピー(`Copy-Item`)。**同じ寄附がファイルをまたいで
再掲されることがある**(まだ「出荷依頼」ステータスのものは毎回の抽出に含まれる)。
既に`orders/`+`sent/`+`external_done/`にある受注番号は自動でスキップされる。

## 2. 「1行=1受注=1送り状」— 数量合算は絶対にしない

**同一商品コードの複数行も合算しない**(2026-09-02、有本様の実例で発覚。2026-09-07、
高橋様でも同型の実例あり)。シフトプラス管理画面(出荷依頼管理)側でも、同じ商品を
複数個寄附された場合は複数行として別掲される。`furusato_shiftplus_csv.py`は行ごとに
`qty=1`のitemを積むだけで、コードが同じでもマージしない設計になっている。1回の寄附に
複数商品が含まれる場合(例: 呉本様の3商品)も同様に**別々の送り状に分割する**
(ユーザー確定・「別々の送り状にしましょう」)。

複数item(複数行)を持つ注文JSONは、`tools/export_furusato_goq_csv.py`が自動的に
受注番号へ`-1`/`-2`の枝番を振って別々のCSV行(=別々のGoQ受注)に分割する。パーサー側は
「合算しない」だけでよい。

**同一商品コードが1つの注文JSON内に複数あってもエラーにしない**(2026-09-07、高橋様の
実例で発覚。有本様と同型)。`order_model.validate_order()`の「codeが重複しています」は
本来register_only(047が1受注内の重複コード行を拒否する)向けのチェックで、export時に
必ず分割されるこのチャネルには当てはまらない。`furusato_shiftplus_csv.py`はこのエラー
文言だけを結果から除外してから検証エラー判定する。

## 3. あられの商品名は短縮する(送り状の内容品欄が切り詰められるため)

GoQの送り状「内容品」欄は商品名の先頭を一定文字数で切り詰めて印字する(2026-09-02実測)。
元の商品名(「【1,000円寄附】水兵あられ(しょうゆ) ku091-067-r」)だと**フレーバーの
判別情報が切れて消える**。`furusato_shiftplus_csv.py`の`ARARE_SHORT_NAME`が既知の
あられ3コードを短縮名に変換する:

```python
ARARE_SHORT_NAME = {
    "ku091-067-r": "水兵あられ(しょうゆ)",
    "ku091-068-r": "水兵あられ(味このみ)",
    "ku091-069-r": "水兵あられ(海老つまみ)",
}
```

新しい返礼品コードが増えた場合、その商品名も長い/紛らわしいなら同様の短縮を検討する。

### あられの配送会社閾値(合算はしないが、1受注内の数量には適用する)

同一受注内の数量合計が**4個まではクリックポスト、5個以上は佐川急便**に自動で切り替わる
(`ARARE_CLICKPOST_MAX_QTY = 4`)。ただし上記の「合算しない」ルールにより、通常は
1受注=1商品=数量1なので、この閾値に達することは稀(同一寄附で同じあられを5個以上
選んだ場合のみ発動)。

## 4. GoQへ登録する(カスタムCSV取込・10件超は必須)

```bash
python tools/export_furusato_goq_csv.py       # orders/FURUSATOCSV-*.json → 取込用CSV生成
```

- 出力は**商品名でソート済み**(ピッキング時に同じ商品をまとめて処理できるように)
- **列名はGoQの正式名称と1文字でも違うと自動マッピングされず無言でスキップされる**
  (`個数`・`お支払い方法`・`注文者氏名（カナ）`全角括弧・`注文者住所：都道府県`等、
  6列で実際に食い違いが発生した)。`export_furusato_goq_csv.py`のHEADER定数を変更した場合は
  必ずアップロード後の列マッピング確認画面で全列を目視確認する
- 手順: GoQ(`https://order.goqsystem.com/goq21/modules/CustomImportCSV/index.php/top/`)で
  対象店舗(唯一の選択肢「CSV」)を選ぶ→CSVファイル選択→「送信」→「確認して実行」
  (列マッピング確認画面が出る、**ここでユーザーに確認を取る**)→「実行」
- 実行後、URLが変わらないことがある(AJAX的処理)。**必ずGoQを読み直して照合する**
  (`tools/goq_lookup.py`の`fetch_goq_info`)。20件超でも1回のバッチ照会で足りる
- アップロード前のCSV内容は、件数が多い場合ほどアーティファクト(HTMLテーブル)で
  一覧表示してからユーザー確認を取る

登録後はローカルの`orders/*.json`を`sent/`へ移し、`result`にGoQのoidを記録する
(手動でjson編集。専用の自動化ツールはまだ無い)。

## 5. 誤登録の訂正(GoQは物理削除できない・キャンセルのみ)

商品名を間違えた等でGoQ側を修正したい場合:

1. GoQ側で該当受注を**キャンセル**(削除機能は無い。ユーザーが手動で行う)
2. **同じ受注番号は再利用しない**。元の受注番号に`-2`枝番を付けて新規登録する
   (例: `FURUSATOCSV-34202260003669` → `FURUSATOCSV-34202260003669-2`)
3. 元の`sent/*.json`は`status: "superseded_canceled_in_goq"`にして残す(履歴として)
4. 新しい`-2`版を`orders/`に作り、通常の登録フロー(手順4)で再登録する

## 6. シフトプラスへ出荷完了を通知する(専用CSVアップロード)

出荷処理(送り状発行)が終わったら、GoQとは別に**シフトプラス側にもCSVアップロードで
出荷完了を知らせる**必要がある。

```bash
python tools/export_shiftplus_shipped_csv.py            # 未通知分のみ・CSV出力+マーク
python tools/export_shiftplus_shipped_csv.py --all       # 全件・マークしない(確認用)
```

列は`配送No/伝票番号/配送業者/出荷予定日`(CP932)。

**🔴 最重要の罠(2026-09-02、実際にアップロード事故が発生)**: **「配送No」列は
シフトプラス側の「配送No.」の値**(例`20260005112`)であり、**GoQの受注番号ではない**。
最初の実装ではGoQ受注番号を入れてしまい、シフトプラス側のアップロードで受理されなかった。
正しくは、出荷依頼CSVの各行が持つ「配送No.」列を`furusato_shiftplus_csv.py`が
item単位で`delivery_no`として保持しており(パース時に`row.get("配送No.")`から採取)、
`export_shiftplus_shipped_csv.py`はこの`delivery_no`をそのまま1列目に出す。
GoQ受注番号は**GoQへ照会して伝票番号・配送会社を読み取るためだけに使う**(出力しない)。

- 伝票番号・配送業者は実際に出荷処理した後にGoQから読み取った実データ
  (`goq_lookup`経由・register_only時点の予定値ではない)
- 出荷予定日 = **実行日**(YYYY/MM/DD)
- 送り状未発行(GoQのtracking_noが空)の注文は自動的に対象外
- 既定は「未通知分のみ」(`sent/*.json`の`shiftplus_reported_at`で管理、Sankeiの
  `make_sankei_report.py`と同じパターン)
- **アップロード前に実物サンプルと突き合わせて検証すること**(2026-09-02、ユーザーの
  既存VBAツールがGoQから直接抽出したサンプルと「配送No/伝票番号/配送業者/行順」が
  完全一致することを確認済み)

## 7. GoQのステータス文言だけで出荷済みと判断しない

GoQの受注ステータスが「処理済」でも、送り状番号(伝票番号)が一切記録されていない
(＝実際には未出荷)ケースが実際に発生した(2026-09-02、SANKEI-319109/319280で発覚。
産経チャネルの実例だが同じ罠がこのチャネルにも当てはまる)。**必ずtracking_noの有無で
「本当に出荷済みか」を判定し、ステータス文言だけでは判断しない**。

## 8. 出力ファイルは必ずNAS+Downloadsへコピー

**絶対厳守**: このPCでCSV/PDFを出力したら、判断せず無条件に以下2箇所へコピーする
([[feedback_user_check_files_downloads_and_nas]]):

1. `C:\Users\user\Downloads\`
2. `\\192.168.1.50\共有スペース\寺下\999.一時ファイル`(PowerShellの`Copy-Item`推奨。
   Bashからの日本語UNCパスは不安定)

---

## 関連ファイル

- パーサー: `parsers/furusato_shiftplus.py`(メール・まとめ登録)、
  `parsers/furusato_shiftplus_csv.py`(実住所CSV・個別送り状)
- GoQ取込CSV生成: `tools/export_furusato_goq_csv.py`
- シフトプラス出荷完了通知CSV生成: `tools/export_shiftplus_shipped_csv.py`
- 基準日ガード: `tools/channel_baseline.py`(チャネルA用。チャネルBは未使用)
- 詳細な設計判断・事故の経緯: `052.卸売注文GoQ統合/README.md`、
  `.claude/skills/wholesale-order/SKILL.md`の「7.5」「7.6」節
- 長期記憶: `project_052_sankei_goq_wholesale.md`、
  `feedback_furusato_arare_carrier_threshold.md`、
  `feedback_bulk_order_needs_csv_upload_not_one_by_one.md`、
  `feedback_goq_status_can_advance_without_tracking.md`
  (`~/.claude/projects/C--ClaudeCode/memory/`)

**🚨 このスキルファイル自体が一度消失した経緯(2026-09-07)**: 初版作成後、`/g-ul`で
claude-sharedへ確定させる前に別セッション/常駐GUIの`/g-dl`が走り、`.claude/skills/`
全体が古いclaude-shared状態でミラー上書きされてこのファイルごと消えた
([[feedback_claude_dir_changes_must_be_gul_or_mirror_reverts]])。**新規スキル/`.claude`配下の
変更は作成直後に`/g-ul`まで完了させること**。
