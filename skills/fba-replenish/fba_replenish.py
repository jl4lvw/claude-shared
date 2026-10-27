"""FBA補充の候補を出す (読み取りだけ。022 のカート・DB は変えない)。

022 の在庫一覧 (GET /inventory/all) を取り、022 PWA と同じ判定 (picking-select.js の
pickReplenish) を node で実行して、次の 3 つを出力フォルダへ書く:

  report.md            区分ごとの件数と上位の表 (チャットにそのまま貼る)
  result.json          判定結果の全件 (run_replenish.js の出力そのまま)
  cart_import_hq.json  本店分のピッキングリスト取り込み用 JSON (PWA の「📥 JSON取り込み」に貼る)

使い方:
  python fba_replenish.py                       # 本番 (127.0.0.1:8090) から取る
  python fba_replenish.py --input inv_all.json  # 保存済みの一覧から
  python fba_replenish.py --include-b           # 取り込み JSON に B (要確認) も入れる
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import urllib.request
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

DEFAULT_URL = "http://127.0.0.1:8090/inventory/all"
SKILL_DIR = Path(__file__).resolve().parent
REPO_ROOT = SKILL_DIR.parents[2]
LIB_PATH = REPO_ROOT / "022.Amazon在庫PWA" / "pwa" / "picking-select.js"
RUNNER = SKILL_DIR / "run_replenish.js"


@dataclass
class Options:
    url: str
    input: Path | None
    out_dir: Path
    top: int
    include_b: bool


def parse_args(argv: list[str] | None = None) -> Options:
    p = argparse.ArgumentParser(description="FBA補充の候補を出す (読み取りだけ)")
    p.add_argument("--url", default=DEFAULT_URL, help="022 の /inventory/all の URL")
    p.add_argument("--input", type=Path, help="保存済みの /inventory/all の JSON (指定時は取得しない)")
    p.add_argument("--out-dir", type=Path, help="出力フォルダ (既定: C:/tmp-ai/fba_replenish/<日時>)")
    p.add_argument("--top", type=int, default=30, help="表に出す上位の件数 (区分ごと)")
    p.add_argument("--include-b", action="store_true", help="取り込み JSON に B (要確認) も入れる")
    a = p.parse_args(argv)
    out = a.out_dir or Path("C:/tmp-ai/fba_replenish") / datetime.now().strftime("%Y%m%d_%H%M%S")
    return Options(url=a.url, input=a.input, out_dir=out, top=a.top, include_b=a.include_b)


def fetch_inventory(url: str, dest: Path) -> None:
    """GET だけ。書き込み系の API は呼ばない。"""
    with urllib.request.urlopen(url, timeout=120) as res:  # noqa: S310 (社内の固定 URL)
        body = res.read()
    data = json.loads(body.decode("utf-8"))
    if not isinstance(data, dict) or not isinstance(data.get("items"), list) or not data["items"]:
        raise RuntimeError("在庫一覧が空、または形式が違います")
    dest.write_bytes(body)


def run_judge(inventory_json: Path) -> dict:
    proc = subprocess.run(
        ["node", str(RUNNER), str(LIB_PATH), str(inventory_json)],
        capture_output=True,
        timeout=120,
        check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError("判定の実行に失敗: " + proc.stderr.decode("utf-8", "replace"))
    return json.loads(proc.stdout.decode("utf-8"))


def group_of(r: dict) -> str:
    return r["tier"] if r["send"] > 0 else "X"


def s7_only_candidates(rows: list[dict]) -> list[dict]:
    """7S専用の印を付ける候補: 本店から補充になっているが、本店在庫が 0 と入力済みで 7S に在庫がある。"""
    return [
        r for r in rows
        if r["source"] == "hq"
        and r.get("hq_warehouse_qty") == 0
        and not r.get("hq_warehouse_infinite")
        and (r.get("airlogi") or 0) > 1
    ]


def _cell(s: object) -> str:
    return str(s).replace("|", "／").replace("\n", " ")


def table(rows: list[dict], top: int, with_source: bool = True) -> list[str]:
    cols = ["SKU", "商品名", *(["補充元"] if with_source else []), "FBA在庫+入荷中", "30日売上", "必要数", "送る数", "印"]
    aligns = ["---", "---", *(["---"] if with_source else []), "---:", "---:", "---:", "---:", "---"]
    out = ["| " + " | ".join(cols) + " |", "|" + "|".join(aligns) + "|"]
    for r in rows[:top]:
        marks = []
        if r["extra"] > 0:
            sendable = r.get("extraSend", r["extra"])
            marks.append(f"設定数まで+{r['extra']}" + (f"(7Sから送れるのは+{sendable})" if sendable < r["extra"] else ""))
        if r["overset"]:
            marks.append("設定数が多すぎる疑い")
        if r["source"] == "7s" and r["short7s"] > 0:
            marks.append(f"7S在庫が{r['short7s']}個足りない")
        src = "7S" if r["source"] == "7s" else "本店"
        req = "" if r["reqFba"] is None else r["reqFba"]
        cells = [_cell(r["sku"]), _cell(r["product_name"][:40]), *([src] if with_source else []),
                 r["fba"] + r["inbound"], r["sales"], req, r["send"], _cell("・".join(marks))]
        out.append("| " + " | ".join(str(c) for c in cells) + " |")
    if len(rows) > top:
        out.append("| " + " | ".join([f"…ほか {len(rows) - top} 件"] + [""] * (len(cols) - 1)) + " |")
    return out


def build_report(res: dict, top: int, include_b: bool) -> str:
    rows = res["rows"] + res["blocked"]
    groups = {g: [r for r in rows if group_of(r) == g] for g in ("A", "B", "X")}
    a_hq = [r for r in groups["A"] if r["source"] == "hq"]
    a_7s = [r for r in groups["A"] if r["source"] == "7s"]
    c = res["constants"]
    lines = [
        f"# FBA補充の候補 ({datetime.now():%Y-%m-%d %H:%M})",
        "",
        f"- 対象の商品 {res['total_items']} 件を判定 (目標 = 30日売上 × {c['sales_factor']}・"
        f"多すぎる疑い = 必要数 > 30日売上 × {c['overset_ratio']}・7S に {c['keep_7s']} 個残す)",
        f"- **A 必ず補充: {len(groups['A'])} 件・{sum(r['send'] for r in groups['A'])} 個**"
        f" (本店から {len(a_hq)} 件・7S から {len(a_7s)} 件)",
        f"- B 要確認: {len(groups['B'])} 件・{sum(r['send'] for r in groups['B'])} 個"
        f" (うち設定数が多すぎる疑い {sum(1 for r in groups['B'] if r['overset'])} 件)",
        f"- 送れない (7S専用で 7S在庫が足りない): {len(groups['X'])} 件",
        f"- 判定できない (30日売上・7S在庫が未取得): {res['unknown']} 件",
    ]
    if not res.get("has_source_7s_only_field"):
        lines.append("- ⚠️ サーバーが「7S専用」の印にまだ対応していません (v61 未反映)。全件を本店からとして判定しています")
    for g, title in (("A", "A 必ず補充"), ("B", "B 要確認"), ("X", "送れない")):
        if groups[g]:
            lines += ["", f"## {title} ({len(groups[g])} 件)", "", *table(groups[g], top)]
    cand = s7_only_candidates(rows)
    if cand:
        lines += [
            "",
            f"## 7S専用の印を付ける候補 ({len(cand)} 件)",
            "",
            "本店在庫が 0 と入力済みで、7S に在庫がある商品。本店に置かないものなら、PWA の FBA補充タブで「7S専用にする」を押す。",
            "",
            *table(cand, top, with_source=False),
        ]
    lines += [
        "",
        "## カートへの入れ方",
        "",
        "- PWA の「🎯 ピッキング対象選定」→「🚚 FBA補充」タブで選んで「カートへ追加」(本店分→ピッキングリスト、7S分→7Sカート)",
        "- 本店分だけなら cart_import_hq.json を「📥 JSON取り込み」に貼っても入る"
        + (" (B も含む)" if include_b else " (A だけ)"),
    ]
    return "\n".join(lines) + "\n"


def build_cart_import(res: dict, include_b: bool) -> dict:
    tiers = {"A", "B"} if include_b else {"A"}
    items = [
        {"sku": r["sku"], "qty": r["send"]}
        for r in res["rows"]
        if r["source"] == "hq" and r["tier"] in tiers and r["send"] > 0
    ]
    return {"version": 1, "mode": "replace", "items": items}


def main(argv: list[str] | None = None) -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    opt = parse_args(argv)
    if not LIB_PATH.exists():
        print(f"判定のファイルが見つかりません: {LIB_PATH}", file=sys.stderr)
        return 2
    opt.out_dir.mkdir(parents=True, exist_ok=True)
    inv = opt.input
    if inv is None:
        inv = opt.out_dir / "inventory_all.json"
        fetch_inventory(opt.url, inv)
    res = run_judge(inv)
    (opt.out_dir / "result.json").write_text(
        json.dumps(res, ensure_ascii=False, indent=1), encoding="utf-8", newline=""
    )
    cart = build_cart_import(res, opt.include_b)
    (opt.out_dir / "cart_import_hq.json").write_text(
        json.dumps(cart, ensure_ascii=False, indent=1), encoding="utf-8", newline=""
    )
    report = build_report(res, opt.top, opt.include_b)
    (opt.out_dir / "report.md").write_text(report, encoding="utf-8", newline="")
    print(report)
    print(f"出力フォルダ: {opt.out_dir.as_posix()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
