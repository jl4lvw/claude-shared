"""cgd doctor — 環境と認証の一括チェック.

cgd / codex スキル起動前に「使えるレベル」を 1 コマンドで確認するためのツール。
Antigravity の doctor コマンドに相当する位置づけ（Zenn 記事から導入）。

使い方:
    python C:/ClaudeCode/.claude/tools/cgd_doctor.py          # オフライン環境チェックのみ
    python C:/ClaudeCode/.claude/tools/cgd_doctor.py --probe  # 各 API に最小プロンプトで疎通テスト

チェック項目（オフライン・既定）:
    - Bash 環境（Git Bash / WSL）
    - openai Python ライブラリ
    - codex CLI 存在 + login status
    - 環境変数 (GEMINI / DEEPSEEK / DASHSCOPE / QWEN_BASE_URL)
    - advisor スクリプト存在
    - C:/tmp-ai 書込権限
    - SKILL.md の SKILL_VERSION スタンプ
    - Lv6-WF / Lv7-WF workflow スクリプト存在

--probe 追加（実費発生・既定オフ）:
    - 各 API に 16 token 程度の最小プロンプトを送って exit code を確認

終了コード（advisor スクリプトと同じ規約）:
    0  = OK
    1  = 1件以上の ❌（実行に支障あり）
"""

from __future__ import annotations

import argparse
import os
import platform
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

# === cgd exit code registry (keep in sync with gemini/deepseek/qwen_advisor.py) ===
EXIT_OK = 0
EXIT_GENERIC = 1
EXIT_AUTH = 10
EXIT_QUOTA = 20
EXIT_TIMEOUT = 30
EXIT_NETWORK = 40
EXIT_INVALID_INPUT = 50

SKILL_PATH = Path("C:/ClaudeCode/.claude/skills/cgd/SKILL.md")
TOOLS_DIR = Path("C:/ClaudeCode/.claude/tools")
WORKFLOWS_DIR = Path("C:/ClaudeCode/.claude/skills/cgd/workflows")
TMP_AI_DIR = Path("C:/tmp-ai")

OK = "OK "
NG = "NG "
WARN = "WARN"

Result = tuple[str, str, str]


def check_shell() -> Result:
    bash = shutil.which("bash")
    if bash:
        return (OK, "shell", f"bash 利用可 ({bash})")
    return (NG, "shell", "bash 不在（cgd は bash 必須・Git Bash or WSL を入れてください）")


def check_openai_lib() -> Result:
    try:
        import openai  # noqa: F401
        return (OK, "openai (python)", "import OK")
    except ImportError as exc:
        return (NG, "openai (python)", f"未インストール: {exc}")


def check_codex_cli() -> Result:
    codex = shutil.which("codex")
    if not codex:
        return (NG, "codex CLI", "未インストール (npm i -g @openai/codex)")
    return (OK, "codex CLI", f"存在 ({codex})")


def check_codex_login() -> Result:
    codex = shutil.which("codex")
    if not codex:
        return (NG, "codex login", "codex CLI 不在のため判定不可")
    # Windows では codex が .CMD ラッパーのため shell=True 経由で起動する必要がある
    use_shell = os.name == "nt"
    cmd: str | list[str] = "codex login status" if use_shell else ["codex", "login", "status"]
    try:
        r = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=15,
            shell=use_shell,
        )
    except subprocess.TimeoutExpired:
        return (WARN, "codex login", "確認タイムアウト（15s）")
    except OSError as exc:
        return (WARN, "codex login", f"確認失敗: {exc}")
    out = (r.stdout + r.stderr).strip()
    if "Logged in" in out or "logged in" in out:
        return (OK, "codex login", "Logged in using ChatGPT")
    return (NG, "codex login", f"未ログイン: {out[:120]}")


def check_env_var(name: str, required_for: str) -> Result:
    if os.environ.get(name):
        return (OK, name, f"設定済（{required_for}）")
    return (WARN, name, f"未設定（{required_for} で必要）")


def check_script(name: str) -> Result:
    p = TOOLS_DIR / name
    if p.exists():
        return (OK, f"tools/{name}", "存在")
    return (NG, f"tools/{name}", "不在")


def check_tmp_writable() -> Result:
    try:
        TMP_AI_DIR.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            dir=TMP_AI_DIR, delete=True, prefix=".doctor_"
        ) as tmp:
            tmp.write(b"ok")
        return (OK, "C:/tmp-ai", "書込権限あり")
    except OSError as exc:
        return (NG, "C:/tmp-ai", f"書込不可: {exc}")


def check_skill_version() -> Result:
    if not SKILL_PATH.exists():
        return (NG, "SKILL_VERSION", "SKILL.md 不在")
    try:
        text = SKILL_PATH.read_text(encoding="utf-8")
    except OSError as exc:
        return (NG, "SKILL_VERSION", f"読込失敗: {exc}")
    m = re.search(r"SKILL_VERSION:\s*([0-9_\-]+)", text)
    if m:
        return (OK, "SKILL_VERSION", m.group(1))
    return (WARN, "SKILL_VERSION", "スタンプ無し（更新忘れの可能性）")


def check_workflows() -> list[Result]:
    out: list[Result] = []
    for name in ("cgd_lv6_review.js", "cgd_lv7_review.js"):
        p = WORKFLOWS_DIR / name
        if p.exists():
            out.append((OK, f"workflows/{name}", "存在"))
        else:
            out.append((WARN, f"workflows/{name}", "不在 (Lv6-WF / Lv7-WF 使用不可)"))
    return out


def check_probe(script: str, env_var: str, role: str = "reviewer") -> Result:
    """各 advisor に最小プロンプトを送って exit code を確認.

    role: "reviewer" (Lv2-7 経路) または "coder" (Lv0 経路)。
    Gemini には coder ロールがないため呼出側で除外する。
    """
    label_key = f"probe({role}):{script}"
    if not os.environ.get(env_var):
        return (WARN, label_key, f"{env_var} 未設定のためスキップ")
    p = TOOLS_DIR / script
    if not p.exists():
        return (NG, label_key, "スクリプト不在")
    try:
        r = subprocess.run(
            [
                sys.executable,
                str(p),
                "--role",
                role,
                "--max-tokens",
                "16",
                "--no-usage",
                "ping",
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=45,
        )
    except subprocess.TimeoutExpired:
        return (NG, label_key, "タイムアウト (45s)")
    except OSError as exc:
        return (NG, label_key, f"起動失敗: {exc}")

    if r.returncode == EXIT_OK:
        return (OK, label_key, "疎通 OK")

    label_map = {
        EXIT_AUTH: "認証エラー",
        EXIT_QUOTA: "quota/rate-limit",
        EXIT_TIMEOUT: "timeout",
        EXIT_NETWORK: "network error",
        EXIT_INVALID_INPUT: "invalid input",
    }
    label = label_map.get(r.returncode, f"exit {r.returncode}")
    detail = (r.stderr or r.stdout).strip().splitlines()
    tail = detail[-1] if detail else ""
    return (NG, label_key, f"{label}: {tail[:120]}")


def judge_level(results: list[Result]) -> str:
    by_label = {label: status for status, label, _ in results}
    codex_ok = by_label.get("codex login") == OK
    gem_ok = by_label.get("GEMINI_API_KEY") == OK
    ds_ok = by_label.get("DEEPSEEK_API_KEY") == OK
    qw_ok = by_label.get("DASHSCOPE_API_KEY") == OK

    levels: list[str] = []
    # Lv0 = 委譲レーン。DS / Qwen のどちらか有れば実行可（Codex は Step 2-0D の任意レビューに使う）
    if ds_ok and qw_ok:
        levels.append("Lv0")
    elif ds_ok or qw_ok:
        levels.append("Lv0(片側のみ)")
    # Lv1-8 = レビューレーン。Codex 必須。
    # 2026-07: Gemini は API エラー多発のため既定オフのオプトイン参加に格下げ済み。
    # 判定条件から gem_ok を外した（Gemini 未設定でも Lv2-8 は実行可能）。
    # Lv8（2026-07新設）は Lv4-7 と同じ認証要件（Codex+DeepSeek+DASHSCOPE）のため同じ括りに入れる。
    if codex_ok:
        levels.append("Lv1")
        if ds_ok:
            levels.append("Lv2-3")
        if ds_ok and qw_ok:
            levels.append("Lv4-8")

    if not levels:
        return "[判定] 利用可能なレベル無し（codex / DeepSeek / Qwen のいずれも認証されていません）"
    gemini_note = "Gemini追加参加も可" if gem_ok else "Geminiはオプトイン機能・未設定のため追加参加不可（既定動作には影響なし）"
    return f"[判定] 実行可能レベル: {' / '.join(levels)}（{gemini_note}）"


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

    parser = argparse.ArgumentParser(
        description="cgd doctor — environment & auth check"
    )
    parser.add_argument(
        "--probe",
        action="store_true",
        help="各 API に最小プロンプトで疎通テスト (reviewer ロール・Lv2-7 経路・実費発生)",
    )
    parser.add_argument(
        "--probe-coder",
        action="store_true",
        help="DS/Qwen を coder ロールでも疎通テスト (Lv0 経路の検証・実費発生)",
    )
    args = parser.parse_args()

    print("=" * 60)
    print("cgd doctor — 環境チェック")
    print("=" * 60)
    print(f"OS:     {platform.platform()}")
    print(f"Python: {sys.version.split()[0]} ({sys.executable})")
    print()

    results: list[Result] = []
    results.append(check_shell())
    results.append(check_openai_lib())
    results.append(check_codex_cli())
    results.append(check_codex_login())
    results.append(check_env_var("GEMINI_API_KEY", "Geminiオプトイン時のみ・既定では不要"))
    results.append(check_env_var("DEEPSEEK_API_KEY", "Lv0/Lv2-7"))
    results.append(check_env_var("DASHSCOPE_API_KEY", "Lv0/Lv4-7"))
    results.append(check_env_var("QWEN_BASE_URL", "DashScope リージョン"))
    for name in ("gemini_advisor.py", "deepseek_coder.py", "qwen_advisor.py", "cgd_usage_log.py"):
        results.append(check_script(name))
    results.append(check_tmp_writable())
    results.append(check_skill_version())
    results.extend(check_workflows())

    if args.probe:
        print("--probe 指定: reviewer ロールで疎通テスト中 (Lv2-7 経路)...")
        results.append(check_probe("gemini_advisor.py", "GEMINI_API_KEY", "reviewer"))
        results.append(check_probe("deepseek_coder.py", "DEEPSEEK_API_KEY", "reviewer"))
        results.append(check_probe("qwen_advisor.py", "DASHSCOPE_API_KEY", "reviewer"))
        print()

    if args.probe_coder:
        print("--probe-coder 指定: coder ロールで疎通テスト中 (Lv0 経路・DS/Qwen のみ)...")
        results.append(check_probe("deepseek_coder.py", "DEEPSEEK_API_KEY", "coder"))
        results.append(check_probe("qwen_advisor.py", "DASHSCOPE_API_KEY", "coder"))
        print()

    print(f"{'状態':<5} {'項目':<28} 詳細")
    print("-" * 60)
    for status, label, detail in results:
        print(f"{status:<5} {label:<28} {detail}")

    print()
    print(judge_level(results))

    has_ng = any(s == NG for s, _, _ in results)
    sys.exit(EXIT_GENERIC if has_ng else EXIT_OK)


if __name__ == "__main__":
    main()
