import save_clipboard_tag as s


def test_normalize_lf_and_trim():
    assert s.normalize("\r\n<table>\r\nあ\r\n</table>\r\n\r\n") == "<table>\nあ\n</table>\n"


def test_sjis_len_counts_bytes_with_lf():
    assert s.sjis_len("<td>\nあ\n") == len("<td>\nあ".encode("cp932"))


def test_looks_like_tag():
    assert s.looks_like_tag("<center><TABLE><tr></tr></TABLE></center>")
    assert not s.looks_like_tag("こんにちは")
