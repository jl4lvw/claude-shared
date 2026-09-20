"""秘匿候補の「伏字後に都度承認」のテスト。

既定は fail-closed (1 件でも当たれば何も送らず exit 1)。--redact を付けたときだけ、当たった位置から
行末までを `[伏字:<パターン名>]` に置換して続行する。承認はドライバでなく SKILL.md が Claude に課す。
"""

from __future__ import annotations

import itertools
import json
import sys
from pathlib import Path

import pytest

TOOLS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TOOLS))

import cgd_lv3a as target  # noqa: E402
from test_cgd_lv3a import (  # noqa: E402
    FakeIntegrator,
    FakeReviewRunner,
    brief_text,
    invoke,
    load_run,
    run_args,
    write,
)

# ダミーの鍵 (実在しない)。テストで「送信内容に残っていないこと」を確かめる目印
DUMMY_KEY = "sk-DUMMYDUMMYDUMMYDUMMY0123"
# secret_brief() で鍵の行が来る行番号 (brief_text の見出し 5 行 + 空行 1 行の次)
SECRET_LINE = 7
PEM_BEGIN = "-----BEGIN RSA PRIVATE KEY-----"
PEM_END = "-----END RSA PRIVATE KEY-----"


class CapturingRunner(FakeReviewRunner):
    """送信される依頼 (プロンプト) を全部記録するレビュアー。"""

    def __init__(self) -> None:
        super().__init__()
        self.prompts: list[str] = []

    def __call__(
        self,
        item: target.ReviewerSpec,
        prompt: str,
        cwd: Path,
        timeout: int,
        codex_path: str,
        tools: Path,
        effort: str,
    ) -> target.ExecResult:
        self.prompts.append(prompt)
        return super().__call__(item, prompt, cwd, timeout, codex_path, tools, effort)


def secret_brief(tmp_path: Path) -> Path:
    return write(tmp_path / "brief.md", brief_text(f'\napi_key = "{DUMMY_KEY}"\n- 続きの事実です\n'))


@pytest.mark.parametrize(
    ("name", "line", "kept"),
    [
        ("sk-key", "use sk-ABCDEFGHIJKL now", "use "),
        ("aws-access-key", "id AKIA1234567890AB rest", "id "),
        ("api-key", "api_key: hidden", ""),
        ("password", "password=hidden", ""),
        ("passwd", "passwd = hidden", ""),
        ("bearer", "Authorization: Bearer abcdefghij.12 tail", "Authorization: "),
        ("email", "contact person@example.com please", "contact "),
        ("jwt", "jwt eyJabcdefghijk.eyJabcdefghijk end", "jwt "),
        ("conn-string", "db postgres://user:pw@host/db end", "db "),
        ("aws-secret", "aws_secret_access_key = value", ""),
        ("secret", "secret: abcdefgh", ""),
        ("token", "token=abcdefghijklmnop", ""),
    ],
)
def test_each_pattern_is_replaced_from_the_hit_to_the_end_of_the_line(name: str, line: str, kept: str) -> None:
    redacted, found = target.redact_text("safe\n" + line + "\nafter\n")
    assert redacted == f"safe\n{kept}[伏字:{name}]\nafter\n"
    assert found == [(2, name)]
    assert target.secret_hits("x", redacted) == []


def test_every_pattern_has_a_readable_ascii_name() -> None:
    names = [item.name for item in target.SECRET_PATTERNS]
    assert len(names) == len(set(names))
    assert all(name.isascii() and name.replace("-", "").isalnum() for name in names)
    assert {
        "api-key", "password", "passwd", "secret", "token", "aws-secret", "private-key", "jwt",
        "conn-string", "bearer", "email", "sk-key", "aws-access-key",
    } <= set(names)


def test_a_line_with_several_hits_is_cut_at_the_leftmost_one() -> None:
    redacted, found = target.redact_text("mail a@example.com then password: hunter2\n")
    assert redacted == "mail [伏字:email]\n"
    assert found == [(1, "email")]
    redacted, found = target.redact_text('x password: hunter2 and a@example.com\n')
    assert redacted == "x [伏字:password]\n"
    assert found == [(1, "password")]


# パターンごとの当たる例 (秘密鍵は複数行なので別のテスト)
SAMPLES = {
    "sk-key": "sk-ABCDEFGHIJKL",
    "aws-access-key": "AKIA1234567890AB",
    "api-key": "api_key: v",
    "password": "password=v",
    "passwd": "passwd = v",
    "bearer": "Bearer abcdefghij.12",
    "email": "person@example.com",
    "jwt": "eyJabcdefghijk.eyJabcdefghijk",
    "conn-string": "postgres://user:pw@host/db",
    "aws-secret": "aws_secret_access_key = v",
    "secret": "secret: abcdefgh",
    "token": "token=abcdefghijklmnop",
}


def test_any_two_patterns_on_one_line_leave_nothing_behind() -> None:
    """どの 2 つが同じ行に並んでも、伏字後の再走査で当たりが残らない (fail-closed の前提)。"""
    assert set(SAMPLES) == {item.name for item in target.SECRET_PATTERNS} - {"private-key"}
    for first, second in itertools.permutations(SAMPLES, 2):
        line = f"x {SAMPLES[first]} y {SAMPLES[second]} z"
        redacted, found = target.redact_text(line + "\nnext\n")
        assert target.secret_hits("x", redacted) == [], (first, second)
        assert len(found) == 1 and found[0][0] == 1, (first, second)  # 1 行 = 1 か所 (いちばん左から行末まで)
        assert redacted.endswith("\nnext\n") and "z" not in redacted.splitlines()[0], (first, second)


def test_api_key_assignment_with_a_key_value_is_removed_whole() -> None:
    redacted, found = target.redact_text(f'api_key = "{DUMMY_KEY}"\n')
    assert DUMMY_KEY not in redacted
    assert found == [(1, "api-key")]  # sk-key も当たるが、いちばん左 (api_key) から行末まで 1 つ


def test_a_multiline_private_key_becomes_one_marker() -> None:
    text = f"before\nkey = {PEM_BEGIN}\nMIIEowIBAAKCAQEA\nabcdef\n{PEM_END}\nafter\n"
    redacted, found = target.redact_text(text)
    assert redacted == "before\nkey = [伏字:private-key]\nafter\n"
    assert found == [(2, "private-key")]
    assert "MIIEow" not in redacted


def test_a_private_key_without_end_runs_to_the_end_of_the_text() -> None:
    redacted, found = target.redact_text(f"before\n{PEM_BEGIN}\nMIIEowIBAAKCAQEA\nmore\n")
    assert redacted == "before\n[伏字:private-key]"
    assert found == [(2, "private-key")]


def test_text_after_the_end_marker_is_scanned_again() -> None:
    text = f"{PEM_BEGIN}\nbody\n{PEM_END} password: hunter2\nok\n"
    redacted, found = target.redact_text(text)
    assert redacted == "[伏字:private-key] [伏字:password]\nok\n"  # END と続きの間の空白は残る
    assert found == [(1, "private-key"), (3, "password")]


def test_a_private_key_swallowed_by_an_earlier_hit_on_the_same_line_is_still_removed() -> None:
    """先に別のパターンが当たった行の末尾に BEGIN があっても、鍵の本体 (どのパターンにも当たらない) を残さない。"""
    text = f"mail a@example.com {PEM_BEGIN}\nMIIEowIBAAKCAQEA\n{PEM_END}\nok\n"
    redacted, found = target.redact_text(text)
    assert redacted == "mail [伏字:email]\nok\n"
    assert "MIIEow" not in redacted
    assert found == [(1, "email"), (1, "private-key")]


def test_clean_text_is_returned_untouched_including_line_endings() -> None:
    text = "a\r\nb\rc\nlast without newline"
    assert target.redact_text(text) == (text, [])


def test_redaction_line_numbers_match_the_scanner() -> None:
    text = "one\npassword: a\nthree\nfour\ntoken=abcdefghijklmnop\n"
    _, found = target.redact_text(text)
    scanned = [int(hit.rsplit("行 ", 1)[1]) for hit in target.secret_hits("x", text)]
    assert [line for line, _ in found] == scanned == [2, 5]


def test_load_inputs_stops_without_redact_and_suggests_it(tmp_path: Path) -> None:
    brief = secret_brief(tmp_path)
    with pytest.raises(target.FrontError) as excinfo:
        target.load_inputs(brief, [])
    message = str(excinfo.value)
    assert "--redact" in message and "承認" in message
    assert f"行 {SECRET_LINE}" in message
    assert DUMMY_KEY not in message


def test_load_inputs_with_redact_returns_clean_text_and_positions(tmp_path: Path) -> None:
    brief = secret_brief(tmp_path)
    source = write(tmp_path / "app.py", f'ok = 1\nSECRET_TOKEN = "x"\npassword = "{DUMMY_KEY}"\n')
    inputs = target.load_inputs(brief, [source], redact=True)
    assert DUMMY_KEY not in inputs.brief and DUMMY_KEY not in inputs.files[0][1]
    assert "[伏字:api-key]" in inputs.brief
    assert "続きの事実です" in inputs.brief  # 当たった行の外は残る
    assert inputs.files[0][1] == "ok = 1\nSECRET_TOKEN = \"x\"\n[伏字:password]\n"
    assert [(item.file, item.line, item.pattern) for item in inputs.redactions] == [
        (str(brief), SECRET_LINE, "api-key"),
        (str(source), 3, "password"),
    ]
    assert target.scan_inputs(brief, inputs.brief, inputs.files) == []


def test_validate_inputs_keeps_its_strict_legacy_contract(tmp_path: Path) -> None:
    brief = secret_brief(tmp_path)
    with pytest.raises(target.FrontError):
        target.validate_inputs(brief, [])


def test_incomplete_redaction_stops(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """伏字後にもう一度走査し、当たりが残れば停止する (redact_text の取りこぼしを想定)。"""
    monkeypatch.setattr(target, "redact_text", lambda text: (text, []))
    with pytest.raises(target.FrontError, match="伏字が不完全") as excinfo:
        target.load_inputs(secret_brief(tmp_path), [], redact=True)
    assert DUMMY_KEY not in str(excinfo.value)


def test_redaction_that_removes_the_facts_heading_is_rejected(tmp_path: Path) -> None:
    """END の無い秘密鍵が見出しより前にあると、文末まで消えて見出しも消える。後で落ちる前に止める。"""
    brief = write(tmp_path / "brief.md", f"# 依頼\n{PEM_BEGIN}\nMIIEow\n\n## 確認済みの事実\n\n- 事実です\n")
    with pytest.raises(target.FrontError, match="確認済みの事実"):
        target.load_inputs(brief, [], redact=True)


def test_plan_without_redact_stops_with_line_numbers_only(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    code = invoke(["plan", "--brief", str(secret_brief(tmp_path))])
    captured = capsys.readouterr()
    assert code == 1
    assert f"行 {SECRET_LINE}" in captured.err
    assert "--redact" in captured.err
    assert DUMMY_KEY not in captured.err + captured.out


def test_plan_redact_previews_counts_and_lines_but_never_the_content(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    brief = secret_brief(tmp_path)
    source = write(tmp_path / "app.py", f'ok = 1\npassword = "{DUMMY_KEY}"\ntoken=abcdefghijklmnop\n')
    assert invoke(["plan", "--brief", str(brief), "--files", str(source), "--redact"]) == 0
    captured = capsys.readouterr()
    out = captured.out
    assert "伏字 3 件" in out
    assert "パターン別: api-key 1 件 / password 1 件 / token 1 件" in out
    assert f"- 依頼文: 行 {SECRET_LINE}" in out
    assert f"- {source}: 行 2, 3" in out
    assert "承認" in out
    assert "[伏字:api-key]" in out  # 依頼文の先頭 200 字は伏字後の本文 (送る内容そのもの)
    assert DUMMY_KEY not in out + captured.err
    assert "abcdefghijklmnop" not in out + captured.err


def test_plan_redact_without_hits_says_so(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    brief = write(tmp_path / "brief.md", brief_text())
    assert invoke(["plan", "--brief", str(brief), "--redact"]) == 0
    assert "伏字: 0 件" in capsys.readouterr().out


def test_plan_redact_still_stops_when_the_redaction_is_incomplete(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(target, "redact_text", lambda text: (text, []))
    assert invoke(["plan", "--brief", str(secret_brief(tmp_path)), "--redact"]) == 1
    assert "伏字が不完全" in capsys.readouterr().err


def test_run_without_redact_stops_before_creating_a_run_or_calling_anyone(tmp_path: Path) -> None:
    runner = CapturingRunner()
    work = tmp_path / "runs"
    assert invoke(run_args(secret_brief(tmp_path), work), runner) == 1
    assert not work.exists()
    assert runner.prompts == []


def test_run_redact_sends_no_secret_and_records_where_it_redacted(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    brief = secret_brief(tmp_path)
    runner = CapturingRunner()
    integrator = FakeIntegrator()
    work = tmp_path / "runs"
    assert invoke(run_args(brief, work, extra=("--redact",)), runner, integrator) == 0
    run_dir, state = load_run(work)
    # 送信内容 (レビュアー 4 者・統合者・review_input.txt) に鍵が残っていない
    assert len(runner.prompts) == 4
    assert all(DUMMY_KEY not in prompt and "[伏字:api-key]" in prompt for prompt in runner.prompts)
    assert all(DUMMY_KEY not in prompt for prompt in integrator.prompts)
    packed = (run_dir / "review_input.txt").read_text(encoding="utf-8")
    assert DUMMY_KEY not in packed and "[伏字:api-key]" in packed
    assert target.REDACTION_NOTE in packed
    # redaction.json は場所だけ (中身は入れない)
    raw = (run_dir / "redaction.json").read_text(encoding="utf-8")
    assert json.loads(raw) == [{"file": str(brief), "line": SECRET_LINE, "pattern": "api-key"}]
    assert DUMMY_KEY not in raw
    assert state["redactions"] == 1
    assert "伏字: 1 件（詳細は redaction.json）" in capsys.readouterr().out
    for produced in run_dir.iterdir():  # run ディレクトリのどこにも鍵は残らない
        assert DUMMY_KEY not in produced.read_text(encoding="utf-8", errors="replace"), produced.name


def test_run_redact_with_nothing_to_redact_changes_nothing(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    brief = write(tmp_path / "brief.md", brief_text())
    work = tmp_path / "runs"
    assert invoke(run_args(brief, work, extra=("--redact",))) == 0
    run_dir, state = load_run(work)
    assert not (run_dir / "redaction.json").exists()
    assert state["redactions"] == 0
    assert target.REDACTION_NOTE not in (run_dir / "review_input.txt").read_text(encoding="utf-8")
    assert "伏字" not in capsys.readouterr().out


def test_run_without_redact_records_zero_redactions(tmp_path: Path) -> None:
    work = tmp_path / "runs"
    assert invoke(run_args(write(tmp_path / "brief.md", brief_text()), work)) == 0
    run_dir, state = load_run(work)
    assert state["redactions"] == 0 and not (run_dir / "redaction.json").exists()


def test_partial_and_redaction_lines_coexist_in_the_report(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    runner = FakeReviewRunner(exit_codes={"ds_crit": 124})
    assert invoke(run_args(secret_brief(tmp_path), tmp_path / "runs", extra=("--redact",)), runner) == 20
    lines = capsys.readouterr().out.splitlines()
    status_index = next(i for i, line in enumerate(lines) if "状態: 暫定" in line)
    assert lines[status_index + 1].startswith("⚠ 暫定:")
    assert lines[status_index + 2] == "伏字: 1 件（詳細は redaction.json）"


@pytest.mark.parametrize("command", ["plan", "run"])
def test_redact_flag_is_accepted_by_both_subcommands(command: str) -> None:
    parser = target.build_parser()
    args = parser.parse_args([command, "--brief", "b.md", "--redact"])
    assert args.redact is True
    assert parser.parse_args([command, "--brief", "b.md"]).redact is False


def test_no_partial_flag_is_run_only() -> None:
    parser = target.build_parser()
    assert parser.parse_args(["run", "--brief", "b.md", "--no-partial"]).no_partial is True
    assert parser.parse_args(["run", "--brief", "b.md"]).no_partial is False
