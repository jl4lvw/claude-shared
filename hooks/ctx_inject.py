"""UserPromptSubmit hook: 文脈台帳の制約・承認・状態を毎ターン会話へ差し戻す。

圧縮に依存しないのが要点。PreCompact/PostCompact は additionalContext を
返せない (2026-07-30 に公式ドキュメントで確認) ため、再注入経路は
UserPromptSubmit の additionalContext 一本に絞っている。毎ターン戻すので、
圧縮が何回起きても・auto でも manual でも、制約は必ず生きた状態で残る。

**出力するのは「圧縮が起きた直後」と「異常時」だけ。** additionalContext は
ユーザーの画面にも見えるため、毎ターン出す初版はノイズになった
(2026-07-30 に指摘を受けて変更)。

これで保護が薄くなるわけではない。注入した行は**会話履歴の一部として残る**ので、
圧縮のたびに 1 回出せば、その内容は次の圧縮まで会話に居続ける。次の圧縮が
起きればまた注入されるので鎖は切れない。逆に圧縮が起きていないターンでは
会話そのものに制約が残っているため、注入する必要がない。

台帳パスは Claude 側が scratchpad ディレクトリ名 (= session_id) から導けるので、
パスの通知も不要 (異常時と圧縮告知にだけ含める)。

出力は 1 行に詰める。注入するのは事故に直結する3枠 (制約・承認・状態) だけで、
値・失敗・検証は台帳を読ませる。上限 (20 項目 / 1500 文字) に当たった場合は
**枠の優先順 (制約 > 承認 > 状態) を守りつつ、枠内では新しい記録を優先** して
残す (古い制約だけ残って最新の制約が見えないと誤解を招くため)。

異常時は degraded mode として「ctx が無効である」ことを明示的に注入する。
静かに無効化されると、保全できている前提で作業を続けてしまうため。

コスト: 台帳1ファイルの read のみ。
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:  # pragma: no cover
    pass

import ctx_ledger as L  # noqa: E402

MAX_ITEMS = 20
MAX_CHARS = 1500

# 枠ごとの短い見出し（1行に詰めるため TAGS のラベルより更に短く）
_MARK = {"LIMIT": "🚫", "OK": "✅", "STATE": "⚙️"}


def _state_path(key: str) -> Path:
    return L.LOG_DIR / f"{key}.json"


def _load_state(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError, ValueError):
        return {}


def _save_state(path: Path, data: dict) -> None:
    """PID 付き一時ファイル経由で atomic 置換する（並行実行で衝突しない）。"""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, path)
    except OSError as exc:
        L.log(f"save_state failed: {path}: {exc}")


def _build(entries: list[tuple[str, str, str]]) -> tuple[list[str], int]:
    """注入する項目と、上限で切り捨てた件数を返す。

    項目は 1 行に詰めるので時刻は付けない（必要なら台帳を読めば判る）。
    """
    act = L.active(entries)
    kept: list[str] = []
    dropped = 0
    total = 0
    for tag in L.INJECT_TAGS:
        rows = [f"{_MARK[tag]} {L.redact(body)}" for _, body in act.get(tag, [])]
        chosen: list[str] = []
        for row in reversed(rows):  # 枠内では新しい記録を優先して確保
            if len(kept) + len(chosen) >= MAX_ITEMS or total + len(row) > MAX_CHARS:
                break
            chosen.append(row)
            total += len(row)
        chosen.reverse()  # 表示は時系列順に戻す
        dropped += len(rows) - len(chosen)
        kept.extend(chosen)
    return kept, dropped


def _emit(text: str) -> None:
    out = {
        "hookSpecificOutput": {
            "hookEventName": "UserPromptSubmit",
            "additionalContext": text,
        }
    }
    sys.stdout.write(json.dumps(out, ensure_ascii=False))


def _run(payload: dict) -> None:
    key = L.session_key(payload)
    path = L.ledger_path(key)
    if key is None or path is None:
        L.log(f"no session identity; payload keys={sorted(payload.keys())}")
        _emit(
            "[ctx] ⚠️ セッション識別子が取得できないため文脈台帳を無効化しました。"
            "制約・承認・状態の自動保全は働いていません。"
            "重要な制約はこの会話で明示的に確認し直してください。"
        )
        return

    L.ensure_ledger(path, key)
    if not path.exists():
        _emit(
            f"[ctx] ⚠️ 文脈台帳を作成できませんでした ({path})。"
            "制約・承認・状態の自動保全は働いていません。"
        )
        return

    entries = L.parse(path)
    lines, dropped = _build(entries)
    compacts = L.compact_events(entries)

    # 圧縮の告知は「増えた回だけ」出す。毎ターン言うと騒がしく無視されるため。
    # 台帳が作り直された場合は世代が変わるので既報数をリセットする
    # (clamp するだけでは以降の圧縮を永久に告知できなくなる)。
    gen = L.generation(path)
    state_path = _state_path(key)
    state = _load_state(state_path)
    same_gen = state.get("generation") == gen
    announced = state.get("announced_compacts", 0)
    if not same_gen or not isinstance(announced, int) or announced < 0:
        announced = 0
    fresh_compact = len(compacts) > announced
    if fresh_compact or not same_gen:
        _save_state(
            state_path, {"generation": gen, "announced_compacts": len(compacts)}
        )

    # 圧縮直後だけ出す。それ以外のターンは黙る（会話にまだ制約が残っているため）
    if not fresh_compact:
        return

    last = compacts[-1]
    parts = [
        f"[ctx] ⚠️ コンテキスト圧縮が起きました (計 {len(compacts)} 回 / "
        f"最新 {last[0]} {last[1]})。{path} を Read して 6 枠を点検し、"
        "守るべき制約・得た承認・起動中プロセスを宣言してから作業を続けてください。"
    ]
    if lines:
        tail = f"  (他 {dropped} 件省略)" if dropped else ""
        parts.append("[ctx] " + " | ".join(lines) + tail)
    else:
        parts.append("[ctx] 台帳に有効な制約・承認・状態の記録はありません。")

    _emit("\n".join(parts))


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        return 0
    if not isinstance(payload, dict):
        return 0
    try:
        _run(payload)
    except Exception as exc:  # 本流は止めないが、無効化されたことは必ず知らせる
        L.log(f"inject failed: {type(exc).__name__}: {exc}")
        try:
            _emit(
                f"[ctx] ⚠️ 文脈台帳フックが異常終了しました ({type(exc).__name__})。"
                "制約・承認・状態の自動保全は働いていません。"
                f"ログ: {L.LOG_DIR / 'ctx_hook.log'}"
            )
        except Exception:  # pragma: no cover
            pass
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:  # pragma: no cover — フックは絶対に本流を止めない
        sys.exit(0)
