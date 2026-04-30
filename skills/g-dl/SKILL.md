---
name: g-dl
description: claude-shared をリモート Git から pull した後、`%USERPROFILE%\claude-shared\` の内容を `.claude/{skills,commands,tools,rules,memory}` にミラーコピーする。Option C ミラー方式。
---

# /g-dl — claude-shared download (mirror approach)

`%USERPROFILE%\claude-shared\` をリモートから fast-forward pull → プロジェクトの `.claude/` にミラーコピー。

**Option C ミラー方式**: ジャンクション不使用。pull 後 robocopy /MIR で `.claude/` を更新する。

## 使い方

```
/g-dl
```

引数なし。

## 手順

### Step 1: PowerShell で実行

**Bash ではなく PowerShell ツール**を使うこと。

```powershell
#requires -Version 7
$ErrorActionPreference = 'Stop'
$env:GIT_TERMINAL_PROMPT = '0'

# プロジェクトルート（CWD = .claude の親想定）
$cwd = (Get-Location).Path
$claudeDir = Join-Path $cwd '.claude'
if (-not (Test-Path -LiteralPath $claudeDir)) {
    Write-Error "CWD に .claude/ が見つかりません: $cwd"
    return
}
$shared  = Join-Path $env:USERPROFILE 'claude-shared'
$targets = @('skills','commands','tools','rules','memory')

if (-not (Test-Path -LiteralPath $shared)) {
    Write-Error "claude-shared not found: $shared (新PC は git clone が必要)"
    return
}

Write-Host "ClaudeDir: $claudeDir"
Write-Host "Shared:    $shared"

# Step A: claude-shared を Git から pull
Push-Location -LiteralPath $shared
try {
    $remoteUrl = git remote get-url origin 2>$null
    if (-not $remoteUrl) {
        Write-Host "origin remote 未設定" -ForegroundColor Red
        return
    }
    Write-Host "remote: $remoteUrl"

    # ローカル dirty チェック
    $status = git status --porcelain
    if (-not [string]::IsNullOrWhiteSpace($status)) {
        Write-Host "ローカル(claude-shared) に未コミット変更があります:" -ForegroundColor Yellow
        git status --short
        Write-Host ""
        Write-Host "先に /g-ul で push してから /g-dl を実行してください。" -ForegroundColor Yellow
        return
    }

    Write-Host ""
    Write-Host "=== fetch ==="
    git fetch --prune
    if ($LASTEXITCODE -ne 0) { throw "git fetch failed" }

    # デフォルトブランチ動的取得
    $defaultBranch = $null
    $headRef = git symbolic-ref --short refs/remotes/origin/HEAD 2>$null
    if ($headRef) { $defaultBranch = ($headRef -replace '^origin/','') }
    if (-not $defaultBranch) {
        $headRef = git rev-parse --abbrev-ref origin/HEAD 2>$null
        if ($headRef -and $headRef -ne 'origin/HEAD') {
            $defaultBranch = ($headRef -replace '^origin/','')
        }
    }
    if (-not $defaultBranch) {
        $remotes = git branch -r 2>$null
        foreach ($candidate in @('main','master')) {
            if ($remotes -match "origin/$candidate(\s|$)") {
                $defaultBranch = $candidate
                break
            }
        }
    }
    if (-not $defaultBranch) { $defaultBranch = git branch --show-current }
    if (-not $defaultBranch) { $defaultBranch = 'main' }
    Write-Host "default branch: $defaultBranch"

    $upstream = git rev-parse --abbrev-ref --symbolic-full-name '@{u}' 2>$null
    if (-not $upstream) {
        Write-Host "upstream 未設定です。次を実行してから再試行: git branch --set-upstream-to=origin/$defaultBranch" -ForegroundColor Yellow
        return
    }

    Write-Host ""
    Write-Host "=== upcoming changes ($upstream vs HEAD) ==="
    $upcoming = git log --oneline "HEAD..$upstream" 2>$null
    $hasUpdate = (-not [string]::IsNullOrWhiteSpace($upcoming))
    if ($hasUpdate) {
        Write-Host $upcoming
        Write-Host ""
        git diff --stat "HEAD..$upstream"
        Write-Host ""
        Write-Host "=== pull --ff-only ==="
        git pull --ff-only
        if ($LASTEXITCODE -ne 0) {
            Write-Host ""
            Write-Host "ff-only pull 失敗。履歴分岐。" -ForegroundColor Red
            Write-Host "  1. stash 退避: git stash push -m 'pre-gdl' ; git pull --ff-only ; git stash pop"
            Write-Host "  2. ブランチ退避+rebase: git branch backup-$(Get-Date -Format yyyyMMddHHmmss) ; git pull --rebase"
            Write-Host "  3. 最終手段 ローカル破棄: git reset --hard $upstream"
            throw "ff-only pull failed"
        }
        Write-Host "OK: claude-shared pulled." -ForegroundColor Green
    } else {
        Write-Host "claude-shared 側更新なし（ミラーは続行）。" -ForegroundColor Green
    }
} finally {
    Pop-Location
}

# Step B: claude-shared/* -> .claude/* ミラー
Write-Host ""
Write-Host "=== Mirror claude-shared/ -> .claude/ ==="
$mirrorOk = $true
foreach ($t in $targets) {
    $src = Join-Path $shared $t
    $dst = Join-Path $claudeDir $t
    if (-not (Test-Path -LiteralPath $src)) {
        Write-Host "  $t : claude-shared 側になし、skip"
        continue
    }
    Write-Host "  mirror $t ..."
    & robocopy $src $dst /MIR /NFL /NDL /NP /R:2 /W:1 `
        /XD __pycache__ '.bootstrap-bak-*' '.migrate-pending-*' `
        /XF '*.bak_*' '*.pyc' | Out-Null
    if ($LASTEXITCODE -ge 8) {
        Write-Host "    robocopy ERROR (exit=$LASTEXITCODE) for $t" -ForegroundColor Red
        $mirrorOk = $false
    }
}

if ($mirrorOk) {
    Write-Host ""
    Write-Host "OK: .claude/ updated from claude-shared." -ForegroundColor Green
} else {
    Write-Host ""
    Write-Host "ミラー中にエラーあり。.claude/ 状態を確認してください。" -ForegroundColor Red
}
```

### Step 2: 完了報告

取り込んだコミット数（あれば）と、ミラー成否を 1〜2 行で報告。

## エラー時

- `CWD に .claude/ が見つかりません` → プロジェクトルートで実行
- `claude-shared not found` → 新PC は `git clone https://github.com/jl4lvw/claude-shared.git C:\Users\<user>\claude-shared`
- ローカル(claude-shared) に未コミット変更 → 先に `/g-ul`
- ff-only pull 失敗 → ガイダンスで対処
- `robocopy ERROR (exit>=8)` → ファイルロック・権限。Claude Code 終了して再試行
