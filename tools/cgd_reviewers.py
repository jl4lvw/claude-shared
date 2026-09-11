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

import shlex
from pathlib import Path

# 生ログを「agent に見せる用」に圧縮するフィルタ。wrap() の表示側だけで使う。
# 自分の隣に置く前提。posix 形式にするのは Git Bash 上で python に渡すため。
LOGFILTER = (Path(__file__).resolve().parent / "cgd_logfilter.py").as_posix()

# 2026-09-11: Codex CLI v0.154.0 で、Codex自身がexec_commandツールでファイルを
# 開こうとすると `CreateProcess { ... rejected: blocked by policy }` で全面拒否される
# ようになったことを実機確認した（--sandbox read-only / workspace-write とも同様）。
# そのため「まず <path> の全文を読み」とファイルを開かせる指示は現行CLIで一切成立せず、
# 「対象実ファイルも追加で読んでよい(最大5個まで)」という探索許可も同時に無効になった。
# 対策: プロンプト本文にファイルパスを書かせず、対象データ自体を _codex() が
# stdin 経由でそのまま流し込む（Codexは自分でファイルを開く必要が無くなる）。
# 詳細・再現手順: INC-20260911-123532bde1cc
CODEX_PROMPT = (
    "記載の差分・対象・評価観点に従ってコードレビューしてください。"
    "関連関数の抜粋は下に同梱済みです。"
    "対象ファイルを直接開くことはできません（環境ポリシーによりシェル実行不可）。"
    "判断に必要な情報はすべてこの入力に含まれています。"
    "不足があれば『情報不足: <欲しい情報>』とだけ書いて終えてください。日本語で回答。"
)

CRITIC_PROMPT = (
    "あなたは辛口の評価者です。"
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
    "対象ファイルを直接開くことはできません。判断に必要な情報はすべて下に同梱済みです。"
    "不足があれば『情報不足: <欲しい情報>』とだけ書いて終えてください。"
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

# **実行環境は Git Bash (MSYS) を前提にしている。**
# Claude Code の Bash ツールが Git Bash なので、生成するコマンドは
#   - `/c/tmp-ai` のような MSYS 形式のパス
#   - `set -o pipefail` / `trap ... EXIT` / `$(dirname ...)` などの bash 構文
# を使う。cmd.exe / PowerShell では動かない（3 者レビューで唯一 3 者一致した指摘）。
# bash の実在は cgd_doctor.py の shell チェックで確認している。
# advisor 3 本(deepseek_coder / qwen_advisor / gemini_advisor)は自分と同じ tools/ にある前提。
# 絶対パス直書きは 2026-08-15 の移行で旧ディレクトリを指し続け、TK 端末で部分故障した
# (旧パスに advisor だけ残っていて気づかなかった)。LOGFILTER と同じく自分からの相対で解く。
TOOLS = Path(__file__).resolve().parent.as_posix()


def _codex(effort: str, prefix: str, input_path: str) -> str:
    """codex の起動コマンド。

    2026-09-11 以降、プロンプト本文は **stdin 経由**で丸ごと流し込む
    （`{ printf 指示文; cat 入力ファイル; } | codex exec ... -`）。
    以前は `codex exec ... "まず <path> の全文を読み..."` という、Codex自身に
    ファイルを開かせる形だったが、Codex CLI v0.154.0 でこの exec_command が
    `blocked by policy` で全面拒否されるようになったため成立しない
    (INC-20260911-123532bde1cc)。stdin 経由に変えると、Codexは自分で
    ファイルを開く必要が無くなるので回避できる。副次効果として、従来
    "ファイルパス渡し" で回避していた ARG_MAX 超過（`Argument list too long`）
    も stdin には引数長の上限が無いため同時に解消する（97KB 相当で実測確認済み）。

    nonce は実行時に WF が置換する。prefix は指示文（短文・API側の定数）、
    input_path は差分＋背景等を書き込んだ入力ファイルの絶対パス。

    `set -o pipefail` を **ここで直接**入れる（wrap() 側の pipefail に依存しない）。
    build_reviewers() が `raw_paths` 付きで呼ばれれば wrap() 側でも二重に設定される
    だけで無害だが、`raw_paths` 省略時（契約テスト用・後方互換パス）は wrap() を
    経由しないため、ここで入れておかないと `cat` の失敗がパイプ越しに隠れ、
    不完全なレビューが成功扱いになる（2026-09-11 Codex Step C 再レビューで検出、
    cgd_lv6/7/8_review.js 側の builtin テンプレートとの契約テストでも同時に発覚）。
    `cat` の対象は JS 側の `cat "__INPUT_0__"` 形式（常時ダブルクォート）に合わせる
    ため、`shlex.quote()` の条件付きクォートではなく固定でダブルクォートを付ける
    （test_python_reviewers_match_workflow_builtin が両者の完全一致を検査する）。
    """
    q_prefix = shlex.quote(prefix)
    return (
        "mkdir -p /c/tmp-ai && cd /c/tmp-ai && set -o pipefail && "
        f'{{ printf \'%s\\n\\n\' {q_prefix}; cat "{input_path}"; }} | '
        f'CGD_WF_RUN=__WF_NONCE__ codex exec -c model_reasoning_effort="{effort}" '
        "--sandbox read-only --skip-git-repo-check -"
    )


def _py(tool: str, role: str, target: str) -> str:
    return f'python "{TOOLS}/{tool}" --role {role} "{target}"'


MKDIR_FAIL_RC = 90


def wrap(cmd: str, raw_path: str) -> str:
    """**生ログと終了コードをシェルに書かせる**ラッパ (2026-08-12)。

    これ以前は「標準出力を Write ツールで保存しろ」と agent に頼み、
    executed / exit_code も agent の自己申告だった。つまり成否の判定が
    LLM に乗っており、collect が検査していたのも **LLM が書いたファイル**だった。

    ここでリダイレクトすると:
      - 生ログ  = コマンドの実出力そのもの（LLM の転記を挟まない）
      - .exit   = シェルが書いた終了コード。Python が読んで executed を機械判定できる

    設計上の判断（すべて実測または Lv6 レビューの指摘に基づく）:

    - **サブシェル `( ... )` を使う。`{ ...; }` ではいけない。**
      ブレースはサブシェルを作らないので、中のコマンドが `exit` を呼ぶと
      シェルごと終了し、続く行が実行されず `.exit` が書かれない（実測確認済み）。

    - **`trap ... EXIT` で必ず .exit を書く。**
      タイムアウトや kill で打ち切られると通常経路の `echo` に到達しない。
      その場合 .exit が不在になり「走ったのか環境が壊れたのか」を区別できない。
      trap で退出時に必ず書くようにし、打ち切られた場合は 124（timeout 相当）を残す。

    - **`set -o pipefail`。** 将来コマンド側にパイプが入ったとき、
      `$?` が末尾プロセスの成否しか拾わないと失敗を取りこぼす。

    - **パスは shlex.quote でエスケープする。**
      いまは Python が生成する固定パスだけだが、二重引用符では `$( )` や
      バッククォートが展開されるため、単一引用符で閉じる。

    - **親ディレクトリは dirname から作る。** `/c/tmp-ai` 決め打ちだと
      raw_path を別の場所に置いた瞬間にリダイレクトが失敗する。
      作成に失敗したら専用コード 90 で即終了し、
      「環境の失敗」と「レビューの失敗」を取り違えないようにする。

    - 末尾の `__CGD_EXIT__=<n>` は agent に**転記させるだけ**の補助表示。
      判定の権威はあくまで Python が読む .exit ファイル側にある。
      本文と衝突しないよう `printf '\\n%s\\n'` で**行頭を保証する**
      (生ログが改行で終わらないとき echo では前の行に連結する)。

    - **Git Bash (MSYS) 前提**。`trap` / `set -o pipefail` / `$(dirname ...)` /
      `shlex.quote` の単一引用符は POSIX シェルの構文であって cmd.exe では動かない。
      cgd は Bash tool 経由でしか起動しないので実害は無いが、前提として明記しておく。

    - **ラッパ全体の終了コードは常に 0 になる**（末尾の printf が成功するため）。
      これは意図どおり。成否は .exit だけを根拠にする。

    - **表示は cgd_logfilter.py を通す (2026-08-12 追加)。**
      codex の生ログが 7.2MB に達し、その 94.7% が 39 行の巨大行だった
      (codex がモデル一覧 JSON 190KB を stderr へ繰り返し吐く不具合)。
      生ログ本体はフル保存のままで、**agent が読む側だけ**要約する。
      `|| cat` を付けてあるので、フィルタが動かない環境でも従来どおり動く。
      rc は表示より前に確定しているため、フィルタは成否判定に触れない。
    """
    q = shlex.quote(raw_path)
    qe = shlex.quote(raw_path + ".exit")
    # **trap の本体に単一引用符を入れない。**
    # `trap '...' EXIT` の中で `printf '%s\n'` と書くと、内側の引用符が
    # trap の文字列を途中で閉じてしまい、書式が `%sn` に化けて
    # .exit の中身が "0n" になる（実測で確認）。
    # 中身は整数 1 個なので echo で足りる。
    return (
        "set -o pipefail ; "
        f"__cgd_out={q} ; __cgd_exit={qe} ; "
        'mkdir -p "$(dirname "$__cgd_out")" || exit ' + str(MKDIR_FAIL_RC) + " ; "
        "__cgd_rc=124 ; "
        'trap \'echo "$__cgd_rc" > "$__cgd_exit"\' EXIT ; '
        f"( {cmd} ) > " + '"$__cgd_out"' + " 2>&1 ; "
        "__cgd_rc=$? ; "
        # 表示は要約版に通す。**rc はこの 1 行前で確定済み**なので、
        # フィルタが落ちても成否判定には影響しない。落ちたら素の cat に戻る。
        "python " + shlex.quote(LOGFILTER) + ' "$__cgd_out" || cat "$__cgd_out" ; '
        # **先頭に改行を置いて行頭を保証する。** 生ログが改行で終わっていないと
        # echo ではマーカーが直前の行に連結し、agent が最終行を読み取れない
        # (Lv6 で DS が指摘)。docstring は以前から printf を前提に書いてあったが、
        # 実装は echo のままで一致していなかった。
        "printf '\\n%s\\n' \"__CGD_EXIT__=$__cgd_rc\""
    )


def build_reviewers(level: int, codex_input: str, aux_input: str | None,
                    include_gemini: bool = False, reasoning: str = "medium",
                    raw_paths: dict[str, str] | None = None) -> list[dict]:
    """レベルごとのレビュアー定義を返す。

    codex_input / aux_input は **正規化済みの絶対パス**を渡すこと
    (WF 側の _toPosix / 絶対パス検査と同じ値)。

    raw_paths を渡すと、各コマンドを wrap() で包んで
    「生ログと終了コードをシェルが書く」形にする。省略時は素のコマンド
    (WF 内蔵定義との契約テスト用・および後方互換)。
    """
    aux = aux_input or codex_input          # lv6 は 1 入力（3 者に同じものを渡す）

    if level == 6:
        rows = [
            {"name": "codex", "kind": "tech",
             "cmd": _codex(reasoning, CODEX_PROMPT, codex_input),
             "timeout": 600000 if reasoning == "high" else 300000,
             "usage": False, "isCodex": True, "authSignals": AUTH_CODEX},
        ]
        gemini_at = 1
    elif level == 7:
        rows = [
            {"name": "codex_med", "kind": "tech",
             "cmd": _codex("medium", CODEX_PROMPT, codex_input),
             "timeout": 300000, "usage": False, "isCodex": True, "authSignals": AUTH_CODEX},
            {"name": "codex_high", "kind": "tech",
             "cmd": _codex("high", CODEX_PROMPT, codex_input),
             "timeout": 600000, "usage": False, "isCodex": True, "authSignals": AUTH_CODEX},
        ]
        gemini_at = 2
    elif level == 8:
        rows = [
            {"name": "codex_med", "kind": "tech",
             "cmd": _codex("medium", CODEX_PROMPT, codex_input),
             "timeout": 300000, "usage": False, "isCodex": True, "authSignals": AUTH_CODEX},
            {"name": "codex_high", "kind": "tech",
             "cmd": _codex("high", CODEX_PROMPT, codex_input),
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
             "cmd": _codex("high", CRITIC_PROMPT, codex_input),
             "timeout": 600000, "usage": False, "isCodex": True, "authSignals": AUTH_CODEX},
            {"name": "deepseek_critic", "kind": "critic",
             "cmd": _py("deepseek_coder.py", "critic", aux),
             "timeout": 600000, "usage": True, "isCodex": False, "authSignals": AUTH_DS},
        ]

    if raw_paths:
        missing = [r["name"] for r in rows if r["name"] not in raw_paths]
        if missing:
            raise ValueError(f"raw_paths に不足があります: {missing}")
        for r in rows:
            r["raw_path"] = raw_paths[r["name"]]
            r["cmd"] = wrap(r["cmd"], raw_paths[r["name"]])
    return rows


def reviewer_names(level: int, include_gemini: bool = False) -> list[str]:
    return [r["name"] for r in build_reviewers(level, "x", "y", include_gemini)]
