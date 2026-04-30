---
description: AI秘書のショートカット（/secretary と同じ）
---

/secretary と同じ動作をする。
briefing.py を実行してブリーフィングを出力し、指示を待つ。

実行コマンド:
```
python "C:\Users\jl4lv\OneDrive\デスクトップ\0.フジ\900.ClaudeCode\006.secretary\scripts\briefing.py"
```

ブリーフィング出力後、**必ず以下の PNG/PDF 出力も同時に実行すること**（省略禁止）:
```
python "C:\Users\jl4lv\OneDrive\デスクトップ\0.フジ\900.ClaudeCode\006.secretary\scripts\png_export.py" briefing
```
保存先: C:\Users\jl4lv\Downloads\briefing_日時.png / briefing_日時.pdf
保存完了を以下の形式で1行添えて報告する:
「📸 PNG/PDF保存: briefing_日時.png / briefing_日時.pdf」

出力後、「何かご指示はありますか？」と添えてユーザーの入力を待つ。
タスク・スケジュール操作は /secretary の手順に従う。
