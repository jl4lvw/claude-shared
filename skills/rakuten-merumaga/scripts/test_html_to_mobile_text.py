import html_to_mobile_text as m

HTML = """
<center><table width="700"><tr><td>
<a target="_blank" href="https://www.rakuten.ne.jp/gold/x/"><img src="a.jpg" /></a>
<p><font size="5">いつもありがとうございます！
<br />ご利用ください</font></p>
</td></tr>
<tr><td><b>■ポイント20倍■</b><hr size="3"></td></tr>
<tr><td>商品A</td></tr>
<tr><td>1,000円</td></tr>
<tr><td><a href="https://item.rakuten.co.jp/x/a"><img src="a.jpg"></a></td></tr>
<tr><td>商品B</td></tr>
<tr><td>2,000円</td></tr>
<tr><td><a href="https://item.rakuten.co.jp/x/b">詳細&nbsp;はこちら</a></td></tr>
</table></center>
<script>var x = "<p>無視</p>";</script>
"""


def test_blocks_and_br_become_lines_and_decoration_dropped():
    out = m.convert(HTML)
    assert "いつもありがとうございます！\nご利用ください" in out
    assert "<" not in out and "font" not in out and "無視" not in out


def test_image_link_becomes_url_only_and_text_link_puts_url_after_text():
    out = m.convert(HTML)
    assert out.startswith("いつもありがとうございます！")  # 先頭のバナー URL は外れる
    assert m.convert(HTML, keep_leading_url=True).startswith("https://www.rakuten.ne.jp/gold/x/\n")
    assert "詳細 はこちら\nhttps://item.rakuten.co.jp/x/b" in out


def test_item_group_is_packed_and_separated_by_one_blank_line():
    out = m.convert(HTML)
    assert "商品A\n1,000円\nhttps://item.rakuten.co.jp/x/a\n\n商品B\n2,000円\n詳細 はこちら\nhttps://item.rakuten.co.jp/x/b" in out


def test_heading_gets_blank_before_and_hr_is_short_line():
    out = m.convert(HTML)
    assert "ご利用ください\n\n■ポイント20倍■\n\n" + m.HR_LINE in out


def test_duplicate_url_nearby_keeps_the_later_one():
    html = '<a href="https://u/1"><img></a><p>タイトル</p><a href="https://u/1">タイトル</a>'
    out = m.convert(html)
    assert out.count("https://u/1") == 1
    assert out.index("タイトル") < out.index("https://u/1")


def test_long_line_wraps_at_break_points_within_width():
    line = "あ" * 60 + "。" + "い" * 60 + "。" + "う" * 30
    out = m.convert(f"<p>{line}</p>")
    assert all(len(x) <= m.WIDTH for x in out.splitlines())
    assert "".join(out.split()) == line  # 文字は 1 つも失わない
    assert out.splitlines()[0].endswith("。")


def test_url_is_never_split():
    url = "https://example.com/" + "a" * 130
    out = m.convert(f"<p>見出し</p><a href='{url}'>x</a>")
    assert url in out.splitlines()


def test_text_before_url_in_same_line_is_cut_before_url():
    long = "あ" * 90 + " 詳細は " + "https://example.com/abc"
    assert all(len(x) <= m.WIDTH for x in m.wrap_line(long))
    assert "https://example.com/abc" in m.wrap_line(long)


def test_inspect_counts_and_flags():
    text = "あ" * 10 + "\n" + "😀" + "\n"
    r = m.inspect(text)
    assert r.chars == 12 and r.chars_crlf == 13 and r.longest == 10
    assert r.unencodable == ["😀"]


def test_main_over_limit_does_not_write(tmp_path):
    src = tmp_path / "in.txt"
    src.write_text("<p>" + "あ" * 90 + "</p>" * 1 + ("<p>" + "い" * 90 + "</p>") * 60, encoding="utf-8")
    out = tmp_path / "out.txt"
    assert m.main([str(src), "--out", str(out)]) == 2
    assert not out.exists()


def test_main_ok_writes_utf8_lf(tmp_path):
    src = tmp_path / "in.txt"
    src.write_text(HTML, encoding="utf-8")
    out = tmp_path / "out.txt"
    assert m.main([str(src), "--out", str(out)]) == 0
    assert b"\r" not in out.read_bytes()


def test_long_url_line_is_exempt_from_width():
    url = "https://example.com/" + "a" * 130
    out = m.convert(f"<p>見出し</p><a href='{url}'>x</a>")
    r = m.inspect(out)
    assert r.longest > m.WIDTH and r.over_width == []


def test_main_exit_0_with_long_url(tmp_path):
    src = tmp_path / "in.txt"
    src.write_text("<p>見出し</p><a href='https://example.com/" + "a" * 130 + "'>x</a>", encoding="utf-8")
    assert m.main([str(src), "--out", str(tmp_path / "o.txt")]) == 0
