---
name: g-ul
description: プロジェクトの `.claude/{skills,commands,tools,rules,memory}` を `%USERPROFILE%\claude-shared\` にミラーコピーしてから Git push する。Option C ミラー方式。コミットメッセージは引数任意（未指定時は自動生成）。
---

# /g-ul — claude-shared upload (mirror approach)

プロジェクトの `.claude/{skills,commands,tools,rules,memory}` を `%USERPROFILE%\claude-shared\` にミラーコピー (`robocopy /MIR`) してから Git に push する。

**Option C ミラー方式**: ジャンクションを使わない。OneDrive が `.claude/` をジャンクション越しに破壊する事故を構造的に回避。

## 使い方

```
/g-ul                                   # 自動メッセージで push
/g-ul "skill: refactor handoff"         # メッセージ指定で push
```

## 手順

### Step 1: 引数からコミットメッセージを取り出す

ユーザー引数があればそれをメッセージに使う。未指定なら `sync: <ComputerName> <yyyy-MM-dd HH:mm>` を自動生成。

### Step 2: PowerShell で実行

**Bash ではなく PowerShell ツール**を使うこと。`<USER_ARGS_OR_EMPTY>` プレースホルダは、Claude が引数の有無で置換:
- 引数あり → そのテキストを埋め込む
- 引数なし → **必ず空文字列 `''` に置換**

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

# Sanity: .claude/skills と claude-shared/skills のファイル数を比較
# .claude が極端に少ない場合は abort（OneDrive 同期途中などで全消し事故防止）
$srcSkills = Join-Path $claudeDir 'skills'
$dstSkills = Join-Path $shared 'skills'
if ((Test-Path -LiteralPath $srcSkills) -and (Test-Path -LiteralPath $dstSkills)) {
    $srcCount = @(Get-ChildItem -LiteralPath $srcSkills -Recurse -File -Force -ErrorAction SilentlyContinue).Count
    $dstCount = @(Get-ChildItem -LiteralPath $dstSkills -Recurse -File -Force -ErrorAction SilentlyContinue).Count
    if (($dstCount -gt 5) -and ($srcCount -lt ($dstCount * 0.5))) {
        Write-Host ""
        Write-Host "Sanity check FAIL: .claude/skills=$srcCount files but claude-shared/skills=$dstCount" -ForegroundColor Red
        Write-Host "ミラーすると大量削除になります。OneDrive 同期途中？ 中断します。" -ForegroundColor Red
        return
    }
}

# Step A: .claude/* -> claude-shared/* ミラー
Write-Host ""
Write-Host "=== Mirror .claude/ -> claude-shared/ ==="
foreach ($t in $targets) {
    $src = Join-Path $claudeDir $t
    $dst = Join-Path $shared $t
    if (-not (Test-Path -LiteralPath $src)) {
        Write-Host "  $t : .claude/ 側になし、skip"
        continue
    }
    Write-Host "  mirror $t ..."
    & robocopy $src $dst /MIR /NFL /NDL /NP /R:2 /W:1 `
        /XD __pycache__ '.bootstrap-bak-*' '.migrate-pending-*' `
        /XF '*.bak_*' '*.pyc' '.deepseek_usage_session.json' | Out-Null
    if ($LASTEXITCODE -ge 8) {
        Write-Host "    robocopy ERROR (exit=$LASTEXITCODE) for $t" -ForegroundColor Red
        return
    }
}
Write-Host "  mirror done." -ForegroundColor Green

Push-Location -LiteralPath $shared
try {
    $remoteUrl = git remote get-url origin 2>$null
    if (-not $remoteUrl) {
        Write-Host "origin remote 未設定" -ForegroundColor Red
        return
    }
    Write-Host ""
    Write-Host "remote: $remoteUrl"

    $status = git status --porcelain
    if ([string]::IsNullOrWhiteSpace($status)) {
        Write-Host "変更なし。push 不要。" -ForegroundColor Yellow
        return
    }

    Write-Host ""
    Write-Host "=== 変更ファイル ==="
    git status --short

    git add -A | Out-Null

    Write-Host ""
    Write-Host "=== 差分サマリ ==="
    git diff --cached --stat

    $msg = '<USER_ARGS_OR_EMPTY>'
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

    $upstream = git rev-parse --abbrev-ref --symbolic-full-name '@{u}' 2>$null
    Write-Host ""
    Write-Host "=== push ==="
    if (-not $upstream) {
        $branch = git branch --show-current
        Write-Host "upstream 未設定。--set-upstream で push ($branch)" -ForegroundColor Yellow
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

push の commit hash と short stat を 1〜2 行で報告。

## エラー時

- `CWD に .claude/ が見つかりません` → プロジェクトルートで実行する
- `claude-shared not found` → 新PC は `git clone https://github.com/jl4lvw/claude-shared.git C:\Users\<user>\claude-shared`
- `Sanity check FAIL` → .claude/ が極端に少ない（OneDrive 同期途中の可能性）。少し待って再試行
- `robocopy ERROR (exit>=8)` → ファイルロック / 権限。Claude Code を完全終了して再試行
- `git push failed` → 認証エラー。`git remote -v` で確認
