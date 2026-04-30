---
name: g-cmp
description: claude-shared のローカル状態とリモート (origin) との差分を一覧表示する。ローカル dirty / ahead / behind / 変更ファイル一覧を一望。`/g-ul` や `/g-dl` の前の確認用。読み取り専用で何も変更しない。
---

# /g-cmp — claude-shared compare

`%USERPROFILE%\claude-shared\` のローカル状態とリモート origin との差分を一覧表示する。

`/g-ul`（push）や `/g-dl`（pull）を実行する前に「今 PC でどんな差分があるか」を確認する用途。

## 使い方

```
/g-cmp
```

引数なし。読み取り専用で、いかなる変更も加えない（fetch のみ実行）。

## 手順

### Step 1: PowerShell で実行

以下の PowerShell スクリプトを実行する。**Bash ではなく PowerShell ツール**を使うこと。

```powershell
#requires -Version 7
$ErrorActionPreference = 'Stop'
$env:GIT_TERMINAL_PROMPT = '0'

# 状態変数（StrictMode で未定義参照を踏まないよう先頭で初期化）
$ahead = 0
$behind = 0
$hasHead = $false
$fetchOk = $true
$revListOk = $false

$shared = Join-Path $env:USERPROFILE 'claude-shared'

if (-not (Test-Path -LiteralPath $shared)) {
    Write-Host "claude-shared dir not found: $shared" -ForegroundColor Red
    Write-Host "新PC の場合は bootstrap.ps1 を先に実行してください。"
    return
}

Push-Location -LiteralPath $shared
try {
    Write-Host "=== claude-shared compare ===" -ForegroundColor Cyan
    Write-Host "PC: $env:COMPUTERNAME"

    # remote 確認
    $remoteUrl = git remote get-url origin 2>$null
    if (-not $remoteUrl) {
        Write-Host "origin remote が未設定です。/g-ul で設定するか、git remote add で追加してください。" -ForegroundColor Red
        return
    }
    Write-Host "remote: $remoteUrl"

    # HEAD 存在チェック（空リポジトリ判定）
    git rev-parse --verify HEAD 2>$null | Out-Null
    if ($LASTEXITCODE -eq 0) { $hasHead = $true }

    if (-not $hasHead) {
        Write-Host ""
        Write-Host "ローカルに commit がありません（空リポジトリ）。/g-ul で初回コミット＆push してください。" -ForegroundColor Yellow
        return
    }

    # branch + upstream
    $branch = git branch --show-current 2>$null
    if ([string]::IsNullOrWhiteSpace($branch)) {
        Write-Host "detached HEAD 状態です。`git checkout main` で main に戻してから再実行してください。" -ForegroundColor Yellow
        return
    }
    $upstream = git rev-parse --abbrev-ref --symbolic-full-name '@{u}' 2>$null
    if (-not $upstream) {
        Write-Host "upstream 未設定: branch=$branch" -ForegroundColor Yellow
        Write-Host "  -> /g-ul で push して --set-upstream するか、手動で設定してください。"
        return
    }
    Write-Host "branch: $branch (upstream: $upstream)"

    # fetch
    Write-Host ""
    Write-Host "fetching from remote..." -ForegroundColor DarkGray
    git fetch --prune 2>&1 | Out-Null
    if ($LASTEXITCODE -ne 0) {
        $fetchOk = $false
        Write-Host "git fetch 失敗（認証 or ネットワーク）。キャッシュ済みリモート参照で続行します。" -ForegroundColor Yellow
    }

    # ローカル状態
    Write-Host ""
    Write-Host "--- ローカル状態 ---" -ForegroundColor Yellow
    $statusShort = git status --short
    $dirtyLines = @()
    if (-not [string]::IsNullOrWhiteSpace($statusShort)) {
        $dirtyLines = @($statusShort -split "`r?`n" | Where-Object { $_ })
    }
    if ($dirtyLines.Count -eq 0) {
        Write-Host "  clean (uncommitted 変更なし)" -ForegroundColor Green
    } else {
        Write-Host "  dirty ($($dirtyLines.Count) entries)" -ForegroundColor Red
        foreach ($l in $dirtyLines) { Write-Host "    $l" }
    }

    # ahead/behind カウント
    Write-Host ""
    Write-Host "--- 同期状態 (vs $upstream) ---" -ForegroundColor Yellow
    $aheadBehindRaw = (git rev-list --left-right --count "$upstream...HEAD" 2>$null | Out-String).Trim()
    $revListOk = ($LASTEXITCODE -eq 0)
    if ($revListOk -and $aheadBehindRaw -match '(\d+)\s+(\d+)') {
        $behind = [int]$Matches[1]
        $ahead  = [int]$Matches[2]
        $color = if (($ahead -eq 0) -and ($behind -eq 0)) { 'Green' } else { 'Yellow' }
        Write-Host "  ahead: $ahead (push 待ち) / behind: $behind (pull 待ち)" -ForegroundColor $color
    } else {
        Write-Host "  rev-list 失敗（取得できず）" -ForegroundColor Red
    }

    # 未push commits
    Write-Host ""
    Write-Host "--- 未push コミット ($upstream..HEAD) ---" -ForegroundColor Yellow
    $unpushed = git log --oneline "$upstream..HEAD" 2>$null
    if ([string]::IsNullOrWhiteSpace($unpushed)) {
        Write-Host "  (なし)"
    } else {
        @($unpushed -split "`r?`n" | Where-Object { $_ }) | ForEach-Object { Write-Host "  $_" }
    }

    # 未取込 commits
    Write-Host ""
    Write-Host "--- 未取込 コミット (HEAD..$upstream) ---" -ForegroundColor Yellow
    $unfetched = git log --oneline "HEAD..$upstream" 2>$null
    if ([string]::IsNullOrWhiteSpace($unfetched)) {
        Write-Host "  (なし)"
    } else {
        @($unfetched -split "`r?`n" | Where-Object { $_ }) | ForEach-Object { Write-Host "  $_" }
    }

    # 変更ファイル一覧
    Write-Host ""
    Write-Host "--- 変更ファイル ---" -ForegroundColor Yellow
    $any = $false

    if ($dirtyLines.Count -gt 0) {
        Write-Host "  作業ツリー (uncommitted, untracked含む):"
        foreach ($l in $dirtyLines) { Write-Host "    $l" }
        $any = $true
    }

    if ($ahead -gt 0) {
        $diffOut = git diff --name-status "$upstream..HEAD" 2>$null
        if (-not [string]::IsNullOrWhiteSpace($diffOut)) {
            Write-Host "  ローカル -> リモート (push 待ち):"
            @($diffOut -split "`r?`n" | Where-Object { $_ }) | ForEach-Object { Write-Host "    $_" }
            $any = $true
        }
    }

    if ($behind -gt 0) {
        $diffIn = git diff --name-status "HEAD..$upstream" 2>$null
        if (-not [string]::IsNullOrWhiteSpace($diffIn)) {
            Write-Host "  リモート -> ローカル (pull 待ち):"
            @($diffIn -split "`r?`n" | Where-Object { $_ }) | ForEach-Object { Write-Host "    $_" }
            $any = $true
        }
    }

    if (-not $any) {
        Write-Host "  (差分なし)"
    }

    # 最終コミット
    Write-Host ""
    Write-Host "--- 最終コミット ---" -ForegroundColor Yellow
    $lastLocal = git log -1 --format='%h %ad %s' --date=short HEAD 2>$null
    Write-Host "  ローカル HEAD     : $lastLocal"
    $lastRemote = git log -1 --format='%h %ad %s' --date=short $upstream 2>$null
    if ($lastRemote) {
        Write-Host "  リモート $upstream : $lastRemote"
    }

    # 推奨アクション
    Write-Host ""
    Write-Host "--- 推奨アクション ---" -ForegroundColor Cyan
    $hadAction = $false
    if ($dirtyLines.Count -gt 0) {
        Write-Host "  - uncommitted 変更あり: /g-ul `"<message>`" で push" -ForegroundColor Yellow
        $hadAction = $true
    }
    if ($revListOk -and ($ahead -gt 0) -and ($behind -eq 0)) {
        Write-Host "  - 未push コミット $ahead 件: /g-ul で push" -ForegroundColor Yellow
        $hadAction = $true
    } elseif ($revListOk -and ($ahead -eq 0) -and ($behind -gt 0)) {
        Write-Host "  - 未取込 コミット $behind 件: /g-dl で pull" -ForegroundColor Yellow
        $hadAction = $true
    } elseif ($revListOk -and ($ahead -gt 0) -and ($behind -gt 0)) {
        Write-Host "  - state=diverged (ahead $ahead / behind $behind): 履歴が分岐しています。" -ForegroundColor Red
        Write-Host "    /g-dl は ff-only で失敗します。`git stash` 退避 → `git pull --rebase` か手動マージが必要。"
        $hadAction = $true
    }
    if (-not $fetchOk) {
        Write-Host "  - fetch が失敗しました。リモートの最新状態は反映されていません。" -ForegroundColor Yellow
        $hadAction = $true
    }
    if (-not $hadAction) {
        Write-Host "  - 同期済み (clean & up-to-date)" -ForegroundColor Green
    }
} finally {
    Pop-Location
}
```

### Step 2: 結果の解釈

- **clean** + **ahead 0 / behind 0** → 完全同期済み
- **dirty** あり → `/g-ul` で push が必要
- **ahead > 0 のみ** → 未push のコミットあり → `/g-ul`
- **behind > 0 のみ** → 未取込のコミットあり → `/g-dl`
- **両方 > 0 (diverged)** → 履歴分岐 → `git pull --rebase` か手動マージ必要

### Step 3: 完了報告

最後に「同期済み / push 待ち / pull 待ち / diverged / fetch 失敗」のいずれかを 1 行で報告して終了。

## エラー時

- `claude-shared dir not found` → bootstrap.ps1 を先に実行
- `origin remote が未設定` → `git remote add` のガイダンス
- `空リポジトリ` → `/g-ul` で初回 push
- `detached HEAD` → `git checkout main` で復帰
- `upstream 未設定` → `/g-ul` で `--set-upstream`
- `git fetch 失敗` → 認証 / ネットワーク確認、キャッシュで続行
