"""cgd_reviewers — cgd Lv6/7/8 のレビュアー定義（コマンド・タイムアウト等）の単一の出所.

なぜ Python 側に置くか (pv の設計理念の第 2 段階 / 2026-08-12):
    同じ定義が cgd_lv6/7/8_review.js に **3 重に複製**されていた。
    このセッションだけで「片方だけ直して差分が残る」事故を何度も踏んでいる:
      - 失敗時フォールバック指示が lv7/lv8 の codex 枠からだけ消えていた
      - 列名が lv6 だけ 'Deepseek'、lv7/lv8 は 'DS'
      - 入力パスの正規化が lv8 の CRITIC_PROMPT だけ漏れていた
    WF スクリプトは import を持てないので、JS 側では共通化できない。

**Workflow はファイルを読めない**ので、生成物を WF へ渡す経路は 2 つしかない:
    (a) agent に読ませて中継する  → LLM を介す。信頼境界は改善しない
    (b) build の出力を主 context が args にそのまま渡す → **介さない**
ここは (b) を使う。`cgd_plan.py build` が WORKFLOW_ARGS に reviewers を載せ、
WF はそれを検証してから使う。args が無ければ WF 内蔵の定義に落ちる（後方互換）。

nonce だけは実行時にしか決まらないため `__WF_NONCE__` のまま置き、WF が置換する。
入力パスは build 時点で確定するので Python 側で埋め込む。
"""

from __future__ import annotations

CODEX_PROMPT = (
    "まず {input} の全文を読み、記載の差分・対象・評価観点に従ってコードレビュー。"
    "関連関数の抜粋は入力に同梱済み。追加で開くのは最大5ファイルまでとし、"
    "超えるなら読まずに『情報不足: <欲しいファイル>』と書いて終えること。日本語で回答。"
)

CRITIC_PROMPT = (
    "まず {input} の全文を読んでください。あなたは辛口の評価者です。"
    "技術的な正しさ（バグの有無）ではなく『使う人が困らないか』"
    "『本来この仕様はどうあるべきか』の観点で、遠慮なく否定的に評価してください。"
    "次の2つの立場を併せ持ってください: "
    "(1) ITに疎い現場担当者 — 実際に使うときの使いにくさ・わかりにくさ・手数の多さ・"
    "エラー時の困りごとを利用者の生の言葉で指摘する。"
    "(2) 熟練ITアーキテクト — 『本来この仕様はどうあるべきか』を理想形から逆算し、"
    "現状の妥協・場当たり対応・本質を外した設計・優先度の誤りを批判する。"
    "出力は次の構造で: 1.現場の不満（各項目に困り度: 高/中/低を付ける） "
    "2.あるべき論とのギャップ 3.そもそも論（この機能は本当に要るか） "
    "4.辛口総評（1〜2行で断言）。"
    "擁護・肯定・『概ね良い』は禁止。技術的なバグ指摘には深入りしない。"
    "追加で開くのは最大5ファイルまでとし、超えるなら読まずに"
    "『情報不足: <欲しいファイル>』と書いて終えること(探索は1回約3,000トークン消費する)。"
    "日本語で回答。"
)

AUTH_CODEX = "Not logged in / 401 / unauthorized"
AUTH_DS = "AuthenticationError / 401 / invalid api key / DEEPSEEK_API_KEY が設定されていません"
AUTH_QWEN = "AuthenticationError / 401 / InvalidApiKey / DASHSCOPE_API_KEY が設定されていません"
AUTH_GEMINI = "AuthenticationError / 401 / invalid api key / GEMINI_API_KEY が設定されていません"

# 非 codex 系(DS/Qwen/Gemini)の timeout は 180000 -> 600000 に引き上げた (2026-08-12)。
# 入力 24.7KB の Lv8 で deepseek_critic が 180 秒に届かず出力 0 バイトで落ち、
# WF が halt: exec_failed になった。同条件で DS reviewer は約 9 分かけて完走していたので
# 単なる上限不足。Bash ツールの上限が 600000 なのでこれ以上は上げられない。
# 同日 pv 側でも同種の DS timeout に当たっている (ENGINE_TIMEOUTS 300->900)。

TOOLS = "C:/ClaudeCode/.claude/tools"


def _codex(effort: str, prompt: str) -> str:
    """codex の起動コマンド。nonce は実行時に WF が置換する。"""
    return (
        "mkdir -p /c/tmp-ai && cd /c/tmp-ai && CGD_WF_RUN=__WF_NONCE__ "
        f'codex exec -c model_reasoning_effort="{effort}" '
        f'--sandbox read-only --skip-git-repo-check "{prompt}" < /dev/null'
    )


def _py(tool: str, role: str, target: str) -> str:
    return f'python "{TOOLS}/{tool}" --role {role} "{target}"'


def build_reviewers(level: int, codex_input: str, aux_input: str | None,
                    include_gemini: bool = False, reasoning: str = "medium") -> list[dict]:
    """レベルごとのレビュアー定義を返す。

    codex_input / aux_input は **正規化済みの絶対パス**を渡すこと
    (WF 側の _toPosix / 絶対パス検査と同じ値)。
    """
    aux = aux_input or codex_input          # lv6 は 1 入力（3 者に同じものを渡す）
    cx = CODEX_PROMPT.format(input=codex_input)

    if level == 6:
        rows = [
            {"name": "codex", "kind": "tech", "cmd": _codex(reasoning, cx),
             "timeout": 600000 if reasoning == "high" else 300000,
             "usage": False, "isCodex": True, "authSignals": AUTH_CODEX},
        ]
        gemini_at = 1
    elif level == 7:
        rows = [
            {"name": "codex_med", "kind": "tech", "cmd": _codex("medium", cx),
             "timeout": 300000, "usage": False, "isCodex": True, "authSignals": AUTH_CODEX},
            {"name": "codex_high", "kind": "tech", "cmd": _codex("high", cx),
             "timeout": 600000, "usage": False, "isCodex": True, "authSignals": AUTH_CODEX},
        ]
        gemini_at = 2
    elif level == 8:
        rows = [
            {"name": "codex_med", "kind": "tech", "cmd": _codex("medium", cx),
             "timeout": 300000, "usage": False, "isCodex": True, "authSignals": AUTH_CODEX},
            {"name": "codex_high", "kind": "tech", "cmd": _codex("high", cx),
             "timeout": 600000, "usage": False, "isCodex": True, "authSignals": AUTH_CODEX},
        ]
        gemini_at = 2
    else:
        raise ValueError(f"Lv{level} は対象外です (対象: 6/7/8)")

    if include_gemini:
        rows.insert(gemini_at, {
            "name": "gemini", "kind": "tech",
            "cmd": _py("gemini_advisor.py", "reviewer", codex_input),
            "timeout": 600000, "usage": True, "isCodex": False, "authSignals": AUTH_GEMINI,
        })

    rows += [
        {"name": "deepseek", "kind": "tech", "cmd": _py("deepseek_coder.py", "reviewer", aux),
         "timeout": 600000, "usage": True, "isCodex": False, "authSignals": AUTH_DS},
        {"name": "qwen", "kind": "tech", "cmd": _py("qwen_advisor.py", "reviewer", aux),
         "timeout": 600000, "usage": True, "isCodex": False, "authSignals": AUTH_QWEN},
    ]

    if level == 8:
        rows += [
            {"name": "codex_critic", "kind": "critic",
             "cmd": _codex("high", CRITIC_PROMPT.format(input=codex_input)),
             "timeout": 600000, "usage": False, "isCodex": True, "authSignals": AUTH_CODEX},
            {"name": "deepseek_critic", "kind": "critic",
             "cmd": _py("deepseek_coder.py", "critic", aux),
             "timeout": 600000, "usage": True, "isCodex": False, "authSignals": AUTH_DS},
        ]
    return rows


def reviewer_names(level: int, include_gemini: bool = False) -> list[str]:
    return [r["name"] for r in build_reviewers(level, "x", "y", include_gemini)]
