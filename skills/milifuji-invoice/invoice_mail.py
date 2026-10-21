"""ミリタリーショップFUJI 宛ての請求書メール: PDF 出力 → 本文 → 080 自動送信の依頼 → 結果待ち。

使い方は SKILL.md。承認(運用者)と DeepSeek レビュー(Claude)はこのスクリプトの外で行う。
"""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
import zipfile
from datetime import date, datetime, timedelta, timezone
from email import message_from_bytes, policy
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")

CUSTOMER_CODE = "0000029000"
CUSTOMER_LABEL = "ミリタリーショップFUJI"
TO = ["milifuji@fuji-dobuita.com"]
CC = ["kawasaki@seifukunofuji.com"]
API_URL = "https://sfuji.f5.si/conphasapi/api/external/invoice/pdf"
SECRETS = Path(r"C:\ClaudeCode\013.CONPHAS-PWA\server\external_secrets.json")
MAIL_DRAFT = Path(r"C:\ClaudeCode\.claude\skills\mail-send\mail_draft.py")
CLI_080 = Path(r"C:\ClaudeCode\080.メール自動送信\cli.py")
DOWNLOADS = Path.home() / "Downloads"
NAS_TMP = Path(r"\\192.168.1.50\共有スペース\寺下\999.一時ファイル")
JST = timezone(timedelta(hours=9))

SIGNATURE = """━－━－━－━－━
寺下貴之（てらした たかゆき）
terashita@seifukunofuji.com
  -=-=-=-=-
〒737-0046
　広島県呉市中通１丁目１番２１号
　(株)制服のフジ
TEL：0823-21-7731
FAX：0823-25-0130
Mob：080-1925-1031
━－━－━－━－━
"""


def default_period(today: date) -> tuple[date, date]:
    """締め日は毎月 20 日。今日以前で最も新しい 20 日が終わり、その前月 21 日が始まり。"""
    end = today.replace(day=20) if today.day >= 20 else (today.replace(day=1) - timedelta(days=1)).replace(day=20)
    prev_month_last = end.replace(day=1) - timedelta(days=1)
    return prev_month_last.replace(day=21), end


def parse_period(args: argparse.Namespace) -> tuple[date, date]:
    if args.start and args.end:
        return date.fromisoformat(args.start), date.fromisoformat(args.end)
    return default_period(date.today())


def jp_date(d: date) -> str:
    return f"{d.year}年{d.month}月{d.day}日"


def range_text(start: date, end: date) -> str:
    """同じ年なら終わりの年を省く(例: 2026年9月21日〜10月20日)。"""
    tail = f"{end.month}月{end.day}日" if start.year == end.year else jp_date(end)
    return f"{jp_date(start)}〜{tail}"


def pdf_name(start: date, end: date) -> str:
    return f"請求書_{CUSTOMER_LABEL}_{start:%Y%m%d}-{end:%Y%m%d}.pdf"


def subject(start: date, end: date) -> str:
    return f"【請求書】{range_text(start, end)}分 / 制服のフジ"


def body_text(start: date, end: date) -> str:
    return (
        "【秘密事項】（開示範囲：取引関係者に限る）\n"
        f"To：{CUSTOMER_LABEL} 御中\n"
        "Cc：\n\n"
        "いつもお世話になっております。\n"
        "寺下＠制服のフジです。\n\n"
        f"{range_text(start, end)}分の請求書を添付にてお送りします。\n"
        "ご確認のほど、よろしくお願いいたします。\n\n\n"
        "以上、宜しくお願い致します。\n\n" + SIGNATURE
    )


def cmd_pdf(args: argparse.Namespace) -> int:
    start, end = parse_period(args)
    keys = json.loads(SECRETS.read_text(encoding="utf-8"))["external_api_keys"]
    key = keys[0]["key"] if isinstance(keys[0], dict) else keys[0]
    q = urllib.parse.urlencode({"customer_code": CUSTOMER_CODE, "start_date": start.isoformat(),
                                "end_date": end.isoformat()})
    req = urllib.request.Request(f"{API_URL}?{q}", headers={"X-API-Key": key})
    try:
        resp = urllib.request.urlopen(req, timeout=120)
    except urllib.error.HTTPError as e:
        print("APIエラー", e.code, e.read()[:200].decode("utf-8", "replace"))
        return 1
    data = resp.read()
    if not data.startswith(b"%PDF"):
        print("PDF ではない応答です")
        return 1
    out_dir = Path(args.out_dir) if args.out_dir else DOWNLOADS
    out = out_dir / pdf_name(start, end)
    out.write_bytes(data)
    print(f"保存: {out} ({len(data)} bytes) 明細行数={resp.headers.get('X-Row-Count')} 期間={resp.headers.get('X-Period')}")
    if not args.out_dir:
        shutil.copy2(out, NAS_TMP / out.name)
        print(f"NAS にも保存: {NAS_TMP / out.name}")
    return 0


def cmd_body(args: argparse.Namespace) -> int:
    start, end = parse_period(args)
    path = Path(args.out)
    path.write_text(body_text(start, end), encoding="utf-8", newline="")
    print(f"本文: {path}\n件名: {subject(start, end)}")
    return 0


def build_job(eml: bytes, start: date, end: date, four_at: str, ds_at: str) -> tuple[str, bytes]:
    msg = message_from_bytes(eml, policy=policy.default)
    atts = [(p.get_filename(), p.get_payload(decode=True)) for p in msg.iter_attachments()]
    sha = hashlib.sha256(eml).hexdigest()
    job_id = uuid.uuid4().hex
    meta = {
        "spec": "077-job-v1", "job_id": job_id,
        "created_at": datetime.now(JST).isoformat(timespec="seconds"),
        "eml_sha256": sha, "to": TO, "cc": CC, "subject": str(msg["Subject"]),
        "purpose": f"取引先への月次請求書({start.month}/{start.day}〜{end.month}/{end.day}分)の送付",
        "attachments": [{"name": n, "size": len(d), "sha256": hashlib.sha256(d).hexdigest()} for n, d in atts],
        "checks": {
            "four_stage": {"eml_sha256": sha, "at": four_at, "result": "pass"},
            "deepseek_review": {"eml_sha256": sha, "at": ds_at, "verdict": "pass"},
        },
    }
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("job.json", json.dumps(meta, ensure_ascii=False, indent=2))
        zf.writestr("message.eml", eml)
    return job_id, buf.getvalue()


def cmd_submit(args: argparse.Namespace) -> int:
    start, end = parse_period(args)
    work = Path(args.work_dir)
    work.mkdir(parents=True, exist_ok=True)
    body = work / "invoice_body.txt"
    body.write_text(body_text(start, end), encoding="utf-8", newline="")
    pdf = DOWNLOADS / pdf_name(start, end)
    if not pdf.exists():
        print(f"PDF がありません。先に pdf を実行してください: {pdf}")
        return 1
    eml_path = work / "invoice.eml"
    r = subprocess.run([sys.executable, str(MAIL_DRAFT), "--no-open", "--to", ",".join(TO), "--cc", ",".join(CC),
                        "--subject", subject(start, end), "--body-file", str(body), "--attach", str(pdf),
                        "--out", str(eml_path)], capture_output=True, text=True, encoding="utf-8")
    print(r.stdout.strip())
    if r.returncode != 0 or "[OK] 検証通過" not in r.stdout:
        print(r.stderr.strip())
        return 1
    job_id, data = build_job(eml_path.read_bytes(), start, end, args.four_stage_at, args.deepseek_at)
    zip_path = work / f"{job_id}.zip"
    zip_path.write_bytes(data)
    print(f"依頼zip: {zip_path}")
    if args.dry_run:
        print("dry-run: 提出していません")
        return 0
    r = subprocess.run([sys.executable, str(CLI_080), "submit", str(zip_path)], capture_output=True,
                       text=True, encoding="utf-8", cwd=str(CLI_080.parent))
    print(r.stdout.strip(), r.stderr.strip())
    return r.returncode


def cmd_wait(args: argparse.Namespace) -> int:
    pending = ("awaiting_approval", "approved", "queued", "sending")
    deadline = time.time() + args.minutes * 60
    line = ""
    while time.time() < deadline:
        r = subprocess.run([sys.executable, str(CLI_080), "status", "--all"], capture_output=True, text=True,
                           encoding="utf-8", cwd=str(CLI_080.parent))
        line = next((ln for ln in r.stdout.splitlines() if ln.startswith(args.job_id[:8])), "")
        if line and not any(p in line for p in pending):
            break
        time.sleep(10)
    time.sleep(30)  # 送信済みの写しの照合(copy=OK)を待つ
    r = subprocess.run([sys.executable, str(CLI_080), "status", "--all"], capture_output=True, text=True,
                       encoding="utf-8", cwd=str(CLI_080.parent))
    print(next((ln for ln in r.stdout.splitlines() if ln.startswith(args.job_id[:8])), line or "状態不明"))
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("pdf", "body", "submit"):
        s = sub.add_parser(name)
        s.add_argument("--start", help="YYYY-MM-DD(省略時は直近の締め日から自動)")
        s.add_argument("--end", help="YYYY-MM-DD")
        if name == "pdf":
            s.add_argument("--out-dir", help="試験用。指定時は Downloads/NAS へ置かない")
        if name == "body":
            s.add_argument("--out", required=True)
        if name == "submit":
            s.add_argument("--work-dir", required=True)
            s.add_argument("--four-stage-at", required=True, help="4段階チェックを実際に行った時刻(ISO・JST)")
            s.add_argument("--deepseek-at", required=True, help="DeepSeek レビューを実際に行った時刻(ISO・JST)")
            s.add_argument("--dry-run", action="store_true")
    w = sub.add_parser("wait")
    w.add_argument("job_id")
    w.add_argument("--minutes", type=int, default=30)
    args = ap.parse_args()
    return {"pdf": cmd_pdf, "body": cmd_body, "submit": cmd_submit, "wait": cmd_wait}[args.cmd](args)


if __name__ == "__main__":
    raise SystemExit(main())
