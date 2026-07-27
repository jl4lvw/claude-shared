---
name: m
description: relay(Claude間連携API)の未読メッセージをチェック・処理する `/message-check` の短縮エイリアス。「/m」で起動。
---

# m — `/message-check` エイリアス

このスキルは **`/message-check` と完全に同じ動作**をする短縮名です。

## このファイルを読んでいる Claude へ

1. `.claude/skills/message-check/SKILL.md` を **Read** で読み込む。
2. その手順(未読取得 → グルーピング → 処理方針 → 完了後の返信)をそのまま実行する。
3. **スキル連鎖禁止**: Skillツールで `/message-check` を呼ばない。Readで読み込み、本体の指示に従って自分で手順を実行する。
