"""reserve_notice.py のテスト(メール検索には行かない)。"""
from __future__ import annotations

import sys
from email.message import Message
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import reserve_notice as rn  # noqa: E402

SAMPLE = """この度、下記配信予約を受け付けました。
・rmail_id：26792904
・申請種別：R-Mail配信予約
・実施日時：2026-10-24 14:11:04
・配信予約日時：2026-10-24 15:00:00
・メールタイプ：HTMLメール
・メール件名：本日20時〜＼6時間限定10%OFFクーポン／Tシャツなど新作も続々♪
・指定条件：
  全件検索
・送信人数：1967
"""


def _mobile(send_at: str = "2026-10-24 15:00:00") -> rn.Reservation:
    return rn.Reservation("26792914", send_at, "モバイルテキスト", "【制服のフジ楽天市場店通信】", 766)


def test_parse_reads_fields():
    r = rn.parse(SAMPLE)
    assert r == rn.Reservation("26792904", "2026-10-24 15:00:00", "HTMLメール",
                               "本日20時〜＼6時間限定10%OFFクーポン／Tシャツなど新作も続々♪", 1967)
    assert rn.parse("関係のないメール") is None


def test_plain_text_works_without_get_body():
    # 古い形式の Message(get_body が無い)からも本文が取れること(2026-10-24 見落としの実例)
    m = Message()
    m.set_payload(SAMPLE.encode("iso-2022-jp"))
    m.set_type("text/plain")
    m.set_param("charset", "iso-2022-jp")
    m["Content-Transfer-Encoding"] = "7bit"
    assert "rmail_id：26792904" in rn.plain_text(m)


def test_body_is_short_and_points_to_image():
    body = rn.build_body([rn.parse(SAMPLE), _mobile()], checked=True)
    assert body == ("◆楽天メルマガ 配信予約完了\n"
                    "10/24(土) 15:00 配信(PC用・携帯用)\n"
                    "リンク・在庫を確認しました。\n"
                    "詳しくは画像をご確認ください。\n")
    assert "https://" not in body  # URL は --url が足す


def test_body_lists_each_time_when_they_differ():
    body = rn.build_body([rn.parse(SAMPLE), _mobile("2026-10-24 20:00:00")])
    assert "PC用 10/24(土) 15:00 / 携帯用 10/24(土) 20:00 配信" in body


FINDINGS = [
    {"level": "ok", "url": "https://item.rakuten.co.jp/seifukunofuji/g2243/", "name": "部隊識別帽",
     "message": "OK(在庫 計125・2バリエーション)"},
    {"level": "warn", "url": "https://item.rakuten.co.jp/seifukunofuji/g1510/", "name": "Tシャツ",
     "message": "一部欠品(在庫 計3)"},
    {"level": "ok", "url": "https://www.rakuten.ne.jp/gold/seifukunofuji/", "name": "", "message": "到達OK"},
    {"level": "warn", "url": "https://item.rakuten.co.jp/seifukunofuji/c/0000000418/", "name": "＞アウトドア",
     "message": "リンク切れの疑い(HTTP 404)"},
]


def test_spec_has_view_table_and_link_table():
    spec = rn.build_spec([rn.parse(SAMPLE), _mobile()], FINDINGS)
    view, links = spec["sections"][0]["grid"], spec["sections"][1]["grid"]
    assert view["header"] == ["観点", "結果"] and view["ok_col"] == 1
    results = dict(view["rows"])
    assert results["商品リンクが正しい商品につながる(2点)"] == "要確認"  # 一部欠品の警告がある
    assert results["そのほかのリンクが開ける(2本)"] == "要確認"  # 404 がある
    assert links["rows"][0] == ["1", "部隊識別帽", "G2243", "125", "OK"]
    assert links["rows"][1][4] == "要確認" and 1 in links["stop_rows"]
    assert links["rows"][-1] == ["4", "そのほかのリンク", "1 本", "―", "OK"]


def test_accept_marks_reviewed_warning_ok_and_strips_emoji():
    f = [{"level": "warn", "url": "https://www.rakuten.co.jp/seifukunofuji/contents/kagurazakaevent/",
          "name": "詳しくは下記リンクをCHECK👀", "message": "名前が合いません"}]
    spec = rn.build_spec([rn.parse(SAMPLE)], f, frozenset({f[0]["url"]}))
    view, links = spec["sections"][0]["grid"], spec["sections"][1]["grid"]
    assert dict(view["rows"])["そのほかのリンクが開ける(1本)"] == "OK"
    assert links["rows"][0] == ["1", "詳しくは下記リンクをCHECK", "リンク", "―", "OK(確認済)"]
    assert links["stop_rows"] == []


def test_spec_without_check_has_only_view_table():
    spec = rn.build_spec([rn.parse(SAMPLE), _mobile()])
    assert len(spec["sections"]) == 1 and all(r[1] == "OK" for r in spec["sections"][0]["grid"]["rows"])
