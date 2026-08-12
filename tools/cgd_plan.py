"""cgd_plan — cgd Lv6/7/8 の run を Python 側で持ち、成果物を機械判定する.

なぜ必要か (pv の設計理念を cgd へ持ち込む / 2026-08-12 ユーザー指示):
    cgd は Preflight（入力の実在確認）だけ Python に寄せてあるが、**レビュー結果の
    成否は今も agent の自己申告**になっている。executed / auth_error / raw_log_path は
    どれも agent が返す値で、WF はそれを検証していない。

    pv Lv3 の 4 者が揃って指摘した:
      - 「Preflight で排除した信頼を Review では許している」(信頼境界の非対称)
      - 「生ログの実在・非空を誰も検証していないので独立した証拠がゼロ」

    実害の形: codex がタイムアウトや deny で死んでも、agent が
    {executed:true, findings:[]} と返せば統合表に「指摘なし」と出る。
    生ログ (cgd_raw_*.md) が 1 バイトも無くても誰も気づけない。

    そこで **成果物の判定を Python に固定**する。判定材料は
    「ファイルが在るか・十分な大きさか・構造を持つか」だけで、LLM を通さない。

やらないこと (段階的導入・2026-08-12 時点):
    - 依頼テキストの生成は移していない。プロンプトは今も WF スクリプトが組み立てる
      (pv は build で生成している。ここまで移すかは次段階の判断)

使い方:
    # 1. run を登録し、期待する生ログのパスを確定させる (WF 起動前)
    python cgd_plan.py build --level 8 --label mytask \\
        --input C:/tmp-ai/in.txt --aux C:/tmp-ai/aux.txt

    # 2. Workflow を起動する (args は build が出力したものをそのまま渡す)

    # 3. **主 context が自分で叩く** — 唯一の非 LLM ゲート
    python cgd_plan.py collect --run <RUN>

    python cgd_plan.py doctor --run <RUN>    # どこで詰まったか
    python cgd_plan.py list                  # 未検証の run 一覧

終了コード: 0 = OK / 1 = 未達・不整合 / 2 = 引数エラー
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import random
import re
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from cgd_reviewers import build_reviewers  # noqa: E402

try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:  # pragma: no cover
    pass

# 置き場は環境変数で差し替えられる。テストが本番の run を汚さないようにするため
# (cgd_wf_gate の CGD_WF_GATE_DIR と同じ考え方)。通常運用では設定しない。
ROOT = Path(os.environ.get("CGD_PLAN_DIR", r"C:/tmp-ai/cgd"))
RAW_DIR = Path(os.environ.get("CGD_RAW_DIR", r"C:/tmp-ai"))   # WF が生ログを書く場所
PENDING_NAME = ".pending_verify"

EXIT_OK, EXIT_NG, EXIT_USAGE = 0, 1, 2

#: 生ログとして最低限これだけ無ければ「答えていない」とみなす。
#: pv の MIN_ANSWER_BYTES と同じ考え方。
MIN_RAW_BYTES = 200

#: レベルごとの参加者。WF スクリプトの reviewers 定義と対で維持すること。
REVIEWERS: dict[int, list[str]] = {
    6: ["codex", "deepseek", "qwen"],
    7: ["codex_med", "codex_high", "deepseek", "qwen"],
    8: ["codex_med", "codex_high", "deepseek", "qwen", "codex_critic", "deepseek_critic"],
}
GEMINI_AFTER = {6: 1, 7: 2, 8: 2}    # include_gemini 時に gemini を挿す位置


def sanitize_label(raw: str) -> str:
    """WF 側 (cgd_lv*_review.js) と **同じ規則** で label を正規化する。

    片方だけ変えると期待するパスと実際のパスがずれるので、規則を変えるときは
    必ず両方直すこと。
    """
    return re.sub(r"[^A-Za-z0-9_.-]", "_", str(raw))[:60] or "target"


def sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def run_dir(run: str) -> Path:
    return ROOT / run


def pending_path(run: str) -> Path:
    return run_dir(run) / PENDING_NAME


def expected_raw_paths(level: int, label: str, run_tag: str,
                       include_gemini: bool) -> dict[str, str]:
    """WF が書くはずの生ログのパスを、WF と同じ規則で組み立てる。

    WF 側は `cgd_raw_${r.name}_${label}_${_runTag}.md` を使う。
    _runTag は入力ファイルの sha256 先頭 8 文字なので、Python 側でも同じ値を出せる。
    """
    names = list(REVIEWERS[level])
    if include_gemini:
        names.insert(GEMINI_AFTER[level], "gemini")
    return {n: (RAW_DIR / f"cgd_raw_{n}_{label}_{run_tag}.md").as_posix() for n in names}


# --------------------------------------------------------------------- build


def cmd_build(args: argparse.Namespace) -> int:
    if args.level not in REVIEWERS:
        print(f"[cgd plan] Lv{args.level} は対象外です (対象: 6/7/8)", file=sys.stderr)
        return EXIT_USAGE

    inp = Path(args.input)
    if not inp.is_file():
        print(f"[cgd plan] 入力が見つかりません: {inp}", file=sys.stderr)
        return EXIT_USAGE
    aux = Path(args.aux) if args.aux else None
    if args.level in (7, 8) and aux is None:
        print("[cgd plan] Lv7/Lv8 は --aux が必須です", file=sys.stderr)
        return EXIT_USAGE
    if aux is not None and not aux.is_file():
        print(f"[cgd plan] aux が見つかりません: {aux}", file=sys.stderr)
        return EXIT_USAGE

    label = sanitize_label(args.label)
    # run_tag は WF 側と同じ「入力の sha256 先頭 8 文字」。
    # 同じ入力・同じ label の再実行では生ログが上書きされる点は WF と揃えてある。
    run_tag = sha256_of(inp)[:8]

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    run = f"{label}_{stamp}_{random.randint(0, 9999):04d}"
    d = run_dir(run)
    d.mkdir(parents=True, exist_ok=True)

    expected = expected_raw_paths(args.level, label, run_tag, args.include_gemini)
    plan = {
        "run": run,
        "level": args.level,
        "label": label,
        "label_given": args.label,
        "input_path": inp.as_posix(),
        "aux_input_path": aux.as_posix() if aux else None,
        "input_sha256": sha256_of(inp),
        "aux_sha256": sha256_of(aux) if aux else None,
        "run_tag": run_tag,
        "include_gemini": bool(args.include_gemini),
        "expected_raw": expected,
        "created_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }
    (d / "plan.json").write_text(json.dumps(plan, ensure_ascii=False, indent=1),
                                 encoding="utf-8", newline="")
    # 検証忘れの印。消せるのは collect が成功したときだけ。
    pending_path(run).write_text(run, encoding="utf-8", newline="")

    # レビュアー定義は **Python を単一の出所**にして args で渡す。
    # Workflow はファイルを読めないので、LLM を介さず届く経路は args だけ。
    # WF 側は受け取った定義を検証してから使い、無ければ内蔵定義に落ちる(後方互換)。
    reviewers = build_reviewers(
        args.level, plan["input_path"], plan["aux_input_path"],
        include_gemini=bool(args.include_gemini),
        reasoning=getattr(args, "codex_reasoning", None) or "medium",
    )
    plan["reviewers"] = reviewers
    (d / "plan.json").write_text(json.dumps(plan, ensure_ascii=False, indent=1),
                                 encoding="utf-8", newline="")

    wf_args = {
        "input_path": plan["input_path"],
        "label": label,
        "reviewers": reviewers,
    }
    if aux:
        wf_args["aux_input_path"] = plan["aux_input_path"]
    if args.include_gemini:
        wf_args["include_gemini"] = True

    print(json.dumps({"run": run, "level": args.level, "label": label,
                      "run_tag": run_tag, "reviewers": list(expected)},
                     ensure_ascii=False))
    print("WORKFLOW_ARGS " + json.dumps(wf_args, ensure_ascii=False))
    return EXIT_OK


# ------------------------------------------------------------------- collect


def _inspect_raw(path_str: str) -> dict:
    p = Path(path_str)
    info = {"path": path_str, "exists": False, "bytes": 0, "structure_lines": 0, "ok": False,
            "reason": ""}
    if not p.is_file():
        info["reason"] = "生ログが存在しない (レビュアーが実際には走っていない可能性)"
        return info
    try:
        data = p.read_bytes()
    except OSError as exc:
        info["exists"] = True
        info["reason"] = f"読めない: {exc}"
        return info
    info["exists"] = True
    info["bytes"] = len(data)
    text = data.decode("utf-8", errors="replace")
    # 「見出しか箇条書きが 3 行以上」を構造の下限とする (pv と同じ判定)。
    info["structure_lines"] = sum(
        1 for line in text.splitlines()
        if line.lstrip().startswith(("#", "-", "*", "|", "1.", "2.", "3."))
    )
    if info["bytes"] < MIN_RAW_BYTES:
        info["reason"] = f"短すぎる ({info['bytes']} < {MIN_RAW_BYTES} bytes)"
        return info
    if info["structure_lines"] < 3:
        info["reason"] = "構造が無い (見出し・箇条書きが 3 行未満)"
        return info
    info["ok"] = True
    return info


def cmd_collect(args: argparse.Namespace) -> int:
    d = run_dir(args.run)
    plan_file = d / "plan.json"
    if not plan_file.is_file():
        print(f"[cgd plan] plan.json がありません: {plan_file}", file=sys.stderr)
        return EXIT_NG
    plan = json.loads(plan_file.read_text(encoding="utf-8"))

    results = [_inspect_raw(p) for p in plan["expected_raw"].values()]
    names = list(plan["expected_raw"])
    for name, r in zip(names, results):
        r["reviewer"] = name
    ok = all(r["ok"] for r in results)

    print(json.dumps({"run": args.run, "level": plan["level"], "reviewers": results, "ok": ok},
                     ensure_ascii=False))

    if ok:
        # 印を消せるのはここだけ。WF 内から消してはいけない
        # (pv で「正常系ではリマインダーが一度も鳴らない」事故が起きている)。
        try:
            pending_path(args.run).unlink(missing_ok=True)
        except OSError:
            pass
        return EXIT_OK
    return EXIT_NG


# -------------------------------------------------------------------- doctor


def cmd_doctor(args: argparse.Namespace) -> int:
    d = run_dir(args.run)
    print(f"[cgd doctor] run={args.run}")
    print(f"  ディレクトリ : {d}  存在={d.is_dir()}")
    plan_file = d / "plan.json"
    print(f"  plan.json    : 存在={plan_file.is_file()}")
    if not plan_file.is_file():
        print("  → まず build を実行してください")
        return EXIT_NG
    plan = json.loads(plan_file.read_text(encoding="utf-8"))
    print(f"  レベル       : Lv{plan['level']} / label={plan['label']} / run_tag={plan['run_tag']}")
    print(f"  入力         : {plan['input_path']}")
    print(f"                 sha256={plan['input_sha256'][:16]}")
    if plan.get("aux_input_path"):
        print(f"  aux          : {plan['aux_input_path']}")
        print(f"                 sha256={(plan.get('aux_sha256') or '')[:16]}")
    print("  生ログ       :")
    ng = 0
    for name, p in plan["expected_raw"].items():
        r = _inspect_raw(p)
        mark = "OK" if r["ok"] else "NG"
        if not r["ok"]:
            ng += 1
        detail = f"{r['bytes']} bytes 構造{r['structure_lines']}行"
        if r["reason"]:
            detail += f" — {r['reason']}"
        print(f"    {mark} {name:<16} {detail}")
        if not r["ok"]:
            print(f"       {p}")
    print(f"  判定         : {'すべて揃っています' if ng == 0 else f'{ng} 件が未達'}")
    pend = pending_path(args.run)
    print(f"  Step検証     : {'⚠️ 未実施' if pend.exists() else '済'}")
    return EXIT_OK if ng == 0 else EXIT_NG


# ---------------------------------------------------------------------- list


def cmd_list(_args: argparse.Namespace) -> int:
    if not ROOT.is_dir():
        print("[cgd plan] run はありません")
        return EXIT_OK
    rows = []
    for pend in sorted(ROOT.glob(f"*/{PENDING_NAME}")):
        run = pend.parent.name
        plan_file = pend.parent / "plan.json"
        lv = "?"
        if plan_file.is_file():
            try:
                lv = json.loads(plan_file.read_text(encoding="utf-8"))["level"]
            except (OSError, ValueError, KeyError):
                pass
        rows.append((run, lv))
    if not rows:
        print("[cgd plan] 未検証の run はありません")
        return EXIT_OK
    print(f"[cgd plan] 未検証の run が {len(rows)} 件:")
    for run, lv in rows:
        print(f"  Lv{lv}  {run}")
        print(f"      python .claude/tools/cgd_plan.py collect --run {run}")
    return EXIT_OK


def main() -> int:
    parser = argparse.ArgumentParser(description="cgd の run を登録し成果物を機械判定する")
    sub = parser.add_subparsers(dest="command", required=True)

    p_build = sub.add_parser("build", help="run を登録し期待する生ログを確定する")
    p_build.add_argument("--level", type=int, required=True)
    p_build.add_argument("--label", required=True)
    p_build.add_argument("--input", required=True)
    p_build.add_argument("--aux")
    p_build.add_argument("--include-gemini", action="store_true")
    p_build.add_argument("--codex-reasoning", default="medium",
                         choices=["low", "medium", "high"],
                         help="Lv6 の codex reasoning (Lv7/Lv8 は med+high 固定)")

    for name, helptext in (("collect", "生ログの実在・サイズ・構造を判定する"),
                           ("doctor", "run の状態を表示する")):
        p = sub.add_parser(name, help=helptext)
        p.add_argument("--run", required=True)

    sub.add_parser("list", help="未検証の run を一覧する")

    args = parser.parse_args()
    return {"build": cmd_build, "collect": cmd_collect,
            "doctor": cmd_doctor, "list": cmd_list}[args.command](args)


if __name__ == "__main__":
    sys.exit(main())
