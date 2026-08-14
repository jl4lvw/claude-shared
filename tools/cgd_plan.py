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
import secrets
import re
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from cgd_reviewers import build_reviewers  # noqa: E402
from cgd_prompts import review_prompt  # noqa: E402

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

#: 「見出しか箇条書きがこれだけあれば構造を持つ」の下限 (pv と同じ判定)。
#: v2 判定と v1 診断の両方から参照する。直値で二重管理すると片方だけ変えたときに
#: 判定が食い違う (2026-08-12 Lv7・codex_med 指摘)。
MIN_STRUCTURE_LINES = 3

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


def expected_raw_paths_v2(run: str, level: int, include_gemini: bool) -> dict[str, str]:
    """run ごとのディレクトリに分けた生ログのパス（並列実行の衝突対策）。

    旧 expected_raw_paths は `<label>_<run_tag>` で一意性を作っていたが、
    **同じ入力・同じ label で並行に回すと同じファイルを取り合う**。
    run 名には時刻と乱数が入っているので、こちらを使えば衝突しない。

    旧形式も残してあるのは、WF 内蔵定義（args を渡さない起動）が
    `cgd_raw_<name>_<label>_<runTag>.md` を使うため。契約テストはそちらを見る。
    """
    names = list(REVIEWERS[level])
    if include_gemini:
        names.insert(GEMINI_AFTER[level], "gemini")
    base = RAW_DIR / "cgd_runs" / run
    return {n: (base / f"{n}.md").as_posix() for n in names}


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
    # 同一秒に複数の run が立つ（並行セッション・連続実行）ので乱数を足す。
    # 4 桁だと同一秒で 1/10000 の衝突があり、実際にテストで踏んだ。
    # 衝突すると mkdir(exist_ok=True) が既存 run を再利用し、生ログを取り合う。
    run = f"{label}_{stamp}_{secrets.token_hex(4)}"
    d = run_dir(run)
    d.mkdir(parents=True, exist_ok=True)

    # 生ログは run ごとのディレクトリへ。並行実行しても取り合わない。
    # (WF 内蔵定義に落ちる起動では旧形式が使われるが、そちらは build を通らない)
    expected = expected_raw_paths_v2(run, args.level, args.include_gemini)
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
    # raw_paths を渡すと、各コマンドが「生ログと終了コードをシェルに書かせる」形に包まれる。
    # これで executed / exit_code が agent の自己申告ではなく実測値になる。
    reviewers = build_reviewers(
        args.level, plan["input_path"], plan["aux_input_path"],
        include_gemini=bool(args.include_gemini),
        reasoning=getattr(args, "codex_reasoning", None) or "medium",
        raw_paths=expected,
    )
    # 依頼テキストも Python で生成して同梱する。3 本の WF に複製されていた文面を
    # 1 か所へ寄せるのが目的（文面自体は元から決定論的だったので、揺らぎ低減ではない）。
    for r in reviewers:
        r["prompt"] = review_prompt(args.level, r, label)
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


def _parse_created_at(value) -> float | None:
    """plan.json の created_at を epoch 秒にする。読めなければ None（判定しない）。"""
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%d %H:%M:%S").timestamp()
    except ValueError:
        return None


def _mtime_after(path_str: str, since: float | None) -> bool:
    """ファイルが `since` 以降に更新されているか。

    since が取れない古い plan では判定できないので True を返す（従来どおり）。
    「判定できない」を「古い」に倒すと、正しい診断まで出なくなるため。
    """
    if since is None:
        return True
    try:
        # 秒精度の created_at と mtime を比べるので 1 秒の余裕を持たせる
        return Path(path_str).stat().st_mtime >= since - 1.0
    except OSError:
        return False


def _inspect_raw(path_str: str) -> dict:
    """生ログと、その隣の .exit（シェルが書いた終了コード）を検査する。

    2026-08-12 以降、生ログは **シェルがリダイレクトで書く**（以前は agent が
    Write ツールで転記していた）。終了コードも `echo $? > <raw>.exit` で
    シェルが残す。したがってここで見ているのは LLM 産の申告ではなく実測値。

    .exit が無い = コマンドが最後まで到達しなかった（シェルごと死んだ等）。
    ログだけあって .exit が無い状態は「途中で死んだ」なので通さない。
    """
    p = Path(path_str)
    info = {"path": path_str, "exists": False, "bytes": 0, "structure_lines": 0, "ok": False,
            "exit_code": None, "reason": ""}
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
    # シェルが書いた終了コードを読む。ここが executed の**唯一の権威**。
    ex = Path(path_str + ".exit")
    if ex.is_file():
        try:
            info["exit_code"] = int(ex.read_text(encoding="utf-8", errors="replace").strip())
        except (OSError, ValueError):
            info["reason"] = ".exit が読めない/数値でない (完走を確認できない)"
            return info
    else:
        info["reason"] = (
            ".exit が無い — コマンドが最後まで到達していない"
            "（途中で落ちた / 古い形式のコマンドで実行された）"
        )
        return info
    if info["exit_code"] != 0:
        # ラッパ (cgd_reviewers.wrap) が使う特別なコードは意味を添える。
        # 「なぜ落ちたか」が分からないと、環境の問題とレビューの問題を取り違える。
        special = {
            124: "タイムアウト/中断（コマンドが最後まで走らなかった。trap が初期値を残した）",
            90: "作業ディレクトリを作れなかった（環境の失敗であってレビューの失敗ではない）",
            127: "コマンドが見つからない（PATH / CLI 未インストール）",
        }
        note = special.get(info["exit_code"])
        info["reason"] = (f"終了コードが {info['exit_code']} (0 以外は失敗)"
                          + (f" — {note}" if note else ""))
        return info

    if info["bytes"] < MIN_RAW_BYTES:
        info["reason"] = f"短すぎる ({info['bytes']} < {MIN_RAW_BYTES} bytes)"
        return info
    if info["structure_lines"] < MIN_STRUCTURE_LINES:
        info["reason"] = f"構造が無い (見出し・箇条書きが {MIN_STRUCTURE_LINES} 行未満)"
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

    # WF 内蔵のレビュアー定義で走った場合（args に reviewers を渡さない起動）は、
    # 生ログが**旧形式のパス**に出る。expected_raw_paths_v2 の docstring が
    # 「旧形式も残してあるのは WF 内蔵定義がそれを使うため」と書いているとおり、
    # これは想定内の起動経路。にもかかわらず collect は新形式しか見ていなかったため、
    # **4 者が完走していても「レビュアーが実際には走っていない可能性」と誤報**していた
    # （2026-08-12 実際に踏んだ）。ゲートが狼少年になるのが一番まずいので、
    # 見つからなければ旧形式も見に行き、どちらで見つかったかを明示する。
    try:
        builtin = expected_raw_paths(
            plan["level"], plan["label"], plan["run_tag"],
            bool(plan.get("include_gemini", False)),
        )
    except (KeyError, TypeError):
        builtin = {}      # 古い plan.json。フォールバックはできないが collect は続ける

    # 見つかっても **合格にはしない**。旧形式の生ログは agent が Write ツールで
    # 保存したもので、隣に .exit（シェルが書いた終了コード）が無い。
    # .exit があることこそがこのゲートを「非 LLM」たらしめているので、
    # 弱い証拠で通すとゲートの意味が消える。
    # 変えるのは**診断の正確さ**だけ:「走っていない可能性」→「旧形式に在るが完走未確認」。
    # 旧形式のパスは `<name>_<label>_<runTag>` で、**同じ入力・同じ label なら
    # run をまたいで衝突する**。過去実行の残骸が転がっているだけで
    # 「今回走った証拠」に見えてしまうため、plan 作成時刻より古いログは採らない
    # （2026-08-12 Lv7: codex_high と DeepSeek が独立に指摘）。
    # それでも「今回走った」と断定はできないので、文言は候補どまりにする。
    started = _parse_created_at(plan.get("created_at"))

    results = []
    found_builtin: list[str] = []
    for name, p in plan["expected_raw"].items():
        r = _inspect_raw(p)
        if not r["ok"] and not r["exists"] and name in builtin:
            alt = _inspect_raw(builtin[name])
            fresh = _mtime_after(builtin[name], started)
            # しきい値は v2 判定と**同じ定数**を使う。直値で書くと後から片方だけ
            # 変えたときに v1 診断と v2 判定が食い違う（Lv7・codex_med 指摘）。
            if (alt["exists"] and alt["bytes"] >= MIN_RAW_BYTES
                    and alt["structure_lines"] >= MIN_STRUCTURE_LINES and fresh):
                found_builtin.append(name)
                r["builtin_log"] = {"path": builtin[name], "bytes": alt["bytes"],
                                    "structure_lines": alt["structure_lines"]}
                r["reason"] = (
                    f"登録先には無いが、**旧形式のパスに生ログの候補がある**"
                    f"（{alt['bytes']} bytes / plan 作成後に更新）。"
                    "ただし .exit が無いため**今回の実行かどうかも完走かも確認できない**。"
                    "WF に args.reviewers を渡さず内蔵定義で走った場合にこの形になる"
                )
        r["reviewer"] = name
        results.append(r)
    ok = all(r["ok"] for r in results)

    # **ここが成否の権威**であることを出力にも明示する。
    # WF の統合表に載る executed / exit_code は agent が JSON へ転記した値なので、
    # 食い違ったらこちらを採る（3 者レビューで「自己申告経路が残っている」と収束した点）。
    print(json.dumps({
        "run": args.run,
        "level": plan["level"],
        "reviewers": results,
        "ok": ok,
        "authority": (
            "executed/exit_code はここ（シェルが書いた .exit の実読値）が権威。"
            "WF の戻り値にある executed は agent の転記なので、食い違ったらこちらを採る"
        ),
        "failed": [r["reviewer"] for r in results if not r["ok"]],
        # 「存在した」だけで「今回走った」証明ではないので、名前も事実どおりにする
        # （2026-08-12 Lv7・DeepSeek 指摘: fell_back という語は断定が強すぎる）。
        "found_in_builtin_path": found_builtin,
    }, ensure_ascii=False))

    if found_builtin:
        print(
            f"[cgd plan] ⚠️ {len(found_builtin)} 者について、**旧形式のパスに生ログの候補**が"
            f"見つかりました: {', '.join(found_builtin)}\n"
            "  → WF に `args.reviewers` を渡さず内蔵定義で走ると、この形になります。\n"
            "  → ただし .exit が無いため、**今回の実行かどうかも完走かも機械的に確認できません**\n"
            "     （plan 作成後に更新されている、までしか言えません）。**ゲートは通しません**。\n"
            "     内容を使う場合は、上の path の生ログを人が中身まで確認してください。\n"
            "  → 次回は `cgd_plan.py build` が出力した WORKFLOW_ARGS の JSON を\n"
            "     **丸ごと**（prompt 本文も書き換えずに）args へ渡してください。",
            file=sys.stderr,
        )

    # **プロセス層の記録は collect が自動で行う。** 新しい手順を増やさないため
    # （記録を独立した手順にすると、cgd の usage log と同じで急ぐと飛ぶ）。
    # 失敗しても判定は変えない — 計測のためにゲートを落とすのは本末転倒。
    doc = _read_metrics(args.run)
    doc.update({
        "run": args.run,
        "level": plan.get("level"),
        "label": plan.get("label"),
        "collected_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "ok": ok,
        "reviewers": [
            {"name": r["reviewer"], "ok": r["ok"], "exit_code": r.get("exit_code"),
             "bytes": r.get("bytes"), "structure_lines": r.get("structure_lines")}
            for r in results
        ],
        "found_in_builtin_path": found_builtin,
    })
    _write_metrics(args.run, doc)

    if ok:
        # 印を消せるのはここだけ。WF 内から消してはいけない
        # (pv で「正常系ではリマインダーが一度も鳴らない」事故が起きている)。
        try:
            pending_path(args.run).unlink(missing_ok=True)
        except OSError:
            pass
        return EXIT_OK
    return EXIT_NG


# ------------------------------------------------------------------ metrics
#
# **なぜ run ディレクトリに置くか (2026-08-13)**
#
# pv Lv3 の 4 者が「Codex を medium + high で多重化する効果を一度も測っていない」
# 「測る仕組みも無い」で収束した。ただし『run metrics を記録する』を新しい手順として
# 足すと、cgd の usage log と同じ末路をたどる —— SKILL.md 自身が
# 「Claude が忘れずに叩く前提なので急いでいるときに飛ぶ」と書いている。
#
# そこで層を分け、**規律を増やさずに取れるところから取る**:
#
#   1. プロセス層 (どのレビュアーが走り、終了コードと生ログのサイズがどうだったか)
#      → `collect` が **自動で**書く。collect は既に必須なので新しい手順は増えない
#   2. findings 層 (指摘の出所と severity の内訳)
#      → `record-merge` に WF の戻り値を **標準入力**で渡す。
#         引数に載せると引用とサイズで壊れるため stdin にした
#   3. 採用層 (どの指摘を実際に直したか)
#      → **まだ記録しない。** 人の判断が要るので、1 と 2 が役に立つと分かる前に
#         規律を発明しない。必要になった時点で、印が消える唯一の場所へ寄せる
#
# 集計は `metrics` サブコマンドが run ディレクトリを走査して行う。
# 「採用数」が無いので、いま答えられるのは「**high 単独で挙がった**重大指摘の率」まで。
# 「**採用された**率」ではないことを出力にも明記する。


METRICS_NAME = "metrics.json"


def metrics_path(run: str) -> Path:
    return run_dir(run) / METRICS_NAME


def _write_metrics(run: str, doc: dict) -> None:
    """metrics.json を書く。**失敗しても collect の判定は変えない。**

    計測のために本来のゲートを落とすのは本末転倒。
    書けなかった事実だけ stderr に出す。
    """
    try:
        p = metrics_path(run)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(doc, ensure_ascii=False, indent=1),
                     encoding="utf-8", newline="")
    except OSError as exc:
        print(f"[cgd plan] WARN: metrics を書けませんでした: {exc}", file=sys.stderr)


def _read_metrics(run: str) -> dict:
    p = metrics_path(run)
    if not p.is_file():
        return {}
    try:
        doc = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return doc if isinstance(doc, dict) else {}


def _severity_counts(findings) -> dict[str, int]:
    out = {"🔴": 0, "🟠": 0, "🟡": 0, "other": 0}
    for f in findings or []:
        sev = (f or {}).get("severity") if isinstance(f, dict) else None
        out[sev if sev in out else "other"] += 1
    return out


def summarize_merge(result: dict) -> dict:
    """WF の戻り値から findings 層の内訳を作る。

    出所の分類は WF の統合スキーマがそのまま持っている:
      convergent        … codex_med と codex_high の両方
      codex_divergent   … どちらか片方のみ (source に med/high が入る)
      aux_only          … 補助 (DS/Qwen) のみ
    """
    conv = result.get("convergent_findings") or []
    div = result.get("codex_divergent_findings") or result.get("divergent_findings") or []
    aux = result.get("aux_only_findings") or []

    by_source = {"med_only": [], "high_only": [], "unknown_single": []}
    for f in div:
        src = str((f or {}).get("source", "")) if isinstance(f, dict) else ""
        low = src.lower()
        if "high" in low:
            by_source["high_only"].append(f)
        elif "med" in low:
            by_source["med_only"].append(f)
        else:
            by_source["unknown_single"].append(f)

    return {
        "merge_model_used": result.get("merge_model_used"),
        "merge_fallback_fired": result.get("merge_fallback_fired"),
        "reviewers_source": result.get("reviewers_source"),
        "participants": result.get("participants"),
        "counts": {
            "convergent": _severity_counts(conv),
            "codex_high_only": _severity_counts(by_source["high_only"]),
            "codex_med_only": _severity_counts(by_source["med_only"]),
            "codex_single_unknown": _severity_counts(by_source["unknown_single"]),
            "aux_only": _severity_counts(aux),
        },
        "note": (
            "これは『挙がった』件数であって『採用された』件数ではない。"
            "採用層はまだ記録していない"
        ),
    }


# --------------------------------------------------- findings の位置照合
#
# pv Lv3 の反証担当が「location と hunk 範囲の照合は決定論的に実装できるのに
# 未実装なだけ」と指摘した点への対応。差分パースと範囲判定は pv_review.py に
# 純関数として既にあり 56 件のテストが付いているので、**作り直さず再利用する**。
#
# **これはゲートではない。** cgd の Codex は read-only sandbox で周辺ファイルを
# 読める設計なので、「差分に無いファイルへの指摘」は pv と違って正当でありうる。
# 幻覚と断定できないものを弾くと、10 件の正当な指摘を 1 件の誤判定で捨てる。
# ここでやるのは **内訳を出すこと**だけで、合否は変えない。
#
# 照合できなかったこと自体も必ず出す。差分が見つからないのに件数だけ 0 と出すと
# 「問題なし」と誤読される —— 検証していないことを「合格」と表示しないのが要点。

_LOCATION_RE = re.compile(r"^(?P<path>.+?):(?P<line>\d+)(?:-(?P<end>\d+))?\s*$")


def _parse_location(location) -> tuple[str | None, int | None]:
    """`path/to/file.py:123` を (パス, 行) に割る。行が無ければ (パス, None)。"""
    if not isinstance(location, str):
        return None, None
    text = location.strip()
    if not text:
        return None, None
    m = _LOCATION_RE.match(text)
    if m:
        try:
            return m.group("path").strip(), int(m.group("line"))
        except ValueError:
            return m.group("path").strip(), None
    return text, None


def _in_any_hunk(line: int, hunks) -> bool:
    return any(start <= line <= end for start, end in (hunks or []))


def locate_findings(input_text: str, findings) -> dict:
    """findings の location を差分に照らして分類する。**合否は判定しない。**

    分類:
      in_diff             … 差分に在るファイルで、行が hunk の範囲内
      out_of_diff         … 差分に在るファイルだが、行が hunk の外
                            （呼び出し元への言及など。**正当なので問題視しない**）
      file_not_in_diff    … 差分に無いファイル。cgd では Codex が周辺を読むので
                            **これ自体は異常ではない**。ただし件数が多ければ
                            「対象を外れたレビューになっていないか」の手がかりになる
      no_line             … ファイル名だけで行が無い
      unparsed            … location が無い / 解釈できない
    """
    try:
        import pv_review
    except ImportError as exc:                      # pragma: no cover - 環境依存
        return {"checked": False, "reason": f"pv_review を読み込めません: {exc}"}

    index = pv_review.parse_unified_diff(input_text or "")
    if not index.files:
        # **「差分が無い」と「指摘が全部妥当」を混同させない。**
        return {
            "checked": False,
            "reason": "入力から unified diff を取り出せませんでした（差分を含まない入力）",
            "findings": len(findings or []),
        }

    buckets = {"in_diff": 0, "out_of_diff": 0, "file_not_in_diff": 0,
               "no_line": 0, "unparsed": 0}
    outside: list[str] = []
    for f in findings or []:
        if not isinstance(f, dict):
            buckets["unparsed"] += 1
            continue
        path, line = _parse_location(f.get("location"))
        if not path:
            buckets["unparsed"] += 1
            continue
        entry = index.find(path)
        if entry is None:
            buckets["file_not_in_diff"] += 1
            if path not in outside:
                outside.append(path)
            continue
        if line is None:
            buckets["no_line"] += 1
            continue
        if _in_any_hunk(line, entry.new_hunks) or _in_any_hunk(line, entry.old_hunks):
            buckets["in_diff"] += 1
        else:
            buckets["out_of_diff"] += 1

    return {
        "checked": True,
        "diff_files": len(index.files),
        "counts": buckets,
        "files_outside_diff": outside[:10],
        "note": (
            "これは位置の内訳であって指摘の正しさではない。"
            "cgd の Codex は周辺ファイルを読めるので file_not_in_diff は異常ではない"
        ),
    }


def cmd_record_merge(args: argparse.Namespace) -> int:
    """WF の戻り値 (JSON) を標準入力から受け取り、metrics.json へ足す。"""
    raw = sys.stdin.buffer.read()
    if not raw.strip():
        print("[cgd plan] 標準入力が空です。WF の戻り値 JSON を渡してください",
              file=sys.stderr)
        return EXIT_USAGE
    try:
        result = json.loads(raw.decode("utf-8", errors="replace"))
    except ValueError as exc:
        print(f"[cgd plan] 標準入力を JSON として解釈できません: {exc}", file=sys.stderr)
        return EXIT_USAGE
    if not isinstance(result, dict):
        print(f"[cgd plan] JSON が object ではありません: {type(result).__name__}",
              file=sys.stderr)
        return EXIT_USAGE

    if not run_dir(args.run).is_dir():
        print(f"[cgd plan] run が見つかりません: {args.run}", file=sys.stderr)
        return EXIT_NG

    doc = _read_metrics(args.run)
    doc["run"] = args.run
    doc["merge"] = summarize_merge(result)

    # 位置の内訳も残す。**合否は変えない**（cgd の Codex は周辺ファイルを読むので
    # 差分外の指摘を幻覚と断定できない）。判断材料を増やすだけ。
    plan_file = run_dir(args.run) / "plan.json"
    try:
        plan = json.loads(plan_file.read_text(encoding="utf-8"))
        src = Path(plan["input_path"]).read_text(encoding="utf-8", errors="replace")
    except (OSError, ValueError, KeyError) as exc:
        doc["locations"] = {"checked": False, "reason": f"入力を読めません: {exc}"}
    else:
        merged_findings = []
        for key in ("convergent_findings", "codex_divergent_findings",
                    "divergent_findings", "aux_only_findings"):
            merged_findings.extend(result.get(key) or [])
        doc["locations"] = locate_findings(src, merged_findings)
    doc["merge_recorded_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    _write_metrics(args.run, doc)
    print(json.dumps({"run": args.run, "merge": doc["merge"],
                      "locations": doc.get("locations")}, ensure_ascii=False))
    return EXIT_OK


def cmd_metrics(args: argparse.Namespace) -> int:
    """run ディレクトリを走査して集計する。"""
    rows: list[dict] = []
    if ROOT.is_dir():
        for d in sorted(ROOT.iterdir()):
            doc = _read_metrics(d.name) if d.is_dir() else {}
            if not doc:
                continue
            if args.since and str(doc.get("collected_at") or "") < args.since:
                continue
            rows.append(doc)

    if not rows:
        print("[cgd plan] metrics がまだありません（collect を通した run が対象）")
        return EXIT_OK

    def _sev(doc: dict, key: str, sev: str) -> int:
        return (((doc.get("merge") or {}).get("counts") or {}).get(key) or {}).get(sev, 0)

    print(f"[cgd plan] metrics — {len(rows)} run")
    # 見出しは ASCII に寄せる。全角は端末で 2 桁ぶん占めるので、
    # Python の桁揃え（文字数基準）と必ずずれて読みにくくなる。
    print(f"  {'run':<40} {'Lv':>2} {'ok':>3} {'conv':>5} {'high':>5}"
          f" {'med':>5} {'?':>4} {'aux':>5}   (数字は 🔴 のみ)")
    # **codex_single_unknown を落とさない。** source が付かない指摘を合計から外すと
    # 内訳の合計が静かに減り、割合が実態より良く見える。
    # 「unknown が多い」こと自体が「merge が source を書いていない」という信号。
    tot = {"convergent": 0, "codex_high_only": 0, "codex_med_only": 0,
           "codex_single_unknown": 0, "aux_only": 0}
    merged_runs = 0
    for doc in rows:
        has_merge = bool(doc.get("merge"))
        if has_merge:
            merged_runs += 1
            for k in tot:
                tot[k] += _sev(doc, k, "🔴")
        cells = [str(doc.get('run'))[:40].ljust(40),
                 str(doc.get('level', '?')).rjust(2),
                 ('OK' if doc.get('ok') else 'NG').rjust(3)]
        for key in ('convergent', 'codex_high_only', 'codex_med_only',
                    'codex_single_unknown', 'aux_only'):
            width = 4 if key == 'codex_single_unknown' else 5
            cells.append(str(_sev(doc, key, '🔴') if has_merge else '-').rjust(width))
        print('  ' + ' '.join(cells))

    print(f"\n  merge を記録済みの run: {merged_runs} / {len(rows)}")
    if merged_runs:
        total_red = sum(tot.values())
        print(f"  🔴 の内訳 (合計 {total_red} 件):")
        for k, v in tot.items():
            pct = f"{100 * v / total_red:.0f}%" if total_red else "-"
            print(f"    {k:<18} {v:>4} ({pct})")
        print("\n  ※ これは『挙がった』件数。**『採用された』件数ではない**ので、")
        print("     Codex 多重化の費用対効果をこの表だけで結論づけないこと。")
    if merged_runs < len(rows):
        print(f"\n  {len(rows) - merged_runs} run は merge 未記録です:")
        print("    WF の戻り値を渡すと findings の内訳まで取れます:")
        print('    <戻り値のJSON> | python cgd_plan.py record-merge --run <RUN>')
    return EXIT_OK


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

    p_rec = sub.add_parser("record-merge",
                           help="WF の戻り値(JSON)を標準入力から受け取り metrics に足す")
    p_rec.add_argument("--run", required=True)

    p_met = sub.add_parser("metrics", help="run を横断して集計する")
    p_met.add_argument("--since", default=None, help="YYYY-MM-DD 以降に collect した run のみ")

    args = parser.parse_args()
    return {"build": cmd_build, "collect": cmd_collect,
            "doctor": cmd_doctor, "list": cmd_list,
            "record-merge": cmd_record_merge,
            "metrics": cmd_metrics}[args.command](args)


if __name__ == "__main__":
    sys.exit(main())
