"""下書き(または配信予約中)の配信日時を入れ直す。--save のときだけ「下書き保存」を押す(配信予約は押さない)。

運用者がチャットで日時を決め、「Claude が入れる」と指示したときだけ使う(2026-10-26 受付 #32 の実例)。
- 配信予約中の配信に使うと、下書き保存で下書きに戻る(予約の取り消しを兼ねる)。そのあと check → enqueue で予約し直す。
- 書き込みは Guard が止め、「下書き保存」を押す直前の 30 秒だけ通す。保存前に画面の値(予約・すべての友だち・日時)を確かめ、違えば保存しない。
使い方(C:\\ClaudeCode\\900.ClaudeCode で):
  python ..\\.claude\\skills\\rakuten-line-reserve\\scripts\\set_schedule.py <ID> 2026/10/26 18:30          入れるだけ(保存しない)
  python ..\\.claude\\skills\\rakuten-line-reserve\\scripts\\set_schedule.py <ID> 2026/10/26 18:30 --save   下書き保存まで
"""
import argparse
import json
import re
import sys

sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, r"C:\ClaudeCode\900.ClaudeCode")

from a_browser_common.browser_session import open_session  # noqa: E402
from lineoa_reserve import config, page  # noqa: E402
from lineoa_reserve.guard import Guard  # noqa: E402


def fill(pg, name: str, value: str) -> None:
    box = pg.locator(f"input[name='{name}']")
    box.click()
    box.press("Control+a")
    box.press_sequentially(value, delay=60)
    box.press("Enter")
    pg.wait_for_timeout(500)
    pg.locator("main").click(position={"x": 5, "y": 5})  # ピッカーを閉じる
    pg.wait_for_timeout(500)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("draft_id")
    ap.add_argument("date", help="YYYY/MM/DD")
    ap.add_argument("time", help="HH:MM")
    ap.add_argument("--account", default="@072pxwvf")
    ap.add_argument("--save", action="store_true")
    a = ap.parse_args()
    if a.account not in config.ACCOUNTS:
        print(f"対象にしてよいアカウントではありません: {a.account}")
        return 2
    g = Guard()
    with open_session(config.SITE, headless=True) as ctx:
        ctx.route("**/*", g.route)
        pg = page.open_edit(ctx, a.account, a.draft_id)
        pg.locator("input[type=radio][name='radio']").nth(1).check(force=True)  # 2 つ目=予約
        pg.wait_for_timeout(800)
        fill(pg, "datepicker", a.date)
        fill(pg, "timepicker", a.time)
        tz = sorted(set(re.findall(r"UTC\s*[+-]\s*\d+:\d+", pg.locator("main").inner_text())))
        s = page.read_screen(pg)
        print(json.dumps({**{k: v for k, v in s.items() if k != "text"}, "tz": tz}, ensure_ascii=False))
        ok = s["scheduled"] and s["all_friends"] and s["date"] == a.date and s["time"] == a.time and tz == ["UTC +9:00"]
        if not ok:
            print("NG: 画面の値が期待と違うので保存しません")
            return 2
        if a.save:
            btn = pg.get_by_role("button", name=re.compile(r"^\s*下書き保存\s*$")).locator("visible=true")
            if btn.count() < 1:
                print("NG: 「下書き保存」が見つかりません")
                return 2
            g.arm()
            btn.first.click()
            pg.wait_for_timeout(5000)
            g.disarm()
            data = page.read_broadcast(ctx, a.account, a.draft_id)
            print("保存後", json.dumps({k: data.get(k) for k in ("status", "immediate", "scheduledDate")}))
    print("\n".join(x for x in g.log if "manager.line.biz" in x and "/switch" not in x and "broadcastReach" not in x))
    return 0


if __name__ == "__main__":
    sys.exit(main())
