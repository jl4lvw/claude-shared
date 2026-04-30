# Cleanup-PwaPort.ps1
# Kill stale listener (and its process tree) on the given port until the port is bindable.
# Uses netstat (fast) instead of Get-NetTCPConnection (slow on Windows w/ many connections).
[CmdletBinding()]
param(
    [Parameter(Mandatory)] [ValidatePattern('^[A-Za-z0-9_.-]+$')] [string] $Name,
    [Parameter(Mandatory)] [ValidateRange(1,65535)] [int] $Port,
    [string] $HostAddress = "127.0.0.1",
    [string] $PidFile,
    [string] $ExpectedExe,
    [string] $ExpectedCommandContains,
    [ValidateRange(1,3600)] [int] $TimeoutSeconds = 30,
    [switch] $KillAnyListener
)

$ErrorActionPreference = "Stop"
if (-not $PidFile) { $PidFile = Join-Path $env:TEMP "pwa-task-$Name.pid" }

# 自殺バグ防止: スクリプト起動時の親プロセス PID を記憶
$script:ParentPid = (Get-CimInstance Win32_Process -Filter "ProcessId = $PID" -ErrorAction SilentlyContinue).ParentProcessId

# ===== ヘルパー =====
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
    # Write-Output -NoEnumerate: 配列をそのまま返す (PS5.1 unwrap 抑止)
    Write-Output -NoEnumerate ($list.ToArray())
}

# Win32_Process を1回だけ取得して再利用 (再帰CIM呼び出しの性能劣化を回避)
function Get-AllProcessSnapshot {
    Get-CimInstance Win32_Process -ErrorAction SilentlyContinue
}

function Get-ProcessTreeIds {
    param([int] $RootPid, [object[]] $Snapshot, [int] $Depth = 0)
    if ($Depth -gt 10) { return @($RootPid) }  # 循環防御
    $children = $Snapshot | Where-Object { $_.ParentProcessId -eq $RootPid }
    $ids = @($RootPid)
    foreach ($child in $children) {
        if ($child.ProcessId -ne $RootPid) {
            $ids += Get-ProcessTreeIds -RootPid $child.ProcessId -Snapshot $Snapshot -Depth ($Depth + 1)
        }
    }
    $ids
}

function Should-StopProcess {
    param([object] $Info, [bool] $IsListener)
    if (-not $Info) { return $false }
    $TargetPid = [int] $Info.ProcessId
    if ($TargetPid -eq $PID) { return $false }
    if ($TargetPid -eq $script:ParentPid) { return $false }

    $exeMatch = $false
    $cmdMatch = $false
    if ($ExpectedExe -and $Info.ExecutablePath) {
        $a = Resolve-LongPath $Info.ExecutablePath
        $b = Resolve-LongPath $ExpectedExe
        if ($a -and $b -and ($a -ieq $b)) { $exeMatch = $true }
    }
    if ($ExpectedCommandContains -and $Info.CommandLine) {
        if ($Info.CommandLine.IndexOf($ExpectedCommandContains, [StringComparison]::OrdinalIgnoreCase) -ge 0) {
            $cmdMatch = $true
        }
    }

    # 両方指定された場合は AND (より厳格)。片方のみ指定された場合はそれだけで OK。
    if ($ExpectedExe -and $ExpectedCommandContains) { return ($exeMatch -and $cmdMatch) }
    if ($ExpectedExe) { return $exeMatch }
    if ($ExpectedCommandContains) { return $cmdMatch }

    # どちらも未指定で KillAnyListener が立っている場合のみ無条件 kill (緊急用)
    if ($KillAnyListener -and $IsListener) { return $true }
    return $false
}

function Test-PortBindable {
    param([string] $Address, [int] $Port)
    $socket = $null
    try {
        $ip = [Net.IPAddress]::Parse($Address)
        $socket = [Net.Sockets.Socket]::new($ip.AddressFamily, [Net.Sockets.SocketType]::Stream, [Net.Sockets.ProtocolType]::Tcp)
        $socket.SetSocketOption([Net.Sockets.SocketOptionLevel]::Socket, [Net.Sockets.SocketOptionName]::ExclusiveAddressUse, $true)
        $socket.Bind([Net.IPEndPoint]::new($ip, $Port))
        return $true
    } catch {
        return $false
    } finally {
        if ($socket) { $socket.Close(); $socket.Dispose() }
    }
}

# ===== 本処理 =====
$snapshot = Get-AllProcessSnapshot

# kill 候補を収集
$candidatePids = @()

if (Test-Path $PidFile) {
    $pidText = (Get-Content $PidFile -ErrorAction SilentlyContinue | Select-Object -First 1)
    $parsedPid = 0
    if ([int]::TryParse($pidText, [ref] $parsedPid)) {
        if ($candidatePids -notcontains $parsedPid) { $candidatePids += $parsedPid }
    }
}
$listenerPidsArr = Get-ListenerPids -Port $Port
foreach ($lp in $listenerPidsArr) {
    if ($candidatePids -notcontains $lp) { $candidatePids += $lp }
}

# 検出時のプロセス情報を保持 (TOCTOU防御のため kill 直前に再照合)
$plannedKills = @()
foreach ($cpid in $candidatePids) {
    $info = $snapshot | Where-Object { $_.ProcessId -eq $cpid } | Select-Object -First 1
    if (-not $info) { continue }
    $isListener = ($listenerPidsArr -contains $cpid)
    if (-not (Should-StopProcess -Info $info -IsListener $isListener)) {
        Write-Verbose "skip pid=$cpid (not matching expected)"
        continue
    }
    $plannedKills += [PSCustomObject]@{
        Pid          = [int]$info.ProcessId
        OriginalExe  = (Resolve-LongPath $info.ExecutablePath)
        OriginalCmd  = $info.CommandLine
        OriginalCreated = $info.CreationDate
    }
}

# プロセスツリー展開 + kill (TOCTOU 再照合付き)
foreach ($plan in $plannedKills) {
    $tree = Get-ProcessTreeIds -RootPid $plan.Pid -Snapshot $snapshot | Select-Object -Unique
    foreach ($treePid in $tree) {
        if ($treePid -eq $PID -or $treePid -eq $script:ParentPid) { continue }
        # kill 直前に同一プロセスか確認 (PID再利用防御)
        $live = Get-CimInstance Win32_Process -Filter "ProcessId = $treePid" -ErrorAction SilentlyContinue
        if (-not $live) { continue }
        # ルートPIDだけは元情報と一致確認、子は親が一致したことで間接的に保証
        if ($treePid -eq $plan.Pid) {
            if ($plan.OriginalCreated -and $live.CreationDate -ne $plan.OriginalCreated) {
                Write-Warning "[$Name] pid=$treePid was reused (creation time changed); skip"
                continue
            }
        }
        $procName = $live.Name
        try {
            Stop-Process -Id $treePid -Force -ErrorAction Stop
            Write-Host "[$Name] kill pid=$treePid name=$procName"
        } catch {
            Write-Warning ("[$Name] kill FAILED pid=$treePid name=" + $procName + ": " + $_.Exception.Message)
        }
    }
}

# bind 可能になるまで待機 (Stopwatch ベースで deadline 厳守)
$sw = [System.Diagnostics.Stopwatch]::StartNew()
$timeoutMs = $TimeoutSeconds * 1000
while ($sw.ElapsedMilliseconds -lt $timeoutMs) {
    $stillListening = (Get-ListenerPids -Port $Port).Count
    if ($stillListening -eq 0 -and (Test-PortBindable -Address $HostAddress -Port $Port)) {
        Remove-Item $PidFile -Force -ErrorAction SilentlyContinue
        Write-Host "[$Name] port $Port is now free"
        exit 0
    }
    $remaining = $timeoutMs - $sw.ElapsedMilliseconds
    if ($remaining -le 0) { break }
    Start-Sleep -Milliseconds ([Math]::Min(500, [int]$remaining))
}

# 最終診断
$remaining = Get-ListenerPids -Port $Port
$remainingList = if ($remaining.Count -gt 0) { ($remaining -join ',') } else { '(none)' }
throw "port $Port did not become bindable within $TimeoutSeconds s; remaining listener PIDs=$remainingList"
