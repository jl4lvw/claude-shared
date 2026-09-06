"""Caddy が配信している *.sfuji.f5.si と LAN 内 DNS の登録状況を突き合わせる。

本社ルーター(RTX1300)は MAP-E 回線でヘアピン NAT 非対応のため、静的 DNS に
登録されていないサブドメインは **LAN 内から到達できない**(公開 IP に解決され、
そこへは戻ってこられない)。外部からは開けるので気づきにくく、実際に 8 件が
埋もれていた(2026-09-07 発見)。

判定は「解決先がプライベート IP か否か」だけで行う。ルーター側の設定を読める
権限がここには無いので、**運用者の一覧を写した表ではなく実際の名前解決**を正とする。

使い方:
    python .claude/skills/subdomain-add/scripts/audit_subdomain_dns.py
    python .claude/skills/subdomain-add/scripts/audit_subdomain_dns.py --host newpwa

終了コード: 0=全件登録済み / 1=未登録あり / 2=前提が崩れている(Caddyfile が無い等)
"""
from __future__ import annotations

import argparse
import ipaddress
import re
import socket
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")

DEFAULT_CADDYFILE = Path(r"C:\ClaudeCode\inventory-replenishment-pwa\Caddyfile")
DOMAIN = "sfuji.f5.si"
HOST_RE = re.compile(r"\b([a-z0-9][a-z0-9.-]*\.)?" + DOMAIN.replace(".", r"\.") + r"\b")


def parse_caddy_hosts(caddyfile: Path) -> list[str]:
    """Caddyfile のサイトアドレス行から *.sfuji.f5.si を抜き出す。

    コメント行と reverse_proxy 等の本文行は除く(サイトアドレス行は `{` で終わる)。
    1 行に複数ホストをカンマ区切りで書く形式にも対応する。
    """
    hosts: set[str] = set()
    for line in caddyfile.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or not stripped.endswith("{"):
            continue
        if line[:1].isspace():  # インデントされている = ディレクティブ内のブロック
            continue
        for match in HOST_RE.finditer(stripped):
            hosts.add(match.group(0))
    return sorted(hosts)


def resolve(host: str) -> str | None:
    try:
        return socket.gethostbyname(host)
    except OSError:
        return None


def is_lan(addr: str | None) -> bool:
    if not addr:
        return False
    return ipaddress.ip_address(addr).is_private


def main() -> int:
    parser = argparse.ArgumentParser(description="LAN 内 DNS の登録漏れを検出する")
    parser.add_argument("--caddyfile", type=Path, default=DEFAULT_CADDYFILE)
    parser.add_argument(
        "--host",
        action="append",
        default=[],
        help="Caddyfile ではなくこのホスト名だけを見る(サブドメイン名のみでも可)",
    )
    args = parser.parse_args()

    if args.host:
        hosts = [h if h.endswith(DOMAIN) else f"{h}.{DOMAIN}" for h in args.host]
    else:
        if not args.caddyfile.exists():
            print(f"Caddyfile が見つかりません: {args.caddyfile}", file=sys.stderr)
            return 2
        hosts = parse_caddy_hosts(args.caddyfile)
        if not hosts:
            print(f"Caddyfile に {DOMAIN} の vhost がありません: {args.caddyfile}", file=sys.stderr)
            return 2

    missing: list[str] = []
    width = max(len(h) for h in hosts)
    for host in hosts:
        addr = resolve(host)
        ok = is_lan(addr)
        if not ok:
            missing.append(host)
        mark = "OK  " if ok else "未登録"
        print(f"{mark} {host:<{width}} -> {addr or '解決失敗'}")

    print(f"\n{len(hosts)}件中 未登録 {len(missing)}件")
    if not missing:
        print("LAN 内からの到達性は全件確保されています。")
        return 0

    print("\n未登録は LAN 内から到達できません(外部からは開けるため気づきにくい)。")
    print("TK へ以下を relay で依頼してください:\n")
    for host in missing:
        print(f"  DNS追加依頼: {host} -> 192.168.1.166")
    print("\n  python .claude/skills/relay/scripts/relay_client.py send --to TK --type task \"...\"")
    return 1


if __name__ == "__main__":
    sys.exit(main())
