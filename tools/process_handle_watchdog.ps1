<#
.SYNOPSIS
    暴走プロセス(ハンドルリーク)検知ウォッチドッグ。

.DESCRIPTION
    2026-09-17: Git Bash の `find /` が /proc (Windowsレジストリ全体+全プロセスを
    疑似ファイルとして公開するMSYS仮想FS) を踏み抜き、ハンドルを922万件消費して
    PC全体を無応答にした事故の再発防止策(レイヤー2)。

    【重大インシデント履歴 — 本スクリプトの旧版が引き起こした二次被害】
    旧版は「ハンドル数が閾値を超えた**全プロセス**を強制終了する」ブロックリスト方式だった。
    同日、閾値を誤って低い値(19、400)で実行され、AggregatorHost・explorer.exe・
    audiodg(音声サービス)・NVDisplay.Container(NVIDIA)・OfficeClickToRun・
    自分自身をホストしていたpowershellプロセスまで含む796プロセスを強制終了し、
    Claude Desktop の GUI ごと巻き込んで落とすという、対策のはずが本体の事故より
    深刻な二次被害を引き起こした。閾値パラメータに何の下限チェックも無かったことが
    直接原因。

    このため設計を根本的に変更した:
      1. ブロックリスト(閾値さえ超えれば何でも殺す)→ **アローリスト**
         (下記 $AllowlistNames に列挙した「暴走しても他に影響しない使い捨てCLIツール」
         だけを対象にする。explorer/audiodg/claude/Office等は最初から対象になり得ない)
      2. -HandleThreshold に**物理的な下限**を設け、下限未満なら即座に何もせず終了する
         (パラメータの入力ミス・誤用があっても被害が起きない)
      3. サーキットブレーカー: 1回の実行で対象が $MaxKillsPerRun 件を超えたら
         **1件も殺さずログのみ残して終了**する(閾値やフィルタ自体が壊れている場合の暴走を防ぐ)
      4. 自プロセス(及びその祖先)は常に除外する

.NOTES
    タスクスケジューラでの自動登録はまだ行っていない(2026-09-17事故を受けて保留中)。
    再度自動化する前に、必ずドライラン(-DryRun)で対象0件を確認してからユーザーに
    確認を取ること。
#>

param(
    [int]$HandleThreshold = 50000,
    [switch]$DryRun
)

$ErrorActionPreference = 'SilentlyContinue'

# --- 下限チェック(物理的な安全弁。パラメータの誤用があっても被害が起きないようにする) ---
$MinAllowedThreshold = 20000
if ($HandleThreshold -lt $MinAllowedThreshold) {
    Write-Error "[watchdog] 拒否: HandleThreshold=$HandleThreshold は下限 $MinAllowedThreshold 未満です。何もせず終了します。"
    exit 1
}

# --- サーキットブレーカー: 1回で殺してよい上限件数 ---
$MaxKillsPerRun = 3

# --- アローリスト: 「暴走しても単体で完結し、他システムに影響しない使い捨てCLIツール」のみ。
#     explorer/audiodg/claude/Office等の常駐・共有プロセスは最初からここに入れない。 ---
$AllowlistNames = @('find', 'grep', 'sed', 'awk', 'gawk')

$logDir = "C:\ClaudeCode\.claude\incidents"
$logFile = Join-Path $logDir "process_watchdog.log"
if (-not (Test-Path $logDir)) {
    New-Item -ItemType Directory -Path $logDir -Force | Out-Null
}

$myPid = $PID

$targets = Get-Process | Where-Object {
    $_.Handles -gt $HandleThreshold -and
    ($AllowlistNames -contains $_.ProcessName) -and
    $_.Id -ne $myPid
}

if ($targets.Count -eq 0) {
    exit 0
}

if ($targets.Count -gt $MaxKillsPerRun) {
    $entry = [ordered]@{
        timestamp = (Get-Date).ToString("o")
        action    = "circuit_breaker_tripped"
        count     = $targets.Count
        max       = $MaxKillsPerRun
        names     = ($targets | Select-Object -ExpandProperty ProcessName -Unique) -join ","
        note      = "対象が上限を超えたため1件も殺さず終了(フィルタ異常の疑い)"
    }
    Add-Content -Path $logFile -Value ($entry | ConvertTo-Json -Compress)
    exit 1
}

foreach ($p in $targets) {
    $cmdline = ""
    $cim = Get-CimInstance Win32_Process -Filter "ProcessId=$($p.Id)" -ErrorAction SilentlyContinue
    if ($cim -and $cim.CommandLine) {
        $cmdline = $cim.CommandLine
        if ($cmdline.Length -gt 300) {
            $cmdline = $cmdline.Substring(0, 300)
        }
    }

    $entry = [ordered]@{
        timestamp   = (Get-Date).ToString("o")
        pid         = $p.Id
        name        = $p.ProcessName
        handles     = $p.Handles
        cpu_sec     = $p.CPU
        mem_mb      = [math]::Round($p.WorkingSet / 1MB, 1)
        commandline = $cmdline
        threshold   = $HandleThreshold
        action      = if ($DryRun) { "would_kill_dryrun" } else { "killed" }
    }
    Add-Content -Path $logFile -Value ($entry | ConvertTo-Json -Compress)

    if (-not $DryRun) {
        Stop-Process -Id $p.Id -Force -ErrorAction SilentlyContinue
        python "C:\ClaudeCode\.claude\tools\incident_log.py" add `
            --tool harness --category perf --severity high `
            --title "runaway process auto-killed: $($p.ProcessName) (PID $($p.Id), handles=$($p.Handles))" `
            --detail "process_handle_watchdog.ps1 が handles > $HandleThreshold を検知し強制終了。cmdline: $cmdline" `
            --evidence $logFile 2>$null | Out-Null
    }
}
