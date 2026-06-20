---
name: codex
description: Codex CLI（OpenAI）と Gemini CLI（Google）を使ったコードレビュー・セカンドオピニオン・調査を取り込むスキル。**本スキルは `/cgd` と統合されており、`/codex` 起動でも `/cgd` と完全に同じフロー（Lv1〜7 の強度レベル選択）が動く**。Lv1=Codex 単独 / Lv2=Codex+Gemini 並列（旧 /codex 等価・既定推奨） / Lv3=Lv2+実装後再レビュー / Lv4=フル相談（Gemini→[DS+Qwen 並列 advisor]→Codex）+再レビュー / Lv5=Lv4+🔴自動修正1周 / Lv6=C+G+DS+Qwen 4者並列レビュー（全員 reviewer 役）+実装+検証+Codex再レビュー+🔴自動修正1周 / Lv7=Codex多重(medium+high)+補助(G/DS/Qwen)の5者並列「Codex集中」+実装+検証+Codex再レビュー+🔴自動修正1周（最深掘り・integrationバグ重視）。Lv4-5 は DS/Qwen を advisor 役で別案出し、Lv6 は 4 者横並び reviewer、Lv7 は Codex 多重化+DS/Qwenに関連関数抜粋を渡して補助役を強化（Lv6 で Codex 単独指摘が多かった反省から派生）。差分レビュー、設計判断の第三者チェック、バグ疑いの検証、長文ログ解析、リサーチに使う。サブスク認証（ChatGPT / Google ログイン）で動作し API キーは不要（Lv4-5 / Lv6 / Lv7 は DEEPSEEK_API_KEY と DASHSCOPE_API_KEY 必要）。全Lv共通の任意オプションで『critic観点』（辛口ユーザー視点＝現場担当者の使い勝手の不満 + あるべき論＝本来どうあるべきかの批判を Claude本体+DS criticで評価）も使え、技術的正しさとは別軸で否定的にチェックする。「Codex にレビュー」「セカンドオピニオン」「C+G」「cg」「cgd」「3者に相談」「4者レビュー」「Codex多重」「Codex集中」「辛口レビュー」「ユーザー視点」「あるべき論」「critic」などのキーワードで起動。
---
<!-- SKILL_VERSION: 2026-06-19_131428 -->

# codex — `/cgd` 統合エイリアス

このスキルは **`/cgd` と完全統合**されました。`/codex` 起動でも `/cgd` 起動でも、まったく同じスキルフロー（Lv1〜7 の強度レベル選択 + 全 Lv 共通の critic 観点オプション）が動きます。

## 必ず行うこと（起動時の順序）

1. **まず最新確認（Step 0）**: `C:/ClaudeCode/.claude/skills/cgd/SKILL.md` の **Step 0（起動時のスキル最新確認）** を実行する。
   - バージョンスタンプ（`grep -m1 'SKILL_VERSION' cgd/SKILL.md`）を確認 → コンテキストの版と違う / 未読なら **Read で読み直し**
   - claude-shared(Git) に未取込更新があれば「`/g-dl` で取り込めます」と**通知のみ**
2. `.claude/skills/cgd/SKILL.md` を Read で読み込み、**Step 0 → Step 1（レベル選択）→ … の手順をそのまま実行**する。

旧 `/codex` 単体（Codex のみ）や `/gemini` 単体の旧来挙動は**廃止**されています。旧 /codex の C+G 並列レビューが欲しい場合は、Step 1 のレベル選択で **Lv2** を選んでください。

## なぜ統合したか

- `/codex`（C+G 並列レビュー）と `/cgd`（統合相談＋実装＋検証）が機能的に重複していた
- ユーザーが「どれくらい外部 AI を使うか」を 1 軸の強度レベル（Lv1〜7）で選べた方が直感的
- スキル名の表記揺れ（codex / cgd / cg / 3者）を統一できる

## このファイルを読んでいる Claude へ

1. 先に cgd/SKILL.md の **Step 0** で最新確認（スタンプ照合 → 必要なら Read 読み直し / claude-shared 未取込なら通知）
2. cgd/SKILL.md を Read で開き、**Step 1（レベル選択）から実行**する（既定 Lv2）
3. **スキル連鎖禁止**: Skill ツールで `/cgd` を呼ばない。Read で読み込み、本体の指示に従って自分で手順を実行する
