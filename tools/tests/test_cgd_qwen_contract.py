"""cgd_lv3a.py が qwen_advisor.py に頼っている「契約」を固定するテスト。

外部ツールの名前・引数・出力の書式を推測で呼ぶと、握りつぶされて黙って別経路へ落ちる
(2026-09-20 の実害)。Lv3A の Qwen 実行器は次の 4 点に頼るので、実物 (qwen_advisor.py) と照合する:
  1. 呼び方: `qwen_advisor.py --role reviewer <入力ファイルのパス>`
  2. 費用の書式: stderr の `[Qwen Usage] 今回: 入力 N (miss) + M (hit) / 出力 K tok (¥Y / $Z)`
  3. 環境変数: 読む変数は DASHSCOPE_ か QWEN_ の接頭辞 (Lv3A が子プロセスへ通す範囲) に収まる
  4. 既定の送信先 (QWEN_BASE_URL が無いときのリージョン)
qwen_advisor.py は import 時に openai を要るので、実物を import / 実行するテストは openai が無ければ skip する
(ソースの文面を見るテストは常に走る)。
"""

from __future__ import annotations

import importlib
import re
import subprocess
import sys
from pathlib import Path

import pytest

TOOLS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TOOLS))

import cgd_lv3a as target  # noqa: E402

SOURCE = (TOOLS / "qwen_advisor.py").read_text(encoding="utf-8")


def test_qwen_advisor_exists_where_the_runner_looks_for_it() -> None:
    assert (TOOLS / "qwen_advisor.py").is_file()
    assert "qwen_advisor.py" in Path(target.__file__).read_text(encoding="utf-8")


def test_source_declares_the_cli_the_runner_uses() -> None:
    assert re.search(r'add_argument\(\s*"input",\s*nargs="\?"', SOURCE), "位置引数 input (ファイルのパス) が無い"
    assert re.search(r'"--role",\s*choices=list\(ROLE_PROMPTS\)', SOURCE), "--role が ROLE_PROMPTS から選ぶ形でない"
    assert re.search(r'^\s+"reviewer": \(', SOURCE, re.MULTILINE), "reviewer の system prompt が無い"
    assert "path.read_text(encoding=\"utf-8\") if path.exists() else args.input" in SOURCE  # パスが実在するときだけファイルとして読む
    assert '"--no-usage"' in SOURCE  # 既定では usage を stderr に出す (Lv3A は付けない)
    reviewer_roles = {spec.role for spec in target.ROSTERS["lv8"] if spec.vendor == "Qwen"}
    assert reviewer_roles == {"reviewer"}


def test_usage_line_format_in_source_matches_the_parser() -> None:
    assert '"今回"' in SOURCE
    assert 'f"[Qwen Usage] {label}: 入力 {cache_miss:,} (miss) + {cache_hit:,} (hit) "' in SOURCE
    assert 'f"/ 出力 {out_tokens:,} tok "' in SOURCE
    assert 'f"(¥{yen:.2f} / ${cost_usd:.4f}){extra}"' in SOURCE


def test_default_base_url_matches_the_one_shown_by_plan() -> None:
    found = re.search(r'^DEFAULT_BASE_URL: str = "([^"]+)"', SOURCE, re.MULTILINE)
    assert found and found.group(1) == target.DEFAULT_QWEN_BASE_URL
    assert '.get("QWEN_BASE_URL", DEFAULT_BASE_URL)' in SOURCE
    for host in target.QWEN_REGIONS:  # plan が名前を付けるリージョンは、qwen_advisor の説明にも出てくる
        assert host in SOURCE, host


def test_every_env_var_the_tool_reads_is_passed_to_it_and_nothing_secret_besides() -> None:
    names = set(re.findall(r'os\.environ\.get\(\s*"([A-Z0-9_]+)"', SOURCE))
    assert {"DASHSCOPE_API_KEY", "QWEN_BASE_URL", "QWEN_USD_TO_JPY"} <= names
    for name in names:
        assert name.startswith(target.QWEN_ENV_PREFIXES) or name in target.CHILD_ENV_ALLOWED, (
            f"qwen_advisor.py が読む {name} は Lv3A から渡らない (接頭辞 {target.QWEN_ENV_PREFIXES})"
        )


def test_the_real_help_lists_role_reviewer_and_the_positional_input() -> None:
    pytest.importorskip("openai")
    done = subprocess.run(
        [sys.executable, str(TOOLS / "qwen_advisor.py"), "--help"], capture_output=True, text=True, encoding="utf-8",
        errors="replace", timeout=60, check=False,
    )
    assert done.returncode == 0, done.stderr
    assert "--role {coder,advisor,reviewer}" in done.stdout
    assert re.search(r"\[input\]", done.stdout) and "--no-usage" in done.stdout


def test_the_parser_reads_what_the_real_formatter_writes() -> None:
    """実物の整形関数の出力を、Lv3A の費用パーサに通す (書式が変わって費用が 0 円扱いになるのを防ぐ)。"""
    pytest.importorskip("openai")
    advisor = importlib.import_module("qwen_advisor")
    line = advisor._format_usage_line("今回", 12_345, 1_000, 2_000, 0.08, 150.0, " [model=qwen3-coder-plus]")
    assert target.qwen_usage(line) == (12_345 + 1_000 + 2_000, 12.0)
    assert target.qwen_usage(advisor._format_usage_line("累計", 1, 2, 3, 0.1, 150.0)) is None  # 累計の行は拾わない
    assert advisor.DEFAULT_BASE_URL == target.DEFAULT_QWEN_BASE_URL
    assert "reviewer" in advisor.ROLE_PROMPTS
