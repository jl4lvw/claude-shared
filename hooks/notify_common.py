"""通知フック(sound_notify.py / lw_phone_notify.py)の共通部品 (2026-10-10)。

Lv3 を観点を変えて 5 回かけたレビューで、次が全回で指摘された。ここで一括して直す:
  - 抑止の記録ファイルが「読む→判定→書く」で排他なし → 並列フックで二重通知・更新消失
  - 壁時計の巻き戻しで、15〜30 秒のはずの抑止が長時間続く
  - ファイルが壊れた/型が違うと、直すまで毎回例外で通知不能
  - 失敗が 3 か所のログに散って見えない

設計:
  - claim(): ロックを取り、窓内なら None、窓外なら今を記録して (前回値, 記録時刻) を返す。
    未来の時刻(時計の巻き戻し)は期限切れ扱い。壊れたファイルは捨てて作り直す。
    ロックが 1 秒取れなくても通知は止めない (fail-open: 二重通知より取りこぼしを嫌う)
  - fail_backoff(): 送信に失敗したら窓を短く縮め、数秒後の再通知は通す
  - ログは relay_local/notify_hook.log の 1 本。上限を超えたら後半だけ残す
"""
from __future__ import annotations

import contextlib
import json
import math
import os
import re
import shutil
import time
from pathlib import Path
from typing import Iterator

LOCAL = Path(__file__).resolve().parents[1] / "relay_local"
LOG = LOCAL / "notify_hook.log"
LOG_MAX = 200_000
PHONE_CONFIG = LOCAL / "phone_notify.json"

LOCK_WAIT_SEC = 1.0
LOCK_STALE_SEC = 5.0
STATE_KEEP_SEC = 3600.0
FUTURE_KEEP_SEC = 60.0  # これ以上先の時刻は時計の巻き戻しの残骸として捨てる

SID_RE = re.compile(r"^[0-9A-Za-z_-]{1,64}$")


def log(msg: str) -> None:
    """1 本のログに追記する。書けなくても通知は止めない。"""
    try:
        LOCAL.mkdir(parents=True, exist_ok=True)
        if LOG.exists() and LOG.stat().st_size > LOG_MAX:
            LOG.write_bytes(LOG.read_bytes()[-LOG_MAX // 2 :])
        with LOG.open("a", encoding="utf-8") as f:
            f.write(f"{time.strftime('%m-%d %H:%M:%S')} {msg}\n")
    except OSError:
        pass


@contextlib.contextmanager
def file_lock(path: Path) -> Iterator[bool]:
    """O_EXCL のロックファイルによる排他。取れなくても例外にはせず held=False で続行する。"""
    lock = path.with_name(path.name + ".lock")
    deadline = time.monotonic() + LOCK_WAIT_SEC
    held = False
    while True:
        try:
            os.close(os.open(str(lock), os.O_CREAT | os.O_EXCL | os.O_WRONLY))
            held = True
            break
        except (FileExistsError, PermissionError):
            # Windows では、他プロセスが unlink した直後(削除保留中)の O_EXCL が PermissionError になる。
            # ロック不可と見なして排他なしで進むと更新が消えるので、存在していた場合と同じく取り直す
            try:
                if time.time() - lock.stat().st_mtime > LOCK_STALE_SEC:
                    lock.unlink()  # 落ちたプロセスの置き土産
                    continue
            except OSError:
                pass
            if time.monotonic() >= deadline:
                break
            time.sleep(0.02)
        except OSError:
            break
    try:
        yield held
    finally:
        if held:
            with contextlib.suppress(OSError):
                lock.unlink()


def _read_state(path: Path) -> dict[str, float]:
    """記録を読む。壊れている・型が違う値は捨てる (直すまで通知不能になる事故を防ぐ)。"""
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(raw, dict):
        return {}
    out: dict[str, float] = {}
    for k, v in raw.items():
        if isinstance(k, str) and isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v):
            out[k] = float(v)
    return out


def _write_state(path: Path, state: dict[str, float]) -> None:
    """一時ファイル → os.replace で置き換える (書き込み途中を他プロセスに読ませない)。"""
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    try:
        tmp.write_text(json.dumps(state), encoding="utf-8")
        os.replace(tmp, path)
    except OSError:
        with contextlib.suppress(OSError):
            tmp.unlink()


def claim(path: Path, key: str, window: float, now: float | None = None) -> tuple[float, float] | None:
    """key を window 秒の間に 1 回だけ許可する。

    許可: (前回の記録値, 今回の記録時刻) を返す。窓内: None。
    前回の記録値が未来(時計が巻き戻った)なら期限切れとして扱う。
    """
    with file_lock(path):
        # 時刻はロックを取ってから決める。待たされた側が先に決めた時刻だと、後から書かれた
        # 他スレッドの記録より過去になり、掃除で「未来の記録」として消してしまう
        at = time.time() if now is None else now
        state = _read_state(path)
        prev = state.get(key, 0.0)
        if 0 <= at - prev < window:
            return None
        state = {k: v for k, v in state.items() if -FUTURE_KEEP_SEC <= at - v < STATE_KEEP_SEC}
        state[key] = at
        _write_state(path, state)
        return prev, at


def fail_backoff(path: Path, key: str, claimed_at: float, window: float, retry_after: float) -> None:
    """送信に失敗したので、retry_after 秒後には再通知を通すよう窓を縮める。"""
    with file_lock(path):
        state = _read_state(path)
        if state.get(key) == claimed_at:
            state[key] = claimed_at - window + retry_after
            _write_state(path, state)


def phone_enabled(path: Path | None = None) -> bool:
    """スマホ通知が有効か。設定ファイルが在り、enabled が false でなければ有効。"""
    path = PHONE_CONFIG if path is None else path
    if not path.exists():
        return False
    try:
        cfg = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return True  # 壊れていても従来どおり有効 (止めたいなら enabled:false か削除)
    return not (isinstance(cfg, dict) and cfg.get("enabled") is False)


def powershell_exe() -> str:
    """PATH 検索に頼らず、標準の Windows PowerShell を絶対パスで指す。無ければ pwsh → 名前解決。"""
    root = os.environ.get("SystemRoot", r"C:\Windows")
    fixed = Path(root) / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe"
    if fixed.exists():
        return str(fixed)
    return shutil.which("pwsh") or shutil.which("powershell") or "powershell"
