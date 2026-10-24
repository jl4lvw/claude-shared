"""発注メールを、LINE WORKS(見張り役)の承認で自動送信する(080.メール自動送信・077-job-v1)。

使い方:
    python send_via_080.py submit --to a@x --subject 件名 --body-file 本文.txt --attach 注文書.pdf \
        --purpose "2027年版カレンダーの仕入先への注文" --work-dir C:/tmp-ai/xxx \
        --four-stage-at 2026-10-22T11:54:00+09:00 --deepseek-at 2026-10-22T11:55:34+09:00 [--cc ...] [--dry-run]
    python send_via_080.py wait <job_id> [--minutes 30]

承認は運用者本人だけが行う(Claude は承認しない)。添付のあるメールは「承認の画面」でだけ承認できる。
添付なしは --chat で、運用者がチャットで「OK」と返事をすれば送れる(chat 承認・080 で有効なときだけ)。
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import subprocess
import sys
import time
import uuid
import zipfile
from datetime import datetime, timedelta, timezone
from email import message_from_bytes, policy
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")

JST = timezone(timedelta(hours=9))
CLI_080 = Path(r"C:\ClaudeCode\080.メール自動送信\cli.py")
MAIL_DRAFT = Path(r"C:\ClaudeCode\.claude\skills\mail-send\mail_draft.py")



def build_job(eml: bytes, purpose: str, four_at: str, ds_at: str) -> tuple[str, bytes]:
    msg = message_from_bytes(eml, policy=policy.default)
    atts = [(p.get_filename(), p.get_payload(decode=True)) for p in msg.iter_attachments()]
    sha = hashlib.sha256(eml).hexdigest()
    job_id = uuid.uuid4().hex

    def addrs(header: str) -> list[str]:
        return [a.addr_spec for a in (msg[header].addresses if msg[header] else [])]

    meta = {
        "spec": "077-job-v1", "job_id": job_id,
        "created_at": datetime.now(JST).isoformat(timespec="seconds"),
        "eml_sha256": sha, "to": addrs("To"), "cc": addrs("Cc"), "subject": str(msg["Subject"]),
        "purpose": purpose,
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
    work = Path(args.work_dir)
    work.mkdir(parents=True, exist_ok=True)
    eml_path = work / "message_for_080.eml"
    cmd = [sys.executable, str(MAIL_DRAFT), "--no-open", "--to", args.to, "--subject", args.subject,
           "--body-file", args.body_file, "--out", str(eml_path)]
    if args.cc:
        cmd += ["--cc", args.cc]
    if args.sender:
        cmd += ["--from", args.sender]
    if args.attach:
        cmd += ["--attach", *args.attach]
    r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8")
    print(r.stdout.strip())
    if r.returncode != 0 or "[OK] 検証通過" not in r.stdout:
        print(r.stderr.strip())
        return 1
    job_id, data = build_job(eml_path.read_bytes(), args.purpose, args.four_stage_at, args.deepseek_at)
    zip_path = work / f"{job_id}.zip"
    zip_path.write_bytes(data)
    print(f"job_id: {job_id}\n依頼zip: {zip_path}")
    if args.dry_run:
        print("dry-run: 提出していません")
        return 0
    submit = [sys.executable, str(CLI_080), "submit", str(zip_path)]
    if args.chat:
        # chat 承認(添付なしだけ)。セッションIDは Claude が手で渡さず、実行中のセッションのものを使う
        sid = os.environ.get("CLAUDE_CODE_SESSION_ID", "")
        if sid:
            submit += ["--chat-session", sid]
        else:
            print("セッションIDが取れないので、chat 承認にしません(承認の画面で承認してください)")
    r = subprocess.run(submit, capture_output=True, text=True, encoding="utf-8", cwd=str(CLI_080.parent))
    print(r.stdout.strip(), r.stderr.strip())
    if r.returncode != 0 or "確認カード(ここから)" in r.stdout or args.no_link:
        return r.returncode
    print_approval_link(job_id, args.link_wait)
    return 0


def print_approval_link(job_id: str, wait_sec: int) -> None:
    """見張り役が承認を求め始めるのを待ち、承認の画面でこのメールを直接開くリンクを出す(2026-10-24 運用者決定)。
    出たリンクはチャットにそのまま示す。承認は運用者が画面で行う(Claude はリンクを開かない・押さない)。"""
    deadline = time.time() + wait_sec
    while True:
        r = subprocess.run([sys.executable, str(CLI_080), "approval-link", job_id], capture_output=True, text=True,
                           encoding="utf-8", cwd=str(CLI_080.parent))
        if r.returncode == 0 and r.stdout.strip().startswith("https://"):
            print(f"承認の画面(このメールを直接開く): {r.stdout.strip()}")
            return
        if time.time() >= deadline:
            print(f"承認の画面への直リンクはまだ出せません({r.stdout.strip()})。"
                  f"後で `python {CLI_080} approval-link {job_id[:8]}` で出せます")
            return
        time.sleep(5)


def cmd_wait(args: argparse.Namespace) -> int:
    pending = ("received", "awaiting_approval", "approved", "queued", "sending", "send_invoked")
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
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("submit")
    s.add_argument("--to", required=True)
    s.add_argument("--cc", default="")
    s.add_argument("--sender", default="", help="差出人。省略時は mail_draft の既定(terashita@)")
    s.add_argument("--subject", required=True)
    s.add_argument("--body-file", required=True)
    s.add_argument("--attach", nargs="*", default=[])
    s.add_argument("--purpose", required=True, help="6〜120字。人名・住所・電話・アドレスは書かない")
    s.add_argument("--work-dir", required=True)
    s.add_argument("--four-stage-at", required=True, help="4段階チェックを実際に行った時刻(ISO・JST)")
    s.add_argument("--deepseek-at", required=True, help="DeepSeek レビューを実際に行った時刻(ISO・JST)")
    s.add_argument("--dry-run", action="store_true")
    s.add_argument("--no-link", action="store_true", help="承認の画面への直リンクを待たない")
    s.add_argument("--link-wait", type=int, default=120, help="直リンクが出るまで待つ秒数(既定 120)")
    s.add_argument("--chat", action="store_true",
                   help="チャットの返事で承認を受ける(添付なしだけ)。出力の確認カードをそのままチャットに出す")
    w = sub.add_parser("wait")
    w.add_argument("job_id")
    w.add_argument("--minutes", type=int, default=30)
    args = ap.parse_args()
    return {"submit": cmd_submit, "wait": cmd_wait}[args.cmd](args)


if __name__ == "__main__":
    raise SystemExit(main())
