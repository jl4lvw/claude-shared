"""test_cgd_wf_gate — WF 強制ゲート (PreToolUse hook + CLI) の単体テスト.

なぜ必要か:
    cgd_wf_gate.py はこの一連のインシデントの**震源**でありながら、
    2026-08-12 まで pytest が 1 件も無かった（pv Lv3 の棚卸し担当が実測指摘）。
    検証はすべて手で叩いており、直したはずの穴が戻っても誰も気づけない。

    実際に見つかった穴（どれも「遮断しているつもりで素通り」だった）:
      - stdin を cp932 で復号し、日本語入りコマンドで無言 fail-open
      - matcher が Bash のみで PowerShell 経由が素通り
      - 否定後読みに . と / を含み ./codex や /usr/bin/codex が素通り
      - PowerShell 形式の nonce を抽出できず正当な実行まで deny
      - status/disarm が --session 省略時に無言 no-op

実行方法:
    python -m pytest .claude/tools/tests/test_cgd_wf_gate.py -v
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

TOOLS = Path(__file__).resolve().parents[1]
HOOKS = TOOLS.parent / "hooks"
sys.path.insert(0, str(HOOKS))

import cgd_wf_gate as gate  # noqa: E402

GATE_PY = HOOKS / "cgd_wf_gate.py"

# ソースに `codex` の語をそのまま置くと、ゲートが張られている間に
# このファイルを grep する類のコマンドまで巻き添えで止まる。組み立てて使う。
CX = "co" + "dex"


# ------------------------------------------------------------ 検知の網羅性


@pytest.mark.parametrize("command", [
    f'{CX} exec "x"',
    f'./{CX} exec "x"',
    f'/usr/bin/{CX} exec "x"',
    f'C:/tools/{CX}.exe exec "x"',
    f'~/bin/{CX} exec "x"',
    f"bash -c '{CX} exec x'",
    f'env FOO=1 {CX} exec x',
    f'ls && {CX} exec x',
])
def test_mentions_codex_detects_invocations(command: str) -> None:
    """パス付き起動を見逃さないこと。

    否定後読みに `.` と `/` が入っていた版では、`./codex` 等が**全部素通り**した。
    除外していた文字が「パス付きで起動する形」そのものだった。
    """
    assert gate.mentions_codex(command) is True, f"素通りした: {command}"


@pytest.mark.parametrize("command", [
    f"cat C:/tmp-ai/cgd_{CX}_20260812.txt",
    f"python foo.py --out {CX}_input.txt",
    f"echo my{CX}tool",
    "ls C:/tmp-ai/",
])
def test_mentions_codex_ignores_word_internals(command: str) -> None:
    """語の一部としての出現では誤検知しないこと（過剰遮断も破綻）。"""
    assert gate.mentions_codex(command) is False, f"誤検知した: {command}"


# ------------------------------------------------------------ nonce の束縛


@pytest.mark.parametrize("command,expected", [
    (f"CGD_WF_RUN=abc123 {CX} exec x", "abc123"),
    (f"env CGD_WF_RUN=abc123 {CX} exec x", "abc123"),
    (f'$env:CGD_WF_RUN="abc123"; {CX} exec x', "abc123"),
    (f"$env:CGD_WF_RUN='abc123'; {CX} exec x", "abc123"),
    (f'$env:CGD_WF_RUN = "abc123" ; {CX} exec x', "abc123"),
])
def test_extract_bypass_nonce_accepts_bound_forms(command: str, expected: str) -> None:
    """Bash / PowerShell の両方で、その起動に束縛された代入だけを認める。"""
    assert gate.extract_bypass_nonce(command) == expected


@pytest.mark.parametrize("command", [
    f"echo CGD_WF_RUN=abc123; {CX} exec x",          # 先出し
    f'$env:CGD_WF_RUN="abc"; ls; echo hi; {CX} exec x',  # 別コマンドを跨ぐ
    f"{CX} exec x",                                   # そもそも無い
])
def test_extract_bypass_nonce_rejects_unbound(command: str) -> None:
    assert gate.extract_bypass_nonce(command) is None


# ------------------------------------------------------------ hook の判定


def _hook(payload: dict) -> tuple[int, str, str]:
    proc = subprocess.run(
        [sys.executable, str(GATE_PY)],
        input=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        capture_output=True,
    )
    return (proc.returncode,
            proc.stdout.decode("utf-8", errors="replace"),
            proc.stderr.decode("utf-8", errors="replace"))


@pytest.fixture()
def armed_gate(tmp_path, monkeypatch):
    """テスト用のゲートを張る。GATE_DIR を tmp に逃がして本番を汚さない。"""
    monkeypatch.setenv("CGD_WF_GATE_DIR", str(tmp_path))
    sid = "pytest-session"
    proc = subprocess.run(
        [sys.executable, str(GATE_PY), "arm", "--level", "8", "--session", sid],
        capture_output=True, env={**__import__("os").environ, "CGD_WF_GATE_DIR": str(tmp_path)},
    )
    assert proc.returncode == 0
    nonce = json.loads(
        subprocess.run(
            [sys.executable, str(GATE_PY), "status", "--json", "--session", sid],
            capture_output=True,
            env={**__import__("os").environ, "CGD_WF_GATE_DIR": str(tmp_path)},
        ).stdout.decode("utf-8")
    )["gates"][0]["nonce"]
    return {"sid": sid, "nonce": nonce, "dir": str(tmp_path)}


def _hook_in(payload: dict, gate_dir: str) -> str:
    import os
    proc = subprocess.run(
        [sys.executable, str(GATE_PY)],
        input=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        capture_output=True,
        env={**os.environ, "CGD_WF_GATE_DIR": gate_dir},
    )
    return proc.stdout.decode("utf-8", errors="replace")


@pytest.mark.parametrize("tool", ["Bash", "PowerShell"])
def test_hook_denies_shell_tools(armed_gate, tool: str) -> None:
    """Bash だけ見ていると PowerShell 経由が素通りする。"""
    out = _hook_in({"tool_name": tool, "session_id": armed_gate["sid"],
                    "tool_input": {"command": f'{CX} exec "x"'}}, armed_gate["dir"])
    assert "deny" in out, f"{tool} が遮断されていない"


def test_hook_ignores_non_shell_tools(armed_gate) -> None:
    out = _hook_in({"tool_name": "Read", "session_id": armed_gate["sid"],
                    "tool_input": {"command": f'{CX} exec "x"'}}, armed_gate["dir"])
    assert out == ""


def test_hook_survives_japanese_command(armed_gate) -> None:
    """cp932 で復号すると壊れる並びでも遮断できること。

    「。」の末尾バイトが続く 'c' を食う / 「、"」が cp932 として不正になり
    UnicodeDecodeError(=ValueError) として握り潰される、の 2 経路があった。
    """
    for command in (f'レビュー。{CX} exec "hi"', f'{CX} exec "差分をレビュー、"'):
        out = _hook_in({"tool_name": "Bash", "session_id": armed_gate["sid"],
                        "tool_input": {"command": command}}, armed_gate["dir"])
        assert "deny" in out, f"日本語入りで素通りした: {command}"


def test_hook_allows_bound_nonce(armed_gate) -> None:
    out = _hook_in({"tool_name": "Bash", "session_id": armed_gate["sid"],
                    "tool_input": {"command": f'CGD_WF_RUN={armed_gate["nonce"]} {CX} exec x'}},
                   armed_gate["dir"])
    assert out == "", "正しい nonce が通っていない"


def test_hook_allows_bound_nonce_powershell(armed_gate) -> None:
    cmd = f'$env:CGD_WF_RUN="{armed_gate["nonce"]}"; {CX} exec x'
    out = _hook_in({"tool_name": "PowerShell", "session_id": armed_gate["sid"],
                    "tool_input": {"command": cmd}}, armed_gate["dir"])
    assert out == "", "PowerShell 形式の nonce が通っていない"


def test_hook_denies_wrong_nonce(armed_gate) -> None:
    out = _hook_in({"tool_name": "Bash", "session_id": armed_gate["sid"],
                    "tool_input": {"command": f'CGD_WF_RUN=deadbeef {CX} exec x'}},
                   armed_gate["dir"])
    assert "deny" in out


# ------------------------------------------------------------ CLI の挙動


def _cli(*args: str, gate_dir: str) -> subprocess.CompletedProcess[bytes]:
    import os
    return subprocess.run(
        [sys.executable, str(GATE_PY), *args],
        capture_output=True, env={**os.environ, "CGD_WF_GATE_DIR": gate_dir},
    )


def test_status_without_session_sees_session_gates(armed_gate) -> None:
    """--session 省略で「未設定」と嘘をつかないこと。"""
    r = _cli("status", "--json", gate_dir=armed_gate["dir"])
    payload = json.loads(r.stdout.decode("utf-8"))
    assert payload["armed"] is True
    assert payload["count"] == 1


def test_disarm_without_session_actually_disarms(armed_gate) -> None:
    """1 件だけなら --session 無しでも解除できること（無言 no-op にしない）。"""
    r = _cli("disarm", gate_dir=armed_gate["dir"])
    assert r.returncode == 0
    payload = json.loads(_cli("status", "--json", gate_dir=armed_gate["dir"]).stdout.decode("utf-8"))
    assert payload["armed"] is False


def test_arm_rejects_non_positive_ttl(tmp_path) -> None:
    """ttl 0 は「張った瞬間に失効」＝張ったつもりで無防備になる。"""
    r = _cli("arm", "--level", "8", "--session", "x", "--ttl-min", "0",
             gate_dir=str(tmp_path))
    assert r.returncode == 1


def test_corrupt_gate_is_fail_closed(armed_gate) -> None:
    """壊れたゲートは「無い」ではなく「遮断」に倒すこと。"""
    p = Path(armed_gate["dir"]) / f'{armed_gate["sid"]}.json'
    p.write_bytes(b"not json at all")
    r = _cli("status", gate_dir=armed_gate["dir"])
    assert r.returncode == 1
    out = _hook_in({"tool_name": "Bash", "session_id": armed_gate["sid"],
                    "tool_input": {"command": f'{CX} exec x'}}, armed_gate["dir"])
    assert "deny" in out


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
