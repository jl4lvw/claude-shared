# Restart-PwaApi.ps1
# Stop-ScheduledTask -> Cleanup-PwaPort.ps1 -> Start-ScheduledTask の安全な再起動。
# デフォルトは ExpectedExe / ExpectedCommandContains の両方一致時のみ kill する (誤kill 防止)。
# -Force を付けたときだけ -KillAnyListener を併用する (緊急用)。
[CmdletBinding()]
param(
    [Parameter(Mandatory)] [ValidatePattern('^[^\*\?]+$')] [string] $TaskName,
    [string] $TaskPath = "\",
    [Parameter(Mandatory)] [ValidatePattern('^[A-Za-z0-9_.-]+$')] [string] $Name,
    [Parameter(Mandatory)] [ValidateRange(1,65535)] [int] $Port,
    [Parameter(Mandatory)] [string] $ExpectedExe,
    [string] $ExpectedCommandContains = "main:app",
    [ValidateRange(1,3600)] [int] $TimeoutSeconds = 120,
    [switch] $Force
)

$ErrorActionPreference = "Stop"
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$cleanup = Join-Path $here "Cleanup-PwaPort.ps1"

function Resolve-LongPath {
    param([string] $Path)
    if (-not $Path) { return $null }
    try { return (Get-Item -LiteralPath $Path -ErrorAction Stop).FullName } catch {
        try { return [IO.Path]::GetFullPath($Path) } catch { return $Path }
    }
}

function Get-ListenerPids {
    [OutputType([int[]])]
    param([int] $Port)
    $list = [System.Collections.Generic.List[int]]::new()
    $pat = ":$Port\s+\S+\s+LISTENING\s+(\d+)"
    foreach ($l in (& netstat -ano)) {
        if ($l -match $pat) {
            $p = [int]$matches[1]
            if (-not $list.Contains($p)) { $list.Add($p) | Out-Null }
        }
    }
    Write-Output -NoEnumerate ($list.ToArray())
}

function Test-ListenerMatchesExpected {
    param([int[]] $ListenerPids, [string] $ExpectedExe, [string] $ExpectedCommandContains)
    foreach ($lpid in $ListenerPids) {
        $info = Get-CimInstance Win32_Process -Filter "ProcessId = $lpid" -ErrorAction SilentlyContinue
        if (-not $info) { continue }
        $exeOk = $false
        $cmdOk = $false
        if ($ExpectedExe -and $info.ExecutablePath) {
            $a = Resolve-LongPath $info.ExecutablePath
            $b = Resolve-LongPath $ExpectedExe
            if ($a -and $b -and ($a -ieq $b)) { $exeOk = $true }
        }
        if ($ExpectedCommandContains -and $info.CommandLine) {
            if ($info.CommandLine.IndexOf($ExpectedCommandContains, [StringComparison]::OrdinalIgnoreCase) -ge 0) {
                $cmdOk = $true
            }
        }
        if ($exeOk -and $cmdOk) { return $lpid }
    }
    return $null
}

# タスク存在確認 (1件のみ)
$tasks = @(Get-ScheduledTask -TaskName $TaskName -TaskPath $TaskPath -ErrorAction SilentlyContinue)
if ($tasks.Count -eq 0) { throw "[$Name] Task not found: $TaskName" }
if ($tasks.Count -gt 1) { throw "[$Name] Ambiguous TaskName matched $($tasks.Count) tasks: $TaskName" }

Write-Host "[$Name] Stop-ScheduledTask"
try {
    Stop-ScheduledTask -TaskName $TaskName -TaskPath $TaskPath
} catch {
    Write-Warning "[$Name] Stop-ScheduledTask failed (continuing with cleanup): $($_.Exception.Message)"
}
Start-Sleep -Seconds 2

Write-Host "[$Name] Cleanup-PwaPort"
$cleanupArgs = @{
    Name = $Name
    Port = $Port
    ExpectedExe = $ExpectedExe
    ExpectedCommandContains = $ExpectedCommandContains
    TimeoutSeconds = $TimeoutSeconds
}
if ($Force) { $cleanupArgs['KillAnyListener'] = $true }
try {
    & $cleanup @cleanupArgs
} catch {
    $remPids = Get-ListenerPids -Port $Port
    $remStr = if ($remPids.Count -gt 0) { ($remPids -join ',') } else { '(none)' }
    throw ("[$Name] cleanup failed: " + $_.Exception.Message + "; remaining listener PIDs=" + $remStr + "; aborting start")
}

Write-Host "[$Name] Start-ScheduledTask"
Start-ScheduledTask -TaskName $TaskName -TaskPath $TaskPath

# 起動確認: netstat ベース + 期待プロセス再検証
$sw = [System.Diagnostics.Stopwatch]::StartNew()
$timeoutMs = $TimeoutSeconds * 1000
$matchedPid = $null
while ($sw.ElapsedMilliseconds -lt $timeoutMs) {
    $listenerPids = Get-ListenerPids -Port $Port
    if ($listenerPids.Count -gt 0) {
        $matchedPid = Test-ListenerMatchesExpected -ListenerPids $listenerPids -ExpectedExe $ExpectedExe -ExpectedCommandContains $ExpectedCommandContains
        if ($matchedPid) {
            Write-Host "[$Name] OK: port $Port LISTENING by expected process (PID $matchedPid)"
            exit 0
        }
        # listener はあるが期待値と不一致 (PythonManager stub 段階の可能性) → さらに待つ
    }
    $remaining = $timeoutMs - $sw.ElapsedMilliseconds
    if ($remaining -le 0) { break }
    Start-Sleep -Milliseconds ([Math]::Min(500, [int]$remaining))
}

$listenerPids = Get-ListenerPids -Port $Port
if ($listenerPids.Count -gt 0) {
    $pidStr = $listenerPids -join ','
    throw ("[$Name] port $Port LISTENING by unexpected PID(s) " + $pidStr + "; ExpectedExe/ExpectedCommandContains did not match")
}
throw "[$Name] task started but port $Port is not LISTENING within $TimeoutSeconds s"
