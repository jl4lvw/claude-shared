# PWA Task Restart Tools

PWAバックエンド (FastAPI/uvicorn) のスケジュールタスクを安全に再起動するためのツール群。

## 解決する問題

`Stop-ScheduledTask` → `Start-ScheduledTask` だけでは、古い `python.exe` がポートを握ったまま残り、新インスタンスが bind 失敗する。原因:

1. **cmd.exe 親子構造**: タスクの Action が `cmd.exe /d /c ... && python -m uvicorn ...` で、`Stop-ScheduledTask` が cmd.exe しか止めない → python.exe がオーファン
2. **Python Manager の二段ロード**: `python` コマンドが Microsoft Store の `PythonManager` stub に解決され、その子として実体 python が起動する。起動完了まで **10秒〜2分** かかる
3. **`Get-NetTCPConnection` の遅さ**: Windows で接続数が多いと1呼び出しに 10秒以上かかる → タイムアウト判定が壊れる

## 含まれるスクリプト

| ファイル | 役割 |
|---|---|
| `Cleanup-PwaPort.ps1` | 指定ポートの古いリスナーを `netstat` で検出 → プロセスツリーごと kill → bind 可能まで待機 |
| `Restart-PwaApi.ps1` | `Stop-ScheduledTask` → `Cleanup-PwaPort.ps1` → `Start-ScheduledTask` を一連で実行 |
| `task-xml-backup/*.bak_YYYYMMDD.xml` | 元タスク定義のバックアップ |

## 使い方

### 単一タスク再起動

```powershell
$py = "C:\Users\user\AppData\Local\Python\pythoncore-3.14-64\python.exe"
$tools = "C:\Users\user\OneDrive\デスクトップ\0.フジ\900.ClaudeCode\.claude\tools\pwa-task"

# TeamTasksAPIServer (port 8086)
powershell.exe -NoProfile -ExecutionPolicy Bypass `
  -File "$tools\Restart-PwaApi.ps1" `
  -TaskName "TeamTasksAPIServer" -Name "TeamTasksAPI" -Port 8086 `
  -ExpectedExe $py -ExpectedCommandContains "main:app"
```

`-TimeoutSeconds 120` がデフォルト (Python Manager の初回起動が遅いため)。

### 各タスクの引数表

| TaskName | Name | Port | ExpectedCommandContains | 備考 |
|---|---|---|---|---|
| TeamTasksAPIServer | TeamTasksAPI | 8086 | main:app | 014.TeamTasks (uvicorn) |
| ConphasAPIServer | ConphasAPI | 8085 | main:app | 013.CONPHAS-PWA (uvicorn) |
| PicklistPWAServer | PicklistAPI | 8084 | print_server | order_to_picklist (start_all.bat → print_server\server.py) |
| (CacheAPI) | CacheAPI | 8087 | (要確認) | inventory-replenishment-pwa |

`ExpectedExe` は上記全タスクで `C:\Users\user\AppData\Local\Python\pythoncore-3.14-64\python.exe`。

**CaddyPWAServer は対象外**: Caddy は caddy.exe + auto-restart ループの bat で、本スクリプトの想定外。Caddy 再起動は別途 `Stop-ScheduledTask` → `Stop-Process -Name caddy` → `Start-ScheduledTask` を手動で。

## 動作確認 (TeamTasks 2026-04-27)

**初版 3ラウンド連続再起動テスト** (-KillAnyListener 既定使用):

| Round | Duration | Result |
|---|---|---|
| 1 | 32.5s | OK (health 200) |
| 2 | 17.2s | OK (health 200) |
| 3 | 12.2s | OK (health 200) |

**C+G ラウンド1反映後 3ラウンド再テスト** (no -Force, ExpectedExe matchで kill):

| Round | Duration | Result |
|---|---|---|
| 1 | 27.8s | OK (PID 15484→5164, health 200) |
| 2 | 8.4s | OK (PID 5164→15924, health 200) |
| 3 | 8.1s | OK (PID 15924→11804, health 200) |

**C+G ラウンド2反映後 3ラウンド再テスト** (期待プロセス再検証付き):

| Round | Duration | Result |
|---|---|---|
| 1 | 26.1s | OK (cleanup→PID 17532, **expected process verified**, health 200) |
| 2 | 9.0s | OK (PID 17532→17600, expected verified, health 200) |
| 3 | 10.0s | OK (PID 17600→17444, expected verified, health 200) |

初回が遅いのは Python Manager の stub→実体起動キャッシュが効いていないため。2回目以降は短縮。

## -Force フラグ (緊急用)

通常はデフォルト動作で OK (`-ExpectedExe` / `-ExpectedCommandContains` 一致時のみ kill)。

ただし古いプロセスが期待値に一致しない（例: 別パスの python.exe で立ち上がったゾンビ）場合のみ:

```powershell
... -File "$tools\Restart-PwaApi.ps1" ... -Force
```

`-Force` は `-KillAnyListener` を Cleanup に伝え、ポートを握っている**任意のプロセス**を kill する。**他サービスを誤kill するリスクあり**なので緊急時限定。

## なぜタスク本体は変更しないか

検討した「2-Action 構成 (Cleanup → python直接実行)」は、PowerShell 5.1 の以下の制約で安定しなかった:

- `Start-Process -RedirectStandardOutput` + 日本語パス + `-WindowStyle Hidden` で stderr が詰まる
- `MultipleInstancesPolicy=StopExisting` を `New-ScheduledTaskSettingsSet` の enum で指定できない (XML直接編集が必要)
- Task Scheduler の `WorkingDirectory` 要素が日本語パスで信頼できない

代わりに、**タスク本体は `cmd.exe /d /c ... python ...` のまま残し、再起動ワークフローを `Restart-PwaApi.ps1` に集約**することで:

- 自動起動 (ログオン時) は既存の動作 → 問題なし
- 手動再起動だけ Restart-PwaApi.ps1 経由 → 古いプロセス確実排除

## C+G レビューで対応した安全策（2026-04-27）

### ラウンド1 反映
| 対策 | 場所 | 効果 |
|---|---|---|
| 自プロセス・親プロセス除外 | Cleanup `Should-StopProcess` | 自殺バグ完全防止 |
| 8.3短縮形パスを正規化して比較 | `Resolve-LongPath` 関数 | `PROGRA~1` などで誤判定回避 |
| ExpectedCommandContains に `IndexOf` 使用 | 同上 | `[`*` `?` 含むパスでのワイルドカード暴発回避 |
| ExecutablePath null guard | 同上 | 権限不足プロセスで例外回避 |
| `-KillAnyListener` を opt-in 化 | Restart `-Force` | デフォルトで他人のリスナー誤kill 防止 |
| `Get-NetTCPConnection` を `netstat` 化 | Restart 起動確認 | 13秒/回の遅延を解消 |
| `Stop-ScheduledTask` 失敗を warning 化 | Restart | サイレント失敗解消 |
| タスク存在事前チェック | Restart | TaskName 誤指定を即時 throw |

### ラウンド2 反映
| 対策 | 場所 | 効果 |
|---|---|---|
| Win32_Process スナップショットを1回だけ取得 | Cleanup `Get-AllProcessSnapshot` | 再帰CIMによる性能劣化を回避 |
| ExpectedExe AND ExpectedCommandContains | Cleanup `Should-StopProcess` | 両者指定時はAND判定で誤kill更に低減 |
| 検出時の CreationDate を保存→kill直前に再照合 | Cleanup `$plannedKills` | PID再利用 TOCTOU 防御 |
| `Stop-Process` の例外を warning に | Cleanup | Access denied / 既に終了 等の診断 |
| 起動後に listener PID が ExpectedExe/Cmd と一致するか再検証 | Restart `Test-ListenerMatchesExpected` | 別プロセスが先に bind した場合を fail 扱い |
| `[ValidateRange]` / `[ValidatePattern]` | 両ファイル | パストラバーサル・範囲外値 入力検証 |
| `Stopwatch` ベースの待機 | 両ファイル | deadline 厳守 (sleep最小化) |
| `Cleanup` throw 時の診断 (残存PID列挙) | Restart | cleanup失敗時の診断性向上 |
| 再帰深度制限 (10層) | Cleanup `Get-ProcessTreeIds` | プロセス循環防御 |
| `Write-Output -NoEnumerate` で int[] 返却 | 両ファイル | PS5.1 unwrap バグ回避 |

## トラブルシュート

### Restart に失敗する
- `-TimeoutSeconds` を 180 など更に伸ばす
- Python Manager の stub が引っかかっている可能性: `Get-CimInstance Win32_Process -Filter "Name='python.exe'"` で `WindowsApps\PythonSoftwareFoundation.PythonManager` のプロセスがあるか確認
- `where python` で 1 位が Store stub かを `where.exe python` で確認

### Cleanup-PwaPort.ps1 が hang する
- 過去版に Mutex があり詰まっていた → 現行版は削除済み
- `Get-NetTCPConnection` を使っていた古い版を引いている可能性 → ファイルが最新か確認 (`netstat -ano` ベースか)

### 別人のリスナーを誤kill しないか
- `Cleanup-PwaPort.ps1` は `-ExpectedExe` か `-ExpectedCommandContains` のいずれかが一致した場合のみ kill する
- `-KillAnyListener` フラグを付けた場合のみ無条件 kill (Restart-PwaApi.ps1 はこれを付けて呼ぶ)
