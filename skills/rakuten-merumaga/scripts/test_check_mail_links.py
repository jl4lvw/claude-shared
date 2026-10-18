import check_mail_links as c

c.SLEEP = 0  # テストでは待たない

SHOP = "https://item.rakuten.co.jp/seifukunofuji"
HTML = f"""
<table><tr><td>防寒ブルゾン ( 海上自衛隊 デジタル迷彩柄 )</td></tr>
<tr><td>18,150円</td></tr>
<tr><td><a href="{SHOP}/g1510"><img></a></td></tr>
<tr><td>九谷焼マグカップ ( 護衛艦かが )</td></tr>
<tr><td>4,356円</td></tr>
<tr><td><a href="{SHOP}/g0963"><img></a></td></tr>
<tr><td><a href="https://item.rakuten.co.jp/seifukunofuji/c/0000000418/">＞アウトドア</a></td></tr></table>
"""


class FakeRms:
    """rms_api の代わり。items と在庫を差し替えて使う。"""

    def __init__(self, items, stock):
        self._items, self._stock = items, stock

    def headers(self):
        return {}

    def bulk_get(self, numbers, hdrs):
        return {n: self._items[n] for n in numbers if n in self._items}

    def http_json(self, method, url, hdrs, body=None):
        invs = [
            {"manageNumber": r["manageNumber"], "variantId": r["variantId"], "quantity": self._stock.get((r["manageNumber"], r["variantId"]), 0)}
            for r in body["inventories"]
        ]
        return {"inventories": invs}


def item(title, variants, hide=False):
    return {"title": title, "hideItem": hide, "variants": variants}


def v(price, hidden=False, sku="S"):
    return {"standardPrice": str(price), "hidden": hidden, "merchantDefinedSkuId": sku}


ITEMS = {
    "g1510": item("楽天1位 防寒ブルゾン ( 海上自衛隊 デジタル迷彩柄 襟ボア付き ) PX品", {"1": v(18150, sku="G1510-S"), "2": v(18150, sku="G1510-L")}),
    "g0963": item("九谷焼マグカップ ( 護衛艦かが ) 食器", {"a": v(4356)}),
}


def web_get(url, timeout=30.0):
    if "/c/0000000418" in url:
        return 404, url, "<title>【楽天市場】エラー</title>"
    return 200, url, "<title>OK</title>"


def run(items, stock, html=HTML):
    return c.run(html, rms=FakeRms(items, stock), get=web_get)


def msgs(findings, level):
    return [f.message for f in findings if f.level == level]


def test_extract_links_name_price_and_manage_number():
    ls = c.extract_links(HTML)
    a = ls[0]
    assert a.manage_number == "g1510" and a.price == 18150 and a.name.startswith("防寒ブルゾン")
    assert next(x for x in ls if "/c/" in x.url).manage_number is None


def test_all_ok_and_dead_category_link_is_warning():
    f, s = run(ITEMS, {("g1510", "1"): 5, ("g1510", "2"): 3, ("g0963", "a"): 9})
    assert s["items"] == 2 and s["items_sellable"] == 2 and not s["all_items_unsellable"]
    assert any("HTTP 404" in x for x in msgs(f, "warn"))
    assert c.exit_code(f) == 1


def test_partial_stockout_is_only_warning():
    f, _ = run(ITEMS, {("g1510", "1"): 5, ("g1510", "2"): 0, ("g0963", "a"): 9})
    assert any("一部欠品" in x and "G1510-L" in x for x in msgs(f, "warn"))
    assert not msgs(f, "error")


def test_one_item_fully_out_is_warning_not_error():
    f, s = run(ITEMS, {("g1510", "1"): 0, ("g1510", "2"): 0, ("g0963", "a"): 9})
    assert any("欠品" in x and "すべて在庫 0" in x for x in msgs(f, "warn"))
    assert s["items_sellable"] == 1 and not msgs(f, "error")


def test_all_items_unsellable_is_error_with_exit_2():
    f, s = run(ITEMS, {})
    assert s["all_items_unsellable"] and msgs(f, "error")
    assert c.exit_code(f) == 2


def test_hidden_item_counts_as_unsellable():
    items = dict(ITEMS, g1510=item("防寒ブルゾン", {"1": v(18150)}, hide=True))
    f, s = run(items, {("g1510", "1"): 5, ("g0963", "a"): 9})
    assert any("非表示" in x for x in msgs(f, "warn"))
    assert s["items_sellable"] == 1


def test_hidden_variants_are_not_counted_as_stock():
    items = dict(ITEMS, g0963=item("九谷焼マグカップ ( 護衛艦かが )", {"a": v(4356, hidden=True)}))
    f, s = run(items, {("g1510", "1"): 5, ("g0963", "a"): 99})
    assert any("表示中のバリエーションがありません" in x for x in msgs(f, "warn"))
    assert s["items_sellable"] == 1


def test_missing_item_in_rms_is_dead_link():
    items = {k: x for k, x in ITEMS.items() if k != "g0963"}
    f, _ = run(items, {("g1510", "1"): 5})
    assert any("RMS に商品が見つかりません" in x for x in msgs(f, "warn"))


def test_name_mismatch_and_price_mismatch():
    items = dict(ITEMS, g1510=item("ステンレスタンブラー 450ml 除籍記念", {"1": v(3025)}))
    f, _ = run(items, {("g1510", "1"): 5, ("g0963", "a"): 9})
    w = msgs(f, "warn")
    assert any("説明と違う商品の疑い" in x for x in w)
    assert any("価格が違います" in x and "18,150" in x for x in w)


def test_name_score_basics():
    assert c.name_score("九谷焼マグカップ", "【楽天】九谷焼マグカップ ( 護衛艦かが )") == 1.0
    assert c.name_score("九谷焼マグカップ", "ステンレスタンブラー") < c.NAME_NG
    assert c.name_score("", "何でも") == 1.0


def test_generic_cta_label_is_not_name_checked():
    html = '<p>▼▼ 特設会場はこちらをクリック ▼▼</p><a href="https://www.rakuten.ne.jp/gold/x/"><img></a>'
    f, _ = c.run(html, rms=FakeRms({}, {}), get=lambda u, timeout=30.0: (200, u, "<title>お買い物マラソン</title>"))
    assert not msgs(f, "warn")


def test_redirect_within_rakuten_is_ok_but_other_site_warns():
    html = '<p>店</p><a href="https://www.rakuten.ne.jp/gold/x/"><img></a>'
    same = c.run(html, rms=FakeRms({}, {}), get=lambda u, timeout=30.0: (200, "https://www.rakuten.co.jp/x/", "<title>店</title>"))[0]
    other = c.run(html, rms=FakeRms({}, {}), get=lambda u, timeout=30.0: (200, "https://example.com/", "<title>店</title>"))[0]
    assert not msgs(same, "warn") and any("別のサイト" in x for x in msgs(other, "warn"))


def test_search_zero_hits_warns():
    html = '<p>20倍</p><a href="https://search.rakuten.co.jp/search/mall/20%E5%80%8D/?sid=1"><img></a>'
    page = '<title>【楽天市場】20倍の通販</title><meta content="楽天市場-「20倍」0件 人気の商品を">'
    f, _ = c.run(html, rms=FakeRms({}, {}), get=lambda u, timeout=30.0: (200, u, page))
    assert any("0 件" in x for x in msgs(f, "warn"))


def test_no_item_links_is_not_an_error():
    html = '<p>店</p><a href="https://www.rakuten.ne.jp/gold/x/"><img></a>'
    f, s = c.run(html, rms=FakeRms({}, {}), get=lambda u, timeout=30.0: (200, u, "<title>店</title>"))
    assert s["items"] == 0 and not s["all_items_unsellable"] and not msgs(f, "error")
