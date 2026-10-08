"""BoxQR の API キーをファイルに保存する（/bo スキル用）。

キーはプロジェクトの外（ユーザーフォルダ）に置く。このスクリプト自体にはキーの値を書かない。

    C:\\Users\\<ユーザー>\\.boxqr\\order_api_key.txt   （1行・キーだけ）

使い方:
    python bo_setkey.py                 # キーを非表示で入力（画面に出ない）
    python bo_setkey.py --verify        # 保存後にキーが有効か確認（何も変更しない確認）
    python bo_setkey.py --status        # 保存状況の確認（キーの値は表示しない）
    python bo_setkey.py --stdin         # 標準入力から1行読む（自動実行用）
    python bo_setkey.py --force         # 既存のファイルを上書き

保存先は環境変数 BOXQR_KEY_FILE で変えられる。プロジェクト内（C:\\ClaudeCode 配下）は拒否する。
保存後、Windows のアクセス権を現在のユーザーだけに絞る（icacls）。
"""
from __future__ import annotations

import argparse
import getpass
import os
import subprocess
import sys
from pathlib import Path

DEFAULT_KEY_FILE = Path.home() / ".boxqr" / "order_api_key.txt"
FORBIDDEN_PARENT = Path(r"C:\ClaudeCode")   # ここ配下に鍵を置かない（git・同期・誤コミットの対策）
MIN_LEN = 20


class SetKeyError(Exception):
    """利用者に見せてよいメッセージのエラー。"""


def key_path() -> Path:
    return Path(os.environ.get("BOXQR_KEY_FILE") or DEFAULT_KEY_FILE)


def _is_under(path: Path, parent: Path) -> bool:
    try:
        path.resolve().relative_to(parent.resolve())
        return True
    except ValueError:
        return False


def validate_path(path: Path) -> None:
    if _is_under(path, FORBIDDEN_PARENT):
        raise SetKeyError(f"保存先がプロジェクトの中です: {path}\nキーはプロジェクトの外（ユーザーフォルダなど）に置いてください。")


def normalize_key(raw: str) -> str:
    key = (raw or "").strip()
    if not key:
        raise SetKeyError("キーが空です。")
    if any(ch.isspace() for ch in key):
        raise SetKeyError("キーに空白や改行が含まれています（貼り付けの誤りの可能性）。")
    if len(key) < MIN_LEN:
        raise SetKeyError(f"キーが短すぎます（{len(key)}文字）。{MIN_LEN}文字以上のキーを入れてください。")
    if "=" in key and key.split("=", 1)[0].isupper():
        raise SetKeyError("「BOXQR_ORDER_API_KEY=…」の形で入っています。「=」の右側の値だけを入れてください。")
    return key


def restrict_acl(path: Path) -> str:
    """現在のユーザーだけが読み書きできるようにする（失敗しても保存自体は有効）。"""
    user = os.environ.get("USERNAME") or getpass.getuser()
    targets = [path.parent, path]
    msgs = []
    for t in targets:
        grant = f"{user}:(OI)(CI)F" if t.is_dir() else f"{user}:F"
        r = subprocess.run(["icacls", str(t), "/inheritance:r", "/grant:r", grant],
                           capture_output=True, text=True, encoding="cp932", errors="replace")
        msgs.append("OK" if r.returncode == 0 else f"失敗({r.returncode})")
    return "アクセス権を現在のユーザーだけに設定: " + " / ".join(msgs)


def write_key(key: str, path: Path, force: bool = False) -> Path:
    validate_path(path)
    if path.exists() and not force:
        raise SetKeyError(f"すでにファイルがあります: {path}\n上書きするときは --force を付けてください。")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(key + "\n", encoding="utf-8", newline="")
    return path


def status(path: Path) -> list[str]:
    lines = [f"保存先: {path}"]
    if not path.exists():
        lines.append("ファイル: なし")
        return lines
    text = path.read_text(encoding="utf-8").strip()
    lines.append(f"ファイル: あり（{len(text)}文字・値は表示しません）")
    lines.append("プロジェクト外: " + ("はい" if not _is_under(path, FORBIDDEN_PARENT) else "いいえ（要移動）"))
    return lines


def main(argv: list[str] | None = None) -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser(description="BoxQR APIキーの保存")
    ap.add_argument("--stdin", action="store_true", help="標準入力から1行読む")
    ap.add_argument("--force", action="store_true", help="既存ファイルを上書き")
    ap.add_argument("--verify", action="store_true", help="保存後（または現在のファイルで）キーが有効か確認")
    ap.add_argument("--status", action="store_true", help="保存状況の確認だけ行う")
    a = ap.parse_args(argv)
    path = key_path()
    try:
        if a.status:
            print("\n".join(status(path)))
            return 0
        if a.stdin:
            raw: str | None = sys.stdin.readline()
        elif a.verify and path.exists() and not a.force:
            raw = None  # 既存ファイルの検証だけ（入力しない）
        else:
            raw = getpass.getpass("BoxQR の API キーを入力（画面には表示されません）: ")
        if raw is not None:
            key = normalize_key(raw)
            write_key(key, path, a.force)
            print(f"保存しました: {path}（{len(key)}文字・値は表示しません）")
            print(restrict_acl(path))
        if a.verify:
            sys.path.insert(0, str(Path(__file__).parent))
            import bo_api
            ok = bo_api.verify_key()
            print("キーは有効です（何も変更していません）" if ok else "キーが無効です（401）。入力を確認してください。")
            return 0 if ok else 1
    except Exception as e:  # noqa: BLE001 - 利用者に原因を見せて終了コード1（SetKeyError を含む）
        print(f"エラー: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
