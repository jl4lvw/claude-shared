---
name: cgd
description: Codex+Gemini+DeepSeek の3者を使った設計・コード相談スキル。差分レビュー、設計判断、別案出し、難しい実装方針の検討に使う。`/cgd` 起動時に **1=軽量モード（C+G 並列、所要1〜2分・現行 /codex と同等）** か **2=フルモード（Gemini案出し→Claude検討→DS別案→Claude再検討→Codexレビュー→Claude最終まとめ の6段直列パイプライン、所要5〜8分）** を必ず選ばせる。「3者に相談」「DSにも別案」「フルパイプ」「cgd」「フル相談」などのキーワードで起動。重要な設計判断・難しいバグ・大きめのリファクタの検討時には積極的にこのスキルを提案すること（軽量で済むなら本スキルの軽量モードか既存 /codex を使う）。既存 /generate-by-deepseek（DSコード生成→Claudeレビュー）とは目的が異なる（cgd は「相談・別案」、後者は「実装代行」）。
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

---

## Bash タイムアウト

| ステップ | コマンド | timeout |
|---|---|---|
| 2L-B / 2F-A | gemini | 180000 (3分) |
| 2F-C | deepseek (advisor) | 120000 (2分) |
| 2L-B / 2F-E | codex medium | 300000 (5分) |
| 2L-B / 2F-E | codex high | 600000 (10分) |

## 注意事項

- **直列実行**: フルモードは順序が重要（前段の出力を後段に渡す）。並列起動はしない
- **軽量モードは並列**: モード 1 では Codex / Gemini を 1 メッセージ内 Bash 2 個で並列起動
- **スキル連鎖禁止**: 本スキル内から `/codex` や別スキルを自動呼び出ししない
- **read-only**: Codex は `--sandbox read-only`、Gemini は `-p` 非対話、DS は API 単発呼び出し
- **API キー**: DS は `DEEPSEEK_API_KEY` を読む。Codex/Gemini はサブスク認証
- **機密情報**: 顧客データ・社内 DB 接続情報を不必要に外部 API（DS）に渡さない
- **DS パスは絶対パス**: 相対 `.claude/tools/...` は CWD=`C:/tmp-ai` で解決失敗するので必ず絶対パス
- **実装フェーズ**: 本スキルで方針が決まった後に実装に入る場合、AGENTS.md / CLAUDE.md ルール（`.bak_YYYYMMDD_HHMMSS` バックアップ・shebang禁止・`encoding="utf-8"`）を必ず守る

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
