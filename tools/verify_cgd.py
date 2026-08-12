"""verify_cgd — cgd 関連の変更を入れたら必ず通す検証セット.

なぜ必要か (2026-08-11/12 の実害):
    preflight_inputs.py から `info["bytes"] = st.st_size` を落としたまま、
    JS 側のガードテストだけ回して「OK」と報告した。JS のテストは
    preflight_inputs をスタブするので Python 側の退行を一切見ない。
    結果、正常な入力が必ず bytes=0 と報告され、**Lv8 が全く起動できなく**なった。
    pytest には bytes を検証するテストが既にあり、**回していれば防げた**。

    「どれを回すか」を毎回判断させるとこの取りこぼしが起きるので、
    まとめて 1 コマンドにする。

含まれる検証 (main() の実行順):
    1. WF スクリプトの構文        — check_wf_syntax.mjs
       (node --check は WF スクリプトに対して壊れたファイルでも exit 0 を返すので使わない)
    2. WF 入力ガードの単体テスト  — wf_guard_test.mjs (本体を評価して halt を検証)
    3. Python ツールの pytest     — tests/ 配下すべて (ファイル固定にしない)
       ここに **cgd_plan と WF の契約テスト** が含まれる。cgd_plan が予測する
       生ログのパスと WF が指示する保存先がずれると、成功した run でも
       「生ログが無い」と誤報し、警告が無視されるようになる
    4. 改行コード                 — ensure_lf.py --check --preset cgd-wf
    5. Python モジュールの import — 実際に import して NameError を検出
    6. preflight の出力契約       — WF の判定条件 (exists/is_file/readable/bytes>0) を当てる
    7. cgd run の未検証           — collect 未実施の run が残っていないか
    8. ゲートの残留               — armed なゲートが残っていないか

方針:
    **fail-fast しない。** 最初の失敗で止めると「他も壊れているかどうか」が
    分からず、直しては走らせを繰り返すことになる。全項目を実行してから集計する。

使い方:
    python C:/ClaudeCode/.claude/tools/verify_cgd.py

終了コード: 0 = 全部 OK / 1 = どれか失敗
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:  # pragma: no cover
    pass

TOOLS = Path(__file__).resolve().parent
HOOKS = TOOLS.parent / "hooks"

Step = tuple[str, list[str]]

STEPS: list[Step] = [
    ("WF スクリプトの構文", ["node", str(TOOLS / "check_wf_syntax.mjs")]),
    ("WF 入力ガードの単体テスト", ["node", str(TOOLS / "wf_guard_test.mjs")]),
    # ファイル指定にすると、新しく足したテストが黙って回らなくなる。
    # ディレクトリごと対象にして拾い漏らしを作らない。
    ("Python ツールの pytest", [sys.executable, "-m", "pytest",
                                str(TOOLS / "tests"), "-q"]),
    ("改行コード (CR 混入)", [sys.executable, str(TOOLS / "ensure_lf.py"),
                              "--check", "--preset", "cgd-wf"]),
]


IMPORTS = ["ensure_lf", "preflight_inputs", "cgd_doctor"]


def run(title: str, cmd: list[str]) -> bool:
    proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    ok = proc.returncode == 0
    print(f"[{'OK ' if ok else 'NG '}] {title}")
    if not ok:
        for stream in (proc.stdout, proc.stderr):
            for line in (stream or "").strip().splitlines()[-15:]:
                print(f"       {line}")
    return ok


def check_imports() -> bool:
    """実際に import する。ast.parse では NameError / ImportError を検出できない。"""
    code = (
        "import sys;"
        f"sys.path.insert(0, r'{TOOLS}');"
        f"sys.path.insert(0, r'{HOOKS}');"
        "import cgd_wf_gate," + ",".join(IMPORTS) + ";"
        "print('ok')"
    )
    return run("Python モジュールの import", [sys.executable, "-c", code])


def check_gate_clean() -> bool:
    """ゲートが残っていないか。**判定は構造化フィールドから読む**。

    文字列 '"armed": true' の有無だけで見ていた版は、コマンドが失敗して
    出力が空でも「OK」と報告した。検査の失敗を合格と読み替えるのは
    このセッションで何度も踏んだ型の事故なので、明示的に NG へ倒す。
    """
    proc = subprocess.run(
        [sys.executable, str(HOOKS / "cgd_wf_gate.py"), "status", "--json"],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    if proc.returncode != 0:
        print("[NG ] ゲートの残留 — status が異常終了 (判定不能)")
        print(f"       exit={proc.returncode} {(proc.stderr or '').strip()[:200]}")
        return False
    try:
        payload = json.loads(proc.stdout or "")
    except (json.JSONDecodeError, ValueError) as exc:
        print(f"[NG ] ゲートの残留 — status の出力を JSON として読めない ({exc})")
        print(f"       {(proc.stdout or '').strip()[:200]}")
        return False
    armed = payload.get("armed")
    if not isinstance(armed, bool):
        print(f"[NG ] ゲートの残留 — armed が boolean でない ({armed!r})")
        return False

    print(f"[{'NG ' if armed else 'OK '}] ゲートの残留")
    if armed:
        print(f"       {(proc.stdout or '').strip()}")
        print("       → python .claude/hooks/cgd_wf_gate.py disarm --all")
    return not armed


def check_preflight_contract() -> bool:
    """**WF が実際に読む形**で preflight_inputs の出力を検証する。

    ここが無いと「単体テストは全部通るのに Lv8 は起動できない」が起き得る。
    実際に 2026-08-12、bytes の代入が消えて Lv8 が全く動かなくなったとき、
    JS 側のテストはスタブを見ていたので気づけなかった。
    WF 側の判定条件 (exists / is_file / readable / bytes>0) をそのまま当てる。
    """
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        sample = Path(tmp) / "sample.txt"
        sample.write_bytes("レビュー入力サンプル\n".encode("utf-8"))
        proc = subprocess.run(
            [sys.executable, str(TOOLS / "preflight_inputs.py"), str(sample)],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
        )
        try:
            files = json.loads(proc.stdout or "")["files"]
            f = files[0]
        except (json.JSONDecodeError, ValueError, KeyError, IndexError) as exc:
            print(f"[NG ] preflight の出力契約 — JSON として読めない ({exc})")
            print(f"       {(proc.stdout or '').strip()[:200]}")
            return False

        problems = []
        if f.get("exists") is not True:
            problems.append("exists が true でない")
        if f.get("is_file") is not True:
            problems.append("is_file が true でない")
        if f.get("readable") is not True:
            problems.append("readable が true でない")
        if not isinstance(f.get("bytes"), int) or f["bytes"] <= 0:
            problems.append(f"bytes が正の整数でない ({f.get('bytes')!r})")
        if f.get("error"):
            problems.append(f"error が載っている ({f['error']})")

    print(f"[{'NG ' if problems else 'OK '}] preflight の出力契約 (WF の判定条件で検証)")
    for p in problems:
        print(f"       {p}")
    if problems:
        print("       → 正常な入力が WF 側で input_missing になります (Lv6/7/8 が起動不能)")
    return not problems


def check_no_pending_runs() -> bool:
    """collect が済んでいない cgd の run が残っていないか。

    レビュアーの成否は agent の自己申告なので、生ログの実在を Python が判定する
    `cgd_plan.py collect` が唯一の非 LLM ゲートになる。未検証のまま結果を採用して
    いないかをここでも見る（リマインダー hook と二重の網）。
    """
    proc = subprocess.run(
        [sys.executable, str(TOOLS / "cgd_plan.py"), "list"],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    if proc.returncode != 0:
        print("[NG ] cgd run の未検証 — list が異常終了 (判定不能)")
        print(f"       exit={proc.returncode} {(proc.stderr or '').strip()[:200]}")
        return False
    pending = "未検証の run が" in (proc.stdout or "")
    print(f"[{'NG ' if pending else 'OK '}] cgd run の未検証")
    if pending:
        for line in (proc.stdout or "").strip().splitlines():
            print(f"       {line}")
    return not pending


def main() -> int:
    print("=== cgd 検証セット ===")
    results = [run(title, cmd) for title, cmd in STEPS]
    results.append(check_imports())
    results.append(check_preflight_contract())
    results.append(check_no_pending_runs())
    results.append(check_gate_clean())

    failed = results.count(False)
    print()
    print("すべて OK" if failed == 0 else f"{failed} 件 NG")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
