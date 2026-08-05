---
name: codex
description: Codex CLI（OpenAI）を中心に DeepSeek・Qwen を使ったコードレビュー・セカンドオピニオン・調査を取り込むスキル（Gemini は2026-07にAPIエラー多発のため既定オフのオプトイン参加）。**本スキルは `/cgd` と統合されており、`/codex` 起動でも `/cgd` と完全に同じフロー（Lv1〜8 の強度レベルは Claude が自動選択）が動く**。Lv1=Codex 単独 / Lv2=Codex+DeepSeek 並列（既定推奨。旧 /codex 等価の C+G 構成は「Geminiも」で再現可） / Lv3=Codex+DeepSeekの技術×批評「2社×2視点」4レビュー（実装なし・review専用） / Lv4=フル相談（[DS+Qwen 並列 advisor]→Codex、Gemini併用可）+再レビュー / Lv5=Lv4+🔴自動修正1周 / Lv6=Codex+DS+Qwen 3者並列レビュー（全員 reviewer 役、Gemini併用で4者）+実装+検証+Codex再レビュー+🔴自動修正1周（Workflow必須） / Lv7=Codex多重(medium+high)+補助(DS/Qwen)の4者並列「Codex集中」（Gemini併用で5者）+実装+検証+Codex再レビュー+🔴自動修正2周（最深掘り・integrationバグ重視・Workflow必須） / Lv8=Lv7の技術構成+Codex(high)とDeepSeekへの批評視点追加で6者並列（Gemini併用で7者）+実装+検証+Codex再レビュー+🔴自動修正2周（技術最深掘り+複眼批評、最重量級・Workflow必須）。Lv4-5 は DS/Qwen を advisor 役で別案出し、Lv6 は横並び reviewer、Lv7 は Codex 多重化+DS/Qwenに関連関数抜粋を渡して補助役を強化（Lv6 で Codex 単独指摘が多かった反省から派生）、Lv8 は Lv7 の技術構成に Lv3 の批評視点を合成。差分レビュー、設計判断の第三者チェック、バグ疑いの検証、長文ログ解析、リサーチに使う。Codex はサブスク認証（ChatGPT ログイン）で API キー不要。Lv2 以降は DEEPSEEK_API_KEY、Lv4-8 は DASHSCOPE_API_KEY も必要（Gemini はオプトイン時のみ GEMINI_API_KEY）。全Lv共通の任意オプションで『critic観点』（辛口ユーザー視点＝現場担当者の使い勝手の不満 + あるべき論＝本来どうあるべきかの批判を Claude本体+DS criticで評価）も使え、技術的正しさとは別軸で否定的にチェックする。「Codex にレビュー」「セカンドオピニオン」「C+G」「cg」「cgd」「3者に相談」「4者レビュー」「Codex多重」「Codex集中」「辛口レビュー」「ユーザー視点」「あるべき論」「critic」「Geminiも」などのキーワードで起動。
---
<!-- SKILL_VERSION: 2026-08-05_214817 -->

# codex — `/cgd` 統合エイリアス

このスキルは **`/cgd` と完全統合**されました。`/codex` 起動でも `/cgd` 起動でも、まったく同じスキルフロー（Lv0〜8 の強度レベル + 全 Lv 共通の critic 観点。**どちらも Claude が自動選択**）が動きます。

## 必ず行うこと（起動時の順序）

1. **まず最新確認（Step 0）**: `C:/ClaudeCode/.claude/skills/cgd/SKILL.md` の **Step 0（起動時のスキル最新確認）** を実行する。
   - バージョンスタンプ（`grep -m1 'SKILL_VERSION' cgd/SKILL.md`）を確認 → コンテキストの版と違う / 未読なら **Read で読み直し**
   - claude-shared(Git) に未取込更新があれば「`/g-dl` で取り込めます」と**通知のみ**
2. `.claude/skills/cgd/SKILL.md` を Read で読み込み、**Step 0 → Step 1（レベル決定）→ … の手順をそのまま実行**する。

**レベル・Codex reasoning・Gemini/critic 観点は Claude が自動選択して宣言する**（2026-08-05 変更・ユーザーに選ばせない）。ユーザーが「Lv7 で最も深く」等と明示した場合はそれに従う。実装許可などの承認ゲートは従来どおり確認する。

旧 `/codex` 単体（Codex のみ）や `/gemini` 単体の旧来挙動は**廃止**されています。旧 /codex の C+G 並列レビューが欲しい場合は **「Lv2 + Geminiも」** と明示してください。

## なぜ統合したか

- `/codex`（C+G 並列レビュー）と `/cgd`（統合相談＋実装＋検証）が機能的に重複していた
- 「どれくらい外部 AI を使うか」を 1 軸の強度レベル（Lv0〜8）で表せた方が直感的
- スキル名の表記揺れ（codex / cgd / cg / 3者）を統一できる

## このファイルを読んでいる Claude へ

1. 先に cgd/SKILL.md の **Step 0** で最新確認（スタンプ照合 → 必要なら Read 読み直し / claude-shared 未取込なら通知）
2. cgd/SKILL.md を Read で開き、**Step 1（レベル決定）から実行**する。レベルは判定基準表で**自分で決めて宣言**する（既定 Lv2・ユーザーに聞かない）
3. **スキル連鎖禁止**: Skill ツールで `/cgd` を呼ばない。Read で読み込み、本体の指示に従って自分で手順を実行する
