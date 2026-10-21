import json

import pytest

import build_draft_request as b

PURPOSE = "10/16 マラソン最終日の楽天メルマガを RMS の下書きに登録するため"


def build(*args, **kw) -> dict:
    """目的(purpose)は必須(2026-10-21)。各試験では決まった目的を付けて呼ぶ。"""
    kw.setdefault("purpose", PURPOSE)
    return b.build(*args, **kw)


HTML = (
    "<center><table><tr><td><p>いつもありがとうございます！</p></td></tr>"
    "<tr><td>商品A</td></tr><tr><td>1,000円</td></tr>"
    '<tr><td><a href="https://item.rakuten.co.jp/seifukunofuji/g1"><img></a></td></tr></table></center>'
)


def test_build_full_payload_shape_and_no_destination_fields():
    req = build(HTML, "【10/16】マラソン最終日", "10/16 マラソン最終日")
    assert req["source"] == "TK" and req["kinds"] == ["html", "mobile_text"]
    assert req["html"]["subject"] == "【10/16】マラソン最終日"
    assert req["html"]["body"].startswith("<center>")
    assert req["html"]["text_body"] == req["mobile_text"]["body"]
    assert "いつもありがとうございます！" in req["mobile_text"]["body"]
    assert "subject" not in req["mobile_text"]  # モバイルの件名は A の固定値


def test_kinds_html_only_and_mobile_only():
    assert "mobile_text" not in build(HTML, "件名", "t", ("html",))
    assert "html" not in build(HTML, "件名", "t", ("mobile_text",))


@pytest.mark.parametrize("subject", ["", "   ", "あ" * 200])
def test_bad_subject(subject):
    with pytest.raises(b.RequestError):
        build(HTML, subject, "t")


def test_bad_inputs():
    with pytest.raises(b.RequestError):
        build("こんにちは", "件名", "t")  # table が無い
    with pytest.raises(b.RequestError):
        build(HTML, "件名", " ")
    with pytest.raises(b.RequestError):
        build(HTML, "件名", "t", ("sms",))


@pytest.mark.parametrize("purpose", [None, "下書き", "あ" * 121])
def test_purpose_missing_short_or_long_is_refused(purpose):
    """目的(purpose)は必須・6〜120 字(2026-10-21 運用者決定。A でも同じ検査をする)。"""
    with pytest.raises(b.RequestError, match=r"目的\(purpose\)"):
        b.build(HTML, "件名", "t", purpose=purpose)
    with pytest.raises(TypeError):  # 付け忘れは呼び出しの誤りとして止まる
        b.build(HTML, "件名", "t")


def test_purpose_is_normalized_into_request():
    req = build(HTML, "件名", "t", purpose=" 秋の新作の\nメルマガ下書き ")
    assert req["purpose"] == "秋の新作の メルマガ下書き"


def test_html_too_large():
    big = "<table><tr><td>" + "a" * (b.HTML_MAX_BYTES + 1) + "</td></tr></table>"
    with pytest.raises(b.RequestError, match="大きすぎ"):
        build(big, "件名", "t")


def test_mobile_over_limit_is_refused_not_trimmed():
    rows = "".join(f"<tr><td>{'い' * 90}{i}</td></tr>" for i in range(60))
    with pytest.raises(b.RequestError, match="超えます"):
        build(f"<table>{rows}</table>", "件名", "t")


def test_main_writes_json_and_hides_body(tmp_path, capsys):
    tag = tmp_path / "tag.txt"
    tag.write_text(HTML, encoding="utf-8")
    out = tmp_path / "req.json"
    assert b.main(["--tag", str(tag), "--subject", "件名", "--template-name", "T", "--purpose", PURPOSE, "--out", str(out)]) == 0
    assert json.loads(out.read_text(encoding="utf-8"))["source"] == "TK"
    assert "商品A" not in capsys.readouterr().out


def test_main_error_exit_2_and_no_file(tmp_path):
    tag = tmp_path / "tag.txt"
    tag.write_text(HTML, encoding="utf-8")
    out = tmp_path / "req.json"
    assert b.main(["--tag", str(tag), "--subject", "", "--template-name", "T", "--purpose", PURPOSE, "--out", str(out)]) == 2
    assert not out.exists()
