"""Lv3A の roster 拡張テストの共通部品: 偽の実行器・決定論的な走行・出力の正規化。

lv3 (従来の組) の出力が拡張の前後で 1 バイトも変わらないことを固定するために使う。
`lv3a_golden_data.py` の GOLDEN は、**拡張前の cgd_lv3a.py** にこのファイルの run_scenario /
direct_report_scenario を当てて作った (作り直すときは変更前の複製から。新しいコードで作ると回帰の意味が消える)。
モジュールは引数で受け取るので、変更前・後のどちらにも同じ手順を当てられる。
"""

from __future__ import annotations

import hashlib
import json
import re
import threading
from pathlib import Path
from typing import Any, Callable, Sequence

# 走行シナリオ: 名前 → (追加の引数, 実行器の設定)
SCENARIOS: dict[str, dict[str, Any]] = {
    "default": {"extra": [], "exit_codes": {}, "invalid_first": set()},
    "no_ds": {"extra": ["--no-ds"], "exit_codes": {}, "invalid_first": set()},
    "partial": {"extra": [], "exit_codes": {"ds_crit": 1}, "invalid_first": set()},
    "effort_high": {"extra": ["--effort", "high"], "exit_codes": {}, "invalid_first": set()},
    "retry": {"extra": [], "exit_codes": {}, "invalid_first": {"codex_tech"}},
}

# レビュアーごとの指摘 (severity, 見出し)。同じ見出しは統合者の偽物が 1 クラスタにまとめる
FINDINGS: dict[str, list[tuple[str, str]]] = {
    "codex_tech": [("🔴", "境界値で例外が出る"), ("🟠", "テストが不足している"), ("🟡", "命名が揺れている")],
    "ds_tech": [("🔴", "境界値で例外が出る"), ("🟠", "ログが出ない")],
    "codex_crit": [("高", "手数が多い"), ("中", "エラー文言が不親切")],
    "ds_crit": [("高", "手数が多い"), ("低", "そもそも要るのか")],
    "codex_tech_high": [("🔴", "境界値で例外が出る"), ("🟠", "呼出経路で副作用が違う")],
    "qwen_tech": [("🟠", "テストが不足している"), ("🟡", "型ヒントが無い")],
}
BRIEF = "# 依頼\n\n## 確認済みの事実\n\n- 現行処理は手動です\n"
SOURCE = "def add(a, b):\n    return a + b\n"

# ベンダーごとの stderr (費用の集計に使う。Qwen は qwen_advisor.py の実物の書式)
STDERR = {
    "Codex": "tokens used\n12,345\n",
    "DeepSeek": "[DS Usage] 今回: 入力 1,000 出力 500 ¥0.500\n",
    "Qwen": "[Qwen Usage] 今回: 入力 800 (miss) + 200 (hit) / 出力 500 tok (¥1.25 / $0.0083) [model=qwen3-coder-plus]\n"
            "[Qwen Usage] 累計: 入力 800 (miss) + 200 (hit) / 出力 500 tok (¥1.25 / $0.0083) [1 calls / since x / 1USD=¥150.00]\n",
}


def review_text(name: str, prefix: str, severities: Sequence[str], valid: bool = True) -> str:
    body = "詳しい指摘です。" * 30
    if not valid:
        return body
    findings = [
        {"id": f"{prefix}{number}", "severity": severity, "headline": headline}
        for number, (severity, headline) in enumerate(FINDINGS[name], 1)
        if severity in severities
    ]
    return body + "\n```json\n" + json.dumps({"findings": findings}, ensure_ascii=False) + "\n```\n"


class DeterministicRunner:
    """呼び出しを記録し、決まった回答を返す偽のレビュアー実行器。"""

    def __init__(self, module: Any, exit_codes: dict[str, int] | None = None, invalid_first: set[str] | None = None) -> None:
        self.module = module
        self.exit_codes = exit_codes or {}
        self.invalid_first = invalid_first or set()
        self.lock = threading.Lock()
        self.log: list[dict[str, Any]] = []
        self.counts: dict[str, int] = {}

    def __call__(self, item: Any, prompt: str, cwd: Path, timeout: int, codex_path: str, tools: Path, effort: str) -> Any:
        del cwd, codex_path, tools
        with self.lock:
            count = self.counts.get(item.name, 0) + 1
            self.counts[item.name] = count
            self.log.append({"name": item.name, "attempt": count, "effort": effort, "timeout": timeout, "prompt": prompt})
        valid = not (item.name in self.invalid_first and count == 1)
        text = review_text(item.name, item.prefix, item.severities, valid)
        return self.module.ExecResult(self.exit_codes.get(item.name, 0), text, STDERR[item.vendor])

    def calls_of(self, name: str) -> list[dict[str, Any]]:
        return [entry for entry in self.log if entry["name"] == name]


class DeterministicIntegrator:
    """統合者の偽物: (kind, 見出し) が同じ指摘を 1 クラスタにまとめる。"""

    def __init__(self, module: Any) -> None:
        self.module = module
        self.prompts: list[str] = []

    def __call__(self, prompt: str, cwd: Path, timeout: int, codex_path: str) -> Any:
        del cwd, timeout, codex_path
        self.prompts.append(prompt)
        kind = "technical"
        groups: dict[tuple[str, str], list[str]] = {}
        for line in prompt.splitlines():
            if line.startswith("## R"):
                kind = "critic" if "(critic)" in line else "technical"
            elif line.startswith("R") and ": [" in line:
                finding_id, rest = line.split(":", 1)
                headline = rest.split("] ", 1)[1]
                groups.setdefault((kind, headline), []).append(finding_id)
        clusters = [
            {
                "id": f"C{number}", "kind": key[0], "title": key[1], "members": members,
                "proposal": "修正する", "adopt": "採用", "adopt_reason": "レビューに基づく",
            }
            for number, (key, members) in enumerate(groups.items(), 1)
        ]
        first = next(iter(groups.values()))[0]
        question = {
            "id": "Q1", "question": "どちらへ進めますか？",
            "options": [{"label": "A", "description": "案 A"}, {"label": "B", "description": "案 B"}],
            "recommended": "A", "recommended_reason": f"{first} が指摘している",
        }
        payload = {"summary": "総評です。", "clusters": clusters, "questions_for_user": [question], "next_actions": ["修正する"]}
        return self.module.ExecResult(0, json.dumps(payload, ensure_ascii=False), "tokens used\n2,000\n")


def fixed_run_dir(work_root: Path, label: str, now: Any = None) -> Path:
    """乱数と時刻を固定した run ディレクトリ (匿名化のシードが run 名なので、これで出力が決まる)。"""
    del now
    run_dir = work_root / f"{label}_20260920_000000_abcdef"
    run_dir.mkdir(parents=True, exist_ok=False)
    return run_dir


def _variants(path: Path) -> list[str]:
    return [str(path), path.as_posix(), str(path).replace("\\", "\\\\")]


def _scrub(text: str, replacements: Sequence[tuple[Path, str]]) -> str:
    for path, token in replacements:
        for variant in _variants(path):
            text = text.replace(variant, token)
    return re.sub(r"所要: [0-9.]+ 秒", "所要: <T> 秒", text)


def _walk(value: Any, fn: Callable[[str], str]) -> Any:
    if isinstance(value, str):
        return fn(value)
    if isinstance(value, list):
        return [_walk(item, fn) for item in value]
    if isinstance(value, dict):
        return {key: _walk(item, fn) for key, item in value.items() if key != "elapsed_seconds"}
    return value


def strip_added_keys(run_json: dict[str, Any]) -> dict[str, Any]:
    """roster 拡張で足したキーを外す (lv3 の run.json が「追加キーを除いて」変更前と同一かを比べるため)。"""
    stripped = json.loads(json.dumps(run_json))
    stripped.pop("roster", None)
    for reviewer in (stripped.get("reviewers") or {}).values():
        reviewer.pop("vendor", None)
        reviewer.pop("effort", None)
    return stripped


def sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def run_scenario(
    module: Any, tmp_path: Path, capsys: Any, monkeypatch: Any, scenario: str, extra: Sequence[str] = (),
) -> dict[str, Any]:
    """1 回走らせ、比べるべき出力を正規化して返す。extra は SCENARIOS の引数の後ろに足す (例: --roster lv3)。"""
    config = SCENARIOS[scenario]
    monkeypatch.setattr(module, "create_run_dir", fixed_run_dir)
    # 相対パスで渡す: 対象のパスはレビュー入力の見出し (### <パス>) に載るので、絶対パスだと tmp の場所で入力の中身が変わる
    monkeypatch.chdir(tmp_path)
    brief, source = Path("brief.md"), Path("sample.py")
    brief.write_text(BRIEF, encoding="utf-8", newline="")
    source.write_text(SOURCE, encoding="utf-8", newline="")
    work = tmp_path / "runs"
    runner = DeterministicRunner(module, config["exit_codes"], config["invalid_first"])
    integrator = DeterministicIntegrator(module)
    argv = ["run", "--brief", str(brief), "--files", str(source), "--work-root", str(work), "--label", "t",
            *config["extra"], *extra]
    exit_code = module.main(argv, reviewer_runner=runner, integrator_runner=integrator,
                            usage_logger=lambda: None, resolver=lambda: "codex", weekly=lambda: 0.0)
    captured = capsys.readouterr()
    run_dir = work / "t_20260920_000000_abcdef"
    replacements = [(run_dir, "<RUN_DIR>"), (tmp_path, "<TMP>")]

    def scrub(text: str) -> str:
        return _scrub(text, replacements)

    def read(name: str) -> str:
        return (run_dir / name).read_text(encoding="utf-8")

    run_json = strip_added_keys(json.loads(read("run.json"))) if (run_dir / "run.json").exists() else None
    return {
        "exit": exit_code,
        "stdout": scrub(captured.out),
        "stderr": scrub(captured.err),
        "report_md": scrub(read("report.md")) if (run_dir / "report.md").exists() else None,
        "run_json": _walk(run_json, scrub) if run_json is not None else None,
        "questions_json": _walk(json.loads(read("questions.json")), scrub) if (run_dir / "questions.json").exists() else None,
        "review_input_sha256": sha(read("review_input.txt")),
        "calls": sorted(
            (
                {"name": e["name"], "attempt": e["attempt"], "effort": e["effort"], "timeout": e["timeout"],
                 "prompt_sha256": sha(e["prompt"]), "prompt_len": len(e["prompt"])}
                for e in runner.log
            ),
            key=lambda entry: (entry["name"], entry["attempt"]),
        ),
        "integration_prompts_sha256": [sha(p) for p in integrator.prompts],
        "run_files": sorted(path.name for path in run_dir.iterdir()),
    }


def direct_report_scenarios(module: Any, tmp_path: Path) -> dict[str, str]:
    """build_report を直接呼ぶ 3 通り (行数超過の縮め方・暫定+伏字・DS なし・🔴 の詳細・質問)。"""

    def inputs(count: int, severity: str = "🟡") -> tuple[dict[str, dict[str, str]], list[dict[str, Any]], dict[str, Any]]:
        metadata: dict[str, dict[str, str]] = {}
        clusters: list[dict[str, Any]] = []
        for number in range(1, count + 1):
            member = f"R1#{number}"
            metadata[member] = {
                "kind": "technical", "vendor": "Codex", "reviewer": "R1", "reviewer_name": "codex_tech",
                "log_file": "codex_tech", "severity": severity, "headline": f"見出し{number:02d}",
            }
            clusters.append({
                "id": f"C{number}", "kind": "technical", "title": f"題名{number:02d}", "members": [member],
                "proposal": "案", "adopt": "採用", "adopt_reason": "理由", "severity": severity,
                "vendors": ["Codex"], "single_source": number % 2 == 0,
            })
        return metadata, clusters, {"summary": "総評", "next_actions": ["次へ"], "questions_for_user": []}

    costs = {"codex_calls": 3, "codex_tokens": 12345, "ds_calls": 2, "ds_yen": 1.5}
    out: dict[str, str] = {}
    metadata, clusters, payload = inputs(90)
    missing = [{"name": "ds_tech", "reason": "JSON不正: 本文が 200 バイト未満"}, {"name": "ds_crit", "reason": "実行失敗(終了コード1)"}]
    out["long_partial"] = _scrub(module.build_report(
        "run", 1, clusters, metadata, payload, [], costs, tmp_path, False,
        partial_missing=missing, redaction_count=3, no_finding_reviewers=["ds_crit"],
    ), [(tmp_path, "<TMP>")])
    metadata, clusters, payload = inputs(2)
    out["short_no_ds"] = _scrub(module.build_report("run", 1, clusters, metadata, payload, [], costs, tmp_path, True), [(tmp_path, "<TMP>")])
    metadata, clusters, payload = inputs(4, "🔴")
    payload["questions_for_user"] = [{
        "id": "Q1", "question": "選ぶ?", "options": [{"label": "A", "description": "a"}, {"label": "B", "description": "b"}],
        "recommended": "A", "recommended_reason": "根拠不明",
    }]
    questions = module.prepare_questions(payload, BRIEF, metadata)
    out["reds_questions"] = _scrub(module.build_report("run", 2.5, clusters, metadata, payload, questions, costs, tmp_path, False), [(tmp_path, "<TMP>")])
    return out
