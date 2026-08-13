"""cgd_prompts — cgd Lv6/7/8 の **レビュー依頼テキスト** の単一の出所.

なぜ Python 側に置くか (2026-08-12 ユーザー指示):
    同じ文面が cgd_lv6/7/8_review.js に 3 重に複製されており、
    「片方だけ直して差分が残る」事故を繰り返し踏んでいる。
    WF スクリプトは import を持てないので JS 側では共通化できない。

    **注意: 元の JS テンプレートも決定論的だった**（LLM が書いていたわけではない）。
    ここへ移して得られるのは「揺らぎの低減」ではなく **重複の解消** である。
    実際の揺らぎは agent が自己申告していた executed/exit_code にあり、
    そちらは cgd_reviewers.wrap() でシェルに書かせる形にして潰した。


    **統合指示はここには無い (2026-08-13)。**
    以前 merge_prompt() を置いていたが、**誰からも呼ばれていなかった上に、
    呼んでも動かないものだった**。WF の MERGE_SCHEMA は
    convergent_findings / codex_divergent_findings / aux_only_findings と
    統合表の列順を要求するのに、この関数はそのどれにも触れていなかったため。
    統合指示は各レベルの findings の突き合わせ方そのもの (Lv7 なら Codex 多重の
    収束/乖離判定) で、**実行時の findings が決まらないと組み立てられない**。
    Python 側で事前生成できないので、cgd_lv6/7/8_review.js に置いたままにする。
    「依頼テキストの単一の出所」を名乗れるのは **レビュー依頼の側だけ**である、
    と正直に書いておく (pv Lv3 の外部視点担当が二重管理として指摘し、実読で確定)。

渡し方:
    Workflow はファイルを読めないので、`cgd_plan.py build` が生成して
    WORKFLOW_ARGS に載せ、主 context が args としてそのまま渡す（LLM を介さない）。
    args に無ければ WF 内蔵の文面に落ちる（後方互換）。
"""

from __future__ import annotations

# 生ログの末尾に必ず出る行。agent はこれを**転記するだけ**にする。
EXIT_MARKER = "__CGD_EXIT__"


def review_prompt(level: int, reviewer: dict, label: str) -> str:
    """1 レビュアー分の依頼テキスト。

    reviewer は cgd_reviewers.build_reviewers() の 1 要素
    （cmd は wrap 済みで、生ログと .exit をシェルが書く形になっている）。
    """
    name = reviewer["name"]
    is_critic = reviewer.get("kind") == "critic"
    raw = reviewer.get("raw_path", "")

    if is_critic:
        structure = (
            "   - difficulty: 現場の困り度 高/中/低"
            f"（{name} が付けた困り度をそのまま尊重する）\n"
            "   - axis: 「現場の不満」/「あるべき論とのギャップ」/「そもそも論」のどれか\n"
            "   - title / rationale (根拠1行・利用者の生の言葉に近いほどよい)"
            " / suggested_direction (改善の方向)\n"
            "   - 重要: 批評は severity を持たない。"
            "技術的なバグ指摘が混ざっていたら **批評 findings には入れない**。"
        )
    else:
        structure = (
            "   - severity: 🔴 (重大: セキュリティ/データ破壊/公開API逸脱/明白な論理バグ/"
            "integrationバグ) / 🟠 (重要) / 🟡 (注意)\n"
            "   - title / location (file:line) / rationale (根拠1行) / recommended_fix\n"
            f"   - 重要: {name} が **実際に挙げた severity をそのまま尊重** する。"
            "あなたが勝手に格上げ/格下げしない。"
        )

    usage = (
        "stderr の [DS Usage] / [Qwen Usage] / [Gemini Usage] の「今回:」行を usage_line に転記する。"
        if reviewer.get("usage") else 'usage_line は空文字 ("")。'
    )

    return f"""あなたは外部レビュアー「{name}」を実行し、その出力を構造化レビュー結果に変換する担当です。
これは cgd Lv{level} の{'**批評レビュー**' if is_critic else '**技術レビュー**'}枠です。

[手順]
1. Bash tool を timeout={reviewer['timeout']} (ミリ秒) で使って次のコマンドを実行する:
{reviewer['cmd']}

   このコマンドは **生ログと終了コードをシェル自身が書く**。
   あなたが Write ツールで保存する必要は無い（保存しないこと）。
     - 生ログ : {raw}
     - 終了   : {raw}.exit
2. 出力の**最終行**に `{EXIT_MARKER}=<数値>` が出る。
   **その数値をそのまま exit_code に入れる。** 自分で判断しない。
   `{EXIT_MARKER}=0` のときだけ executed=true、それ以外は executed=false。
   マーカーが見当たらない場合は executed=false / exit_code=-1 とする。
3. 出力を読み、指摘を findings 配列に構造化する:
{structure}
4. {usage}
5. 認証エラー ({reviewer.get('authSignals', '')}) を検出したら auth_error=true、findings は空配列。
   それ以外は auth_error=false。
6. reviewer フィールドに "{name}" を入れる。
7. raw_log_path に "{raw}" を入れる。

[重要]
- 最終出力 (schema JSON) だけが親に返る。生レビュー文を return に含めない。
- 指摘が 0 件でも findings は空配列で返す（成否は exit_code で伝わる）。
- **コマンドが失敗した場合も findings に 🟠「{name} 実行失敗」を 1 件入れる。**
  「指摘なし」と「実行失敗」は別物。
- 出力を要約・脚色しない。{name} が言っていないことを書かない。

JSON で返す。"""
