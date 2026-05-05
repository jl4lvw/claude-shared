---
name: cgd
description: Codex+Gemini+DeepSeek の3者を使った設計・コード相談スキル。差分レビュー、設計判断、別案出し、難しい実装方針の検討に使う。`/cgd` 起動時に **1=軽量モード（C+G 並列、所要1〜2分・現行 /codex と同等）** か **2=フルモード（Gemini案出し→Claude検討→DS別案→Claude再検討→Codexレビュー→Claude最終まとめ の6段直列パイプライン、所要5〜8分）** を必ず選ばせる。**相談結果が出た後は実装・検証まで一気通貫で実行可能**。再レビュー（Codex 単独）と自動修正ループ（最大1周）はユーザーがオプトインしたときのみ実行（既定はトークン節約のため省略）。「3者に相談」「DSにも別案」「フルパイプ」「cgd」「フル相談」などのキーワードで起動。重要な設計判断・難しいバグ・大きめのリファクタの検討時には積極的にこのスキルを提案すること（軽量で済むなら本スキルの軽量モードか既存 /codex を使う）。既存 /generate-by-deepseek（DSコード生成→Claudeレビュー）とは目的が異なる（cgd は「相談・別案＋実装・検証（任意で再レビュー）」、後者は「実装代行」）。
---

# cgd — Codex + Gemini + DeepSeek 統合相談スキル

Claude Code は司令塔。Codex / Gemini / DeepSeek を **役割分担** で使い分ける:

| エージェント | 役割 | 呼び出し |
|---|---|---|
| Gemini | 案出し・調査・長文要約 | `gemini -p "..."` |
| DeepSeek (advisor) | 別案・代替アプローチ提示 | `python "<絶対パス>/deepseek_coder.py" --role advisor "..."` |
| Codex | 最終レビュー・厳密な品質ゲート | `codex exec ...` |
| Claude Code | 各ステップの統合判断・最終まとめ | （本体） |

**既存スキルとの棲み分け**:
- `/codex` (C+G並列) — 軽い差分レビュー専用。本スキルの軽量モードと等価
- `/generate-by-deepseek` — DS にコード生成させて Claude がレビュー（実装代行）
- `/cgd` — 設計相談・別案出し（実装はしない、方針を決める）

## 前提

- **シェル: Bash 必須**（Git Bash または WSL）。PowerShell では `< /dev/null` `cat <<EOF` `$(...)` が解釈されないため不可
- `codex` CLI（ChatGPT サブスクログイン済み）
- `gemini` CLI（Google サブスクログイン済み）
- `DEEPSEEK_API_KEY` 環境変数が設定済み
- すべて read-only 運用：書き込み・実行は Claude Code 本体が行う
- **実装フェーズに入ったら AGENTS.md / CLAUDE.md ルール（バックアップ必須・shebang禁止・`encoding="utf-8"` 明示）を強制適用する**

## Step 1: モード選択（必須・1/2 で答えさせる）

**直前指示でモードが明示済み（「フルで」「軽量で」等）ならスキップして Step 2 へ。**

`AskUserQuestion` でモードを 1 つ聞く（**所要時間と概算コストを必ず併記**）:

```
Q: 相談モード
1. 軽量 — Codex + Gemini 並列レビュー（所要 1〜2分・概算 ¥0〜数円）
2. フル — Gemini→Claude→DS→Claude→Codex→Claude の6段直列（所要 5〜8分・概算 ¥5〜20）
```

選択後、**対象**（差分／ファイル／貼り付けテキスト）と、フル時は **検討テーマ**（設計判断／バグ原因／実装方針／リファクタ案 など）も同時に確認する。

---

## モード 1: 軽量（C+G 並列）

本スキル内に自前で実装する（他スキル参照せず、解釈ブレを防ぐ）。

### Step 2L-A: Codex level と Gemini 観点を確認

`AskUserQuestion` で同時に聞く:
- Codex reasoning: low / medium（推奨） / high
- Gemini 観点: 要約 / 原因特定 / 参考情報収集 / 比較評価

### Step 2L-B: 並列起動（**1 メッセージで Bash 2 個**）

```bash
# Bash #1（Codex）
mkdir -p "C:/tmp-ai" && cd "C:/tmp-ai" && codex exec -c model_reasoning_effort="<low|medium|high>" --sandbox read-only --skip-git-repo-check "<Codex プロンプト>" < /dev/null

# Bash #2（Gemini）
mkdir -p "C:/tmp-ai" && cd "C:/tmp-ai" && gemini --skip-trust -p "<Gemini プロンプト>" < /dev/null
```

`cd "C:/tmp-ai"` は日本語 CWD 文字化け＆Gemini の AGENTS.md 自動読込回避。`< /dev/null` はハング防止（必須）。

### Step 2L-C: 5列統合表で出力

| 指摘（重大度＋根拠1行） | Codex | Gemini | Claude採用 | 対応 |
|---|---|---|---|---|

- 重大度: 🔴 重大 / 🟠 重要 / 🟡 注意
- 「指摘」列に **根拠1行を内包**（横長を避ける）
- Claude採用: ✅採用 / ⏭️スキップ / 🔄部分採用
- 表の前後に **総評（1〜3行）** と **次アクション（箇条書き）** を必ず添える

**完了後、共通フロー Step 3 へ進む**（実装→検証。再レビューは任意）。

---

## モード 2: フル（6段パイプライン）

### Step 2F-A: Gemini に案出し依頼

```bash
mkdir -p "C:/tmp-ai" && cd "C:/tmp-ai" && gemini --skip-trust -p "<案出しプロンプト>" < /dev/null
```

**プロンプト雛形**:
```
以下の[テーマ]について、複数の実装案・設計案を提示してください。
案ごとに「概要・メリット・デメリット・実装難度」を簡潔に整理してください。
日本語で回答。AGENTS.md がある場合はそれに従う。

[対象]
<差分／ファイル内容／説明文>
```

### Step 2F-B: Claude が Gemini 案を検討

Claude Code 自身が Gemini 出力を読み、以下を整理して **ユーザー向けに表示**:

- Gemini が提示した案の一覧（番号付き）
- 各案について Claude が見た **追加の懸念点・改善余地**
- Claude の現時点での **第1推し案**

### Step 2F-C: DS に別案依頼（advisor モード）

**DS 入力ルール（重要）**:
- 原文/差分は **絶対パス** で参照させる（コピペで肥大化させない）
- Gemini と Claude の検討結果は **要約版**（各 500 字以内）を渡す
- DS への総入力は **2KB 以下** を目安にする（タイムアウト・品質低下を回避）
- Claude Code の **絶対パスで** Python スクリプトを呼ぶ（CWD は `C:/tmp-ai` のため相対パス不可）

```bash
cat > "C:/tmp-ai/ds_prompt.txt" <<'EOF'
以下の設計テーマについて、Gemini と Claude が検討した結果を踏まえ、
**根本的に別アプローチ**を 1〜3 個提示してください。
既出案の改良ではなく、別の発想を求めます。

[原文/差分パス]
<絶対パス>

[Gemini の案要約（500字以内）]
<要約>

[Claude の検討要約（500字以内）]
<要約>
EOF

python "C:/Users/jl4lv/OneDrive/デスクトップ/0.フジ/900.ClaudeCode/.claude/tools/deepseek_coder.py" --role advisor "C:/tmp-ai/ds_prompt.txt"
```

DS は `ROLE_PROMPTS["advisor"]` の構造（別案 / 見落とし / 採否コメント）で返す。

**DS 呼び出し後の使用量表示（必須・転記）**:
- スクリプトが stderr に `[DS Usage] 今回: ... / 累計: ...` を 2 行出力する（今回トークン・累計・¥/$ 換算）
- Claude は実行結果に含まれるこの 2 行を **そのまま Step 2F-D の手前でユーザー向けに表示** すること（料金可視化目的・省略禁止）
- セッション累計は `.claude/tools/.deepseek_usage_session.json` に atomic write で保存。最終呼び出しから 4 時間で自動リセット。手動リセットは `--reset-session`
- 円換算レートは既定 1USD=150JPY。環境変数 `DEEPSEEK_USD_TO_JPY` で上書き可能

### Step 2F-D: Claude が再検討

Gemini 案 + DS 別案を統合し、**ユーザー向けに表示**:

| # | 出所 | 案概要 | Claude評価 |
|---|---|---|---|
| 1 | Gemini | ... | ... |
| 2 | DS別案 | ... | ... |

その上で **Claude の統合推し案**（Gemini と DS のハイブリッドも可）を 1 つ決める。

### Step 2F-E: Codex に最終レビュー依頼

統合推し案を Codex にレビューさせる。reasoning level は原則 **medium**、設計判断が重い場合は **high**。

```bash
cat > "C:/tmp-ai/codex_prompt.txt" <<'EOF'
以下の実装方針を、バグ・設計上の懸念・セキュリティ観点・改善点で厳密にレビューしてください。
日本語回答。プロジェクトの AGENTS.md に従う。

[統合推し案]
<Step 2F-D の結論>

[対象ファイル（参考・絶対パス）]
<絶対パス>
EOF

mkdir -p "C:/tmp-ai" && cd "C:/tmp-ai" && codex exec -c model_reasoning_effort="medium" --sandbox read-only --skip-git-repo-check "$(cat 'C:/tmp-ai/codex_prompt.txt')" < /dev/null
```

### Step 2F-F: Claude 最終まとめ（必須出力フォーマット）

**6列統合表**（指摘列に根拠1行を内包）:

| # | 指摘/論点（🔴/🟠/🟡＋根拠1行） | Gemini | DS | Codex | Claude最終判断 |
|---|---|---|---|---|---|
| 1 | 🔴 ... — 根拠 | ✓/—/短評 | ✓/—/短評 | ✓/—/短評 | ✅採用 / ⏭️スキップ / 🔄部分採用 |

表の前後に必ず添える:

1. **採用方針** — 最終的にどの案で進めるか（1〜3行）
2. **次アクション** — Claude Code が実装する項目（箇条書き、ファイル・行・修正方針）
3. （任意）各エージェントの原文抜粋を折りたたみで添付

**最終まとめをログ保存**:
```bash
cp <最終まとめ.md> "C:/tmp-ai/cgd_$(date +%Y%m%d_%H%M%S).md"
```

**完了後、共通フロー Step 3 へ進む**（実装→検証。再レビューは任意）。

---

## Step 3〜7: 共通実装フロー（実装→検証。再レビューは任意）

軽量モードの Step 2L-C / フルモードの Step 2F-F で「次アクション」が確定したら、以下の共通フローへ進む。
**このフローは軽量・フル両モード共通**。

**設計方針（トークン節約）**:
- 既定は **実装 + 検証のみ** で完了（Step 6 / 6.5 はスキップ）
- 再レビュー（Step 6, Codex 単独）と自動修正ループ（Step 6.5, 最大 1 周）は **ユーザーが Step 3 でオプトインしたときのみ**実行
- 大物変更・本番影響あり・PR 直前など慎重に進めたい時に「1（再レビューあり）」を選ぶ運用

### Step 3: 実装許可確認（条件付きスキップ）

**直前のユーザー指示にモード明示があればスキップ**:
- 「実装まで一気に」「再レビューあり実装」「フル実装」 → 選択 `1` 相当でスキップ
- 「実装＋検証のみ」「軽実装」「再レビューなしで実装」「impl まで」 → 選択 `2` 相当でスキップ
- 「実装せず終了」「相談だけ」 → 選択 `3` 相当でスキップ

それ以外は `AskUserQuestion` で実装方針を 1 回聞く（数字 1 文字で答えさせる運用に合わせる）:

```
Q: 実装に進む？
1. 実装＋検証＋再レビュー（Codex 単独・🔴 自動修正最大1周）— 重要変更・PR 直前向け
2. 実装＋検証のみ（再レビュー省略）— 既定推奨・最頻ユース
3. 実装せず終了（相談のみ）
4. 修正して実装（次アクションをユーザー指示で差し替えてから 1 か 2 を再選択）
```

| 選択 | 流れ |
|---|---|
| 1 | Step 4 → Step 5 → Step 6 → Step 6.5 → Step 7 |
| 2 | Step 4 → Step 5 → Step 7（**既定推奨**） |
| 3 | Step 7（簡易まとめ）だけ出して終了 |
| 4 | 次アクションを差し替えた上で Step 3 を再表示 |

### Step 4: 実装フェーズ

AGENTS.md / CLAUDE.md ルールを **強制適用** する:

- **既存ファイル編集前に必ず `cp <file> <file>.bak_$(date +%Y%m%d_%H%M%S)` でバックアップ**（バックアップなしの編集禁止）
- シバン行（`#!/usr/bin/env python3`）禁止（Windows で `py.exe` が即終了する）
- ファイル読み書きは常に `encoding="utf-8"` 明示
- CP932 コンソール対策: Python ワンライナー先頭に `sys.stdout.reconfigure(encoding="utf-8")`
- 日本語パスへの Edit / Write が失敗する場合は **Python スクリプトを `C:/Users/jl4lv/` に書いて実行**（`open(..., encoding='utf-8')`）
- バッチファイル（.bat）は **CP932** で書く（Write ツールは UTF-8 で書くため文字化けする）
- API キー / シークレットを書き込まない・コミットしない

実装は Step 2L-C / 2F-F で出した「次アクション」の項目順に進める。
TodoWrite で進捗を管理する（項目数が 3 個以上の場合は必須）。

### Step 5: 検証フェーズ（CLAUDE.md「コーディング後の必須検証」を強制）

実装ファイルごとに以下 4 項目を確認し、**結果を表形式で必ず報告**:

| # | 検証項目 | 結果 |
|---|---|---|
| 1 | 対象ファイルの import 一覧（先頭 20 行）を確認 | ✅/⚠️ |
| 2 | 実 import で動作確認（`python -c "import sys; sys.path.insert(0, r'PATH'); from <mod> import <fn>; print('OK')"`） | ✅/❌ |
| 3 | パス定数が実在するか（`python -c "from pathlib import Path; p=Path(r'...'); print(p, p.exists())"`） | ✅/❌ |
| 4 | `ast.parse` だけで済ませていない（実 import まで実行した） | ✅ |

**`ast.parse` だけの確認は NG**（NameError / ImportError を検出できないため）。必ず実 import まで実行する。

検証 NG（❌）が 1 つでも出たら、**Step 4 に戻って修正**してから Step 5 をやり直す（このループは Step 6.5 とは別系統で、検証通過まで回す）。

Python 以外（JS / TS / シェル等）の場合は、その言語の実行可能な最小確認（`node --check`、`tsc --noEmit`、`bash -n` 等）に置き換える。

### Step 6: 再レビュー（Codex 単独）— Step 3 で `1` を選んだ場合のみ

**Step 3 で `2`（再レビュー省略）を選んだ場合はこのステップを丸ごとスキップして Step 7 へ進む。**

実装した差分を **Codex 単独** でレビューする。Gemini は呼ばない（トークン節約）。

- 対象: 直近の `git diff`（または変更ファイル一覧の絶対パス）
- reasoning: 原則 **medium**（設計判断が重い／セキュリティに関わる場合は **high**）
- 観点: バグ・設計上の懸念・副作用・既存仕様との整合性

```bash
mkdir -p "C:/tmp-ai" && cd "C:/tmp-ai" && codex exec -c model_reasoning_effort="medium" --sandbox read-only --skip-git-repo-check "<差分レビュープロンプト>" < /dev/null
```

差分が大きい場合は `git diff > C:/tmp-ai/impl_diff.patch` してから絶対パスでプロンプトに貼る。

結果は **3 列レビュー表**（軽量 5 列表から Gemini 列を省略）で出力する:

| 指摘（🔴/🟠/🟡＋根拠1行） | Codex | Claude採用 | 対応 |
|---|---|---|---|

### Step 6.5: 重大指摘の自動修正ループ（最大 1 周）— Step 3 で `1` を選んだ場合のみ

Step 6 の表で **🔴 重大指摘** が 1 つ以上 ✅採用 になった場合:

1. 該当箇所を Step 4 と同じ要領で修正（**バックアップ必須**）
2. Step 5（検証）を再実行
3. Step 6（Codex 単独再レビュー）を再実行

**自動で回す上限は 1 周まで**。2 周目に入る前に必ず一旦止め、ユーザーに状況報告して手動判断を仰ぐ（無限ループ防止・トークン浪費防止）。

🟠 重要 / 🟡 注意 のみの場合は自動ループせず、Step 7 のまとめに「未対応指摘」として記載する（対応するかはユーザー判断）。

### Step 7: 最終まとめ

以下を 1 つの報告にまとめる:

1. **実装した内容** — 変更ファイル（絶対パス）と主要な変更点を箇条書き
2. **検証結果** — Step 5 の表
3. **再レビュー指摘と対応状況** — Step 6 の 3 列レビュー表 + 修正ループ周回数（0 / 1 / 中断）。Step 6 をスキップした場合は「再レビュー: 省略（Step 3 で 2 を選択）」と明記
4. **未対応指摘**（あれば） — 🟠 / 🟡 で残ったもの
5. **残課題・申し送り事項**（あれば）

**最終報告をログ保存**:
```bash
cp <最終報告.md> "C:/tmp-ai/cgd_impl_$(date +%Y%m%d_%H%M%S).md"
```

---

## Bash タイムアウト

| ステップ | コマンド | timeout |
|---|---|---|
| 2L-B / 2F-A | gemini | 180000 (3分) |
| 2F-C | deepseek (advisor) | 120000 (2分) |
| 2L-B / 2F-E / 6 | codex medium | 300000 (5分) |
| 2L-B / 2F-E / 6 | codex high | 600000 (10分) |
| 5 | python -c "from <mod> ..." | 60000 (1分) |

## 認証エラー検出時の挙動（必須）

Codex CLI / Gemini CLI / DeepSeek API の **いずれか一つでも認証エラーを返したら、そこで即中断**する。残り段を片肺で続行しない・自動で別ツールに切り替えない。

**検出シグナル（例）**:
- Codex: `Not logged in` / `401` / `unauthorized`
- Gemini: `/auth` 要求 / `401` / `permission denied`
- DeepSeek: `openai.AuthenticationError` / `401 Unauthorized` / `invalid api key` / `DEEPSEEK_API_KEY が設定されていません`

**中断時にユーザーへ報告する内容**（1〜3 行）:
1. どの API で何のエラーが出たか
2. 復旧手順（`codex login` / `gemini` 対話起動の `/auth` / `DEEPSEEK_API_KEY` の設定確認）
3. 復旧後に `/cgd` を再実行する旨

**特にやってはいけないこと**:
- 軽量モード（C+G 並列）で片方が認証エラーのまま、もう片方の出力だけで 5 列統合表を作る
- フルモード（6段直列）で途中段の認証エラーを無視して中間結果を「結論」として扱う
- 別の API（例: 認証成功している側）で代用して続行する

## 注意事項

- **直列実行**: フルモードは順序が重要（前段の出力を後段に渡す）。並列起動はしない
- **軽量モードは並列**: モード 1 では Codex / Gemini を 1 メッセージ内 Bash 2 個で並列起動
- **再レビュー（Step 6）は Codex 単独**: トークン節約のため Gemini は呼ばない（軽量モードの相談段は C+G 並列だが、再レビューは Codex のみ）
- **再レビューはオプトイン**: Step 3 で `1` を選んだときのみ Step 6 / 6.5 が走る。既定（`2`）はスキップ
- **スキル連鎖禁止**: 本スキル内から `/codex` や別スキルを自動呼び出ししない（Step 6 は本スキル内に直接埋め込んだ Codex 単独呼び出し）
- **相談段階は read-only**: Step 2L-B / 2F-A / 2F-C / 2F-E / 6 では Codex `--sandbox read-only`、Gemini `-p` 非対話、DS は API 単発呼び出し
- **書き込みフェーズは Step 4 のみ**: ファイル編集・新規作成は **Step 4 に集約**。Codex / Gemini / DS には絶対に書き込ませない
- **API キー**: DS は `DEEPSEEK_API_KEY` を読む。Codex/Gemini はサブスク認証
- **機密情報**: 顧客データ・社内 DB 接続情報を不必要に外部 API（DS）に渡さない
- **DS パスは絶対パス**: 相対 `.claude/tools/...` は CWD=`C:/tmp-ai` で解決失敗するので必ず絶対パス
- **実装フェーズの規約強制**: Step 4 では AGENTS.md / CLAUDE.md ルール（`.bak_YYYYMMDD_HHMMSS` バックアップ・shebang 禁止・`encoding="utf-8"` 明示・日本語パスは Python スクリプト経由・.bat は CP932）を必ず守る
- **検証フェーズの省略禁止**: Step 5 で `ast.parse` だけで済ませず、必ず実 import まで実行する
- **修正ループ上限**: Step 6.5 の自動修正は **最大 1 周**。2 周目に入る前に必ずユーザーに状況報告（無限ループ＆トークン浪費の防止）

## トラブルシュート

- **DS が `DEEPSEEK_API_KEY が設定されていません`** → 環境変数を確認
- **DS のレスポンスがコード生成っぽい** → `--role advisor` 付け忘れ。デフォルトは `coder`
- **DS が「ファイルが見つかりません」** → 相対パス指定の罠。絶対パスで指定し直す
- **Codex/Gemini の認証エラー** → `codex login` / `gemini` 対話起動 `/auth`
- **無応答ハング** → `< /dev/null` 付け忘れ
- **日本語 CWD 文字化け** → `cd "C:/tmp-ai"` 忘れ
- **PowerShell でエラー** → 本スキルは Bash 必須。Git Bash で起動し直す

## いつ軽量 / いつフルか

| シーン | 推奨モード |
|---|---|
| 差分レビュー、PR 直前チェック | 軽量 |
| 既知パターンの実装方針確認 | 軽量 |
| 重要な設計判断（DB 設計、API 設計、状態管理方式） | フル |
| 難しいバグの原因仮説出し（症状から複数候補が必要） | フル |
| 大規模リファクタの方針決め | フル |
| 新規機能のアーキテクチャ案出し | フル |
