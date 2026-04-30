---
name: g-dl
description: claude-shared をリモート Git から pull する。`%USERPROFILE%\claude-shared\` を `git pull --ff-only`。ローカル変更がある場合は中断して `/g-ul` を促す。
---

# /g-dl — claude-shared download

`%USERPROFILE%\claude-shared\` をリモートから取得する（fast-forward only）。

## 使い方

```
/g-dl
```

引数なし。

## 手順

### Step 1: PowerShell で実行

以下の PowerShell スクリプトを実行する。**Bash ではなく PowerShell ツール**を使うこと。

```powershell
#requires -Version 7
$ErrorActionPreference = 'Stop'
$env:GIT_TERMINAL_PROMPT = '0'   # 認証ポップアップでハングしないよう非対話化

$shared = Join-Path $env:USERPROFILE 'claude-shared'

if (-not (Test-Path -LiteralPath $shared)) {
    Write-Error "claude-shared dir not found: $shared"
    Write-Host "新PCの場合は bootstrap.ps1 を先に実行してください。"
    exit 1
}

Push-Location -LiteralPath $shared
try {
    # リモート設定の事前確認
    $remoteUrl = git remote get-url origin 2>$null
    if (-not $remoteUrl) {
        Write-Host "origin remote が未設定です。" -ForegroundColor Red
        Write-Host "  cd $shared"
        Write-Host "  git remote add origin <URL>"
        Write-Host "を実行してから再試行してください。"
        exit 1
    }
    Write-Host "remote: $remoteUrl"

    # ローカル dirty チェック
    $status = git status --porcelain
    if (-not [string]::IsNullOrWhiteSpace($status)) {
        Write-Host "ローカルに未コミット変更があります:" -ForegroundColor Yellow
        git status --short
        Write-Host ""
        Write-Host "先に /g-ul で push してから /g-dl を実行してください。" -ForegroundColor Yellow
        exit 1
    }

    Write-Host ""
    Write-Host "=== fetch ==="
    git fetch --prune
    if ($LASTEXITCODE -ne 0) { throw "git fetch failed" }

    # デフォルトブランチを動的に取得（main / master 等）
    # フォールバックチェーン: symbolic-ref → rev-parse → branch -r 検索 → カレント → 'main'
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
        # origin/main, origin/master のどちらが存在するか探す
        $remotes = git branch -r 2>$null
        foreach ($candidate in @('main','master')) {
            if ($remotes -match "origin/$candidate(\s|$)") {
                $defaultBranch = $candidate
                break
            }
        }
    }

    if (-not $defaultBranch) {
        $defaultBranch = git branch --show-current
    }
    if (-not $defaultBranch) { $defaultBranch = 'main' }
    Write-Host "default branch: $defaultBranch"

    # upstream 確認
    $upstream = git rev-parse --abbrev-ref --symbolic-full-name '@{u}' 2>$null
    if (-not $upstream) {
        Write-Host "upstream 未設定です。" -ForegroundColor Yellow
        Write-Host "  git branch --set-upstream-to=origin/$defaultBranch"
        Write-Host "を実行してから再試行してください。"
        exit 1
    }

    # 取り込まれる変更の概要
    Write-Host ""
    Write-Host "=== upcoming changes ($upstream vs HEAD) ==="
    $upcoming = git log --oneline "HEAD..$upstream" 2>$null
    if ([string]::IsNullOrWhiteSpace($upcoming)) {
        Write-Host "更新なし。最新です。" -ForegroundColor Green
        exit 0
    }
    Write-Host $upcoming
    Write-Host ""
    git diff --stat "HEAD..$upstream"

    Write-Host ""
    Write-Host "=== pull --ff-only ==="
    git pull --ff-only
    if ($LASTEXITCODE -ne 0) {
        Write-Host ""
        Write-Host "ff-only pull に失敗しました。リモートと履歴が分岐しています。" -ForegroundColor Red
        Write-Host "対処方針 (どれかをユーザーが選択):" -ForegroundColor Yellow
        Write-Host "  1. 安全策 - stash 退避してから pull:" -ForegroundColor Yellow
        Write-Host "       git stash push -m 'pre-gdl' ; git pull --ff-only ; git stash pop"
        Write-Host "  2. ローカルブランチを退避してから pull:"
        Write-Host "       git branch backup-$(Get-Date -Format yyyyMMddHHmmss) ; git pull --rebase"
        Write-Host "  3. 最終手段 - ローカルを破棄 (バックアップ取得後):"
        Write-Host "       git reset --hard $upstream"
        throw "ff-only pull failed"
    }

    Write-Host ""
    Write-Host "OK: pulled." -ForegroundColor Green
} finally {
    Pop-Location
}
```

### Step 2: 完了報告

取り込んだコミット数と更新ファイル数を 1〜2 行で報告して終了。

## エラー時

- `origin remote が未設定` → ガイダンス通り `git remote add` を促す
- `upstream 未設定` → `git branch --set-upstream-to` を促す
- ローカルに未コミット変更 → 先に `/g-ul` を促す
- ff-only pull 失敗 → 履歴分岐の解決方法を提示してユーザー判断を仰ぐ（reset --hard は最終手段）
