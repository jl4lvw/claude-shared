' run_hidden.vbs - generic launcher that runs any command with NO console window (2026-10-12)
' ASCII only: wscript reads .vbs as ANSI, so non-ASCII comments would be garbled.
'
' Why: a scheduled task (interactive logon) that starts powershell.exe / cmd.exe / .cmd
'      directly opens a Windows Terminal window in the FOREGROUND and steals the text
'      cursor from whatever the user is typing in. Running it through wscript with
'      WshShell.Run(cmd, 0, True) shows no window at all and still returns the exit code.
'
' Task action:
'   Program:   wscript.exe
'   Arguments: "C:\ClaudeCode\.claude\tools\run_hidden.vbs" powershell.exe -NoProfile -ExecutionPolicy Bypass -File "C:\path\script.ps1"
'
' Note: wscript splits the arguments on spaces and strips the outer quotes, so this script
'       re-quotes only the arguments that contain spaces. Commands with nested quotes or
'       redirections (>>) cannot be passed this way; write a dedicated .vbs instead
'       (example: 047.GoQ*/tools/run_worker_hidden.vbs for SevensNotifyWorker).
'       Original task definitions are backed up as *_before_hidden_*.xml in folder 040.
Option Explicit
Dim sh, args, i, a, cmd
Set sh = CreateObject("WScript.Shell")
Set args = WScript.Arguments
If args.Count = 0 Then
    WScript.Echo "usage: wscript.exe run_hidden.vbs <exe> [args...]"
    WScript.Quit 2
End If
cmd = ""
For i = 0 To args.Count - 1
    a = args(i)
    If InStr(a, " ") > 0 Or Len(a) = 0 Then a = """" & a & """"
    If i > 0 Then cmd = cmd & " "
    cmd = cmd & a
Next
' 0 = SW_HIDE, True = wait for exit and hand the child's exit code back to Task Scheduler
WScript.Quit sh.Run(cmd, 0, True)
