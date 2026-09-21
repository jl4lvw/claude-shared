"""session_name.py — セッション名 `072★写真アルバム 4` の生成・検証・台帳。

背景 (2026-09-21 ユーザー決定):
  ハンドオフで起こすセッションも、通常のセッションも、同じ規則で名前を付ける。
  名前は 4 部品:  <数字3桁><端末記号><サブプロジェクト名> <通し番号>
    - 数字     : フォルダの通し番号 (022 / 072 / 042 …)。決まらない作業は省略する
    - 端末記号 : 実行している PC。★=A / ■=TK / ●=TS (PC 名から自動で決まる)
    - 名前     : 今何をやっているかの短い名詞句
    - 通し番号 : プロジェクトではなくセッション自体の連番。**数字+記号ごとの最大値+1**
                 (4 からのハンドオフで 5 が既にあれば 6 になる)

台帳:
  - ハンドオフ側: `.handoff/SESSION_*.md` 先頭 10 行の `次セッション名:` 行
  - 通常セッション側: `.handoff/session_names.log` (追記専用・1 行 = 日時 TAB 名前)
  両方を合わせて「使用済みの名前」とし、通し番号の最大値を求める。

使い方:
  python .claude/tools/session_name.py symbol
  python .claude/tools/session_name.py next --folder 072 --topic 写真アルバム
  python .claude/tools/session_name.py register "072★写真アルバム 4"
  python .claude/tools/session_name.py check "072★写真アルバム 4"

終了コード: 0=成功 / 1=端末名が対応表に無い / 2=名前が不正 / 3=登録済み(重複)
"""
from __future__ import annotations

import argparse
import os
import re
import socket
import sys
import time
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Iterator

try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:  # pragma: no cover  # noqa: BLE001
    pass

HANDOFF_DIR = Path(r"C:/ClaudeCode/.handoff")
LEDGER_NAME = "session_names.log"
MAX_LEN = 40  # handoff/SKILL.md「次セッション名のルール」と同じ上限
HEAD_LINES = 10  # handoff の名前行を探す範囲 (load と同じ)

# PC 名 (小文字) → 端末記号。呼称は reference_pc_terminals_specs のとおり。
TERMINALS: dict[str, str] = {
    "desktop-7osurhd": "★",  # A (AI 間リレーメッセージの端末 A)
    "ryzen7-5800x": "■",  # TK
    "pc-ff11": "●",  # TS
}
SYMBOLS = "".join(TERMINALS.values())

_NAME_RE = re.compile(rf"^(?P<folder>\d{{3}})?(?P<symbol>[{SYMBOLS}])(?P<topic>\S.*?) (?P<serial>\d+)$")
_HANDOFF_NAME_RE = re.compile(r"^次セッション名[:：][ \t]*(.+?)[ \t]*$")


@dataclass(frozen=True)
class ParsedName:
    folder: str  # 3 桁。省略された名前は ""
    symbol: str
    topic: str
    serial: int


def detect_symbol(hostname: str | None = None) -> str | None:
    """この PC の端末記号。対応表に無い PC 名なら None。"""
    host = (hostname if hostname is not None else socket.gethostname()).strip().lower()
    return TERMINALS.get(host)


def parse_name(name: str) -> ParsedName | None:
    """新書式の名前を分解する。旧書式・不正な名前は None。"""
    m = _NAME_RE.match(name)
    if m is None:
        return None
    return ParsedName(m["folder"] or "", m["symbol"], m["topic"], int(m["serial"]))


def problems(name: str) -> list[str]:
    """名前の問題点 (空なら問題なし)。handoff/SKILL.md の要件と同じ基準。"""
    out: list[str] = []
    if not name.strip():
        out.append("空の名前")
    if any(ord(c) < 0x20 or ord(c) == 0x7F for c in name):
        out.append("改行・タブ・制御文字を含む")
    if len(name) > MAX_LEN:
        out.append(f"{len(name)} 文字 (上限 {MAX_LEN})")
    if parse_name(name) is None:
        out.append("書式が `<数字3桁>?<★■●><名前> <通し番号>` ではない")
    return out


def read_handoff_names(handoff_dir: Path) -> list[str]:
    """ハンドオフファイル先頭の `次セッション名:` を全部集める。"""
    names: list[str] = []
    for path in sorted(handoff_dir.glob("SESSION_*.md")):
        try:
            with path.open(encoding="utf-8", errors="replace") as f:
                for _, line in zip(range(HEAD_LINES), f):
                    m = _HANDOFF_NAME_RE.match(line.rstrip("\r\n"))
                    if m:
                        names.append(m[1])
                        break
        except OSError:
            continue
    return names


def read_ledger(handoff_dir: Path) -> list[str]:
    """通常セッションの名前台帳 (session_names.log) を読む。"""
    path = handoff_dir / LEDGER_NAME
    if not path.is_file():
        return []
    names: list[str] = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        name = line.split("\t")[-1].strip()
        if name:
            names.append(name)
    return names


def used_names(handoff_dir: Path) -> list[str]:
    return read_handoff_names(handoff_dir) + read_ledger(handoff_dir)


def next_serial(folder: str, symbol: str, names: list[str]) -> int:
    """同じ (数字, 記号) の最大の通し番号 + 1。無ければ 1。"""
    best = 0
    for name in names:
        p = parse_name(name)
        if p is not None and p.folder == folder and p.symbol == symbol:
            best = max(best, p.serial)
    return best + 1


def build_name(folder: str, symbol: str, topic: str, serial: int) -> str:
    """部品から名前を組み立てる。不正な部品・長すぎる名前は ValueError。"""
    if folder and not re.fullmatch(r"\d{3}", folder):
        raise ValueError(f"数字は 3 桁 (または省略): {folder!r}")
    if symbol not in SYMBOLS:
        raise ValueError(f"記号は {'/'.join(SYMBOLS)} のどれか: {symbol!r}")
    topic = " ".join(topic.split())
    if not topic:
        raise ValueError("サブプロジェクト名が空")
    name = f"{folder}{symbol}{topic} {serial}"
    bad = problems(name)
    if bad:
        raise ValueError("; ".join(bad))
    return name


def suggest(folder: str, topic: str, handoff_dir: Path, symbol: str | None = None) -> str:
    """使用済みの名前と重ならない次の名前を返す (台帳には書かない)。"""
    sym = symbol or detect_symbol()
    if sym is None:
        raise LookupError(f"端末名 {socket.gethostname()!r} が対応表に無い")
    return build_name(folder, sym, topic, next_serial(folder, sym, used_names(handoff_dir)))


@contextmanager
def _locked(ledger: Path, timeout: float = 3.0, stale: float = 30.0) -> Iterator[None]:
    """台帳への同時追記を避ける簡易ロック (並行セッション対策)。"""
    lock = ledger.with_suffix(".lock")
    deadline = time.monotonic() + timeout
    while True:
        try:
            os.close(os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY))
            break
        except FileExistsError:
            try:
                if time.time() - lock.stat().st_mtime > stale:
                    lock.unlink(missing_ok=True)
                    continue
            except OSError:
                pass
            if time.monotonic() > deadline:
                raise TimeoutError(f"台帳のロックが取れない: {lock}") from None
            time.sleep(0.05)
    try:
        yield
    finally:
        lock.unlink(missing_ok=True)


def register(name: str, handoff_dir: Path) -> bool:
    """名前を台帳に追記する。既に使用済みなら False (書かない)。不正なら ValueError。"""
    bad = problems(name)
    if bad:
        raise ValueError("; ".join(bad))
    handoff_dir.mkdir(parents=True, exist_ok=True)
    ledger = handoff_dir / LEDGER_NAME
    with _locked(ledger):
        if name in used_names(handoff_dir):
            return False
        with ledger.open("a", encoding="utf-8", newline="") as f:
            f.write(f"{datetime.now().isoformat(timespec='seconds')}\t{name}\n")
    return True


def _cmd_symbol(_: argparse.Namespace) -> int:
    sym = detect_symbol()
    if sym is None:
        print(f"端末名 {socket.gethostname()!r} が対応表に無い (TERMINALS に追加が必要)", file=sys.stderr)
        return 1
    print(sym)
    return 0


def _cmd_next(args: argparse.Namespace) -> int:
    try:
        print(suggest(args.folder, args.topic, args.handoff_dir, args.symbol))
    except LookupError as exc:
        print(exc, file=sys.stderr)
        return 1
    except ValueError as exc:
        print(f"名前が不正: {exc}", file=sys.stderr)
        return 2
    return 0


def _cmd_register(args: argparse.Namespace) -> int:
    try:
        if not register(args.name, args.handoff_dir):
            print(f"登録済み (重複): {args.name}", file=sys.stderr)
            return 3
    except ValueError as exc:
        print(f"名前が不正: {exc}", file=sys.stderr)
        return 2
    print(f"登録: {args.name}")
    return 0


def _cmd_check(args: argparse.Namespace) -> int:
    bad = problems(args.name)
    if bad:
        print("; ".join(bad), file=sys.stderr)
        return 2
    print("OK")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="セッション名の生成・検証・台帳")
    ap.add_argument("--handoff-dir", type=Path, default=HANDOFF_DIR)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("symbol", help="この PC の端末記号").set_defaults(func=_cmd_symbol)
    p_next = sub.add_parser("next", help="次の名前を提案する (台帳には書かない)")
    p_next.add_argument("--folder", default="", help="フォルダ番号 3 桁 (省略可)")
    p_next.add_argument("--topic", required=True, help="サブプロジェクト名")
    p_next.add_argument("--symbol", default=None, help="端末記号を明示 (既定は PC 名から自動)")
    p_next.set_defaults(func=_cmd_next)
    p_reg = sub.add_parser("register", help="名前を台帳へ追記する")
    p_reg.add_argument("name")
    p_reg.set_defaults(func=_cmd_register)
    p_chk = sub.add_parser("check", help="名前の書式・長さを検証する")
    p_chk.add_argument("name")
    p_chk.set_defaults(func=_cmd_check)
    args = ap.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    sys.exit(main())
