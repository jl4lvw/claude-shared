"""FBA補充を Claude が判断するための材料を作る (読み取りだけ)。

機械の判定 (run_replenish.js の結果) に、022 の注文明細 (order_items_raw) から出した
売上の動き (7日・30日・90日・昨年の同じ時期) を足し、一件ずつ見る表にする。

見る対象:
  A / 送れない    全件
  B               30日売上か「昨年のこの先30日」の売上があるものだけ (無いものは一括で送らない)
  準A (near)      機械では対象外だが、納品にかかる日数を足すと 30 日分に届かないもの

注文明細は 2026-04 から本格的に入っている (それより前は数件だけ)。履歴が始まる前の期間は
0 ではなく None (データなし) にする。
"""

from __future__ import annotations

import math
import sqlite3
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

PROD_DB = Path("C:/ClaudeCode/022.Amazon在庫PWA/server/amzinv.db")
JST = timezone(timedelta(hours=9))
TARGET_DAYS = 30  # 機械の判定 (30日売上 × 1.0) と同じ日数

# sales_worker.compute_sales_30d_by_sku と同じ「販売」の条件
_SALES_SQL = (
    "SELECT sku, purchase_date, SUM(quantity) FROM order_items_raw "
    "WHERE purchase_date >= ? "
    "  AND order_status IN ('Shipped','Unshipped') "
    "  AND (item_status = '' OR item_status != 'Cancelled') "
    "GROUP BY sku, purchase_date"
)


# ピッキングで集める単位 (2026-10-28 運用者の指示: ジャンルを混ぜると集めるときに混乱する)。
# 商品名に含む語で上から順に当てる。どれにも当たらないものは OTHER_GENRE。
GENRES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("パーカー", ("パーカー",)),
    ("Tシャツ・ポロ", ("Tシャツ", "ポロシャツ")),
    ("ワッペン", ("ワッペン",)),
    ("ステッカー・金蒔絵", ("ステッカー", "金蒔絵")),
    ("手ぬぐい・タオル", ("手ぬぐい", "タオル")),
    ("帽子", ("帽子", "キャップ")),
    ("靴", ("靴",)),
)
OTHER_GENRE = "小物"
GENRE_NAMES: tuple[str, ...] = tuple(g for g, _ in GENRES) + (OTHER_GENRE,)


def genre_of(product_name: str) -> str:
    for g, keys in GENRES:
        if any(k in product_name for k in keys):
            return g
    return OTHER_GENRE


@dataclass(frozen=True)
class Windows:
    """集計の期間 (どれも両端を含む)。today は JST の今日。"""

    today: date

    def span(self, days: int, end_offset: int = 0) -> tuple[date, date]:
        end = self.today - timedelta(days=end_offset)
        return end - timedelta(days=days - 1), end

    @property
    def d7(self) -> tuple[date, date]:
        return self.span(7)

    @property
    def d30(self) -> tuple[date, date]:
        return self.span(30)

    @property
    def d90(self) -> tuple[date, date]:
        return self.span(90)

    @property
    def ly_same30(self) -> tuple[date, date]:
        """昨年の、今日までの 30 日。"""
        return self.span(30, end_offset=365)

    @property
    def ly_next30(self) -> tuple[date, date]:
        """昨年の、明日からの 30 日 (これから売れる量の手がかり)。"""
        start = self.today - timedelta(days=364)
        return start, start + timedelta(days=29)


def today_jst() -> date:
    return datetime.now(JST).date()


_MONTHLY_SQL = "SELECT substr(purchase_date, 1, 7), COUNT(*) FROM order_items_raw GROUP BY 1 ORDER BY 1"


def _connect_ro(db_path: Path) -> sqlite3.Connection:
    return sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True, timeout=10)


def history_start(db_path: Path) -> date | None:
    """注文明細が本格的に入り始めた月の 1 日。

    月ごとの件数が、件数の多い月の中央値の 1 割に届く最初の月とする
    (2024-11〜2026-03 は月に数件だけで、昨年の売上としては使えない)。
    """
    con = _connect_ro(db_path)
    try:
        months = con.execute(_MONTHLY_SQL).fetchall()
    finally:
        con.close()
    if not months:
        return None
    counts = sorted(n for _, n in months)
    floor = counts[len(counts) // 2] * 0.1
    for ym, n in months:
        if n >= floor:
            return date.fromisoformat(f"{ym}-01")
    return None


def load_daily_sales(db_path: Path, since: date) -> dict[str, dict[date, int]]:
    """{sku: {日付: 個数}}。DB は読み取り専用で開く。"""
    con = _connect_ro(db_path)
    try:
        rows = con.execute(_SALES_SQL, (since.isoformat(),)).fetchall()
    finally:
        con.close()
    out: dict[str, dict[date, int]] = {}
    for sku, d, qty in rows:
        out.setdefault(sku, {})[date.fromisoformat(str(d)[:10])] = int(qty or 0)
    return out


def sum_span(daily: dict[date, int], span: tuple[date, date]) -> int:
    s, e = span
    return sum(q for d, q in daily.items() if s <= d <= e)


def sales_summary(daily: dict[date, int], w: Windows, start: date | None = None) -> dict[str, int | None]:
    """期間ごとの売上。start (履歴の始まり) より前に掛かる期間は None。"""

    def span(sp: tuple[date, date]) -> int | None:
        if start is not None and sp[0] < start:
            return None
        return sum_span(daily, sp)

    return {
        "s7": sum_span(daily, w.d7),
        "s30": sum_span(daily, w.d30),
        "s90": span(w.d90),
        "ly_same30": span(w.ly_same30),
        "ly_next30": span(w.ly_next30),
    }


def guide_qty(sales30: int, have: int, lead_days: int) -> int:
    """目安: 30日売上の 1 日あたり × (30日 + 納品にかかる日数) − (FBA在庫 + 入荷中)。"""
    target = math.ceil(sales30 / TARGET_DAYS * (TARGET_DAYS + lead_days))
    return max(0, target - have)


def cover_days(sales30: int, have: int) -> float | None:
    """今の FBA在庫 + 入荷中 が何日もつか (30日売上の割合で)。売上 0 は None。"""
    if sales30 <= 0:
        return None
    return round(have / (sales30 / TARGET_DAYS), 1)


def kind_of(r: dict, near: bool) -> str:
    if near:
        return "準A"
    return r["tier"] if r["send"] > 0 else "送れない"


def build_rows(res: dict, sales: dict[str, dict[date, int]], w: Windows, lead_days: int,
               keep_7s: int, start: date | None = None) -> tuple[list[dict], list[dict]]:
    """(一件ずつ見る行, 一括で送らない B の行) を返す。"""
    picked = [(r, False) for r in res["rows"] + res["blocked"]] + [(r, True) for r in res.get("near", [])]
    review: list[dict] = []
    skipped: list[dict] = []
    for r, near in picked:
        sm = sales_summary(sales.get(r["sku"], {}), w, start)
        have = r["fba"] + r["inbound"]
        guide = guide_qty(r["sales"], have, lead_days)
        if r["source"] == "7s" and r.get("airlogi") is not None:
            guide = min(guide, max(0, r["airlogi"] - keep_7s))
        row = {
            "kind": kind_of(r, near),
            "genre": genre_of(r.get("product_name", "")),
            "sku": r["sku"],
            "product_name": r.get("product_name", ""),
            "source": r["source"],
            "source_7s_only": bool(r.get("source_7s_only")),
            "fba": r["fba"],
            "inbound": r["inbound"],
            "have": have,
            "sales30_pwa": r["sales"],
            **sm,
            "cover_days": cover_days(r["sales"], have),
            "req_fba": r.get("reqFba"),
            "overset": bool(r.get("overset")),
            "machine": 0 if near else r["send"],
            "guide": guide,
            "airlogi": r.get("airlogi"),
            "inbound_7s": r.get("inbound7s"),
            "hq_warehouse_qty": r.get("hq_warehouse_qty"),
        }
        if row["kind"] == "B" and r["sales"] <= 0 and not sm["ly_next30"]:
            skipped.append(row)
        else:
            review.append(row)
    order = {"A": 0, "準A": 1, "B": 2, "送れない": 3}
    review.sort(key=lambda x: (GENRE_NAMES.index(x["genre"]), order[x["kind"]], -x["guide"], x["sku"]))
    return review, skipped


def _cell(v: object) -> str:
    if v is None:
        return "—"
    return str(v).replace("|", "／").replace("\n", " ")


def materials_md(review: list[dict], skipped: list[dict], w: Windows, lead_days: int, keep_7s: int,
                 start: date | None = None) -> str:
    """Claude が一件ずつ判断するための表 (チャットには貼らない作業用)。"""
    head = [
        f"# FBA補充 判断の材料 ({w.today.isoformat()} 時点・納品にかかる日数 {lead_days} 日)",
        "",
        f"- 一件ずつ見る: {len(review)} 件 / 売上が無いので一括で送らない B: {len(skipped)} 件",
        f"- 目安 = 30日売上の1日あたり × ({TARGET_DAYS} + {lead_days}) − (FBA在庫+入荷中)。7S からは 7S在庫 − {keep_7s} まで",
        "- 昨年この先30日 = 昨年の明日からの 30 日の売上 (季節の手がかり)。"
        f"注文明細は {start.isoformat() if start else '不明'} から。それより前に掛かる期間は — (データなし)",
        "",
        "## ジャンルごとの候補 (今回ピッキングリストに入れるジャンルを選ぶ材料)",
        "",
        "| ジャンル | 件数 | A | 目安の合計 | FBA在庫+入荷中が0 | 30日売上の合計 |",
        "|---|---|---|---|---|---|",
    ]
    for g in GENRE_NAMES:
        rs = [r for r in review if r["genre"] == g]
        if rs:
            head.append(f"| {g} | {len(rs)} | {sum(r['kind'] == 'A' for r in rs)} | {sum(r['guide'] for r in rs)} | "
                        f"{sum(r['have'] == 0 for r in rs)} | {sum(r['s30'] for r in rs)} |")
    head += ["", "## 一件ずつの材料 (ジャンル順)", ""]
    cols = ["ジャンル", "区分", "SKU", "商品名", "元", "在庫+入荷中", "7日", "30日", "90日", "昨年同期30日",
            "昨年この先30日", "もつ日数", "必要数", "機械", "目安", "7S在庫(入荷待ち)", "印"]
    lines = ["| " + " | ".join(cols) + " |", "|" + "|".join(["---"] * len(cols)) + "|"]
    for r in review:
        marks = []
        if r["overset"]:
            marks.append("設定数が多すぎる疑い")
        if r["s7"] >= 3 and r["s7"] * 30 >= r["s30"] * 7 * 2:
            marks.append("直近7日が伸びている")
        s7cell = "—" if r["airlogi"] is None else f"{r['airlogi']}({r['inbound_7s'] or 0})"
        cells = [r["genre"], r["kind"], r["sku"], r["product_name"][:30], "7S" if r["source"] == "7s" else "本店",
                 f"{r['fba']}+{r['inbound']}", r["s7"], r["s30"], r["s90"], r["ly_same30"], r["ly_next30"],
                 r["cover_days"], r["req_fba"], r["machine"], r["guide"], s7cell, "・".join(marks)]
        lines.append("| " + " | ".join(_cell(c) for c in cells) + " |")
    return "\n".join(head + lines) + "\n"
