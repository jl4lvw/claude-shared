<#
relay 新着見張り(2026-09-30)。常駐GUIを廃止し、Claude Code 本体で処理する方式の第1段。

「待っている間はトークンを使わない」ための門番。GET /messages/summary(副作用なし・超軽量)
だけを見て、着手してよい新着が増えたときに **終了する**。Claude Code の background で
走らせておくと、終了がそのままセッションを起こす合図になる(TS構築の見張りと同じ方式)。

  -Mode watch   : 監視する。新着で終了(exit 0)、上限時間で終了(exit 0・本文に「変化なし」)
  -Mode status  : いま台帳と受信箱がどうなっているかを表示するだけ
  -Mode init    : いまの max_pending_id を「処理済み」として記録する(溜まっている分を無視)
  -Mode ack     : 処理し終えたIDを記録する(-AckId。省略時はいまの max_pending_id)。
                  その範囲に誰も手を付けていない着信(未読・処理権なし)が残っていれば断る(exit 7)。
                  処理しないと決めた件だけ -Force で記録できる
  -Mode beat    : 生存報告を1回だけ送る(処理が長引いているときにセッションから呼ぶ)
  -Mode arm     : このセッションを「待ち受け」として登録する(危険操作の歯止めがこのセッションに効く)
  -Mode disarm  : 登録を外す

危険操作の歯止め(2026-09-30): 待ち受けと処理役は確認なし(bypass)で動かすので、その代わりに
.claude\hooks\relay_watch_guard.py(PreToolUse)が削除・force push・プロセス停止などを拒否する。
フックは %LOCALAPPDATA%\RelayWatch\guard_session.json のセッションID と一致したときだけ働く。
arm はここへ環境変数 CLAUDE_CODE_SESSION_ID を書く(watch の起動時にも自動で書く)。

**処理中はこのスクリプトは走っていない**(検知して終了しているため)。だから busy フラグは
持たない。セッションが処理を終えたら ack して、もう一度 watch で起動する。

watch の終了コード: 0=新着あり または 変化なし(出力の文言で見分ける) / 3=relay サーバーに届かない /
4=別の見張りを止められない・セッションIDが無く担当を確かめられない / 6=待ち受けの担当が別のセッション
(引き継がれた・登録が外された。起動し直さない)。ロックは %LOCALAPPDATA%\RelayWatch\watch.lock。

引き継ぎ(2026-09-30 運用者決定: 後から起動したセッションが勝つ): /m watch は -Mode arm で担当を
自分へ書き換えてから watch を起動する。前の担当の見張りは、担当の変化に1秒以内に気づいて exit 6 で
終わる(古い版で気づけないものは、新しい見張りが5秒待ってから止める)。担当でないセッションの watch は
exit 6 で起動しない(担当を外されたセッションが見張りを起動し直して取り返すのを防ぐ)。

生存報告(2026-09-30 追加): watch 中は 60 秒ごとに POST /agent/presence を送る。
常駐GUIが止まっても、スマホの端末一覧で「停止」にならないようにするため。
**PUT /agent/attention は使わない。** あれは確認待ちを丸ごと差し替える口で、
空で送ると運用者の確認待ちが全部消える。

置き場所: .claude\skills\message-check\scripts\ (claude-shared で各端末へ配る。relay_client.py と同じ方式)。
Windows PowerShell 5.1 と PowerShell 7 のどちらでも動く。

注意:
- APIキーは表示しない・ログに書かない。設定の探し方は relay_client.py と同じ
  (環境変数 RELAY_ENV_FILE → .claude\relay_local\.env。各項目は同名の環境変数が優先)
- 状態とログは %LOCALAPPDATA%\RelayWatch\ (端末ローカル。git に入れない)
- summary の pending_count は「着手してよい件数」で、予約(lease)・human_only・
  相槌ループはサーバー側で既に除かれている
#>
[CmdletBinding()]
param(
    [ValidateSet('watch', 'status', 'init', 'ack', 'beat', 'arm', 'disarm')]
    [string]$Mode = 'watch',
    [int]$IntervalSec = 30,
    [int]$MaxMinutes = 720,
    [int]$AckId = 0,
    # 省略時は relay_client.py と同じ順で探す: 環境変数 RELAY_ENV_FILE → <.claude>\relay_local\.env
    [string]$EnvPath = '',
    # ack の歯止めを外す。運用者が「処理しない」と決めた件・処理役が APPROVAL_NEEDED で止めた件だけに使う
    [switch]$Force
)

$ErrorActionPreference = 'Stop'
# 出力を UTF-8 で出す。既定のままだと Git Bash や CP932 のコンソール経由で
# 日本語が文字化けし、**起こされた側がこの報告を読めない**(2026-09-30 実測)
try {
    [Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
    $OutputEncoding = [System.Text.UTF8Encoding]::new($false)
} catch {
    # 端末が変更を拒む場合もあるので、失敗しても見張り自体は続ける
}
$StateDir = Join-Path $env:LOCALAPPDATA 'RelayWatch'
$StatePath = Join-Path $StateDir 'state.json'
# 二重起動防止(2026-09-30)。待ち受けセッションを2つ開くと、同じ着信で2回起こしてしまう
# (claim で二重処理は防げるが、トークンは2回分かかる)
$LockPath = Join-Path $StateDir 'watch.lock'
# 危険操作の歯止めを効かせるセッション(relay_watch_guard.py が読む)
$GuardPath = Join-Path $StateDir 'guard_session.json'

function Write-WatchLog {
    param([string]$Message)
    if (-not (Test-Path -LiteralPath $StateDir)) {
        New-Item -ItemType Directory -Force -Path $StateDir | Out-Null
    }
    $logPath = Join-Path $StateDir ('watch_' + (Get-Date -Format 'yyyyMMdd') + '.log')
    $line = '[' + (Get-Date -Format 'yyyy-MM-ddTHH:mm:ss') + '] ' + $Message
    Add-Content -LiteralPath $logPath -Value $line -Encoding UTF8
}

function Read-RelayEnv {
    param([string]$Path)
    if (-not (Test-Path -LiteralPath $Path)) {
        throw "relay の設定が見つかりません: $Path"
    }
    $values = @{}
    foreach ($line in Get-Content -LiteralPath $Path -Encoding UTF8) {
        if ($line -match '^\s*#') { continue }
        $i = $line.IndexOf('=')
        if ($i -lt 1) { continue }
        $values[$line.Substring(0, $i).Trim()] = $line.Substring($i + 1).Trim()
    }
    foreach ($key in 'RELAY_BASE_URL', 'RELAY_API_KEY', 'RELAY_SELF_USER_ID') {
        # relay_client.py と同じく**環境変数が .env より優先**
        $fromEnv = [Environment]::GetEnvironmentVariable($key)
        if ($fromEnv) { $values[$key] = $fromEnv.Trim() }
        if (-not $values.ContainsKey($key) -or -not $values[$key]) {
            throw "$key が設定にありません ($Path)"
        }
    }
    return $values
}

function Resolve-RelayEnvPath {
    param([string]$Given)
    if ($Given) { return $Given }
    if ($env:RELAY_ENV_FILE) { return $env:RELAY_ENV_FILE }
    # このスクリプトは <.claude>\skills\message-check\scripts\ にある。
    # relay_client.py(<.claude>\skills\relay\scripts\)と同じ <.claude>\relay_local\.env を読む
    $claudeDir = Split-Path -Parent (Split-Path -Parent (Split-Path -Parent $PSScriptRoot))
    return Join-Path $claudeDir 'relay_local\.env'
}

function Get-State {
    if (Test-Path -LiteralPath $StatePath) {
        try { return Get-Content -LiteralPath $StatePath -Raw -Encoding UTF8 | ConvertFrom-Json }
        catch { Write-WatchLog "状態ファイルが読めないので作り直します: $($_.Exception.Message)" }
    }
    return [pscustomobject]@{ handled_max_id = 0; updated_at = ''; note = 'initialized' }
}

function Set-State {
    param([int]$HandledMaxId, [string]$Note)
    if (-not (Test-Path -LiteralPath $StateDir)) {
        New-Item -ItemType Directory -Force -Path $StateDir | Out-Null
    }
    $obj = [pscustomobject]@{
        handled_max_id = $HandledMaxId
        updated_at     = (Get-Date -Format 'yyyy-MM-ddTHH:mm:ss')
        note           = $Note
    }
    $obj | ConvertTo-Json | Set-Content -LiteralPath $StatePath -Encoding UTF8
    return $obj
}

function Get-LiveWatcher {
    # 生きている見張りの PID を返す。いなければ $null。
    # 強制終了などで消えなかったロック(残骸)は「いない」と判定する。
    # PID は使い回されるので、コマンドラインにこのスクリプトが入っているかまで見る
    if (-not (Test-Path -LiteralPath $LockPath)) { return $null }
    $raw = Get-Content -LiteralPath $LockPath -Raw -ErrorAction SilentlyContinue
    $lockPid = 0
    if (-not [int]::TryParse((($raw + '') -split '\s+')[0], [ref]$lockPid)) { return $null }
    if ($lockPid -eq $PID) { return $null }
    $proc = Get-CimInstance Win32_Process -Filter "ProcessId=$lockPid" -ErrorAction SilentlyContinue
    if ($proc -and $proc.CommandLine -match 'relay_watch\.ps1' -and
        $proc.CommandLine -notmatch '-Mode\s+(status|init|ack|beat|arm|disarm)') {
        return $lockPid
    }
    return $null
}

function Set-Guard {
    # このセッションを待ち受けとして登録する。セッションIDが取れなければ $null
    $sid = ($env:CLAUDE_CODE_SESSION_ID + '').Trim()
    if (-not $sid) { return $null }
    if (-not (Test-Path -LiteralPath $StateDir)) {
        New-Item -ItemType Directory -Force -Path $StateDir | Out-Null
    }
    [pscustomobject]@{
        session_id    = $sid
        registered_at = (Get-Date -Format 'yyyy-MM-ddTHH:mm:ss')
    } | ConvertTo-Json | Set-Content -LiteralPath $GuardPath -Encoding UTF8
    return $sid
}

function Get-GuardSession {
    if (-not (Test-Path -LiteralPath $GuardPath)) { return $null }
    try { return (Get-Content -LiteralPath $GuardPath -Raw -Encoding UTF8 | ConvertFrom-Json).session_id }
    catch { return $null }
}

$script:GuardStamp = -1
$script:GuardOwner = $null
function Get-GuardOwnerCached {
    # 見張りのループから毎秒呼ぶので、ファイルが変わったときだけ読み直す
    # 戻り値: 担当のセッションID / $null(登録が外された) / '?'(書き込み途中などで読めない)
    $item = Get-Item -LiteralPath $GuardPath -ErrorAction SilentlyContinue
    if (-not $item) {
        $script:GuardStamp = 0
        $script:GuardOwner = $null
        return $null
    }
    $stamp = $item.LastWriteTimeUtc.Ticks
    if ($stamp -ne $script:GuardStamp) {
        $read = Get-GuardSession
        # 読めなかった結果は覚えない(Set-Content の書き込み途中を「登録が外れた」と取り違えない)
        if (-not $read) { return '?' }
        $script:GuardStamp = $stamp
        $script:GuardOwner = $read
    }
    return $script:GuardOwner
}

function Exit-Watch {
    # watch の終了は必ずここを通す。自分が持っているロックだけ外す
    param([int]$Code)
    try {
        $raw = Get-Content -LiteralPath $LockPath -Raw -ErrorAction SilentlyContinue
        if ($raw -and (($raw -split '\s+')[0] -eq "$PID")) {
            Remove-Item -LiteralPath $LockPath -Force -ErrorAction SilentlyContinue
        }
    } catch {
        # ロックが外せなくても次の起動が残骸として扱うので、ここでは止めない
    }
    exit $Code
}

# --- API ---------------------------------------------------------------

$EnvPath = Resolve-RelayEnvPath -Given $EnvPath
$conf = Read-RelayEnv -Path $EnvPath
$headers = @{ 'X-API-Key' = $conf['RELAY_API_KEY'] }
$me = $conf['RELAY_SELF_USER_ID']
$base = $conf['RELAY_BASE_URL'].TrimEnd('/')

function Get-Summary {
    $url = "$base/messages/summary?to=$me"
    return Invoke-RestMethod -Uri $url -Headers $headers -TimeoutSec 20
}

function Send-Presence {
    # 生存報告。失敗しても見張りは止めない(返り値で成否だけ返す)
    param([Nullable[int]]$Pending)
    $body = @{ holder = "watch:$me"; pid = $PID }
    if ($null -ne $Pending) { $body['pending_count'] = [int]$Pending }
    $json = $body | ConvertTo-Json -Compress
    try {
        Invoke-RestMethod -Uri "$base/agent/presence" -Method Post -Headers $headers `
            -ContentType 'application/json; charset=utf-8' `
            -Body ([System.Text.Encoding]::UTF8.GetBytes($json)) -TimeoutSec 20 | Out-Null
        return $true
    } catch {
        # ここでは記録しない。呼び出し側が回数を見て間引いて記録する
        # (毎回書くと、サーバーが報告だけ受け付けない状態で30秒ごとに1行増える)
        $script:LastPresenceError = $_.Exception.Message
        return $false
    }
}

function Get-PendingMeta {
    # peek=true は status を動かさない(既定は既読化の副作用がある)
    $url = "$base/messages?to=$me&unread=true&peek=true"
    try {
        $rows = Invoke-RestMethod -Uri $url -Headers $headers -TimeoutSec 30
    } catch {
        Write-WatchLog "本文一覧の取得に失敗: $($_.Exception.Message)"
        return @()
    }
    return @($rows)
}

function Format-PendingLine {
    param($Row)
    $flags = @()
    if ($Row.human_only) { $flags += 'human_only' }
    if ($Row.no_reply_needed) { $flags += 'no_reply_needed' }
    $flagText = if ($flags.Count) { ' [' + ($flags -join ',') + ']' } else { '' }
    $head = ''
    if ($Row.content) {
        $head = ($Row.content -replace '\s+', ' ')
        if ($head.Length -gt 60) { $head = $head.Substring(0, 60) + '…' }
    }
    return ('#{0} {1}→{2} type={3}{4} {5}' -f `
        $Row.id, $Row.from_user, $Row.to_user, $Row.type, $flagText, $head)
}

# --- モード ------------------------------------------------------------

switch ($Mode) {
    'status' {
        $s = Get-State
        $sum = Get-Summary
        "名義: $me / 接続先: $base"
        $live = Get-LiveWatcher
        if ($live) { "見張り: 動作中 (PID $live)" } else { "見張り: 停止中" }
        $guard = Get-GuardSession
        if ($guard) {
            $short = $guard.Substring(0, [Math]::Min(8, $guard.Length))
            $mine = if ($guard -eq ($env:CLAUDE_CODE_SESSION_ID + '').Trim()) { '・このセッション' } else { '' }
            "待ち受けの担当(歯止めの対象): セッション $short…$mine"
        } else { "待ち受けの担当(歯止めの対象): 未登録" }
        "処理済みとして記録している最大ID: $($s.handled_max_id) (更新 $($s.updated_at) / $($s.note))"
        ("着手してよい件数: {0} / 最大ID: {1} / 最終着信: {2}" -f `
            $sum.pending_count, $sum.max_pending_id, $sum.latest_created_at)
        ("除かれている件数: 人が対応 {0} / 予約 {1} / 引き継ぎ {2} / 相槌ループ {3} (合計未処理 {4})" -f `
            $sum.human_only_count, $sum.lease_reserved_count, $sum.handoff_count,
            $sum.agent_loop_count, $sum.total_pending_count)
        foreach ($row in Get-PendingMeta) { '  ' + (Format-PendingLine -Row $row) }
        exit 0
    }
    'init' {
        $sum = Get-Summary
        $max = [int]($sum.max_pending_id | ForEach-Object { if ($_) { $_ } else { 0 } })
        $s = Set-State -HandledMaxId $max -Note 'init'
        Write-WatchLog "init: handled_max_id=$max (着手してよい件数 $($sum.pending_count))"
        "処理済みの基準を #$max に置きました。これ以下の着信では起こしません。"
        exit 0
    }
    'ack' {
        $max = $AckId
        if ($max -le 0) {
            $sum = Get-Summary
            $max = [int]($sum.max_pending_id | ForEach-Object { if ($_) { $_ } else { 0 } })
        }
        $prev = (Get-State).handled_max_id
        if ($max -lt $prev) { $max = $prev }  # 巻き戻さない
        # 未処理のまま ack させない(2026-10-01)。ack 済みの ID では見張りは二度と起こさないので、
        # 待ち受けが処理役を呼ばずに ack すると、その着信は誰にも気づかれずに埋もれる
        # (A の待ち受けが「情報通知なので処理不要」と自己判断して7件を埋もれさせた)。
        # 「誰も手を付けていない」= 未読のまま・処理権なし。人が対応する件(human_only)は数えない。
        # 別のセッションが予約中のスレッドは一覧に出ないので、これも数えない
        if (-not $Force -and $max -gt $prev) {
            $untouched = @(Get-PendingMeta | Where-Object {
                [int]$_.id -gt $prev -and [int]$_.id -le $max -and -not $_.human_only -and
                $_.status -eq 'unread' -and -not $_.claimed_by
            })
            if ($untouched.Count -gt 0) {
                $ids = ($untouched | ForEach-Object { "#$($_.id)" }) -join ' '
                Write-WatchLog "ack を断った(誰も手を付けていない: $ids)"
                "まだ誰も手を付けていない着信があるため、ack しません:"
                foreach ($row in $untouched) { '  ' + (Format-PendingLine -Row $row) }
                "処理役に渡してください(/m watch の W3)。種類が result や report でも、処理役が読んで done にします。"
                "処理役が APPROVAL_NEEDED で止めた件・運用者が「処理しない」と決めた件だけは、-Force を付けて ack できます。"
                exit 7
            }
        }
        Set-State -HandledMaxId $max -Note $(if ($Force) { 'ack -Force' } else { 'ack' }) | Out-Null
        Write-WatchLog "ack: handled_max_id=$max"
        "#$max まで処理済みとして記録しました。"
        exit 0
    }
    'beat' {
        if (Send-Presence -Pending $null) {
            "生存報告を送りました(watch:$me)。"
            exit 0
        }
        Write-WatchLog "beat: 生存報告に失敗: $script:LastPresenceError"
        "生存報告を送れませんでした: $script:LastPresenceError"
        exit 3
    }
    'arm' {
        $sid = Set-Guard
        if (-not $sid) {
            "このセッションのIDが取れないため、危険操作の歯止めを登録できません(CLAUDE_CODE_SESSION_ID が空)。"
            exit 5
        }
        Write-WatchLog "arm: 歯止めをセッション $($sid.Substring(0, [Math]::Min(8, $sid.Length)))… に登録"
        "このセッションを待ち受けとして登録しました。危険操作の歯止めはこのセッションと処理役に効きます。"
        exit 0
    }
    'disarm' {
        if (Test-Path -LiteralPath $GuardPath) { Remove-Item -LiteralPath $GuardPath -Force }
        Write-WatchLog "disarm: 歯止めの登録を外した"
        "危険操作の歯止めの登録を外しました。"
        exit 0
    }
}

# --- watch -------------------------------------------------------------

# --- 担当の確認と引き継ぎ(2026-09-30 運用者決定: 後から起動したセッションが勝つ) ---
# /m watch は先に -Mode arm で「待ち受けの担当」を自分へ書き換えてから watch を起動する。
# だから、担当が別のセッションなのに watch が呼ばれるのは「担当を外された側」だけで、
# そこで起動すると取り返し合いになる(A で前のセッションが見張りを起動し直し、
# 担当を取り返した実例がある)。担当の見張りが既に動いていれば、それを引き継ぐ。
$mySid = ($env:CLAUDE_CODE_SESSION_ID + '').Trim()
$owner = Get-GuardSession
if ($mySid -and $owner -and $owner -ne $mySid) {
    $short = $owner.Substring(0, [Math]::Min(8, $owner.Length))
    Write-WatchLog "watch 起動を見送り: 担当は別のセッション($short…)"
    "待ち受けは別のセッション($short…)が担当しています。このセッションでは見張りを起動しません。"
    "このセッションで引き継ぐときは、/m watch の手順どおり先に -Mode arm を実行してください。"
    exit 6
}
if ($mySid -and -not $owner) {
    Set-Guard | Out-Null
}
if (-not $mySid) {
    Write-WatchLog "歯止めを登録できません(CLAUDE_CODE_SESSION_ID が空)"
    "注意: このセッションのIDが取れないため、危険操作の歯止めと引き継ぎが効きません。"
}

$other = Get-LiveWatcher
$tookOver = $null
if ($other) {
    if (-not $mySid) {
        # 担当を確かめられないので、従来どおり二重には起動しない
        Write-WatchLog "watch 起動を見送り: 既に見張りが動いています(PID $other)"
        "既に見張りが動いています(PID $other)。二重には起動しません。"
        exit 4
    }
    # 担当はこのセッション。前の見張りが残っているので引き継ぐ。
    # 新しい版の見張りは担当が変わったことに1秒以内に気づいて自分で終了する(exit 6)
    Write-WatchLog "引き継ぎ: 前の見張り(PID $other)の終了を待つ"
    $waitUntil = (Get-Date).AddSeconds(5)
    while ((Get-Date) -lt $waitUntil -and (Get-LiveWatcher)) {
        Start-Sleep -Milliseconds 500
    }
    $still = Get-LiveWatcher
    if ($still) {
        # 古い版(担当の変化に気づけない)と、同じセッションで二重に起動した場合はここに来る。
        # 止めるのは Get-LiveWatcher がコマンドラインまで確かめた見張りだけ
        try {
            Stop-Process -Id $still -Force -ErrorAction Stop
        } catch {
            Write-WatchLog "引き継ぎ: 前の見張り(PID $still)を止められない: $($_.Exception.Message)"
        }
        Start-Sleep -Milliseconds 500
        if (Get-LiveWatcher) {
            "前の見張り(PID $still)を止められなかったため、起動しません。"
            exit 4
        }
        Write-WatchLog "引き継ぎ: 前の見張り(PID $still)を停止した"
    }
    $tookOver = $other
}
if (-not (Test-Path -LiteralPath $StateDir)) {
    New-Item -ItemType Directory -Force -Path $StateDir | Out-Null
}
Set-Content -LiteralPath $LockPath -Value ("$PID " + (Get-Date -Format 's')) -Encoding ASCII
# 同時に2つ起動した場合に備えて、書いたあと読み直して自分のものか確かめる
Start-Sleep -Milliseconds 300
$mine = Get-Content -LiteralPath $LockPath -Raw -ErrorAction SilentlyContinue
if (-not $mine -or (($mine -split '\s+')[0] -ne "$PID")) {
    "同時に別の見張りが起動したため、こちらは終了します。"
    exit 4
}

$state = Get-State
$handled = [int]$state.handled_max_id
$deadline = (Get-Date).AddMinutes($MaxMinutes)
$idleLogAt = Get-Date
$polls = 0
$failures = 0
$BeatEverySec = 60
$lastBeat = [datetime]::MinValue
$beatFailures = 0

Write-WatchLog "watch 開始: handled_max_id=$handled / 間隔 ${IntervalSec}秒 / 上限 ${MaxMinutes}分"
if ($tookOver) {
    "前の見張り(PID $tookOver)から引き継ぎました。"
}
"relay を見張ります(処理済み #$handled まで / ${IntervalSec}秒ごと)。新着があれば終了します。"
# 起動直後に1回送る(GUIを止めてから最初の確認までの間も「停止」に見せない)
if (-not (Send-Presence -Pending $null)) {
    $beatFailures = 1
    Write-WatchLog "生存報告に失敗(起動時): $script:LastPresenceError"
}
$lastBeat = Get-Date

while ((Get-Date) -lt $deadline) {
    # 1秒ずつ眠り、そのたびに担当が変わっていないかを見る(引き継ぎを待たせない)
    for ($tick = 0; $tick -lt $IntervalSec; $tick++) {
        Start-Sleep -Seconds 1
        if (-not $mySid) { continue }
        $nowOwner = Get-GuardOwnerCached
        if ($nowOwner -eq '?') { continue }
        if ($nowOwner -ne $mySid) {
            if ($nowOwner) {
                $short = $nowOwner.Substring(0, [Math]::Min(8, $nowOwner.Length))
                Write-WatchLog "watch 終了: 担当が別のセッション($short…)へ移った"
                "待ち受けは別のセッション($short…)に引き継がれました。このセッションでは見張りを再開しないでください(再開しても起動しません)。"
            } else {
                Write-WatchLog "watch 終了: 待ち受けの登録が外された"
                "待ち受けの登録が外されたため、見張りを終了します。このセッションでは見張りを再開しないでください。"
            }
            Exit-Watch -Code 6
        }
    }
    $polls++
    try {
        $sum = Get-Summary
        $failures = 0
    } catch {
        $failures++
        # 一時的な失敗で騒がない。連続5回(既定2分半)続いたら記録して終了し、運用者に気づかせる
        if ($failures -ge 5) {
            Write-WatchLog "summary の取得が連続 $failures 回失敗: $($_.Exception.Message)"
            "relay サーバーに $failures 回続けて届きませんでした: $($_.Exception.Message)"
            Exit-Watch -Code 3
        }
        continue
    }

    $max = 0
    if ($sum.max_pending_id) { $max = [int]$sum.max_pending_id }
    $count = [int]$sum.pending_count

    if (((Get-Date) - $lastBeat).TotalSeconds -ge $BeatEverySec) {
        # 成否にかかわらず次は60秒後。失敗のたびに次の確認で再送すると、
        # 報告だけが通らない状態で間隔が詰まり、ログも膨らむ
        $lastBeat = Get-Date
        if (Send-Presence -Pending $count) {
            if ($beatFailures -gt 0) {
                Write-WatchLog "生存報告が回復しました(それまで連続 $beatFailures 回失敗)"
            }
            $beatFailures = 0
        } else {
            # 生存報告だけが続けて失敗するなら、端末一覧で「停止」に見えている。
            # 見張り自体は続ける(新着の検知の方が大事)。記録は初回と10回ごと
            $beatFailures++
            if ($beatFailures -eq 1 -or $beatFailures % 10 -eq 0) {
                Write-WatchLog ("生存報告に失敗(連続 {0} 回・端末一覧で停止に見えている可能性): {1}" -f `
                    $beatFailures, $script:LastPresenceError)
            }
        }
    }

    if ($count -gt 0 -and $max -gt $handled) {
        # 起こす直前にも送る。処理中はこのスクリプトが止まるので、ここが最後の報告になる
        Send-Presence -Pending $count | Out-Null
        Write-WatchLog "新着: pending=$count max_id=$max (handled=$handled) -> 終了して起こす"
        ''
        "=== relay に着手してよい新着が $count 件あります(最大ID #$max・処理済み #$handled まで) ==="
        foreach ($row in Get-PendingMeta) {
            $line = Format-PendingLine -Row $row
            Write-WatchLog "  $line"
            '  ' + $line
        }
        ''
        # ack には**この時点の最大ID**を渡す。ID を付けずに ack すると、処理中に届いた
        # 新着まで処理済みとして記録され、次の新着が来るまで放置される
        # いま動いている PowerShell の種類で案内する(5.1 の端末に pwsh と書くと、そのまま実行できない)
        $hostExe = if ($PSVersionTable.PSEdition -eq 'Core') { 'pwsh' } else { 'powershell' }
        "処理したら次を実行してから、また見張りを起動してください(ID は必ず付ける):"
        "  $hostExe -NoProfile -ExecutionPolicy Bypass -File `"$PSCommandPath`" -Mode ack -AckId $max"
        "処理が3分を超えそうなら、途中で生存報告を送ってください(端末一覧で停止に見えないように):"
        "  $hostExe -NoProfile -ExecutionPolicy Bypass -File `"$PSCommandPath`" -Mode beat"
        Exit-Watch -Code 0
    }

    # 人が対応する着信(human_only)でも起こす(2026-10-08)。summary の pending_count には
    # 数えられないので、これが無いと寺下さん宛ての質問が来ても見張りは黙ったままになる
    # (#3100 を見落とした)。処理済みID(handled)より新しい human_only だけを対象にし、
    # 通知後の ack で handled が進めば二度と起こさない(ack は human_only を未処理扱いしない)
    if ([int]$sum.human_only_count -gt 0) {
        $humanRows = @(Get-PendingMeta | Where-Object { $_.human_only -and [int]$_.id -gt $handled })
        if ($humanRows.Count -gt 0) {
            $hmax = ($humanRows | ForEach-Object { [int]$_.id } | Measure-Object -Maximum).Maximum
            Send-Presence -Pending $humanRows.Count | Out-Null
            Write-WatchLog "人が対応する着信: $($humanRows.Count) 件 max_id=$hmax (handled=$handled) -> 終了して起こす"
            ''
            "=== 運用者本人が答える着信(human_only)が $($humanRows.Count) 件あります(最大ID #$hmax・処理済み #$handled まで) ==="
            foreach ($row in $humanRows) {
                $line = Format-PendingLine -Row $row
                Write-WatchLog "  $line"
                '  ' + $line
            }
            ''
            "AI は答えません。処理役に全文を読み取りで取らせ(claim・返信・done はしない)、運用者に QUESTION で伝えてください。"
            $hostExe = if ($PSVersionTable.PSEdition -eq 'Core') { 'pwsh' } else { 'powershell' }
            "運用者へ伝えたら次を実行してから、また見張りを起動してください(ID は必ず付ける):"
            "  $hostExe -NoProfile -ExecutionPolicy Bypass -File `"$PSCommandPath`" -Mode ack -AckId $hmax"
            Exit-Watch -Code 0
        }
    }

    # 動いていることが分かるように10分ごとだけ記録する(30秒ごとに書くと読めなくなる)
    if (((Get-Date) - $idleLogAt).TotalMinutes -ge 10) {
        Write-WatchLog ("idle: pending={0} max_id={1} handled={2} (除外: 人{3}/予約{4}/引継{5}/相槌{6})" -f `
            $count, $max, $handled, $sum.human_only_count, $sum.lease_reserved_count,
            $sum.handoff_count, $sum.agent_loop_count)
        $idleLogAt = Get-Date
    }
}

Write-WatchLog "watch 終了: 上限 ${MaxMinutes}分に達しました(${polls}回確認・新着なし)"
"変化なし: ${MaxMinutes}分見張って新着はありませんでした(${polls}回確認)。"
Exit-Watch -Code 0
