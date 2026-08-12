"""cgd_logfilter のテスト.

守りたい性質は 4 つ:
    1. **指摘を落とさない** — 末尾（レビュアーの結論が出る場所）は必ず残る
    2. **加工していないときは素の cat と同じ**（余計な注記を足さない）
    3. **異常な行だけを刈る** — 通常行は 1 文字も変えない
    4. **壊れても cgd を壊さない** — 失敗時は非 0 で返り、呼び出し側の `|| cat` に落ちる
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

TOOLS = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(TOOLS))

import cgd_logfilter as F  # noqa: E402

SCRIPT = TOOLS / "cgd_logfilter.py"


def run(path: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(SCRIPT), str(path)],
        capture_output=True, text=True, encoding="utf-8",
    )


# --- 1. 通常のログは素通しする ------------------------------------------------

def test_short_log_is_passed_through_byte_for_byte(tmp_path: Path) -> None:
    """**バイト単位で**素の cat と一致すること。

    以前は `\\r\\n` を `\\n` に潰してから比較していたため、
    Windows のテキストモード stdout が改行を変換していることを隠していた
    (Lv6 で Codex と DS が収束して指摘)。潰さずに比べる。
    """
    body = "行1\n行2 — 日本語も含む\n🔴 重大な指摘\n"
    p = tmp_path / "raw.md"
    p.write_text(body, encoding="utf-8", newline="")
    r = subprocess.run([sys.executable, str(SCRIPT), str(p)], capture_output=True)
    assert r.returncode == 0
    assert r.stdout == body.encode("utf-8")


def test_log_without_trailing_newline_is_not_padded(tmp_path: Path) -> None:
    """改行で終わらない生ログに、勝手に改行を足さない。"""
    body = "最後の行に改行が無い"
    p = tmp_path / "raw.md"
    p.write_text(body, encoding="utf-8", newline="")
    r = subprocess.run([sys.executable, str(SCRIPT), str(p)], capture_output=True)
    assert r.stdout == body.encode("utf-8")


def test_no_notice_when_nothing_was_cut(tmp_path: Path) -> None:
    p = tmp_path / "raw.md"
    p.write_text("ふつうの出力\n", encoding="utf-8", newline="")
    assert "cgd_logfilter" not in run(p).stdout


# --- 2. 長い行だけを刈る ------------------------------------------------------

def test_long_line_is_truncated_but_others_untouched() -> None:
    keep = "🔴 これは残さないといけない指摘"
    lines, cut, chars = F.cap_lines(f"{keep}\n{'x' * 5000}\n{keep}\n", max_line=2000)
    assert cut == 1
    assert chars == 3000
    assert lines[0] == keep and lines[2] == keep      # 通常行は無改変
    assert lines[1].startswith("x" * 2000)
    assert "3000字を省略" in lines[1]


def test_boundary_line_exactly_at_limit_is_not_cut() -> None:
    lines, cut, _ = F.cap_lines("y" * 2000 + "\n", max_line=2000)
    assert cut == 0
    assert lines[0] == "y" * 2000


def test_truncation_keeps_valid_utf8() -> None:
    # 日本語を文字単位で切る。バイト単位で切ると壊れた文字が出る
    lines, _, _ = F.cap_lines("あ" * 3000 + "\n", max_line=10)
    assert lines[0].startswith("あ" * 10)
    lines[0].encode("utf-8")  # 例外が出なければ妥当な UTF-8


# --- 3. 総量超過では末尾を優先して残す ----------------------------------------

def test_tail_is_preserved_when_middle_is_dropped() -> None:
    body = [f"noise {i}" for i in range(20000)]
    body += ["🔴 最後に出る結論", "__末尾__"]
    out, dropped = F.cap_total(body, max_total=2000)
    assert dropped > 0
    assert out[-1] == "__末尾__"
    assert "🔴 最後に出る結論" in out
    assert out[0] == "noise 0"                        # 先頭も少しは残る
    assert any("中間" in x and "省略" in x for x in out)


def test_cap_total_is_noop_under_limit() -> None:
    body = ["a", "b", "c"]
    out, dropped = F.cap_total(body, max_total=10_000)
    assert out == body and dropped == 0


def test_tail_gets_more_budget_than_head() -> None:
    body = [f"line{i}" for i in range(5000)]
    out, _ = F.cap_total(body, max_total=3000)
    marker = next(i for i, x in enumerate(out) if "中間" in x)
    assert len(out) - marker - 1 > marker             # 末尾側のほうが厚い


# --- 4. 実ログ相当（巨大 1 行）で削減が効く -----------------------------------

def test_realistic_codex_noise_is_reduced(tmp_path: Path) -> None:
    # 実測した codex の症状: 190KB の JSON 1 行が 13 回、末尾に結論
    noise = "ERROR codex_models_manager: " + ("{json}" * 32000)
    body = "\n".join([noise] * 13 + ["🔴 結論: ここが本文", ""])
    p = tmp_path / "raw.md"
    p.write_text(body, encoding="utf-8", newline="")

    r = run(p)
    assert r.returncode == 0
    assert len(r.stdout) < len(body) * 0.05           # 95% 以上削れる
    assert "🔴 結論: ここが本文" in r.stdout           # 指摘は残る
    assert "この表示は要約版です" in r.stdout


# --- 5. 失敗経路 --------------------------------------------------------------

def test_missing_file_exits_nonzero(tmp_path: Path) -> None:
    r = run(tmp_path / "nope.md")
    assert r.returncode == F.EXIT_NO_FILE


def test_bad_usage_exits_nonzero() -> None:
    r = subprocess.run([sys.executable, str(SCRIPT)], capture_output=True, text=True)
    assert r.returncode == F.EXIT_USAGE


@pytest.mark.parametrize("bad", ["abc", "", "-1", "0", "3.5"])
def test_invalid_env_values_fall_back_to_defaults(bad: str, tmp_path: Path) -> None:
    """環境変数が不正でも import 時に落ちない。

    以前は `int(os.environ.get(...))` を素で書いており ValueError で即死した。
    `|| cat` で表示は救われるが、原因の分からない失敗が増える。
    """
    p = tmp_path / "raw.md"
    p.write_text("ふつうの出力\n", encoding="utf-8", newline="")
    env = {**os.environ, "CGD_LOG_MAX_LINE": bad, "CGD_LOG_MAX_TOTAL": bad}
    r = subprocess.run([sys.executable, str(SCRIPT), str(p)],
                       capture_output=True, text=True, encoding="utf-8", env=env)
    assert r.returncode == 0, r.stderr
    assert "ふつうの出力" in r.stdout


def test_undecodable_bytes_do_not_crash(tmp_path: Path) -> None:
    p = tmp_path / "raw.md"
    p.write_bytes(b"\xff\xfe not utf-8 \xc3\x28\n\xe6\x97\xa5\xe6\x9c\xac\n")
    r = run(p)
    assert r.returncode == 0
    assert "日本" in r.stdout


# --- 6. wrap() への組み込み（呼び出し側との契約） -----------------------------

def test_wrap_uses_filter_with_cat_fallback() -> None:
    import cgd_reviewers as R
    cmd = R.wrap("echo hi", "/c/tmp-ai/x/raw.md")
    assert "cgd_logfilter.py" in cmd
    # フォールバックが無いと、フィルタ不在の環境で本文が丸ごと消える
    assert '|| cat "$__cgd_out"' in cmd
    # rc の確定はフィルタより**前**でなければならない
    assert cmd.index("__cgd_rc=$?") < cmd.index("cgd_logfilter.py")


BASH = shutil.which("bash")


@pytest.mark.skipif(BASH is None, reason="bash が無い環境")
# `"` は Windows のファイル名に使えないので実パスでは検証できない。
# ここで確かめたいのは「シェルに解釈されずそのままパスとして届くか」なので、
# Windows 上で実在させられる危険文字だけを並べる。
@pytest.mark.parametrize("bad", ["a b", "a'b", "a$b", "a`b", "a$(touch pwned)b", "a;b", "a&b"])
def test_wrap_writes_to_the_exact_path_even_with_hostile_names(bad: str, tmp_path) -> None:
    """**実際にシェルへ食わせて**、意図したパスに書けることを確かめる。

    旧テストは `assert path in cmd or "'" in cmd` という形で、
    後半が trap の単一引用符で**常に真**だったため何も検証していなかった
    (Lv6 で Codex と DS が収束して指摘)。文字列を目視する代わりに評価する。
    """
    import cgd_reviewers as R
    raw = (tmp_path / bad / "raw.md").as_posix()
    cmd = R.wrap("echo HELLO", raw)

    r = subprocess.run([BASH, "-c", cmd], capture_output=True, text=True,
                       encoding="utf-8", cwd=tmp_path)
    assert r.returncode == 0, r.stderr

    written = tmp_path / bad / "raw.md"
    assert written.is_file(), f"意図したパスに書かれていない: {written}"
    assert "HELLO" in written.read_text(encoding="utf-8")
    assert (tmp_path / bad / "raw.md.exit").read_text(encoding="utf-8").strip() == "0"
    # コマンド置換が発火していれば、この副作用ファイルが残る
    assert not (tmp_path / "pwned").exists(), "パスの中身がシェルに解釈された"


@pytest.mark.skipif(BASH is None, reason="bash が無い環境")
def test_exit_marker_starts_a_line_even_without_trailing_newline(tmp_path) -> None:
    """生ログが改行で終わらなくても、マーカーは行頭から始まる。

    echo だと直前の行に連結し、agent が「最終行」を読み取れなくなる。
    """
    import cgd_reviewers as R
    raw = (tmp_path / "raw.md").as_posix()
    # printf は改行を付けないので、改行なしで終わる生ログになる
    cmd = R.wrap("printf 'no-trailing-newline'", raw)
    r = subprocess.run([BASH, "-c", cmd], capture_output=True, text=True,
                       encoding="utf-8", cwd=tmp_path)
    assert r.returncode == 0, r.stderr
    marker = [ln for ln in r.stdout.splitlines() if ln.startswith("__CGD_EXIT__=")]
    assert marker == ["__CGD_EXIT__=0"], r.stdout[-200:]


def test_long_multibyte_line_is_cut_on_character_boundary() -> None:
    """マルチバイトと ASCII が混在する極端に長い行でも UTF-8 が壊れない。"""
    line = ("日本語abc🔴" * 3000)
    lines, cut, _ = F.cap_lines(line + "\n", max_line=2000)
    assert cut == 1
    body = lines[0].encode("utf-8").decode("utf-8")   # 壊れていれば例外
    assert body.startswith("日本語abc🔴")


def test_tail_survives_even_when_last_line_exceeds_budget() -> None:
    """末尾行が予算より長くても、切り詰めて**必ず 1 行残す**。

    旧実装は予算に入らない行で即 break したため tail が空になり、
    「指摘が出る場所」そのものが消えた (3 者収束の 🔴)。
    """
    body = [f"noise {i}" for i in range(500)] + ["🔴" + "x" * 5000]
    out, dropped = F.cap_total(body, max_total=1000)
    assert dropped > 0
    assert out[-1].startswith("🔴")
    assert "字を省略" in out[-1]
