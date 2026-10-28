"""新商品登録パイプライン (楽天登録済みの G 番号 → 023 / Eストア / Yahoo! / 092 自前在庫管理 / 在庫数).

2026-10-26〜 新商品の在庫は GoQ に登録しない(運用者決定)。092(zaiko.sfuji.f5.si)に載せ、台帳の数を各モールへ配る。
GoQ の手順(goq_resync・goq_stock)は、それより前に作った plan(goq.resync=true)の再開のためだけに残してある。

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
import urllib.parse
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
STORE_RAKUTEN = "https://item.rakuten.co.jp/seifukunofuji/{code}/"


def store_links(plan: dict) -> list[tuple[str, str]]:
    """完了報告の最後に貼る販売ページのリンク(ユーザーが実際に開いて目視確認する。2026-09-28 ユーザー指示).

    登録・公開の対象にしたモールだけを返す。楽天は本スキルの登録対象ではないが元商品として必ず含める。
    """
    links = [("楽天", STORE_RAKUTEN.format(code=plan["manage_number"]))]
    if plan["estore"]["register"]:
        links.append(("Eストア", STORE_SHOPSERVE.format(code=plan["g"])))
    if plan["yahoo"]["register"]:
        links.append(("Yahoo!", STORE_YAHOO.format(code=plan["manage_number"])))
    return links
TAX = 1.1
EXIT_NEEDS_USER = 10
APPROVAL_TTL = timedelta(hours=24)
LOCK_STALE = timedelta(hours=2)
YAHOO_PENDING_ALLOW = 4  # 下見のときの未反映ページ数に、今回の登録分として許す増加数
G_RE = re.compile(r"^[A-Z]\d{4}$")  # 商品番号は英字1文字+4桁(G・U・H・R など 2026-10-28)


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


def _dict_body(body: Any) -> dict:
    """http() の応答が JSON オブジェクトであることを要求する(サーバーが例外的にプレーンテキストや

    HTML を 200 で返した場合に、その場の body.get() で AttributeError になるのを防ぐ)。
    """
    if not isinstance(body, dict):
        raise StepFail(f"サーバー応答が JSON オブジェクトでない: {str(body)[:200]}")
    return body


def plan_hash(plan: dict) -> str:
    body = {k: v for k, v in plan.items() if k != "approval"}
    return hashlib.sha256(json.dumps(body, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()


# ------------------------------------------------------------- preflight
def rakuten_remote(mn: str) -> dict | None:
    """楽天の商品情報 (読み取りのみ)。023 サーバーのモジュールを直接使う."""
    sys.path.insert(0, str(PM_ROOT))
    from server.services import rakuten_sync
    return rakuten_sync.get_remote(mn)


def rakuten_fingerprint(remote: dict) -> str:
    """楽天側の内容指紋。承認後に楽天RMSが変更されていないかの検知に使う(023の既存関数を再利用)."""
    sys.path.insert(0, str(PM_ROOT))
    from server.services import rakuten_sync
    return rakuten_sync.canonical_hash(remote)


def rakuten_field_hashes(remote: dict) -> dict:
    """項目ごとの短い指紋。下見の時点のものを plan に残し、あとで「どの項目が変わったか」を示すために使う(2026-10-23)。"""
    sys.path.insert(0, str(PM_ROOT))
    from server.services import rakuten_sync
    return {k: hashlib.sha256(json.dumps(remote.get(k), sort_keys=True, ensure_ascii=False,
                                         separators=(",", ":")).encode("utf-8")).hexdigest()[:8]
            for k in rakuten_sync.HASHED_FIELDS}


FIELD_LABELS = {"title": "商品名", "tagline": "キャッチコピー", "price": "価格", "inventories": "在庫",
                "images": "画像", "productDescription": "商品説明", "variants": "バリエーション"}


def changed_rakuten_fields(plan: dict, remote: dict) -> str:
    """下見の後に楽天側で変わった項目の名前(「商品説明・画像」など)。記録が無い古い plan は「不明」。"""
    old = plan.get("rakuten_field_hashes")
    if not isinstance(old, dict):
        return "どの項目かは記録がありません"
    now = rakuten_field_hashes(remote)
    names = [FIELD_LABELS.get(k, k) for k, v in now.items() if old.get(k) != v]
    return "・".join(names) if names else "項目の指紋は同じ"


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


THIN_PATHS = {"【ネコポス可】"}  # これだけでは商品カテゴリにも組織フォルダにも出ない

# 楽天の紐づけから Yahoo! の紐づけを決める部品(099 カテゴリー統一・2026-10-23)
CAT_SYNC_DIR = Path(r"C:\ClaudeCode\099.ECサイトカテゴリー統一プロジェクト")


def rakuten_shipping_groups(remote: dict) -> set[str]:
    """楽天の配送方法グループ('1'=メール便)。023 rakuten_sync と同じ読み方。"""
    variants = remote.get("variants") or {}
    items = variants.values() if isinstance(variants, dict) else variants
    return {str((v.get("shipping") or {}).get("shippingMethodGroup")) for v in items
            if isinstance((v.get("shipping") or {}).get("shippingMethodGroup"), (str, int))}


def yahoo_paths_from_rakuten(mn: str, remote: dict) -> dict:
    """楽天に登録済みの紐づけを対応表で Yahoo! のパスに直す(読み取りのみ)。

    099 の cat_sync.derive に任せる: Yahoo! に作らないカテゴリーは外す / 名前を変えたものは変えた後 /
    【ネコポス可】はメール便のときだけ / 送料無料は付けずに要確認 / Yahoo! にまだ無いものは外して記録 /
    実体(1行目)は商品分類・組織の下で一番深いもの。決められなければ status が ok 以外になる。
    """
    groups = rakuten_shipping_groups(remote)
    if len(groups) > 1:
        return {"status": "needs_check", "detail": f"バリエーションで配送方法グループが違います: {sorted(groups)}"}
    args = [sys.executable, "-m", "cat_sync.derive", "paths", mn]
    if groups:
        args += ["--shipping-group", next(iter(groups))]
    try:
        done = subprocess.run(args, cwd=CAT_SYNC_DIR, capture_output=True, timeout=600,
                              creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except subprocess.TimeoutExpired:
        return {"status": "failed", "detail": "楽天・Yahoo! の読み取りが時間内に終わりませんでした"}
    lines = done.stdout.decode("utf-8", errors="replace").strip().splitlines()
    try:
        return json.loads(lines[-1])
    except (ValueError, IndexError):
        return {"status": "failed", "detail": done.stderr.decode("utf-8", errors="replace")[-300:]}


def path_lines(path: str | None) -> list[str]:
    return [p.strip() for p in (path or "").split("\n") if p.strip()]


def yahoo_copy_candidates(title: str, is_variation: bool, exclude: str = "", limit: int = 60) -> list[dict]:
    """Yahoo! 新規登録のコピー元候補を「紐付くパスの組み合わせ」ごとにまとめて返す.

    コピー元から引き継ぐのは **path(ストア内カテゴリ)と product_category だけ**
    (`yahoo_register_export.COPY_FIELDS`)。名前・価格・説明文は楽天の内容が入る。
    したがって候補は価格ではなく**どのフォルダへ紐付くか**で選ぶ(2026-09-28 G2231 でユーザー指摘:
    「コピー元と同じフォルダに紐付くなら、陸上部隊や潜水艦が護衛艦と紐付く不整合が起きる。
    パスを見せてもらわないと判断できない」)。旧版は同価格・新しい順で、実際に g2225 等
    「【ネコポス可】」しか持たない商品を推奨しており、G2225 はワッペンのカテゴリにも出ない状態で登録された。

    - 同じパス集合の商品は1グループにまとめ、件数と代表コード(新しい順)を返す
    - `org_paths`(組織・キャラで選ぶ…)が艦・部隊の紐付き先。Claude が商品に合うものを選び、
      質問ではパス全文を必ず見せる
    - パスが【ネコポス可】だけのグループは `thin_path=True` として末尾に回す(推奨しない)

    バリエーション判定は `yahoo_snapshot_variants` の有無で行う(variation{1-5}_name 列は実データで
    常に空。2026-09-28 G2230 で発覚)。
    """
    keywords = [k for k in ("ワッペン", "Tシャツ", "コイン", "帽子", "キャップ", "ポロシャツ", "パーカー",
                            "タオル", "ぬいぐるみ", "バッグ", "ステッカー") if k in title]
    out = _yahoo_copy_groups(keywords, is_variation, exclude)
    out.sort(key=lambda d: (d["thin_path"], -d["count"], -int(re.sub(r"\D", "", d["code"]) or 0)))
    return out[:limit]


def _yahoo_copy_groups(keywords: list[str], is_variation: bool, exclude: str) -> list[dict]:
    """Yahoo! の既存商品を「商品カテゴリ+紐付くパスの組み合わせ」ごとにまとめる(並べ替え・件数の打ち切りはしない)。"""
    rows = q_all("SELECT code, name, path, product_category FROM yahoo_snapshot_products")
    variant_codes = {(r["code"] or "").lower() for r in q_all("SELECT DISTINCT code FROM yahoo_snapshot_variants")}
    groups: dict[tuple, dict] = {}
    for r in rows:
        name = r["name"] or ""
        if keywords and not any(k in name for k in keywords):
            continue
        code_l = (r["code"] or "").lower()
        if (code_l in variant_codes) != is_variation or code_l == exclude.lower():
            continue
        lines = path_lines(r["path"])
        if not lines:
            continue
        key = (str(r["product_category"] or ""), tuple(sorted(lines)))
        g = groups.setdefault(key, {"codes": [], "names": {}, "lines": lines})
        g["codes"].append(r["code"])
        g["names"][r["code"]] = name
    out = []
    for (cat, key_lines), g in groups.items():
        codes = sorted(g["codes"], key=lambda c: -int(re.sub(r"\D", "", c) or 0))
        rep = codes[0]
        out.append({
            "code": rep, "name": g["names"][rep][:50], "product_category": cat,
            "count": len(codes), "other_codes": codes[1:4],
            "thin_path": set(key_lines) <= THIN_PATHS,
            "org_paths": [p for p in g["lines"] if p.startswith("組織・キャラ")],
            "paths": g["lines"],
        })
    return out


def _name_tokens(text: str) -> set[str]:
    """商品名の語(2文字以上)。名前が近いコピー元を選ぶために使う。"""
    return {t for t in re.split(r"[\s　()（）【】\[\]「」・、,/]+", text or "") if len(t) >= 2}


def yahoo_copy_candidates_for_paths(paths: list[str], is_variation: bool, title: str = "",
                                    exclude: str = "", limit: int = 10) -> list[dict]:
    """選んだ紐付くパスを持つコピー元を、全商品から探す(件数の上位で打ち切らない・2026-10-28 受付 #42)。

    下見の候補(yahoo_copy_candidates)は件数の多い組み合わせ60件で打ち切るため、身分証ケースのように
    件数の少ない組み合わせが候補に入らず「指定のパスを持つ既存商品が無い」と誤って止まった(U0153)。
    並べ方: 選んだパスを全部持つ → 商品名の語が多く重なる → 選んでいないパスが少ない → 件数が多い。
    """
    want = {p.strip() for p in paths if p.strip()}
    if not want:
        return []
    title_tokens = _name_tokens(title)
    scored = []
    for c in _yahoo_copy_groups([], is_variation, exclude):
        have = set(c["paths"])
        overlap = len(want & have)
        if not overlap:
            continue
        name_sim = len(title_tokens & _name_tokens(c["name"]))
        extra = len((have - want) - THIN_PATHS)
        scored.append(((not want <= have, -name_sim, -overlap, extra, c["thin_path"], -c["count"]), c))
    scored.sort(key=lambda x: x[0])
    return [c for _, c in scored[:limit]]


YAHOO_ITEM_SEARCH = "https://shopping.yahooapis.jp/ShoppingWebService/V3/itemSearch"
YAHOO_CATEGORY_SEARCH = "https://shopping.yahooapis.jp/ShoppingWebService/V1/json/categorySearch"
YAHOO_SELLER = "seifukunofuji"


def _yahoo_appid() -> str:
    """Yahoo! の公開 API のアプリID(023 の YAHOO_API_CLIENT_ID)。値は表示しない。"""
    v = os.getenv("YAHOO_API_CLIENT_ID", "")
    if not v:
        env = PM_ROOT / ".env"
        for line in env.read_text(encoding="utf-8").splitlines() if env.is_file() else []:
            if line.startswith("YAHOO_API_CLIENT_ID="):
                v = line.split("=", 1)[1].strip().strip('"').strip("'")
    return v


def _get_json(url: str, params: dict) -> tuple[int, Any]:
    full = url + "?" + urllib.parse.urlencode(params)
    try:
        with urllib.request.urlopen(full, timeout=30) as r:
            return r.status, json.load(r)
    except urllib.error.HTTPError as e:
        return e.code, None
    except (urllib.error.URLError, TimeoutError, ValueError):
        return 0, None


def yahoo_genre_check(code: str) -> dict:
    """コピー元に Yahoo! が付けているカテゴリ(ジャンル)を API で読み、そのカテゴリが今も使えるかを確かめる(読み取りのみ)。

    店の商品データの product_category(例 40863)は公開 API のカテゴリとは別の番号なので直接は確かめられない。
    そこで商品検索 API(V3 itemSearch)でコピー元の商品に付いているカテゴリを読み、カテゴリID取得 API(V1 categorySearch)で
    そのカテゴリが今も有効か(同じ ID が返るか)を確かめる。新しい商品はコピー元の product_category を引き継ぐので、
    同じカテゴリに入る。2026-10-28 運用者指示「API でそのカテゴリーが使用可能であることを確認してから進める」
    (U0151 がワッペンのカテゴリに入っていた件)。
    戻り値: {"ok", "code", "id", "name", "path", "detail"}
    """
    appid = _yahoo_appid()
    if not appid:
        return {"ok": False, "code": code, "detail": "Yahoo! のアプリIDが読めません"}
    st, d = _get_json(YAHOO_ITEM_SEARCH, {"appid": appid, "seller_id": YAHOO_SELLER, "query": code, "results": 20})
    if st != 200 or not isinstance(d, dict):
        return {"ok": False, "code": code, "detail": f"商品検索 API が読めません(HTTP {st})"}
    want = f"{YAHOO_SELLER}_{code.lower()}"
    hit = next((h for h in d.get("hits") or [] if str(h.get("code", "")).lower() == want), None)
    if hit is None:
        return {"ok": False, "code": code, "detail": "商品検索 API でコピー元の商品が見つかりません(非公開・在庫切れ等)"}
    genre = hit.get("genreCategory") or {}
    gid = str(genre.get("id") or "")
    if not gid:
        return {"ok": False, "code": code, "detail": "コピー元にカテゴリが付いていません"}
    names = [str(x.get("name") or "") for x in hit.get("parentGenreCategories") or []] + [str(genre.get("name") or "")]
    st2, c = _get_json(YAHOO_CATEGORY_SEARCH, {"appid": appid, "category_id": gid})
    cur = (((c or {}).get("ResultSet") or {}).get("0") or {}).get("Result", {}).get("Categories", {}).get("Current", {}) \
        if isinstance(c, dict) else {}
    if st2 != 200 or str(cur.get("Id") or "") != gid:
        return {"ok": False, "code": code, "id": gid, "name": genre.get("name"),
                "detail": f"カテゴリ {gid} がカテゴリID取得 API で確かめられません(HTTP {st2})"}
    return {"ok": True, "code": code, "id": gid, "name": str(genre.get("name") or ""),
            "path": " > ".join(n for n in names if n), "detail": ""}


def store_path_options() -> list[str]:
    """既存商品で使われている紐付くパスの一覧(選べるパスの例。存在確認には使わない・2026-10-23)。"""
    out: set[str] = set()
    for r in q_all("SELECT path FROM yahoo_snapshot_products WHERE path IS NOT NULL"):
        out.update(path_lines(r["path"]))
    return sorted(out)


def pin_copy_source_paths(plan: dict) -> None:
    """承認時にコピー元のパスを plan へ固定する(承認ハッシュの対象になる).

    - コピー元が 023 のスナップショットに無い → 拒否(register-new が拒否するため。API で取り込む)
    - パスが【ネコポス可】だけ → 拒否(`yahoo.allow_thin_path: true` を明示した場合だけ通す)
    - 固定したパスは `step_yahoo_register` が登録後の読み戻しと突き合わせる
    """
    y = plan["yahoo"]
    if not y.get("register"):
        return
    row = q_one("SELECT path FROM yahoo_snapshot_products WHERE lower(code)=?", (str(y["copy_code"]).lower(),))
    if row is None:
        raise SystemExit(f"NG: コピー元 {y['copy_code']} が023の yahoo_snapshot_products にありません"
                         "(Yahoo!に実在するなら get_item で取得して取り込む。SKILL.md 参照)")
    if y.get("paths"):
        # パスを直接指定した場合: コピー元からは product_category だけを使う(2026-10-22)
        # 実在は Yahoo! の今の木で確かめる(2026-10-23。スナップショットには後から作ったカテゴリーや
        # 商品の無いカテゴリーが無いため)。023 の register-new も同じ確かめ方をする
        want = sorted({p.strip() for p in y["paths"]})
        try:
            res = run_json_tool(["-m", "server.services.yahoo_store_tree", "check", *want])
        except StepFail as e:
            raise SystemExit(f"NG: Yahoo! のカテゴリー一覧を読めませんでした。少し待ってからやり直してください: {e}")
        if res.get("missing"):
            raise SystemExit(f"NG: Yahoo! のストアに無いパスです: {res['missing']}")
        y["expected_paths"] = want
        return
    lines = path_lines(row["path"])
    if set(lines) <= THIN_PATHS and not y.get("allow_thin_path"):
        raise SystemExit(f"NG: コピー元 {y['copy_code']} のパスが {lines} だけで、ワッペン等のカテゴリにも"
                         "組織フォルダにも出ません。別のコピー元を選ぶ(意図的なら yahoo.allow_thin_path=true)")
    y["expected_paths"] = sorted(lines)


def estore_name_examples(title: str, limit: int = 6) -> list[dict]:
    """Eストア表示名を短くする際の参考例(実在の兄弟SKU)。そのまま使う候補ではなく、

    文体の参考にする(2026-09-25 の教訓: 機械的なテンプレート適用は誤変換を生む。
    feedback_naming_use_sibling_clean_template 参照)。
    """
    keywords = [k for k in ("ワッペン", "Tシャツ", "コイン", "帽子", "識別帽", "キャップ", "ポロシャツ",
                            "パーカー", "タオル", "ぬいぐるみ", "バッグ", "ステッカー", "ポーチ", "ベルト",
                            "短靴") if k in title]
    if not keywords:
        return []
    rows = q_all("SELECT sku, product_name, estore_product_name FROM products "
                 "WHERE estore_product_name IS NOT NULL AND estore_product_name != product_name "
                 "AND deleted_at IS NULL")
    out = []
    for r in rows:
        if any(k in (r["estore_product_name"] or "") or k in (r["product_name"] or "") for k in keywords):
            out.append({"sku": r["sku"], "short_name": r["estore_product_name"], "length": len(r["estore_product_name"])})
    return out[:limit]


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
        flags.append("バリエーション商品は初回の実機検証が済んでいません(試験運用): 092 の書き先(色・サイズ)の確認などで途中で確認が入る可能性があります")
    y_pending = run_json_tool(["tools/yahoo_publish_tool.py", "summary"])
    pending_rows = y_pending.get("pending_rows", 0)
    if pending_rows:
        flags.append(f"Yahoo!に他の未反映が {pending_rows} 件あります。店頭反映するとこれらも一緒に公開されます(反映は人が行う設定を推奨)")
    y_from_rakuten = yahoo_paths_from_rakuten(mn, remote)
    if y_from_rakuten.get("status") == "ok":
        for p in y_from_rakuten.get("missing") or []:
            flags.append(f"Yahoo! にまだ無いカテゴリーなので外して登録します(カテゴリー統一の段階で付け替え): {p}")
        for p in y_from_rakuten.get("needs_check") or []:
            flags.append(f"Yahoo! の紐づけの要確認: {p}")
    else:
        flags.append("楽天から Yahoo! の紐づけを決められませんでした("
                     f"{y_from_rakuten.get('detail') or y_from_rakuten.get('needs_check')})。コピー元の紐づけで登録します")
    name_examples = estore_name_examples(title)
    flags.append("Eストア表示名は登録時点で短く決める(未設定のまま登録すると、後で管理画面から手動で"
                "直すことになり023の記録と食い違う実例があった: G2225 2026-09-25)。楽天の長い商品名を"
                "そのまま使わないこと")
    draft = {
        "g": g, "manage_number": mn, "sku_main": sku_main, "is_variation": is_var, "title": title,
        "variants": [{**v, "estore_price": excl_tax(v["price"]) if v["price"] else None,
                     "stock": 0}  # 在庫数は必ず0で登録する(絶対ルール・2026-09-28 ユーザー決定。質問しない)
                     for v in variants],
        "estore_price_main": excl_tax(min(prices)) if prices else None,
        "estore_product_name": None,
        "rakuten_fingerprint": rakuten_fingerprint(remote),
        "rakuten_field_hashes": rakuten_field_hashes(remote),
        "lead_time_days": 5,
        "estore": {"register": True, "publish": True},
        "yahoo": {"register": True, "copy_code": None, "publish": True,
                  "reserve_publish": pending_rows == 0,
                  "pending_before": {"rows": pending_rows, "item_pages": y_pending.get("pending_item_pages", 0)},
                  "pending_allow": YAHOO_PENDING_ALLOW},
        # 新商品の在庫は GoQ に登録しない(2026-10-26 運用者決定)。092 に載せる。initial_stock は台帳に入れる数
        # (0=入れない。単品は整数、バリエーションは {SKU: 数})。モールは常に在庫0で登録し、092 から配る
        "goq": {"resync": False},
        "zaiko": {"register": True, "initial_stock": 0},
        "not_included": ["Amazon (amz-register)", "LCL", "7S(コマースロボ)"],
        "created_at": datetime.now().isoformat(timespec="seconds"),
    }
    if y_from_rakuten.get("status") == "ok":  # 紐づけは楽天から決める。コピー元は product_category だけに使う
        draft["yahoo"]["paths"] = y_from_rakuten["paths_api"]  # 023 の書き方(":" 区切り・1行目が実体)
        draft["yahoo"]["paths_source"] = "rakuten"
    return {
        "draft_plan": draft,
        "already_in_023": existing,
        # 楽天の紐づけから決めた Yahoo! の紐づけ(paths=登録する・missing=Yahoo! に無いので外した・
        # dropped=外した理由つき・needs_check=要確認)。2026-10-23 カテゴリー統一
        "yahoo_paths_from_rakuten": y_from_rakuten,
        "yahoo_copy_candidates": yahoo_copy_candidates(title, is_var, exclude=mn),
        # 紐付くパスを直接指定するときに選べるパス(ストアに実在するもの。2026-10-22)
        "yahoo_path_options": store_path_options(),
        "estore_name_examples": name_examples,
        "flags": flags,
        "questions": ["Eストア表示名(短く。参考例=estore_name_examples。楽天の長い商品名は使わない)",
                      "Eストア価格(既定: 楽天税込価格÷1.1)と納期日数(既定: 5日)",
                      "Yahoo!のコピー元商品(yahoo_paths_from_rakuten が ok なら紐づけは楽天から決めた paths を使い、"
                      "コピー元は product_category だけに使う。ok でなければ従来どおり紐付くパスで選ぶ)",
                      "公開の可否(Eストア/Yahoo!)。Yahoo!の店頭反映はストア全体の未反映分をまとめて公開する",
                      "092(自前在庫管理)の台帳に入れる在庫数(決まっていれば。0なら入れず、画面で後から入れる)"],
    }


# ------------------------------------------------------------ plan checks
def _is_int(x: Any) -> bool:
    return isinstance(x, int) and not isinstance(x, bool)


def validate_plan(p: dict) -> None:
    need = ["g", "manage_number", "sku_main", "is_variation", "variants", "estore_price_main",
            "estore_product_name", "lead_time_days", "estore", "yahoo", "goq"]
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
    if p["is_variation"]:
        axis_sets = {frozenset(v.get("selectors") or {}) for v in p["variants"]}
        if len(axis_sets) != 1:
            raise SystemExit(f"NG: variant 間で軸(色・サイズ等)の構成が一致しません: "
                             f"{[dict(v.get('selectors') or {}) for v in p['variants']]}")
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
    for section, name_s, keys in (("estore", e, ("register", "publish")),
                                  ("yahoo", y, ("register", "publish", "reserve_publish"))):
        for k in keys:
            if not isinstance(name_s.get(k), bool):
                raise SystemExit(f"NG: {section}.{k} は true/false で指定してください: {name_s.get(k)!r}")
    if not isinstance(p["goq"].get("resync"), bool):
        raise SystemExit(f"NG: goq.resync は true/false で指定してください: {p['goq'].get('resync')!r}")
    if "zaiko" in p:   # 2026-10-26〜の plan。無いのはそれより前の plan(GoQ の再開のため従来どおり)
        z = p["zaiko"]
        if not isinstance(z, dict) or not isinstance(z.get("register"), bool):
            raise SystemExit(f"NG: zaiko.register は true/false で指定してください: {z!r}")
        if p["goq"]["resync"]:
            raise SystemExit("NG: 新商品の在庫は GoQ に登録しません(2026-10-26 運用者決定)。goq.resync は false にしてください")
        n = z.get("initial_stock", 0)
        if isinstance(n, dict):
            bad = {k: v for k, v in n.items() if not _is_int(v) or not 0 <= v <= 100000}
            if bad:
                raise SystemExit(f"NG: zaiko.initial_stock は 0〜100000 の整数で指定してください: {bad}")
            if not p["is_variation"]:
                raise SystemExit("NG: 単品の zaiko.initial_stock は整数で指定してください")
        elif not _is_int(n) or not 0 <= n <= 100000:
            raise SystemExit(f"NG: zaiko.initial_stock は 0〜100000 の整数で指定してください: {n!r}")
        elif n > 0 and p["is_variation"]:
            raise SystemExit("NG: バリエーションの zaiko.initial_stock は {SKU: 数} で指定してください")
        if (n if _is_int(n) else any(n.values())) and not z["register"]:
            raise SystemExit("NG: 092 に登録しないのに zaiko.initial_stock が指定されています")
    allow = y.get("pending_allow", YAHOO_PENDING_ALLOW)
    if not _is_int(allow) or allow < 0:
        raise SystemExit(f"NG: yahoo.pending_allow は0以上の整数で指定してください: {allow!r}")
    if e["register"]:
        name = (p.get("estore_product_name") or "").strip()
        if not name:
            raise SystemExit("NG: estore_product_name(Eストア表示名)が未設定です。登録時点で短い名前を決めてください"
                             "(楽天の長い商品名をそのまま使わない)")
        if len(name) > 60:
            raise SystemExit(f"NG: estore_product_name が長すぎます({len(name)}文字・上限60文字)。"
                             "参考: 既存の兄弟SKUは実測19〜52文字です。短くしてください")
        if name == (p.get("title") or ""):
            raise SystemExit("NG: estore_product_name が楽天の商品名(title)と同じです。短縮してください")
    if y.get("paths") is not None:
        ps = y["paths"]
        if (not isinstance(ps, list) or not ps or len(ps) > 20
                or not all(isinstance(x, str) and x.strip() for x in ps)):
            raise SystemExit(f"NG: yahoo.paths は1〜20件の文字列の配列で指定してください: {ps!r}")
        if set(x.strip() for x in ps) <= THIN_PATHS:
            raise SystemExit("NG: yahoo.paths が【ネコポス可】だけです。商品カテゴリのパスを入れてください")
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
    try:
        approved_at = datetime.fromisoformat(ap["approved_at"])
    except (TypeError, ValueError, KeyError):
        raise SystemExit("NG: 承認日時の形式が不正です。approve をやり直してください")
    age = datetime.now() - approved_at
    if age < timedelta(0):
        raise SystemExit("NG: 承認日時が未来になっています。approve をやり直してください")
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
        # 途中終了で JSON が壊れて再開不能にならないよう、一時ファイル経由で置き換える
        tmp = self.state_path.with_suffix(self.state_path.suffix + ".tmp")
        tmp.write_text(json.dumps(self.state, ensure_ascii=False, indent=1), encoding="utf-8")
        os.replace(tmp, self.state_path)

    def row(self, sku: str) -> sqlite3.Row | None:
        return q_one("SELECT * FROM products WHERE sku=? AND deleted_at IS NULL", (sku,))

    def children(self) -> list[sqlite3.Row]:
        return q_all("SELECT * FROM products WHERE parent_sku=? AND deleted_at IS NULL ORDER BY sku", (self.main,))

    def images(self) -> list[sqlite3.Row]:
        return q_all("SELECT * FROM estore_image_map WHERE sku=?", (self.main,))


def step_import(c: Ctx) -> tuple[str, str]:
    # 楽天側の指紋チェックは「これから取り込むとき」だけ止める理由になる(古い内容を取り込む恐れがあるため)。
    # 023 に取込済みなら取込はしないので、止めない(GoQ だけ後から進める再開が、翌日以降も止まらないように。
    # 2026-10-23 cgd Lv3: 取込済みでも指紋で止めていたため、DEFERRED の再開が G2244/G2245 で止まった)
    drift = ""
    if c.plan.get("rakuten_fingerprint"):
        remote = rakuten_remote(c.mn)
        if remote is None:
            raise StepFail("楽天RMSに商品が見つかりません(承認後に削除された可能性)")
        if rakuten_fingerprint(remote) != c.plan["rakuten_fingerprint"]:
            if not c.row(c.main):
                raise NeedsUser("楽天RMS側の内容が下見(preflight)の時点から変わっています"
                                f"(変わった項目: {changed_rakuten_fields(c.plan, remote)})。"
                                "価格・画像・バリエーション構成が古いまま登録される恐れがあるため中断しました。"
                                "preflight をやり直して plan を作り直し、承認し直してください")
            drift = (f"(楽天側は下見の後に変わっています: {changed_rakuten_fields(c.plan, remote)}。"
                     "取込はしていないので、この変更は 023 に反映されていません)")
    if c.row(c.main):
        detail = f"023 に {c.main} あり" + drift
        status = "SKIP"
    else:
        code, body = http("POST", "/api/rakuten/import",
                          {"manage_number": c.mn, "sku": c.main, "enable_sync": True, "include_images": True})
        if code != 200:
            raise StepFail(f"import HTTP {code}: {str(body)[:200]}")
        body = _dict_body(body)
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
    fields = {"estore_price": p["estore_price_main"], "lead_time_days": p["lead_time_days"]}
    if p["estore"]["register"]:
        # Eストア対象外のときは estore_product_name に触れない(2026-09-28 実装漏れ: 対象外でも
        # 無条件PATCHしており、既存の estore_product_name を None で上書き消去しかねなかった)
        fields["estore_product_name"] = p["estore_product_name"]
    _patch(c.main, fields)
    got = c.row(c.main)
    if got["estore_price"] != p["estore_price_main"] or got["lead_time_days"] != p["lead_time_days"]:
        raise StepFail("価格/納期の読み戻しが一致しない")
    if p["estore"]["register"] and got["estore_product_name"] != p["estore_product_name"]:
        raise StepFail("estore_product_name(Eストア表示名)の読み戻しが一致しない")
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
    return "OK", f"estore_price={got['estore_price']} 納期={got['lead_time_days']}日 表示名={got['estore_product_name']!r}"


def step_estore_register(c: Ctx) -> tuple[str, str]:
    if not c.plan["estore"]["register"]:
        return "SKIP", "plan で対象外"
    row = c.row(c.main)
    if row["estore_item_code"]:
        return "SKIP", f"登録済み item_code={row['estore_item_code']}"
    code, body = http("POST", f"/api/estore/register/{c.main}")
    if code != 200:
        raise StepFail(f"Eストア登録 HTTP {code}: {str(body)[:300]}")
    body = _dict_body(body)
    if body.get("final_status") != "ok" or body.get("images_failed"):
        raise StepFail(f"Eストア登録 完了せず: {json.dumps(body, ensure_ascii=False)[:300]}")
    row = c.row(c.main)
    if not row["estore_item_code"]:
        raise StepFail("登録後に estore_item_code が空")
    return "OK", f"item_code={body['item_code']} 画像={len(body['images_uploaded'])}枚(非表示で作成)"


# Eストアのカテゴリ(2026-10-24 G2246): 登録の道具はカテゴリを指定しないので、何もしないと
# 「海上自衛隊(制服ほか)・船員服」だけに入る(運用者「よくない」)。楽天の紐付け(Yahoo! と同じ
# yahoo.paths)を Eストアのカテゴリにする。Eストアに無いカテゴリは外して報告する。
def _estore_cat_api(method: str, path: str, body: dict | None = None, kind: str = "manager") -> Any:
    sys.path.insert(0, str(PM_ROOT))
    from server.services.estore_client import request as es_request
    return es_request(method, path, api_kind=kind, json_body=body).body


def _estore_category_exists(path: list[str]) -> bool:
    try:
        body = _estore_cat_api("POST", "/item-categories/_get",
                               {"category_path": path[:-1]} if len(path) > 1 else {}, kind="open_shop")
    except Exception:  # noqa: BLE001  親カテゴリが無いと 404
        return False
    return path[-1] in (body or {}).get("children", [])


def estore_categories_wanted(plan: dict) -> list[list[str]]:
    y = plan.get("yahoo") or {}
    if y.get("paths_source") != "rakuten":
        return []
    return [p.split(":") for p in y.get("paths") or [] if p]


def _estore_categories_now(code: str) -> list[list[str]]:
    body = _estore_cat_api("GET", f"/items/{code}/categories") or {}
    return [c["category_names"] for c in sorted(body.get("categories") or [], key=lambda x: x["sort_number"])]


def step_estore_categories(c: Ctx) -> tuple[str, str]:
    if not c.plan["estore"]["register"]:
        return "SKIP", "plan で対象外"
    code = c.row(c.main)["estore_item_code"]
    if not code:
        raise StepFail("Eストアの商品コードが空(estore_register が済んでいない)")
    wanted = estore_categories_wanted(c.plan)
    if not wanted:
        raise NeedsUser("楽天の紐付けから Eストアのカテゴリを決められません(plan の yahoo.paths_source が rakuten でない)。"
                        "カテゴリを人が決めて、Eストアの管理画面か商品カテゴリ設定 API で付けてください")
    ok = [p for p in wanted if _estore_category_exists(p)]
    missing = [" > ".join(p) for p in wanted if p not in ok]
    if not ok:
        raise NeedsUser(f"楽天の紐付けに当たるカテゴリが Eストアに1つもありません: {missing}")
    if _estore_categories_now(code) == ok:
        return "SKIP", f"カテゴリは付け済み({len(ok)}件)"
    _estore_cat_api("PUT", f"/items/{code}/categories", {"categories": [{"category": p} for p in ok]})
    back = _estore_categories_now(code)
    if back != ok:
        raise StepFail(f"Eストアのカテゴリを読み戻すと違います: {back}")
    return "OK", f"カテゴリ {len(ok)}件を設定" + (f"(Eストアに無いので外した: {missing})" if missing else "")

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
    if code != 200:
        raise StepFail(f"bind-images HTTP {code}: {str(body)[:200]}")
    if not _dict_body(body).get("ok"):
        raise StepFail(f"bind-images 失敗: {str(body)[:200]}")
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
    req = {"copy_code": y["copy_code"], "publish": bool(y["publish"])}
    if y.get("paths"):
        req["paths"] = y["paths"]  # 紐付くパスの直接指定(コピー元のパスは使わない)
    code, body = http("POST", f"/api/yahoo/api-sync/{c.main}/register-new", req)
    if code != 200:
        raise StepFail(f"Yahoo登録 HTTP {code}: {str(body)[:300]}")
    body = _dict_body(body)
    if body.get("register_status") != "OK" or body.get("image_errors") or body.get("stock_errors"):
        raise StepFail(f"Yahoo登録 完了せず: {json.dumps(body, ensure_ascii=False)[:300]}")
    info = run_json_tool(["tools/yahoo_publish_tool.py", "item", c.mn])
    want = "1" if y["publish"] else "0"
    problems = []
    if str(info.get("display")) != want:
        problems.append(f"display={info.get('display')}")
    if not info.get("has_explanation"):
        problems.append("説明文なし")
    if not info.get("postage_set"):
        problems.append("送料未設定")
    problems += _yahoo_path_problems(c, info)
    if problems:
        raise StepFail("Yahoo読み戻し不一致: " + ", ".join(problems))
    return "OK", f"画像={len(body['uploaded_images'])}枚 display={info['display']} 送料グループ={info['postage_set']}"


def _yahoo_path_problems(c: Ctx, info: dict) -> list[str]:
    """紐付くパスが承認時に固定したコピー元のパスと一致するか(2026-09-28 G2231 で追加).

    `expected_paths` は `approve` が入れる。それ以前に承認された plan には無いので照合しない。
    """
    want = c.plan["yahoo"].get("expected_paths")
    if want is None:
        return []
    got = sorted(info.get("paths") or [])
    if got == sorted(want):
        return []
    return [f"パスがコピー元と不一致(不足={sorted(set(want) - set(got))} 余分={sorted(set(got) - set(want))})"]


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


GOQ_JUDGE_WAIT = 300  # 秒
GOQ_RECHECK_AFTER = 45  # 判定が NG・古いままのとき、この秒数たったら GoQ の連携表を取り直させる
GOQ_RECHECK_MAX = 3  # 連携表の取り直しは、1回の待ちで最大この回数(GoQ を占有するジョブなので連打しない)


def _judgement_fresh(body: dict, since_ms: float | None) -> bool:
    """判定が「連携の更新(resync)より後に作られたもの」か。since_ms が無ければ鮮度は問わない。"""
    if since_ms is None:
        return True
    at = body.get("checked_at")
    return isinstance(at, (int, float)) and not isinstance(at, bool) and at >= since_ms


def _request_status_sync() -> bool:
    """023 に「GoQ の連携表CSV出力→取込」だけを1回依頼し、終わるまで待つ(GoQ では読み取りだけ)。成否を返す。

    判定は、キューが空になった「その時点」の GoQ の連携表から作られる。GoQ 側の登録は遅れて反映されることがあり、
    その遅れの前に作られた NG の判定は、待つだけでは新しくならない(2026-10-23 G2245)。そこで取り直しを促す。
    """
    try:
        code, q = http("POST", "/api/queue/goq-status-sync", {})
        if code != 200 or not isinstance(q, dict) or "id" not in q:
            return False
        return _wait_queue(q["id"], timeout_s=480).get("status") == "ok"
    except StepFail:
        return False


def _wait_goq_judgement(c: Ctx, wait: int = GOQ_JUDGE_WAIT, every: int = 15,
                        since_ms: float | None = None) -> tuple[bool, dict]:
    """連携判定が「全OK」になるまで待つ。戻り値は (全OKか, 最後に見た判定)。

    判定(goq_sync_status)は goq_resync ジョブ自体では作られず、キューが空になった後に
    サーバーが「連携表CSV出力→023取込」を1回まとめて行ったときに作られ、そのたびに全置換される(job_queue._worker_loop)。
    - 旧版は 20 秒だけ待ち(G2231 は約84秒かかって FAIL)、その後「判定行があれば NG でも待たずに返す」にしたが、
      NG の判定行が GoQ の反映の遅れ(「基本コードが存在しません」)で作られたものだと、待ちが働かず FAIL した
      (2026-10-23 G2245。約90秒後には全OKになっていた)。
    - いまは、判定が全OK かつ(since_ms があれば)それより新しいときだけ成功とする。NG・古い判定は期限まで待ち、
      GOQ_RECHECK_AFTER 秒ごとに連携表の取り直し(goq_status_sync)を最大 GOQ_RECHECK_MAX 回促す。
    - 期限までに全OK にならなければ (False, 最後の判定) を返す。呼び出し側が FAIL ではなく PENDING として扱う。
    """
    start = time.time()
    deadline = start + wait
    rechecks = 0
    last_recheck = start
    while True:
        ok, body = _goq_all_ok(c)
        if ok and _judgement_fresh(body, since_ms):
            return True, body
        now = time.time()
        if now >= deadline:
            return False, body
        if rechecks < GOQ_RECHECK_MAX and now - last_recheck >= GOQ_RECHECK_AFTER:
            rechecks += 1
            _request_status_sync()  # 結果ではなく、そのあとの判定を読み直して決める
            last_recheck = time.time()
            continue
        time.sleep(every)


# GoQ にアクセスできないとき(メンテナンス中・つながらない・自動ログインできない)の目印。
# このときは在庫連携を行わず、登録だけ先に進める(2026-10-22 運用者決定)。状態は DEFERRED で残し、
# plan は「完了」にしない(GoQ が使えるようになってから同じ plan で run すると、GoQ の手順だけ進む)
GOQ_UNREACHABLE = ("メンテナンス中", "自動ログインに失敗", "接続", "タイムアウト", "Timeout", "timed out", "Connection")
DEFERRED = "DEFERRED"
# 連携の更新(resync)は済んだが、期限までに全モールの連携が確認できなかった(GoQ の反映の遅れ)。FAIL ではなく、
# 確認待ちとして plan を未完了で残す。次の run は resync を打ち直さず、確認から再開する(2026-10-23 cgd Lv3)
PENDING = "PENDING"
RESYNC_JOB_REUSE_SEC = 6 * 3600  # 前回の resync ジョブの完了をこの時間内なら再利用(打ち直さない)


def _goq_deferred(c: Ctx) -> bool:
    """GoQ の在庫連携が行われていない/確認できていない(在庫の設定へ進んではいけない状態)."""
    return (c.state.get("goq_resync") or {}).get("status") in (DEFERRED, PENDING)


def _pending_text(body: dict) -> str:
    return ("GoQの連携更新は済みましたが、期限までに全モールの連携を確認できませんでした(GoQ側の反映の遅れの可能性)。"
            "在庫の設定は行いません。しばらくしてから同じ plan で run すると、連携更新はやり直さず確認から再開します。"
            f"最後に見た判定: {json.dumps(body, ensure_ascii=False)[:200]}")


def step_goq_resync(c: Ctx) -> tuple[str, str]:
    if not c.plan["goq"]["resync"]:
        return "SKIP", "plan で対象外"
    ok, body = _goq_all_ok(c)
    if ok:
        return "SKIP", "GoQ 連携判定はすでに全モール OK"
    # 前回、resync は済んで確認待ち(PENDING)になっていたなら、打ち直さずに確認から再開する(二重の連携更新を避ける)
    prev = c.state.get("_goq_resync_job") or {}
    reuse = (c.state.get("goq_resync") or {}).get("status") == PENDING \
        and time.time() * 1000 - float(prev.get("finished_ms") or 0) < RESYNC_JOB_REUSE_SEC * 1000
    if reuse:
        ok, body = _wait_goq_judgement(c, since_ms=prev["finished_ms"])
        if ok:
            return "OK", f"GoQ連携判定 {body.get('detail')}(前回の連携更新の確認)"
        return PENDING, _pending_text(body)
    last = ""
    for _ in (1, 2):  # 自動ログインの一時失敗に 1 回だけ再実行 (連続失敗はロックを招くので 2 回まで)
        code, q = http("POST", "/api/queue/goq-resync", {"item_code": c.g})
        if code != 200:
            raise StepFail(f"enqueue HTTP {code}: {str(q)[:200]}")
        job = _wait_queue(q["id"])
        if job["status"] == "ok":
            finished_ms = time.time() * 1000
            c.state["_goq_resync_job"] = {"id": q["id"], "finished_ms": finished_ms}
            c.save()
            ok, body = _wait_goq_judgement(c, since_ms=finished_ms)
            if ok:
                return "OK", f"GoQ連携判定 {body.get('detail')}"
            return PENDING, _pending_text(body)
        last = job.get("error_msg") or ""
        if "自動ログインに失敗" not in last:
            break
        time.sleep(60)
    if any(k in last for k in GOQ_UNREACHABLE):
        return DEFERRED, ("GoQにアクセスできないため、在庫連携は行いませんでした(登録は続けます)。"
                          f"GoQが使えるようになったら、同じ plan で run すると在庫連携から進みます。理由: {last[:150]}")
    raise StepFail(f"GoQ在庫連携 失敗: {last[:200]}")


def _goq_rows(c: Ctx) -> list[dict]:
    """GoQ の検索結果の行 (読み取り)。キューが空になるまで少し待ってから呼ぶ.

    直前の goq_resync が終わった直後は、後続ジョブ(他 SKU の分等)がまだキューに残っている
    ことがある。以前は即座に FAIL していたが、それだけで在庫設定が無用に失敗しうるため、
    最大 2 分待ってから諦める(2026-09-28 cgd Lv3 指摘)。
    """
    for _ in range(12):
        code, st = http("GET", "/api/queue/status")
        if not (st.get("running") or st.get("queued")):
            break
        time.sleep(10)
    else:
        raise StepFail("キューが動いたままで GoQ 検索を開始できない(2分待機)。落ち着いてから再実行してください")
    code, res = http("POST", "/api/goq-browser/search", {"item_code": c.g})
    if code != 200:
        raise StepFail(f"GoQ検索 HTTP {code}: {str(res)[:200]}")
    rows = _dict_body(res).get("rows") or []
    return [{"value": str(r.get("value", "")), "text": json.dumps(r, ensure_ascii=False)} for r in rows]


def _g_number_match(g: str, text: str) -> bool:
    """商品番号が「部分文字列」ではなく単独で現れるかを見る(G2225 が G22250 に誤一致しない)."""
    for m in re.finditer(re.escape(g), text, re.IGNORECASE):
        start, end = m.start(), m.end()
        if start > 0 and text[start - 1].isalnum():
            continue
        if end < len(text) and text[end].isdigit():
            continue
        return True
    return False


def _token_match(token: str, text: str) -> bool:
    """selector 値(色・サイズ)が行テキストに含まれるかを見る.

    英数字だけのトークン(サイズ "M"/"L"/"3L" 等)は前後が英数字でない位置での一致を要求する
    ("M" が "ML" の一部に誤一致しない)。日本語トークン(色名等)は GoQ の行データが列構造を
    持たず空白区切りの説明文しか無い(goq_browser.py の select_all_checkboxes 参照)ため、
    単純な部分一致のまま(2026-09-28 cgd Lv3 で指摘: 構造化列を使った完全一致は現状のデータ
    構造では実現できない。将来 goq_browser.py が列単位のデータを返すようになれば厳格化する)。
    """
    if not token:
        return False
    if re.fullmatch(r"[0-9A-Za-z]+", token):
        pattern = re.compile(r"(?<![0-9A-Za-z])" + re.escape(token) + r"(?![0-9A-Za-z])")
        return bool(pattern.search(text))
    return token in text


def _plan_goq_jobs(c: Ctx, rows: list[dict]) -> list[tuple[str, int]]:
    """variant ごとに書き込む GoQ の行を特定する。曖昧なら NEEDS_USER (誤書き込みより停止を選ぶ)."""
    p = c.plan
    if not p["is_variation"]:
        if len(rows) != 1:
            raise NeedsUser(f"GoQ の対象行が {len(rows)} 行あります(単品は 1 行のはず)。"
                            f"行を確認してください: {[r['value'] for r in rows]}")
        if not _g_number_match(c.g, rows[0]["text"]):
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
        hit = [r for r in rows if all(_token_match(t, r["text"]) for t in tokens)]
        if len(hit) != 1 or hit[0]["value"] in used:
            raise NeedsUser(
                "GoQ の行を variant に一意に対応づけられません(初回のバリエーション商品は対応を確認してください)。"
                f"variant={v['selectors']} 候補={[r['value'] for r in hit]} 全行={[r['text'][:90] for r in rows]}")
        used.add(hit[0]["value"])
        jobs.append((hit[0]["value"], v["stock"]))
    return jobs


def _goq_current_quantity(c: Ctx) -> int | None:
    """GoQ の現在庫(単品)。読めなければ None(その場合は従来どおり設定へ進む)。023 の読み取り専用の下見を使う。"""
    try:
        code, body = http("GET", f"/api/line-agent/stock/preview?sku={c.main}", timeout=120)
    except StepFail:
        return None
    if code != 200 or not isinstance(body, dict):
        return None
    qty = (((body.get("snapshot") or {}).get("goq") or {}).get("quantity"))
    return qty if isinstance(qty, int) and not isinstance(qty, bool) else None


def step_goq_stock(c: Ctx) -> tuple[str, str]:
    if not c.plan["goq"]["resync"]:
        return "SKIP", "plan で対象外(goq.resync=false のため在庫書込みも行わない)"
    if _goq_deferred(c):
        status = (c.state.get("goq_resync") or {}).get("status")
        return status, ("GoQの在庫連携を行っていない/確認できていないため、GoQの在庫数の設定も行いませんでした"
                        if status == DEFERRED else
                        "GoQの連携の確認待ちのため、GoQの在庫数の設定は行いませんでした")
    progress: dict = c.state.setdefault("goq_stock_progress", {})
    rows = _goq_rows(c)
    jobs = _plan_goq_jobs(c, rows)
    if not c.plan["is_variation"] and len(jobs) == 1 and not progress:
        # 再開で在庫を0へ戻さない: 人がすでに在庫を入れていたら(0以外)上書きしない。バリエーションは行ごとの
        # 現在庫を読む手段が無いため、この保護の対象外(従来どおり)
        cur = _goq_current_quantity(c)
        qty = jobs[0][1]
        if isinstance(cur, int) and cur != int(qty):
            return "SKIP", f"GoQの現在庫がすでに {cur} のため、{qty} には書き換えません(再開で在庫を戻さないため)"
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
        # row_count は「反映画面で選択された全行数」であり「変更された行数」ではない
        # (goq_worker.set_quantity の仕様: 選択チェックボックスは全行必須のため、row_ids で絞っても
        # 検索にヒットした行数がそのまま row_count になる。バリエーション商品で row_count==1 を
        # 要求すると常に FAIL する実装ミスだった。実際に変更された行は changed_fields で判定する。
        # 2026-09-28 G2230 実登録で発覚)
        if job["status"] != "ok" or res.get("changed_fields") != [f"stock[{row_id}]"] \
                or res.get("quantity") != int(qty) or res.get("target_row_ids") != [row_id]:
            raise StepFail(f"在庫数の反映に失敗 row={row_id}: {job.get('error_msg')} "
                           f"{json.dumps(res, ensure_ascii=False)[:200]}")
        progress[row_id] = qty  # 行ごとに進捗を残す(途中で落ちても済んだ行を再実行しない)
        c.save()
        done.append(f"{row_id}={qty}")
    return "OK", "在庫数を反映: " + ", ".join(done)


# ------------------------------------------------- 092 自前在庫管理(zaiko)
# 2026-10-26 運用者決定: 新商品の在庫は GoQ に一切登録しない。092(zaiko.sfuji.f5.si)に登録し、そこから各モールへ配る。
# 順序: 各モールを在庫0で登録 → 092 に親SKUを追加 → (在庫数が決まっていれば)台帳に入れる → 各モールへ伝わったことを確かめる。
# 092 の画面の API はメインPC(192.168.1.200)以外はログインが要るので、画面と同じ関数(shadow.web.stock_edit)を直接呼ぶ。
# モールへ書くのは 092 の画面のサーバーが持つワーカー(30秒おき)。ここでは依頼を積み、結果(mall_sync_state)を待つ。
ZAIKO_ROOT = Path(r"C:\ClaudeCode\092.受注影武者と在庫台帳")
ZAIKO_DB = ZAIKO_ROOT / "data" / "shadow.db"
ZAIKO_ACTOR = "register-product"
ZAIKO_SYNC_WAIT = 300  # 秒。ワーカーは30秒おきに見に来る。失敗は2分おきに再試行する


def _zaiko_mod():
    if str(ZAIKO_ROOT) not in sys.path:
        sys.path.insert(0, str(ZAIKO_ROOT))
    from shadow import db as zdb, mall_sync, stop_sale  # noqa: PLC0415 — 092 は必要なときだけ読む
    from shadow.mall_targets import TargetIndex  # noqa: PLC0415
    from shadow.product_index import DEFAULT_DB  # noqa: PLC0415
    from shadow.web import catalog, stock_edit  # noqa: PLC0415
    return zdb, mall_sync, stop_sale, TargetIndex, DEFAULT_DB, catalog, stock_edit


def zaiko_wanted_stock(plan: dict) -> dict[str, int]:
    """092 の台帳に入れる数 {SKU: 数}。0 や未指定の SKU は入れない(台帳は空のまま=画面で後から入れる)."""
    z = plan.get("zaiko") or {}
    n = z.get("initial_stock", 0)
    if isinstance(n, dict):
        return {str(k): v for k, v in n.items() if _is_int(v) and v > 0}
    if _is_int(n) and n > 0:
        return {plan["sku_main"]: n}
    return {}


def _zaiko_children(c: Ctx) -> list[str]:
    return [r["sku"] for r in c.children()] if c.plan["is_variation"] else [c.main]


def _zaiko_malls(c: Ctx) -> list[str]:
    """092 から在庫を書くモール。このパイプラインで登録したモール+楽天(登録済みが前提)."""
    malls = ["rakuten"]
    if c.plan["estore"]["register"]:
        malls.append("estore")
    if c.plan["yahoo"]["register"]:
        malls.append("yahoo")
    return malls


def _zaiko_conn():
    zdb = _zaiko_mod()[0]
    if not ZAIKO_DB.exists():
        raise StepFail(f"092 の DB が見つかりません: {ZAIKO_DB}")
    return zdb.connect(ZAIKO_DB)


def _zaiko_managed(conn, skus: list[str]) -> dict[str, list[str]]:
    """自前連携に載っている SKU → モールの一覧."""
    out: dict[str, list[str]] = {}
    for s in skus:
        r = conn.execute("SELECT malls FROM cutover_sku WHERE sku=?", (s,)).fetchone()
        if r is not None:
            out[s] = [m for m in re.split(r"[,\s]+", str(r["malls"] or "")) if m]
    return out


def _zaiko_qty(conn, sku: str) -> int | None:
    r = conn.execute("SELECT qty FROM sku_stock WHERE sku=?", (sku,)).fetchone()
    return None if r is None else r["qty"]


def step_zaiko_register(c: Ctx) -> tuple[str, str]:
    if not (c.plan.get("zaiko") or {}).get("register"):
        return "SKIP", "plan で対象外"
    _, _, _, TargetIndex, products_db, catalog, stock_edit = _zaiko_mod()
    skus = _zaiko_children(c)
    malls = _zaiko_malls(c)
    conn = _zaiko_conn()
    try:
        have = _zaiko_managed(conn, skus)
        if len(have) == len(skus) and all(set(malls) <= set(have[s]) for s in skus):
            return "SKIP", f"092 に登録済み({len(skus)} SKU・{'/'.join(malls)})"
        cat, targets = catalog.Catalog.from_db(products_db), TargetIndex.from_db(products_db)
        p = cat.parents.get(c.main)
        if p is None:
            raise StepFail(f"092 から見た商品マスタに {c.main} がありません")
        missing = {}
        for s in skus:
            got = set(targets.targets_for(s).targets)
            if not set(malls) <= got:
                missing[s] = sorted(set(malls) - got)
        if missing:
            raise NeedsUser(f"092 で在庫を書くモールが決まらない SKU があります {missing}"
                            "(023 の楽天・Yahoo!・Eストアの番号を確かめてください)")
        res = stock_edit.add_parent(conn, cat, targets, c.main, who=ZAIKO_ACTOR)
        have = _zaiko_managed(conn, skus)
    finally:
        conn.close()
    lost = [s for s in skus if s not in have]
    if lost:
        raise StepFail(f"092 に載らなかった SKU があります {lost}(追加の結果 {res})")
    return "OK", f"092 に登録 {len(skus)} SKU({'/'.join(malls)})。台帳在庫は{'未設定(画面で入れる)' if not zaiko_wanted_stock(c.plan) else 'この後に入れる'}"


def _zaiko_sync_rows(conn, sku: str) -> dict[str, dict]:
    return {r["mall"]: dict(r) for r in conn.execute("SELECT * FROM mall_sync_state WHERE sku=?", (sku,))}


def _wait_zaiko_sync(want: dict[str, int], malls: list[str], wait: int = ZAIKO_SYNC_WAIT, every: int = 10) -> dict:
    """台帳の数がモールへ書かれて読み戻せるまで待つ。{SKU: {mall: 状態}} を返す."""
    deadline = time.time() + wait
    while True:
        conn = _zaiko_conn()
        try:
            rows = {s: _zaiko_sync_rows(conn, s) for s in want}
        finally:
            conn.close()
        done = all(rows[s].get(m, {}).get("status") == "ok" and rows[s][m].get("synced_qty") == n
                   for s, n in want.items() for m in malls)
        bad = [f"{s}/{m}:{rows[s].get(m, {}).get('status')}" for s in want for m in malls
               if rows[s].get(m, {}).get("status") == "gave_up"]
        if done or bad or time.time() >= deadline:
            return {"done": done, "bad": bad, "rows": rows}
        time.sleep(every)


def step_zaiko_stock(c: Ctx) -> tuple[str, str]:
    want = zaiko_wanted_stock(c.plan)
    if not (c.plan.get("zaiko") or {}).get("register") or not want:
        return "SKIP", "台帳に入れる数の指定なし(在庫は 092 の画面で入れる)"
    _, _, stop_sale, _, _, _, stock_edit = _zaiko_mod()
    malls = _zaiko_malls(c)
    foreign = sorted(set(want) - set(_zaiko_children(c)))
    if foreign:   # 別の商品の SKU に在庫を入れない(Codex レビュー 2026-10-26)
        raise StepFail(f"zaiko.initial_stock にこの商品の子SKUでないものがあります: {foreign}")
    conn = _zaiko_conn()
    try:
        if not stop_sale.push_enabled(conn):
            raise NeedsUser("092 のモールへの送信がオフです(`python -m shadow push on` は運用者の判断)")
        changes, kept = [], []
        for sku, n in list(want.items()):
            cur = _zaiko_qty(conn, sku)
            if cur is None:
                changes.append({"sku": sku, "expected": None, "value": str(n)})
            elif cur != n:
                kept.append(f"{sku}={cur}")   # 人が入れた数を上書きしない(再開で戻さない)。ほかの SKU は続ける
                del want[sku]
        if not want:
            return "SKIP", f"092 の台帳にすでに別の数があるので書きません: {', '.join(kept)}"
        if changes:
            rid = f"rp-{c.g}-{plan_hash(c.plan)[:12]}"
            r = stock_edit.apply_changes(conn, changes, reason="inbound", note="新商品の初回の在庫(register-product)",
                                         who=ZAIKO_ACTOR, request_id=rid)
            if r.conflicts:
                raise StepFail(f"092 の台帳が保存の直前に変わりました {r.conflicts}")
    finally:
        conn.close()
    res = _wait_zaiko_sync(want, malls)
    if not res["done"]:
        detail = {s: {m: (v.get("status"), v.get("synced_qty"), (v.get("last_error") or "")[:80])
                      for m, v in rows.items()} for s, rows in res["rows"].items()}
        raise StepFail(f"092 から各モールへの反映を確かめられません({'失敗 ' + ', '.join(res['bad']) if res['bad'] else '時間切れ'})"
                       f" {json.dumps(detail, ensure_ascii=False)[:400]}")
    return "OK", ("092 の台帳在庫を各モールへ反映: " + ", ".join(f"{s}={n}" for s, n in want.items()) + f"({'/'.join(malls)})"
                  + (f"。台帳に別の数があり書かなかった: {', '.join(kept)}" if kept else ""))


def _verify_zaiko(c: Ctx, issues: list[str], facts: list[str]) -> None:
    skus = _zaiko_children(c)
    malls = _zaiko_malls(c)
    conn = _zaiko_conn()
    try:
        have = _zaiko_managed(conn, skus)
        lost = [s for s in skus if s not in have or not set(malls) <= set(have[s])]
        if lost:
            issues.append(f"092: 自前連携に載っていない・モールが足りない SKU {lost}")
            return
        want = zaiko_wanted_stock(c.plan)
        qtys = {s: _zaiko_qty(conn, s) for s in skus}
        for s, n in want.items():
            # 確かめるのは、このパイプラインが入れた数のまま残っている SKU だけ。人が数を変えた・0 以下にした SKU は
            # 092 の画面の管理(0 以下は 092 がモールへ書かず停止の送信に任せる仕様)なので、ここでは問わない
            if qtys.get(s) != n or n <= 0:
                continue
            rows = _zaiko_sync_rows(conn, s)
            not_synced = [m for m in malls if rows.get(m, {}).get("status") != "ok"
                          or rows[m].get("synced_qty") != qtys.get(s)]
            if not_synced:
                issues.append(f"092: {s} の台帳 {qtys.get(s)} がモールへ反映されていない {not_synced}")
    finally:
        conn.close()
    facts.append("092 登録済み(台帳 " + ", ".join(f"{s}={'未設定' if q is None else q}" for s, q in qtys.items()) + ")")


def step_verify(c: Ctx) -> tuple[str, str]:
    p, issues, facts = c.plan, [], []
    # 店頭に出る表示名はチャネルで別物: Eストアは estore_product_name(短縮名)、Yahoo!は楽天由来の title
    # (yahoo_register_export.build_preview が product_name をそのまま使うため)。両方に同じ key を
    # 使うと、短縮名を導入した意味がなくなり正常登録でも FAIL する(2026-09-28 の実装漏れ)。
    if p["estore"]["register"] and p["estore"]["publish"]:
        estore_key = (p.get("estore_product_name") or "")[:12]
        code, html = fetch_text(STORE_SHOPSERVE.format(code=c.g))
        if code != 200:
            issues.append(f"Eストア店頭 HTTP {code}")
        else:
            if estore_key and estore_key not in html:
                issues.append("Eストア: ページに商品名(表示名)が見つからない(別ページの可能性)")
            if "limg/noimage.gif" in html:
                issues.append("Eストア: 画像なし表示")
            price_txt = f"{p['estore_price_main']:,}円"
            if price_txt not in html:
                issues.append(f"Eストア: 価格 {price_txt} が見つからない")
            # 写真を送り直すと {G番号}_v{日時}_{NN}.jpg の名前になる(tools/photo_expand.py)
            n_img = len(set(re.findall(rf"{c.g}_(?:v\d+_)?\d+\.jpg", html)))
            if n_img < 1:
                issues.append("Eストア: 商品画像の参照が見つからない")
            # 楽天の「関連商品」の表が説明文に写ると、商品説明と写真の間に他の商品が大量に並ぶ
            # (2026-10-20 G0191・2026-10-24 G2246 で運用者が「絶対にダメ」と指摘)
            if "<b>関連商品</b>" in html or "TJ R PC" in html:
                issues.append("Eストア: 説明文に楽天の「関連商品」の表が入っている")
            facts.append(f"Eストア店頭OK(画像{n_img}枚)" + ("(在庫切れ表示)" if "btn_nostock" in html else ""))
    if p["estore"]["register"] and estore_categories_wanted(p):
        now_cats = _estore_categories_now(c.row(c.main)["estore_item_code"])
        if not now_cats or not set(map(tuple, now_cats)) <= set(map(tuple, estore_categories_wanted(p))):
            issues.append(f"Eストア: カテゴリが楽天の紐付けと違う {now_cats}")
        else:
            facts.append(f"Eストアカテゴリ{len(now_cats)}件")
    if p["yahoo"]["register"]:
        info = run_json_tool(["tools/yahoo_publish_tool.py", "item", c.mn])
        path_issues = _yahoo_path_problems(c, info)
        issues += [f"Yahoo!: {x}" for x in path_issues]
        if info.get("explanation_has_related"):
            issues.append("Yahoo!: 説明文に楽天の「関連商品」の羅列が入っている")
        if not path_issues and p["yahoo"].get("expected_paths") is not None:
            facts.append(f"Yahoo!パス一致({len(info.get('paths') or [])}本)")
    if p["yahoo"]["register"] and p["yahoo"]["publish"] and p["yahoo"]["reserve_publish"]:
        yahoo_key = (p.get("title") or "")[:12]
        code, html = fetch_text(STORE_YAHOO.format(code=c.mn))
        if code != 200 or (yahoo_key and yahoo_key not in html):
            issues.append(f"Yahoo!店頭が確認できない(HTTP {code})")
        else:
            facts.append("Yahoo!店頭OK" + ("(在庫切れ表示)" if re.search("在庫なし|売り切れ|在庫切れ", html) else ""))
    if (p.get("zaiko") or {}).get("register"):
        _verify_zaiko(c, issues, facts)
    if p["goq"]["resync"] and _goq_deferred(c):
        facts.append("GoQ: 在庫連携は未実施(GoQにアクセスできなかった)"
                     if (c.state.get("goq_resync") or {}).get("status") == DEFERRED
                     else "GoQ: 連携更新は済み・全モールの連携は確認待ち(PENDING)")
    elif p["goq"]["resync"]:
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
    ("estore_categories", step_estore_categories),
    ("estore_images", step_estore_images),
    ("estore_bind", step_estore_bind),
    ("estore_publish", step_estore_publish),
    ("yahoo_register", step_yahoo_register),
    ("yahoo_reserve", step_yahoo_reserve),
    ("zaiko_register", step_zaiko_register),
    ("zaiko_stock", step_zaiko_stock),
    ("goq_resync", step_goq_resync),
    ("goq_stock", step_goq_stock),
    ("verify", step_verify),
]


# ------------------------------------------------------------------- run
def _pid_alive(pid: int) -> bool | None:
    """PID がまだ生きているか (Windows)。判定できない場合は None(呼び出し側は時間で判断する)."""
    try:
        import ctypes
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        handle = ctypes.windll.kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            return False
        ctypes.windll.kernel32.CloseHandle(handle)
        return True
    except Exception:  # noqa: BLE001 — Windows 以外・ctypes 失敗時は判定不能を返す
        return None


def _acquire_lock(path: Path) -> None:
    if path.exists():
        try:
            pid = int(path.read_text(encoding="utf-8").strip())
        except (ValueError, OSError):
            pid = None
        alive = _pid_alive(pid) if pid else None
        if alive is False:
            # プロセスが確認できる範囲で死んでいるなら、経過時間に関係なく即座に回収してよい
            path.unlink()
        else:
            # 生存確認できない(別OS・ctypes失敗)場合だけ、経過時間で判定する(旧来のフォールバック)
            age = datetime.now() - datetime.fromtimestamp(path.stat().st_mtime)
            if alive or age < LOCK_STALE:
                raise SystemExit(f"NG: 同じ plan が実行中です(ロック {path.name}, pid={pid})。"
                                 "実行中でなければ、ロックファイルを削除してください")
            path.unlink()
    try:
        fd = os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        raise SystemExit(f"NG: 同じ plan がちょうど実行を開始しました(ロック {path.name})。"
                         "少し待って再実行してください")
    os.write(fd, str(os.getpid()).encode("ascii"))
    os.close(fd)


def run(plan_path: Path, only: str | None, reconcile: bool = False) -> int:
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    validate_plan(plan)
    read_only = only == "verify"
    if only and not read_only and only not in dict(STEPS):
        raise SystemExit(f"NG: --only に無効なステップ名です: {only!r}。"
                         f"有効な値: {', '.join(n for n, _ in STEPS)}")
    if not read_only:
        check_approval(plan)
    c = Ctx(plan, plan_path.with_suffix(".state.json"))
    if not read_only:
        # 完了判定は state の実際の各ステップ状態で決める(hash やこれまでの実行回数に依らない)。
        # --only を付けても、完了済み plan は素通りさせない (2026-09-28 cgd Lv3 指摘: 以前は
        # `not only and ...` で --only 使用時にこの拒否が丸ごと外れており、完了後でも
        # `--only goq_stock` 等の書き込み系を単発で再実行できてしまっていた)
        all_done = bool(c.state) and all((c.state.get(n) or {}).get("status") in ("OK", "SKIP") for n, _ in STEPS)
        if all_done and not reconcile:
            raise SystemExit("NG: この plan は完了済みです。確認だけなら `verify`、"
                             "意図して再確認・再実行するなら `--reconcile` を付けてください")
        # plan の内容が承認しなおされて変わった場合、未完了の run なら再開を許可する
        # (NEEDS_USER で止まった plan を直して承認し直すのは SKILL.md が案内する正規の手順のため)。
        # hash 不一致を無条件で拒否していた旧実装は、この案内どおりに動かせない矛盾があった
        # (cgd Lv3 2026-09-28 指摘)。完了済みかどうかは上の all_done だけで判定する。
        c.state["_plan_hash"] = plan_hash(plan)
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
        pin_copy_source_paths(plan)
        plan["approval"] ={"hash": plan_hash(plan), "approved_at": datetime.now().isoformat(timespec="seconds")}
        path.write_text(json.dumps(plan, ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"OK: 承認を記録しました(24時間有効) hash={plan['approval']['hash'][:12]}")
        return 0
    rc = run(Path(a.plan), "verify") if a.cmd == "verify" else run(Path(a.plan), a.only, a.reconcile)
    if rc == 0:
        plan = json.loads(Path(a.plan).read_text(encoding="utf-8"))
        for mall, url in store_links(plan):
            print(f"LINK {mall}: {url}")
    return rc


if __name__ == "__main__":
    sys.exit(main())
