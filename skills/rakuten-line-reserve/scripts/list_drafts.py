"""LINE 公式アカウントの配信の一覧(下書き・配信予約中)を読むだけ。書き込みの通信は Guard が止める。

使い方(C:\\ClaudeCode\\900.ClaudeCode で):
  python ..\\.claude\\skills\\rakuten-line-reserve\\scripts\\list_drafts.py [--account @072pxwvf]
専用ブラウザ(lineoa のログイン)を使う。切れていたら `python -m lineoa_reserve.cli login`。
"""
import argparse
import datetime as dt
import sys

sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, r"C:\ClaudeCode\900.ClaudeCode")

from a_browser_common.browser_session import open_session  # noqa: E402
from lineoa_reserve import config  # noqa: E402
from lineoa_reserve.guard import Guard  # noqa: E402


def _when(ts: int | None) -> str:
    return dt.datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M") if ts else "(日時なし=今すぐ配信)"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--account", default="@072pxwvf")
    a = ap.parse_args()
    got: dict[str, dict] = {}

    def on_resp(r) -> None:
        if r.request.method == "GET" and "/broadcasts/v2/list?" in r.url:
            status = r.url.split("status=")[1].split("&")[0]
            try:
                got[status] = r.json()
            except Exception:
                pass

    with open_session(config.SITE, headless=True) as ctx:
        ctx.route("**/*", Guard().route)
        pg = ctx.new_page()
        pg.on("response", on_resp)
        pg.goto(f"{config.BASE}/account/{a.account}/broadcast")
        pg.wait_for_timeout(8000)
        if "account.line.biz" in pg.url:
            print("ログインが切れています。python -m lineoa_reserve.cli login")
            return 2
    for status in ("DRAFTED", "SCHEDULED", "SENT"):
        body = got.get(status) or {}
        print(f"[{status}] {body.get('total', '?')} 件(一覧の 1 ページ目)")
        for b in body.get("list", []):
            head = next((x.get("text", "") for x in b.get("balloons", []) if x.get("contentType") == "TEXT"), "")
            kinds = ",".join(x.get("contentType", "?") for x in b.get("balloons", []))
            print(f"  ID {b['id']}  配信 {_when(b.get('scheduledDate'))}  保存 {_when(b.get("savedDate"))}  届いた人数 {b.get("deliveredCount")}"
                  f"  吹き出し[{kinds}]  {head[:30]!r}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
