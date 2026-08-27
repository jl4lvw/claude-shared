"""test_serena_launcher — serena_launcher.py の孤児判定ロジックの単体テスト.

なぜ必要か:
    2026-08-27、初版(直接の親PIDのみを見る実装)がcgd Lv8レビューで
    「claude.exe死後もラッパープロセスが残る経路では孤児判定が機能しない」
    「serena.exeの子孫(tsserver/pyright等)が掃除対象外」等の欠陥を指摘され、
    祖先チェーンを辿る方式に再設計した。

    再設計の実装直後、実機の`--diagnose`(dry-run)テストで「全プロセスを
    誤って孤児判定する」(wmic出力のエンコーディング事故)が発覚し、
    PowerShell(Get-CimInstance)に置き換えた。

    さらにcgd Lv8のStep C差分レビューで、「tsserver/pyright等の汎用的な
    ツール名でnode.exeを対象化すると、VS Code等の無関係なツールが起動した
    同名の言語サーバーまで誤ってkillしうる」という🔴指摘を受け、Serena固有の
    インストール先パス(~/.serena/language_servers/)での判定に変更した。
    生きているセッション・無関係な他ツールを誤ってkillしかねない機能なので、
    ロジックの退行に気づけないと実害が大きい。

    実プロセスを対象にした統合テストは書かない(本物のセッションや他ツールを
    誤ってkillするリスクがあるため)。ここでは合成データで純粋関数のロジックだけを
    検証する。

実行方法:
    python -m pytest .claude/tools/tests/test_serena_launcher.py -v
"""

from __future__ import annotations

import sys
from pathlib import Path

TOOLS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TOOLS))

import serena_launcher as sl  # noqa: E402

_SERENA_TS_CMDLINE = (
    str(Path.home() / ".serena" / "language_servers" / "static" / "TypeScriptLanguageServer")
    + r"\ts-lsp\node_modules\typescript-language-server\lib\cli.mjs --stdio"
)
_VSCODE_TS_CMDLINE = (
    r"C:\Users\user\AppData\Local\Programs\Microsoft VS Code\resources\app\extensions"
    r"\typescript-language-features\node_modules\typescript\lib\tsserver.js"
)


def _proc(
    pid: str, ppid: str, name: str, creation: str, cmdline: str = ""
) -> dict[str, str]:
    return {
        "ProcessId": pid,
        "ParentProcessId": ppid,
        "Name": name,
        "CommandLine": cmdline,
        "CreationDateSortable": creation,
    }


def test_ancestor_chain_alive_normal_session() -> None:
    """serena.exe -> pythonランチャー -> claude.exe -> 更に上位(自然終端) は孤児ではない。"""
    procs = {
        "300": _proc("300", "200", "serena.exe", "2026-08-27T10:00:00"),
        "200": _proc("200", "100", "python.exe", "2026-08-27T09:59:00"),
        "100": _proc("100", "50", "claude.exe", "2026-08-27T08:00:00"),
        "50": _proc("50", "0", "explorer.exe", "2026-08-26T08:00:00"),
    }
    assert sl._ancestor_chain_alive("300", procs) is True


def test_ancestor_chain_orphan_root_missing() -> None:
    """ラッパー(python)は生存しているが、その親(claude.exe)が既に居ない=孤児。

    2026-08-27 Lv8 1回目レビュー指摘の本丸: 旧実装は直接の親(python)しか
    見ておらず、このケースを孤児と判定できなかった。
    """
    procs = {
        "300": _proc("300", "200", "serena.exe", "2026-08-27T10:00:00"),
        "200": _proc("200", "100", "python.exe", "2026-08-27T09:59:00"),
        # "100"(claude.exe)は既に存在しない(死んだ)
    }
    assert sl._ancestor_chain_alive("300", procs) is False


def test_ancestor_chain_pid_reuse_rejected() -> None:
    """親PIDに別の(子より後に生まれた)プロセスが居座っている場合は孤児扱い。

    WindowsはPIDを再利用するため、単純な「PIDが存在するか」だけでは
    無関係な後発プロセスを「親が生きている」と誤判定しうる
    (2026-08-27 Lv8 1回目レビュー指摘B1)。
    """
    procs = {
        "300": _proc("300", "200", "serena.exe", "2026-08-27T10:00:00"),
        # PID 200は元の親が終了した後、無関係な別プロセスに再利用された
        # (CreationDateが子(300)より後になっている)
        "200": _proc("200", "1", "notepad.exe", "2026-08-27T11:00:00"),
    }
    assert sl._ancestor_chain_alive("300", procs) is False


def test_ancestor_chain_depth_limit_is_safe_default() -> None:
    """claude.exeに達しないまま上限深さに達した場合は判定不能として安全側(生存扱い)に倒す。"""
    procs: dict[str, dict[str, str]] = {}
    # 0 -> 1 -> 2 -> ... の自己参照しないループ(claude.exeに到達せず、自然終端もない)
    for i in range(30):
        procs[str(i)] = _proc(str(i), str(i + 1), "python.exe", "2026-08-27T09:00:00")
    assert sl._ancestor_chain_alive("0", procs) is True


def test_ancestor_chain_no_parent_pid_without_claude_is_orphan() -> None:
    """claude.exeに達する前に親PIDが空/自己参照/'0'(自然終端)になった場合は孤児扱い。

    (2026-08-27 Lv8 2回目レビュー指摘を受けて一度「特定の名前に依存せず、
    チェーンが自然終端まで途切れないか」に一般化したが、実機の`--diagnose`で
    「claude.exe自身の祖先(Desktopアプリ本体等)が、既に終了した起動元を
    親PIDに持つ」正常なケースまで誤って孤児判定する退行を起こしたため、
    名前ベース(claude.exeに達したら生存確定)の判定に戻した。この関数が
    見ているのは`_is_target_serena`/`_is_target_node`で既に絞り込み済みの
    Serenaファミリーのみなので、claude.exeに達する前の自然終端は
    「対象外プロセスか孤児」としてFalseにする)
    """
    assert sl._ancestor_chain_alive(
        "300", {"300": _proc("300", "", "serena.exe", "t")}
    ) is False
    assert sl._ancestor_chain_alive(
        "300", {"300": _proc("300", "300", "serena.exe", "t")}
    ) is False
    assert sl._ancestor_chain_alive(
        "300", {"300": _proc("300", "0", "serena.exe", "t")}
    ) is False


def test_ancestor_chain_stops_at_claude_exe_without_walking_further() -> None:
    """claude.exeに到達したら、その先(祖先のさらに祖先)は一切見ない。

    claude.exe自身の親が既に終了していて`all_procs`に存在しなくても、
    claude.exeを見つけた時点でTrueを返すため影響しない
    (2026-08-27 実機で確認した正常パターン: Desktopアプリ本体の起動元
    ランチャーが先に終了しているケース)。
    """
    procs = {
        "300": _proc("300", "200", "serena.exe", "2026-08-27T10:00:00"),
        "200": _proc("200", "100", "claude.exe", "2026-08-27T09:00:00"),
        # "100"(claude.exeの親)は既に存在しない(起動元ランチャーが先に終了)が、
        # 200で claude.exe を見つけた時点で確定するので無関係。
    }
    assert sl._ancestor_chain_alive("300", procs) is True


def test_is_target_serena_matches_exact_project_arg() -> None:
    cmdline = f"serena start-mcp-server --context claude-code --project {sl.PROJECT_PATH}"
    assert sl._is_target_serena("serena.exe", cmdline) is True


def test_is_target_serena_rejects_similar_but_different_project() -> None:
    """`--project`の値が部分文字列として一致するだけの別プロジェクトは対象外。

    2026-08-27 Lv8 2回目レビュー指摘: 旧実装は`PROJECT_PATH in cmdline`という
    部分一致だったため、`C:/ClaudeCode2`のような別プロジェクトにも
    誤って一致しうる欠陥があった。
    """
    cmdline = f"serena start-mcp-server --project {sl.PROJECT_PATH}2"
    assert sl._is_target_serena("serena.exe", cmdline) is False


def test_is_target_serena_requires_project_flag_present() -> None:
    # PROJECT_PATHが引数値としてではなく別の場所に出現しても対象化しない
    cmdline = f"serena start-mcp-server --note {sl.PROJECT_PATH}"
    assert sl._is_target_serena("serena.exe", cmdline) is False


def test_is_target_node_matches_serena_home_path() -> None:
    assert sl._is_target_node("node.exe", _SERENA_TS_CMDLINE) is True


def test_is_target_node_rejects_unrelated_vscode_tsserver() -> None:
    """VS Code等、無関係なツールが起動した同名の言語サーバーは対象化しない。

    2026-08-27 Lv8 Step C(差分)レビューの🔴指摘: 旧実装は'tsserver'という
    汎用的な文字列だけで判定していたため、VS Codeのtsserverまで対象に含まれ、
    祖先にclaude.exeが無いため誤ってkillされる恐れがあった。
    """
    assert sl._is_target_node("node.exe", _VSCODE_TS_CMDLINE) is False


def test_is_target_node_rejects_unrelated_node_process() -> None:
    assert sl._is_target_node("node.exe", "node some_unrelated_script.js") is False


def test_is_target_node_rejects_sibling_dir_with_shared_prefix() -> None:
    """`.serena`配下でも`language_servers`以外や、名前が前方一致するだけの
    無関係なディレクトリは対象化しない。

    2026-08-27 Lv8 Step C再レビューの🔴指摘: 旧実装は`~/.serena`
    (language_serversを含まない親ディレクトリ)を丸ごとシグネチャにしていた
    ため、`~/.serena_backup/...`や`~/.serena/cache/...`のような無関係な
    パスまで前方一致で誤って含んでしまっていた。
    """
    home = str(Path.home()).replace("\\", "/")
    assert sl._is_target_node("node.exe", f"node {home}/.serena/cache/foo.js") is False
    assert sl._is_target_node("node.exe", f"node {home}/.serena_backup/foo.js") is False
    assert (
        sl._is_target_node("node.exe", f"node {home}/.serena/language_servers2/foo.js")
        is False
    )


def test_collect_targets_filters_correctly() -> None:
    procs = {
        # 対象: このプロジェクトのserena.exe
        "1": _proc(
            "1", "0", "serena.exe", "t",
            cmdline=f"serena start-mcp-server --project {sl.PROJECT_PATH}",
        ),
        # 対象外: serena.exeだが別プロジェクト
        "2": _proc(
            "2", "0", "serena.exe", "t",
            cmdline=f"serena start-mcp-server --project {sl.PROJECT_PATH}_other",
        ),
        # 対象: Serena自身の言語サーバー展開先から起動されたnode.exe
        "3": _proc("3", "0", "node.exe", "t", cmdline=_SERENA_TS_CMDLINE),
        # 対象外: VS Codeのtsserver(無関係な他ツール)
        "4": _proc("4", "0", "node.exe", "t", cmdline=_VSCODE_TS_CMDLINE),
        # 対象外: そもそも無関係なプロセス
        "5": _proc("5", "0", "notepad.exe", "t"),
    }
    targets = sl._collect_targets(procs)
    target_pids = {pid for pid, _name in targets}
    assert target_pids == {"1", "3"}


def test_verify_process_unchanged_matches(monkeypatch) -> None:
    expected = _proc("300", "0", "serena.exe", "2026-08-27T10:00:00")
    monkeypatch.setattr(
        sl,
        "_run_powershell_csv",
        lambda _cmd: [{"Name": "serena.exe", "CreationDateSortable": "2026-08-27T10:00:00"}],
    )
    assert sl._verify_process_unchanged("300", expected) is True


def test_verify_process_unchanged_detects_pid_reuse(monkeypatch) -> None:
    """kill直前に再取得した情報がsnapshot時と食い違う(=PID再利用)場合はFalse。

    2026-08-27 Lv8 Step C再レビューの🔴指摘: `cleanup_orphans`はループ先頭の
    1回のsnapshotを使い回すため、kill実行までの間にPIDが再利用されると
    無関係なプロセスをkillしうる欠陥があった。
    """
    expected = _proc("300", "0", "serena.exe", "2026-08-27T10:00:00")
    monkeypatch.setattr(
        sl,
        "_run_powershell_csv",
        lambda _cmd: [{"Name": "notepad.exe", "CreationDateSortable": "2026-08-27T11:00:00"}],
    )
    assert sl._verify_process_unchanged("300", expected) is False


def test_verify_process_unchanged_already_gone(monkeypatch) -> None:
    """先行するtaskkill `/T`で既に道連れ終了している場合はFalse(kill不要)。"""
    expected = _proc("300", "0", "serena.exe", "2026-08-27T10:00:00")
    monkeypatch.setattr(sl, "_run_powershell_csv", lambda _cmd: [])
    assert sl._verify_process_unchanged("300", expected) is False


def test_verify_process_unchanged_rejects_non_numeric_pid(monkeypatch) -> None:
    def _fail(_cmd: str) -> None:
        raise AssertionError("PIDが数値でない場合はPowerShellを呼ぶ前に弾くべき")

    monkeypatch.setattr(sl, "_run_powershell_csv", _fail)
    expected = _proc("300", "0", "serena.exe", "2026-08-27T10:00:00")
    assert sl._verify_process_unchanged("300; Remove-Item C:\\", expected) is False


def test_verify_process_unchanged_integrates_with_real_csv_parsing(monkeypatch) -> None:
    """`_run_powershell_csv`をモックせず、実際のCSVパース+フィルタ経路を通して確認する。

    2026-08-27 実プロセスに対する統合確認で発覚した自己バグの再発防止用テスト:
    `_verify_process_unchanged`のPowerShellクエリが`ProcessId`を
    Select-Objectに含めていなかったため、`_run_powershell_csv`の
    `row.get("ProcessId")`フィルタで全行が握り潰され、本関数が実環境では
    常にFalse(=一切killしない)になっていた。他のテストは`_run_powershell_csv`
    自体をモックしていたためこの欠陥を検出できなかった。ここでは
    `subprocess.run`だけをモックし、実際のCSVパースを通す。
    """
    csv_output = (
        '"ProcessId","Name","CreationDateSortable"\r\n'
        '"12345","serena.exe","2026-08-27T10:00:00.0000000+09:00"\r\n'
    )

    class _FakeResult:
        returncode = 0
        stdout = csv_output.encode("utf-8")
        stderr = b""

    monkeypatch.setattr(sl.subprocess, "run", lambda *_a, **_k: _FakeResult())
    expected = _proc("12345", "0", "serena.exe", "2026-08-27T10:00:00.0000000+09:00")
    assert sl._verify_process_unchanged("12345", expected) is True


def test_cleanup_orphans_skips_target_whose_state_changed_since_snapshot(monkeypatch) -> None:
    """snapshot後に状態が変化したtargetはtaskkillせずスキップする。

    2026-08-27 Lv8 Step C再レビューの🔴指摘の実地確認: serena.exe(親)への
    taskkill `/T`がnode.exe(子)を道連れにした後、ループが子のPIDに到達しても
    再killを試みない(既に居ないので`_verify_process_unchanged`がFalseを返す)。
    """
    procs = {
        "1": _proc(
            "1", "2", "serena.exe", "2026-08-27T10:00:00",
            cmdline=f"serena --project {sl.PROJECT_PATH}",
        ),
        # "2"(親)が既に存在しない = 孤児
    }

    monkeypatch.setattr(sl, "_verify_process_unchanged", lambda _pid, _expected: False)

    def _fail(cmd: list[str], **_kwargs: object) -> None:
        raise AssertionError(f"状態変化を検知したtargetでtaskkillが呼ばれた: {cmd}")

    monkeypatch.setattr(sl.subprocess, "run", _fail)
    killed = sl.cleanup_orphans(dry_run=False, snapshot=procs)
    assert killed == 0


def test_cleanup_orphans_dry_run_does_not_call_taskkill(monkeypatch) -> None:
    """dry_run=True のとき、実際のtaskkill呼び出しが一切発生しないことを確認する。

    (2026-08-27: 生きているセッション/無関係なプロセスを誤ってkillしないことを
    保証する最後の砦)
    """
    procs = {
        # 孤児(祖先が見つからない) - dry_runなら「候補」として数えるだけ
        "10": _proc(
            "10", "20", "serena.exe", "2026-08-27T10:00:00",
            cmdline=f"serena --project {sl.PROJECT_PATH}",
        ),
    }

    called: list[list[str]] = []

    def _fake_run(cmd: list[str], **_kwargs: object) -> None:
        called.append(cmd)
        raise AssertionError("taskkill must not be called in dry_run mode")

    monkeypatch.setattr(sl.subprocess, "run", _fake_run)
    killed = sl.cleanup_orphans(dry_run=True, snapshot=procs)
    assert killed == 1
    assert called == []


def test_cleanup_orphans_leaves_alive_family_untouched(monkeypatch) -> None:
    """生存中のSerenaファミリーには一切triggerがかからないことを確認する。"""
    procs = {
        "1": _proc(
            "1", "2", "serena.exe", "2026-08-27T10:00:00",
            cmdline=f"serena --project {sl.PROJECT_PATH}",
        ),
        "2": _proc("2", "3", "python.exe", "2026-08-27T09:59:00"),
        "3": _proc("3", "0", "claude.exe", "2026-08-27T08:00:00"),
        "4": _proc("4", "1", "node.exe", "2026-08-27T10:01:00", cmdline=_SERENA_TS_CMDLINE),
    }

    def _fake_run(cmd: list[str], **_kwargs: object) -> None:
        raise AssertionError(f"taskkill must not be called for a live family: {cmd}")

    monkeypatch.setattr(sl.subprocess, "run", _fake_run)
    killed = sl.cleanup_orphans(dry_run=False, snapshot=procs)
    assert killed == 0
