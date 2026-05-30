---
name: sr
description: Skill Reload。既存セッション内で `C:/ClaudeCode/.claude/skills/cgd/SKILL.md` を再読み込みして、cgd/codex スキルの最新定義を反映する。`/codex` や `/cgd` を更新した直後に、セッションを再起動せずに新しい挙動を取り込みたいときに使う。
trigger: ユーザーが /sr を実行したとき
---

# Skill Reload (`/sr`)

## 目的
既存セッションで `C:/ClaudeCode/.claude/skills/cgd/SKILL.md` を **Read ツールで再読み込み** するだけのスキル。

`/codex` `/cgd` などの SKILL.md を編集した直後でも、新セッションを開かずに最新定義を反映できる。

## 手順
1. `Read` ツールで `C:/ClaudeCode/.claude/skills/cgd/SKILL.md` を全文読み込む。
2. 読み込んだ内容を以後の `/cgd` `/codex` 起動時の挙動として採用する。
3. ユーザーには「cgd SKILL.md を再読み込みしました（バージョン/更新日時など分かる範囲）」と短く報告する。

## 注意
- 他のスキル (handoff, mail 等) は対象外。必要なら明示的に依頼してもらう。
- 本スキル自身は引数を取らない。
