---
name: hq-tick
description: 司令塔(/hq)の見張り番 1 回分。状態ボードの差分コマンドを 1 回実行し、新しく止まったセッションがあれば 1 行通知する。`/hq watch` が `/loop` 経由で呼ぶ軽量版(判断・助言・中継はしない。それらは /hq)。
---

# /hq-tick — 見張り番 1 回分(軽量)

`<SID>` は system prompt の scratchpad パス `...\C--ClaudeCode-900-ClaudeCode\<SID>\scratchpad` の `<SID>`。

このコマンドで実行するのは次の 1 つだけ(他のツール・ファイル読取・トランスクリプト閲覧はしない):

```
python C:/ClaudeCode/900.ClaudeCode/.claude/tools/hq_board_cli.py diff --commit --self <SID> --remote
```

出力の扱い:
- `NOCHANGE` → 「変化なし」とだけ答える
- `NEW` / `STILL` 行あり → `PushNotification` で 1 行(例: `🔐許可待ち 12分: ■外注手配 / 💬回答待ち 3分: SGW経理`)。
  Discord が ON なら同じ文を `python C:/ClaudeCode/900.ClaudeCode/006.secretary/scripts/discord_d.py send "<文>"` でも送る
  (ON/OFF は `discord_d.py status`)。その後、NEW/STILL 行をそのまま列挙する
- `OK` 行のみ → 通知せず「〇〇の待ちが解消」と 1 行

助言・回答の中継・代理承認はこのコマンドでは行わない(必要なら `/hq`)。
