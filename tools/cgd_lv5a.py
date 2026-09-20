"""cgd Lv5A: 設計相談 → 実装 → 検証 → 再レビュー → 🔴 自動修正 1 周 を Codex 側で回す薄い調整役。

呼ぶのは CLI (cgd_lv0_auto.py = 実装 / cgd_lv3a.py = 2社x2視点レビュー) と、その出力ファイルだけ。
他ドライバの内部関数は import しない (2026-09-20: 関数名を推測して呼び、例外を握りつぶして
黙って別経路へ落ちた実害があったため。test_no_driver_imports がこれを守る)。
構成: cgd_lv5a.py (状態機械・サブコマンド) / cgd_lv5a_io.py (CLI 起動と出力ファイルの読取) / cgd_lv5a_text.py (文面)

  plan       承認表用の事前情報を出す (何も書かない)
  consult    設計案を Codex に書かせ → Lv3A でレビュー → 質問を出して exit 20 (ユーザーの回答待ち)
  implement  回答を検証し (G1 = 「この設計で実装する」のときだけ) 実装 → 差分レビュー → 🔴 自動修正(最大 1 周)
  status     状態を表示する (何も書かない)
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import random
import re
import shutil
import sys
import traceback
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping, Sequence

from cgd_lv5a_io import (
    EXIT_AWAITING, EXIT_CODEX_FAILED, EXIT_GENERIC, EXIT_NEEDS_JUDGMENT, EXIT_OK, EXIT_REVIEW_FAILED, LV3A_OK,
    REDACT_UNSUPPORTED, CliResult, Drivers, Lv0Run, Lv3aRun, cost_line, default_drivers, lv0_cost, lv3a_cost, map_lv0_exit,
    map_lv3a_exit, parse_lv0, parse_lv3a, total_costs)
from cgd_lv5a_text import (
    DESIGN_EXCERPT, FACTS_HEADING, G1_REDO, G1_YES, RED, add_facts, add_review_target, answer_lines, answers_fact, build_fix_spec,
    build_impl_spec, clip, cluster_block, consult_lines, design_spec, final_lines, fit_report, fix_targets, g1_question,
    g2_question, impl_targets, same_title, technical)

DEFAULT_WORK_ROOT = Path("C:/tmp-ai/cgd_lv5a")
P_CONSULTING, P_AWAIT_DIR, P_IMPLEMENTING = "consulting", "awaiting_direction", "implementing"
P_AWAIT_ACCEPT, P_CONSULT_FAILED, P_IMPL_FAILED = "awaiting_acceptance", "consult_failed", "implement_failed"
LABEL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,39}$")
DESIGN_ROUND_TIMEOUT, IMPL_ROUND_TIMEOUT, LV3A_TIMEOUT, PLAN_TIMEOUT = 1200, 3000, 3000, 300  # 秒


class DriverError(Exception):
    """前提の不備など。exit_code で終わる。"""

    def __init__(self, message: str, exit_code: int = EXIT_GENERIC) -> None:
        super().__init__(message)
        self.exit_code = exit_code


def write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="")


def write_json(path: Path, payload: Any) -> None:
    write_text(path, json.dumps(payload, ensure_ascii=False, indent=2) + "\n")


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def read_required_json(path: Path, what: str) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise DriverError(f"{what} を読めない: {path}（{exc}）") from exc


def now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def lv0_timeout(rounds: int, per_round: int, extra: int = 0) -> int:
    return rounds * (per_round + 300) + 900 + extra


# ---------------------------------------------------------------- state と工程の実行


@dataclass
class Session:
    run_dir: Path
    state: dict[str, Any]
    drivers: Drivers

    def save(self) -> None:
        self.state["updated"] = now_iso()
        self.state["costs"] = total_costs(self.state["stages"])
        write_json(self.run_dir / "state.json.tmp", self.state)
        os.replace(self.run_dir / "state.json.tmp", self.run_dir / "state.json")

    def phase(self, name: str, **extra: Any) -> None:
        self.state.update(phase=name, **extra)
        self.state["history"].append({"phase": name, "at": now_iso()})
        self.save()

    @property
    def last(self) -> dict[str, Any]:
        return self.state["stages"][-1]

    def call(self, name: str, tool: str, argv: Sequence[str], timeout: int) -> CliResult:
        """下請けを呼び、出力と終了コードを必ず残す (握りつぶさない)。"""
        runner = self.drivers.lv0 if tool == "lv0" else self.drivers.lv3a
        try:
            result = runner(list(argv), timeout)
        except Exception as exc:  # 実行器の失敗も記録して失敗として扱う (別経路へ黙って落ちない)
            result = CliResult(EXIT_GENERIC, "", f"実行器が例外: {exc!r}")
        seq = len(self.state["stages"]) + 1
        write_text(self.run_dir / "logs" / f"{seq:02d}_{name}.txt",
                   f"# argv: {list(argv)}\n# exit: {result.returncode}\n\n## stdout\n{result.stdout}\n\n## stderr\n{result.stderr}\n")
        self.state["stages"].append({
            "seq": seq, "name": name, "tool": tool, "argv": list(argv), "exit": result.returncode,
            "elapsed": round(result.elapsed, 1), "record_dir": None, "cost": {}, "notes": [],
            "stderr_tail": clip(result.stderr.strip(), 600) if result.returncode else ""})
        self.save()
        return result

    def lv0(self, name: str, argv: Sequence[str], timeout: int) -> Lv0Run:
        run = parse_lv0(self.call(name, "lv0", argv, timeout))
        self.last.update(cost=lv0_cost(run), status=run.status, notes=run.warnings,
                         record_dir=str(run.autodir) if run.autodir else None)
        self.save()
        return run

    def lv3a(self, name: str, argv: Sequence[str], work_root: Path) -> Lv3aRun:
        run = parse_lv3a(self.call(name, "lv3a", argv, LV3A_TIMEOUT), work_root)
        self.last.update(cost=lv3a_cost(run), notes=run.warnings, record_dir=str(run.run_dir) if run.run_dir else None)
        self.save()
        return run

    def fail(self, phase: str, exit_code: int, reason: str, detail: str = "") -> int:
        """失敗を理由つきで記録して exit する。detail は Codex の最終報告 (質問が返ってきた等) をそのまま見せる。"""
        self.phase(phase, failure={"exit": exit_code, "reason": reason, "detail": detail}, exit_code=exit_code)
        print(f"{phase}: {reason}（exit {exit_code}）\n記録: {self.run_dir}", file=sys.stderr)
        if detail:
            print("Codex の最終報告（抜粋）:\n" + detail, file=sys.stderr)
        return exit_code


def new_run_dir(root: Path, label: str, now: datetime | None = None) -> Path:
    stamp = (now or datetime.now()).strftime("%Y%m%d_%H%M%S")
    run_dir = root / f"{label}_{stamp}_{random.SystemRandom().randrange(0x1000000):06x}"
    run_dir.mkdir(parents=True, exist_ok=False)
    return run_dir


def load_state(run_dir: Path) -> dict[str, Any]:
    state = read_required_json(run_dir / "state.json", "state.json")
    if not isinstance(state, dict) or "phase" not in state:
        raise DriverError(f"state.json の形が不正: {run_dir}")
    return state


def lv0_argv(state: Mapping[str, Any], spec: Path, effort: str, fix_rounds: int, review: bool) -> list[str]:
    """実装系の Lv0 呼び出し。作業フォルダ・検査定義は必ず state の値 (全工程で同じ)。"""
    a = state["args"]
    argv = ["run", "--workdir", a["workdir"], "--checks", a["checks"], "--spec", str(spec), "--effort", effort,
            "--max-fix-rounds", str(fix_rounds), "--timeout", str(IMPL_ROUND_TIMEOUT)]
    return argv + (["--review", "deepseek"] if review else [])


# ---------------------------------------------------------------- plan / consult


def flag_args(no_ds: bool) -> list[str]:
    """Lv3A へ引き継ぐフラグ。--redact は引き継がない (Lv5A は受け付けない。REDACT_UNSUPPORTED)。"""
    return ["--no-ds"] if no_ds else []


def plan_calls(args: argparse.Namespace, drivers: Drivers) -> list[tuple[str, CliResult]]:
    lv0 = ["plan", "--workdir", args.workdir, "--checks", args.checks] + ([] if args.no_ds else ["--review", "deepseek"])
    lv3a = ["plan", "--brief", args.brief, *(["--files", *args.files] if args.files else []), *flag_args(args.no_ds)]
    return [("1. 実装側の点検（cgd_lv0_auto.py plan）", drivers.lv0(lv0, PLAN_TIMEOUT)),
            ("2. 設計レビュー側の点検（cgd_lv3a.py plan）", drivers.lv3a(lv3a, PLAN_TIMEOUT))]


def command_plan(args: argparse.Namespace, drivers: Drivers) -> int:
    check_common_inputs(args)  # consult が最初に止まる不備は、承認の前に見せる
    results = plan_calls(args, drivers)
    print("# Lv5A plan（何も書きません）")
    for title, result in results:
        print(f"\n## {title}  exit={result.returncode}\n{result.stdout.rstrip()}")
        if result.stderr.strip():
            print(result.stderr.rstrip(), file=sys.stderr)
    targets = "Codex（OpenAI）" + ("" if args.no_ds else "、DeepSeek（中国本土サーバ）")
    print("\n## 3. 流れと送信先\n"
          "- consult: 設計案（Codex が docs/lv5a_design_<label>.md に書く）→ Lv3A の 2社x2視点レビュー → 質問（G1）で停止\n"
          "- implement: Codex 実装+機械検査 → 差分レビュー → 🔴 の自動修正（最大 1 周）→ 最終質問（G2）\n"
          f"- 送信先: {targets}。作業フォルダの内容と依頼文が渡る。巻き戻せるのは Lv0 の写し（2MB 以下の通常ファイル）だけ\n"
          "- 作業フォルダに設計ファイル docs/lv5a_design_<label>.md が残る（削除しません）")
    return next((r.returncode for _, r in results if r.returncode != 0), EXIT_OK)


def check_common_inputs(args: argparse.Namespace) -> str:
    """費用をかける前に見られる不備を止める。依頼文の本文を返す。"""
    if args.redact:  # 何も呼ばず・何も書かず止める (plan も consult も同じ)
        raise DriverError(REDACT_UNSUPPORTED)
    try:
        text = Path(args.brief).read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise DriverError(f"依頼文を読めない: {args.brief}（{exc}）") from exc
    if FACTS_HEADING not in text:
        raise DriverError(f"依頼文に『{FACTS_HEADING}』の欄が要る（Lv3A が止まるため、先に直す）")
    for path in [args.checks, *args.files]:
        if not Path(path).is_file():
            raise DriverError(f"ファイルが無い: {path}")
    workdir = Path(args.workdir).resolve()
    if not workdir.is_dir():
        raise DriverError(f"作業フォルダが無い: {args.workdir}")
    if workdir in Path(args.checks).resolve().parents:
        raise DriverError("検査定義は作業フォルダの外に置く（Codex が書き換えられないように）")
    return text


def command_consult(args: argparse.Namespace, drivers: Drivers) -> int:
    if not LABEL_RE.match(args.label):
        raise DriverError("label は英数字で始まり、英数字・ピリオド・アンダースコア・ハイフンの 40 文字以内")
    brief_text = check_common_inputs(args)
    workdir, checks = Path(args.workdir).resolve(), Path(args.checks).resolve()
    design_rel = f"docs/lv5a_design_{args.label}.md"
    if (workdir / design_rel).exists():
        raise DriverError(f"設計ファイルが既にある: {workdir / design_rel}。label を変える（例 {args.label}-v2）。削除はしない")
    for title, result in plan_calls(args, drivers):  # 何も書かない点検。落ちるなら費用をかける前に止める
        if result.returncode != 0:
            print(f"## {title}  exit={result.returncode}\n{result.stdout.rstrip()}\n{result.stderr.rstrip()}", file=sys.stderr)
            return result.returncode
    run_dir = new_run_dir(Path(args.work_root), args.label)
    write_text(run_dir / "brief.md", brief_text)
    state = {"version": 1, "label": args.label, "run_dir": str(run_dir), "phase": P_CONSULTING, "created": now_iso(),
             "design_rel": design_rel, "history": [], "stages": [],
             "args": {"brief": str(Path(args.brief).resolve()), "workdir": str(workdir), "checks": str(checks),
                      "checks_sha256": sha256_file(checks), "files": [str(Path(f).resolve()) for f in args.files],
                      "effort": args.effort, "no_ds": args.no_ds}}
    sess = Session(run_dir, state, drivers)
    sess.phase(P_CONSULTING)
    try:
        return consult_body(sess, brief_text)
    except DriverError as exc:
        return sess.fail(P_CONSULT_FAILED, exc.exit_code, str(exc))
    except Exception as exc:  # 想定外も理由付きで記録して終える (握りつぶさない)
        sess.state["crash"] = traceback.format_exc(limit=8)
        return sess.fail(P_CONSULT_FAILED, EXIT_GENERIC, f"想定外の例外: {exc!r}")


def consult_body(sess: Session, brief_text: str) -> int:
    st, a, run_dir = sess.state, sess.state["args"], sess.run_dir
    design_rel, design_path = st["design_rel"], Path(a["workdir"]) / st["design_rel"]
    checks_design, spec = run_dir / "checks_design.json", run_dir / "spec_design.txt"
    write_json(checks_design, {"checks": [{"name": "lf", "type": "lf"}], "frozen": ["*"],
                               "note": "設計だけ: 既存ファイルはすべて凍結 (追加のみ可)"})
    write_text(spec, design_spec(brief_text, design_rel))
    run = sess.lv0("design", ["run", "--workdir", a["workdir"], "--checks", str(checks_design), "--spec", str(spec),
                              "--effort", a["effort"], "--max-fix-rounds", "1", "--timeout", str(DESIGN_ROUND_TIMEOUT)],
                   lv0_timeout(2, DESIGN_ROUND_TIMEOUT))
    if run.exit_code != 0:
        return sess.fail(P_CONSULT_FAILED, map_lv0_exit(run.exit_code),
                         f"設計の Codex 実行が失敗（Lv0 exit {run.exit_code}・{run.status or '状態不明'}: {run.note}）", run.codex_note)
    try:
        design_text = design_path.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        design_text = ""
    if not design_text.strip():
        return sess.fail(P_CONSULT_FAILED, EXIT_CODEX_FAILED, f"設計ファイルが作られていない（または空）: {design_path}")
    st["design_sha256"] = sha256_file(design_path)
    warnings = list(run.warnings)
    if others := [p for p in run.changed if p.replace("\\", "/") != design_rel]:
        warnings.append("設計以外の新規ファイルができた（要確認）: " + ", ".join(others[:5]))
    if run.frozen_violation:
        warnings.append("設計の Codex が既存ファイルを変更しようとして戻された: " + run.frozen_violation)
    brief_review = run_dir / "brief_review.md"
    write_text(brief_review, add_review_target(
        brief_text, f"これは実装前の**設計案**（{design_rel}）のレビュー依頼です。コードはまだありません。"
                    "設計の妥当性・抜け・リスク・依頼文との整合を評価してください。"))
    root = run_dir / "review_design"
    rv = sess.lv3a("design_review", ["run", "--brief", str(brief_review), "--files", str(design_path), *a["files"],
                                     "--label", f"{st['label']}-design", "--work-root", str(root), "--effort", a["effort"],
                                     *flag_args(a["no_ds"])], root)
    if rv.exit_code not in LV3A_OK:
        return sess.fail(P_CONSULT_FAILED, map_lv3a_exit(rv.exit_code),
                         f"設計レビュー（Lv3A）が失敗（exit {rv.exit_code}）: {clip(' '.join(rv.stderr.split()), 300)}")
    if rv.run_dir is None:
        return sess.fail(P_CONSULT_FAILED, EXIT_REVIEW_FAILED, "Lv3A の run ディレクトリを特定できない（唯一のサブフォルダが無い）")
    if any(q.get("id") == "G1" for q in rv.questions):
        return sess.fail(P_CONSULT_FAILED, EXIT_REVIEW_FAILED, "Lv3A の質問 ID が G1 と衝突した")
    warnings += rv.warnings
    if rv.partial_note:
        warnings.append(rv.partial_note)
    write_json(run_dir / "questions.json", [*rv.questions, g1_question()])
    st["consult"] = {"lv3a_exit": rv.exit_code, "review_root": str(root), "warnings": warnings}
    report = fit_report(lambda n_design, n_red: consult_lines(st, design_text, rv, n_design, n_red), [DESIGN_EXCERPT, 8], [5, 1])
    write_text(run_dir / "consult_report.md", report)
    sess.phase(P_AWAIT_DIR, exit_code=EXIT_AWAITING)
    print(report, end="")
    return EXIT_AWAITING


# ---------------------------------------------------------------- implement


def load_answers(path: Path, questions: Sequence[Mapping[str, Any]]) -> tuple[dict[str, str], str]:
    data = read_required_json(path, "回答ファイル")
    answers = data.get("answers") if isinstance(data, dict) else None
    if not isinstance(answers, dict):
        raise DriverError('回答ファイルは {"answers": {"G1": "<label>", ...}, "notes": "..."} の形にする')
    notes = data.get("notes", "")
    if not isinstance(notes, str):
        raise DriverError("notes は文字列にする")
    try:
        labels = {str(q["id"]): [str(o["label"]) for o in q["options"]] for q in questions}
    except (KeyError, TypeError) as exc:
        raise DriverError(f"questions.json の形が不正: {exc!r}") from exc
    for qid, label in answers.items():
        if qid not in labels:
            raise DriverError(f"存在しない質問 ID: {qid}（有効: {', '.join(labels)}）")
        if not isinstance(label, str) or label not in labels[qid]:
            raise DriverError(f"{qid} の回答が選択肢の label と完全一致しない: {label!r}（言い換えず、questions.json の文字列のまま）")
    if "G1" not in answers:
        raise DriverError("G1（この設計で実装に進みますか？）が未回答")
    return {str(k): str(v) for k, v in answers.items()}, notes


def verify_inputs(state: Mapping[str, Any]) -> Path:
    """consult のときと同じ作業フォルダ・検査定義・設計ファイルであることを確かめる。設計ファイルを返す。"""
    a = state["args"]
    if not Path(a["workdir"]).is_dir():
        raise DriverError(f"作業フォルダが無い: {a['workdir']}")
    if not Path(a["checks"]).is_file() or sha256_file(Path(a["checks"])) != a["checks_sha256"]:
        raise DriverError(f"検査定義が consult のときと違う（または無い）: {a['checks']}。同じ検査定義で実装するため止める")
    design = Path(a["workdir"]) / state["design_rel"]
    if not design.is_file() or sha256_file(design) != state.get("design_sha256"):
        raise DriverError(f"設計ファイルが consult のときと違う（または無い）: {design}。レビュー済みの設計と食い違うため止める")
    return design


def command_implement(args: argparse.Namespace, drivers: Drivers) -> int:
    run_dir = Path(args.run)
    state = load_state(run_dir)
    if state["phase"] != P_AWAIT_DIR:
        raise DriverError(f"implement は phase={P_AWAIT_DIR} でだけ呼べる（今は {state['phase']}）。"
                          "やり直すなら consult を新しい label で。status で状態を確認できる")
    questions = read_required_json(run_dir / "questions.json", "questions.json")
    answers, notes = load_answers(Path(args.answers), questions)
    if answers["G1"] != G1_YES:
        hint = ("依頼文を直して consult を新しい label でやり直す（設計ファイルと記録は残る）" if answers["G1"] == G1_REDO
                else "中止が選ばれた。この run は awaiting_direction のまま残る")
        raise DriverError(f"G1 が「{G1_YES}」ではないため実装しない: {answers['G1']}。{hint}")
    design_path = verify_inputs(state)
    sess = Session(run_dir, state, drivers)
    sess.phase(P_IMPLEMENTING, answers=answers, answers_notes=notes)
    try:
        return implement_body(sess, args, questions, answers, notes, design_path)
    except DriverError as exc:
        return sess.fail(P_IMPL_FAILED, exc.exit_code, str(exc))
    except Exception as exc:
        sess.state["crash"] = traceback.format_exc(limit=8)
        return sess.fail(P_IMPL_FAILED, EXIT_GENERIC, f"想定外の例外: {exc!r}")


@dataclass
class Review:
    performed: bool
    reason: str = ""
    run: Lv3aRun | None = None

    @property
    def clusters(self) -> list[dict[str, Any]]:
        return self.run.clusters if self.run else []

    def record(self) -> dict[str, Any]:
        return {"performed": self.performed, "reason": self.reason, "run_dir": str(self.run.run_dir) if self.run and self.run.run_dir else "",
                "reds": [{"title": c.get("title"), "adopt": c.get("adopt"), "proposal": c.get("proposal")}
                         for c in technical(self.clusters, RED)],
                "oranges": [str(c.get("title")) for c in technical(self.clusters, "🟠")],
                "yellows": len(technical(self.clusters, "🟡"))}


def diff_review(sess: Session, suffix: str, brief: str, facts: Sequence[str], patches: Sequence[Path],
                design_path: Path, effort: str, no_ds: bool) -> Review:
    """差分と設計ファイルを Lv3A に回す。--redact は付けない (実装差分の伏字はユーザー承認が取れないため)。"""
    name = f"review_{suffix}"
    root, brief_path = sess.run_dir / name, sess.run_dir / f"brief_{name}.md"
    write_text(brief_path, add_review_target(
        add_facts(brief, facts), "実装差分（unified diff）と、その設計ファイルです。実装コードとして評価してください。"))
    rv = sess.lv3a(name, ["run", "--brief", str(brief_path), "--files", *map(str, patches), str(design_path),
                          "--label", f"{sess.state['label']}-{suffix}", "--work-root", str(root), "--effort", effort,
                          *(["--no-ds"] if no_ds else [])], root)
    if rv.ok:
        return Review(True, ("暫定: " + rv.partial_note) if rv.partial_note else "", rv)
    detail = clip(" ".join(rv.stderr.split()), 300) or "Lv3A の run ディレクトリを特定できない"
    if rv.exit_code == 1:
        return Review(False, f"レビュー未実施（Lv3A が前段で停止: {detail}）。差分 {patches[0]} は自動レビューしていない。"
                             "内容を確認し、必要なら /lv3a で個別にレビューする（伏字が要るなら --redact 付き）", rv)
    return Review(False, f"レビュー失敗（Lv3A exit {rv.exit_code}: {detail}）", rv)


def check_text(run: Lv0Run) -> str:
    return " / ".join(f"{k}={v}" for k, v in run.checks.items()) or "(検査の内訳なし)"


def impl_record(run: Lv0Run) -> dict[str, Any]:
    return {"base_run": run.base_run, "autodir": str(run.autodir or ""), "status": run.status, "rounds": len(run.rounds),
            "tokens": run.tokens, "seconds": run.seconds, "counts": list(run.counts), "plus": run.plus,
            "minus": run.minus, "files": run.changed[:10], "checks": run.checks, "review_line": run.review_line}


def implement_body(sess: Session, args: argparse.Namespace, questions: Sequence[Mapping[str, Any]],
                   answers: Mapping[str, str], notes: str, design_path: Path) -> int:
    st, a, run_dir = sess.state, sess.state["args"], sess.run_dir
    effort, no_ds = args.effort or a["effort"], bool(a["no_ds"] or args.no_ds)
    brief = (run_dir / "brief.md").read_text(encoding="utf-8")
    checks = read_required_json(Path(a["checks"]), "検査定義")
    frozen = [p for p in (checks.get("frozen") if isinstance(checks, dict) else None) or [] if isinstance(p, str)]
    root = Path(st["consult"]["review_root"])
    design_review = parse_lv3a(CliResult(int(st["consult"]["lv3a_exit"])), root)
    if not design_review.ok:
        raise DriverError(f"consult の Lv3A 結果を読めない: {root}")
    blocks = [cluster_block(design_review, c) for c in impl_targets(design_review.clusters)]
    spec = run_dir / "spec_impl.txt"
    decided = answer_lines(questions, answers)
    write_text(spec, build_impl_spec(brief, st["design_rel"], blocks, decided, notes, frozen))
    impl = sess.lv0("impl", lv0_argv(st, spec, effort, args.max_fix_rounds, not no_ds),
                    lv0_timeout(args.max_fix_rounds + 1, IMPL_ROUND_TIMEOUT, 900))
    result: dict[str, Any] = {"impl": impl_record(impl)}
    st["result"] = result
    if impl.exit_code != 0:
        return sess.fail(P_IMPL_FAILED, map_lv0_exit(impl.exit_code),
                         f"実装の Codex 実行または検査が失敗（Lv0 exit {impl.exit_code}・{impl.status or '状態不明'}: {impl.note}）",
                         impl.codex_note)
    autofix: dict[str, Any] = {"rounds": 0, "targets": [], "resolved": 0, "remaining": [], "recurred": [], "stop_reason": ""}
    if impl.patch is None:
        review1 = Review(False, "レビュー未実施（Lv0 の overall.patch が無い）")
    else:
        shutil.copyfile(impl.patch, run_dir / "impl_overall.patch")
        mod, add, dele = impl.counts
        facts = [f"機械検査は全部合格した（{check_text(impl)}）",
                 f"Codex の周回数: {len(impl.rounds)}（初回+出し直し {max(len(impl.rounds) - 1, 0)}）。変更: 更新 {mod} / 新規 {add} / 削除 {dele}、+{impl.plus} -{impl.minus} 行",
                 "変更ファイル: " + (", ".join(impl.changed[:10]) or "(不明)"),
                 f"設計ファイル {st['design_rel']} は実装前に Lv3A でレビュー済みで、採用/部分採用の 🔴/🟠 は実装依頼に含めた",
                 answers_fact(decided)]
        review1 = diff_review(sess, "impl", brief, facts, [run_dir / "impl_overall.patch"], design_path, effort, no_ds)
    final = review1
    if review1.performed and fix_targets(review1.clusters):
        final = autofix_once(sess, autofix, review1, brief, design_path, effort, frozen, decided)
    result.update(autofix=autofix, review1=review1.record(), review2=final.record() if final is not review1 else None)
    result["needs"] = needs = (not final.performed) or bool(technical(final.clusters, RED)) or bool(autofix["stop_reason"])
    report = fit_report(lambda n_red, n_orange: final_lines(st, n_red, n_orange), [6, 4], [1, 0])
    write_text(run_dir / "final_report.md", report)
    write_json(run_dir / "questions_final.json", [g2_question(needs)])
    code = EXIT_NEEDS_JUDGMENT if needs else EXIT_OK
    sess.phase(P_AWAIT_ACCEPT, exit_code=code)
    print(report, end="")
    return code


def autofix_once(sess: Session, autofix: dict[str, Any], review1: Review, brief: str, design_path: Path,
                 effort: str, frozen: Sequence[str], decided: Sequence[str]) -> Review:
    """🔴 の自動修正。呼べるのは 1 回だけ (2 周目は構造的に回さない)。修正後の再レビュー結果を返す。"""
    if autofix["rounds"] >= 1 or review1.run is None:
        raise DriverError("自動修正は 1 周まで（2 周目は回さない）")
    st, run_dir = sess.state, sess.run_dir
    targets = fix_targets(review1.clusters)
    titles = [str(c.get("title")) for c in targets]
    autofix.update(rounds=1, targets=titles)
    spec = run_dir / "spec_fix.txt"
    write_text(spec, build_fix_spec(brief, st["design_rel"], [cluster_block(review1.run, c) for c in targets], frozen, decided))
    fix = sess.lv0("fix", lv0_argv(st, spec, effort, 1, False), lv0_timeout(2, IMPL_ROUND_TIMEOUT))
    st["result"]["fix"] = impl_record(fix)
    if fix.exit_code != 0:
        autofix["stop_reason"] = f"自動修正が失敗（Lv0 exit {fix.exit_code}・{fix.status or '状態不明'}: {fix.note}）。要ユーザー判断"
        return review1
    if fix.patch is None:
        autofix["stop_reason"] = "自動修正は完了したが差分（overall.patch）が無く再レビューできない。要ユーザー判断"
        return review1
    shutil.copyfile(fix.patch, run_dir / "fix_overall.patch")
    facts = ["直前のレビューの 🔴（" + " / ".join(titles) + "）を Codex に自動修正させた（1 周・機械検査は全部合格: " + check_text(fix) + "）",
             "差分は 2 つ。1 つ目が最初の実装、2 つ目が自動修正（1 つ目の上に適用）。再レビューの目的は 🔴 が解消したかの確認", answers_fact(decided)]
    review2 = diff_review(sess, "impl2", brief, facts, [run_dir / "impl_overall.patch", run_dir / "fix_overall.patch"],
                          design_path, effort, True)
    if not review2.performed:
        autofix["stop_reason"] = f"再レビューを確認できない: {review2.reason}。要ユーザー判断"
        return review2
    remaining = [str(c.get("title")) for c in technical(review2.clusters, RED)]
    first_reds = [str(c.get("title")) for c in technical(review1.clusters, RED)]
    # 題名は run ごとに言い回しが変わる (実走 3 回目: 同じ問題が別の題名で残った) ので、残りがあるときの「解消」は
    # 題名の照合ではなく件数で数える (下限)。再発の検出 (recurred) は従来どおり題名の類似で行う
    autofix.update(remaining=remaining, resolved=len(titles) if not remaining else max(len(titles) - len(remaining), 0),
                   recurred=[r for r in remaining if any(same_title(r, t) for t in first_reds)])
    if remaining:
        autofix["stop_reason"] = ("再レビューにも 🔴 が残る" + ("（同じ題名の 🔴 が再発）" if autofix["recurred"] else "")
                                  + "。2 周目は回さない。要ユーザー判断")
    return review2


# ---------------------------------------------------------------- status / main


def command_status(args: argparse.Namespace, drivers: Drivers) -> int:
    run_dir = Path(args.run)
    st = load_state(run_dir)
    print(f"phase: {st['phase']}（label={st.get('label')} / exit={st.get('exit_code', '-')}）\nrun: {run_dir}\n作業フォルダ: {st['args']['workdir']}")
    if st.get("failure"):
        print(f"失敗: {st['failure']['reason']}（exit {st['failure']['exit']}）")
    for s in st.get("stages", []):
        print(f"  {s['seq']:02d} {s['name']:<14} {s['tool']:<5} exit={s['exit']:<3} {s['elapsed']}秒  {s.get('record_dir') or ''}")
    for name in ("consult_report.md", "questions.json", "final_report.md", "questions_final.json"):
        print(f"  {'あり' if (run_dir / name).is_file() else 'なし'}  {name}")
    print(cost_line(total_costs(st.get("stages", []))))
    return EXIT_OK


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="cgd Lv5A: 設計相談→実装→検証→再レビュー→🔴自動修正 を Codex 側で回す")
    sub = parser.add_subparsers(dest="command", required=True)

    def inputs(sp: argparse.ArgumentParser) -> None:
        for flag in ("--brief", "--workdir", "--checks"):
            sp.add_argument(flag, required=True)
        sp.add_argument("--files", nargs="*", default=[])
        sp.add_argument("--no-ds", action="store_true")
        sp.add_argument("--redact", action="store_true", help="使えない（指定すると拒否する。設計段階に伏字が効かないため）")

    inputs(sub.add_parser("plan", help="承認用の事前情報（何も書かない）"))
    consult = sub.add_parser("consult", help="設計案→Lv3A レビュー→質問（exit 20）")
    inputs(consult)
    consult.add_argument("--label", required=True)
    consult.add_argument("--effort", choices=("medium", "high"), default="medium")
    consult.add_argument("--work-root", default=str(DEFAULT_WORK_ROOT))
    impl = sub.add_parser("implement", help="回答を検証して実装→差分レビュー→🔴自動修正")
    impl.add_argument("--run", required=True)
    impl.add_argument("--answers", required=True)
    impl.add_argument("--effort", choices=("medium", "high"), default=None)
    impl.add_argument("--max-fix-rounds", type=int, default=2, choices=range(0, 4))
    impl.add_argument("--no-ds", action="store_true")
    sub.add_parser("status", help="状態を表示（何も書かない）").add_argument("--run", required=True)
    return parser


def main(argv: Sequence[str] | None = None, *, drivers: Drivers | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        if callable(reconfigure := getattr(stream, "reconfigure", None)):
            reconfigure(encoding="utf-8", errors="replace")
    try:
        args = build_parser().parse_args(argv)
    except SystemExit as exc:
        return 0 if exc.code in (0, None) else EXIT_GENERIC
    handlers = {"plan": command_plan, "consult": command_consult, "implement": command_implement, "status": command_status}
    try:
        return handlers[args.command](args, drivers or default_drivers())
    except DriverError as exc:
        print(f"NG: {exc}", file=sys.stderr)
        return exc.exit_code


if __name__ == "__main__":
    raise SystemExit(main())
