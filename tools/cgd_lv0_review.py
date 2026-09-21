"""Lv0A のレビュー段: 差分を束ねて Lv3A に渡し、重大指摘を 1 周だけ自動修正する。"""

from __future__ import annotations

import re
import time
import traceback
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

from cgd_lv5a_io import (
    DEFAULT_ROSTER,
    LV3A_OK,
    CliResult,
    Drivers,
    Lv0Run,
    Lv3aRun,
    cost_line,
    default_drivers,
    lv0_cost,
    lv3a_cost,
    parse_lv0,
    parse_lv3a,
    total_costs,
)
from cgd_lv5a_text import ADOPTED, RED, add_facts, add_review_target, cluster_block, fix_targets, same_title, technical

BUNDLE_MAX_BYTES = 50_000
MAX_BUNDLES = 3
SPEC_EXCERPT_CHARS = 8_000
PLAN_TIMEOUT = 300
FIX_ROUND_TIMEOUT = 3_000
FIX_PROCESS_TIMEOUT = 7_500
PATCH_HEAD_RE = re.compile(r"^--- a/(.*?)\n\+\+\+ b/.*?(?:\n|$)", re.MULTILINE)


@dataclass(frozen=True)
class PatchBundle:
    files: list[str]
    text: str

    @property
    def size(self) -> int:
        return len(self.text.encode("utf-8"))


@dataclass(frozen=True)
class BundleSplit:
    bundles: list[PatchBundle]
    unreviewed: list[str]


def write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="")


def clip(text: str, limit: int = 600) -> str:
    compact = " ".join(text.split())
    return compact if len(compact) <= limit else compact[: limit - 1] + "…"


def flag_args(no_ds: bool, no_qwen: bool = False) -> list[str]:
    return [*(["--no-ds"] if no_ds else []), *(["--no-qwen"] if no_qwen else [])]


def roster_args(roster: str) -> list[str]:
    return [] if roster == DEFAULT_ROSTER else ["--roster", roster]


def review_flags(roster: str, effort: str, no_ds: bool, no_qwen: bool) -> list[str]:
    return [*roster_args(roster), *(["--effort", effort] if roster == DEFAULT_ROSTER else []),
            *flag_args(no_ds, no_qwen)]


def rereview_flags(effort: str) -> list[str]:
    return ["--effort", effort, "--no-ds"]


def plan_flags(roster: str, no_ds: bool, no_qwen: bool) -> list[str]:
    return [*roster_args(roster), *flag_args(no_ds, no_qwen)]


def roster_problem(run: Lv3aRun, roster: str) -> str:
    if run.roster == roster or (roster == DEFAULT_ROSTER and not run.roster):
        return ""
    return (f"Lv3A が組 {run.roster or '(記録なし)'} で走った（要求: {roster}）。"
            "要求した組のレビューではないため使わない")


def preflight(
    runs_base: Path,
    roster: str,
    no_ds: bool,
    no_qwen: bool,
    drivers: Drivers | None = None,
) -> CliResult:
    """Lv3A の plan をダミー依頼文で呼ぶ。依頼文は作業フォルダの外に置く。"""
    scratch = runs_base / f"cgd_lv0a_plan_{time.time_ns()}"
    brief = scratch / "brief.md"
    write_text(brief, "# Lv0A 事前点検\n\n## 確認済みの事実\n- これは送信先と実行条件を確かめるダミーです\n")
    runner = drivers or default_drivers()
    try:
        return runner.lv3a(["plan", "--brief", str(brief), *plan_flags(roster, no_ds, no_qwen)], PLAN_TIMEOUT)
    except Exception as exc:
        return CliResult(1, "", f"Lv3A の実行器が例外: {exc!r}")


def split_patch(
    patch: str,
    max_bytes: int = BUNDLE_MAX_BYTES,
    max_bundles: int = MAX_BUNDLES,
) -> BundleSplit:
    """unified diff をファイル単位のまま上限以内の束へ順番どおりに詰める。"""
    matches = list(PATCH_HEAD_RE.finditer(patch))
    files = [(match.group(1), patch[match.start(): matches[i + 1].start() if i + 1 < len(matches) else len(patch)])
             for i, match in enumerate(matches)]
    bundles: list[PatchBundle] = []
    names: list[str] = []
    parts: list[str] = []
    size = 0
    unreviewed: list[str] = []
    for name, part in files:
        part_size = len(part.encode("utf-8"))
        if part_size > max_bytes:
            unreviewed.append(name)
            continue
        if parts and size + part_size > max_bytes:
            bundles.append(PatchBundle(names, "".join(parts)))
            names, parts, size = [], [], 0
        if len(bundles) >= max_bundles:
            unreviewed.append(name)
            continue
        names.append(name)
        parts.append(part)
        size += part_size
    if parts:
        if len(bundles) < max_bundles:
            bundles.append(PatchBundle(names, "".join(parts)))
        else:
            unreviewed.extend(names)
    return BundleSplit(bundles, unreviewed)


def _new_result(roster: str, review_dir: Path) -> dict[str, Any]:
    return {
        "performed": False,
        "reason": "",
        "roster": roster,
        "bundles": [],
        "autofix": {"rounds": 0, "targets": [], "resolved": 0, "remaining": [], "recurred": [],
                    "stop_reason": "", "base_run": "", "fix_plus": 0, "fix_minus": 0, "fix_files": []},
        "unreviewed": [],
        "costs": {},
        "cost_line": "",
        "record_dir": str(review_dir),
        "partial_notes": [],
        "needs_judgment": False,
        "judgment_reasons": [],
    }


def _call(
    drivers: Drivers,
    tool: str,
    argv: Sequence[str],
    timeout: int,
    logs: Path,
    seq: int,
    name: str,
) -> CliResult:
    runner = drivers.lv0 if tool == "lv0" else drivers.lv3a
    try:
        result = runner(list(argv), timeout)
    except Exception as exc:
        result = CliResult(1, "", f"実行器が例外: {exc!r}")
    write_text(
        logs / f"{seq:02d}_{name}.txt",
        f"# argv: {list(argv)}\n# exit: {result.returncode}\n\n"
        f"## stdout（抜粋）\n{result.stdout[-6_000:]}\n\n## stderr（抜粋）\n{result.stderr[-6_000:]}\n",
    )
    return result


def _checks_fact(results: Sequence[object]) -> str:
    labels = {"ok": "ok", "fail": "FAIL", "skip": "skip"}
    parts = [f"{getattr(item, 'name', '?')}={labels.get(str(getattr(item, 'status', '')), 'FAIL')}" for item in results]
    return "機械検査の結果: " + (" / ".join(parts) or "記録なし")


def _spec_excerpt(spec: str, spec_path: Path) -> str:
    if len(spec) <= SPEC_EXCERPT_CHARS:
        return spec
    return spec[:SPEC_EXCERPT_CHARS] + f"\n\n（仕様は長いためここまで。全文は {spec_path}）\n"


def _review_brief(
    cfg: object,
    out: object,
    bundle: PatchBundle,
    index: int,
    total: int,
    spec_path: Path,
    purpose: str,
    not_sent: Sequence[str] = (),
) -> str:
    overall = getattr(out, "overall")
    rounds = getattr(out, "rounds")
    manifest = getattr(cfg, "manifest")
    changed = [str(p) for key in ("modified", "added", "deleted") for p in overall.get(key, [])]
    hidden = [str(p) for key in ("untracked_changed", "notes") for p in overall.get(key, [])]
    facts = [
        _checks_fact(getattr(out, "results")),
        (f"Codex の周回数: {len(rounds)}。変更: 更新 {len(overall.get('modified', []))} / "
         f"新規 {len(overall.get('added', []))} / 削除 {len(overall.get('deleted', []))}、"
         f"+{overall.get('plus', 0)} -{overall.get('minus', 0)} 行"),
        "変更ファイル（最大 10）: " + (", ".join(changed[:10]) or "なし"),
        "差分に中身を載せていないファイル・変化: " + (" / ".join(hidden) or "なし"),
        "束の上限により自動レビューへ送らないファイル: " + (" / ".join(not_sent) or "なし"),
        "凍結ファイル: " + (" / ".join(getattr(manifest, "frozen")) or "なし"),
        f"この差分は全 {total} 束の第 {index} 束（ファイル: {', '.join(bundle.files)}）。ほかの束は別に審査される",
    ]
    brief = add_facts(_spec_excerpt(getattr(cfg, "spec"), spec_path), facts)
    return add_review_target(brief, purpose)


def _rereview_brief(cfg: object, fix_run: Lv0Run, spec_path: Path, titles: Sequence[str]) -> str:
    """自動修正の差分を確かめる再レビューの依頼文。事実は最初の実装ではなく、自動修正の run のもの。"""
    modified, added, deleted = fix_run.counts
    checks = " / ".join(f"{name}={status}" for name, status in fix_run.checks.items()) or "記録なし"
    facts = [
        "機械検査の結果（自動修正の run）: " + checks,
        (f"自動修正の Codex 周回数: {len(fix_run.rounds)}。変更: 更新 {modified} / 新規 {added} / 削除 {deleted}、"
         f"+{fix_run.plus} -{fix_run.minus} 行"),
        "変更ファイル（最大 10）: " + (", ".join(fix_run.changed[:10]) or "なし"),
        "この差分は、最初の実装（審査済み）への自動修正だけ。最初の実装の差分は含まれない",
        "凍結ファイル: " + (" / ".join(getattr(getattr(cfg, "manifest"), "frozen")) or "なし"),
    ]
    purpose = ("直前のレビューの 🔴（" + " / ".join(titles) + "）を自動修正した。"
               "目的は 🔴 が解消したかの確認と、修正で回帰を入れていないかの確認です。")
    return add_review_target(add_facts(_spec_excerpt(getattr(cfg, "spec"), spec_path), facts), purpose)


def _record_review(run: Lv3aRun, files: Sequence[str], reason: str, performed: bool) -> dict[str, Any]:
    clusters = run.clusters if performed else []
    return {
        "performed": performed,
        "reason": reason,
        "files": list(files),
        "run_dir": str(run.run_dir) if run.run_dir else "",
        "exit": run.exit_code,
        "reds": [{"title": str(c.get("title", "")), "adopt": str(c.get("adopt", "")),
                  "proposal": str(c.get("proposal", ""))} for c in technical(clusters, RED)],
        "oranges": [str(c.get("title", "")) for c in technical(clusters, "🟠")],
        "yellows": len(technical(clusters, "🟡")),
    }


def _review_one(
    drivers: Drivers,
    argv: list[str],
    root: Path,
    expected_roster: str,
    files: Sequence[str],
    timeout: int,
    logs: Path,
    seq: int,
    name: str,
) -> tuple[dict[str, Any], Lv3aRun]:
    called = _call(drivers, "lv3a", argv, timeout, logs, seq, name)
    run = parse_lv3a(called, root)
    if run.exit_code in LV3A_OK and (problem := roster_problem(run, expected_roster)):
        return _record_review(run, files, f"レビュー未実施（{problem}）", False), run
    if run.ok:
        reason = run.partial_note
        return _record_review(run, files, reason, True), run
    detail = clip(run.stderr or called.stdout or "Lv3A の run ディレクトリを特定できない")
    if run.exit_code == 1 and "秘匿" in detail:
        reason = ("レビュー未実施（Lv3A が秘匿候補で停止）。差分は自動レビューしていない。内容を確認し、"
                  "必要なら /lv3a を --redact 付きで個別に回す")
    else:
        reason = f"レビュー失敗（Lv3A exit {run.exit_code}: {detail}）"
    return _record_review(run, files, reason, False), run


def _fix_spec(spec: str, frozen: Sequence[str], runs: Sequence[Lv3aRun], targets: Sequence[Mapping[str, Any]]) -> str:
    blocks = [cluster_block(run, cluster) for run, cluster in zip(runs, targets)]
    return (
        "【自動レビューの 🔴 の修正】以下の指摘だけを直し、それ以外は変えない。"
        "テストを書き換えて通してはいけない。原因が仕様側にあるなら直さず最終報告で質問する。\n\n"
        "凍結ファイル: " + (" / ".join(frozen) or "なし") + "\n\n"
        "## 直す指摘\n" + "\n".join(blocks) + "\n\n【元の仕様】\n" + spec.strip() + "\n"
    )


def _finalize(result: dict[str, Any], costs: list[dict[str, Any]], problems: list[str]) -> dict[str, Any]:
    result["costs"] = total_costs([{"cost": cost} for cost in costs])
    result["cost_line"] = cost_line(result["costs"])
    result["performed"] = bool(result["bundles"]) and all(b["performed"] for b in result["bundles"])
    if problems:
        result["reason"] = " / ".join(problems)
    autofix = result["autofix"]
    final_reds = autofix["remaining"]
    result["needs_judgment"] = bool(problems or result["unreviewed"] or autofix["stop_reason"] or final_reds)
    reasons = list(problems)
    if autofix["stop_reason"]:
        reasons.append(autofix["stop_reason"])
    if final_reds:
        reds = [red for bundle in result["bundles"] for red in bundle["reds"]]
        if autofix["rounds"] == 0 and reds:  # 自動修正をしていないときは、統合者の採否も添える（見送られた 🔴 も人が判断する）
            adopted = sum(1 for red in reds if red["adopt"] in ADOPTED)
            reasons.append(f"🔴 が {len(final_reds)} 件残る（統合者: 採用/部分採用 {adopted}・見送り等 {len(reds) - adopted}）")
        else:
            reasons.append(f"🔴 が {len(final_reds)} 件残る")
    result["judgment_reasons"] = reasons if result["needs_judgment"] else []
    return result


def review_stage(
    cfg: object,
    out: object,
    *,
    checks_path: str,
    roster: str = DEFAULT_ROSTER,
    no_ds: bool = False,
    no_qwen: bool = False,
    no_autofix: bool = False,
    lv3a_timeout: int = 3_000,
    drivers: Drivers | None = None,
) -> dict[str, Any]:
    """Lv0A のレビュー段全体。どの例外も外へ投げず、要判断に変換する。"""
    review_dir: Path | None = None
    result = _new_result(roster, Path("."))
    costs: list[dict[str, Any]] = []
    problems: list[str] = []
    try:
        autodir = Path(getattr(out, "autodir"))
        review_dir = autodir / "review"
        result = _new_result(roster, review_dir)
        plus = int(getattr(out, "overall").get("plus", 0))
        minimum = int(getattr(cfg, "review_min_plus"))
        if plus < minimum:
            result.update(performed=False, reason=f"レビュー省略（+{plus} 行 < 閾値 {minimum}）")
            return _finalize(result, costs, [])
        runner = drivers or default_drivers()
        patch_path = autodir / "overall.patch"
        patch = patch_path.read_text(encoding="utf-8")
        split = split_patch(patch)
        result["unreviewed"] = list(split.unreviewed)
        if split.unreviewed:
            problems.append("未レビューのファイルがある")
        if not split.bundles:
            problems.append("レビュー未実施（レビューできる差分がない）")
            return _finalize(result, costs, problems)
        spec_path = autodir / "spec_r0.txt"
        target_pairs: list[tuple[Lv3aRun, Mapping[str, Any]]] = []
        partials: list[str] = []
        seq = 0
        for index, bundle in enumerate(split.bundles, start=1):
            bundle_path = review_dir / f"bundle_{index}.patch"
            brief_path = review_dir / f"brief_b{index}.md"
            root = review_dir / f"b{index}"
            write_text(bundle_path, bundle.text)
            write_text(brief_path, _review_brief(
                cfg, out, bundle, index, len(split.bundles), spec_path,
                "実装差分（unified diff）です。依頼どおり動くか、バグ・回帰・規約逸脱を評価してください。",
                split.unreviewed,
            ))
            seq += 1
            argv = ["run", "--brief", str(brief_path), "--files", str(bundle_path),
                    "--label", f"lv0-{getattr(out, 'base_run')}-b{index}", "--work-root", str(root),
                    *review_flags(roster, getattr(cfg, "effort"), no_ds, no_qwen)]
            record, run = _review_one(runner, argv, root, roster, bundle.files, lv3a_timeout,
                                      review_dir / "logs", seq, f"review_b{index}")
            result["bundles"].append(record)
            costs.append(lv3a_cost(run))
            if not record["performed"]:
                problems.append(record["reason"])
            if run.partial_note:
                partials.append(run.partial_note)
            for cluster in fix_targets(run.clusters) if record["performed"] else []:
                target_pairs.append((run, cluster))
        result["partial_notes"] = partials
        initial_reds = [red["title"] for bundle in result["bundles"] for red in bundle["reds"]]
        autofix = result["autofix"]
        autofix["remaining"] = list(initial_reds)
        if not target_pairs:
            return _finalize(result, costs, problems)
        autofix["targets"] = [str(cluster.get("title", "")) for _, cluster in target_pairs]
        if no_autofix:
            autofix["stop_reason"] = "--no-autofix のため自動修正を行わない"
            return _finalize(result, costs, problems)
        autofix["rounds"] = 1
        fix_spec = review_dir / "spec_fix.txt"
        write_text(fix_spec, _fix_spec(getattr(cfg, "spec"), getattr(cfg, "manifest").frozen,
                                       [run for run, _ in target_pairs], [cluster for _, cluster in target_pairs]))
        seq += 1
        fix_argv = ["run", "--workdir", str(getattr(cfg, "workdir")), "--checks", checks_path,
                    "--spec", str(fix_spec), "--effort", getattr(cfg, "effort"), "--max-fix-rounds", "1",
                    "--timeout", str(FIX_ROUND_TIMEOUT), "--review", "none"]
        fix_result = _call(runner, "lv0", fix_argv, FIX_PROCESS_TIMEOUT, review_dir / "logs", seq, "autofix")
        fix_run = parse_lv0(fix_result)
        costs.append(lv0_cost(fix_run))
        autofix["base_run"] = fix_run.base_run
        # レポートの「変更」は最初の実装だけなので、自動修正で増減した分は別に持つ（最終状態の大きさを誤解させない）
        autofix.update(fix_plus=fix_run.plus, fix_minus=fix_run.minus, fix_files=list(fix_run.changed[:10]))
        if fix_result.returncode != 0:
            autofix["stop_reason"] = (f"自動修正が失敗（Lv0 exit {fix_result.returncode}・基準 RUN "
                                      f"{fix_run.base_run or '不明'}）。戻すときは cgd_lv0_codex.py restore の確認のみが既定")
            return _finalize(result, costs, problems)
        if fix_run.patch is None:
            autofix["stop_reason"] = (f"自動修正の差分なし（基準 RUN {fix_run.base_run or '不明'}）。"
                                      "戻すときは cgd_lv0_codex.py restore の確認のみが既定")
            return _finalize(result, costs, problems)
        if fix_run.patch.stat().st_size > BUNDLE_MAX_BYTES:
            autofix["stop_reason"] = "再レビューを確認できない（自動修正の差分が 50,000 バイトを超える）"
            return _finalize(result, costs, problems)
        titles = autofix["targets"]
        rereview_files = fix_run.changed or ["（ファイル名不明）"]
        rereview_brief = review_dir / "brief_rereview.md"
        write_text(rereview_brief, _rereview_brief(cfg, fix_run, spec_path, titles))
        root = review_dir / "rereview"
        seq += 1
        argv = ["run", "--brief", str(rereview_brief), "--files", str(fix_run.patch),
                "--label", f"lv0-{getattr(out, 'base_run')}-rereview", "--work-root", str(root),
                *rereview_flags(getattr(cfg, "effort"))]
        rerecord, rerun = _review_one(runner, argv, root, DEFAULT_ROSTER, rereview_files, lv3a_timeout,
                                      review_dir / "logs", seq, "rereview")
        result["rereview"] = rerecord
        costs.append(lv3a_cost(rerun))
        if not rerecord["performed"]:
            autofix["stop_reason"] = "再レビューを確認できない: " + rerecord["reason"]
            return _finalize(result, costs, problems)
        remaining = [str(c.get("title", "")) for c in technical(rerun.clusters, RED)]
        autofix["remaining"] = remaining
        autofix["resolved"] = len(titles) if not remaining else max(len(titles) - len(remaining), 0)
        autofix["recurred"] = [title for title in remaining if any(same_title(title, old) for old in initial_reds)]
        if remaining:
            autofix["stop_reason"] = ("再レビューにも 🔴 が残る" +
                                      ("（同じ題名の 🔴 が再発）" if autofix["recurred"] else "") +
                                      "。2 周目は回さない")
        return _finalize(result, costs, problems)
    except Exception:
        if review_dir is not None:
            try:
                write_text(review_dir / "crash.txt", traceback.format_exc())
            except Exception:
                pass
        result["reason"] = "レビュー未実施（想定外の例外）"
        result["needs_judgment"] = True
        finalized = _finalize(result, costs, [result["reason"]])
        finalized["performed"] = False
        finalized["needs_judgment"] = True
        return finalized
