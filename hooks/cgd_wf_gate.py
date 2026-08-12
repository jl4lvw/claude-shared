"""cgd Lv6/Lv7/Lv8 の Workflow 実行を機械的に強制するゲート (PreToolUse hook + CLI).

背景 (2026-08-05):
  cgd の Lv6/Lv7/Lv8 はレビュー段を Workflow に委譲すると主 context への生出力流入を
  94% 削減できる。しかし SKILL.md の導線が inline に誘導していたため、同日に独立した
  2 セッションが揃って inline で実行した。「本文に書いてあるが読まれない」型の失敗
  なので、文章の修正だけでは再発する。本 hook が inline の codex 起動を遮断する。

初版 (同日) の欠陥と、それを受けた再設計:
  初版は「`codex exec` を正規表現で拾い、コマンドに `CGD_WF_RUN=1` が含まれていたら通す」
  という素朴な作りで、Lv8 セルフレビューで以下が実証された:
    1. `bash -c "codex exec ..."` / `env X=1 codex exec` / `$(codex exec)` / `time` / `eval`
       の 5 形式が素通りした (6 形式中 5 つ)。「強制」と呼べる代物ではなかった
    2. バイパス判定が部分文字列一致だったため、手順書を書くだけのコマンドに
       `CGD_WF_RUN=1` の文字が入っているだけでゲートが解除された
    3. 通過を検知した時点で自動 disarm していたため、WF の 1 本目が走った瞬間に
       以後の inline が無防備になった (WF が halt しても解除されたまま)
    4. ゲートが単一グローバルファイルだったため、並行セッションが互いの arm/disarm を
       破壊した (A の Lv6 を B の Lv8 が上書きし、B の disarm で A の分も消える)

  再設計の第 1 版はシェル構文を正規表現で解析する方向だったが、Codex 再レビューを
  3 周しても新しい抜け道 (`sudo` → 単一引用符 → `!` / `{ }`) が出続けたため断念した。
  「正規表現ゲートは迂回と誤検知を構造的に同時に抱える」という Lv8 批評の指摘どおり。

  現在の設計:
    - **シェルを解析しない**: ゲート中は `codex` という語を含む Bash を **一律遮断**する。
      引用符・heredoc・ラッパー・シェル構文を一切見ないので、この種の抜け道が原理的に無い。
      代償としてゲート中は `grep "codex exec"` や `codex login status` も止まるが、
      止まるのはゲートが張られている間だけで、deny メッセージが逃げ道を案内する
    - **nonce 照合**: arm 時にランダム nonce を発行し、`CGD_WF_RUN=<nonce> ... codex ...` の
      形（その codex 起動への環境変数代入）でのみバイパスを認める。
      文字列の混入や `echo` での先出しでは通らない
    - **自動 disarm の廃止**: 通過しても解除しない。解除は明示 `disarm` のみ (+ TTL 90 分)
    - **セッション単位**: `--session <SID>` を付けて arm すれば自セッションだけを止める。
      省略時は全体ゲートになり全セッションを止める。**claim(所有権の移動)はしない** —
      レースと「claim 後に disarm できないデッドロック」の原因だったため
    - **fail-closed の限定導入**: ゲートファイルが「存在するが壊れている」場合は遮断側に倒す
      (握り潰すと強制が形骸化するため)。ファイルが無い場合と hook 自体の例外は通す

使い方 (CLI):
    python cgd_wf_gate.py arm --level 7 [--session <SID>] [--ttl-min 90]
    python cgd_wf_gate.py disarm [--session <SID>]
    python cgd_wf_gate.py status [--session <SID>]
    python cgd_wf_gate.py nonce  [--session <SID>]   # WF に渡す nonce を取り出す

hook として: stdin に PreToolUse の JSON を渡す (サブコマンドなしで起動)。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import secrets
import sys
import traceback
from datetime import datetime, timedelta
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
    # stdin も UTF-8 に寄せる（多重防御）。本命は _run_hook() が buffer から
    # バイト列で読んで明示復号すること — ここが失敗しても素通りしない作りにしてある。
    sys.stdin.reconfigure(encoding="utf-8", errors="replace")
except Exception as _exc:  # pragma: no cover
    # 黙って諦めると「なぜゲートが効かないのか」を追えない。
    print(f"[cgd wf-gate] WARN: 標準入出力の UTF-8 化に失敗: {_exc}", file=sys.stderr)

GATE_DIR: Path = Path(os.environ.get("CGD_WF_GATE_DIR", r"C:/tmp-ai/.cgd_wf_gates"))
GLOBAL_KEY = "_unclaimed"

BYPASS_PREFIX = "CGD_WF_RUN="

WF_REQUIRED_LEVELS = (6, 7, 8)
DEFAULT_TTL_MIN = 90

# 絶対パス直書きだと別クローンで案内が嘘になる。このファイルからの相対で解決し、
# 見つからないときだけ従来の固定パスに落とす (ensure_lf の PRESETS と同じ方針)。
def _workflows_dir() -> Path:
    here = Path(__file__).resolve().parent          # .../.claude/hooks
    for cand in (here.parent / "skills" / "cgd" / "workflows",
                 Path("C:/ClaudeCode/.claude/skills/cgd/workflows")):
        if cand.is_dir():
            return cand
    return here.parent / "skills" / "cgd" / "workflows"


_WORKFLOW_SCRIPT = {
    n: (_workflows_dir() / f"cgd_lv{n}_review.js").as_posix() for n in (6, 7, 8)
}

_TS_FMT = "%Y-%m-%d %H:%M:%S"

# --------------------------------------------------------------------------
# コマンド判定 — **シェルを解析しない**
# --------------------------------------------------------------------------
# 2026-08-05: 正規表現でシェル構文を判定する方式を廃止した。
#   `bash -c "..."` → `sudo`/`if`/`while` → 単一引用符 → `!`/`{ }` と、塞ぐたびに
#   Codex 再レビューが新しい構文を見つけ、3 周しても終わらなかった。
#   「迂回と誤検知を構造的に同時に抱える」という Lv8 批評の指摘どおりだったので、
#   **ゲート中は `codex` という語を含む Bash を一律遮断し、束縛された nonce だけ通す**
#   方式に切り替えた。引用符もラッパーもシェル構文も解析しないので、この種の
#   抜け道が原理的に生じない。
#
#   代償: ゲート中は `grep "codex exec"` や `codex login status` も止まる。
#   ただし止まるのはゲートが張られている間だけで、deny メッセージが
#   nonce と disarm の両方を案内する。**見逃すより過剰に止める方を選ぶ。**
# 2026-08-12: 否定後読みから `.` と `/` を外した。
#   旧: (?<![\w./-])codex(?![\w-])
#   これだと `./codex` `/usr/bin/codex` `C:/tools/codex.exe` `~/bin/codex` が
#   **すべて素通り**していた（pv Lv3 の反証担当が指摘し、実測で確認）。
#   除外していたのは誤検知抑制のつもりだったが、除外した文字は
#   「パス付きで起動する形」そのもので、遮断力だけを削っていた。
#   語の内部（`mycodex` / `codex_input.txt` / `cgd_codex_20260812.txt`）は
#   前後が \w なので引き続き一致しない。
_CODEX_TOKEN_RE = re.compile(r"(?<![\w-])codex(?![\w-])", re.IGNORECASE)

# nonce は「その codex 起動の環境変数代入」でなければ意味がない。
# `echo CGD_WF_RUN=<nonce>; codex exec ...` で通るのを防ぐ。
_BOUND_NONCE_RE = re.compile(
    r"(?:^|[\s;|&(])(?:env\s+)?" + BYPASS_PREFIX + r"([A-Za-z0-9_-]+)"
    r"(?:\s+\w+=[^\s]*)*\s+codex(?![\w-])"
)

# PowerShell では `VAR=値 コマンド` の形が使えず、`$env:VAR = "値"; コマンド` になる。
# 2026-08-12 に matcher を Bash|PowerShell へ広げた結果、PowerShell 経由の
# **正当な WF 実行まで nonce を抽出できずに遮断される**ようになったため追加した。
# Bash 版と同じく「代入が同じコマンド内にあり codex より前」という束縛を要求する。
_BOUND_NONCE_PS_RE = re.compile(
    r"\$env:" + BYPASS_PREFIX.rstrip("=") + r"\s*=\s*['\"]?([A-Za-z0-9_-]+)['\"]?\s*;"
    r"[^;]*?\bcodex(?![\w-])",
    re.IGNORECASE,
)


def mentions_codex(command: str) -> bool:
    """コマンドが codex に言及しているか。**起動かどうかは判定しない**（安全側）。"""
    return bool(_CODEX_TOKEN_RE.search(command))


def extract_bypass_nonce(command: str) -> str | None:
    """nonce を「その codex 起動に束縛された代入」からのみ取り出す。

    認める形は 2 つだけ:
      Bash       : `CGD_WF_RUN=<nonce> [他の代入...] codex ...`
      PowerShell : `$env:CGD_WF_RUN = "<nonce>"; ... codex ...`

    どちらも「代入が同じコマンド内にあり codex より前」であることを要求する。
    `echo CGD_WF_RUN=xxx; codex ...` のような先出しでは通らない。
    """
    for pattern in (_BOUND_NONCE_RE, _BOUND_NONCE_PS_RE):
        m = pattern.search(command)
        if m:
            return m.group(1)
    return None


# --------------------------------------------------------------------------
# ゲート状態 (セッション単位)
# --------------------------------------------------------------------------
def _gate_path(session: str | None) -> Path:
    key = "".join(c for c in (session or GLOBAL_KEY) if c.isalnum() or c in "-_")[:64] or GLOBAL_KEY
    return GATE_DIR / f"{key}.json"


class GateWriteError(RuntimeError):
    """ゲートの書込に失敗した。**対象外レベル(None)と区別する**ため専用の例外にする。

    どちらも None を返していたため、CLI が書込失敗を「Lv? はゲート対象外」という
    無害なメッセージ + exit 0 で報告していた（2026-08-11 の 6 観点監査 windows 観点）。
    ゲートが張られていないのに張れたつもりで先へ進むのが最悪の挙動なので分離する。
    """


def _write_gate(path: Path, payload: dict) -> bool:
    tmp = path.with_name(f"{path.stem}.{os.getpid()}.{secrets.token_hex(3)}.tmp")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, path)
        return True
    except OSError as exc:
        print(f"[cgd wf-gate] WARN: ゲート書込に失敗: {exc}", file=sys.stderr)
        # 差し替え前に落ちると一時ファイルが GATE_DIR に溜まり続ける。
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        return False


def arm(level: int, session: str | None = None, ttl_min: int = DEFAULT_TTL_MIN) -> dict | None:
    """ゲートを張り、発行した内容(nonce 含む)を返す。

    対象外レベルなら None。**書込に失敗した場合は GateWriteError を投げる**
    (「張れていない」を成功と誤認させないため)。
    """
    if level not in WF_REQUIRED_LEVELS:
        return None
    # ttl_min が 0/負値だと「張った瞬間に失効したゲート」ができ、
    # 張ったつもりで無防備という最悪の状態になる。
    if ttl_min < 1:
        raise GateWriteError(f"ttl-min は 1 以上にしてください (指定: {ttl_min})")
    now = datetime.now()
    payload = {
        "level": level,
        "session": session or None,
        "nonce": secrets.token_hex(8),
        "armed_at": now.strftime(_TS_FMT),
        "expires_at": (now + timedelta(minutes=ttl_min)).strftime(_TS_FMT),
        "armed_pid": os.getpid(),
        "wf_used_at": None,
        "workflow_script": _WORKFLOW_SCRIPT[level],
    }
    if not _write_gate(_gate_path(session), payload):
        raise GateWriteError(
            f"ゲートファイルを書けませんでした: {_gate_path(session)}"
            " — ゲートは張られていません（codex は遮断されません）"
        )
    return payload


def disarm(session: str | None = None, all_sessions: bool = False) -> list[str]:
    """ゲートを解除し、解除したキー名の一覧を返す。

    自セッション用ゲートと、全体ゲート(_unclaimed)の両方が対象。
    全体ゲートは特定セッションの持ち物ではなく全員を止めるものなので、
    止められている側が解除できなければ筋が通らない（解除できずに TTL まで
    Step C が通らないデッドロックを実際に踏んだ）。

    all_sessions=True なら他セッションのゲートも含めて全部消す。
    既定で全消ししないのは、並行セッションのゲートを巻き込まないため。
    """
    targets = set(list_gate_paths()) if all_sessions else {_gate_path(session), _gate_path(None)}
    removed: list[str] = []
    for path in sorted(targets):
        try:
            if path.exists():
                path.unlink()
                removed.append(path.stem)
        except OSError as exc:
            print(f"[cgd wf-gate] WARN: ゲート解除に失敗 ({path.name}): {exc}", file=sys.stderr)
    return removed


def _load(path: Path) -> tuple[dict | None, bool]:
    """(gate, corrupt) を返す。corrupt=True は「在るのに読めない」= 遮断側に倒す合図。"""
    # Path.exists() は PermissionError を握り潰して False を返す。それだと
    # 「読めないゲート」が「無いゲート」と同じ扱いになり、遮断すべき場面で
    # fail-open する。os.stat を直接呼んで不在と読めないを区別する。
    try:
        os.stat(path)
    except FileNotFoundError:
        return None, False
    except (OSError, ValueError) as exc:
        print(f"[cgd wf-gate] WARN: ゲートを確認できません ({path.name}): {exc}", file=sys.stderr)
        return None, True

    try:
        raw = path.read_bytes()
        data = json.loads(raw.decode("utf-8", errors="replace"))
    except (OSError, json.JSONDecodeError, ValueError):
        return None, True
    if not isinstance(data, dict) or data.get("level") not in WF_REQUIRED_LEVELS:
        return None, True
    try:
        expires = datetime.strptime(str(data.get("expires_at", "")), _TS_FMT)
    except ValueError:
        return None, True
    if datetime.now() >= expires:
        # 期限切れの掃除。**読んだ内容と同じものが今もあるときだけ**消す。
        # 無条件に unlink すると、判定した直後に別プロセスが arm した
        # 有効なゲートを消してしまう (TOCTOU / 6観点監査 concurrency 観点)。
        try:
            if path.read_bytes() == raw:
                path.unlink(missing_ok=True)
        except OSError:
            pass
        return None, False
    return data, False


def list_gate_paths() -> list[Path]:
    """GATE_DIR にある全ゲートファイルのパス。存在しなければ空。"""
    try:
        return sorted(GATE_DIR.glob("*.json"))
    except OSError:
        return []


def list_all_gates() -> list[tuple[str, dict | None, bool]]:
    """全セッション分のゲートを (キー名, gate, corrupt) で返す。

    **`--session` を省略した CLI が「未設定」と嘘をつかないために必要**。
    ゲートはセッション単位のキーで保存されるため、_unclaimed だけを見ると
    自セッションのゲートが張られていても「未設定」と表示されてしまう。
    2026-08-11 にこれで「解除したつもりで残る」事故を踏んだ
    (INC-20260811-1440406a0dfc)。

    期限切れは _load() 側で削除されるので、戻り値には現れない。
    """
    out: list[tuple[str, dict | None, bool]] = []
    for path in list_gate_paths():
        gate, corrupt = _load(path)
        if gate is not None or corrupt:
            out.append((path.stem, gate, corrupt))
    return out


def read_gate(session: str | None = None) -> tuple[dict | None, bool]:
    """このセッションを遮断するゲートを返す。

    「自セッション用ゲート」または「セッション未指定で張られた全体ゲート」のどちらかが
    有効ならブロック対象。**claim（所有権の移動）はしない** — read→write→unlink の
    レースと、claim 後に disarm できなくなるデッドロックの原因だったため（Step C 指摘）。
    """
    own, corrupt = _load(_gate_path(session))
    if own is not None or corrupt:
        return own, corrupt
    if session is None:
        return None, False
    return _load(_gate_path(None))


# --------------------------------------------------------------------------
# hook 本体
# --------------------------------------------------------------------------
def _deny_reason(gate: dict) -> str:
    # 案内するコマンドは「そのまま打てば通る 1 行」にする。
    # --session の指定や nonce の手動取得を案内に含めない (2026-08-11):
    #   disarm は残り 1 件なら --session 不要、nonce は WF が自分で取得する。
    #   「準備の呪文が多くレビューまで遠い」は Lv8 批評の「高」指摘。
    level = gate["level"]
    script = gate.get("workflow_script", _WORKFLOW_SCRIPT.get(level, ""))
    used = gate.get("wf_used_at")
    head = (
        f"[cgd wf-gate] Lv{level} は Workflow 実行が必須です。codex に触れるコマンドを遮断しました。\n"
        f"（このゲートは {gate.get('armed_at')} に Lv{level} を選んだ時点で張られ、"
        f"{gate.get('expires_at')} に失効します）\n"
        "※ ゲート中は `grep \"codex ...\"` や `codex login status` など"
        "**起動でないコマンドもまとめて止まります**（シェルを解析せず安全側に倒すため）。\n"
    )
    if used:
        return (
            head
            + f"\n**Workflow は既に {used} に実行済みです。**レビュー段は終わっているので、\n"
            "次のコマンドでゲートを解除してから Step C の再レビューに進んでください:\n"
            '  python "C:/ClaudeCode/.claude/hooks/cgd_wf_gate.py" disarm\n'
        )
    return (
        head
        + "\n次に打つコマンド:\n"
        f'  Workflow({{ scriptPath: "{script}", args: {{ input_path, aux_input_path, label }} }})\n'
        "  ※ wf_nonce は WF が自分で取得するので渡す必要はありません。\n"
        "\n手順は cgd/SKILL.md「Workflow 経由実行 (Lv6-WF / Lv7-WF / Lv8-WF)」節。\n"
        "Workflow 完了後は disarm を実行してください（自動解除はしません）:\n"
        '  python "C:/ClaudeCode/.claude/hooks/cgd_wf_gate.py" disarm\n'
        "\nWorkflow が使えない事情がある場合のみ、正しい nonce を付けて意図的に迂回できます。\n"
        "迂回した理由は必ずユーザーに伝えてください。"
    )


def _corrupt_reason() -> str:
    return (
        "[cgd wf-gate] ゲートファイルが壊れており、Lv6/7/8 の実行中かどうか判定できません。\n"
        "強制が形骸化しないよう安全側に倒して codex を遮断しました。\n"
        "状態を確認して解除してください:\n"
        '  python "C:/ClaudeCode/.claude/hooks/cgd_wf_gate.py" status\n'
        '  python "C:/ClaudeCode/.claude/hooks/cgd_wf_gate.py" disarm'
    )


def _deny(reason: str) -> dict:
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": reason,
        }
    }


#: コマンド文字列を持つシェル系ツール。**Bash だけを見ていると素通りする**。
#  2026-08-11: この環境には PowerShell ツールもあり、matcher が "Bash" だけだったため
#  PowerShell 経由の codex 起動はゲートに一切かからなかった (6観点監査の security 観点)。
#  settings.local.json の matcher ("Bash|PowerShell") と対で維持すること。
SHELL_TOOLS = frozenset({"Bash", "PowerShell"})


def _note_open(reason: str, tool: str, command: str) -> None:
    """**通してしまった**理由を記録する。

    全 Bash 呼び出しを記録するのは重いので、記録するのは「ゲートが張られて
    いるのに通した」場合だけにする。このセッションで実際にあった
    「cp932 誤復号で無言素通り」「PowerShell 経由で hook が呼ばれない」は
    どちらも痕跡が無く、外から気づく手段が無かった。
    """
    try:
        GATE_DIR.mkdir(parents=True, exist_ok=True)
        line = json.dumps(
            {
                "ts": datetime.now().strftime(_TS_FMT),
                "reason": reason,
                "tool": tool,
                "command_head": command[:160],
            },
            ensure_ascii=False,
        )
        with open(GATE_DIR / "fail_open.log", "a", encoding="utf-8", newline="") as fh:
            fh.write(line + "\n")
    except OSError:
        pass  # 記録できなくても本来の処理は止めない


def handle_hook(payload: dict) -> dict | None:
    """deny する場合は hook 出力 dict、通す場合は None を返す。"""
    tool = payload.get("tool_name")
    if tool not in SHELL_TOOLS:
        return None
    command = (payload.get("tool_input") or {}).get("command")
    if not isinstance(command, str) or not mentions_codex(command):
        return None

    session = payload.get("session_id") or None
    gate, corrupt = read_gate(session)
    if corrupt:
        return _deny(_corrupt_reason())
    if gate is None:
        # このセッションを止めるゲートが無い。他セッションのゲートが有効な間は
        # 「なぜ通ったのか」が後から分からなくなるので、そのときだけ記録する。
        if list_all_gates():
            _note_open("no_gate_for_this_session", str(tool), command)
        return None

    nonce = extract_bypass_nonce(command)
    if nonce is not None and nonce == gate.get("nonce"):
        # WF が実際に走り出した。**解除はしない**(1 本目通過で無防備になる事故があったため)。
        # 走った事実だけ記録し、以後の deny メッセージで「disarm してよい」と案内する。
        if not gate.get("wf_used_at"):
            gate = dict(gate, wf_used_at=datetime.now().strftime(_TS_FMT))
            if not _write_gate(_gate_path(gate.get("session")), gate):
                # 記録できないと deny メッセージが「まだ WF を実行していない」と
                # 案内し続ける。通過自体は許すが、黙って落とさない。
                print(
                    "[cgd wf-gate] WARN: WF 実行の記録に失敗しました"
                    " (通過は許可しますが wf_used_at は残りません)",
                    file=sys.stderr,
                )
        return None

    return _deny(_deny_reason(gate))


def _run_hook() -> int:
    # 🚨 stdin は **必ずバイト列で読んで UTF-8 として明示復号する**。
    #
    # 2026-08-11 に実測: この hook は `python <script>` で起動され、Windows の既定で
    # sys.stdin.encoding == "cp932" になる。json.load(sys.stdin) はその cp932 で
    # 復号するため、UTF-8 の日本語を含むコマンドで 2 通りに壊れて **無言 fail-open** した:
    #
    #   1) `レビュー。codex exec ...` — 「。」(E3 80 82) の末尾 0x82 が続く 'c'(0x63) と
    #      結合して 1 文字になり、`codex` が `odex` に化ける。_CODEX_TOKEN_RE が
    #      一致せず mentions_codex=False → 遮断対象と見なされず素通り
    #   2) `codex exec "…レビュー、"` — 「、」(E3 80 81) の末尾 0x81 と次の '"'(0x22) が
    #      cp932 として不正な並びになり UnicodeDecodeError。これは ValueError の
    #      サブクラスなので下の except に捕まり、無言で return 0（完全な素通り）
    #
    # 日本語プロンプトは cgd の常用なので、実運用でゲートが頻繁に無効化されていた。
    # errors="replace" にしているのは、化けても「遮断側で判定を続ける」ため
    # （復号に失敗して return 0 に落ちるのが最悪の挙動）。
    try:
        raw = sys.stdin.buffer.read()
    except OSError as exc:
        print(f"[cgd wf-gate] WARN: stdin を読めません: {exc} (fail-open)", file=sys.stderr)
        return 0

    try:
        payload = json.loads(raw.decode("utf-8", errors="replace"))
    except (json.JSONDecodeError, ValueError) as exc:
        # 握り潰すと「ゲートが効いていない」ことに誰も気づけない。必ず声を上げる。
        print(
            f"[cgd wf-gate] WARN: hook 入力を JSON として解釈できません: {exc} "
            "(ゲート未評価 = fail-open)",
            file=sys.stderr,
        )
        return 0
    if not isinstance(payload, dict):
        print(
            f"[cgd wf-gate] WARN: hook 入力が dict ではありません: {type(payload).__name__} "
            "(ゲート未評価 = fail-open)",
            file=sys.stderr,
        )
        return 0
    try:
        out = handle_hook(payload)
    except Exception:  # noqa: BLE001 — hook 自体の不具合で作業を止めない
        # 握り潰すと不発の原因を追えないので、必ずトレースを残す。
        print("[cgd wf-gate] WARN: hook 内で例外 (fail-open)", file=sys.stderr)
        traceback.print_exc(file=sys.stderr)
        return 0
    if out is not None:
        sys.stdout.write(json.dumps(out, ensure_ascii=False))
    return 0


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------
def main() -> int:
    if len(sys.argv) <= 1:
        return _run_hook()

    parser = argparse.ArgumentParser(description="cgd Lv6/Lv7/Lv8 Workflow 強制ゲート")
    sub = parser.add_subparsers(dest="command", required=True)

    p_arm = sub.add_parser("arm", help="ゲートを張る (Lv6/Lv7/Lv8 のみ有効)")
    p_arm.add_argument("--level", type=int, required=True)
    p_arm.add_argument("--session", default=None, help="セッションID(省略時は最初に触れたセッションが所有)")
    p_arm.add_argument("--ttl-min", type=int, default=DEFAULT_TTL_MIN)

    for name, helptext in (("disarm", "ゲートを解除する"), ("status", "状態を表示する"), ("nonce", "WF に渡す nonce を表示する")):
        p = sub.add_parser(name, help=helptext)
        p.add_argument("--session", default=None)
        if name == "disarm":
            p.add_argument("--all", action="store_true", help="他セッションのゲートも含めて全部解除する")
        if name == "status":
            p.add_argument("--json", action="store_true", help="機械可読な JSON で出力する")

    args = parser.parse_args()

    if args.command == "arm":
        try:
            gate = arm(args.level, session=args.session, ttl_min=args.ttl_min)
        except GateWriteError as exc:
            # 「張れなかった」を exit 0 で返すと、張れたつもりで WF を回してしまう。
            print(f"[cgd wf-gate] ⚠️ {exc}", file=sys.stderr)
            return 1
        if gate is None:
            print(f"[cgd wf-gate] Lv{args.level} はゲート対象外 (対象: Lv{'/Lv'.join(map(str, WF_REQUIRED_LEVELS))})")
            return 0
        print(f"[cgd wf-gate] Lv{args.level}: WF 必須ゲートを張りました ({args.ttl_min} 分で失効)")
        # 「nonce を控えて args に貼る」手順そのものを無くす。
        # WF は status --json から自分で nonce を取るので、通常は貼る必要がない。
        # そのまま実行できる形を出して、組み立てを人間にやらせない。
        print("[cgd wf-gate] 次をそのまま実行してください (wf_nonce は WF が自分で取得します):")
        print(f'  Workflow({{ scriptPath: "{gate["workflow_script"]}",')
        print('             args: { input_path: "...", aux_input_path: "...", label: "..." } })')
        print(f"[cgd wf-gate] 終わったら: python \"{Path(__file__).as_posix()}\" disarm")
        print(f"[cgd wf-gate] (手動で迂回する場合の wf_nonce = {gate['nonce']})")
        return 0

    if args.command == "disarm":
        removed = disarm(args.session, all_sessions=args.all)
        # 解除後に残っているゲートを必ず報告する。
        # 「ゲートは張られていません」と言って終わると、セッション単位で
        # 張られたゲートが残ったまま解除できたと誤認する (INC-20260811-1440406a0dfc)。
        remaining = list_all_gates()
        # 残りが 1 件だけなら、それを解除して終える。
        # 「--session を付けないと解除されない」仕様は事故を生んだだけで
        # (INC-20260811-1440406a0dfc)、単一利用者の端末では守るものが無い。
        # 複数残っている場合だけ、取り違え防止のため明示指定を求める。
        if len(remaining) == 1 and not args.session and not args.all:
            _key, gate, _corrupt = remaining[0]
            owner = (gate or {}).get("session")
            if owner:
                # 誰のゲートを消したのか必ず言う。黙って他セッションの
                # WF 強制を無効化すると、向こうは理由も分からず素通りになる。
                print(f"[cgd wf-gate] ⚠️ セッション {owner} が張ったゲートを解除します")
            removed += disarm(owner, all_sessions=(owner is None))
            remaining = list_all_gates()

        # 解除したものは**全部**挙げる。1 件だけ報告して他を黙って消すと、
        # 「何が消えたか」が分からず解除漏れ/巻き込みに気づけない。
        if removed:
            print(f"[cgd wf-gate] 解除しました ({', '.join(removed)})")
        elif not remaining:
            print("[cgd wf-gate] ゲートは張られていません")

        if remaining:
            print(f"[cgd wf-gate] ⚠️ 解除されていないゲートが {len(remaining)} 件残っています:")
            for key, gate, corrupt in remaining:
                label = "⚠️ 破損" if corrupt else f"Lv{gate['level']} / 失効 {gate['expires_at']}"
                print(f"    {key}  {label}")
            print("[cgd wf-gate] 個別に解除: ... disarm --session <SID>   / 全部消す: ... disarm --all")
            return 1
        return 0

    # status / nonce: --session 省略時も GATE_DIR 全体を見る。
    # _unclaimed だけを見て「未設定」と返す実装だったため、自セッションのゲートが
    # 有効なのに「未設定」と表示されていた (INC-20260811-1440406a0dfc)。
    if args.session is not None:
        gate, corrupt = read_gate(args.session)
        found = [(args.session, gate, corrupt)] if (gate is not None or corrupt) else []
    else:
        found = list_all_gates()

    if args.command == "status" and getattr(args, "json", False):
        payload = {
            "armed": bool(found),
            "count": len(found),
            "gates": [
                {
                    "key": key,
                    "corrupt": corrupt,
                    "level": (gate or {}).get("level"),
                    "session": (gate or {}).get("session"),
                    "expires_at": (gate or {}).get("expires_at"),
                    "wf_used_at": (gate or {}).get("wf_used_at"),
                    # WF が自分で nonce を取れるようにする (2026-08-11)。
                    # 手で取得して args に貼る運用は「安全機構が手順書運用に落ちている」と
                    # Lv8 批評で指摘され、実際 --session の付け忘れ事故も起きた。
                    # nonce は元々 `nonce` サブコマンドで誰でも取得でき、意図的迂回も
                    # 認めている「意図の印」なので、ここに載せても防御力は変わらない。
                    "nonce": (gate or {}).get("nonce"),
                }
                for key, gate, corrupt in found
            ],
        }
        print(json.dumps(payload, ensure_ascii=False))
        return 0

    if not found:
        print("[cgd wf-gate] 未設定 (codex は遮断されません)")
        return 0

    if any(corrupt for _, _, corrupt in found):
        print("[cgd wf-gate] ⚠️ ゲートファイルが壊れています (codex は遮断されます)。disarm してください")
        return 1

    if args.command == "nonce":
        if len(found) > 1:
            print(
                "[cgd wf-gate] ⚠️ ゲートが複数あります。--session <SID> で指定してください:",
                file=sys.stderr,
            )
            for key, gate, _ in found:
                print(f"    {key}  Lv{gate['level']} / 失効 {gate['expires_at']}", file=sys.stderr)
            return 1
        print(found[0][1]["nonce"])
        return 0

    for key, gate, _ in found:
        used = gate.get("wf_used_at") or "未実行"
        owner = gate.get("session") or "(未所有)"
        print(
            f"[cgd wf-gate] Lv{gate['level']} 有効 / 所有 {owner} / 張った {gate['armed_at']}"
            f" / 失効 {gate['expires_at']} / WF実行 {used}"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
