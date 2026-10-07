<#
.SYNOPSIS
  「Claude 以外のアプリが操作できない」等の入力不具合を切り分ける読み取り専用の診断。
  入力は一切変更しない(フックは CallNextHookEx で素通し)。
.DESCRIPTION
  -ScanOnly : OS が「押しっぱなし」と認識しているキー/ボタンを一覧して終了(1秒)。
              XButton1(戻る)や 0xF4(半角/全角)が出たら注入元の「離す」信号の取りこぼし。
  既定      : -Seconds 秒間、低レベルフック+Raw Input で全マウス/キーを記録する。
              「物理」か「注入(SendInput: MWB/RustDesk/AHK 等)」かを区別し、
              前面ウィンドウの変化と OS の押下状態の変化も記録する。-Out でファイルへ逐次保存。
.EXAMPLE
  pwsh -File C:\ClaudeCode\.claude\tools\input_probe.ps1 -ScanOnly
  pwsh -File C:\ClaudeCode\.claude\tools\input_probe.ps1 -Seconds 120 -Out $env:TEMP\probe.log
.NOTES
  2026-10-07 作成(A 端末で XButton1 の押しっぱなし→Claude 以外が操作不能になった件)。
  記憶: project_a_terminal_non_claude_apps_inoperable_stuck_xbutton
  観測中はキー入力内容(vk コード)が記録に残る。パスワード入力中は実行しないこと。
#>
param([int]$Seconds = 40,[string]$Out = "",[switch]$ScanOnly)
if ($ScanOnly) {
  Add-Type -Name KS -Namespace ProbeScan -MemberDefinition '[DllImport("user32.dll")] public static extern short GetAsyncKeyState(int v);'
  $names = @{1='LButton';2='RButton';4='MButton';5='XButton1(戻る)';6='XButton2(進む)';16='Shift';17='Ctrl';18='Alt';91='LWin';92='RWin';160='LShift';161='RShift';162='LCtrl';163='RCtrl';164='LAlt';165='RAlt';243='VK_OEM_AUTO';244='VK_OEM_ENLW(半角/全角)'}
  $down = @()
  foreach ($v in 1..254) {
    if (([ProbeScan.KS]::GetAsyncKeyState($v) -band 0x8000) -ne 0) {
      $down += $(if ($names.ContainsKey($v)) { "$($names[$v])[0x{0:X2}]" -f $v } else { "VK 0x{0:X2}" -f $v })
    }
  }
  if ($down.Count) { "押しっぱなし(OS認識): " + ($down -join ', ') } else { "押しっぱなし: なし" }
  return
}
Add-Type -TypeDefinition @"
using System;using System.Collections.Generic;using System.Runtime.InteropServices;using System.Text;using System.Diagnostics;
public static class Probe{
 const int WH_KEYBOARD_LL=13, WH_MOUSE_LL=14;
 delegate IntPtr HookProc(int n,IntPtr w,IntPtr l);
 [StructLayout(LayoutKind.Sequential)] struct MSLL{public int x,y;public uint data,flags,time;public IntPtr extra;}
 [StructLayout(LayoutKind.Sequential)] struct KBLL{public uint vk,scan,flags,time;public IntPtr extra;}
 [StructLayout(LayoutKind.Sequential)] struct MSG{public IntPtr hwnd;public uint message;public IntPtr wParam,lParam;public uint time;public int x,y;}
 [StructLayout(LayoutKind.Sequential)] struct RID{public ushort usagePage,usage;public uint flags;public IntPtr target;}
 [StructLayout(LayoutKind.Sequential)] struct RIH{public uint type,size;public IntPtr dev,wParam;}
 [DllImport("user32.dll")] static extern IntPtr SetWindowsHookEx(int id,HookProc p,IntPtr m,uint t);
 [DllImport("user32.dll")] static extern bool UnhookWindowsHookEx(IntPtr h);
 [DllImport("user32.dll")] static extern IntPtr CallNextHookEx(IntPtr h,int n,IntPtr w,IntPtr l);
 [DllImport("kernel32.dll")] static extern IntPtr GetModuleHandle(string n);
 [DllImport("user32.dll")] static extern bool PeekMessage(out MSG m,IntPtr h,uint a,uint b,uint r);
 [DllImport("user32.dll",CharSet=CharSet.Unicode)] static extern IntPtr CreateWindowEx(int ex,string cls,string name,int st,int x,int y,int w,int h,IntPtr parent,IntPtr menu,IntPtr inst,IntPtr p);
 [DllImport("user32.dll")] static extern void mouse_event(uint f,int dx,int dy,uint d,IntPtr e);
 [DllImport("user32.dll")] static extern IntPtr GetForegroundWindow();
 [DllImport("user32.dll")] static extern uint GetWindowThreadProcessId(IntPtr h,out uint p);
 [DllImport("user32.dll",CharSet=CharSet.Unicode)] static extern int GetWindowText(IntPtr h,StringBuilder s,int n);
 [DllImport("user32.dll",CharSet=CharSet.Unicode)] static extern int GetClassName(IntPtr h,StringBuilder s,int n);
 [DllImport("user32.dll")] static extern short GetAsyncKeyState(int v);
 static IntPtr lastFg=IntPtr.Zero; static long lastPoll=0; static string lastDown="";
 static void Poll(){ if(sw.ElapsedMilliseconds-lastPoll<250) return; lastPoll=sw.ElapsedMilliseconds; var h=GetForegroundWindow(); if(h!=lastFg){ lastFg=h; uint pid; GetWindowThreadProcessId(h,out pid); var t=new StringBuilder(80); var c=new StringBuilder(60); GetWindowText(h,t,80); GetClassName(h,c,60); string pn=""; try{ pn=Process.GetProcessById((int)pid).ProcessName; }catch{} Add("[前面変更] "+pn+"("+pid+") "+c+" '"+t+"'"); } string d=""; foreach(int v in new int[]{0x01,0x02,0x04,0x05,0x06,0x10,0x11,0x12,0x5B,0x5C,0xA0,0xA1,0xA2,0xA3,0xA4,0xA5,0xF3,0xF4}){ if((GetAsyncKeyState(v)&0x8000)!=0) d+="0x"+v.ToString("X2")+","; } if(d!=lastDown){ lastDown=d; Add("[OS押下状態] "+(d==""?"(なし)":d)); } }
 [DllImport("user32.dll")] static extern bool RegisterRawInputDevices(RID[] d,uint n,uint sz);
 [DllImport("user32.dll")] static extern uint GetRawInputData(IntPtr h,uint cmd,byte[] buf,ref uint sz,uint hdr);
 [DllImport("user32.dll",CharSet=CharSet.Unicode)] static extern uint GetRawInputDeviceInfo(IntPtr dev,uint cmd,StringBuilder d,ref uint sz);
 static HookProc mh,kh; static IntPtr mhh,khh; static Stopwatch sw=new Stopwatch();
 public static List<string> Log=new List<string>(); public static int Moves=0,InjMoves=0;
 static Dictionary<IntPtr,string> devn=new Dictionary<IntPtr,string>();
 static string Dev(IntPtr d){ string s; if(devn.TryGetValue(d,out s)) return s; var sb=new StringBuilder(300); uint n=300; GetRawInputDeviceInfo(d,0x20000007,sb,ref n); s=sb.ToString(); int i=s.IndexOf("VID_"); s=i>=0? s.Substring(i,Math.Min(17,s.Length-i)):s; devn[d]=s; return s; }
 public static string OutPath=null;
 static void Add(string s){ string line=string.Format("{0:HH:mm:ss.fff} {1,7}ms {2}",DateTime.Now,sw.ElapsedMilliseconds,s); lock(Log){ Log.Add(line); if(OutPath!=null){ try{ System.IO.File.AppendAllText(OutPath,line+Environment.NewLine); }catch{} } } }
 static IntPtr M(int n,IntPtr w,IntPtr l){
  if(n>=0){ var s=(MSLL)Marshal.PtrToStructure(l,typeof(MSLL)); int m=(int)w; bool inj=(s.flags&1)!=0, low=(s.flags&2)!=0;
   if(m==0x200){ Moves++; if(inj) InjMoves++; }
   else { string nm= m==0x201?"L-DOWN":m==0x202?"L-UP":m==0x204?"R-DOWN":m==0x205?"R-UP":m==0x207?"M-DOWN":m==0x208?"M-UP":m==0x20B?"X"+(s.data>>16)+"-DOWN":m==0x20C?"X"+(s.data>>16)+"-UP":m==0x20A?"WHEEL":"msg0x"+m.ToString("X");
     Add(string.Format("[LLマウス] {0} {1}{2} extra=0x{3:X} pos=({4},{5})",nm,inj?"注入(SendInput等)":"物理",low?"/低IL":"",(long)s.extra,s.x,s.y)); } }
  return CallNextHookEx(mhh,n,w,l); }
 static IntPtr K(int n,IntPtr w,IntPtr l){
  if(n>=0){ var s=(KBLL)Marshal.PtrToStructure(l,typeof(KBLL)); int m=(int)w; bool inj=(s.flags&0x10)!=0;
   Add(string.Format("[LLキー] vk=0x{0:X2} scan=0x{1:X2} {2} {3} extra=0x{4:X}",s.vk,s.scan,(m==0x100||m==0x104)?"DOWN":"UP",inj?"注入(SendInput等)":"物理",(long)s.extra)); }
  return CallNextHookEx(khh,n,w,l); }
 public static void Run(int sec){
  mh=M; kh=K; var mod=GetModuleHandle(null);
  mhh=SetWindowsHookEx(WH_MOUSE_LL,mh,mod,0); khh=SetWindowsHookEx(WH_KEYBOARD_LL,kh,mod,0);
  Add("フック設置 mouse="+(mhh!=IntPtr.Zero)+" kbd="+(khh!=IntPtr.Zero));
  var hw=CreateWindowEx(0,"STATIC","probe",0,0,0,0,0,new IntPtr(-3),IntPtr.Zero,IntPtr.Zero,IntPtr.Zero);
  var rid=new RID[]{ new RID{usagePage=1,usage=2,flags=0x100,target=hw}, new RID{usagePage=1,usage=6,flags=0x100,target=hw} };
  Add("RawInput登録="+RegisterRawInputDevices(rid,2,(uint)Marshal.SizeOf(typeof(RID)))+" hwnd=0x"+hw.ToString("X"));
  sw.Start(); MSG msg; bool tested=false;
  while(sw.ElapsedMilliseconds<sec*1000){
   while(PeekMessage(out msg,IntPtr.Zero,0,0,1)){
    if(msg.message==0x00FF){ uint sz=0; GetRawInputData(msg.lParam,0x10000003,null,ref sz,(uint)Marshal.SizeOf(typeof(RIH)));
     var buf=new byte[sz]; if(GetRawInputData(msg.lParam,0x10000003,buf,ref sz,(uint)Marshal.SizeOf(typeof(RIH)))==sz){
      uint type=BitConverter.ToUInt32(buf,0); IntPtr dev=new IntPtr(BitConverter.ToInt64(buf,8)); int o=24;
      if(type==0){ ushort bf=BitConverter.ToUInt16(buf,o+4); // usButtonFlags
        if(bf!=0 && (bf&0x0400)==0) Add(string.Format("[RAWマウス] dev={0} buttonFlags=0x{1:X} (Btn4 DOWN=0x40 UP=0x80 / Btn5 DOWN=0x100 UP=0x200)",Dev(dev),bf)); }
      else if(type==1){ ushort vk=BitConverter.ToUInt16(buf,o+6); ushort fl=BitConverter.ToUInt16(buf,o+2); Add(string.Format("[RAWキー] dev={0} vk=0x{1:X2} {2}",Dev(dev),vk,(fl&1)!=0?"UP":"DOWN")); } } }
   }
   if(!tested && sw.ElapsedMilliseconds>3000){ tested=true; Add("自己テスト: 1px移動して戻す"); mouse_event(1,1,0,0,IntPtr.Zero); mouse_event(1,-1,0,0,IntPtr.Zero); }
   Poll(); System.Threading.Thread.Sleep(2); }
  UnhookWindowsHookEx(mhh); UnhookWindowsHookEx(khh); }
}
"@
"観測開始 $(Get-Date -Format HH:mm:ss) ($Seconds 秒)"
[Probe]::OutPath=$(if($Out){$Out}else{$null}); [Probe]::Run($Seconds)
"観測終了 $(Get-Date -Format HH:mm:ss)"
"マウス移動イベント数=$([Probe]::Moves)  うち注入=$([Probe]::InjMoves)"
"--- 記録 (最大150行) ---"
[Probe]::Log | Select-Object -First 150
"--- 終了時点のOS入力状態 ---"
Add-Type -Name KS -Namespace Z -MemberDefinition '[DllImport("user32.dll")] public static extern short GetAsyncKeyState(int v);'
$d=@(); foreach($v in 1..254){ if(([Z.KS]::GetAsyncKeyState($v) -band 0x8000) -ne 0){ $d+="0x{0:X2}" -f $v } }; "押下中VK: " + $(if($d){$d -join ','}else{'(なし)'})
