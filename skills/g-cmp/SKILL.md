---
name: g-cmp
description: 三方比較スキル。`.claude/` (作業) ↔ `claude-shared/` (Git WD) ↔ `origin` (GitHub) の差分を一覧表示する。Option C ミラー方式。`/g-ul` や `/g-dl` の前の確認用。読み取り専用。
---

# /g-cmp — three-way compare (mirror approach)

`.claude/` (作業), `%USERPROFILE%\claude-shared\` (Git WD), `origin/main` (GitHub) の三方差分を表示。

`/g-ul`（push）や `/g-dl`（pull）を実行する前の確認用。読み取り専用、いかなる変更も加えない（fetch のみ）。

## 使い方

```
/g-cmp
```

引数なし。

## 手順

### Step 1: PowerShell で実行

**Bash ではなく PowerShell ツール**を使うこと。

```powershell
#requires -Version 7
$ErrorActionPreference = 'Stop'
$env:GIT_TERMINAL_PROMPT = '0'

# 状態変数
$ahead = 0
$behind = 0
$hasHead = $false
$fetchOk = $true
$revListOk = $false

# パス解決
$cwd = (Get-Location).Path
$claudeDir = Join-Path $cwd '.claude'
$shared    = Join-Path $env:USERPROFILE 'claude-shared'
$targets   = @('skills','commands','tools','rules','memory')

if (-not (Test-Path -LiteralPath $claudeDir)) {
    Write-Host "CWD に .claude/ が見つかりません: $cwd" -ForegroundColor Red
    return
}
if (-not (Test-Path -LiteralPath $shared)) {
    Write-Host "claude-shared not found: $shared" -ForegroundColor Red
    Write-Host "新PC は git clone https://github.com/jl4lvw/claude-shared.git $shared"
    return
}

Write-Host "=== three-way compare ===" -ForegroundColor Cyan
Write-Host "PC:        $env:COMPUTERNAME"
Write-Host "ClaudeDir: $claudeDir"
Write-Host "Shared:    $shared"

# ----- Section 1: .claude/ vs claude-shared/ (mirror gap) -----
Write-Host ""
Write-Host "--- [1/3] .claude/ vs claude-shared/ (ミラー差分) ---" -ForegroundColor Yellow

$mirrorDiffs = @()
foreach ($t in $targets) {
    $src = Join-Path $claudeDir $t
    $dst = Join-Path $shared $t
    if (-not (Test-Path -LiteralPath $src) -and -not (Test-Path -LiteralPath $dst)) { continue }

    # robocopy /L (list only, no copy) で差分を取得
    if (-not (Test-Path -LiteralPath $src)) {
        $mirrorDiffs += "  $t : .claude 側欠落 (claude-shared 側のみ存在)"
        continue
    }
    if (-not (Test-Path -LiteralPath $dst)) {
        $mirrorDiffs += "  $t : claude-shared 側欠落 (.claude 側のみ存在)"
        continue
    }
    # /L /MIR で「もし MIR したら何が起きるか」を取得
    # /g-ul / /g-dl と同じ除外パターンに揃える（差分検出と同期で挙動を一致させる）
    $rcOut = & robocopy $src $dst /L /MIR /NJH /NJS /NS /NC /NDL /FP /R:0 /W:0 `
        /XD __pycache__ '.bootstrap-bak-*' '.migrate-pending-*' `
        /XF '*.bak_*' '*.pyc' '.deepseek_usage_session.json' 2>&1
    $diffLines = @($rcOut | Where-Object { $_ -match '\S' -and $_ -notmatch '^\s+ROBOCOPY' -and $_ -notmatch '^---' })
    if ($diffLines.Count -gt 0) {
        $mirrorDiffs += "  $t : $($diffLines.Count) entries differ"
        foreach ($l in ($diffLines | Select-Object -First 5)) {
            $mirrorDiffs += "      $($l.Trim())"
        }
        if ($diffLines.Count -gt 5) { $mirrorDiffs += "      ... ($($diffLines.Count - 5) more)" }
    }
}
if ($mirrorDiffs.Count -eq 0) {
    Write-Host "  (差分なし: .claude/ と claude-shared/ は同期済み)" -ForegroundColor Green
} else {
    foreach ($l in $mirrorDiffs) { Write-Host $l }
    Write-Host ""
    Write-Host "  -> /g-ul で .claude -> claude-shared に反映できます。" -ForegroundColor Yellow
}

# ----- Section 2 & 3: claude-shared vs origin (Git) -----
Push-Location -LiteralPath $shared
try {
    Write-Host ""
    Write-Host "--- [2/3] claude-shared (ローカル Git WD) 状態 ---" -ForegroundColor Yellow

    $remoteUrl = git remote get-url origin 2>$null
    if (-not $remoteUrl) {
        Write-Host "  origin remote 未設定" -ForegroundColor Red
        return
    }
    Write-Host "  remote: $remoteUrl"

    git rev-parse --verify HEAD 2>$null | Out-Null
    if ($LASTEXITCODE -eq 0) { $hasHead = $true }
    if (-not $hasHead) {
        Write-Host "  ローカルに commit なし（空リポジトリ）" -ForegroundColor Yellow
        return
    }

    $branch = git branch --show-current 2>$null
    if ([string]::IsNullOrWhiteSpace($branch)) {
        Write-Host "  detached HEAD" -ForegroundColor Yellow
        return
    }
    $upstream = git rev-parse --abbrev-ref --symbolic-full-name '@{u}' 2>$null
    if (-not $upstream) {
        Write-Host "  upstream 未設定 (branch=$branch)" -ForegroundColor Yellow
        return
    }
    Write-Host "  branch: $branch (upstream: $upstream)"

    git fetch --prune 2>&1 | Out-Null
    if ($LASTEXITCODE -ne 0) {
        $fetchOk = $false
        Write-Host "  fetch 失敗（キャッシュ参照で続行）" -ForegroundColor Yellow
    }

    # claude-shared dirty
    $statusShort = git status --porcelain
    $dirtyLines = @()
    if (-not [string]::IsNullOrWhiteSpace($statusShort)) {
        $dirtyLines = @($statusShort -split "`r?`n" | Where-Object { $_ })
    }
    if ($dirtyLines.Count -eq 0) {
        Write-Host "  uncommitted: なし (clean)" -ForegroundColor Green
    } else {
        Write-Host "  uncommitted: $($dirtyLines.Count) entries" -ForegroundColor Red
        foreach ($l in $dirtyLines) { Write-Host "    $l" }
    }

    # ahead/behind
    Write-Host ""
    Write-Host "--- [3/3] claude-shared vs $upstream (Git 同期) ---" -ForegroundColor Yellow
    $aheadBehindRaw = (git rev-list --left-right --count "$upstream...HEAD" 2>$null | Out-String).Trim()
    $revListOk = ($LASTEXITCODE -eq 0)
    if ($revListOk -and $aheadBehindRaw -match '(\d+)\s+(\d+)') {
        $behind = [int]$Matches[1]
        $ahead  = [int]$Matches[2]
        $color = if (($ahead -eq 0) -and ($behind -eq 0)) { 'Green' } else { 'Yellow' }
        Write-Host "  ahead: $ahead (push 待ち) / behind: $behind (pull 待ち)" -ForegroundColor $color
    } else {
        Write-Host "  rev-list 失敗" -ForegroundColor Red
    }

    if ($ahead -gt 0) {
        Write-Host "  未push commits:"
        $unpushed = git log --oneline "$upstream..HEAD" 2>$null
        @($unpushed -split "`r?`n" | Where-Object { $_ }) | ForEach-Object { Write-Host "    $_" }
    }
    if ($behind -gt 0) {
        Write-Host "  未取込 commits:"
        $unfetched = git log --oneline "HEAD..$upstream" 2>$null
        @($unfetched -split "`r?`n" | Where-Object { $_ }) | ForEach-Object { Write-Host "    $_" }
    }

    # 最終コミット
    Write-Host ""
    Write-Host "  最終コミット:"
    $lastLocal = git log -1 --format='%h %ad %s' --date=short HEAD 2>$null
    Write-Host "    ローカル HEAD     : $lastLocal"
    $lastRemote = git log -1 --format='%h %ad %s' --date=short $upstream 2>$null
    if ($lastRemote) { Write-Host "    リモート $upstream : $lastRemote" }
} finally {
    Pop-Location
}

# ----- 推奨アクション -----
Write-Host ""
Write-Host "--- 推奨アクション ---" -ForegroundColor Cyan
$hadAction = $false
if ($mirrorDiffs.Count -gt 0) {
    Write-Host "  - .claude/ と claude-shared/ がズレています: /g-ul で push (ミラー含む)" -ForegroundColor Yellow
    $hadAction = $true
}
if ($dirtyLines.Count -gt 0) {
    Write-Host "  - claude-shared 側 uncommitted 変更あり: /g-ul で push" -ForegroundColor Yellow
    $hadAction = $true
}
if ($revListOk -and ($ahead -gt 0) -and ($behind -eq 0)) {
    Write-Host "  - 未push commit $ahead 件: /g-ul" -ForegroundColor Yellow
    $hadAction = $true
} elseif ($revListOk -and ($ahead -eq 0) -and ($behind -gt 0)) {
    Write-Host "  - 未取込 commit $behind 件: /g-dl" -ForegroundColor Yellow
    $hadAction = $true
} elseif ($revListOk -and ($ahead -gt 0) -and ($behind -gt 0)) {
    Write-Host "  - state=diverged (ahead $ahead / behind $behind)" -ForegroundColor Red
    Write-Host "    git stash 退避 → git pull --rebase か手動マージが必要"
    $hadAction = $true
}
if (-not $fetchOk) {
    Write-Host "  - fetch 失敗（最新リモート未反映）" -ForegroundColor Yellow
    $hadAction = $true
}
if (-not $hadAction) {
    Write-Host "  - 完全同期 (.claude == claude-shared == origin/main)" -ForegroundColor Green
}
```

### Step 2: 結果の解釈

3 つのレベルでの差分を表示:
1. **`.claude/` vs `claude-shared/`** — ミラー差分（`/g-ul` で同期）
2. **`claude-shared/` 状態** — uncommitted 変更
3. **`claude-shared` vs `origin/main`** — Git push/pull 差分

完全同期 = (1) 差分なし + (2) clean + (3) ahead 0 / behind 0

### Step 3: 完了報告

「完全同期 / mirror ズレ / push 待ち / pull 待ち / diverged」のいずれかを 1 行で報告。

## エラー時

- `CWD に .claude/ が見つかりません` → プロジェクトルートで実行
- `claude-shared not found` → 新PC は git clone
- `origin remote 未設定` / `upstream 未設定` → /g-ul で初回 push
- `git fetch 失敗` → 認証 / ネットワーク確認
