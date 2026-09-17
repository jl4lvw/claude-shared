<#
.SYNOPSIS
    CPU使用状況の定点観測(読み取り専用・プロセスは一切操作しない)。

.DESCRIPTION
    2026-09-17: 断続的なCPU100%張り付きの原因を後から分析できるよう、
    プロセスごとのCPU/ハンドル/メモリと全体負荷を10分おきにCSVへ追記する。

    process_handle_watchdog.ps1 (同日、閾値の下限チェック無しで796プロセスを誤って
    強制終了しClaude Desktop GUIを巻き込んだ) の反省を踏まえ、本スクリプトは
    **Stop-Process 等の変更系コマンドを一切含まない**、Get-Process / Get-CimInstance
    による読み取りと Add-Content によるログ追記のみで構成する。

.NOTES
    後で分析しやすいよう CSV (long format: 1スナップショットにつき複数行) で記録する。
    列: timestamp, total_cpu_pct, pid, name, cpu_total_sec, handles, mem_mb, threads
    cpu_total_sec は累積値なので、分析時に同一 pid+name の連続スナップショット間の
    差分を取ると「その10分間で実際に使ったCPU秒数」が求まる。
#>

param(
    [int]$TopN = 40
)

$ErrorActionPreference = 'SilentlyContinue'

$logDir = "C:\ClaudeCode\.claude\incidents"
$logFile = Join-Path $logDir "cpu_snapshots.csv"
if (-not (Test-Path $logDir)) {
    New-Item -ItemType Directory -Path $logDir -Force | Out-Null
}

$timestamp = (Get-Date).ToString("o")
$totalCpuPct = (Get-CimInstance Win32_Processor | Measure-Object -Property LoadPercentage -Average).Average

$procs = Get-Process | Sort-Object CPU -Descending | Select-Object -First $TopN

$rows = foreach ($p in $procs) {
    [PSCustomObject]@{
        timestamp     = $timestamp
        total_cpu_pct = $totalCpuPct
        pid           = $p.Id
        name          = $p.ProcessName
        cpu_total_sec = [math]::Round($p.CPU, 2)
        handles       = $p.Handles
        mem_mb        = [math]::Round($p.WorkingSet / 1MB, 1)
        threads       = $p.Threads.Count
    }
}

$rows | Export-Csv -Path $logFile -Append -NoTypeInformation -Encoding utf8 -Force
