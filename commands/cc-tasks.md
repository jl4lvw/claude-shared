---
description: Google Tasks の「ClaudeCode連携」リストを順に処理するスキル
---

# /cc-tasks — ClaudeCode連携タスク処理スキル

Google Tasks の「ClaudeCode連携」リストを読み込み、1件ずつ確認しながら処理する。

---

## ▶ 起動時の動作（必須）

### Step 1: タスク一覧を取得

```python
import sys
sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, r"C:\Users\jl4lv\OneDrive\デスクトップ\0.フジ\900.ClaudeCode\006.secretary\scripts")
from tasks_api import get_tasks_service

service = get_tasks_service()
lists_result = service.tasklists().list().execute()

target_id = None
for tl in lists_result.get("items", []):
    if "ClaudeCode" in tl["title"]:
        target_id = tl["id"]
        break

if target_id:
    result = service.tasks().list(
        tasklist=target_id,
        showCompleted=False,
        showHidden=False,
        maxResults=100,
    ).execute()
    tasks = result.get("items", [])
    for t in tasks:
        print(f'ID:{t["id"]}  TITLE:{t["title"]}')
        if t.get("notes"):
            print(f'  NOTES:{t["notes"]}')
else:
    print("ClaudeCode連携リストが見つかりませんでした")
```

### Step 2: 理解・アクション一覧を表形式で表示

取得したタスクを分析し、以下の表を作成してユーザーに提示する：

| # | タスク内容（要約） | Claudeの解釈 | 想定アクション | 対象ファイル/フォルダ |
|---|---|---|---|---|
| 1 | ... | ... | ... | ... |

- 「Claudeの解釈」: タスク文を読んで何をすべきか自然言語で説明
- 「想定アクション」: 具体的に何をするか（コード修正・新規作成・設定変更 等）
- 「対象ファイル/フォルダ」: 変更対象のパスを特定して記載。不明な場合は「要調査」と記載し、調査してから埋める

表の表示後、以下を一言添える：
「以上 N 件です。1件目から処理を始めますか？」

---

## ▶ タスク処理ループ（1件ずつ）

表示後、ユーザーの「はい」「次へ」「進めて」等の許可を受けて1件目から処理する。

### 各タスクの処理手順

**[確認フェーズ]**

処理前に以下を提示してユーザーの許可を得る：

```
── タスク N / 全N件 ──────────────────────────
タスク: <タスク全文>

■ 解釈
  <このタスクで何をすべきかの説明>

■ 対象ファイル
  <変更するファイルのフルパス or フォルダ>

■ 実施内容
  - <具体的な変更点1>
  - <具体的な変更点2>
  ...

この内容で実装してよいですか？ [はい / スキップ / 中断]
```

- **はい / OK / 進めて** → 実装開始
- **スキップ** → このタスクを飛ばして次へ
- **中断** → スキルを終了する（タスクは未完了のまま残す）

**[実装フェーズ]**

許可を得たら実装を行う。CLAUDE.md の検証ルールに従い、実装後は必ず動作確認を行う。

**[完了マークフェーズ]**

実装が成功したら、Google Tasks 上のタスクを完了にする：

```python
import sys
sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, r"C:\Users\jl4lv\OneDrive\デスクトップ\0.フジ\900.ClaudeCode\006.secretary\scripts")
from tasks_api import complete_google_task

complete_google_task("<task_id>", tasklist_name="ClaudeCode連携")
print("✅ タスク完了マーク済み")
```

完了後、次のタスクがあれば「次のタスクに進みますか？」と確認して続ける。

---

## ▶ 終了時

全タスクを処理（または中断）したら：
- 処理結果のサマリーを表示（完了 N件 / スキップ N件 / 残り N件）
- 「ClaudeCode連携リストの処理が完了しました。」と報告する

---

## ⚠️ 注意事項

- `complete_google_task` の `tasklist_name` は必ず `"ClaudeCode連携"` を指定する
- 実装前に必ず対象ファイルを Read ツールで確認する（推測で変更しない）
- 対象ファイルが不明な場合はコードベースを検索してから確認フェーズに入る
- 日本語パスへの Edit/Write は失敗する可能性があるため、必要に応じて Python スクリプト経由で書き込む
