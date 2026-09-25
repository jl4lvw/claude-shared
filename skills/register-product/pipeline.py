"""新商品登録パイプライン (楽天登録済みの G 番号 → 023 / Eストア / Yahoo! / GoQ 在庫連携 / 在庫数).

使い方 (詳細は SKILL.md):
  python pipeline.py preflight G2225                 # 読み取りのみ。plan の下書きと確認事項を出す
  python pipeline.py approve --plan <plan.json>      # ユーザーが承認した内容の plan に承認ハッシュを刻む
  python pipeline.py run --plan <plan.json>          # 承認済み plan を上から順に実行 (途中から再開できる)
  python pipeline.py run --plan <plan.json> --only estore_register
  python pipeline.py verify --plan <plan.json>       # 店頭・GoQ の最終確認のみ (読み取りのみ・承認不要)

設計 (cgd Lv3 レビュー 2026-09-25 を反映):
  - 各ステップは「すでに済んでいれば SKIP → 実行 → 結果の状態を読み戻して検証」の順。
    コマンドの成否ではなく反映後の状態で判定する (AGENTS.md「反映系は結果を検証して終える」)。
  - 承認ハッシュ: run は「承認された内容と同一の plan」しか実行しない。承認後に plan を編集した・
    承認から 24 時間過ぎた・すでに完了した plan の再実行、は拒否する (誤操作・古い plan の再利用対策)。
    これは認証ではなく手順の整合性チェック(ハッシュ)であり、承認を取る責任は SKILL.md 側にある。
  - 同じ plan の二重起動は <plan>.lock で拒否する。
  - ユーザーの操作が要る場合 (セッション切れ・Yahoo!の未反映が想定より多い・GoQ の行が特定できない等) は
    NEEDS_USER で止まる (終了コード 10)。パスワードは一切扱わない。
  - 外部への書き込みで通信が切れたときは FAIL で止める。サーバー側の状態を確認してから再実行すること。
  - 出力は 1 ステップ 1 行 `STEP <name>: <OK|SKIP|FAIL|NEEDS_USER> <詳細>`。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sqlite3
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable

sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

PM_ROOT = Path(r"C:\ClaudeCode\023.商品マスタDB")
DB_PATH = PM_ROOT / "data" / "productmaster.db"
RUNS_DIR = PM_ROOT / "data" / "register_runs"
API = "http://127.0.0.1:8290"
ACTOR = "register-product"
PY = sys.executable
STORE_SHOPSERVE = "https://seifukunofuji.co.jp/SHOP/{code}.html"
STORE_YAHOO = "https://store.shopping.yahoo.co.jp/seifukunofuji/{code}.html"
TAX = 1.1
EXIT_NEEDS_USER = 10
APPROVAL_TTL = timedelta(hours=24)
LOCK_STALE = timedelta(hours=2)
YAHOO_PENDING_ALLOW = 4  # 下見のときの未反映ページ数に、今回の登録分として許す増加数
G_RE = re.compile(r"^[GU]\d{4}$")


class StepFail(RuntimeError):
    pass


class NeedsUser(RuntimeError):
    pass


# ---------------------------------------------------------------- helpers
def http(method: str, path: str, body: dict | None = None, headers: dict | None = None,
         timeout: int = 300) -> tuple[int, Any]:
    data = json.dumps(body).encode("utf-8") if body is not None else None
    h = {"X-Actor": ACTOR, **(headers or {})}
    if data is not None:
        h["Content-Type"] = "application/json"
    req = urllib.request.Request(API + path, data=data, method=method, headers=h)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read()
            code = r.status
    except urllib.error.HTTPError as e:
        raw, code = e.read(), e.code
    except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
        # 書き込み系では「サーバー側で成功したが応答が届かない」もありうる。再実行前に必ず状態を照会すること
        raise StepFail(f"通信エラー {method} {path}: {type(e).__name__}。"
                       "サーバー側の状態を確認してから再実行") from e
    text = raw.decode("utf-8", "replace")
    try:
        return code, json.loads(text)
    except ValueError:
        return code, text


def fetch_text(url: str, timeout: int = 30) -> tuple[int, str]:
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, ""
    except (urllib.error.URLError, TimeoutError, ConnectionError):
        return 0, ""


def q_all(sql: str, params: tuple = ()) -> list[sqlite3.Row]:
    conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        return conn.execute(sql, params).fetchall()
    finally:
        conn.close()


def q_one(sql: str, params: tuple = ()) -> sqlite3.Row | None:
    rows = q_all(sql, params)
    return rows[0] if rows else None


def run_tool(args: list[str], timeout: int = 600) -> tuple[int, str]:
    try:
        p = subprocess.run([PY, *args], cwd=str(PM_ROOT), capture_output=True, timeout=timeout,
                           env={**os.environ, "PYTHONIOENCODING": "utf-8"})
    except subprocess.TimeoutExpired as e:
        raise StepFail(f"外部ツールがタイムアウト: {' '.join(args[:2])}") from e
    return p.returncode, (p.stdout or b"").decode("utf-8", "replace") + (p.stderr or b"").decode("utf-8", "replace")


def run_json_tool(args: list[str]) -> dict:
    """JSON を 1 行返すツールを呼ぶ。読み取り失敗は例外にして呼び出し側を fail-closed にする."""
    rc, out = run_tool(args)
    lines = [ln for ln in out.strip().splitlines() if ln.strip().startswith("{")]
    if not lines:
        raise StepFail(f"ツールの出力が JSON でない: {out[-200:]}")
    res = json.loads(lines[-1])
    if not res.get("ok"):
        raise StepFail(f"ツールが失敗: {res.get('error')}")
    return res


def excl_tax(price_incl: int) -> int:
    return round(price_incl / TAX)


def plan_hash(plan: dict) -> str:
    body = {k: v for k, v in plan.items() if k != "approval"}
    return hashlib.sha256(json.dumps(body, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()


# ------------------------------------------------------------- preflight
def rakuten_remote(mn: str) -> dict | None:
    """楽天の商品情報 (読み取りのみ)。023 サーバーのモジュールを直接使う."""
    sys.path.insert(0, str(PM_ROOT))
    from server.services import rakuten_sync
    return rakuten_sync.get_remote(mn)


def _to_int(x: Any) -> int | None:
    try:
        return int(float(str(x).replace(",", "")))
    except (TypeError, ValueError):
        return None


def parse_variants(remote: dict) -> list[dict]:
    out = []
    variants = remote.get("variants") or {}
    items = variants.items() if isinstance(variants, dict) else enumerate(variants)
    for vid, v in items:
        sel = v.get("selectorValues") or {}
        out.append({
            "variant_id": str(vid),
            "selectors": {str(k): str(x) for k, x in sel.items()},
            "price": _to_int(v.get("standardPrice")),
            "merchant_sku": v.get("merchantDefinedSkuId"),
        })
    return out


def yahoo_copy_candidates(title: str, price: int | None, n_axes: int, exclude: str = "",
                          limit: int = 4) -> list[dict]:
    """Yahoo! 新規登録のコピー元候補: 同種キーワードを含み、軸の数が同じで、同価格・新しい順."""
    keywords = [k for k in ("ワッペン", "Tシャツ", "コイン", "帽子", "キャップ", "ポロシャツ", "パーカー",
                            "タオル", "ぬいぐるみ", "バッグ", "ステッカー") if k in title]
    rows = q_all("SELECT code, name, price, variation1_name, variation2_name, variation3_name, "
                 "variation4_name, variation5_name FROM yahoo_snapshot_products")
    cands = []
    for r in rows:
        name = r["name"] or ""
        if keywords and not any(k in name for k in keywords):
            continue
        axes = sum(1 for i in range(1, 6) if r[f"variation{i}_name"])
        if axes != n_axes or (r["code"] or "").lower() == exclude.lower():
            continue
        try:
            same_price = price is not None and int(r["price"]) == int(price)
        except (TypeError, ValueError):
            same_price = False
        cands.append((same_price, r["code"], name, r["price"]))
    cands.sort(key=lambda t: (not t[0], -int(re.sub(r"\D", "", t[1]) or 0)))
    return [{"code": c, "name": n[:50], "price": p, "same_price": s} for s, c, n, p in cands[:limit]]


def preflight(g: str) -> dict:
    g = g.upper()
    if not G_RE.match(g):
        raise SystemExit(f"NG: G番号の形式が違います: {g}")
    mn = g.lower()
    remote = rakuten_remote(mn)
    if remote is None:
        raise SystemExit(f"NG: 楽天に {mn} がありません。先に楽天RMSで登録してください")
    variants = parse_variants(remote)
    is_var = len(variants) >= 2
    title = remote.get("title") or ""
    sku_main = f"{g}-parent" if is_var else g
    existing = sorted(r["sku"] for r in q_all(
        "SELECT sku FROM products WHERE (sku=? OR parent_sku=?) AND deleted_at IS NULL", (sku_main, sku_main)))
    prices = [v["price"] for v in variants if v["price"]]
    flags = []
    for v in variants:
        if v["price"] and abs(v["price"] / TAX - round(v["price"] / TAX)) > 1e-6:
            flags.append(f"税抜換算に端数あり: {v['variant_id']} {v['price']}円 → {v['price'] / TAX:.2f}(丸め方を決めてください)")
    if is_var:
        flags.append("バリエーション商品は初回の実機検証が済んでいません(試験運用): GoQ の行の対応確認などで途中で確認が入る可能性があります")
    n_axes = len(variants[0]["selectors"]) if is_var and variants else 0
    y_pending = run_json_tool(["tools/yahoo_publish_tool.py", "summary"])
    pending_rows = y_pending.get("pending_rows", 0)
    if pending_rows:
        flags.append(f"Yahoo!に他の未反映が {pending_rows} 件あります。店頭反映するとこれらも一緒に公開されます(反映は人が行う設定を推奨)")
    draft = {
        "g": g, "manage_number": mn, "sku_main": sku_main, "is_variation": is_var, "title": title,
        "variants": [{**v, "estore_price": excl_tax(v["price"]) if v["price"] else None, "stock": None}
                     for v in variants],
        "estore_price_main": excl_tax(min(prices)) if prices else None,
        "lead_time_days": 5,
        "estore": {"register": True, "publish": True},
        "yahoo": {"register": True, "copy_code": None, "publish": True,
                  "reserve_publish": pending_rows == 0,
                  "pending_before": {"rows": pending_rows, "item_pages": y_pending.get("pending_item_pages", 0)},
                  "pending_allow": YAHOO_PENDING_ALLOW},
        "goq": {"resync": True},
        "not_included": ["Amazon (amz-register)", "LCL", "7S(コマースロボ)"],
        "created_at": datetime.now().isoformat(timespec="seconds"),
    }
    return {
        "draft_plan": draft,
        "already_in_023": existing,
        "yahoo_copy_candidates": yahoo_copy_candidates(title, prices[0] if prices else None, n_axes, exclude=mn),
        "flags": flags,
        "questions": ["在庫数(単品なら1つ、バリエーションは色×サイズごと。全て同数なら1つで可)",
                      "Eストア価格(既定: 楽天税込価格÷1.1)と納期日数(既定: 5日)",
                      "Yahoo!のコピー元商品",
                      "公開の可否(Eストア/Yahoo!)。Yahoo!の店頭反映はストア全体の未反映分をまとめて公開する"],
    }


# ------------------------------------------------------------ plan checks
def _is_int(x: Any) -> bool:
    return isinstance(x, int) and not isinstance(x, bool)


def validate_plan(p: dict) -> None:
    need = ["g", "manage_number", "sku_main", "is_variation", "variants", "estore_price_main",
            "lead_time_days", "estore", "yahoo", "goq"]
    miss = [k for k in need if k not in p]
    if miss:
        raise SystemExit(f"NG: plan に不足: {miss}")
    g = p["g"]
    if not G_RE.match(g) or p["manage_number"] != g.lower():
        raise SystemExit(f"NG: G番号/manage_number が不正または食い違い: {g} / {p['manage_number']}")
    expect_main = f"{g}-parent" if p["is_variation"] else g
    if p["sku_main"] != expect_main:
        raise SystemExit(f"NG: sku_main が {expect_main} ではありません: {p['sku_main']}")
    if not p["variants"]:
        raise SystemExit("NG: variants が空です")
    if p["is_variation"] and len(p["variants"]) < 2:
        raise SystemExit("NG: is_variation なのに variant が 2 件未満です")
    if not p["is_variation"] and len(p["variants"]) != 1:
        raise SystemExit("NG: 単品なのに variant が複数あります")
    ids = [v.get("variant_id") for v in p["variants"]]
    if len(set(ids)) != len(ids):
        raise SystemExit("NG: variant_id が重複しています")
    for v in p["variants"]:
        label = v.get("selectors") or p["g"]
        st = v.get("stock")
        if not _is_int(st) or not (0 <= st <= 100000):
            raise SystemExit(f"NG: 在庫数は 0〜100000 の整数で指定してください: {label} = {st!r}")
        ep = v.get("estore_price")
        if not _is_int(ep) or not (0 < ep <= 10_000_000):
            raise SystemExit(f"NG: Eストア価格は正の整数で指定してください: {label} = {ep!r}")
        if p["is_variation"] and not v.get("selectors"):
            raise SystemExit(f"NG: バリエーションの selectors(色・サイズ)が空です: {v.get('variant_id')}")
    for key in ("estore_price_main", "lead_time_days"):
        if not _is_int(p[key]) or p[key] <= 0:
            raise SystemExit(f"NG: {key} は正の整数で指定してください: {p[key]!r}")
    y, e = p["yahoo"], p["estore"]
    if y["register"] and not y.get("copy_code"):
        raise SystemExit("NG: Yahoo! を登録するには copy_code が必要です")
    if y.get("copy_code") and str(y["copy_code"]).lower() == p["manage_number"]:
        raise SystemExit("NG: copy_code が自分自身です")
    if y["publish"] and not y["register"]:
        raise SystemExit("NG: Yahoo! を登録しないのに公開が指定されています")
    if y["reserve_publish"] and not y["publish"]:
        raise SystemExit("NG: Yahoo! を公開しないのに店頭反映が指定されています")
    if e["publish"] and not e["register"]:
        raise SystemExit("NG: Eストアを登録しないのに公開が指定されています")


def check_approval(plan: dict) -> None:
    ap = plan.get("approval")
    if not ap:
        raise SystemExit("NG: 承認されていない plan です。ユーザーの承認後に `approve --plan` を実行してください")
    if ap.get("hash") != plan_hash(plan):
        raise SystemExit("NG: 承認後に plan が変更されています。内容を確認し直して `approve` からやり直してください")
    age = datetime.now() - datetime.fromisoformat(ap["approved_at"])
    if age > APPROVAL_TTL:
        raise SystemExit("NG: 承認から 24 時間を超えています。plan を作り直して承認を取り直してください")


# ---------------------------------------------------------------- steps
class Ctx:
    def __init__(self, plan: dict, state_path: Path):
        self.plan = plan
        self.state_path = state_path
        self.state: dict = json.loads(state_path.read_text(encoding="utf-8")) if state_path.exists() else {}
        self.g: str = plan["g"]
        self.mn: str = plan["manage_number"]
        self.main: str = plan["sku_main"]

    def save(self) -> None:
        self.state_path.write_text(json.dumps(self.state, ensure_ascii=False, indent=1), encoding="utf-8")

    def row(self, sku: str) -> sqlite3.Row | None:
        return q_one("SELECT * FROM products WHERE sku=? AND deleted_at IS NULL", (sku,))

    def children(self) -> list[sqlite3.Row]:
        return q_all("SELECT * FROM products WHERE parent_sku=? AND deleted_at IS NULL ORDER BY sku", (self.main,))

    def images(self) -> list[sqlite3.Row]:
        return q_all("SELECT * FROM estore_image_map WHERE sku=?", (self.main,))


def step_import(c: Ctx) -> tuple[str, str]:
    if c.row(c.main):
        detail = f"023 に {c.main} あり"
        status = "SKIP"
    else:
        code, body = http("POST", "/api/rakuten/import",
                          {"manage_number": c.mn, "sku": c.main, "enable_sync": True, "include_images": True})
        if code != 200:
            raise StepFail(f"import HTTP {code}: {str(body)[:200]}")
        if body.get("image_errors"):
            raise StepFail(f"画像取込エラー: {body['image_errors']}")
        detail = f"作成={body.get('created_skus')} 画像={body.get('images_imported')}枚"
        status = "OK"
    if not c.row(c.main):
        raise StepFail("取込後に 023 で見つからない")
    if c.plan["is_variation"]:
        want = {v["variant_id"] for v in c.plan["variants"]}
        got = {str(k["rakuten_variant_id"]) for k in c.children()}
        if got != want:
            raise StepFail(f"子SKUが plan の variant と一致しない (欠け={sorted(want - got)} 余り={sorted(got - want)})")
    n_img = q_one("SELECT COUNT(*) AS n FROM product_images WHERE sku=?", (c.main,))
    if not n_img or n_img["n"] < 1:
        raise StepFail("023 に画像が 1 枚もありません(楽天の画像取込を確認)")
    return status, detail


def _patch(sku: str, fields: dict) -> None:
    code, row = http("GET", f"/api/products/{sku}")
    if code != 200:
        raise StepFail(f"GET {sku} HTTP {code}")
    if all(row.get(k) == v for k, v in fields.items()):
        return
    code, body = http("PATCH", f"/api/products/{sku}", fields, {"If-Match": str(row["version"])})
    if code != 200:
        raise StepFail(f"PATCH {sku} HTTP {code}: {str(body)[:200]}")


def step_prices(c: Ctx) -> tuple[str, str]:
    p = c.plan
    _patch(c.main, {"estore_price": p["estore_price_main"], "lead_time_days": p["lead_time_days"]})
    got = c.row(c.main)
    if got["estore_price"] != p["estore_price_main"] or got["lead_time_days"] != p["lead_time_days"]:
        raise StepFail("価格/納期の読み戻しが一致しない")
    if p["is_variation"]:
        by_id = {v["variant_id"]: v for v in p["variants"]}
        for k in c.children():
            v = by_id.get(str(k["rakuten_variant_id"]))
            if not v:
                raise StepFail(f"子 {k['sku']} に対応する variant が plan にない")
            _patch(k["sku"], {"estore_price": v["estore_price"]})
        for k in c.children():  # 子も読み戻す
            if k["estore_price"] != by_id[str(k["rakuten_variant_id"])]["estore_price"]:
                raise StepFail(f"子 {k['sku']} の estore_price の読み戻しが一致しない")
    return "OK", f"estore_price={got['estore_price']} 納期={got['lead_time_days']}日"


def step_estore_register(c: Ctx) -> tuple[str, str]:
    if not c.plan["estore"]["register"]:
        return "SKIP", "plan で対象外"
    row = c.row(c.main)
    if row["estore_item_code"]:
        return "SKIP", f"登録済み item_code={row['estore_item_code']}"
    code, body = http("POST", f"/api/estore/register/{c.main}")
    if code != 200 or body.get("final_status") != "ok" or body.get("images_failed"):
        raise StepFail(f"Eストア登録 HTTP {code}: {json.dumps(body, ensure_ascii=False)[:300]}")
    row = c.row(c.main)
    if not row["estore_item_code"]:
        raise StepFail("登録後に estore_item_code が空")
    return "OK", f"item_code={body['item_code']} 画像={len(body['images_uploaded'])}枚(非表示で作成)"


def _all_bound(c: Ctx) -> bool:
    imgs = c.images()
    return bool(imgs) and all(r["bound_at"] for r in imgs)


def step_estore_images(c: Ctx) -> tuple[str, str]:
    if not c.plan["estore"]["register"]:
        return "SKIP", "plan で対象外"
    if _all_bound(c):
        return "SKIP", "画像はすでに商品へ紐付け済み(023 の記録)"
    rc, out = run_tool(["tools/estore_admin_session.py", "check"])
    if rc != 0:
        raise NeedsUser("Eストア管理画面のセッション切れ。次を実行してください: "
                        f'python "{PM_ROOT}\\tools\\estore_admin_session.py" login')
    rc, out = run_tool(["tools/estore_ledger_roundtrip.py", "--sku", c.main])
    if rc == 3:
        raise NeedsUser("Eストア管理画面のセッション切れ。estore_admin_session.py login を実行してください")
    if rc == 5:
        raise StepFail("FTP送信済みの画像が未登録リストに無い(FTP未反映・画像名不一致の可能性)。"
                       "Eストア管理画面の画像台帳を確認してください")
    if rc == 6:
        raise NeedsUser("未登録リストに他の商品の画像が含まれています。台帳に一括登録すると他商品も登録されるため、"
                        "内容を確認してから estore_ledger_roundtrip.py --sku <SKU> --apply を実行してください")
    if rc != 0:
        raise StepFail(f"台帳検証 rc={rc}: {out[-300:]}")
    rc, out = run_tool(["tools/estore_ledger_roundtrip.py", "--sku", c.main, "--apply"])
    if rc != 0:
        raise StepFail(f"台帳アップロード rc={rc}: {out[-300:]}")
    return "OK", "画像台帳へ登録"


def step_estore_bind(c: Ctx) -> tuple[str, str]:
    if not c.plan["estore"]["register"]:
        return "SKIP", "plan で対象外"
    if _all_bound(c):
        return "SKIP", "紐付け済み(023 の記録)"
    code, body = http("POST", f"/api/estore/bind-images/{c.main}")
    if code != 200 or not body.get("ok"):
        raise StepFail(f"bind-images HTTP {code}: {str(body)[:200]}")
    if not _all_bound(c):
        raise StepFail("紐付け後も 023 の bound_at が埋まらない")
    return "OK", f"画像 {len(c.images())} 枚を商品に紐付け"


def step_estore_publish(c: Ctx) -> tuple[str, str]:
    if not (c.plan["estore"]["register"] and c.plan["estore"]["publish"]):
        return "SKIP", "plan で対象外(非公開のまま)"
    if c.row(c.main)["estore_status"] != "public":
        code, body = http("POST", f"/api/estore/control/{c.main}", {"display": "Yes"})
        if code != 200:
            raise StepFail(f"control HTTP {code}: {str(body)[:200]}")
        code, body = http("POST", f"/api/estore/show/{c.main}")
        if code != 200:
            raise StepFail(f"show HTTP {code}: {str(body)[:200]}")
    if c.row(c.main)["estore_status"] != "public":
        raise StepFail("公開後も estore_status が public にならない")
    return "OK", "Eストア公開(display=Yes・陳列期間解除)"


def step_yahoo_register(c: Ctx) -> tuple[str, str]:
    y = c.plan["yahoo"]
    if not y["register"]:
        return "SKIP", "plan で対象外"
    info = run_json_tool(["tools/yahoo_publish_tool.py", "item", c.mn])  # 読み取り失敗は例外(fail-closed)
    if info.get("found"):
        return "SKIP", f"Yahoo! に登録済み(内容は verify で確認) display={info.get('display')}"
    code, body = http("POST", f"/api/yahoo/api-sync/{c.main}/register-new",
                      {"copy_code": y["copy_code"], "publish": bool(y["publish"])})
    if code != 200 or body.get("register_status") != "OK" or body.get("image_errors") or body.get("stock_errors"):
        raise StepFail(f"Yahoo登録 HTTP {code}: {json.dumps(body, ensure_ascii=False)[:300]}")
    info = run_json_tool(["tools/yahoo_publish_tool.py", "item", c.mn])
    want = "1" if y["publish"] else "0"
    problems = []
    if str(info.get("display")) != want:
        problems.append(f"display={info.get('display')}")
    if not info.get("has_explanation"):
        problems.append("説明文なし")
    if not info.get("postage_set"):
        problems.append("送料未設定")
    if problems:
        raise StepFail("Yahoo読み戻し不一致: " + ", ".join(problems))
    return "OK", f"画像={len(body['uploaded_images'])}枚 display={info['display']} 送料グループ={info['postage_set']}"


def _yahoo_on_storefront(c: Ctx) -> bool:
    code, html = fetch_text(STORE_YAHOO.format(code=c.mn))
    key = (c.plan.get("title") or "")[:12]
    return code == 200 and bool(key) and key in html


def step_yahoo_reserve(c: Ctx) -> tuple[str, str]:
    y = c.plan["yahoo"]
    if not (y["register"] and y["publish"] and y["reserve_publish"]):
        return "SKIP", "plan で対象外(店頭反映は行わない)"
    if _yahoo_on_storefront(c):
        return "SKIP", "Yahoo! 店頭に出ている(反映済み)"
    if not c.state.get("yahoo_reserve_detail"):
        now = run_json_tool(["tools/yahoo_publish_tool.py", "summary"])
        base = (y.get("pending_before") or {}).get("item_pages", 0)
        allow = y.get("pending_allow", YAHOO_PENDING_ALLOW)
        if now.get("pending_rows", 0) == 0:
            return "SKIP", "未反映の項目なし"
        if now["pending_item_pages"] > base + allow:
            raise NeedsUser(
                f"Yahoo! の未反映ページが想定より多い(下見 {base} → 現在 {now['pending_item_pages']}、許容 +{allow})。"
                "他の担当者の編集が含まれる可能性があるため反映しません。Yahoo!ストアクリエイターProで内容を"
                "確認して人が反映するか、了承のうえ plan の yahoo.pending_allow を上げて承認し直してください")
        res = run_json_tool(["tools/yahoo_publish_tool.py", "reserve", "--confirm-store-wide"])
        if res.get("status") != "OK" or res.get("errors"):
            raise StepFail(f"店頭反映が完了しない: {res}")
        c.state["yahoo_reserve_detail"] = res  # 反映を実行した事実を、店頭確認より先に残す
        c.save()
    for _ in range(12):  # 反映は非同期。店頭ページが開くまで最大 3 分待つ
        if _yahoo_on_storefront(c):
            before = (c.state.get("yahoo_reserve_detail") or {}).get("before")
            return "OK", f"店頭反映済み (反映前の未反映={before})"
        time.sleep(15)
    raise StepFail("反映を実行したが Yahoo! 店頭ページを確認できない(数分後に verify を再実行)")


def _wait_queue(job_id: int, timeout_s: int = 420) -> dict:
    end = time.time() + timeout_s
    while time.time() < end:
        code, st = http("GET", "/api/queue/status")
        if code == 200:
            for h in st.get("history", []):
                if h["id"] == job_id:
                    return h
        time.sleep(10)
    raise StepFail(f"キュー job#{job_id} が {timeout_s} 秒で終わらない(GoQ 側の状態を確認)")


def _goq_all_ok(c: Ctx) -> tuple[bool, dict]:
    code, body = http("GET", f"/api/goq/status/{c.main}")
    det = body.get("detail") if isinstance(body, dict) else None
    ok = code == 200 and isinstance(body, dict) and body.get("ok") is True and bool(det) \
        and all(v is True for v in det.values())
    return ok, (body if isinstance(body, dict) else {})


def step_goq_resync(c: Ctx) -> tuple[str, str]:
    if not c.plan["goq"]["resync"]:
        return "SKIP", "plan で対象外"
    ok, body = _goq_all_ok(c)
    if ok:
        return "SKIP", "GoQ 連携判定はすでに全モール OK"
    last = ""
    for _ in (1, 2):  # 自動ログインの一時失敗に 1 回だけ再実行 (連続失敗はロックを招くので 2 回まで)
        code, q = http("POST", "/api/queue/goq-resync", {"item_code": c.g})
        if code != 200:
            raise StepFail(f"enqueue HTTP {code}: {str(q)[:200]}")
        job = _wait_queue(q["id"])
        if job["status"] == "ok":
            time.sleep(20)
            ok, body = _goq_all_ok(c)
            if ok:
                return "OK", f"GoQ連携判定 {body.get('detail')}"
            raise StepFail(f"resync は ok だが連携判定が全OKでない: {body}")
        last = job.get("error_msg") or ""
        if "自動ログインに失敗" not in last:
            break
        time.sleep(60)
    raise StepFail(f"GoQ在庫連携 失敗: {last[:200]}")


def _goq_rows(c: Ctx) -> list[dict]:
    """GoQ の検索結果の行 (読み取り)。キューが空のときだけ呼ぶ."""
    code, st = http("GET", "/api/queue/status")
    if st.get("running") or st.get("queued"):
        raise StepFail("キューが動いているため GoQ 検索を待機")
    code, res = http("POST", "/api/goq-browser/search", {"item_code": c.g})
    if code != 200:
        raise StepFail(f"GoQ検索 HTTP {code}: {str(res)[:200]}")
    rows = res.get("rows") or []
    return [{"value": str(r.get("value", "")), "text": json.dumps(r, ensure_ascii=False)} for r in rows]


def _plan_goq_jobs(c: Ctx, rows: list[dict]) -> list[tuple[str, int]]:
    """variant ごとに書き込む GoQ の行を特定する。曖昧なら NEEDS_USER (誤書き込みより停止を選ぶ)."""
    p = c.plan
    if not p["is_variation"]:
        if len(rows) != 1:
            raise NeedsUser(f"GoQ の対象行が {len(rows)} 行あります(単品は 1 行のはず)。"
                            f"行を確認してください: {[r['value'] for r in rows]}")
        if c.g.lower() not in rows[0]["text"].lower():
            raise NeedsUser(f"GoQ の行に商品番号 {c.g} が見当たりません。別の商品の行の可能性があります: "
                            f"{rows[0]['text'][:120]}")
        return [(rows[0]["value"], p["variants"][0]["stock"])]
    if len(rows) != len(p["variants"]):
        raise NeedsUser(
            f"GoQ の行数({len(rows)})が variant 数({len(p['variants'])})と一致しません。別チャネルの行が混ざっている"
            f"可能性があるため、対応を確認してください。行={[r['text'][:90] for r in rows]}")
    jobs, used = [], set()
    for v in p["variants"]:
        tokens = [t for t in v["selectors"].values() if t]
        hit = [r for r in rows if all(t in r["text"] for t in tokens)]
        if len(hit) != 1 or hit[0]["value"] in used:
            raise NeedsUser(
                "GoQ の行を variant に一意に対応づけられません(初回のバリエーション商品は対応を確認してください)。"
                f"variant={v['selectors']} 候補={[r['value'] for r in hit]} 全行={[r['text'][:90] for r in rows]}")
        used.add(hit[0]["value"])
        jobs.append((hit[0]["value"], v["stock"]))
    return jobs


def step_goq_stock(c: Ctx) -> tuple[str, str]:
    progress: dict = c.state.setdefault("goq_stock_progress", {})
    rows = _goq_rows(c)
    jobs = _plan_goq_jobs(c, rows)
    done = []
    for row_id, qty in jobs:
        if progress.get(row_id) == qty:
            done.append(f"{row_id}={qty}(済)")
            continue
        code, q = http("POST", "/api/queue/set-quantity", {"item_code": c.g, "quantity": int(qty), "row_ids": [row_id]})
        if code != 200:
            raise StepFail(f"set-quantity enqueue HTTP {code}: {str(q)[:200]}")
        job = _wait_queue(q["id"])
        res = job.get("result") or {}
        if job["status"] != "ok" or res.get("row_count") != 1 or res.get("quantity") != int(qty) \
                or res.get("target_row_ids") != [row_id]:
            raise StepFail(f"在庫数の反映に失敗 row={row_id}: {job.get('error_msg')} "
                           f"{json.dumps(res, ensure_ascii=False)[:200]}")
        progress[row_id] = qty  # 行ごとに進捗を残す(途中で落ちても済んだ行を再実行しない)
        c.save()
        done.append(f"{row_id}={qty}")
    return "OK", "在庫数を反映: " + ", ".join(done)


def step_verify(c: Ctx) -> tuple[str, str]:
    p, issues, facts = c.plan, [], []
    key = (p.get("title") or "")[:12]
    if p["estore"]["register"] and p["estore"]["publish"]:
        code, html = fetch_text(STORE_SHOPSERVE.format(code=c.g))
        if code != 200:
            issues.append(f"Eストア店頭 HTTP {code}")
        else:
            if key and key not in html:
                issues.append("Eストア: ページに商品名が見つからない(別ページの可能性)")
            if "limg/noimage.gif" in html:
                issues.append("Eストア: 画像なし表示")
            price_txt = f"{p['estore_price_main']:,}円"
            if price_txt not in html:
                issues.append(f"Eストア: 価格 {price_txt} が見つからない")
            n_img = len(set(re.findall(rf"{c.g}_\d+\.jpg", html)))
            if n_img < 1:
                issues.append("Eストア: 商品画像の参照が見つからない")
            facts.append(f"Eストア店頭OK(画像{n_img}枚)" + ("(在庫切れ表示)" if "btn_nostock" in html else ""))
    if p["yahoo"]["register"] and p["yahoo"]["publish"] and p["yahoo"]["reserve_publish"]:
        code, html = fetch_text(STORE_YAHOO.format(code=c.mn))
        if code != 200 or (key and key not in html):
            issues.append(f"Yahoo!店頭が確認できない(HTTP {code})")
        else:
            facts.append("Yahoo!店頭OK" + ("(在庫切れ表示)" if re.search("在庫なし|売り切れ|在庫切れ", html) else ""))
    if p["goq"]["resync"]:
        ok, body = _goq_all_ok(c)
        if not ok:
            issues.append(f"GoQ連携判定NG: {body}")
        else:
            facts.append(f"GoQ連携 {body.get('detail')}")
    if issues:
        raise StepFail("; ".join(issues))
    return "OK", " / ".join(facts)


STEPS: list[tuple[str, Callable[[Ctx], tuple[str, str]]]] = [
    ("import", step_import),
    ("prices", step_prices),
    ("estore_register", step_estore_register),
    ("estore_images", step_estore_images),
    ("estore_bind", step_estore_bind),
    ("estore_publish", step_estore_publish),
    ("yahoo_register", step_yahoo_register),
    ("yahoo_reserve", step_yahoo_reserve),
    ("goq_resync", step_goq_resync),
    ("goq_stock", step_goq_stock),
    ("verify", step_verify),
]


# ------------------------------------------------------------------- run
def _acquire_lock(path: Path) -> None:
    if path.exists():
        age = datetime.now() - datetime.fromtimestamp(path.stat().st_mtime)
        if age < LOCK_STALE:
            raise SystemExit(f"NG: 同じ plan が実行中です(ロック {path.name})。"
                             "実行中でなければ、ロックファイルを削除してください")
        path.unlink()
    fd = os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    os.write(fd, str(os.getpid()).encode("ascii"))
    os.close(fd)


def run(plan_path: Path, only: str | None, reconcile: bool = False) -> int:
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    validate_plan(plan)
    read_only = only == "verify"
    if not read_only:
        check_approval(plan)
    c = Ctx(plan, plan_path.with_suffix(".state.json"))
    if not read_only:
        h = plan_hash(plan)
        if c.state.get("_plan_hash") not in (None, h):
            raise SystemExit("NG: この state は別の内容の plan で作られています。plan を作り直してください")
        all_done = all((c.state.get(n) or {}).get("status") in ("OK", "SKIP") for n, _ in STEPS)
        if not only and not reconcile and all_done:
            raise SystemExit("NG: この plan は完了済みです。確認だけなら `verify`、"
                             "意図して再確認するなら `--reconcile` を付けてください")
        c.state["_plan_hash"] = h
    lock = plan_path.with_suffix(".lock")
    if not read_only:
        _acquire_lock(lock)
    try:
        for name, fn in STEPS:
            if only and name != only:
                continue
            try:
                status, detail = fn(c)
            except NeedsUser as e:
                print(f"STEP {name}: NEEDS_USER {e}")
                c.state[name] = {"status": "NEEDS_USER", "detail": str(e), "at": datetime.now().isoformat()}
                c.save()
                return EXIT_NEEDS_USER
            except StepFail as e:
                print(f"STEP {name}: FAIL {e}")
                c.state[name] = {"status": "FAIL", "detail": str(e), "at": datetime.now().isoformat()}
                c.save()
                return 1
            except Exception as e:  # noqa: BLE001 — 想定外でも state を残して報告する
                msg = f"想定外のエラー {type(e).__name__}: {str(e)[:200]}"
                print(f"STEP {name}: FAIL {msg}")
                c.state[name] = {"status": "FAIL", "detail": msg, "at": datetime.now().isoformat()}
                c.save()
                return 1
            print(f"STEP {name}: {status} {detail}")
            c.state[name] = {"status": status, "detail": detail, "at": datetime.now().isoformat()}
            c.save()
    finally:
        if not read_only and lock.exists():
            lock.unlink()
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    p1 = sub.add_parser("preflight")
    p1.add_argument("g")
    p_ap = sub.add_parser("approve")
    p_ap.add_argument("--plan", required=True)
    p2 = sub.add_parser("run")
    p2.add_argument("--plan", required=True)
    p2.add_argument("--only")
    p2.add_argument("--reconcile", action="store_true")
    p3 = sub.add_parser("verify")
    p3.add_argument("--plan", required=True)
    a = ap.parse_args()
    RUNS_DIR.mkdir(parents=True, exist_ok=True)
    if a.cmd == "preflight":
        out = preflight(a.g)
        path = RUNS_DIR / f"{a.g.upper()}_{datetime.now():%Y%m%d_%H%M%S}.plan.json"
        path.write_text(json.dumps(out["draft_plan"], ensure_ascii=False, indent=1), encoding="utf-8")
        out["draft_plan_path"] = str(path)
        print(json.dumps(out, ensure_ascii=False, indent=1))
        return 0
    if a.cmd == "approve":
        path = Path(a.plan)
        plan = json.loads(path.read_text(encoding="utf-8"))
        validate_plan(plan)
        plan["approval"] = {"hash": plan_hash(plan), "approved_at": datetime.now().isoformat(timespec="seconds")}
        path.write_text(json.dumps(plan, ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"OK: 承認を記録しました(24時間有効) hash={plan['approval']['hash'][:12]}")
        return 0
    if a.cmd == "verify":
        return run(Path(a.plan), "verify")
    return run(Path(a.plan), a.only, a.reconcile)


if __name__ == "__main__":
    sys.exit(main())
