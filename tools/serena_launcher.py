"""Serena MCPサーバーの透過ラッパー起動スクリプト。

背景:
  Serenaの`local`scope登録(`serena start-mcp-server ...`)は、Claude Codeを
  再起動するたびに新しいプロセス(+ pyright/TypeScript Language Serverの
  子プロセス一式)を起動するが、前セッションの古いプロセスが自動終了せず、
  再起動を繰り返すたびに際限なく積み上がる(2026-08-27に実機で30個以上を確認)。

対策(2026-08-27 初版 -> 同日中に2回再設計):
  「起動直前に、前セッションの孤児プロセスを検査して片付けてから本体を起動する」
  という設計を導入したが、cgd Lv8レビュー(1回目・2回目とも)で欠陥が判明し、
  同日中に2回再設計した。

  [1回目のレビューで判明した欠陥と対策]
  1. 孤児判定が「直接の親PIDの生死」しか見ておらず、ラッパー自身(python)が
     serena.exeの直接の親になる構造のため、claude.exeが死んでもラッパー
     プロセス自体が生き残っていれば孤児と判定できなかった。
     -> 対策: 祖先を辿って判定する方式に変更(後述のとおり後にさらに一般化)。
  2. 掃除対象がserena.exe単体(name='serena.exe')のみで、serena.exeが
     起動するtsserver/pyright/typingsInstaller等の子孫は対象外だった。
     -> 対策: 既知のシグネチャを持つnode.exeも対象に含める。
  3. PIDの存在有無しか見ておらず、WindowsのPID再利用により
     「親は生きている」と誤判定しうる欠陥があった。
     -> 対策: 祖先チェーンの各ホップで「起動時刻が子より前か」を照合する。

  [再設計の実装直後、実機テストで新たに発見した欠陥と対策]
  4. `wmic`のCSV出力を`utf-8`固定でdecodeしていたが、実機では`wmic`の出力が
     UTF-16LEで返ってきており、全プロセスの`Name`/`CreationDate`等が
     文字化けしていた。結果、上記1〜3の対策実装直後の実機テストで
     **全プロセスを誤って孤児判定する**(=生きている7セッション全ての
     serenaを誤ってkillしかねない)という、旧バグより悪い状態になっていた。
     `--diagnose`(dry-run)で実行してから気づけた。
     -> 対策: `wmic`への依存をやめ、`Get-CimInstance`(PowerShell)に置き換えた。

  [2回目のcgd Lv8再レビュー(Step C差分レビュー)で判明した欠陥と対策]
  5. 🔴 node.exeの対象化条件が「コマンドラインに'tsserver'/'pyright'等の
     汎用的な文字列を含むか」だけだったため、**VS Code等の無関係なツールが
     起動したtsserver/pyright**まで対象に含まれてしまい、それらの祖先には
     claude.exeが存在しないため「孤児」と誤判定してkillしうる欠陥があった。
     -> 対策: 汎用的なツール名ではなく、Serena自身の言語サーバー展開先
        (`~/.serena/language_servers/...`)という**Serena固有のパス**を
        シグネチャにした。他ツールの言語サーバーは別の場所にインストール
        されるため、このパスには一致しない。
  6. 🔴 serena.exeの対象化条件が「コマンドラインにPROJECT_PATHの文字列を
     含むか」という単純な部分一致だったため、`C:/ClaudeCode2`のような
     別プロジェクトにも誤って一致しうる欠陥があった。
     -> 対策: `--project`引数の直後の値を抽出して厳密比較する方式に変更。
  7. 祖先チェーンの終端を`claude.exe`という特定の名前に固定していたため、
     起動経路が変わると正常なプロセスも孤児と誤判定しうる、という指摘が
     あった。「チェーンが途切れずに自然終端(親なし)まで生存しているか」に
     一般化してみたが、**これは実機で新たな誤判定を生んだ**:
     Windowsでは、claude.exe自身の祖先(Claude Desktopアプリ本体等)が、
     既に終了した起動元(ショートカットのランチャー等)を親PIDとして
     持っていることが普通にあり、これは正常な状態である(起動元が
     役目を終えて先に終了するのはWindowsのGUIアプリでは一般的な挙動で、
     「クラッシュして子を置き去りにした」こととは全く別の話)。
     「途切れずに自然終端まで」という条件では、この正常なケースを
     「途中で途切れた」と誤判定し、生きているclaude.exeセッション配下の
     serenaまで孤児扱いしてしまった(`--diagnose`で発覚)。
     -> 対策: 名前ベースの判定に戻した。「祖先に`claude.exe`という名前の
        プロセスが見つかった時点で生存確定」とし、そこから**先**(desktop
        アプリ本体やその起動元)まで遡って確認する必要はない。Codexの
        指摘(名前固定は将来の起動経路変更に弱い)は理論上妥当だが、
        実機検証の結果、今回はこちらを優先した(既知の限界として明記する)。

  [7の修正後、最終差分に対する1回限りのCodex再レビュー(Step C)で判明した
   欠陥と対策(Step C2・自動修正1周)]
  8. 🔴 `_SERENA_HOME_SIGNATURE`が`~/.serena`(language_serversを含まない
     親ディレクトリ)を丸ごとシグネチャにしていたため、`~/.serena_backup/...`
     や`~/.serena/cache/...`のような無関係なパスまで前方一致で誤って
     含みうる欠陥があった(6と同じ「部分一致が広すぎる」系統のバグ)。
     -> 対策: シグネチャを`~/.serena/language_servers/`(末尾"/"込み)に
        narrowingし、`language_servers2/...`のような境界違いの誤一致も防いだ。
  9. 🔴 `cleanup_orphans`がループの先頭で取得した1回のsnapshotを使い回すため、
     ループ途中で先行するtaskkillの`/T`(祖先killが子孫を道連れにする)が
     発生したり、PIDが再利用されたりすると、snapshot時点の情報がkill実行時
     には古くなっている可能性があった(integrationバグ)。
     -> 対策: kill直前に対象PIDを単体で再取得し、Name/CreationDateSortableが
        snapshotと一致する場合のみkillする(`_verify_process_unchanged`)。
        既に終了している/情報が変化している場合は安全側(killしない)に倒す。

  [Step Cで指摘されたが今回は見送った項目(🟠・ユーザー判断)]
  - `_extract_flag_value`(旧`_extract_project_arg`)が`cmdline.split()`のため、
    値に空白やダブルクォートを含むパスを正しく扱えない。2026-08-27夜に
    `--project`の固定指定自体を撤去し、判定を`--context`(値に空白を含まない
    固定文字列`claude-code`)に切り替えたため、この限界は現状のコードパスでは
    顕在化しない。

  なお、実機調査(2026-08-27)では「serenaプロセスが多い」という体感の主因は
  孤児蓄積ではなく、**同時に開いているClaude Codeセッションの数に比例して
  serenaインスタンスが増える**(1セッション=1serena+言語サーバー一式、という
  MCPの設計どおりの挙動)ことだった。孤児清掃はこの体感の主因には効かないが、
  「クラッシュしたセッションの残骸が永久に残る」という別の実害には対処になる
  ため、対策自体は維持し、上記の欠陥だけを修正する。

  [2026-08-27夜: サブプロジェクト単位スコープ化への対応]
  さらなる実測で、モノリポ全体(C:/ClaudeCode)を`--project`固定で開くと
  1インスタンスあたり1GB超(pyrightが数千ファイルを型推論込みで常時保持)
  になる一方、1サブプロジェクト(例: 047、89ファイル)に絞ると約200MBまで
  下がることを確認した。そこで本スクリプトの起動引数から`--project`固定を
  撤去し、各セッションが`activate_project`ツールで必要なサブプロジェクトだけを
  動的に持つ方式に変更した(読み取り専用の探索は別途`project_server_launcher.py`
  が立てる共有ProjectServer経由の`query_project`に寄せる。詳細は同スクリプトの
  docstring参照)。
  副作用: 孤児判定の照合条件だった「`--project`の値が一致するか」が使えなく
  なった(起動時に`--project`を渡さなくなったため)。「`--context claude-code`
  で起動されたものか」に緩めて対応した。既知の限界: このマシンで
  C:/ClaudeCode以外にSerenaを使うリポジトリが増えた場合、それらのserena.exeも
  ここに一致してしまう(誤ってkillされうる)。2026-08-27時点でそのような
  リポジトリは存在しないため実害なしと判断した。増えた場合は再度リポジトリ
  固有の識別子を導入すること。

安全設計:
  - 「祖先が途切れずに生存を確認できる」プロセスには触らない
    (Serena固有パスに一致しないnode.exe/python.exeは、そもそも掃除対象の
    候補にすら入らない)
  - serena.exeは`--project`引数の値を厳密比較、node.exeは
    `~/.serena/language_servers/`配下から起動されたものだけを対象にする
    (無関係な他ツールのプロセスを巻き込まない)
  - 判定不能(PowerShell失敗・情報欠落・祖先チェーンが異常に深い等)な場合は
    常に「殺さない」側に倒す

**重要**: このスクリプトの標準出力はSerenaとClaude Code間のJSON-RPC通信路そのもの。
診断メッセージは必ず標準エラー出力(stderr)に書くこと。標準出力を汚すとMCP通信が壊れる。
"""

from __future__ import annotations

import csv
import io
import subprocess
import sys
from pathlib import Path

SERENA_EXE = str(Path.home() / ".local" / "bin" / "serena.exe")

# Serena自身が言語サーバー一式を展開する先(2026-08-27 Lv8 2回目レビュー指摘対応)。
# VS Code等、他ツールの言語サーバーはこのパス配下にはインストールされないため、
# 汎用的なツール名(tsserver/pyright等)で判定するより誤爆リスクが低い。
#
# 末尾に"/"を明示的に含める(2026-08-27 Lv8 Step C再レビュー指摘: 当初`~/.serena`
# (language_serversを含まない親ディレクトリ)を丸ごとシグネチャにしていたため、
# `~/.serena_backup/...`や`~/.serena/cache/...`のような無関係なパスまで
# 前方一致で誤って含んでしまっていた。PROJECT_PATHの部分一致バグ(既に修正済み)と
# 同じ種類の欠陥。`language_servers`の直後に"/"が続くことまで確認することで、
# `~/.serena/language_servers2/...`のような境界違いの誤一致も防ぐ)。
_SERENA_HOME_SIGNATURE = (
    str(Path.home() / ".serena" / "language_servers").lower().replace("\\", "/") + "/"
)

# 祖先チェーンの中にこの名前のプロセスが見つかれば「生存確定」として
# それより先(desktopアプリ本体やその起動元)は遡らない(2026-08-27 既知の限界:
# Claude Codeの起動経路が変わると追従できない。実機検証の結果、より一般化した
# 「自然終端まで途切れないか」判定は、正常なプロセス(起動元が先に終了した
# 親)を誤って孤児判定する退行を実際に起こしたため、名前ベースに戻した)。
_ROOT_PROCESS_NAME = "claude.exe"

# 祖先を辿る深さの上限(循環・異常なツリーへの安全弁)。
_MAX_ANCESTOR_DEPTH = 16


def _log(message: str) -> None:
    print(message, file=sys.stderr, flush=True)


# Get-CimInstanceの結果をCSVで受け取るPowerShellコマンド。
# CreationDateはロケール依存の文字列表現だと辞書順ソートが破綻する
# (例: "9:5:00" > "10:5:00" と誤判定される)ため、ISO-8601形式(sortable)に
# 明示変換してから受け取る。
_PS_SNAPSHOT_CMD = (
    "Get-CimInstance Win32_Process | "
    "Select-Object ProcessId, ParentProcessId, Name, CommandLine, "
    "@{Name='CreationDateSortable'; Expression={ $_.CreationDate.ToString('o') }} | "
    "ConvertTo-Csv -NoTypeInformation"
)


def _run_powershell_csv(command: str) -> list[dict[str, str]]:
    """PowerShellコマンドを実行し、CSV出力を辞書のリストとして返す(空リスト=失敗)。

    (2026-08-27: 当初`wmic`を使っていたが、出力が実機でUTF-16LEで返り
    `utf-8`固定decodeで文字化けし、全プロセスを誤って孤児判定する事故直前の
    状態になった。`Get-CimInstance`は出力エンコーディングが安定しており、
    実測でも正しく取得できることを確認済み)

    失敗時は空リストを返すだけでなく、stderrへ警告を出す(2026-08-27 Lv8指摘:
    サイレント失敗で掃除が機能不全になっても運用者が気づけなかったため)。
    """
    try:
        result = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", command],
            capture_output=True,
            timeout=20,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        _log(f"[serena_launcher] PowerShell実行に失敗(掃除処理をスキップします): {exc}")
        return []
    if result.returncode != 0:
        detail = result.stderr.decode("utf-8", errors="replace").strip()
        _log(
            "[serena_launcher] PowerShellがエラー終了しました"
            f"(returncode={result.returncode})。掃除処理をスキップします: {detail[:300]}"
        )
        return []
    text = result.stdout.decode("utf-8", errors="replace")
    lines = [line for line in text.splitlines() if line.strip()]
    if len(lines) < 2:
        _log("[serena_launcher] PowerShell出力が空でした。掃除処理をスキップします")
        return []
    reader = csv.DictReader(io.StringIO("\n".join(lines)))
    rows = [row for row in reader if row.get("ProcessId")]
    if not rows:
        _log("[serena_launcher] PowerShell出力の解析結果が0件でした。掃除処理をスキップします")
    return rows


def _snapshot_processes() -> dict[str, dict[str, str]]:
    """全プロセスのPID/親PID/名前/コマンドライン/起動時刻を一括取得する。

    (2026-08-27 Lv8指摘: プロセスごとにtasklistを個別起動する旧実装は、
    プロセス数に比例して起動が遅くなっていた。一括取得+メモリ上の辞書参照に変更)
    """
    rows = _run_powershell_csv(_PS_SNAPSHOT_CMD)
    return {row["ProcessId"].strip(): row for row in rows if row.get("ProcessId", "").strip()}


def _extract_flag_value(cmdline: str, flag: str) -> str | None:
    """コマンドラインから指定フラグの直後の値を抽出する。

    (2026-08-27 Lv8 2回目レビュー指摘: 旧実装(`--project`専用)は部分文字列一致
    だったため、`C:/ClaudeCode2`のような別プロジェクトにも誤って一致しうる
    欠陥があった。実際に引数値を取り出して比較する。
    2026-08-27夜: `--project`固定指定を撤去したのに伴い、`--context`にも
    使えるよう汎用化した)
    """
    tokens = cmdline.split()
    for i, token in enumerate(tokens):
        if token == flag and i + 1 < len(tokens):
            return tokens[i + 1].strip()
    return None


def _is_target_serena(name: str, cmdline: str) -> bool:
    """serena.exeで、このラッパー経由(--context claude-code)で起動されたものか。

    (2026-08-27夜: `--project`を起動時の固定引数から撤去し、セッション側から
    `activate_project`で動的に切り替える方式に変更したため、従来の
    「`--project`の値を厳密比較」という判定はもう使えない。`--context`の値で
    判定するよう変更した。既知の限界はモジュールdocstring参照)
    """
    if name != "serena.exe":
        return False
    context_arg = _extract_flag_value(cmdline, "--context")
    return context_arg == "claude-code"


def _is_target_node(name: str, cmdline: str) -> bool:
    """node.exeで、Serena自身の言語サーバー展開先から起動されたものか。

    (2026-08-27 Lv8 2回目レビュー指摘: 旧実装は'tsserver'/'pyright'等の
    汎用的なツール名文字列だけで判定しており、VS Code等の無関係なツールが
    起動した同名の言語サーバーまで対象に含まれ、それらは祖先にclaude.exeが
    無いため誤ってkillされる恐れがあった。Serena固有のインストール先パスを
    シグネチャにすることで、他ツールのプロセスを巻き込まないようにする)
    """
    if name != "node.exe":
        return False
    return _SERENA_HOME_SIGNATURE in cmdline.lower().replace("\\", "/")


def _ancestor_chain_alive(pid: str, all_procs: dict[str, dict[str, str]]) -> bool:
    """pidから祖先をさかのぼり、`claude.exe`まで生存を確認できるかを判定する。

    (2026-08-27 Lv8 1回目レビュー指摘: 旧実装は「直接の親PIDが生きているか」
    だけを見ていたため、ラッパー自身(python)がserena.exeの直接の親になる
    構造では、claude.exeが死んでもラッパープロセスさえ生きていれば孤児と
    判定できなかった。祖先を辿って判定するよう修正した)

    (2026-08-27 Lv8 2回目レビュー指摘を受けて一度「特定の名前に依存せず、
    チェーンが自然終端まで途切れないか」に一般化したが、実機の`--diagnose`で
    新たな誤判定(生きているclaude.exeセッション配下を孤児扱い)を起こした
    ため、名前ベースの判定に戻した。詳細はモジュールdocstring項目7を参照)。

    祖先に`claude.exe`という名前のプロセスが見つかった時点で生存確定とし、
    それより先(desktopアプリ本体等)は遡らない。`claude.exe`に達する前に
    親が空/自己参照/PID 0(自然終端)になった場合や、祖先PID自体が見つから
    ない場合は「対象外プロセスか孤児」としてFalseを返す。

    各ホップで「現在そのPIDにいるプロセスの起動時刻が、子より前か」を確認し、
    Windowsのプロセス番号再利用(無関係な後発プロセスが同じPIDを名乗るケース)を
    弾く(2026-08-27 Lv8 1回目レビュー指摘)。祖先が異常に深い/循環している
    場合のみ判定不能として安全側(生存扱い)に倒す。
    """
    current = pid
    child_creation = ""
    for _ in range(_MAX_ANCESTOR_DEPTH):
        proc = all_procs.get(current)
        if proc is None:
            return False  # 祖先が見つからない = チェーンが途切れている(孤児)
        name = (proc.get("Name") or "").strip().lower()
        creation = (proc.get("CreationDateSortable") or "").strip()
        if child_creation and creation and creation > child_creation:
            # 現在このPIDにいるプロセスが、子より後に生まれている
            # = PID再利用による無関係な別プロセス。祖先ではない。
            return False
        if name == _ROOT_PROCESS_NAME:
            return True  # claude.exeまで生存を確認できた(これより先は遡らない)
        ppid = (proc.get("ParentProcessId") or "").strip()
        if not ppid or ppid == current or ppid == "0":
            return False  # claude.exeに達する前に自然終端 = 対象外プロセスか孤児
        child_creation = creation
        current = ppid
    return True  # 異常に深い/循環している場合は判定不能として安全側に倒す


def _verify_process_unchanged(pid: str, expected: dict[str, str]) -> bool:
    """kill直前に、対象PIDがsnapshot取得時から変化していないか再確認する。

    (2026-08-27 Lv8 Step C再レビュー指摘: `cleanup_orphans`はループの先頭で
    取得した1回のsnapshotを使い回すが、ループの途中で先行するtaskkillの`/T`
    (祖先killが子孫を道連れにする)が発生したり、時間経過でPIDが再利用
    されたりすると、snapshot時点の情報がkill実行時には古くなっている
    可能性がある。killの直前に単体で再取得し、Name/CreationDateSortableが
    一致する場合のみ「snapshot時と同一プロセス」とみなす。判定不能な場合は
    安全側(kill しない)に倒す)

    (2026-08-27 Step C2実装直後、実プロセスに対する統合確認で発覚した自己バグ:
    `_run_powershell_csv`は`row.get("ProcessId")`が空の行を無条件に捨てる仕様
    だが、当初このクエリは`ProcessId`列をSelect-Objectに含めていなかったため、
    **常に空リストが返り、本関数が常にFalse(=一切killしない)になっていた**。
    単体テストはモックで`_run_powershell_csv`自体を差し替えていたためこの欠陥を
    検出できず、実プロセスに対する統合確認で初めて発覚した。`ProcessId`を
    明示的にSelect-Objectへ含めることで解消した)
    """
    if not pid.isdigit():
        return False
    rows = _run_powershell_csv(
        f"Get-CimInstance Win32_Process -Filter 'ProcessId={pid}' | "
        "Select-Object ProcessId, Name, "
        "@{Name='CreationDateSortable'; Expression={ $_.CreationDate.ToString('o') }} | "
        "ConvertTo-Csv -NoTypeInformation"
    )
    if not rows:
        return False  # 既に終了している(道連れ等) = kill対象として扱わない
    current = rows[0]
    expected_name = (expected.get("Name") or "").strip().lower()
    expected_creation = (expected.get("CreationDateSortable") or "").strip()
    return (
        (current.get("Name") or "").strip().lower() == expected_name
        and (current.get("CreationDateSortable") or "").strip() == expected_creation
    )


def _collect_targets(all_procs: dict[str, dict[str, str]]) -> list[tuple[str, str]]:
    """掃除対象候補(このプロジェクトのSerenaファミリー)を洗い出す。

    戻り値: [(pid, name), ...]。
    """
    targets: list[tuple[str, str]] = []
    for pid, row in all_procs.items():
        name = (row.get("Name") or "").strip().lower()
        cmdline = row.get("CommandLine") or ""
        if _is_target_serena(name, cmdline) or _is_target_node(name, cmdline):
            targets.append((pid, name))
    return targets


def cleanup_orphans(
    *, dry_run: bool = False, snapshot: dict[str, dict[str, str]] | None = None
) -> int:
    """このプロジェクトのSerenaファミリー(serena.exe本体+言語サーバー群)のうち、
    祖先チェーンが途切れているものを終了する。

    dry_run=True の場合は実際にはkillせず、対象と判定理由をstderrへログするのみ
    (2026-08-27 Lv8レビューでCodexが提案した安全弁。新しい判定ロジックを
    実データで検証してから有効化できるようにする)。

    snapshot を渡すと、そのプロセス一覧を使う(2026-08-27 Lv8 2回目レビュー指摘:
    `--diagnose`が件数表示用と判定用で別々にsnapshotを取っており、その間に
    プロセスが増減すると表示件数と実際の判定対象がずれ得た。呼び出し側で
    1回だけ取得したsnapshotを使い回せるようにする)。

    戻り値: 終了した(dry_runなら「終了するはずだった」)数。
    """
    all_procs = snapshot if snapshot is not None else _snapshot_processes()
    if not all_procs:
        return 0

    killed = 0
    for pid, name in _collect_targets(all_procs):
        if _ancestor_chain_alive(pid, all_procs):
            continue  # 祖先チェーンが途切れず生存 = 孤児ではない。触らない

        if dry_run:
            _log(f"[serena_launcher][dry-run] 孤児候補: pid={pid} name={name} (終了はしません)")
            killed += 1
            continue

        if not _verify_process_unchanged(pid, all_procs[pid]):
            _log(
                f"[serena_launcher] pid={pid} はsnapshot取得後に状態が変化"
                "(道連れ終了/PID再利用の疑い) -> 安全のためスキップします"
            )
            continue

        _log(f"[serena_launcher] 孤児検出: pid={pid} name={name} -> 終了します")
        try:
            result = subprocess.run(
                ["taskkill", "/F", "/T", "/PID", pid],
                capture_output=True,
                timeout=15,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            _log(f"[serena_launcher] pid={pid} の終了に失敗: {exc}")
            continue
        if result.returncode == 0:
            killed += 1
        else:
            detail = result.stderr.decode("utf-8", errors="replace").strip()
            _log(
                f"[serena_launcher] pid={pid} のtaskkillが失敗"
                f"(returncode={result.returncode}): {detail}"
            )
    if killed:
        label = "孤児プロセス候補" if dry_run else "孤児プロセス"
        _log(f"[serena_launcher] {label}を{killed}件検出しました")
    return killed


def main() -> int:
    try:
        cleanup_orphans()
    except Exception as exc:  # noqa: BLE001 — クリーンアップ失敗で起動自体を止めない
        _log(f"[serena_launcher] クリーンアップ処理で例外(無視して起動継続): {exc}")

    # 標準入出力を継承したまま本体を起動する(透過ラッパー)。
    # stdin/stdout/stderrをリダイレクトしないことで、Claude Code <-> Serena の
    # JSON-RPC通信はこのラッパーを素通りする。
    # --project は固定しない(2026-08-27夜: サブプロジェクト単位スコープ化)。
    # セッション側が activate_project ツールで必要なプロジェクトだけを動的に開く。
    proc = subprocess.Popen([SERENA_EXE, "start-mcp-server", "--context", "claude-code"])
    return proc.wait()


if __name__ == "__main__":
    if "--diagnose" in sys.argv[1:]:
        # 手動診断用: 実際には何もkillせず、現在のserenaファミリーの状態を一覧する。
        _snap = _snapshot_processes()
        _n = cleanup_orphans(dry_run=True, snapshot=_snap)
        _log(f"[serena_launcher] --diagnose: 孤児候補 {_n} 件(スキャン対象プロセス数 {len(_snap)})")
        sys.exit(0)
    sys.exit(main())
