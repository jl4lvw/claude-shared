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

    2026-09-21 追記: cpu_snapshots.csv は累積CPU順の上位N件しか見えず、保護プロセス
    (System/MsMpEng)と短命プロセスが記録に出ない(9/17-9/21のログで可視プロセスの合計は
    実負荷の約1割しか説明できなかった)。そこで同じスクリプトの末尾で、パフォーマンス
    カウンタから「現在値」を5秒サンプリングして cpu_current.csv にも追記する。
    unattributed_pct(全体負荷 - 可視プロセス合計)が大きければ、短命プロセスの連発か
    カーネル/ドライバ時間が原因。既存の cpu_snapshots.csv の列は変えていない。
#>

param(
    [int]$TopN = 40,
    [int]$SampleSec = 5   # seconds of current-rate sampling (cpu_current.csv)
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

# ---------------------------------------------------------------------------
# 2026-09-21 added: current-rate sampling -> cpu_current.csv
#
# Why: the cumulative-CPU-sorted top-N above cannot show (a) protected processes
# (System / MsMpEng: CPU is unreadable without admin), (b) processes that started and
# exited between snapshots. In the 9/17-9/21 log the visible processes explained only
# ~11% of CPU while the logged load averaged ~50%. This block samples the *current* rate
# with performance counters (they include protected processes) and records the gap
# between the system total and the sum of visible processes (unattributed_pct).
# A large unattributed_pct = short-lived process churn or kernel/driver time.
# Read-only, like the block above. Failure here never affects the CSV above.
# ---------------------------------------------------------------------------
try {
    $curFile = Join-Path $logDir "cpu_current.csv"
    if ((Test-Path $curFile) -and ((Get-Item $curFile).Length -gt 20MB)) {
        Move-Item -Path $curFile -Destination ($curFile + ".1") -Force
    }
    $cores = [Environment]::ProcessorCount
    $paths = @(
        '\Processor(_Total)\% Processor Time',
        '\Processor(_Total)\% Privileged Time',
        '\Processor(_Total)\% Interrupt Time',
        '\Processor(_Total)\% DPC Time',
        '\Process(*)\% Processor Time',
        '\Process(*)\ID Process'
    )
    $samples = Get-Counter -Counter $paths -SampleInterval 1 -MaxSamples $SampleSec -ErrorAction SilentlyContinue
    if ($samples) {
        $tot = New-Object System.Collections.Generic.List[double]
        $priv = New-Object System.Collections.Generic.List[double]
        $intr = New-Object System.Collections.Generic.List[double]
        $dpc = New-Object System.Collections.Generic.List[double]
        $unattr = New-Object System.Collections.Generic.List[double]
        $procSum = @{}
        $pidOf = @{}
        foreach ($s in $samples) {
            $visible = 0.0
            $total = $null
            foreach ($c in $s.CounterSamples) {
                $path = $c.Path
                if ($path -like '*\processor(_total)\% processor time') { $total = $c.CookedValue; $tot.Add($c.CookedValue) }
                elseif ($path -like '*\processor(_total)\% privileged time') { $priv.Add($c.CookedValue) }
                elseif ($path -like '*\processor(_total)\% interrupt time') { $intr.Add($c.CookedValue) }
                elseif ($path -like '*\processor(_total)\% dpc time') { $dpc.Add($c.CookedValue) }
                elseif ($path -like '*\process(*)\% processor time') {
                    $inst = $c.InstanceName
                    if ($inst -eq '_total' -or $inst -eq 'idle') { continue }
                    $v = $c.CookedValue / $cores
                    $visible += $v
                    if ($procSum.ContainsKey($inst)) { $procSum[$inst] += $v } else { $procSum[$inst] = $v }
                }
                elseif ($path -like '*\process(*)\id process') { $pidOf[$c.InstanceName] = [int]$c.CookedValue }
            }
            if ($null -ne $total) { $unattr.Add([math]::Max(0.0, $total - $visible)) }
        }
        $n = [math]::Max(1, $samples.Count)
        function Get-Mean($list) { if ($list.Count -eq 0) { return $null } ; return [math]::Round((($list | Measure-Object -Average).Average), 1) }
        $totMax = if ($tot.Count -gt 0) { [math]::Round((($tot | Measure-Object -Maximum).Maximum), 1) } else { $null }
        $top = $procSum.GetEnumerator() | Sort-Object Value -Descending | Select-Object -First 8
        $rank = 0
        $curRows = foreach ($t in $top) {
            $rank++
            $procId = $pidOf[$t.Key]
            $cmd = ''
            if ($procId) {
                $w = Get-CimInstance Win32_Process -Filter ("ProcessId=" + $procId) -ErrorAction SilentlyContinue
                if ($w -and $w.CommandLine) {
                    $cmd = $w.CommandLine
                    # command lines may carry secrets: redact key/token/password-like values, then truncate
                    $cmd = [regex]::Replace($cmd, '(?i)(key|token|secret|passw(or)?d|pin|authorization)([=: ]+)("[^"]*"|\S+)', '$1$3***')
                    if ($cmd.Length -gt 200) { $cmd = $cmd.Substring(0, 200) }
                }
            }
            [PSCustomObject]@{
                timestamp        = $timestamp
                total_mean_pct   = Get-Mean $tot
                total_max_pct    = $totMax
                privileged_pct   = Get-Mean $priv
                interrupt_pct    = Get-Mean $intr
                dpc_pct          = Get-Mean $dpc
                unattributed_pct = Get-Mean $unattr
                rank             = $rank
                name             = ($t.Key -replace '#\d+$', '')
                pid              = $procId
                cpu_pct          = [math]::Round($t.Value / $n, 1)
                cmdline          = $cmd
            }
        }
        if ($curRows) { $curRows | Export-Csv -Path $curFile -Append -NoTypeInformation -Encoding utf8 -Force }
    }
} catch {
    # never let the added sampling break the scheduled task
}
