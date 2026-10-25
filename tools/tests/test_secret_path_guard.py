"""secret_path_guard: 署名の秘密鍵の置き場だけを止め、ほかは通す。"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "hooks"))

import secret_path_guard as guard  # noqa: E402

# 置き場の文字列はここで組み立てる(このファイルを扱うコマンドが hook に止められないように)
FOLDER = "a_browser" + "_data"
KEY = "signing" + "_key.pem"


def denied(tool: str, **tool_input: str) -> bool:
    out = guard.handle_hook({"tool_name": tool, "tool_input": tool_input})
    return out is not None and out["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_blocks_the_key_folder_and_file_in_any_form() -> None:
    assert denied("Read", file_path=f"C:\\ClaudeCode\\900.ClaudeCode\\{FOLDER}\\ts_command\\{KEY}")
    assert denied("Bash", command=f"cat 900.ClaudeCode/{FOLDER}/ts_command/x")
    assert denied("Grep", pattern="BEGIN", path=f"C:/ClaudeCode/900.ClaudeCode/{FOLDER.upper()}/TS_COMMAND")
    assert denied("PowerShell", command=f"Get-Content .\\{KEY}")
    assert denied("Write", file_path=f"C:/x/{FOLDER}//ts_command/new.pem")


def test_allows_everything_else() -> None:
    assert not denied("Read", file_path="C:/ClaudeCode/.codex-review/ts_command_pubkey.pem")
    assert not denied("Bash", command="python -m ts_command.cli enqueue x.ps1 --purpose y")
    assert not denied("Read", file_path=f"C:/ClaudeCode/900.ClaudeCode/{FOLDER}/watch.log")
    assert not denied("WebFetch", url=f"https://example.com/{KEY}")
