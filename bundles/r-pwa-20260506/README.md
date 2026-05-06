# r-pwa-20260506 — `/r` 専用 PWA 実装ファイルバンドル

別 PC（OneDrive 同期外の作業 PC）で実装した `/r` スキル専用 PWA の成果物を、
本番 PC（TeamTasks サーバ稼働 PC）に転送するための一式。

ハンドオフ詳細は `files/.handoff/SESSION_20260506_2309_434.md` 参照。
セッション ID: **434**

## 中身

```
files/
├── 014.TeamTasks/
│   ├── server/
│   │   ├── models.py            (上書き：RInstruction / RInstructionVersion 追加)
│   │   ├── db.py                (上書き：PRAGMA WAL/busy_timeout 追加)
│   │   ├── main.py              (上書き：include_router(r_router.router) 追加)
│   │   ├── schemas.py           (上書き：RInstruction* 5 スキーマ追加)
│   │   └── routers/r.py         (新規：7 エンドポイント API)
│   └── pwa/r/
│       ├── index.html           (新規)
│       ├── app.js               (新規)
│       ├── style.css            (新規)
│       ├── manifest.json        (新規)
│       └── sw.js                (新規)
├── .claude/commands/r.md        (上書き：DB 経由に全面書き換え)
└── .handoff/
    └── SESSION_20260506_2309_434.md (引継ぎ詳細)
install.ps1                       (自動展開スクリプト)
README.md                         (このファイル)
```

## 適用手順（本番 PC で実行）

### 1. claude-shared を最新化

```powershell
cd $env:USERPROFILE\claude-shared
git pull
```

### 2. install.ps1 を実行

```powershell
cd $env:USERPROFILE\claude-shared\bundles\r-pwa-20260506
pwsh -ExecutionPolicy Bypass -File install.ps1
```

`install.ps1` は以下を行う：
- プロジェクトルート（`C:\Users\<user>\OneDrive\デスクトップ\0.フジ\900.ClaudeCode`）を自動探索
- 既存ファイルを `.bak_YYYYMMDD_HHMMSS` でバックアップ
- バンドル内の `files/` をプロジェクトに上書き展開
- 適用結果を表示

### 3. 依存パッケージ確認

```powershell
& "C:\Users\user\AppData\Local\Python\pythoncore-3.14-64\python.exe" -c "import fastapi, sqlalchemy, uvicorn, pydantic; from starlette.testclient import TestClient; import multipart; print('OK')"
```

エラーが出たら：

```powershell
& "C:\Users\user\AppData\Local\Python\pythoncore-3.14-64\python.exe" -m pip install python-multipart httpx
```

（`fastapi sqlalchemy uvicorn pydantic` は既存の TeamTasks で使われているはずなので入っているはず）

### 4. TeamTasks サーバ再起動

タスクスケジューラ経由なら、タスクの停止→開始。手動起動なら現在のプロセスを止めて：

```powershell
cd C:\Users\user\OneDrive\デスクトップ\0.フジ\900.ClaudeCode\014.TeamTasks\server
.\start.bat
```

起動ログに `r-instructions` 関連エラーが出ていないことを確認。

### 5. 動作確認

```powershell
# health
curl https://sfuji.f5.si/health

# /r API（空配列が返るはず）
curl https://sfuji.f5.si/tasksapi/r/

# PWA
start https://sfuji.f5.si/tasks/r/
```

スマホ Safari で `https://sfuji.f5.si/tasks/r/` を開き、1 件追加 → PC で `/r 番号` 単発取り込みできるか確認。

### 6. ハンドオフを load

```
/handoff load 434
```

## 残課題（ハンドオフ参照）

- 🔴 `_generate_code` 修正（3 桁 800 件超で 503 リスク）— ユーザー判断保留中
- 認証なし公開（ユーザー判断済み「不要」）

詳細は `files/.handoff/SESSION_20260506_2309_434.md`。

## 戻し方（万が一）

`install.ps1` 実行時に作成した `.bak_<TS>` を逆コピーで戻せる：

```powershell
cd C:\Users\user\OneDrive\デスクトップ\0.フジ\900.ClaudeCode\014.TeamTasks\server
Copy-Item models.py.bak_<TS> models.py -Force
Copy-Item db.py.bak_<TS> db.py -Force
Copy-Item main.py.bak_<TS> main.py -Force
Copy-Item schemas.py.bak_<TS> schemas.py -Force
Remove-Item routers\r.py
Remove-Item -Recurse ..\pwa\r
```

`.claude/commands/r.md` は claude-shared の旧版から戻す（`/g-dl` で同期済み）。
