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


def test_body_uses_html_subject_and_counts():
    body = rn.build_body([rn.parse(SAMPLE), _mobile()])
    assert body == ("◆楽天メルマガ 配信予約完了\n\n"
                    "10/24(土) 15:00 に配信されます。\n"
                    "件名：本日20時〜＼6時間限定10%OFFクーポン／Tシャツなど新作も続々♪\n"
                    "PC用 1,967人 / 携帯用 766人\n")
    assert "https://" not in body  # URL は --url が足す


def test_body_lists_each_time_when_they_differ():
    body = rn.build_body([rn.parse(SAMPLE), _mobile("2026-10-24 20:00:00")])
    assert "PC用：10/24(土) 15:00 に配信" in body
    assert "携帯用：10/24(土) 20:00 に配信" in body
