"""cgd Lv5A の文面: 実装仕様・レビュー依頼・質問・レポートを組み立てる純関数 (入出力・状態は持たない)。"""

from __future__ import annotations

import difflib
import re
from typing import Any, Callable, Mapping, Sequence

from cgd_lv5a_io import DEFAULT_ROSTER, Lv3aRun, cost_line, total_costs

G1_YES, G1_STOP = "この設計で実装する", "中止する"
G1_REDO = "設計を直したい（依頼文を直して consult をやり直す）"
G2_ACCEPT, G2_REDO, G2_DISCARD = "受け入れる（反映は別手順）", "指摘を反映して Codex に出し直す", "破棄して元に戻す"
FACTS_HEADING = "## 確認済みの事実"
SPEC_LIMIT, REPORT_LIMIT, DESIGN_EXCERPT = 12 * 1024, 60, 25
ADOPTED, RED, ORANGE, YELLOW = ("採用", "部分採用"), "🔴", "🟠", "🟡"
# レビュアーの組 (Lv3A の --roster) の表示名。Lv3A の定義は import しない (説明の文だけ。選択肢の一致は契約テストが守る)
ROSTER_LABELS = {
    "lv3": "lv3（Codex・DeepSeek の 技術×批評 4 者）",
    "lv7": "lv7（Codex medium+high・DeepSeek・Qwen の 技術 4 者。integration バグ重視）",
    "lv8": "lv8（lv7 の 4 者 + 批評 2 者〔Codex high・DeepSeek〕= 6 者）",
}


def clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


# ---------------------------------------------------------------- クラスタの扱い


def technical(clusters: Sequence[dict[str, Any]], *marks: str) -> list[dict[str, Any]]:
    return [c for c in clusters if c.get("kind") == "technical" and (not marks or c.get("severity") in marks)]


def impl_targets(clusters: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    """実装仕様に載せるもの: technical・採用/部分採用・🔴/🟠 (🔴 が先)。"""
    picked = [c for c in technical(clusters, RED, ORANGE) if c.get("adopt") in ADOPTED]
    return sorted(picked, key=lambda c: c.get("severity") != RED)


def fix_targets(clusters: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    """自動修正の対象: technical・🔴・採用/部分採用だけ。"""
    return [c for c in technical(clusters, RED) if c.get("adopt") in ADOPTED]


def same_title(a: str, b: str) -> bool:
    x, y = (re.sub(r"[\s、。・「」（）()\[\]]+", "", t).casefold() for t in (a, b))
    return x == y or difflib.SequenceMatcher(None, x, y).ratio() >= 0.85


def cluster_block(run: Lv3aRun, cluster: Mapping[str, Any]) -> str:
    origins = []
    for member in list(cluster.get("members", []))[:4]:
        vendor, headline = run.finding(str(member))
        origins.append(f"{member} ({vendor}): {headline or '(見出し不明)'}")
    return (f"- [{cluster.get('severity')}] {cluster.get('title')}（{cluster.get('adopt')}）\n"
            f"  対応案: {cluster.get('proposal')}\n  出所: " + " / ".join(origins))


# ---------------------------------------------------------------- 仕様・依頼文


def fit_spec(render: Callable[[str, list[str], int], str], brief: str, blocks: list[str], limit: int = SPEC_LIMIT) -> str:
    """12KB (バイト) に収める。削る順: 依頼文の末尾 (1500 字まで) → 指摘の末尾 (🔴 が先頭なので残る) → 最後に切る。"""
    cap, kept = len(brief), list(blocks)
    while True:
        cut = brief if cap >= len(brief) else brief[:cap] + "\n（依頼文は長いため以降を省略。設計ファイルに要点がある）"
        text = render(cut, kept, len(blocks) - len(kept))
        if len(text.encode("utf-8")) <= limit:
            return text
        if cap > 1500:
            cap = max(1500, int(min(cap, len(brief)) * 0.8))
        elif kept:
            kept.pop()
        else:
            return text.encode("utf-8")[:limit].decode("utf-8", "ignore")


def constraints_text(frozen: Sequence[str], design_rel: str) -> str:
    lines = ["- 作業フォルダの外に書かない・読まない。上位フォルダの AGENTS.md や .claude/skills 配下は読まない",
             f"- 設計ファイル `{design_rel}` に従う。ユーザーの回答は設計より優先する: 回答と設計が食い違う点は回答に合わせて実装し、"
             "設計ファイルの該当箇所（方針・未解決事項）も回答に合わせて更新してよい（設計の他の部分は変えない）。"
             "依頼文そのものと矛盾して判断できないことだけ、実装せずに最終報告で質問する"]
    if frozen:
        lines.append("- 凍結ファイル（すでにあるものは変更・削除しない。新しく足すのは可）: " + " / ".join(frozen))
    return "\n".join(lines)


def build_impl_spec(brief: str, design_rel: str, blocks: list[str], answers: list[str], notes: str, frozen: Sequence[str]) -> str:
    def render(brief_text: str, kept: list[str], dropped: int) -> str:
        parts = ["【Lv5A 実装依頼】\n設計ファイルと作業フォルダの既存コードを読み、設計どおりに実装する。",
                 "## 依頼文\n" + brief_text.strip(),
                 f"## 設計ファイル\n`{design_rel}`（作業フォルダ内。先に読む。中身はここに貼らない）"]
        if kept:
            parts.append("## 設計レビューの指摘のうち反映すること（Lv3A の統合結果・technical・採用/部分採用・🔴/🟠）\n"
                         + "\n".join(kept) + (f"\n（ほか {dropped} 件は省略）" if dropped else ""))
        parts.append("## ユーザーの回答（原文）\n" + "\n".join(answers) + (f"\n補足: {clip(notes, 1500)}" if notes else ""))
        return "\n\n".join([*parts, "## Lv5A の制約（必ず守る）\n" + constraints_text(frozen, design_rel)]) + "\n"

    return fit_spec(render, brief, blocks)


def build_fix_spec(brief: str, design_rel: str, blocks: list[str], frozen: Sequence[str], answers: Sequence[str]) -> str:
    def render(brief_text: str, kept: list[str], dropped: int) -> str:
        return "\n\n".join([
            "【Lv5A 🔴 自動修正（この 1 周だけ）】\n直前の実装の差分レビューで、次の重大指摘（technical・🔴）が採用/部分採用された。"
            "**これだけ**を直し、他は変えない。",
            "**設計書だけの修正で済ませない。** 指摘が設計書との不一致なら、設計書と実装（コード・テスト）を必ず一致させる"
            "（基本は実装を設計書どおりに直す。設計書の記述を直す必要があるときも、コードとテストを同じ内容へ揃える）。",
            "## 直すこと\n" + "\n".join(kept) + (f"\n（ほか {dropped} 件は省略）" if dropped else ""),
            "## 元の依頼文\n" + brief_text.strip(), "## ユーザーの回答（原文）\n" + "\n".join(answers),
            "## Lv5A の制約（必ず守る）\n" + constraints_text(frozen, design_rel)]) + "\n"

    return fit_spec(render, brief, blocks)


def design_spec(brief: str, design_rel: str) -> str:
    return (
        "【Lv5A 設計案の作成】\n"
        f"作業フォルダの既存コードを読み、下の【依頼文】を実装するための設計案を **`{design_rel}` だけ**に書くこと。\n"
        "- コードは書かない。既存ファイルは変更しない（変更しても戻される）。設計ファイル以外の新規ファイルも作らない\n"
        "- 設計案に書くこと: 変更対象ファイル / 方針 / 受け入れ条件 / リスク / 検証方法。末尾に「未解決事項」の節を置く\n"
        "- 設計ファイルは UTF-8・LF で書く\n"
        "- 上位フォルダの AGENTS.md や .claude/skills 配下は読まない\n\n【依頼文】\n" + brief.strip() + "\n")


def add_facts(brief: str, lines: Sequence[str]) -> str:
    """『## 確認済みの事実』の節の末尾に箇条書きを足す (節が無ければ末尾に作る)。"""
    bullets = "\n".join(f"- {line}" for line in lines)
    head, found, rest = brief.partition(FACTS_HEADING)
    if not found:
        return brief.rstrip() + f"\n\n{FACTS_HEADING}\n{bullets}\n"
    body, nxt, tail = rest.partition("\n## ")
    return head + found + body.rstrip("\n") + "\n" + bullets + "\n" + (f"\n## {tail}" if nxt else "")


def add_review_target(brief: str, what: str) -> str:
    return brief.rstrip() + f"\n\n## レビュー対象\n{what}\n"


# ---------------------------------------------------------------- 質問


def choice_question(qid: str, question: str, options: Sequence[tuple[str, str]], recommended: str, reason: str) -> dict[str, Any]:
    """Lv3A の questions.json と同じ形 (reason_ok は Lv3A 由来の質問との整合のため true)。"""
    return {"id": qid, "question": question, "recommended": recommended, "recommended_reason": reason,
            "options": [{"label": label, "description": desc} for label, desc in options], "reason_ok": True, "source": "lv5a"}


def g1_question() -> dict[str, Any]:
    return choice_question("G1", "この設計で実装に進みますか？", [
        (G1_YES, "設計案どおり Codex に実装させ、機械検査・差分レビューまで自動で回す"),
        (G1_REDO, "設計案に問題がある。依頼文を直して consult をやり直す（label を変える）"),
        (G1_STOP, "ここで打ち切る（設計ファイルと記録は残る）")], G1_YES, "")


def g2_question(needs_judgment: bool) -> dict[str, Any]:
    return choice_question("G2", "受け入れますか？", [
        (G2_ACCEPT, "実装を採用する。反映（コミット・デプロイ等）は別手順で行う"),
        (G2_REDO, "レビューの指摘を反映して Codex に出し直す（新しい依頼文・label で consult から）"),
        (G2_DISCARD, "Lv0 の写しから元に戻す（巻き戻せるのは 2MB 以下の通常ファイルだけ）")],
        G2_REDO if needs_judgment else G2_ACCEPT,
        "🔴 が残る・レビュー未実施・自動修正の停止のいずれかで、要ユーザー判断" if needs_judgment else "🔴 なし・全検査合格")


def answer_lines(questions: Sequence[Mapping[str, Any]], answers: Mapping[str, str]) -> list[str]:
    """回答の原文。未回答の質問は、推奨を採用したものとして明記する。"""
    return [f"- {q['id']} {q.get('question')} → "
            + (answers[q["id"]] if q["id"] in answers else f"（未回答・推奨を採用）{q.get('recommended')}") for q in questions]


def answers_fact(lines: Sequence[str]) -> str:
    """差分レビューの『確認済みの事実』用。統合者は会話を知らないので、ユーザーの決定を根拠として渡す。"""
    return "ユーザーが方向性を確認済み（回答の原文）: " + " / ".join(ln.removeprefix("- ") for ln in lines)


def roster_of(args: Mapping[str, Any]) -> str:
    """state の args から組の名前。組が無い (この機能より前の) state は lv3。"""
    return str(args.get("roster") or DEFAULT_ROSTER)


def roster_line(args: Mapping[str, Any], note: str = "") -> str:
    """レポートの「組: …」の 1 行。--no-ds / --no-qwen で外した者があれば添える (Qwen のいない lv3 の --no-qwen は無関係)。"""
    roster = roster_of(args)
    off = [name for name, flag in (("DeepSeek", args.get("no_ds")), ("Qwen", args.get("no_qwen") and roster != DEFAULT_ROSTER)) if flag]
    return f"組: {ROSTER_LABELS.get(roster, roster)}" + (f"（{'・'.join(off)} なし）" if off else "") + note


# ---------------------------------------------------------------- レポート (60 行以内)


def fit_report(build: Callable[..., list[str]], caps: list[int], floors: list[int], limit: int = REPORT_LIMIT) -> str:
    """行数上限に収まるまで、上限を持つ項目 (caps) を順に減らす。それでも超えるなら最後に切る。"""
    caps = list(caps)
    lines = build(*caps)
    for i, floor in enumerate(floors):
        while len(lines) > limit and caps[i] > floor:
            caps[i] -= 1
            lines = build(*caps)
    if len(lines) > limit:
        lines = [*lines[: limit - 1], "…（行数上限のため以降を省略。全文は各記録を参照）"]
    return "\n".join(lines) + "\n"


def limited(items: Sequence[str], cap: int) -> list[str]:
    return [*items[:cap], f"- 他 {len(items) - cap} 件（state.json / run.json 参照）"] if len(items) > cap else list(items)


def consult_lines(st: Mapping[str, Any], design_text: str, rv: Lv3aRun, n_design: int, n_red: int) -> list[str]:
    count = {m: len(technical(rv.clusters, m)) for m in (RED, ORANGE, YELLOW)}
    reds = [f"- [{RED}] {c.get('title')}（{c.get('adopt')}）: {clip(str(c.get('proposal')), 90)}" for c in technical(rv.clusters, RED)]
    lines = [f"# Lv5A consult レポート — {st['label']}", f"run: {st['run_dir']}", f"作業フォルダ: {st['args']['workdir']}",
             f"設計ファイル: {st['design_rel']}（作業フォルダに残る。削除しない）", roster_line(st["args"], "（設計レビューに適用）"),
             "", f"## 設計案（冒頭 {n_design} 行）"]
    lines += [f"> {clip(line, 160)}" for line in [ln for ln in design_text.splitlines() if ln.strip()][:n_design]]
    lines += ["", "## 設計レビュー（Lv3A）",
              f"technical: 🔴{count[RED]} 🟠{count[ORANGE]} 🟡{count[YELLOW]} / クラスタ計 {len(rv.clusters)} 件（Lv3A exit {rv.exit_code}）"]
    lines += limited(reds, n_red) if reds else ["- 🔴 なし"]
    lines += [f"⚠ {clip(w, 200)}" for w in st["consult"]["warnings"][:3]]
    lines += ["", "## ユーザーへの質問（questions.json・言い換えずに取り次ぐ）"]
    lines += [f"- {q.get('id')} {clip(str(q.get('question')), 60)} → 推奨: {q.get('recommended')}" for q in [*rv.questions, g1_question()][:5]]
    return lines + ["", "## 費用", cost_line(total_costs(st["stages"])), "",
                    "## 次にすること", "questions.json をそのまま AskUserQuestion にし、回答を answers.json に書いて implement を呼ぶ"]


def final_lines(st: Mapping[str, Any], n_red: int, n_orange: int) -> list[str]:
    r = st["result"]
    impl, autofix, rv = r["impl"], r["autofix"], r["review2"] or r["review1"]
    mod, add, dele = impl["counts"]
    lines = [f"# Lv5A 最終レポート — {st['label']}",
             "状態: " + ("要ユーザー判断（exit 21）" if r["needs"] else "完了・🔴 なし（exit 0）"), f"作業フォルダ: {st['args']['workdir']}",
             roster_line(st["args"], "（差分レビューに適用" + ("。🔴 の自動修正後の再レビューは軽い構成のまま）" if autofix["rounds"] else "）")),
             "", "## 実装結果",
             f"- 変更: 更新 {mod} / 新規 {add} / 削除 {dele}、+{impl['plus']} -{impl['minus']} 行（{', '.join(impl['files'][:6]) or '-'}）",
             f"- Codex: {impl['rounds']} 周 / {impl['tokens']:,} tokens / {impl['seconds']} 秒。検査: "
             + (" / ".join(f"{k}={v}" for k, v in impl["checks"].items()) or "-"),
             f"- 設計ファイル {st['design_rel']} は作業フォルダに残っている（削除しない）", "", "## レビュー結果"]
    if rv["performed"]:
        lines.append(f"- 実施: 🔴{len(rv['reds'])} 🟠{len(rv['oranges'])} 🟡{rv['yellows']}（technical）" + (f" ※{rv['reason']}" if rv["reason"] else ""))
        lines += limited([f"- [{RED}] {x['title']}（{x['adopt']}）" for x in rv["reds"]], n_red)
        lines += limited([f"- [{ORANGE}] {t}" for t in rv["oranges"]], n_orange)
    else:
        lines.append(f"- {rv['reason']}")
    if r["review2"] and r["review1"]["performed"]:
        lines.append(f"- 初回レビューの 🔴: {len(r['review1']['reds'])} 件")
    lines += ["", "## 自動修正の記録"]
    if autofix["rounds"]:
        floor = "（下限・件数で数えた）" if autofix["remaining"] else ""
        lines += [f"- 周回数: {autofix['rounds']}（最大 1）/ 解消した 🔴: {autofix['resolved']} 件{floor} / 残った 🔴: "
                  + (" / ".join(autofix["remaining"]) or "なし"), f"- 停止理由: {autofix['stop_reason'] or 'なし（解消した）'}"]
        if "fix" in r:
            fx = r["fix"]
            lines.append(f"- 修正の差分: 更新 {fx['counts'][0]} / 新規 {fx['counts'][1]} / 削除 {fx['counts'][2]}、+{fx['plus']} -{fx['minus']} 行"
                         f"（{', '.join(fx['files'][:4]) or '-'}）・Codex {fx['tokens']:,} tokens・検査: "
                         + (" / ".join(f"{k}={v}" for k, v in fx["checks"].items()) or "-"))
    else:
        lines.append("- 実施せず（採用/部分採用の technical 🔴 なし）")
    fix = f" / 自動修正 Lv0: {r['fix']['autodir']}" if "fix" in r else ""
    lines += ["", "## 費用", cost_line(total_costs(st["stages"])), "", "## 記録", f"- run: {st['run_dir']}（state.json / logs）",
              f"- 実装 Lv0: {impl['autodir']}{fix}", f"- 差分レビュー（Lv3A）: {rv['run_dir'] or '(なし)'}",
              "- 巻き戻し: cgd_lv0_codex.py restore --workdir <作業フォルダ> --run <基準の RUN>（新しい順・確認のみが既定）",
              "", "## 最終質問（questions_final.json の G2）", f"- G2 受け入れますか？ → 「{G2_ACCEPT}」/「{G2_REDO}」/「{G2_DISCARD}」"]
    return lines + [f"⚠ {clip(n, 160)}" for s in st["stages"] for n in s["notes"]][:3]
