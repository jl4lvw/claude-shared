---
name: lv5a
description: cgd Lv5（設計相談→実装→検証→再レビュー→🔴自動修正1周）を Codex 側に任せて回す軽量スキル。「Lv5A」「レベル5A」「設計から実装まで Codex に任せて」で使う。Claude は依頼文を書く→承認→consult→質問の取り次ぎ→implement→🔴だけ突合→最終質問の取り次ぎ、だけ。cgd 全文（約 76K トークン）を読み込まない分離版
---
<!-- SKILL_VERSION: 2026-09-20_150111 -->

# lv5a — 設計相談から実装・再レビューまで Codex に任せる Lv5（Claude のトークンを最小にする）

**目的**: Lv5 で Claude が消費していた「設計案づくり・レビュー起動・実装委譲・検証・再レビュー・🔴 の自動修正」を `cgd_lv5a.py` に移す。
実体は `/lv3a`（2社×2視点レビュー）と `/lv0`（Codex 実装+機械検査）の 2 つのドライバを **CLI とファイルだけ**で束ねる調整役。
**ユーザーへの確認（方向性・最終受け入れ）は、ドライバが終了コード 20 / 21 で止まり、Claude が質問を取り次ぐ**。

## 設計の要点
- 流れ: `consult`（Codex が設計案を `docs/lv5a_design_<label>.md` に書く → Lv3A がレビュー）→ **G1（方向性の質問）** → `implement`（実装+機械検査 → 差分レビュー → 🔴 自動修正は最大 1 周）→ **G2（最終質問）**
- **`implement` は G1 が「この設計で実装する」のときだけ動く**（機械が強制。Claude が勝手に進めない）
- 自動修正の対象は「technical の 🔴 で採用/部分採用」だけ。2 周目は回さない。再レビューに 🔴 が残る/再発したら止めて exit 21

## 使う / 使わない
| 使う | 使わない |
|---|---|
| 新規モジュール・複数ファイルの実装で、設計の確認→実装→レビューまで Claude を経由させたくない | 変更が約 200 行未満（Claude が直接直す）・設計判断が重い（DB 設計・セキュリティ）→ 通常の `/cgd` |
| 作業フォルダを 1 つに絞れる。Codex の週枠に余裕（80% 未満） | 本番 DB・外部 API への書込（サンドボックスはネット無し） |

## 手順
### 1. 準備（Claude・Write ツール）
- **作業フォルダ**: git リポジトリの内側のサブフォルダ（最上位は不可）。`.hq/board` で同じフォルダを触る他セッションが無いか確認
- **検査定義 JSON**: **作業フォルダの外**（`C:/tmp-ai/lv5a_checks_<名前>.json`。書式は `/lv0` の `checks.example.json`）。`frozen` のテスト等は変更させない
- **依頼文** `C:/tmp-ai/lv5a_brief_<名前>.md`: 統合者は会話を知らないので根拠にしてほしい事実をすべて書く。**`## 確認済みの事実` の見出しは必須**。設計は Codex が書くので、依頼文は「何を・なぜ・受け入れ条件」

### 2. 承認（Claude → ユーザー）
```bash
python "C:/ClaudeCode/.claude/tools/cgd_lv5a.py" plan --brief "<依頼文>" --workdir "<作業フォルダ>" --checks "<検査定義>" [--files "<参考ファイル…>"] [--no-ds]
```
出力（Lv0 の点検＝**秘密情報らしいファイル**・検査コマンド・凍結、Lv3A の点検＝送信先と週枠、流れ）を表にして **AskUserQuestion で承認**。**承認前に何も実行しない**。
承認に含める: 送信先（Codex=OpenAI／DeepSeek=中国本土）・作業フォルダ・**巻き戻せる範囲**（Lv0 の写し=2MB 以下の通常ファイルだけ）・作業フォルダに設計ファイルが残ること。exit 1/2/11 なら中止。**`--redact` は使えない**（指定すると拒否される。設計段階の Codex に依頼文がそのまま渡り、伏字が効かないため）。plan が秘匿情報候補で止まったら、該当行を依頼文・参考ファイルから外してやり直す（伏字してレビューだけ受けたいなら `/lv3a`。実装はしない）。

### 3. consult（バックグラウンド・待つだけ）
```bash
python "C:/ClaudeCode/.claude/tools/cgd_lv5a.py" consult --brief … --workdir … --checks … --label "<名前>" [--files …] [--effort medium|high] [--no-ds]
```
`run_in_background: true` で起動し終了通知を待つ（実測 4 分）。途中で見に行かない。同じ label の設計ファイルが作業フォルダにあると止まる（label を変える）。**exit 20 = 正常**（回答待ち）。標準出力が `consult_report.md`（60 行以内）。

### 4. 質問の取り次ぎ（Claude → ユーザー）
`consult_report.md` を読み、run ディレクトリ（`C:/tmp-ai/cgd_lv5a/<label>_<時刻>_<乱数>/`）の `questions.json` を **言い換えずに** AskUserQuestion にする（推奨に「(Recommended)」）。**G1 を必ず含める**。
`reason_ok` が false の質問は推奨に「(根拠なし)」と添える。レポート全文は転記しない（総評・設計の要点・🔴 を 10〜20 行で伝え、パスを添える）。
**レポートに「Lv3A は暫定成功（exit 20）: 欠落 …」があれば、欠けた担当（その視点が出ていない）を質問と一緒にユーザーへ必ず伝える**（consult は暫定でも exit 20 なので見落としやすい）。推奨が依頼文で決めた内容と食い違って見える質問は、依頼文のどの記述と食い違うかを添えて取り次ぐ。AskUserQuestion の header には質問の id（Q1 など）を使う（1 回 4 問まで）。

### 5. 回答ファイル（Claude・Write ツール）
`<run>/answers.json`: `{"answers": {"G1": "<label>", "Q1": "<label>"}, "notes": "任意の補足"}`。**label は questions.json の文字列と完全一致**（言い換え禁止。AskUserQuestion で付けた「(Recommended)」は表示用なので回答に含めない）。G1 が「この設計で実装する」以外なら implement は拒否される（設計を直す→依頼文を直して**新しい label** で consult）。

### 6. implement（バックグラウンド・待つだけ）
```bash
python "C:/ClaudeCode/.claude/tools/cgd_lv5a.py" implement --run "<run ディレクトリ>" --answers "<answers.json>" [--effort medium|high] [--max-fix-rounds 2] [--no-ds]
```
`run_in_background: true`（実測 7 分）。回答は設計より優先される（食い違う点は Codex が回答に合わせて実装し設計書も更新する）。**依頼文と矛盾する回答だと Codex が質問を返して停止する（exit 3・質問は stderr と `logs/`）**。exit 0 = 🔴 なし／**21 = 完了したが要ユーザー判断**（🔴 が残る・レビュー未実施・自動修正が止まった）。標準出力が `final_report.md`。`--max-fix-rounds` は Lv0 の「検査に落ちたときの出し直し」の回数で、🔴 の自動修正（最大 1 周）とは別。final_report に「差分は自動レビューしていない」とあれば（実装差分に秘匿情報の候補があって Lv3A が止まった場合など）、差分は run ディレクトリの `impl_overall.patch`。内容を確認し、必要なら `/lv3a` で個別にレビューする（伏字が要るなら `--redact` 付き）。

### 7. 🔴 の突合と最終質問（Claude → ユーザー）
- **🔴 の突き合わせ（ユーザー決定: Claude の関与は「🔴 だけ原文突合」）**: レポートに 🔴 があれば、レビューの run（`state.json` の `result.review2`/`review1` の `run_dir`）の `report.md`「🔴 の詳細」の各見出しを、生ログ（`codex_tech.md` 等）で grep し、題名・対応案と食い違わないかだけ確かめる。**全体の読み直しはしない**
- `questions_final.json`（G2）を **言い換えずに** AskUserQuestion にする。回答は「受け入れる（反映は別手順）」なら反映（コミット等）は別手順、「破棄して元に戻す」なら Lv0 の写しから `cgd_lv0_codex.py restore`（確認のみが既定）
- 費用（Codex 呼出回数・tokens／DeepSeek ¥）はレポートの数字をそのまま報告

## 終了コード
| exit | 意味 | やること |
|---|---|---|
| 0 | implement 完了・🔴 なし | final_report を読み G2 へ |
| 1 | 前段・引数・状態違反（欄なし・秘匿候補・phase 違反・G1 未承認・入力が consult と違う） | メッセージに従う |
| 2 | 使える Codex 実行ファイルが無い | `python C:/ClaudeCode/.claude/tools/cgd_lv0_codex.py resolve` |
| 3 | Codex 失敗・変更なし（設計ファイルが作られない含む。Codex の質問は stderr に出る） | 質問の答えを依頼文の「確認済みの事実」へ足し、新しい label で consult |
| 10 | 実装の検査が通らない・停滞 | 失敗の抜粋が小さければ Claude が直し `/lv0` の `check` で再検査。大きければ降格 |
| 11 | 週枠 80% 以上 | 枠が戻るまで待つ／降格 |
| 12 | Lv3A のレビュー失敗で consult 不成立 | run.json を見て 1 回だけ新しい label で再 consult。続くなら降格 |
| **20** | **consult 完了・ユーザーの回答待ち（異常ではない）** | 質問を取り次ぐ |
| **21** | **implement 完了・要ユーザー判断** | final_report と 🔴 突合 → G2 |
| 30 | タイムアウト | `status --run` で確認し再実行／降格 |

## 降格
**Lv0 手動手順**（`/lv0`、`cgd/reference/lv0_manual_steps.md`）か、**通常の `/cgd` Lv5**（Claude が進行）へ。設計ファイル `docs/lv5a_design_<label>.md` と Lv3A の生ログは残っているので、入力は作り直さなくてよい。`implement_failed` の run は再開できない（新しい consult）。

## ガードレール（機械が守る）
- G1 が「この設計で実装する」でない・phase が `awaiting_direction` でない・検査定義/設計ファイルが consult 後に変わっている → implement は動かない（exit 1）
- 自動修正は最大 1 周（構造的に 2 周目を呼べない）。実装差分の秘匿候補は**自動で伏字にしない**（レビュー未実施として記録し exit 21）。`--redact` は受け付けない（設計段階の Codex に効かないため）
- 子プロセスには許可リストの環境変数だけ（鍵・トークン類は通さない。`DEEPSEEK_`/`CODEX_`/`CGD_` の接頭辞のみ許可）。サブプロセスは打ち切りで exit 30
- 削除しない（run ディレクトリ・設計ファイル・patch は残る）。費用は失敗した試行も合算し、各工程の終了コードを `state.json` に記録
- 統合結果は提案。**最終判断はユーザー（Claude ではない）**

## 実測
玩具（slugify・2026-09-20）: consult 4 分（Codex 4 回 約 4.4 万 tok・DS ¥0.6〜0.7）／implement 7 分（🔴 の自動修正 1 周を含め、全体で Codex 12 回 約 14.9 万 tok・DS ¥1.32）。
2 回目は回答が設計と食い違い Codex が質問して停止（exit 3）。制約に「回答は設計より優先」を足し、同じ入力を Lv0 で再実行すると実装できた。Claude 側の消費は未集計。
Claude 側の削減は**見積りが外れやすい**（Lv0 は見積り約 3 割に対し実測約 1 割）。使った回ごとに、final_report の費用と Claude 側の往復数を記録して判断材料にする。
