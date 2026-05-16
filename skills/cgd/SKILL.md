---
name: cgd
description: Codex+Gemini+DeepSeek の3者を統合したコードレビュー・設計相談・実装・検証スキル。**5段階レベル（Lv1〜Lv5）**でユーザーがトークン消費・所要時間を選択。Lv1=Codex単独 / Lv2=Codex+Gemini並列（既定推奨・旧/codex等価） / Lv3=Lv2+実装後Codex再レビュー / Lv4=Gemini→DS→Codex直列フル相談+再レビュー / Lv5=Lv4+🔴重大指摘の自動修正1周。差分レビュー、設計判断、別案出し、実装、検証まで一気通貫。**旧 `/codex` `/gemini` 単体スキルは廃止され、本スキル（`/cgd` または `/codex` 起動）が必ずレベル選択から始まる**。「3者に相談」「DSにも別案」「フルパイプ」「cgd」「Codexにレビュー」「セカンドオピニオン」「C+G」「cg」などのキーワードで起動。重要な設計判断・難しいバグ・大きめのリファクタの検討時には積極的に提案すること。既存 /generate-by-deepseek（DSコード生成→Claudeレビュー）とは目的が異なる。
---

# cgd — Codex + Gemini + DeepSeek 統合スキル（Lv1〜5）

Claude Code は司令塔。Codex / Gemini / DeepSeek を **役割分担** で使い分ける:

| エージェント | 役割 | 呼び出し |
|---|---|---|
| Codex | コードレビュー・厳密な品質ゲート | `codex exec ...` |
| Gemini | 長文解析・調査・案出し | `gemini -p "..."` |
| DeepSeek (advisor) | 別案・代替アプローチ提示（Lv4-5 のみ） | `python "<絶対パス>/deepseek_coder.py" --role advisor "..."` |
| Claude Code | 統合判断・実装・検証 | （本体） |

## 旧スキルとの関係

- **旧 `/codex` 単体・`/gemini` 単体スキルは廃止**
- `/codex` 起動でも本スキル `/cgd` と同じフローが動く（必ず Step 1 のレベル選択を求める・後方互換なし）
- 旧 /codex の挙動（C+G 並列レビュー）が欲しい場合は **Lv2** を選ぶ
- `/generate-by-deepseek`（DS コード生成→Claude レビュー）とは目的が異なる（実装代行）

## 前提

- **Bash 必須**（Git Bash または WSL）。PowerShell では `< /dev/null` `cat <<EOF` `$(...)` が解釈されない
- `codex` CLI（`npm i -g @openai/codex`）と `gemini` CLI（`npm i -g @google/gemini-cli`）がインストール済み
- `codex login status` が `Logged in using ChatGPT` を返す
- `gemini` は初回 `/auth` で Google サブスクログイン済み
- API キーは使わない（`OPENAI_API_KEY` / `GEMINI_API_KEY` を環境変数にセットしない）
- Lv4-5 では `DEEPSEEK_API_KEY` 環境変数が必要
- 相談段はすべて read-only 運用：書き込み・実行は Claude Code 本体が行う
- 実装フェーズに入ったら AGENTS.md / CLAUDE.md ルール（バックアップ必須・shebang禁止・`encoding="utf-8"` 明示）を強制適用する

---

## Step 1: レベル選択（必須・既定 Lv2）

**直前指示にレベル明示があればスキップ**:
- 「Lv1」「軽く」「Codex だけ」「クイック」 → Lv1
- 「Lv2」「通常」「軽量」「C+G」 → Lv2
- 「Lv3」「PR 直前」「再レビューも」 → Lv3
- 「Lv4」「フル相談」「3 者で」 → Lv4
- 「Lv5」「厳しく」「自動修正も」 → Lv5

それ以外は `AskUserQuestion` で 1 回聞く（既定 = Lv2）:

```
Q: レビュー強度レベル（既定 Lv2）
1. Lv1 — Codex のみ ×1（軽い差分チェック・小修正・低リスク）
2. Lv2 — Codex + Gemini 並列 ×1（通常開発の標準・既定推奨・旧 /codex 等価）
3. Lv3 — Lv2 + 実装後 Codex 再レビュー（差分のみ）×1（PR 直前の品質ゲート）
4. Lv4 — Gemini→DS→Codex 直列フル相談 + 実装後 Codex 再レビュー ×1（高リスク変更・設計判断・本番影響大）
5. Lv5 — Lv4 + 🔴 重大指摘の自動修正 1 周（Codex 再レビュー計 ×2、改善なしで停止）（リリース直前・障害再発防止）
```

選択後、対象（差分／ファイル／貼り付けテキスト）と検討テーマも同時に確認する。
ユーザーが何も指定せず Enter / 空応答した場合は **Lv2 を採用**して進める。

---

## 🔴 重大指摘の定義（Lv5 自動修正ループの対象）

以下を満たす指摘のみ「🔴 重大」とし、Lv5 では自動修正ループの対象になる:

1. **セキュリティ脆弱性** — XSS / SQL injection / コマンドインジェクション / 認証認可バグ等 OWASP Top 10
2. **データ破壊リスク** — 誤った DELETE / UPDATE / マイグレーション不可逆操作・バックアップなし上書き
3. **公開 API 仕様逸脱** — 後方互換破壊・契約違反・破壊的変更
4. **明白な論理バグ** — テストで検出可能な失敗パス・既知の例外を握り潰す等

これら以外は 🟠 重要 / 🟡 注意とし、自動修正ループの対象外（Lv5 でもユーザー判断扱い）。

---

## 失敗時の扱い（レベル別）

| Lv | 検証 NG（Step B） | 再レビューで 🔴 検出 |
|---|---|---|
| 1-2 | 中断してユーザー判断（実装フェーズなし） | 該当なし |
| 3 | Step A に戻り 1 回まで自動修正→再検証 | 報告のみ（Step C2 自動修正なし） |
| 4 | 同上 | 報告のみ |
| 5 | 同上 | Step C2 で 1 周まで自動修正→再レビュー（改善なしで停止＋ユーザー判断必須） |

---

## Lv1: Codex のみ

### Step 2-1A: Codex level 確認

`AskUserQuestion`:
- Codex reasoning: low / medium（推奨） / high

### Step 2-1B: Codex 単独実行

```bash
mkdir -p "C:/tmp-ai" && cd "C:/tmp-ai" && codex exec -c model_reasoning_effort="<low|medium|high>" --sandbox read-only --skip-git-repo-check "<Codex プロンプト>" < /dev/null
```

### Step 2-1C: 3 列レビュー表で出力

| 指摘（🔴/🟠/🟡＋根拠1行） | Codex | Claude採用 | 対応 |
|---|---|---|---|

- Claude採用: ✅採用 / ⏭️スキップ / 🔄部分採用
- 表の前後に **総評（1〜3行）** と **次アクション（箇条書き）** を必ず添える

**Lv1 はここで終了**（実装・再レビューなし）。次アクションは Claude Code 本体が必要に応じて実行。

---

## Lv2: Codex + Gemini 並列（既定・旧 /codex 等価）

### Step 2-2A: Codex level と Gemini 観点を確認

`AskUserQuestion` で同時に聞く:
- Codex reasoning: low / medium（推奨） / high
- Gemini 観点: 要約 / 原因特定 / 参考情報収集 / 比較評価

### Step 2-2B: 並列起動（**1 メッセージで Bash 2 個**）

```bash
# Bash #1（Codex）
mkdir -p "C:/tmp-ai" && cd "C:/tmp-ai" && codex exec -c model_reasoning_effort="<low|medium|high>" --sandbox read-only --skip-git-repo-check "<Codex プロンプト>" < /dev/null

# Bash #2（Gemini）
mkdir -p "C:/tmp-ai" && cd "C:/tmp-ai" && gemini --skip-trust -p "<Gemini プロンプト>" < /dev/null
```

`cd "C:/tmp-ai"` は日本語 CWD 文字化け＆Gemini の AGENTS.md 自動読込回避。`< /dev/null` はハング防止（必須）。

### Step 2-2C: 5 列統合表で出力

| 指摘（🔴/🟠/🟡＋根拠1行） | Codex | Gemini | Claude採用 | 対応 |
|---|---|---|---|---|

- 「指摘」列に **根拠1行を内包**（横長を避ける）
- 表の前後に **総評（1〜3行）** と **次アクション（箇条書き）** を必ず添える

**Lv2 はここで終了**（実装・再レビューなし）。次アクションは Claude Code 本体が必要に応じて実行。

---

## Lv3: Lv2 + 実装 + 検証 + Codex 再レビュー（差分のみ）

### Step 2-3A〜C: Lv2 と同じ流れで C+G 並列レビュー

Step 2-2A〜C をそのまま実行（5 列統合表まで）。

### Step 2-3D: 実装許可確認

`AskUserQuestion`:
```
1. 実装する（→ Step A へ）
2. 実装せず終了
3. 修正して実装（次アクション差し替え後）
```

「実装まで一気に」等が指示済みならスキップして Step A へ。

### Step 2-3E〜H: 共通フロー実行

Step A（実装）→ Step B（検証）→ Step C（Codex 再レビュー・差分のみ）→ Step D（最終まとめ）

🔴 が検出されても **Step C2 は走らない**（Lv5 のみ）。報告のみで Step D へ。

---

## Lv4: フル相談（直列）+ 実装 + 検証 + Codex 再レビュー

### Step 2-4A: Gemini に案出し依頼

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

### Step 2-4B: Claude が Gemini 案を検討

Claude Code 自身が Gemini 出力を読み、ユーザー向けに表示:
- Gemini が提示した案の一覧（番号付き）
- 各案について Claude が見た **追加の懸念点・改善余地**
- Claude の現時点での **第1推し案**

### Step 2-4C: DS に別案依頼（advisor モード）

**DS 入力ルール**:
- 原文/差分は **絶対パス** で参照（コピペで肥大化させない）
- Gemini と Claude の検討結果は **要約版**（各 500 字以内）を渡す
- DS への総入力は **2KB 以下** を目安（タイムアウト・品質低下回避）
- DS スクリプトは **絶対パス**で呼ぶ（CWD は `C:/tmp-ai` のため相対パス不可）

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

python "C:/ClaudeCode/.claude/tools/deepseek_coder.py" --role advisor "C:/tmp-ai/ds_prompt.txt"
```

DS は `ROLE_PROMPTS["advisor"]` の構造（別案 / 見落とし / 採否コメント）で返す。

**DS 呼び出し後の使用量表示（必須・転記）**:
- スクリプトが stderr に `[DS Usage] 今回: ... / 累計: ...` を 2 行出力する（今回トークン・累計・¥/$ 換算）
- Claude は実行結果のこの 2 行を **そのまま Step 2-4D の手前でユーザー向けに表示**（料金可視化目的・省略禁止）
- セッション累計は `.claude/tools/.deepseek_usage_session.json` に atomic write で保存（最終呼出から 4 時間で自動リセット、手動リセットは `--reset-session`）
- 円換算レートは既定 1USD=150JPY。環境変数 `DEEPSEEK_USD_TO_JPY` で上書き可能

### Step 2-4D: Claude が再検討

Gemini 案 + DS 別案を統合し、ユーザー向けに表示:

| # | 出所 | 案概要 | Claude評価 |
|---|---|---|---|
| 1 | Gemini | ... | ... |
| 2 | DS別案 | ... | ... |

その上で **Claude の統合推し案**（Gemini と DS のハイブリッドも可）を 1 つ決める。

### Step 2-4E: Codex に最終レビュー依頼

統合推し案を Codex にレビューさせる。reasoning level は原則 **medium**、設計判断が重い場合は **high**。

```bash
cat > "C:/tmp-ai/codex_prompt.txt" <<'EOF'
以下の実装方針を、バグ・設計上の懸念・セキュリティ観点・改善点で厳密にレビューしてください。
日本語回答。プロジェクトの AGENTS.md に従う。

[統合推し案]
<Step 2-4D の結論>

[対象ファイル（参考・絶対パス）]
<絶対パス>
EOF

mkdir -p "C:/tmp-ai" && cd "C:/tmp-ai" && codex exec -c model_reasoning_effort="medium" --sandbox read-only --skip-git-repo-check "$(cat 'C:/tmp-ai/codex_prompt.txt')" < /dev/null
```

### Step 2-4F: Claude 相談まとめ（6 列統合表）

| # | 指摘/論点（🔴/🟠/🟡＋根拠1行） | Gemini | DS | Codex | Claude最終判断 |
|---|---|---|---|---|---|

表の前後に必ず添える:
1. **採用方針** — 最終的にどの案で進めるか（1〜3行）
2. **次アクション** — 実装する項目（箇条書き、ファイル・行・修正方針）

```bash
cp <相談まとめ.md> "C:/tmp-ai/cgd_lv4_$(date +%Y%m%d_%H%M%S).md"
```

### Step 2-4G: 実装許可確認

Lv3 と同じ AskUserQuestion（1=実装する / 2=実装せず終了 / 3=修正して実装）。

### Step 2-4H〜K: 共通フロー実行

Step A（実装）→ Step B（検証）→ Step C（Codex 再レビュー・差分のみ）→ Step D（最終まとめ）

🔴 が検出されても **Step C2 は走らない**。報告のみで Step D へ。

---

## Lv5: Lv4 + 🔴 重大指摘の自動修正 1 周

Step 2-4A〜H までは Lv4 と同一。Step C の後に **Step C2** を追加。

### Step C2: 🔴 重大指摘の自動修正ループ（最大 1 周）

Step C の表で **🔴 重大指摘**（前述定義に該当）が 1 つ以上 ✅採用 になった場合のみ実行:

1. 該当箇所を Step A と同じ要領で修正（**バックアップ必須**）
2. Step B（検証）を再実行
3. Step C（Codex 再レビュー）を再実行

**安全弁**:
- 自動で回す上限は **1 周まで**
- 1 周回しても **同じ🔴指摘が再発した（改善なし）** 場合は **即座に停止**してユーザー判断を仰ぐ
- 別の新しい🔴が出た場合も **2 周目には進まず**ユーザー判断を仰ぐ
- 🟠 重要 / 🟡 注意のみの場合は自動ループせず、Step D の「未対応指摘」に記載

**ユーザー判断を仰ぐときの報告内容**（1〜3 行）:
1. 何周回したか
2. どの🔴が解消され、どれが残ったか
3. なぜ自動継続をやめたか（改善なし / 新規🔴 / 上限到達）

---

## Step A: 実装フェーズ（Lv3-5 共通）

AGENTS.md / CLAUDE.md ルールを **強制適用**:

- **既存ファイル編集前に必ず `cp <file> <file>.bak_$(date +%Y%m%d_%H%M%S)` でバックアップ**（バックアップなしの編集禁止）
- シバン行（`#!/usr/bin/env python3`）禁止（Windows で `py.exe` が即終了する）
- ファイル読み書きは常に `encoding="utf-8"` 明示
- CP932 コンソール対策: Python ワンライナー先頭に `sys.stdout.reconfigure(encoding="utf-8")`
- 日本語パスへの Edit / Write が失敗する場合は **Python スクリプトを `C:/ClaudeCode/` に書いて実行**
- バッチファイル（.bat）は **CP932** で書く（Write ツールは UTF-8 で書くため文字化け）
- API キー / シークレットを書き込まない・コミットしない

実装は前段で出した「次アクション」の項目順に進める。
TodoWrite で進捗を管理する（項目数が 3 個以上の場合は必須）。

---

## Step B: 検証フェーズ（Lv3-5 共通、CLAUDE.md「コーディング後の必須検証」を強制）

実装ファイルごとに以下 4 項目を確認し、**結果を表形式で必ず報告**:

| # | 検証項目 | 結果 |
|---|---|---|
| 1 | 対象ファイルの import 一覧（先頭 20 行）を確認 | ✅/⚠️ |
| 2 | 実 import で動作確認（`python -c "import sys; sys.path.insert(0, r'PATH'); from <mod> import <fn>; print('OK')"`） | ✅/❌ |
| 3 | パス定数が実在するか（`python -c "from pathlib import Path; p=Path(r'...'); print(p, p.exists())"`） | ✅/❌ |
| 4 | `ast.parse` だけで済ませていない（実 import まで実行した） | ✅ |

**`ast.parse` だけの確認は NG**。必ず実 import まで実行する。

検証 NG（❌）が 1 つでも出たら Step A に戻って **1 回まで自動修正**→再検証。2 回連続で NG なら停止してユーザー判断（無限ループ防止）。

Python 以外（JS / TS / シェル等）の場合は、その言語の実行可能な最小確認（`node --check`、`tsc --noEmit`、`bash -n` 等）に置き換える。

---

## Step C: Codex 再レビュー（Lv3-5 共通、差分のみ）

実装した差分を **Codex 単独**でレビューする。Gemini は呼ばない（トークン節約）。

- 対象: 直近の `git diff`（**差分のみ**で全ファイル渡さない・時間とトークン圧縮）
- reasoning: 原則 **medium**（Lv5 でセキュリティに関わる場合は **high**）
- 観点: バグ・設計上の懸念・副作用・既存仕様との整合性・🔴重大指摘定義への該当性

```bash
git diff > "C:/tmp-ai/impl_diff.patch"
mkdir -p "C:/tmp-ai" && cd "C:/tmp-ai" && codex exec -c model_reasoning_effort="medium" --sandbox read-only --skip-git-repo-check "<差分レビュープロンプト・絶対パスで diff 参照>" < /dev/null
```

結果は **3 列レビュー表**（Lv1 と同じフォーマット）で出力:

| 指摘（🔴/🟠/🟡＋根拠1行） | Codex | Claude採用 | 対応 |
|---|---|---|---|

🔴 が検出された場合の扱いは **Lv による**:
- Lv3-4: 報告のみ（Step C2 は走らない）
- Lv5: Step C2 へ進む（自動修正ループ最大 1 周）

---

## Step D: 最終まとめ（共通・必須出力）

以下を 1 つの報告にまとめる:

1. **実装した内容** — 変更ファイル（絶対パス）と主要な変更点を箇条書き
2. **検証結果** — Step B の表
3. **再レビュー結果**（Lv3-5）— Step C の 3 列レビュー表 + 修正ループ周回数（Lv5 のみ、0 / 1 / 中断理由）
4. **未対応指摘**（あれば） — 🟠 / 🟡 で残ったもの・Lv3-4 で残った 🔴
5. **残課題・申し送り事項**（あれば）

```bash
cp <最終報告.md> "C:/tmp-ai/cgd_impl_$(date +%Y%m%d_%H%M%S).md"
```

---

## Bash タイムアウト

| ステップ | コマンド | timeout |
|---|---|---|
| Lv2-2B / Lv4-2A | gemini | 180000 (3分) |
| Lv4-2C | deepseek (advisor) | 120000 (2分) |
| Lv1-2B / Lv2-2B / Lv4-2E / C | codex medium | 300000 (5分) |
| Lv1-2B / Lv2-2B / Lv4-2E / C | codex high | 600000 (10分) |
| Step B | python -c "from <mod> ..." | 60000 (1分) |

---

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
- Lv2 で片方が認証エラーのまま、もう片方の出力だけで 5 列統合表を作る
- Lv4-5 で途中段の認証エラーを無視して中間結果を「結論」として扱う
- 別の API（例: 認証成功している側）で代用して続行する

---

## 注意事項

- **直列実行**: Lv4-5 の相談段（2-4A→F）は順序が重要（前段の出力を後段に渡す）。並列起動はしない
- **並列実行**: Lv2 と Lv3 の C+G レビュー段は 1 メッセージ内 Bash 2 個で並列起動
- **再レビュー（Step C）は Codex 単独**: トークン節約のため Gemini は呼ばない
- **差分のみ**: Step C は `git diff` を渡し、全ファイルは渡さない（時間とコスト圧縮）
- **スキル連鎖禁止**: 本スキル内から `/codex` や別スキルを Skill ツール経由で自動呼び出ししない
- **相談段は read-only**: Codex `--sandbox read-only`、Gemini `-p` 非対話、DS は API 単発呼び出し
- **書き込みフェーズは Step A のみ**: ファイル編集・新規作成は **Step A に集約**。Codex / Gemini / DS には絶対に書き込ませない
- **API キー**: DS は `DEEPSEEK_API_KEY` を読む。Codex/Gemini はサブスク認証（`OPENAI_API_KEY` / `GEMINI_API_KEY` を環境変数にセットしない）
- **機密情報**: 顧客データ・社内 DB 接続情報を不必要に外部 API（DS）に渡さない
- **DS パスは絶対パス**: 相対 `.claude/tools/...` は CWD=`C:/tmp-ai` で解決失敗するので必ず絶対パス
- **実装フェーズの規約強制**: Step A では AGENTS.md / CLAUDE.md ルール（バックアップ・shebang 禁止・`encoding="utf-8"` 明示・日本語パスは Python スクリプト経由・.bat は CP932）を必ず守る
- **検証フェーズの省略禁止**: Step B で `ast.parse` だけで済ませず、必ず実 import まで実行
- **Lv5 自動修正ループ上限**: 最大 1 周。改善なし / 新規🔴 / 上限到達のいずれかで停止しユーザー判断
- **巨大対象は要点抽出**: Codex / Gemini に丸投げせず Claude Code 側で要点抜粋してから渡す

---

## トラブルシュート

- **DS が `DEEPSEEK_API_KEY が設定されていません`** → 環境変数を確認
- **DS のレスポンスがコード生成っぽい** → `--role advisor` 付け忘れ。デフォルトは `coder`
- **DS が「ファイルが見つかりません」** → 相対パス指定の罠。絶対パスで指定し直す
- **Codex `Not logged in`** → `codex login` の実行を依頼して停止
- **Gemini 認証エラー** → 一度 `gemini` を対話起動して `/auth` でログインを案内し、停止
- **無応答ハング** → `< /dev/null` 付け忘れ
- **日本語 CWD 文字化け** → `cd "C:/tmp-ai"` 忘れ
- **`unexpected argument '--ephemeral'` 等** → CLI バージョン差異。該当フラグを外して再試行
- **PowerShell でエラー** → 本スキルは Bash 必須。Git Bash で起動し直す
- **レート制限（特に Gemini 無料枠）** → 時間を空けて再試行
- **タイムアウト** → Codex は `reasoning=low` に落とす、Gemini はプロンプトを分割

---

## レベル選びの目安

| シーン | 推奨 Lv |
|---|---|
| 小修正・低リスク・急ぎ対応 | Lv1 |
| 通常開発の標準（機能追加・軽中規模リファクタ） | **Lv2（既定）** |
| マージ前の品質ゲート（PR 直前見落とし削減） | Lv3 |
| 高リスク変更（DB 設計・API 設計・状態管理方式・セキュリティ） | Lv4 |
| 重要な設計判断（アーキ変更・性能影響大） | Lv4 |
| 難しいバグの原因仮説出し（症状から複数候補が必要） | Lv4 |
| 大規模リファクタの方針決め | Lv4 |
| 新規機能のアーキテクチャ案出し | Lv4 |
| リリース直前・障害再発防止・絶対に通したい | Lv5 |
