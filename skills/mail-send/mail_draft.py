"""EML下書きを生成して Thunderbird で開く共通CLI（mail-send スキル専用）。

このスクリプトがあるので、メールのたびに EmailMessage の組み立てコードを書かない。
本文は必ずファイルで渡す（コマンドライン引数に日本語・改行を載せると Windows で壊れる）。

使い方:
    python mail_draft.py --to "a@x.com,b@y.com" --subject "件名" --body-file body.txt
    python mail_draft.py --to ... --subject ... --body-file ... --cc "c@z.com"
    python mail_draft.py --to ... --subject ... --body-file ... --attach report.csv
    python mail_draft.py --to ... --subject ... --body-file ... --no-open   # 開かずに生成だけ

禁止事項（過去に事故済み。理由は SKILL.md 参照）:
    - thunderbird -compose "to=...,body=..." の直叩き（本文が URL エンコードのまま出る）
    - write_text() での EML 書き出し（Windows で CRLF 変換され MIME が壊れる）
"""

from __future__ import annotations

import argparse
import mimetypes
import subprocess
import sys
from email.message import EmailMessage
from email.policy import SMTP
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")

THUNDERBIRD = Path(r"C:\Program Files\Mozilla Thunderbird\thunderbird.exe")
DEFAULT_FROM = "寺下貴之 <terashita@seifukunofuji.com>"
DEFAULT_OUT_DIR = Path(r"C:\ClaudeCode\.mail_drafts")

# 社内の定型宛先。「いつもの3人」= 問合せメールを回す先。
TEAM = {
    "いつもの3人": "fuji@seifukunofuji.com, kaneko@seifukunofuji.com, kentaro@seifukunofuji.com",
}


def build_message(
    to: str,
    subject: str,
    body: str,
    cc: str = "",
    sender: str = DEFAULT_FROM,
    attachments: list[Path] | None = None,
) -> EmailMessage:
    msg = EmailMessage(policy=SMTP)
    msg["From"] = sender
    msg["To"] = to
    if cc:
        msg["Cc"] = cc
    msg["Subject"] = subject
    msg.set_content(body, charset="utf-8")

    for path in attachments or []:
        ctype, _ = mimetypes.guess_type(path.name)
        maintype, subtype = (ctype or "application/octet-stream").split("/", 1)
        msg.add_attachment(
            path.read_bytes(), maintype=maintype, subtype=subtype, filename=path.name
        )
    return msg


def verify_eml(eml_path: Path, expected_body: str, expected_attachments: int) -> None:
    """書き出した EML を読み直して、本文・添付が欠けていないか確かめる。

    「生成した」で終えず結果を検証する（AGENTS.md「反映系の操作は結果を検証して終える」）。
    文字化け・改行変換・添付欠落はここで落とす。
    """
    import email

    parsed = email.message_from_bytes(eml_path.read_bytes(), policy=SMTP)

    got_body = parsed.get_body(preferencelist=("plain",))
    if got_body is None:
        raise SystemExit("[NG] 本文パートが見つかりません")

    actual = got_body.get_content().replace("\r\n", "\n").rstrip("\n")
    expected = expected_body.replace("\r\n", "\n").rstrip("\n")
    if actual != expected:
        raise SystemExit(
            "[NG] 本文が一致しません（文字化け・改行変換の疑い）\n"
            f"  expected {len(expected)} 文字 / actual {len(actual)} 文字"
        )

    n_attach = sum(1 for _ in parsed.iter_attachments())
    if n_attach != expected_attachments:
        raise SystemExit(f"[NG] 添付数が違います: 期待 {expected_attachments} / 実際 {n_attach}")

    print(f"[OK] 検証通過: 本文 {len(actual)} 文字 / 添付 {n_attach} 件")


def main() -> None:
    p = argparse.ArgumentParser(description="EML下書きを生成して Thunderbird で開く")
    p.add_argument("--to", required=True, help="宛先。'いつもの3人' と書くと社内3名に展開")
    p.add_argument("--subject", required=True)
    p.add_argument("--body-file", required=True, type=Path, help="本文のUTF-8テキストファイル")
    p.add_argument("--cc", default="")
    p.add_argument("--from", dest="sender", default=DEFAULT_FROM)
    p.add_argument("--attach", nargs="*", type=Path, default=[])
    p.add_argument("--out", type=Path, default=None, help="EML出力先（既定: .mail_drafts/<本文ファイル名>.eml）")
    p.add_argument("--no-open", action="store_true", help="Thunderbirdを起動しない")
    args = p.parse_args()

    to = TEAM.get(args.to.strip(), args.to)

    if not args.body_file.exists():
        raise SystemExit(f"[NG] 本文ファイルがありません: {args.body_file}")
    body = args.body_file.read_text(encoding="utf-8")

    for a in args.attach:
        if not a.exists():
            raise SystemExit(f"[NG] 添付ファイルがありません: {a}")

    msg = build_message(to, args.subject, body, args.cc, args.sender, args.attach)

    out = args.out or (DEFAULT_OUT_DIR / f"{args.body_file.stem}.eml")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(bytes(msg))  # write_text は使わない

    print(f"[written] {out}  ({out.stat().st_size} bytes)")
    print(f"  To     : {to}")
    if args.cc:
        print(f"  Cc     : {args.cc}")
    print(f"  Subject: {args.subject}")

    verify_eml(out, body, len(args.attach))

    if args.no_open:
        print("[skip] --no-open のため Thunderbird は起動しません")
        return

    if not THUNDERBIRD.exists():
        raise SystemExit(f"[NG] Thunderbird が見つかりません: {THUNDERBIRD}")

    subprocess.Popen([str(THUNDERBIRD), "-file", str(out)])
    print("[open] Thunderbird を -file で起動しました")
    print("       → ユーザーが Ctrl+E で下書きに変換し、内容確認のうえ手動送信します")


if __name__ == "__main__":
    main()
