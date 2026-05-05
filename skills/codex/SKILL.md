---
name: codex
description: Codex CLI（OpenAI）と Gemini CLI（Google）を使ったコードレビュー・セカンドオピニオン・調査を取り込むスキル。**本スキルは `/cgd` と統合されており、`/codex` 起動でも `/cgd` と完全に同じフロー（Lv1〜5 の強度レベル選択）が動く**。Lv1=Codex 単独 / Lv2=Codex+Gemini 並列（旧 /codex 等価・既定推奨） / Lv3=Lv2+実装後再レビュー / Lv4=フル相談（Gemini→DS→Codex）+再レビュー / Lv5=Lv4+🔴自動修正1周。差分レビュー、設計判断の第三者チェック、バグ疑いの検証、長文ログ解析、リサーチに使う。サブスク認証（ChatGPT / Google ログイン）で動作し API キーは不要（Lv4-5 は DEEPSEEK_API_KEY 必要）。「Codex にレビュー」「セカンドオピニオン」「C+G」「cg」「cgd」「3者に相談」などのキーワードで起動。
---

# codex — `/cgd` 統合エイリアス

このスキルは **`/cgd` と完全統合**されました。`/codex` 起動でも `/cgd` 起動でも、まったく同じスキルフロー（Lv1〜5 の強度レベル選択）が動きます。

## 必ず行うこと

`.claude/skills/cgd/SKILL.md` を Read ツールで読み込み、**そこに書かれた手順をそのまま実行**してください。

`/codex` 単体（Codex のみ）や `/gemini` 単体の旧来挙動は**廃止**されています。後方互換はありません。

旧 `/codex` の挙動（C+G 並列レビュー）が欲しい場合は、Step 1 のレベル選択で **Lv2** を選んでください。

## なぜ統合したか

- `/codex`（C+G 並列レビュー）と `/cgd`（3者統合相談＋実装＋検証）が機能的に重複していた
- ユーザーが「どれくらい外部 AI を使うか」を 1 軸の強度レベル（1〜5）で選べた方が直感的
- スキル名の表記揺れ（codex / cgd / cg / 3者）を統一できる

## このファイルを読んでいる Claude へ

1. `.claude/skills/cgd/SKILL.md` を Read で開く
2. その Step 1（レベル選択）から実行を開始する
3. 既定値は Lv2（Codex+Gemini 並列・旧 /codex 等価）

スキル連鎖（Skill ツール経由で /cgd を呼ぶ）は **しないでください**。Read で SKILL.md を読み、本体の指示に従って自分で手順を実行します。
