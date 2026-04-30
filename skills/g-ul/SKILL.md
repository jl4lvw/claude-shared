---
name: g-ul
description: claude-shared をリモート Git に push する。`%USERPROFILE%\claude-shared\` の差分を add+commit+push。コミットメッセージは引数（任意、未指定時は自動生成）。
---

# /g-ul — claude-shared upload

`%USERPROFILE%\claude-shared\` の変更を Git にアップロードする。

## 使い方

```
/g-ul                                   # 自動メッセージで push
/g-ul "skill: refactor handoff"         # メッセージ指定で push
```

## 手順

### Step 1: 引数からコミットメッセージを取り出す

ユーザー引数（`$ARGUMENTS`）があればそれをメッセージとして使う。
未指定なら `sync: <ComputerName> <yyyy-MM-dd HH:mm>` を自動生成する。

### Step 2: PowerShell で実行

以下の PowerShell スクリプトを実行する。**Bash ではなく PowerShell ツール**を使うこと。
スクリプト内の `<USER_ARGS_OR_EMPTY>` プレースホルダは、Claude が引数の有無で置換する:
- 引数あり → そのテキストを埋め込む
- 引数なし → **必ず空文字列 `''` に置換する**（プレースホルダ文字列を残してはいけない）

```powershell
#requires -Version 7
$ErrorActionPreference = 'Stop'
$env:GIT_TERMINAL_PROMPT = '0'   # 認証ポップアップでハングしないよう非対話化

$shared = Join-Path $env:USERPROFILE 'claude-shared'

if (-not (Test-Path -LiteralPath $shared)) {
    Write-Error "claude-shared dir not found: $shared"
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

    # 変更検出
    $status = git status --porcelain
    if ([string]::IsNullOrWhiteSpace($status)) {
        Write-Host "変更なし。push 不要。" -ForegroundColor Yellow
        exit 0
    }

    Write-Host ""
    Write-Host "=== 変更ファイル ==="
    git status --short

    git add -A | Out-Null

    Write-Host ""
    Write-Host "=== 差分サマリ ==="
    git diff --cached --stat

    # コミットメッセージ
    $msg = '<USER_ARGS_OR_EMPTY>'
    # プレースホルダがそのまま残っていたら空扱い（防衛策）
    if ($msg -eq '<USER_ARGS_OR_EMPTY>') { $msg = '' }
    if ([string]::IsNullOrWhiteSpace($msg)) {
        $stamp = Get-Date -Format 'yyyy-MM-dd HH:mm'
        $msg = "sync: $env:COMPUTERNAME $stamp"
    }
    Write-Host ""
    Write-Host "=== commit ==="
    Write-Host "message: $msg"
    git commit -m "$msg"
    if ($LASTEXITCODE -ne 0) { throw "git commit failed" }

    # upstream 設定確認
    $upstream = git rev-parse --abbrev-ref --symbolic-full-name '@{u}' 2>$null
    Write-Host ""
    Write-Host "=== push ==="
    if (-not $upstream) {
        $branch = git branch --show-current
        Write-Host "upstream 未設定。--set-upstream で push します ($branch)" -ForegroundColor Yellow
        git push --set-upstream origin $branch
    } else {
        git push
    }
    if ($LASTEXITCODE -ne 0) { throw "git push failed" }

    Write-Host ""
    Write-Host "OK: pushed." -ForegroundColor Green
} finally {
    Pop-Location
}
```

### Step 3: 完了報告

push の commit hash と short stat を 1〜2 行で報告して終了。

## エラー時

- `origin remote が未設定` → ガイダンス通り `git remote add` を促す
- `git push failed` → 認証エラーの可能性。`git remote -v` を表示して原因を伝える（Git Credential Manager の確認 / SSH キー / トークン）
- `nothing to commit` → 変更なし。Step 2 で early-return する
