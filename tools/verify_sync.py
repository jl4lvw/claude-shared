"""claude-shared の反映を **事後に** 検証する。

なぜ必要か(2026-08-05 の実事故):
  「push した」と報告したのに中身が古い、という事故が同日に2回起きた。
    1. relay_client.py の --peek 復活が push 前の版だったまま配布され、RC の機能が退行した
    2. robocopy のミラーが1ファイルも動いていないのに、エラーも出ず素通りした
  どちらも「実行した」で終わり「結果がそうなっている」を確かめていなかったのが原因。
  /g-cmp は三方比較を持っていたが **「/g-ul の前の確認用」** と位置づけられており、
  事後の検証がどこにも無かった。任意で事前の検証は、忙しいときに必ず飛ばされる。

そこで本ツールを **/g-ul の完了条件** として組み込む。次の3つを検証する:
  [1] .claude/{targets} == claude-shared/{targets}  (ミラーが実際に効いたか)
  [2] claude-shared の作業ツリーがクリーン          (commit し忘れが無いか)
  [3] ローカル HEAD == origin/<branch>              (push が実際に届いたか)

1つでも崩れていれば **非0で終了** する。「push したが反映されていない」を
黙って通さないことが唯一の目的。

    python verify_sync.py            # 検証(不一致なら exit 1)
    python verify_sync.py --quiet    # 一致時は何も出さない
"""

from __future__ import annotations

import argparse
import fnmatch
import hashlib
import os
import subprocess
import sys
import time
from pathlib import Path

# 警告は stderr に出す。**stderr も UTF-8 にしないと CP932 コンソールで化けて読めず、
# 「反映されていない」という肝心の指摘が伝わらない(実際に化けた)
sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

EXIT_OK = 0
EXIT_DRIFT = 1        # 反映されていない(検証失敗)
EXIT_ENV = 2          # 前提が揃っていない(パスが無い等)

# /g-ul のミラー対象と必ず一致させること。ずれると誤検知になる
TARGETS: tuple[str, ...] = ("skills", "commands", "tools", "rules", "memory", "hooks", "incidents")

# robocopy の除外指定と同じもの。合わせないと「毎回不一致」になって
# 警告が形骸化する(狼少年になった検証は無いのと同じ)
EXCLUDE_DIRS: tuple[str, ...] = ("__pycache__", ".bootstrap-bak-*", ".migrate-pending-*")
EXCLUDE_FILES: tuple[str, ...] = (
    "*.bak_*",
    "*.pyc",
    "*.migrated",
    ".deepseek_usage_session.json",
    ".qwen_usage_session.json",
    ".gemini_usage_session.json",
    # 端末ローカルの自動計測 (量が多く共有しない)。incidents.jsonl だけを共有する
    "telemetry.jsonl",
    # 端末ごとの使用量 DB。//MIR に載せると /g-dl していない端末が
    # 他端末の記録を消すため robocopy 側で除外している (2026-08-12)。
    # **こちらへ追随させ忘れていて、/g-ul が構造的に exit 0 へ到達できなくなっていた。**
    # 上のコメントが警告しているとおりの形骸化が実際に起きたので、
    # test_verify_sync_excludes.py で SKILL.md の //XF と機械照合するようにした。
    # 端末別ファイル名だけを除外する。`cgd_usage*` と広げると移行前の旧
    # `cgd_usage.sqlite3` まで除外され、共有側にしか旧DBが無い端末が履歴を
    # 受け取れなくなる (2026-08-12 cgd Lv7・codex_high 指摘)。
    "pv_usage_*.sqlite3",
    "cgd_usage_*.sqlite3",
    # 移行途中の一時ファイル。残骸が出たときにミラーへ載せない。
    "*.migrating.*",
)


def _excluded(rel: Path) -> bool:
    """ミラー対象外か判定する(robocopy の //XD //XF と同じ意味)。"""
    for part in rel.parts[:-1]:
        if any(fnmatch.fnmatch(part, pat) for pat in EXCLUDE_DIRS):
            return True
    return any(fnmatch.fnmatch(rel.name, pat) for pat in EXCLUDE_FILES)


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _snapshot(root: Path) -> dict[str, str]:
    """対象ツリーの {相対パス: sha256} を作る。"""
    out: dict[str, str] = {}
    if not root.exists():
        return out
    for p in root.rglob("*"):
        if not p.is_file():
            continue
        rel = p.relative_to(root)
        if _excluded(rel):
            continue
        try:
            out[rel.as_posix()] = _digest(p)
        except OSError as exc:
            # 読めないファイルを「一致」に倒すと検証の意味が消えるので差分扱い
            out[rel.as_posix()] = f"<読み取り失敗: {type(exc).__name__}>"
    return out


def _git(shared: Path, *args: str) -> tuple[int, str]:
    proc = subprocess.run(
        ["git", "-C", str(shared), *args],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    return proc.returncode, (proc.stdout or "").strip()


# 「並行セッションが編集中」と見なす更新の新しさ (秒)。
# /g-ul は「ミラー → commit → push → verify」で 1 分前後かかる。
# その間に別セッションが .claude を書けば、こちらのミラーには載らず差分として残る。
FRESH_EDIT_SEC = 600


def _fresh_hint(path: Path) -> str:
    """直近に更新されたファイルなら、その事実を添える。

    並行セッション下では **/g-ul は原理的に収束しない**
    (相手が .claude を書き続ける限り、こちらのミラーは常に古い)。
    それ自体は直せないが、「自分の push が失敗した」のか
    「他人が編集中なだけ」なのかは区別できる。
    2026-08-12 に実際に、別セッションが編集中のファイル 1 本のせいで
    exit 1 になり、自分の反映が失敗したのかを確かめ直す羽目になった
    (INC-20260812-111904c95948)。
    """
    try:
        age = time.time() - path.stat().st_mtime
    except OSError:
        return ""
    if age < 0 or age > FRESH_EDIT_SEC:
        return ""
    return f" ({int(age // 60)} 分前に更新 — 並行セッションが編集中の可能性)"


def check_mirror(claude_dir: Path, shared: Path) -> list[str]:
    """[1] .claude と claude-shared のツリー一致を検証する。"""
    problems: list[str] = []
    for target in TARGETS:
        src, dst = claude_dir / target, shared / target
        if not src.exists() and not dst.exists():
            continue
        if not src.exists():
            problems.append(f"{target}: .claude 側に無い(claude-shared 側のみ存在)")
            continue
        if not dst.exists():
            problems.append(f"{target}: claude-shared 側に無い(ミラー未実行の疑い)")
            continue
        a, b = _snapshot(src), _snapshot(dst)
        only_src = sorted(set(a) - set(b))
        only_dst = sorted(set(b) - set(a))
        differ = sorted(k for k in set(a) & set(b) if a[k] != b[k])
        for rel in only_src[:5]:
            problems.append(
                f"{target}/{rel}: 未反映(claude-shared に無い){_fresh_hint(src / rel)}")
        for rel in only_dst[:5]:
            problems.append(f"{target}/{rel}: 削除漏れ(.claude に無い)")
        for rel in differ[:5]:
            problems.append(f"{target}/{rel}: 内容が違う{_fresh_hint(src / rel)}")
        extra = len(only_src) + len(only_dst) + len(differ) - min(
            5, len(only_src)
        ) - min(5, len(only_dst)) - min(5, len(differ))
        if extra > 0:
            problems.append(f"{target}: ほか {extra} 件の差分")
    return problems


def check_git(shared: Path) -> list[str]:
    """[2] 作業ツリーのクリーンさと [3] origin への到達を検証する。"""
    problems: list[str] = []
    code, status = _git(shared, "status", "--porcelain")
    if code != 0:
        return [f"git status に失敗しました(リポジトリではない?): {shared}"]
    if status:
        head = "\n".join(f"      {line}" for line in status.splitlines()[:5])
        problems.append("commit されていない変更が残っています:\n" + head)

    code, branch = _git(shared, "branch", "--show-current")
    if code != 0 or not branch:
        problems.append("現在のブランチを取得できません")
        return problems

    # fetch しないと origin の情報が古く、push 済みか判定できない
    fcode, _ = _git(shared, "fetch", "--quiet")
    if fcode != 0:
        problems.append("git fetch に失敗しました(オフライン? 認証?)。push 到達を確認できません")
        return problems

    code, local = _git(shared, "rev-parse", "HEAD")
    rcode, remote = _git(shared, "rev-parse", f"origin/{branch}")
    if code != 0 or rcode != 0:
        problems.append(f"HEAD / origin/{branch} を解決できません")
        return problems
    if local != remote:
        acode, ahead = _git(shared, "rev-list", "--count", f"origin/{branch}..HEAD")
        bcode, behind = _git(shared, "rev-list", "--count", f"HEAD..origin/{branch}")
        detail = []
        if acode == 0 and ahead not in ("", "0"):
            detail.append(f"未 push {ahead} 件")
        if bcode == 0 and behind not in ("", "0"):
            detail.append(f"未取込 {behind} 件")
        problems.append(
            f"origin/{branch} と一致していません"
            + (f" ({' / '.join(detail)})" if detail else "")
        )
    return problems


def main() -> int:
    parser = argparse.ArgumentParser(description="claude-shared への反映を事後検証する")
    parser.add_argument(
        "--claude-dir", default=r"C:\ClaudeCode\.claude", help=".claude のパス"
    )
    parser.add_argument("--shared", default=None, help="claude-shared のパス")
    parser.add_argument("--quiet", action="store_true", help="一致時は何も出さない")
    args = parser.parse_args()

    claude_dir = Path(args.claude_dir)
    shared = Path(
        args.shared
        or os.environ.get("CLAUDE_SHARED_DIR")
        or (Path(os.environ.get("USERPROFILE", Path.home())) / "claude-shared")
    )
    if not claude_dir.is_dir():
        print(f"ERROR: .claude が見つかりません: {claude_dir}", file=sys.stderr)
        return EXIT_ENV
    if not shared.is_dir():
        print(f"ERROR: claude-shared が見つかりません: {shared}", file=sys.stderr)
        return EXIT_ENV

    problems = check_mirror(claude_dir, shared) + check_git(shared)

    if not problems:
        if not args.quiet:
            print(f"[verify_sync] OK: .claude == claude-shared == origin ({shared})")
        return EXIT_OK

    print("[verify_sync] 反映されていません。以下を解消してください:", file=sys.stderr)
    for p in problems:
        print(f"  - {p}", file=sys.stderr)
    print(
        "\n  ミラー差分なら /g-ul をやり直す。commit 漏れなら commit してから push。\n"
        "  未取込があるなら先に /g-dl で取り込むこと。",
        file=sys.stderr,
    )
    return EXIT_DRIFT


if __name__ == "__main__":
    raise SystemExit(main())
