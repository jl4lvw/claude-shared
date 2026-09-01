---
name: imaizumi-mail-report
description: 今泉さん(imaizumi@seifukunofuji.com)がFrom/To/Ccに入っているメールをThunderbirdのローカルmboxから読み取り、案件(商談)単位に整理して「何をしたか・どんな成果があったか」をまとめたHTMLレポートを作る。各案件の中はメールを時系列に並べる。毎日自動実行し、NAS(999.一時ファイル)へ日付付きで保存する。
trigger: 今泉さんの業務メール分析レポートを作る/更新するとき。毎日の定期実行から呼ばれる
---

<!-- SKILL_VERSION: 2026-09-01_004500 -->

# imaizumi-mail-report — 今泉さん業務メール分析

作業フォルダ: `C:\ClaudeCode\057.業務メール分析`(以下すべて相対パス)。

今泉さんは制服のフジの海外(中国)取引先とのやり取り(見積・サンプル・出張調整等)を
担当している。ThunderbirdのIMAPフィルタで今泉さん宛て/発のメールが専用フォルダへ
自動で振り分けられている。このスキルはそのフォルダを直接読み、内容を要約して
**案件(商談)単位**にまとめたレポート化する。

レポートは「時系列」ではなく「案件ごと」が構成の主軸(2026-09-01ユーザー指示)。
件名(subject)はリプライの多重引用でほぼ意味を持たないため**レポートに表示しない**
(データとしては保持するが、案件分類の材料以外には使わない)。1つの案件は「同じ
取引先・同じ商品/目的についての一連のやり取り」。単価交渉→サンプル調整→出荷のように
話が進んでいるだけなら1案件のまま、商品/目的が明確に変われば別案件に分ける。

表示はタブ切り替え(案件=タブ、直近動いている案件が左/デフォルト表示)。各タブの中は
**新しいメールが上**(直近の動きがすぐ見える)。タブ直下に案件サマリー(現状どうなって
いるか)を表示する(2026-09-01追加指示。「縦に長すぎて目的の箇所にたどり着けない・
どこが更新されたか分かりにくい」というフィードバックに対応)。

対象メールボックス(mbox直読み):
```
C:\Users\user\AppData\Roaming\Thunderbird\Profiles\099w3lj0.default-release
  \ImapMail\mail48.onamae.ne-2.jp\INBOX.sbd\&TspsyTBVMJM-
  (Thunderbird上の表示名: fw-terashita@seifukunofuji.com アカウント配下 INBOX/今泉さん)
```
`config.py` の `MBOX_PATH` で定義済み。パスを変更する場合はここを直す。

## 全体の流れ(この順を必ず守る)

```
1. 抽出    python extract_new.py          → data/pending_summaries.json
2. 要約    Claude が pending を読んで判断  → data/claude_summaries.json(案件分類含む)
3. 合流    python apply_summaries.py      → data/timeline.json / data/state.json / data/cases.json 更新
3.5 サマリー更新  Claude が動きのあった案件のオーバービューを書き直す → data/case_overviews.json
4. 生成    python render_report.py        → reports/imaizumi_report_YYYYMMDD.html (NAS保存用)
           python render_artifact.py      → artifact_report.html (Artifact公開用・任意)
5. 保存    PowerShellでNASへコピー(下記コマンド)
6. 公開    Artifactツールで artifact_report.html を公開/更新(任意・オンデマンド確認用)
```

新着が0件(`extract_new.py` の出力が「新着 0 件」)の場合は、2〜4をスキップして
「本日は新着なし」とだけ報告して終了してよい。

mbox以外(手渡し・ファイル共有等)で得た参考資料を追加したいだけの場合は、
1〜3の代わりに `python add_manual_entry.py` を使う(3.6参照)。その場合も
3.5(サマリー更新)以降は同じ。

## 1. 抽出(メカニカル・判断不要)

```bash
python extract_new.py
```

`data/state.json` の `processed_ids` に無いメッセージだけを mbox から取り出し、
`data/pending_summaries.json` へ出力する。添付のうちExcel(1MB以下)とPDFは
このスクリプトが中身をテキスト化して `attachments[].extracted_text` に入れる
(写真・Word・zip等は対象外、`skipped_reason` が付く)。

**この段階ではまだ `state.json` は更新されない**(要約が完了して初めてprocessed扱いになる。
途中で失敗しても再実行すれば同じメールがもう一度 pending に出てくる)。

## 2. 要約+案件分類(Claudeの判断が必要)

`data/pending_summaries.json` を読み、1メッセージ = 1エントリで
「今泉さんが何をしたか・どんな成果や進捗があったか」を日本語で要約し、
**どの案件(case_id)に属するか**も判定する。

先に `data/cases.json` を読み、既存の案件一覧を把握すること。同じ商談が続いているなら
**既存のcase_idを再利用する**(新規IDを乱発すると同じ案件がレポート上で分裂する)。
明らかに新しい商談・目的なら新しいcase_id(英数字とハイフンのみのスラッグ)を作ってよい。

指針:
- 同じスレッドへの返信が多い(引用が本文に丸ごと入っている)。**そのメール固有の
  新しい進展だけ**を書く。引用部分の繰り返しをそのまま要約に書かない
- `attachments[].extracted_text` に具体的な数字(請求金額・発注数量など)があれば
  `highlights` に反映する
- 事実ベースで書く。本文に無い推測(成立していない契約を「成立」と書く等)をしない。
  交渉継続中なら「継続中」「未確定」と明記する
- 写真・Word・zip等 `skipped_reason` 付きの添付は中身を見なくてよい
- 案件は同じ取引先・同じ商品/目的のやり取りをひとまとめにする。話が進んでいるだけなら
  分けない。商品/目的が変われば分ける(1メールずつ別案件にしない・全部1案件にもしない)

出力形式(**pending全件のmessage_idキーが必須**。1件でも漏れると次の合流でエラーになる):

```json
{
  "<message_id>": {
    "summary": "1〜2文の要約",
    "highlights": ["成果・重要事項(0〜3個、無ければ空配列)"],
    "case_id": "案件スラッグ(既存の再利用 or 新規)",
    "case_name": "案件の表示名(日本語、取引先・商品・目的が分かる具体名)"
  }
}
```

`data/claude_summaries.json` として保存する。件数が多い場合(目安20件超)は
general-purpose の Agent へ委譲してよい(実績: 72件の要約+5案件への分類を
別々のエージェントで実施済み、2026-09-01)。委譲する場合も出力ファイルパスと
件数一致の確認、および既存 `cases.json` の再利用を必ず指示すること。

### 既存メールの再分類が必要な場合

新しい観点で案件を分け直したい等、`timeline.json` に既にあるエントリの
case_idを直接更新したい場合は、`data/case_assignments.json`
(`{message_id: case_id}`)と `data/cases.json` を書いてから
`python assign_cases.py` を実行する(通常フローの2〜3の代わりに使う一括更新手段)。

## 3. 合流

```bash
python apply_summaries.py
```

`pending_summaries.json` + `claude_summaries.json` を `timeline.json` へ追記し、
`state.json` の `processed_ids` / `last_run` を更新する。要約が1件でも欠けていると
エラー終了する(欠けたまま取り込まない)。成功すると両方の一時ファイルが空にリセットされる。

## 3.5 案件サマリー更新(Claudeの判断が必要)

`data/case_overviews.json` は各案件タブの直下に表示される現状サマリー。
2026-09-01ユーザー指示で「もっと詳細に」と修正が入っており、**2〜4文の
簡潔な要約ではなく、経緯が追える程度の詳しさ**で書く。目安:
- 案件の背景(いつ・誰と・何をきっかけに始まったか)
- 主要な経緯を日付/金額/数量つきで(単価の推移、サンプル発送の追跡番号、
  サイズ変更の理由など、具体的な事実を並べる)
- 現状のステータスと、確定していない点(価格・納期・仕様等)を明記

**値は文字列1本ではなく、段落の配列にする**(同じくユーザー指示、2026-09-01。
長文を1つのdivに詰めると改行が無く読みにくいため、3〜5段落程度に分けて
`<p>`ごとに表示する。レンダラー側は後方互換で文字列1本も受け付けるが、
新規に書くときは必ず配列にする)。

今回新着があった案件だけ、`timeline.json` の該当case_idの全エントリ
(summary/highlights)を読み直して書き直す(新着が無かった案件はそのままでよい)。

```json
{
  "<case_id>": ["段落1(背景)", "段落2(経緯の前半)", "段落3(経緯の後半)", "段落4(現状ステータス)"]
}
```

既存の他案件のキーを消さないこと(部分更新)。

## 3.6 メール以外の参考情報を追加する場合

今泉さんから手渡し・ファイル共有・口頭等、**mbox以外の経路**で得た参考資料
(見積書PDF/Word等)を記録したい場合は、通常フロー(1〜3)を使わず
`add_manual_entry.py` で直接追加する(2026-09-01追加、実績: 七福タオル見積書)。

1. `data/manual_entry_draft.json` にエントリ内容を書く(スキーマは
   `add_manual_entry.py` のdocstring参照)。金額・数量の表は `tables`
   フィールド(`[{"caption", "headers", "rows"}]`)で構造化して渡す
   (highlightsの箇条書きだけでは表として見づらいため)
2. `python add_manual_entry.py` を実行 → `timeline.json` / `cases.json` /
   `case_overviews.json` を更新し、draftを空にリセットする
3. 案件名に迷ったら「参考情報」(case_id: `reference-info`)を使う。特定の
   商談スレッドに属さない資料を溜めておく汎用バケツとして運用している

## 4. レポート生成

```bash
python render_report.py     # NAS保存用の自己完結HTML → reports/imaizumi_report_YYYYMMDD.html
python render_artifact.py   # Artifact公開用フラグメント → artifact_report.html
```

案件はタブ切り替え(直近動きがあった案件が左/デフォルト表示)、各タブ内は
新しいメールが上(降順)。

`render_report.py` は毎回**その日の日付のファイル名**で新規出力する(上書きしない、履歴を残す)。

## 5. NASへ保存(日次で必須)

```powershell
$src = "C:\ClaudeCode\057.業務メール分析\reports\imaizumi_report_<YYYYMMDD>.html"
$dstDir = "\\192.168.1.50\共有スペース\寺下\999.一時ファイル"
if (Test-Path -LiteralPath $dstDir -PathType Container) {
    Copy-Item -LiteralPath $src -Destination (Join-Path $dstDir (Split-Path $src -Leaf)) -Force
} else {
    # フォルダが無い/名前不一致の場合は上書きファイルを作ってしまう事故があるので
    # 絶対にそのままコピーしない。ユーザーに確認する
}
```

`-PathType Container` の確認を省略しない([[feedback_user_check_files_downloads_and_nas]]参照、
2026-08-09にフォルダ消失+誤コピー事故の実績あり)。日本語UNCパスはGit Bash経由のPythonだと
`WinError3` になることがあるため、**このコピーは必ずPowerShellツールで直接実行する**
(Bash経由のpython -cは使わない)。

## 6. Artifact公開(任意)

ユーザーがすぐ見たい/確認したい場合のみ、`artifact_report.html` をArtifactツールで
公開・更新する。favicon絵文字は 🧵 で固定(再公開時も変えない)。日次の自動実行では
NAS保存が必須要件で、Artifact公開は無くても失敗にはしない。

## 定期実行(未着手・現状は手動起動のみ)

「毎日自動」が最終目標だが、2026-09-01時点では**自動化していない**。
`/imaizumi-mail-report` 等でユーザーが都度起動する運用。

自動化を再検討する際の前提:
- `CronCreate` はセッション限定・7日で自動失効するため不可(このセッションでの実測)
- `schedule` スキルのクラウドルーティンはAnthropicのクラウド実行でローカルファイル
  (Thunderbirdのmbox・NAS共有)に一切アクセスできないため不可(このセッションで確認済み、
  何も作成せず中断した)
- ローカルで完結させるには、他のPWA監視タスクと同様に**Windowsタスクスケジューラ**から
  Claude Code CLIをヘッドレス起動する方式が候補([[feedback_task_scheduler_ascii_launcher]]の
  ASCIIランチャー規約に従う)。ユーザー了承の上で着手すること
- Discord通知は不要(2026-09-01ユーザー指示)

## 既知の注意点

- **文字コード**: 一部のメール(特に中国側取引先からの転送分)はヘッダが `gb2312` と
  自己申告しているが実体は `gb18030`(GBK超過範囲の日本語漢字含む)で、strictな
  gb2312でデコードすると文字化けする。`mbox_reader.py` の `CHARSET_ALIASES` で
  gb2312→gb18030に読み替え済み(2026-09-01に発見・修正)。新しい文字化けパターンが
  出たら同じ要領で `CHARSET_ALIASES` に追加する
- 添付PDFが画像のみ(スキャン画像等)の場合、`extracted_text` が空文字列になる
  ことがある(OCRはしていない)
- mboxファイルは大きくなる一方なので、`iter_raw_messages` は毎回ファイル全体を
  読み込む。今のところ数十MB規模なので問題ないが、将来的に重くなったら
  逆順読み・末尾からの差分読みに変更を検討する
- **日時ソート**: メールのDateヘッダは `+00:00`(Exchange)/`+08:00`(中国側)/
  `+09:00`(日本側)が混在する。`date_iso` 文字列をそのまま辞書順ソートすると
  オフセットの違いで実際の前後関係と逆転することがある(2026-09-01発見・修正)。
  日時で並べる処理は必ず `sort_utils.date_sort_key()` を経由すること
  (生の文字列比較 `key=lambda e: e["date_iso"] or ""` を新しく書かない)
