# install.ps1 — r-pwa-20260506 バンドルを本番 PC のプロジェクトへ展開する
# 使い方: pwsh -ExecutionPolicy Bypass -File install.ps1

[CmdletBinding()]
param(
    [string]$ProjectRoot,
    [switch]$DryRun
)

$ErrorActionPreference = "Stop"
$BundleRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$FilesRoot = Join-Path $BundleRoot "files"

if (-not (Test-Path $FilesRoot)) {
    Write-Error "Bundle files/ ディレクトリが見つかりません: $FilesRoot"
    exit 1
}

# 1) ProjectRoot 自動探索
if (-not $ProjectRoot) {
    $candidates = @(
        "$env:USERPROFILE\OneDrive\デスクトップ\0.フジ\900.ClaudeCode",
        "C:\Users\user\OneDrive\デスクトップ\0.フジ\900.ClaudeCode",
        "C:\Users\jl4lv\OneDrive\デスクトップ\0.フジ\900.ClaudeCode"
    )
    foreach ($c in $candidates) {
        if (Test-Path (Join-Path $c "CLAUDE.md")) {
            $ProjectRoot = $c
            break
        }
    }
}

if (-not $ProjectRoot -or -not (Test-Path (Join-Path $ProjectRoot "CLAUDE.md"))) {
    Write-Error "ProjectRoot が決定できません。 -ProjectRoot <path> で明示指定してください"
    exit 1
}

Write-Host "Bundle:        $BundleRoot" -ForegroundColor Cyan
Write-Host "ProjectRoot:   $ProjectRoot" -ForegroundColor Cyan
if ($DryRun) {
    Write-Host "Mode:          DryRun (no changes will be applied)" -ForegroundColor Yellow
} else {
    Write-Host "Mode:          Apply" -ForegroundColor Green
}
Write-Host ""

$ts = Get-Date -Format "yyyyMMdd_HHmmss"

# 2) 全ファイル列挙
$srcFiles = Get-ChildItem -Path $FilesRoot -Recurse -File
$summary = @{ copied = 0; backed_up = 0; created = 0; skipped = 0 }

foreach ($srcFile in $srcFiles) {
    $relPath = $srcFile.FullName.Substring($FilesRoot.Length + 1)
    $dstFile = Join-Path $ProjectRoot $relPath
    $dstDir  = Split-Path -Parent $dstFile

    # 親ディレクトリ作成
    if (-not (Test-Path $dstDir)) {
        if ($DryRun) {
            Write-Host "  [mkdir]   $dstDir" -ForegroundColor DarkGray
        } else {
            New-Item -ItemType Directory -Path $dstDir -Force | Out-Null
        }
    }

    if (Test-Path $dstFile) {
        # 既存 → backup してから上書き
        $bak = "$dstFile.bak_$ts"
        if ($DryRun) {
            Write-Host "  [bak+ovr] $relPath  -> $bak" -ForegroundColor Yellow
        } else {
            Copy-Item -LiteralPath $dstFile -Destination $bak -Force
            Copy-Item -LiteralPath $srcFile.FullName -Destination $dstFile -Force
        }
        $summary.backed_up++
        $summary.copied++
    } else {
        # 新規
        if ($DryRun) {
            Write-Host "  [new]     $relPath" -ForegroundColor Green
        } else {
            Copy-Item -LiteralPath $srcFile.FullName -Destination $dstFile -Force
        }
        $summary.created++
        $summary.copied++
    }
}

Write-Host ""
Write-Host "Summary:" -ForegroundColor Cyan
Write-Host "  Copied:    $($summary.copied)"
Write-Host "  Backed up: $($summary.backed_up)  (.bak_$ts)"
Write-Host "  Created:   $($summary.created)"
Write-Host ""

if ($DryRun) {
    Write-Host "DryRun でした。実適用するには -DryRun を外して再実行してください。" -ForegroundColor Yellow
    exit 0
}

Write-Host "次のステップ:" -ForegroundColor Cyan
Write-Host "  1. 依存確認:" -ForegroundColor White
Write-Host '     & "C:\Users\user\AppData\Local\Python\pythoncore-3.14-64\python.exe" -c "import fastapi, sqlalchemy, uvicorn, pydantic, multipart, httpx; print(''OK'')"'
Write-Host "  2. TeamTasks サーバ再起動 (タスクスケジューラ または 014.TeamTasks/server/start.bat)"
Write-Host "  3. 動作確認:"
Write-Host "     curl https://sfuji.f5.si/tasksapi/r/    (空配列が返れば OK)"
Write-Host "     start https://sfuji.f5.si/tasks/r/      (PWA 表示確認)"
Write-Host "  4. /handoff load 434                       (Claude Code セッションで)"
