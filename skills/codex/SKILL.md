---
name: codex
description: Codex CLI（OpenAI）と Gemini CLI（Google）を並列起動してコードレビュー・セカンドオピニオン・調査を取り込むスキル。/codex 単体呼び出しは廃止され、必ず Codex+Gemini の両方を同時に呼ぶ（C+G 並列）。差分レビュー、設計判断の第三者チェック、バグ疑いの検証、長文ログ解析、リサーチに使う。サブスク認証（ChatGPT / Google ログイン）で動作し API キーは不要。「Codex にレビュー」「セカンドオピニオン」「C+G」「cg」などで起動する。
---

# codex — Codex + Gemini 並列レビュアー（C+G）

Claude Code は司令塔、Codex は **コードレビュアー / セカンドオピニオン**、Gemini は **長文解析 / リサーチャー** の役割で同時並列に動かす。
**`/codex` 単体（Codex のみ）の呼び出しは廃止**。このスキルは必ず両方を並列起動する。
（旧 `/gemini` 単体スキルも廃止済み — 単独利用したいケースも本スキル経由で C+G として呼ぶ）

## 前提

- `codex` CLI（`npm i -g @openai/codex`）と `gemini` CLI（`npm i -g @google/gemini-cli`）がインストール済み
- `codex login status` が `Logged in using ChatGPT` を返す
- `gemini` は初回 `/auth` で Google サブスクログイン済み
- API キーは使わない（`OPENAI_API_KEY` / `GEMINI_API_KEY` を環境変数にセットしない）
- read-only 運用：書き込み・コマンド実行はさせない

## 実行手順

### Step 1: AskUserQuestion で対象・Codex レベル・Gemini 観点を一度に確認（必須）

**※ 直前指示に「対象・Codex レベル(low/medium/high)・Gemini 観点」が明示済みなら AskUserQuestion をスキップして Step 2 へ即進む。**

`AskUserQuestion` を 1 回呼び、以下 3 点を同時に聞く。

**Question 1: 対象**
- 現在の差分（`git diff` の内容）
- 特定ディレクトリ（パスを指定）
- 特定ファイル（パスを指定）
- 貼り付けテキスト / 説明文のみ

**Question 2: Codex の reasoning レベル**
- `low` — 軽い確認・スタイルチェック
- `medium` — 通常レビュー（推奨デフォルト）
- `high` — 設計判断・難しいバグ・セキュリティレビュー

**Question 3: Gemini の観点**
- 要約（長文を短くまとめる）
- 原因特定（エラーログから根本原因を推定）
- 参考情報収集（関連仕様・ベストプラクティス・事例）
- 比較 / 評価（複数案の長短整理）

### Step 2: プロンプトを組み立てる

Codex 用と Gemini 用、それぞれ別プロンプトを構築する。

- **共通**: 日本語回答指示。`AGENTS.md` がある場合は「プロジェクトの AGENTS.md に従う」と明示。
- **Codex 向け**: 「以下の差分／ファイル をレビューし、バグ・設計上の懸念・改善点を指摘してください」
  - Codex は read-only サンドボックス内でファイルを絶対パスで読める
- **Gemini 向け**: Step 1 Q3 で選んだ観点に応じた指示（要約／原因特定／調査／比較）
  - 大きなファイルは Claude Code 側で要点抜粋してから渡す（Gemini に丸投げしない）

### Step 3: 並列実行（**1 メッセージで Bash を 2 個同時に出す**）

**重要**: Codex と Gemini は同一メッセージ内に Bash ツールを 2 個並べて **並列起動** する（直列実行は禁止）。

```bash
# Bash #1（Codex）
mkdir -p "C:/tmp-ai" && cd "C:/tmp-ai" && codex exec -c model_reasoning_effort="<low|medium|high>" --sandbox read-only --skip-git-repo-check "<Codex プロンプト>" < /dev/null
```

```bash
# Bash #2（Gemini）
mkdir -p "C:/tmp-ai" && cd "C:/tmp-ai" && gemini --skip-trust -p "<Gemini プロンプト>" < /dev/null
```

| 要素 | 理由 |
|---|---|
| `cd "C:/tmp-ai" &&` | 日本語 CWD で発生する文字化け（Codex の `x-codex-turn-metadata` UTF-8 エンコーディングエラー）と Gemini の自動文脈混入（CWD の `AGENTS.md` / `GEMINI.md` 自動読込）を防ぐ。事前に `mkdir -p C:/tmp-ai` で ASCII 空ディレクトリを作って移動する |
| `< /dev/null` | stdin 明示クローズ。Bash ツールは親プロセスの stdin を引き渡すため、付け忘れると EOF 待ちで永遠にハングする（必須） |
| `--sandbox read-only`（Codex） | 書き込み・コマンド実行を禁止（固定） |
| `--skip-trust`（Gemini） | trusted-folder チェックをスキップ |
| `--skip-git-repo-check`（Codex） | git 外でも動かす |
| `-p`（Gemini） | 非対話モード（1 発プロンプト → 出力して終了） |

### 長いプロンプト・改行を含む場合（一時ファイル経由）

```bash
cat > "C:/tmp-ai/codex_prompt.txt" <<'EOF'
...Codex 向け長文プロンプト...
EOF
cat > "C:/tmp-ai/gemini_prompt.txt" <<'EOF'
...Gemini 向け長文プロンプト...
EOF
```

その後、別メッセージで以下を **並列**（Bash を同一メッセージに 2 個）で実行:

```bash
mkdir -p "C:/tmp-ai" && cd "C:/tmp-ai" && codex exec -c model_reasoning_effort="medium" --sandbox read-only --skip-git-repo-check "$(cat 'C:/tmp-ai/codex_prompt.txt')" < /dev/null
mkdir -p "C:/tmp-ai" && cd "C:/tmp-ai" && gemini --skip-trust -p "$(cat 'C:/tmp-ai/gemini_prompt.txt')" < /dev/null
```

### ファイル・差分を渡す場合（Codex）

Codex は CWD 外のファイルも絶対パスで読める。プロンプトに絶対パスで指示する:

```
以下のファイルをレビューしてください:
C:\Users\jl4lv\OneDrive\デスクトップ\0.フジ\900.ClaudeCode\<path>
```

### Bash ツールのタイムアウト

- Codex: low=180000（3分）/ medium=300000（5分）/ high=600000（10分）
- Gemini: 180000（3分）以上

### Step 4: 統合レビュー表で出力（必須・省略禁止）

両者の生出力をそのまま貼らず、以下の **5 列統合レビュー表** で必ず出力する:

| 指摘 | Codex | Gemini | Claude採用 | 対応 |
|---|---|---|---|---|

ルール:
- 「指摘」列の頭に **重要度絵文字**（🔴 重大 / 🟠 重要 / 🟡 注意）を必ず付ける
- 「Codex」「Gemini」列: その指摘を挙げたかを `✓` / `—` または短評で記入
- 「Claude採用」列: ✅採用 / ⏭️スキップ / 🔄部分採用 のいずれか
- 「対応」列: Claude Code が次に何をするか（ファイル・行・修正方針）

表の前後に以下を添える:
1. **総評** — 両者の評価を踏まえた 1〜3 行のまとめ
2. **次アクション** — Claude Code 側で実装／修正する項目（箇条書き）
3. （任意）原文の抜粋を折りたたみで添付

## 注意事項

- **`/codex` 単体・`/gemini` 単体の呼び出しは廃止**。本スキルは必ず両方を並列起動する
- スキル連鎖禁止: 本スキル内から別スキルを自動呼び出ししない
- Codex / Gemini に **書き込み・コマンド実行をさせない**（read-only / 非対話）
- API キーを環境変数にセットしない
- 巨大対象は Claude Code 側で要点抽出してから渡す
- 機密情報（社内 DB 接続情報・顧客データ）を不必要に渡さない

## トラブルシュート

- Codex `Not logged in` → ユーザーに `codex login` の実行を依頼して停止
- Gemini 認証エラー → 一度 `gemini` を対話起動して `/auth` でログインを案内し、停止
- **無応答・永遠に終わらない** → `< /dev/null` を付け忘れている可能性大。Step 3 のコマンドに必ず付ける
- レート制限（特に Gemini 無料枠） → 時間を空けて再試行
- タイムアウト → Codex は `reasoning=low` に落とす、Gemini はプロンプトを分割
- `unexpected argument '--ephemeral'` 等 → CLI バージョン差異。該当フラグを外して再試行
