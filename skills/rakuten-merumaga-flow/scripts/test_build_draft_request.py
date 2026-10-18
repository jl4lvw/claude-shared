import json

import pytest

import build_draft_request as b

HTML = (
    "<center><table><tr><td><p>いつもありがとうございます！</p></td></tr>"
    "<tr><td>商品A</td></tr><tr><td>1,000円</td></tr>"
    '<tr><td><a href="https://item.rakuten.co.jp/seifukunofuji/g1"><img></a></td></tr></table></center>'
)


def test_build_full_payload_shape_and_no_destination_fields():
    req = b.build(HTML, "【10/16】マラソン最終日", "10/16 マラソン最終日")
    assert req["source"] == "TK" and req["kinds"] == ["html", "mobile_text"]
    assert req["html"]["subject"] == "【10/16】マラソン最終日"
    assert req["html"]["body"].startswith("<center>")
    assert req["html"]["text_body"] == req["mobile_text"]["body"]
    assert "いつもありがとうございます！" in req["mobile_text"]["body"]
    assert "subject" not in req["mobile_text"]  # モバイルの件名は A の固定値


def test_kinds_html_only_and_mobile_only():
    assert "mobile_text" not in b.build(HTML, "件名", "t", ("html",))
    assert "html" not in b.build(HTML, "件名", "t", ("mobile_text",))


@pytest.mark.parametrize("subject", ["", "   ", "あ" * 200])
def test_bad_subject(subject):
    with pytest.raises(b.RequestError):
        b.build(HTML, subject, "t")


def test_bad_inputs():
    with pytest.raises(b.RequestError):
        b.build("こんにちは", "件名", "t")  # table が無い
    with pytest.raises(b.RequestError):
        b.build(HTML, "件名", " ")
    with pytest.raises(b.RequestError):
        b.build(HTML, "件名", "t", ("sms",))


def test_html_too_large():
    big = "<table><tr><td>" + "a" * (b.HTML_MAX_BYTES + 1) + "</td></tr></table>"
    with pytest.raises(b.RequestError, match="大きすぎ"):
        b.build(big, "件名", "t")


def test_mobile_over_limit_is_refused_not_trimmed():
    rows = "".join(f"<tr><td>{'い' * 90}{i}</td></tr>" for i in range(60))
    with pytest.raises(b.RequestError, match="超えます"):
        b.build(f"<table>{rows}</table>", "件名", "t")


def test_main_writes_json_and_hides_body(tmp_path, capsys):
    tag = tmp_path / "tag.txt"
    tag.write_text(HTML, encoding="utf-8")
    out = tmp_path / "req.json"
    assert b.main(["--tag", str(tag), "--subject", "件名", "--template-name", "T", "--out", str(out)]) == 0
    assert json.loads(out.read_text(encoding="utf-8"))["source"] == "TK"
    assert "商品A" not in capsys.readouterr().out


def test_main_error_exit_2_and_no_file(tmp_path):
    tag = tmp_path / "tag.txt"
    tag.write_text(HTML, encoding="utf-8")
    out = tmp_path / "req.json"
    assert b.main(["--tag", str(tag), "--subject", "", "--template-name", "T", "--out", str(out)]) == 2
    assert not out.exists()
