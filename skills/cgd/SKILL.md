---
name: cgd
description: Codex+Gemini+DeepSeek+Qwen の統合コードレビュー・設計相談・実装・検証スキル。**7段階レベル（Lv1〜Lv7）**でユーザーがトークン消費・所要時間を選択。Lv1=Codex単独 / Lv2=Codex+Gemini並列（既定推奨・旧/codex等価） / Lv3=Lv2+実装後Codex再レビュー / Lv4=Gemini→[DS+Qwen並列advisor]→Codex直列フル相談+再レビュー / Lv5=Lv4+🔴重大指摘の自動修正1周 / Lv6=C+G+DS+Qwen 4者並列レビュー（全員reviewer役）+実装+検証+Codex再レビュー+🔴自動修正1周 / Lv7=Codex多重(medium+high)+補助(G/DS/Qwen)の5者並列「Codex集中」構成+実装+検証+Codex再レビュー+🔴自動修正1周（最深掘り）。Lv4-5はDS/Qwenをadvisor役で別案出し、Lv6は4者横並びreviewer、Lv7は深いintegrationバグ検出を狙ってCodex多重化+DS/Qwenに関連関数抜粋を渡して補助役を強化。差分レビュー、設計判断、別案出し、実装、検証まで一気通貫。**旧 `/codex` `/gemini` 単体スキルは廃止され、本スキル（`/cgd` または `/codex` 起動）が必ずレベル選択から始まる**。全Lv共通の任意オプションで『critic観点』（辛口ユーザー視点＝ITに疎い現場担当者の使い勝手の不満 + あるべき論＝本来この仕様はどうあるべきかの批判を Claude本体+DS criticで評価）を追加でき、技術的正しさとは別軸で使い勝手・仕様の妥当性を否定的にチェックする。「3者に相談」「フルパイプ」「4者レビュー」「Codex多重」「Codex集中」「辛口レビュー」「ユーザー視点」「あるべき論」「critic」「cgd」「Codexにレビュー」「セカンドオピニオン」「C+G」「cg」などのキーワードで起動。重要な設計判断・難しいバグ・大きめのリファクタの検討時には積極的に提案すること。既存 /generate-by-deepseek（DSコード生成→Claudeレビュー）とは目的が異なる。
---
<!-- SKILL_VERSION: 2026-05-30_162008 -->

# cgd — Codex + Gemini + DeepSeek + Qwen 統合スキル（Lv1〜7）

Claude Code は司令塔。Codex / Gemini / DeepSeek / Qwen を **役割分担** で使い分ける:

| エージェント | 役割 | 呼び出し |
|---|---|---|
| Codex | コードレビュー・厳密な品質ゲート（Lv7 では medium + high の **多重実行** で深さ・別視点を並列取得） | `codex exec -c model_reasoning_effort="<low\|medium\|high>" ...` |
| Gemini | 長文解析・調査・案出し（Lv7 では補助役・対象ファイル絶対パスを渡し全体構造の参考視点を出させる） | `gemini -p "..."` |
| DeepSeek | Lv4-5: 推論寄り別案出し (`--role advisor`) / Lv6: 並列 reviewer / Lv7: 補助 reviewer（**関連関数抜粋 + 差分**で表層指摘を減らす） | `python "<絶対パス>/deepseek_coder.py" --role <advisor\|reviewer> "..."` |
| Qwen3-Coder-Plus | Lv4-5: 実装寄り別案出し / Lv6: 並列 reviewer / Lv7: 補助 reviewer（**関連関数抜粋 + 差分**で表層指摘を減らす） | `python "<絶対パス>/qwen_advisor.py" --role <advisor\|reviewer> "..."` |
| Claude Code | 統合判断・実装・検証（Lv7 では事前に grep + Read で関連関数を抜粋して DS/Qwen に渡す） | （本体） |

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
- Lv4-5 / Lv6 / Lv7 では `DEEPSEEK_API_KEY` と `DASHSCOPE_API_KEY` 環境変数が必要（DS と Qwen を並列で呼ぶため・Lv4-5 は advisor、Lv6/Lv7 は reviewer 役）
- 相談段はすべて read-only 運用：書き込み・実行は Claude Code 本体が行う
- 実装フェーズに入ったら AGENTS.md / CLAUDE.md ルール（バックアップ必須・shebang禁止・`encoding="utf-8"` 明示）を強制適用する

---

## Step 0: 起動時のスキル最新確認（必須・Step 1 の前に実行）

`/cgd` `/codex` 起動のたびに、**Step 1 に入る前に**スキル定義が最新か確認する。既存セッションに古い手順が残ったまま実行する事故（このスキルは頻繁に改修される）を防ぐ。

### 手順
1. **バージョンスタンプ確認**（軽量・必須）: Bash で最新スタンプを取得:
   ```bash
   grep -m1 'SKILL_VERSION' "C:/ClaudeCode/.claude/skills/cgd/SKILL.md"
   ```
2. **判定**:
   - 取得スタンプが、いま自分のコンテキストにある cgd/SKILL.md 本文の `<!-- SKILL_VERSION: ... -->` と**一致** → 最新。そのまま Step 1 へ
   - **不一致**、または **今セッションで cgd/SKILL.md を読んだ記憶がない** → `Read` で cgd/SKILL.md を読み直してから Step 1 へ（`/sr` 相当）
3. **claude-shared(Git) 未取込チェック**（他端末の更新検知・推奨・重い時はスキップ可）:
   ```bash
   git -C "$USERPROFILE/claude-shared" fetch --quiet && git -C "$USERPROFILE/claude-shared" status -sb | head -1
   ```
   - `behind` が出たら「claude-shared に未取込更新があります。`/g-dl` で取り込めます」と**通知のみ**（自動 pull しない・ユーザー判断）
   - claude-shared 無し / Git エラーは黙ってスキップ（致命ではない）

### 注意
- Step 0 は**起動直後に1回だけ**。同一セッションで連続使用する場合、2回目以降はスタンプ一致なら省略可
- **スキル連鎖禁止**: Step 0 で `/g-dl` `/sr` を Skill ツールで自動呼び出ししない。Read で読み直す / 通知するに留める
- スタンプ運用は末尾「スタンプ運用ルール」を参照（編集時に必ず更新）

---

## Step 1: レベル選択（必須・既定 Lv2）

**直前指示にレベル明示があればスキップ**:
- 「Lv1」「軽く」「Codex だけ」「クイック」 → Lv1
- 「Lv2」「通常」「軽量」「C+G」 → Lv2
- 「Lv3」「PR 直前」「再レビューも」 → Lv3
- 「Lv4」「フル相談」「3 者で」 → Lv4
- 「Lv5」「厳しく」「自動修正も」 → Lv5
- 「Lv6」「4 者レビュー」「DS もレビュー」「最重量」「複眼レビュー」 → Lv6
- 「Lv7」「Codex 多重」「Codex 集中」「Integration バグ重視」「最深掘り」 → Lv7

それ以外は `AskUserQuestion` で 1 回聞く（既定 = Lv2）:

```
Q: レビュー強度レベル（既定 Lv2）
1. Lv1 — Codex のみ ×1（軽い差分チェック・小修正・低リスク）
2. Lv2 — Codex + Gemini 並列 ×1（通常開発の標準・既定推奨・旧 /codex 等価）
3. Lv3 — Lv2 + 実装後 Codex 再レビュー（差分のみ）×1（PR 直前の品質ゲート）
4. Lv4 — Gemini→[DS+Qwen 並列 advisor]→Codex 直列フル相談 + 実装後 Codex 再レビュー ×1（高リスク変更・設計判断・本番影響大）
5. Lv5 — Lv4 + 🔴 重大指摘の自動修正 1 周（Codex 再レビュー計 ×2、改善なしで停止）（リリース直前・障害再発防止）
6. Lv6 — Codex + Gemini + DS + Qwen の 4 者並列レビュー（全員 reviewer 役、advisor 段廃止） + 実装 + 検証 + Codex 再レビュー + 🔴 自動修正 1 周（最重量級・複眼レビューで盲点を潰したい・Lv4-5 で DS/Qwen 別案が機能しない対象の代替）
7. Lv7 — **Codex 多重（medium + high）** + Gemini + DS + Qwen の **5 者並列「Codex 集中」レビュー** + 実装 + 検証 + Codex 再レビュー + 🔴 自動修正 1 周（最深掘り・integration バグ重視・関数間整合性・大規模 IIFE/モジュール内のクロスリファレンス検査）
```

選択後、対象（差分／ファイル／貼り付けテキスト）と検討テーマも同時に確認する。
ユーザーが何も指定せず Enter / 空応答した場合は **Lv2 を採用**して進める。

**critic 観点（任意・全 Lv 共通）**: レベル選択と同時に「**critic 観点（辛口ユーザー視点 / あるべき論）も追加するか**」を確認する（既定オフ）。「辛口で」「ユーザー視点で」「あるべき論で」「現場目線で」「critic」等の指示があれば自動で有効。技術レビューとは別軸で「使う人が困らないか・本来どうあるべきか」を Claude 本体 + DS critic で否定的に評価する。詳細は後述「**critic 観点**」セクション。

---

## 🔴 重大指摘の定義（Lv5 / Lv6 / Lv7 自動修正ループの対象）

以下を満たす指摘のみ「🔴 重大」とし、Lv5 / Lv6 / Lv7 では自動修正ループの対象になる:

1. **セキュリティ脆弱性** — XSS / SQL injection / コマンドインジェクション / 認証認可バグ等 OWASP Top 10
2. **データ破壊リスク** — 誤った DELETE / UPDATE / マイグレーション不可逆操作・バックアップなし上書き
3. **公開 API 仕様逸脱** — 後方互換破壊・契約違反・破壊的変更
4. **明白な論理バグ** — テストで検出可能な失敗パス・既知の例外を握り潰す等
5. **Integration バグ** (Lv7 で特に重視) — 関数間の暗黙の前提違反・スコープを跨いだ状態管理の整合性破綻・呼出経路ごとの副作用差異

これら以外は 🟠 重要 / 🟡 注意とし、自動修正ループの対象外（Lv5 / Lv6 / Lv7 でもユーザー判断扱い）。

---

## 失敗時の扱い（レベル別）

| Lv | 検証 NG（Step B） | 再レビューで 🔴 検出 |
|---|---|---|
| 1-2 | 中断してユーザー判断（実装フェーズなし） | 該当なし |
| 3 | Step A に戻り 1 回まで自動修正→再検証 | 報告のみ（Step C2 自動修正なし） |
| 4 | 同上 | 報告のみ |
| 5 | 同上 | Step C2 で 1 周まで自動修正→再レビュー（改善なしで停止＋ユーザー判断必須） |
| 6 | 同上 | Step C2 で 1 周まで自動修正→再レビュー（Lv5 と同仕様） |
| 7 | 同上 | Step C2 で 1 周まで自動修正→再レビュー（Lv5 と同仕様・再レビューは Codex 多重ではなく medium 単独） |

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

### Step 2-4C: DS と Qwen に別案依頼（並列・advisor モード）

DeepSeek（推論寄り）と Qwen3-Coder-Plus（実装寄り）に**並列で**別案を求める。
両者は **同じ入力プロンプト** を共有してよい（system prompt が違うので出力傾向は分かれる）。

**入力ルール（DS / Qwen 共通）**:
- 原文/差分は **絶対パス** で参照（コピペで肥大化させない）
- Gemini と Claude の検討結果は **要約版**（各 500 字以内）を渡す
- 総入力は **2KB 以下** を目安（タイムアウト・品質低下回避）
- スクリプトは **絶対パス**で呼ぶ（CWD は `C:/tmp-ai` のため相対パス不可）
- 両者で同じ `advisor_prompt.txt` を再利用する（重複生成しない）

**プロンプト準備**（先に1回だけ作る）:

```bash
cat > "C:/tmp-ai/advisor_prompt.txt" <<'EOF'
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
```

**並列起動（1 メッセージで Bash 2 個）**:

```bash
# Bash #1（DeepSeek — 推論寄り別案）
python "C:/ClaudeCode/.claude/tools/deepseek_coder.py" --role advisor "C:/tmp-ai/advisor_prompt.txt"

# Bash #2（Qwen3-Coder-Plus — 実装寄り別案）
python "C:/ClaudeCode/.claude/tools/qwen_advisor.py" --role advisor "C:/tmp-ai/advisor_prompt.txt"
```

両者とも `ROLE_PROMPTS["advisor"]` の構造（別案 / 見落とし / 採否コメント）で返す。
DS は推論ベースで「根本発想」を、Qwen はコーダー視点で「実装観点の別アプローチ」を出すように system prompt で誘導済み。

**使用量表示（必須・転記、両方とも）**:
- DS スクリプトが stderr に `[DS Usage] 今回: ... / 累計: ...` を 2 行出力
- Qwen スクリプトが stderr に `[Qwen Usage] 今回: ... / 累計: ...` を 2 行出力
- Claude は両者の実行結果のこの 2 行を **そのまま Step 2-4D の手前でユーザー向けに表示**（料金可視化目的・省略禁止）
- セッション累計は別ファイルで管理（DS: `.deepseek_usage_session.json` / Qwen: `.qwen_usage_session.json`）。最終呼出から 4 時間で自動リセット、手動リセットは各々 `--reset-session`
- 円換算レートは既定 1USD=150JPY。環境変数で上書き可能（DS: `DEEPSEEK_USD_TO_JPY` / Qwen: `QWEN_USD_TO_JPY`）

**並列段の認証エラー時**:
DS / Qwen のどちらか一方でも認証エラーを返したら即中断（後述「認証エラー検出時の挙動」に従う）。
片肺で続行しない・自動切替もしない。

### Step 2-4D: Claude が再検討

Gemini 案 + DS 別案 + Qwen 別案を統合し、ユーザー向けに表示:

| # | 出所 | 案概要 | Claude評価 |
|---|---|---|---|
| 1 | Gemini | ... | ... |
| 2 | DS別案 | ... | ... |
| 3 | Qwen別案 | ... | ... |

その上で **Claude の統合推し案**（Gemini / DS / Qwen のハイブリッドも可）を 1 つ決める。
DS と Qwen で似た案が出た場合は **収束したシグナル**として扱い、推し案の信頼度を上げる根拠にする。
逆に大きく食い違う場合は **トレードオフが大きい設計判断**としてユーザーに明示する。

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

mkdir -p "C:/tmp-ai" && cd "C:/tmp-ai" && codex exec -c model_reasoning_effort="medium" --sandbox read-only --skip-git-repo-check "まず C:/tmp-ai/codex_prompt.txt の全文を読み、記載の実装方針を厳密にレビュー。必要なら対象実ファイルも読んでよい。日本語で回答。" < /dev/null
```

### Step 2-4F: Claude 相談まとめ（7 列統合表）

| # | 指摘/論点（🔴/🟠/🟡＋根拠1行） | Gemini | DS | Qwen | Codex | Claude最終判断 |
|---|---|---|---|---|---|---|

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

### Step C2: 🔴 重大指摘の自動修正ループ（Lv5 / Lv6 / Lv7 共通、最大 1 周）

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

## Lv6: C+G+DS+Qwen 4 者並列レビュー + 実装 + 検証 + Codex 再レビュー + 🔴 自動修正 1 周

Lv4-5 の DS / Qwen は **advisor 役（別案出し）** だが、実運用で「DS の別案が採用されることがほとんどなく実効性が薄い」という課題を受けて新設した最重量級レベル。advisor 段（Gemini→[DS+Qwen 並列]→Codex の直列）を **廃止**し、代わりに **DS / Qwen を Codex / Gemini と同じ原データ（差分・ファイル）を直接受け取る並列レビュアー** として運用する。

「複数の独立した目で同じコードを見る」をスケールアップさせた構成。同じ指摘が複数 AI から挙がれば信頼度が高い（収束シグナル）、1 者だけの指摘は false positive を疑う、という運用ができる。

### Step 2-6A: レビュー観点を確認

`AskUserQuestion` で同時に聞く:
- Codex reasoning: low / medium（推奨） / high
- Gemini 観点: 要約 / 原因特定 / 参考情報収集 / 比較評価

DS / Qwen は両者とも `--role reviewer` 固定で呼び出す（advisor との切替はしない・Lv6 の本質）。

### Step 2-6B: 4 者並列起動（**1 メッセージで Bash 4 個**）

入力データは 4 者とも **同じ原文（差分／ファイル絶対パス／貼り付けテキスト）** を受け取る。advisor 段でやっていた「Gemini 検討要約」「Claude 検討要約」は **渡さない**（要約による情報損失を避け、各 AI が独自に原文を解釈する）。

**プロンプト準備**（先に 1 回だけ作る・4 者で共有）:

```bash
cat > "C:/tmp-ai/review_input.txt" <<'EOF'
以下の差分／実装をレビューしてください。
バグ・設計上の懸念・セキュリティ・副作用・既存仕様との整合性を厳密に評価してください。
日本語で回答。AGENTS.md / CLAUDE.md がある場合はそれに従う。

[対象（絶対パスまたは内容）]
<差分内容または絶対パス>

[背景・狙い]
<1〜3 行>
EOF
```

**並列起動**:

```bash
# Bash #1（Codex）
mkdir -p "C:/tmp-ai" && cd "C:/tmp-ai" && codex exec -c model_reasoning_effort="<low|medium|high>" --sandbox read-only --skip-git-repo-check "まず C:/tmp-ai/review_input.txt の全文を読み、記載の差分・対象・評価観点に従ってレビュー。必要なら対象実ファイルも読んでよい。日本語で回答。" < /dev/null

# Bash #2（Gemini）
mkdir -p "C:/tmp-ai" && cd "C:/tmp-ai" && gemini --skip-trust -p "まず C:/tmp-ai/review_input.txt の全文を読み、記載の差分・対象・評価観点に従ってレビュー。日本語で回答。" < /dev/null

# Bash #3（DeepSeek reviewer — 推論寄り）
python "C:/ClaudeCode/.claude/tools/deepseek_coder.py" --role reviewer "C:/tmp-ai/review_input.txt"

# Bash #4（Qwen reviewer — 実装寄り）
python "C:/ClaudeCode/.claude/tools/qwen_advisor.py" --role reviewer "C:/tmp-ai/review_input.txt"
```

`cd "C:/tmp-ai"` は日本語 CWD 文字化け＆Gemini の AGENTS.md 自動読込回避。`< /dev/null` はハング防止（必須）。

**使用量表示（必須・転記、DS と Qwen 両方）**:
- DS スクリプトが stderr に `[DS Usage] 今回: ... / 累計: ...` を 2 行出力
- Qwen スクリプトが stderr に `[Qwen Usage] 今回: ... / 累計: ...` を 2 行出力
- Claude は両者の実行結果のこの 2 行を **そのまま Step 2-6C の手前でユーザー向けに表示**（料金可視化目的・省略禁止）

**reasoning を厳しくしたい場合**:
- DS: `--model deepseek-reasoner` で reasoner モデルに切替（料金 2 倍弱）
- Qwen: 既定の `qwen3-coder-plus` で十分。`qwen3-coder-flash` で軽量化も可（精度は落ちる）
- Codex: `model_reasoning_effort="high"` に上げる（タイムアウト 600s）

**並列段の認証エラー時**:
4 者のいずれか一つでも認証エラーを返したら **即中断**（後述「認証エラー検出時の挙動」に従う）。
3 者で続行しない・自動切替もしない。Lv6 の価値は「4 者すべての視点」なので 1 者でも欠けたら Lv2 や Lv4 と同じ意味になる。

### Step 2-6C: 6 列統合レビュー表で出力

| 指摘（🔴/🟠/🟡＋根拠1行） | Codex | Gemini | DS | Qwen | Claude 採用 | 対応 |
|---|---|---|---|---|---|---|

- 各 AI 列は ✅指摘あり / ❌指摘なし / 🔄部分一致 を記入
- **収束シグナル**: 同じ指摘が 2 者以上から挙がった場合は信頼度が高い扱いとし Claude 採用に強く反映
- **乖離シグナル**: 1 者だけが挙げた指摘は false positive の可能性も含めて吟味（特に Gemini / Qwen は推測寄りの指摘が混じる傾向）
- 表の前後に **総評（1〜3 行）** と **次アクション（箇条書き）** を必ず添える

```bash
cp <相談まとめ.md> "C:/tmp-ai/cgd_lv6_$(date +%Y%m%d_%H%M%S).md"
```

### Step 2-6D: 実装許可確認

`AskUserQuestion`:
```
1. 実装する（→ Step A へ）
2. 実装せず終了
3. 修正して実装（次アクション差し替え後）
```

「実装まで一気に」等が指示済みならスキップして Step A へ。

### Step 2-6E〜H: 共通フロー実行

Step A（実装）→ Step B（検証）→ Step C（Codex 再レビュー・差分のみ）→ **Step C2（🔴 自動修正ループ最大 1 周・Lv5 と同仕様）** → Step D（最終まとめ）

Step C2 の自動修正ループは **Lv5 の Step C2 セクションをそのまま適用** する（上限 1 周・改善なし／新規 🔴／上限到達で停止）。

---

## Lv7: Codex 集中 (medium + high 多重) + 補助 (G/DS/Qwen) 5 者並列レビュー + 実装 + 検証 + Codex 再レビュー + 🔴 自動修正 1 周

Lv6 動作テストの観察知見から派生した **最深掘り構成**。Lv6 は「4 者横並びレビュー」だが、実運用で **Codex が integration バグ（関数間の暗黙の前提違反・スコープを跨いだ状態管理破綻）の単独検出に圧倒的に強い**（sandbox read-only でファイル全体を探索できる）一方、DS/Qwen の reviewer は diff だけだと表層的になりがちだった。Lv7 はこのアンバランスを **Codex を medium と high で多重化** して底上げし、DS/Qwen には **関連関数を Claude が事前抽出して** 渡して補助役の質を上げる。

「複数の独立した目で同じコードを見る」（Lv6）より、「深い目 2 つ重ね + 補助で横から検査」（Lv7）のアプローチ。

### Step 2-7A: レビュー観点を確認

`AskUserQuestion` で同時に聞く:
- Gemini 観点: 要約 / 原因特定 / 参考情報収集 / 比較評価

Codex は **medium + high の 2 並列固定**（Lv7 の本質なので reasoning level 確認は不要）。
DS / Qwen は **`--role reviewer` 固定**。

### Step 2-7B: 関連関数の事前抽出（Claude 本体作業）

Lv6 と違い、DS / Qwen には **差分 + 関連関数抜粋** を渡す。Codex / Gemini はファイル絶対パスを渡して自分で読みに行かせる（sandbox 探索能力を活用）。

**Claude 本体の事前作業手順**:

1. `git diff` または `diff -u` で差分を取得し、変更 hunk の行範囲を特定
2. 各 hunk の周辺で **関連する関数の境界を grep + Read で抽出**
   - JS: `function ` / `const X = function` / `X = (...) =>` の宣言行
   - Python: `def ` `class ` の宣言行
   - GAS: `function ` の宣言行
3. 関連関数の本体を Read で取得し、`C:/tmp-ai/lv7_related_funcs.txt` に結合保存
4. ファイル全体が小さい（< 500 行）場合は **全文をそのまま抜粋** として使う

**抽出量の目安**:
- 差分行数の **5〜10 倍程度**（差分 50 行 → 抜粋 300〜500 行）が現実的
- 5KB 未満を目標（DS/Qwen の argv 制限・トークン制約・タイムアウト回避）
- 大きすぎる場合は「変更 hunk 直近 ± 30 行 + 呼び出し元関数 1〜2 個」に絞る

### Step 2-7C: 入力ファイル準備（先に 1 回だけ作る）

**Codex / Gemini 用入力**（ファイルパス渡し・自分で読みに行ける）:

```bash
cat > "C:/tmp-ai/lv7_codex_input.txt" <<'EOF'
以下の差分／実装を厳密にレビューしてください。
バグ・設計上の懸念・セキュリティ・副作用・既存仕様との整合性、特に
**関数間の integration バグ**（暗黙の前提違反・スコープを跨いだ状態管理破綻・
呼出経路ごとの副作用差異・catch ブロックでの throw 握り潰し等）を重点的に評価してください。
必要に応じて対象ファイルを直接読んで確認してください。
日本語で回答。AGENTS.md / CLAUDE.md がある場合はそれに従う。

[対象ファイル絶対パス]
<絶対パス>  ← 関数定義は自分で grep+Read してよい

[変更概要]
<1〜3 行>

[差分内容 — unified diff]
EOF
cat <diff-file> >> "C:/tmp-ai/lv7_codex_input.txt"
```

**DS / Qwen 用入力**（差分 + 関連関数抜粋・API なのでファイルアクセス不可）:

```bash
cat > "C:/tmp-ai/lv7_aux_input.txt" <<'EOF'
以下の差分と関連関数抜粋を厳密にレビューしてください。
バグ・設計上の懸念・セキュリティ・副作用・既存仕様との整合性を評価してください。
特に関連関数抜粋を読んで、差分が他関数の前提を破っていないか確認してください。
日本語で回答。

[対象ファイル絶対パス参考]
<絶対パス・API なので直接アクセス不可>

[変更概要]
<1〜3 行>

[関連関数抜粋 (Claude が事前抽出)]
EOF
cat "C:/tmp-ai/lv7_related_funcs.txt" >> "C:/tmp-ai/lv7_aux_input.txt"
cat >> "C:/tmp-ai/lv7_aux_input.txt" <<'EOF'

[差分内容 — unified diff]
EOF
cat <diff-file> >> "C:/tmp-ai/lv7_aux_input.txt"
```

### Step 2-7D: 5 者並列起動（**1 メッセージで Bash 5 個**）

```bash
# Bash #1（Codex medium — バランス重視）
mkdir -p "C:/tmp-ai" && cd "C:/tmp-ai" && codex exec -c model_reasoning_effort="medium" --sandbox read-only --skip-git-repo-check "まず C:/tmp-ai/lv7_codex_input.txt の全文を読み、記載の差分・対象・評価観点に従ってレビュー。必要なら対象実ファイルも読んでよい。日本語で回答。" < /dev/null

# Bash #2（Codex high — 深掘り）
mkdir -p "C:/tmp-ai" && cd "C:/tmp-ai" && codex exec -c model_reasoning_effort="high" --sandbox read-only --skip-git-repo-check "まず C:/tmp-ai/lv7_codex_input.txt の全文を読み、記載の差分・対象・評価観点に従ってレビュー。必要なら対象実ファイルも読んでよい。日本語で回答。" < /dev/null

# Bash #3（Gemini — 補助・全体構造把握）
mkdir -p "C:/tmp-ai" && cd "C:/tmp-ai" && gemini --skip-trust -p "まず C:/tmp-ai/lv7_codex_input.txt の全文を読み、記載の差分・対象・評価観点に従ってレビュー。日本語で回答。" < /dev/null

# Bash #4（DeepSeek reviewer — 補助・推論寄り）
python "C:/ClaudeCode/.claude/tools/deepseek_coder.py" --role reviewer "C:/tmp-ai/lv7_aux_input.txt"

# Bash #5（Qwen reviewer — 補助・実装寄り）
python "C:/ClaudeCode/.claude/tools/qwen_advisor.py" --role reviewer "C:/tmp-ai/lv7_aux_input.txt"
```

**Codex 多重の注意**:
- ChatGPT サブスククォータを **Codex 2 回分** 消費（同時実行）。Plus/Pro プラン上限を意識する
- reasoning=high は最大 10 分（timeout 600000 必須）
- 結果は別 session_id で独立（共有なし・互いに知らない）

**使用量表示（必須・転記、DS と Qwen 両方）**:
- DS / Qwen の stderr `[DS Usage]` / `[Qwen Usage]` の 2 行を Step 2-7E の手前で表示

**並列段の認証エラー時**:
5 者のいずれか一つでも認証エラーを返したら **即中断**（後述「認証エラー検出時の挙動」に従う）。特に Codex は medium と high の **両方** が認証成功している必要がある（片方失敗で続行しない）。Codex 多重が Lv7 の本質なので 1 つで代用しない。

### Step 2-7E: 7 列統合レビュー表で出力

| 指摘（🔴/🟠/🟡＋根拠1行） | Codex(med) | Codex(high) | Gemini | DS | Qwen | Claude 採用 | 対応 |
|---|---|---|---|---|---|---|---|

- 各 AI 列は ✅指摘あり / ❌指摘なし / 🔄部分一致 を記入
- **Codex 多重収束シグナル**: med と high で同じ指摘 → reasoning level に依らない確度高（最強の信頼度）
- **Codex 乖離シグナル**: high のみが指摘 → 深掘り効果で発見された integration バグの可能性。med のみが指摘 → 過剰反応や false positive の可能性も含めて吟味
- **補助からの追加発見**: Gemini/DS/Qwen 単独指摘 → Codex 多重の盲点候補（採否は慎重に）
- 表の前後に **総評（1〜3 行）** と **次アクション（箇条書き）** を必ず添える

```bash
cp <相談まとめ.md> "C:/tmp-ai/cgd_lv7_$(date +%Y%m%d_%H%M%S).md"
```

### Step 2-7F: 実装許可確認

`AskUserQuestion`:
```
1. 実装する（→ Step A へ）
2. 実装せず終了
3. 修正して実装（次アクション差し替え後）
```

### Step 2-7G〜J: 共通フロー実行

Step A（実装）→ Step B（検証）→ Step C（Codex 再レビュー・差分のみ・**medium 単独**）→ **Step C2（🔴 自動修正ループ最大 1 周・Lv5 と同仕様）** → Step D（最終まとめ）

**Step C を Codex 多重で回さない理由**:
- 再レビュー段は差分のみで規模が小さく、medium で十分な深さが出る
- Codex high をもう 1 回回すとサブスククォータ消費とタイムアウトのリスクが上がるが、得るものは少ない
- Lv7 の本質は **初回レビューでの深掘り**（Step 2-7D）にあり、再レビューはコスト圧縮を優先する

---

## Workflow 経由実行（Lv6-WF / Lv7-WF）— 主 context 汚染回避の高速版

Lv6 / Lv7 の **レビュー段（Step 2-XB の並列レビュー）は Workflow ツールに委譲できる**。Codex high の巨大出力（160KB+）を subagent context に閉じ込め、主 context には構造化 findings + 統合表（数KB）だけ返す方式。

**実測効果**（pickorder ScanCheck 差分で検証済）:
- 4者の生出力 計 **210KB**（うち Codex 198KB）が subagent 内に閉じ、主 context 流入は **約12KB**（**94%削減**）
- 費用 ¥1.06（DS+Qwen のみ・Codex/Gemini はサブスク）
- 同一セッションでの Lv6/Lv7 反復が現実的に（インライン版は 1-2 回で context 枯渇）

### スクリプト

| Lv | スクリプト | 構成 |
|---|---|---|
| Lv6-WF | `.claude/skills/cgd/workflows/cgd_lv6_review.js` | C+G+DS+Qwen 4者並列 → 6列表 |
| Lv7-WF | `.claude/skills/cgd/workflows/cgd_lv7_review.js` | Codex(med)+Codex(high)+G+DS+Qwen 5者並列 → 収束/乖離判定 → 7列表 |

### 責務分割（重要）

| 主 context が担当 | Workflow が担当 |
|---|---|
| Step 1 レベル選択（AskUserQuestion） | 並列レビュー段（agent が内部で codex/gemini/python 起動） |
| レビュー入力ファイルの準備（差分+背景） | 生出力を subagent に閉じ込め → 構造化 findings に圧縮 |
| Lv7: 関連関数の事前抽出（grep+Read） | 認証エラー/欠員チェック（全員成功で次段） |
| 完了後の table_md 描画・🔴 採否判断 | 収束/乖離判定 + 統合表生成 |
| 実装許可（AskUserQuestion）と **Step A〜D の実行** | 生ログを `C:/tmp-ai/cgd_raw_*.md` に保存しパス返却 |

**AskUserQuestion は workflow 内で使えない**ため、レベル選択・実装許可・Step D 確認は主 context に残す。**Step A（実装）/B（検証）/C（再レビュー）/C2（自動修正）は当面 主 context 実行**（AGENTS.md 規約の対話的監査が必要なため。将来 implement_and_fix workflow 化を検討）。

### 起動手順（ユニーク名方式・衝突回避）

入力ファイルは **ユニークサフィックス付き**にして他作業との衝突を根絶する。args は JSON 文字列で届くが、両スクリプトが冒頭で `JSON.parse` するので `input_path`/`label` が確実に効く（プローブで実証済）。

```bash
# 1. 主context: ユニークサフィックス生成 (Workflow 内は Date.now 禁止なので主context側で date)
RUN=$(date +%Y%m%d_%H%M%S)   # 例 20260529_185800

# Lv6-WF: 入力をユニーク名で配置
cat > "C:/tmp-ai/cgd_in_${RUN}.txt" <<'EOF'
<差分 + 背景 + 評価観点 + 対象ファイル絶対パス>
EOF

# Lv7-WF: codex用 と aux用 を別ユニーク名で
cat > "C:/tmp-ai/cgd_codex_${RUN}.txt" <<'EOF'
<差分 + 背景 + 対象ファイル絶対パス (Codex は sandbox で読める)>
EOF
cat > "C:/tmp-ai/cgd_aux_${RUN}.txt" <<'EOF'
<差分 + 関連関数抜粋 (DS/Qwen 用)>
EOF
```

```
# 2. Workflow 起動 (label にも RUN を含めると raw も cgd_raw_<reviewer>_<対象>_<RUN>.md でユニーク化)
# Lv6-WF:
Workflow({ scriptPath: ".../cgd_lv6_review.js",
           args: { input_path: "C:/tmp-ai/cgd_in_<RUN>.txt", codex_reasoning: "medium", label: "<対象名>_<RUN>" } })
# Lv7-WF:
Workflow({ scriptPath: ".../cgd_lv7_review.js",
           args: { input_path: "C:/tmp-ai/cgd_codex_<RUN>.txt", aux_input_path: "C:/tmp-ai/cgd_aux_<RUN>.txt", label: "<対象名>_<RUN>" } })

# 3. 完了通知 → 戻り値の label が "<対象名>_<RUN>" なら args 到達OK ("target" ならパース失敗を疑う)
# 4. table_md を描画、🔴 を raw 検証 (下記ガード) → 実装許可 AskUserQuestion → Step A〜D を主contextで実行
```

### 🔴 raw 検証ガード（必須・省略禁止）

**merge agent の収束判定・severity を鵜呑みにしない。🔴 採用前に必ず `raw_log_paths` の該当 raw を主 context で確認する。**

理由（実例）: Lv6-WF 動作確認で merge agent が DS の **🟠「可能性」指摘を「🔴 4者一致の真バグ」に過剰格上げ**（over-attribution）した。raw を grep したところ Codex/Gemini は無指摘、DS は 🟠 だったと判明し、実コード検証で false positive と確定（unfullKeys 無限ループ — `_incrementItemAtIndex` の加算ループは shortageFlag を見ないため再現しない）。

→ **schema 圧縮 + merge 統合は severity/確信度を歪めうる**。raw_log_path 設計はこの検証のために存在する。DS は 5.8KB / Qwen は 2.6KB と軽いので主 context で全文確認可。Codex raw（198KB）は grep で該当箇所だけ抽出する。

### ⚠️ 入力ファイルの罠（必須・Lv7-WF 実走で発覚）

Lv7-WF 実走で **3連鎖事故** が起きた。**起動前後の確認を怠ると別プロジェクトをレビューしても気づけない**:

1. **args 不達**: Workflow に `args` が届かないことがある（実例: `args.label="pickorder-lv7wf"` を渡したのに結果が `label:"target"`=デフォルト値に化けた）。→ スクリプトは `args.input_path` を読めず **デフォルトパスを黙って読む**。
2. **C:/tmp-ai 固定ファイル名の上書き**: 他スキル/セッションが同名ファイルを使い回し、`lv7_codex_input.txt` が別プロジェクト（label designer）のレビュー入力に変質していた。→ デフォルトパスを読んだ結果、pickorder ではなく designer.js をレビューしてしまった。
3. **黙ってデフォルト読み**: args 不達時に halt せずデフォルトを読むため、誤対象に気づけない（Lv6-WF はデフォルトが偶然正しい `review_input.txt` のままで救われていた）。

**対策（実装済 + 必須運用）**:
- ✅ **args 不達は解決済み**: 真因は「Workflow が args を **JSON 文字列**で渡す」こと（プローブ workflow で実証: `typeof args === 'string'`、`JSON.parse` で完全復元可）。両スクリプト冒頭に `JSON.parse` フォールバックを実装済みで `input_path`/`label` が効くようになった。
- ✅ **固定ファイル名の上書きはユニーク名で根絶**: 上記「起動手順」の通り `cgd_in_<RUN>.txt` 等のユニークサフィックス（主 context で `date` 生成）を使う。`label` にも `<RUN>` を含め raw もユニーク化。これで他作業・並行実行と衝突しない。
- **起動後の `label` 確認は継続**: 戻り値の `label` が渡した `<対象名>_<RUN>` と一致するか確認（`target` なら JSON.parse 失敗 = args 経路が壊れた合図）。
- フォールバックのデフォルトパス（`review_input.txt` 等）はスクリプトに残してあるが、**ユニーク名 + args を正規ルート**とする。

### 費用集計

戻り値 `usage[].usage_line`（DS/Qwen の `[Usage]` 行）から Step D の費用表を組み立てる。Codex/Gemini はサブスクで ¥0（呼出回数のみ記録）。Workflow の usage 集計が手作業転記より正確。

### スコープ判断

- **Lv1-3**: workflow 化しない（元々 30-50KB と軽量・overhead が見合わない）
- **Lv4-5**: 当面インライン（直列相談段の pipeline 化は将来課題）
- **Lv6-7**: review 段を workflow 化推奨（主 context 圧迫の主要因がここ）

---

## critic 観点（辛口ユーザー視点 / あるべき論）— 全 Lv 共通オプション

技術レビュー（Codex/Gemini/DS/Qwen の reviewer）は「バグ・設計・セキュリティ」を見るが、**critic 観点は別軸**:「**使う人が困らないか**」「**本来この仕様はどうあるべきか**」を否定的に評価する。技術的に正しくても使い勝手が悪い・仕様が場当たり、という所を辛口で突く。

### いつ使うか / 自動提案ルール（重要）

critic は **Lv に組み込まず、Lv と直交するオプション**。技術レビュー（Lv1-7）の強度とは別軸（使えるか/あるべきか）。Lv に埋めると DS advisor のように選択肢の奥で埋もれて使われなくなるため、**「使うべき時に Claude が提案する」運用**で能動的に活かす。

**起動契機（3通り）**:
- ユーザーが「辛口で」「ユーザー視点で」「あるべき論で」「現場目線で」「critic」等を指示 → 即有効
- Step 1 のレベル選択時に「critic 観点も追加するか」を確認（既定オフ）
- **Claude からの自動提案 → ユーザーが OK したら実行**（下記）。**勝手に実行しない・提案止まり**

**Claude が critic を自動提案すべきタイミング**:
以下を検知したら **AskUserQuestion で「技術面とは別に critic（辛口ユーザー視点+あるべき論）でも見ておきますか？」と一度だけ提案**する:
1. **実装前の仕様・設計の検討**（最重要 — 手戻り防止効果が最大。作る前に「使えるか/あるべきか」を潰す）
2. **新機能・UI・画面・操作フローの追加/変更**（現場担当者・エンドユーザーが触る部分）
3. **ユーザー向けメッセージ・エラー文言・確認ダイアログの追加/変更**
4. 技術レビュー（Lv6/Lv7 等）が「ほぼ問題なし」だったが、ユーザーが実際に使う機能の場合（「動くが使えるか」の最終チェック）

**自動提案しない（critic 不要）ケース**:
- 純粋なバグ修正・内部リファクタ・ロジックのみの変更（UX に関わらない）
- ライブラリ更新・設定変更・テスト追加など、ユーザー操作に影響しない作業

**提案の作法**:
- 提案は **AskUserQuestion でクリック選択**（例: 「critic も実行 / 不要」）。テキストで「やりますか？」と聞かない
- ユーザーが OK → critic 実行（Claude 本体 + DS critic）。不要 → スキップして本来の作業を続行
- **しつこく繰り返さない**（1 タスクにつき提案は原則 1 回。断られたら同一タスク中は再提案しない）

**使えるフェーズ**: 仕様評価（実装前）・コードレビュー（実装後）・単独、どこでも（Lv と直交）。最も価値が高いのは **実装前**。

### 担い手（2者）

| 担い手 | 役割 | 呼び出し |
|---|---|---|
| **Claude 本体** | 現場担当者になりきり使い勝手の不満を生の言葉で + あるべき論 | （本体） |
| **DS critic role** | 推論で「なぜこの仕様」「本来こうあるべき」を批判 | `python "<絶対パス>/deepseek_coder.py" --role critic "<入力ファイル>"` |

- DS critic は深掘りしたいとき `--model deepseek-reasoner`（コスト2倍弱・あるべき論が深くなる）
- Qwen / Codex は critic に使わない（日本語業務文脈・あるべき論は Claude/DS が適任）

### 実行手順
1. 対象（仕様説明 / 差分 / 画面の説明）を **ファイルに準備**（`C:/tmp-ai/critic_input.txt`。argv 肥大回避・ファイル読ませ方式）
2. DS critic を Bash で呼ぶ:
   ```bash
   python "C:/ClaudeCode/.claude/tools/deepseek_coder.py" --role critic "C:/tmp-ai/critic_input.txt"
   ```
3. Claude 本体も「現場担当者 + あるべき論」で辛口評価する（外部出力を待つ間に並行可）
4. 2者を統合した **critic 表**で出力。DS の `[DS Usage]` 行を費用集計に転記

### 出力フォーマット（技術レビューと別表）

| 観点（困り度 高/中/低） | Claude（現場 / あるべき） | DS critic | 採用 |
|---|---|---|---|

- 列挙する軸: **現場の不満**（使う人視点）/ **あるべき論とのギャップ** / **そもそも論**（要る機能か・優先度）
- severity（🔴🟠🟡）でなく **困り度（高/中/低）** で表現 — 技術 OK でもユーザー視点 NG を可視化する
- 表の前後に **辛口総評（一番の問題を断言・1〜2行）** と **改善の方向（箇条書き）**

### 注意
- **擁護・肯定・「概ね良い」は禁止**。甘い評価は無価値。粗探しに徹する
- 技術バグは reviewer 担当。critic は **使い勝手とあるべき論に集中**（バグに深入りしない）
- 否定で終わらせず、各指摘に **改善の方向を短く添える**（代替案の詳細出しは advisor の担当）
- critic は **採否を強制しない** — 「ユーザーが困る/あるべき姿と違う」という視点を可視化するのが目的。最終判断はユーザー

---

## Step A: 実装フェーズ（Lv3-7 共通）

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

## Step B: 検証フェーズ（Lv3-7 共通、CLAUDE.md「コーディング後の必須検証」を強制）

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

## Step C: Codex 再レビュー（Lv3-7 共通、差分のみ・Lv7 も medium 単独で OK）

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
- Lv6: Step C2 へ進む（Lv5 と同仕様）
- Lv7: Step C2 へ進む（Lv5 と同仕様・再レビューも medium 単独）

---

## Step D: 最終まとめ（共通・必須出力）

以下を 1 つの報告にまとめる:

1. **実装した内容** — 変更ファイル（絶対パス）と主要な変更点を箇条書き
2. **検証結果** — Step B の表
3. **再レビュー結果**（Lv3-7）— Step C の 3 列レビュー表 + 修正ループ周回数（Lv5・Lv6・Lv7 のみ、0 / 1 / 中断理由）
4. **未対応指摘**（あれば） — 🟠 / 🟡 で残ったもの・Lv3-4 で残った 🔴
5. **残課題・申し送り事項**（あれば）
6. **💰 費用集計**（**全 Lv 必須・省略禁止**） — 後述「費用集計の出力フォーマット」に従う

```bash
cp <最終報告.md> "C:/tmp-ai/cgd_impl_$(date +%Y%m%d_%H%M%S).md"
```

### 費用集計の出力フォーマット（全 Lv 必須・省略禁止）

Step D の最終報告に **「💰 費用集計」セクション**を必ず含める。
Lv1-3 でも Codex / Gemini の呼出回数は記録する（サブスククォータ消費量の把握目的）。
Lv4-5 / Lv6 は DS / Qwen の従量課金もあるので必須中の必須。

**1. 今回のセッションで発生した費用表（必須）**:

| AI | 呼出回数 | 今回入力 tok | 今回出力 tok | 今回費用 |
|---|---|---|---|---|
| Codex | N 回 (どの Step か注記) | — | — | ¥0（ChatGPT サブスク・料金可視化なし） |
| Gemini | N 回 (どの Step か注記) | — | — | ¥0（Google サブスク・料金可視化なし） |
| DeepSeek | N 回 | 計 X tok | 計 Y tok | ¥A |
| Qwen | N 回 | 計 X tok | 計 Y tok | ¥B |
| **合計（従量課金分）** | — | — | — | **¥A+B** |

- **呼出回数**: 今回のスキル実行中の Bash 呼出を Claude が記録（Step 2-XX / Step C / Step C2 のうちどこで呼んだか注記）
- **DS / Qwen の入力・出力・費用**: stderr の `[DS Usage] 今回:` / `[Qwen Usage] 今回:` の数値を **複数回呼んだ場合は積算**
- **Codex / Gemini**: サブスク認証で料金可視化不可。ただし呼出回数だけは計上する（無料という意味ではなくクォータを消費している）
- 呼ばなかった AI の行は省略可（ただし合計行は必ず出す）

**2. セッション累計（参考、過去 4 時間以内）**:

```
[DS Usage]   累計: ... (このセッション+過去呼出含む)
[Qwen Usage] 累計: ... (このセッション+過去呼出含む)
```

取得コマンド（必要に応じて Step D 出力前に Bash で実行）:
```bash
python "C:/ClaudeCode/.claude/tools/deepseek_coder.py" --show-session
python "C:/ClaudeCode/.claude/tools/qwen_advisor.py" --show-session
```

セッション累計は今回の作業以外（過去 4 時間以内の別作業）を含むため、**当該作業の正確な費用は「1. 今回費用表」が真実のソース**。

**3. 費用観点の所感（任意・1〜3 行）**:

例: 「Lv6 を 1 周フルで回して従量課金 ¥0.99。Codex/Gemini はサブスク内なので追加コストなし。検出 🔴 4 件に対し費用対効果は高い」

---

**省略禁止の理由**:
- ユーザーは Lv5/Lv6 など高コストレベルの継続判断に費用情報が必要
- セッション内で複数回 Lv6 を回したときの累計把握
- Step 2-XB での「今回」表示と Step D の最終まとめは別目的（前者は即時可視化、後者は確定報告）
- Lv1-3 でも Codex 呼出回数の蓄積は ChatGPT クォータ消費の目安になる

**書き忘れたら**:
ユーザーから「費用の表示がない」と指摘されたら、その時点で `--show-session` を取得して費用表を後追い追加する。同じ指摘を受けないよう Step D 出力前に必ずチェック。

---

## Bash タイムアウト

| ステップ | コマンド | timeout |
|---|---|---|
| Lv2-2B / Lv4-2A / Lv6-2B / Lv7-2D | gemini | 180000 (3分) |
| Lv4-2C | deepseek (advisor) | 120000 (2分) |
| Lv4-2C | qwen (advisor) | 120000 (2分) |
| Lv6-2B / Lv7-2D | deepseek (reviewer) | 180000 (3分) |
| Lv6-2B / Lv7-2D | qwen (reviewer) | 180000 (3分) |
| Lv1-2B / Lv2-2B / Lv4-2E / Lv6-2B / Lv7-2D / C | codex medium | 300000 (5分) |
| Lv1-2B / Lv2-2B / Lv4-2E / Lv6-2B / **Lv7-2D** / C | codex high | 600000 (10分) |
| Step B | python -c "from <mod> ..." | 60000 (1分) |

---

## 認証エラー検出時の挙動（必須）

Codex CLI / Gemini CLI / DeepSeek API / DashScope (Qwen) API の **いずれか一つでも認証エラーを返したら、そこで即中断**する。残り段を片肺で続行しない・自動で別ツールに切り替えない。

**検出シグナル（例）**:
- Codex: `Not logged in` / `401` / `unauthorized`
- Gemini: `/auth` 要求 / `401` / `permission denied`
- DeepSeek: `openai.AuthenticationError` / `401 Unauthorized` / `invalid api key` / `DEEPSEEK_API_KEY が設定されていません`
- Qwen (DashScope): `openai.AuthenticationError` / `401 Unauthorized` / `InvalidApiKey` / `DASHSCOPE_API_KEY が設定されていません`

**中断時にユーザーへ報告する内容**（1〜3 行）:
1. どの API で何のエラーが出たか
2. 復旧手順（`codex login` / `gemini` 対話起動の `/auth` / `DEEPSEEK_API_KEY` の設定確認 / `DASHSCOPE_API_KEY` の設定確認）
3. 復旧後に `/cgd` を再実行する旨

**特にやってはいけないこと**:
- Lv2 で片方が認証エラーのまま、もう片方の出力だけで 5 列統合表を作る
- Lv4-5 で途中段の認証エラーを無視して中間結果を「結論」として扱う
- 別の API（例: 認証成功している側）で代用して続行する
- Lv4-5 の Step 2-4C で DS と Qwen 片方だけ成功した場合、もう片方なしで Step 2-4D に進む（必ず両方成功してから次段へ）
- Lv6 の Step 2-6B で 4 者のうち 1 者でも認証エラーが出た場合、残り 3 者の結果で 6 列統合表を作る（必ず 4 者全部成功してから Step 2-6C へ。Lv6 は「4 者すべての視点」が価値の本質なので 1 者欠けたら Lv2/Lv4 と同等になり Lv6 の意味が消える）
- Lv7 の Step 2-7D で 5 者のうち 1 者でも認証エラーが出た場合、残り 4 者の結果で 7 列統合表を作る（必ず 5 者全部成功してから Step 2-7E へ。特に **Codex medium と Codex high の両方** が成功している必要があり、片方失敗で代用すると Lv7 の本質である Codex 多重が崩れる）

---

## 注意事項

- **直列実行**: Lv4-5 の相談段（2-4A→F）は基本的に順序が重要（前段の出力を後段に渡す）。ただし **Step 2-4C の DS と Qwen は同じ入力に対する別意見取得なので並列**
- **並列実行**: Lv2 と Lv3 の C+G レビュー段（Bash 2 個）、Lv4-5 の Step 2-4C（DS+Qwen 並列、Bash 2 個）、Lv6 の Step 2-6B（C+G+DS+Qwen 4 者並列、Bash 4 個）、および **Lv7 の Step 2-7D（Codex(med) + Codex(high) + G + DS + Qwen の 5 者並列、Bash 5 個）** は 1 メッセージ内で並列起動
- **DS/Qwen 役割の Lv 別整理**:
  - Lv1-3: 呼ばない
  - Lv4-5: `--role advisor`（別案出し・Gemini と Claude の検討要約を渡す）
  - Lv6: `--role reviewer`（Codex / Gemini と同じ原データ＝差分のみを直接渡す並列レビュー・要約は使わない）
  - Lv7: `--role reviewer`（差分 + **関連関数抜粋**（Claude が事前抽出）を渡す補助レビュー・表層指摘を減らす）
- **Codex 多重 (Lv7)**: medium と high を **同じ入力で並列実行**。reasoning level の違いから別視点を取得。再レビュー（Step C）は medium 単独で OK
- **関連関数の事前抽出 (Lv7)**: Claude が grep + Read で関数境界を見つけて抜粋し、`C:/tmp-ai/lv7_related_funcs.txt` に結合保存してから DS/Qwen に渡す。差分の 5〜10 倍程度のサイズが目安
- **再レビュー（Step C）は Codex 単独**: トークン節約のため Gemini は呼ばない
- **差分のみ**: Step C は `git diff` を渡し、全ファイルは渡さない（時間とコスト圧縮）
- **スキル連鎖禁止**: 本スキル内から `/codex` や別スキルを Skill ツール経由で自動呼び出ししない
- **相談段は read-only**: Codex `--sandbox read-only`、Gemini `-p` 非対話、DS は API 単発呼び出し
- **書き込みフェーズは Step A のみ**: ファイル編集・新規作成は **Step A に集約**。Codex / Gemini / DS / Qwen には絶対に書き込ませない
- **API キー**: DS は `DEEPSEEK_API_KEY`、Qwen は `DASHSCOPE_API_KEY` を読む。Codex/Gemini はサブスク認証（`OPENAI_API_KEY` / `GEMINI_API_KEY` を環境変数にセットしない）
- **機密情報**: 顧客データ・社内 DB 接続情報を不必要に外部 API（DS / Qwen / Gemini）に渡さない。特に Qwen は DashScope International（Singapore リージョン）に送信されることを意識する
- **DS / Qwen パスは絶対パス**: 相対 `.claude/tools/...` は CWD=`C:/tmp-ai` で解決失敗するので必ず絶対パス
- **実装フェーズの規約強制**: Step A では AGENTS.md / CLAUDE.md ルール（バックアップ・shebang 禁止・`encoding="utf-8"` 明示・日本語パスは Python スクリプト経由・.bat は CP932）を必ず守る
- **検証フェーズの省略禁止**: Step B で `ast.parse` だけで済ませず、必ず実 import まで実行
- **Lv5 自動修正ループ上限**: 最大 1 周。改善なし / 新規🔴 / 上限到達のいずれかで停止しユーザー判断
- **巨大対象は要点抽出 + ファイル読ませ**: Codex / Gemini に丸投げせず Claude Code 側で要点抜粋。さらに **プロンプトを argv で渡さずファイル経由で読ませる**（`"$(cat file)"` は入力が大きいと `Argument list too long`（ARG_MAX 超過）になる。`"まず <file> を読み…"` 形式で Codex/Gemini にファイルを読ませる）。Gemini は cwd=`C:/tmp-ai` の workspace 内ファイルのみ読めるので、対象は必ず `C:/tmp-ai` に置く

---

## トラブルシュート

- **DS が `DEEPSEEK_API_KEY が設定されていません`** → 環境変数を確認
- **DS のレスポンスがコード生成っぽい** → `--role advisor` または `--role reviewer` 付け忘れ。デフォルトは `coder`
- **DS reviewer が別案を返す（advisor 風出力）** → Lv6 で `--role advisor` のまま実行している。必ず `--role reviewer` を付ける
- **DS が「ファイルが見つかりません」** → 相対パス指定の罠。絶対パスで指定し直す
- **Qwen が `DASHSCOPE_API_KEY が設定されていません`** → 環境変数を確認（Singapore リージョンのキーであることも確認、US/中国本土キーとは非互換）
- **Qwen が `InvalidApiKey` / `401`** → キーとエンドポイントのリージョン不一致。Singapore/US Virginia/China Beijing は**非互換**。環境変数 `QWEN_BASE_URL` でエンドポイント切替:
  - Singapore (既定): `https://dashscope-intl.aliyuncs.com/compatible-mode/v1`
  - US Virginia: `https://dashscope-us.aliyuncs.com/compatible-mode/v1`
  - China Beijing: `https://dashscope.aliyuncs.com/compatible-mode/v1`
- **Qwen のレスポンスが英語コード** → `--role advisor` または `--role reviewer` 付け忘れ。Qwen のデフォルトは `advisor` だが明示推奨
- **Qwen reviewer が別案を返す（advisor 風出力）** → Lv6 で `--role reviewer` を明示しないと既定の `advisor` が選ばれる。必ず `--role reviewer` を付ける
- **Lv6 で 4 者並列のうち 1 者が認証エラー** → 即中断（Lv6 は 4 者揃ってこそ意味があるので 3 者で続行しない）。該当 API の復旧後に再実行
- **Lv7 で 5 者並列のうち Codex 片方 (med or high) が認証エラー** → 即中断（Lv7 の本質は Codex 多重なので片方では成立しない）。`codex login status` を確認し復旧後に再実行
- **Lv7 で関連関数抜粋が大きすぎてタイムアウト** → 抜粋を「変更 hunk 直近 ± 30 行 + 直接の呼出元 1〜2 関数」に絞る。argv 制限 32KB 以下を目標
- **Lv7 の Codex high が 10 分でタイムアウト** → 入力プロンプトを「対象ファイル絶対パス + 変更概要 + 差分」に圧縮（関数定義は Codex が sandbox で読みに行く想定なので Claude から渡さない）
- **Codex `Not logged in`** → `codex login` の実行を依頼して停止
- **Gemini 認証エラー** → 一度 `gemini` を対話起動して `/auth` でログインを案内し、停止
- **`Argument list too long`（Codex/Gemini 起動時）** → 大きい入力を `"$(cat 'file')"` で argv 展開して ARG_MAX 超過（62KB で実発生）。**Codex/Gemini にはファイルパスを渡して自分で読ませる**短いプロンプト（`codex exec ... "まず C:/tmp-ai/review_input.txt を読み…"` / `gemini -p "まず … を読み…"`）にする。DS/Qwen はファイルパス引数なので影響なし。Gemini はファイルを `C:/tmp-ai`（workspace 内）に置くこと
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
| 複眼レビューで盲点を潰したい（4 者の視点を全部欲しい） | Lv6 |
| Lv4-5 を試したが DS/Qwen の別案が機能しなかった対象の代替 | Lv6 |
| 最重量複眼レビュー（Codex / Gemini / DS / Qwen の 4 者で同じデータを評価） | Lv6 |
| **Integration バグ・関数間の暗黙の前提違反・スコープを跨いだ状態管理の検査** | **Lv7** |
| **大規模 IIFE / モジュール内のクロスリファレンス整合性チェック** | **Lv7** |
| **Lv6 で Codex 単独指摘が多く、他者からの追加発見が少なかった対象の深掘り** | **Lv7** |
| **長大ファイル（5000 行〜）に対する厳密なレビューが必要・diff だけだと文脈不足** | **Lv7** |

---

## スタンプ運用ルール（Step 0 のバージョン照合用）

冒頭の `<!-- SKILL_VERSION: YYYY-MM-DD_HHMMSS -->` は、Step 0 で「セッションのコンテキストが最新か」を軽量に判定するためのスタンプ。

- **このスキル（cgd/codex/critic のいずれか）を編集したら、必ずスタンプを実時刻で更新する**
  - 実時刻は **必ず `date '+%Y-%m-%d_%H%M%S'` で取得**（手入力の固定値は禁止・DEPLOY_TIME ルール準拠）
  - cgd / codex / critic は連動するので、**どれか1つを編集したら3ファイルとも同じスタンプに揃える**（Step 0 の照合が崩れないように）
- スタンプは frontmatter（`---` の閉じ）直後の1行に置く（`grep -m1 'SKILL_VERSION'` で確実に拾える位置）
- Step 0 はこのスタンプ1行だけ grep するので、900 行を毎回読まずに最新判定できる
- `/g-ul` で claude-shared に push する前にもスタンプ更新を確認（他端末の Step 0 が「最新」と誤判定しないように）
