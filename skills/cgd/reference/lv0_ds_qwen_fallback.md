# cgd Lv0 代替手順: DeepSeek／Qwen に書かせる（2026-09-18 までの Lv0 本体）

> 2026-09-18 に Lv0 の実装担当を Codex へ切り替えた（SKILL.md の Lv0 節）。
> この文書は旧 Lv0 の手順を**そのまま**残したもの。使うのは SKILL.md「代替手順」に書いた場合だけ:
> ユーザーが「DS で書かせる」「Qwen で書かせる」と明示した / Codex の週枠が 80% 以上 / `cgd_lv0_codex.py resolve` が NG。
> 本文中の「Lv0」はこの代替手順を指す。

## Lv0: 委譲レーン（DS/Qwen にコード生成を任せて Claude は分解と検証に専念）

> ⚠️ **番号の慣例とのギャップに注意**: 他の Lv は「番号大=重い」だが、Lv0 は「軽量」ではなく「**量産特化の例外モード**」。小修正向きではない（Step 2-0A の小ライン警告で誘導）。一般的な開発作業の既定は依然として **Lv2**。

Lv1-8 が「**レビュー強度**を上げる」のに対し、Lv0 は「**実装の主体**を Claude → DS/Qwen に切り替える」コストレーン。scaffold / 定型実装 / pytest 量産 / docstring 追加 / 機械的リファクタ等、**量産的で判断が薄い作業**を委譲して Claude 本体のトークン消費・コンテキスト消費を抑える（Antigravity Plugin の「実行委譲」相当の思想）。

**Lv0 が機能する前提（重要・誤解されやすい）**:
- **Claude/Codex のサブスククォータを「有限資源」とみなす場合に節約効果**が出る（量産作業を外部委譲してクォータ温存）
- **サブスククォータが余っているなら Lv2 のほうが安定かつ安い**（Claude 自前は実費 ¥0、Codex/Gemini もサブスク内）
- **Lv0 は実費発生**（DS / Qwen は従量課金、scaffold 5 ファイル概算 ¥1〜5）
- → **動機**: 「サブスククォータ温存」or「Claude 本体 context の節約」なら Lv0、「総合コスト最小」だけなら Lv2 の方が安いこともある

**向く対象**:
- 複数ファイルの scaffold（API / CLI / ETL の雛形）
- 似たパターンの繰り返し実装（CRUD、データ変換）
- pytest テストケースの量産（仕様から N 件生成）
- docstring / 型ヒントの一括追加
- 機械的リネーム・import 整理
- 単一ファイルでも **100 行以上** の量産的 scaffold（API ハンドラ 1 本、テスト 1 ファイル 20 ケース等）

**向かない対象 → Lv1-2 へ誘導**:
- **想定総作業量 < 100 行 かつ 対象ファイル数 < 2**（小修正は委譲の往復コストが勝つ）
- 設計判断が重い変更（DB 設計・状態管理方式・セキュリティ）
- 既存仕様との微妙な整合性が必要なバグ修正

### Step 2-0A: 委譲計画（Claude 本体）

1. 仕様・対象を Claude が読み、**ファイル単位 / 関数単位** にユニット分解
2. 各ユニットを割振:
   - **DS coder** (`deepseek_coder.py --role coder`): 推論寄り・複雑なロジック・データ変換
   - **Qwen coder** (`qwen_advisor.py --role coder`): 実装寄り・scaffold・テスト量産
   - **Claude 自前**: 設計判断・既存コードとの精密な整合・少量修正
3. **小ライン警告**: 委譲対象の **想定総作業量**（追加+変更の合計行数）と **対象ファイル数** をユニット分解時に見積もる。以下を満たす場合は AskUserQuestion で「Lv0 はオーバースペック。Lv1-2 を推奨」と確認して中断 or 続行:
   - 想定総作業量 < 100 行 **AND** 対象ファイル数 < 2
   - つまり「1 ファイル 100 行以上の scaffold」「2 ファイル以上の量産」のいずれかなら OK、両方該当しない小修正だけ弾く
4. 計画を表で提示し AskUserQuestion で承認（**外部 API 送信前の秘匿チェック必須**）:

| # | ユニット | 担当 | 想定行数 | ファイル | 送信不可情報チェック |
|---|---|---|---|---|---|

- **送信不可情報チェック**: API キー / パスワード / 顧客個人情報 / 社内 DB 接続文字列 / 未公開仕様 等が **既存コード抜粋やプロンプト** に含まれないか目視確認。含まれる場合は伏字化（既存メモリ feedback_redact_credentials_before_llm 参照）
- DS は中国本土サーバ、Qwen は DashScope International（Singapore or US Virginia、`QWEN_BASE_URL` で切替）。送信先リージョンが業務 repo の制約に合うか確認

### Step 2-0B: 並列委譲実行（1 メッセージで Bash N 個）

承認後、DS / Qwen にコード生成依頼。

**ユニーク化（必須・複数回実行や他作業との衝突回避）**: 主 context で `RUN=$(date +%Y%m%d_%H%M%S)` を生成し、すべての委譲ファイル名に `_${RUN}` を付ける。

**プロンプト準備（ユニットごとに 1 ファイル・対象言語を必ず明示）**:

```bash
RUN=$(date +%Y%m%d_%H%M%S)
cat > "C:/tmp-ai/delegate_unit1_${RUN}.txt" <<'EOF'
以下の仕様に従ってコードを生成してください。
AGENTS.md / CLAUDE.md の規約に従う（shebang 禁止、Python なら encoding="utf-8" 明示と型ヒント、Python 3.12）。

[対象言語]
<Python / JS / TS / Go / Bash 等 — 必ず明示。DS/Qwen の coder ロールは多言語対応だが、未指定だと Python に偏る>

[仕様]
<1〜3 段落>

[既存コード抜粋（参考・最小限、5KB 以下推奨）]
<関連関数の本体のみ>

[出力]
コードのみ（説明文不要・コード断片で完結させる）。
EOF
```

**並列起動（1 メッセージで Bash N 個・out/err/status 3 ファイル wrapper）**:

並列で出力を確実に分離回収するため、各ユニットを **サブシェル `( ... )` で包んで stdout / stderr / exit code を別ファイルに保存** する。`>` で stdout だけリダイレクトする旧形式は `[DS Usage]`（stderr 出力）が落ちる + 並列で stderr が混線するため使わない。

```bash
# Bash #1: ユニット1 → DS
( python "C:/ClaudeCode/.claude/tools/deepseek_coder.py" --role coder \
    "C:/tmp-ai/delegate_unit1_${RUN}.txt" \
    > "C:/tmp-ai/delegate_unit1_${RUN}.out" \
    2> "C:/tmp-ai/delegate_unit1_${RUN}.err"; \
  echo $? > "C:/tmp-ai/delegate_unit1_${RUN}.status" )

# Bash #2: ユニット2 → Qwen
( python "C:/ClaudeCode/.claude/tools/qwen_advisor.py" --role coder \
    "C:/tmp-ai/delegate_unit2_${RUN}.txt" \
    > "C:/tmp-ai/delegate_unit2_${RUN}.out" \
    2> "C:/tmp-ai/delegate_unit2_${RUN}.err"; \
  echo $? > "C:/tmp-ai/delegate_unit2_${RUN}.status" )
```

**回収手順（Step 2-0C 直前で必ず実行）**:

```bash
for u in unit1 unit2; do
  STATUS=$(cat "C:/tmp-ai/delegate_${u}_${RUN}.status" 2>/dev/null || echo "?")
  USAGE=$(grep '^\[\(DS\|Qwen\) Usage\] 今回:' "C:/tmp-ai/delegate_${u}_${RUN}.err" 2>/dev/null | head -1)
  echo "[${u}] exit=${STATUS} ${USAGE}"
done
```

**使用量表示（必須・転記）**:
- `.err` ファイルから `[DS Usage] 今回:` / `[Qwen Usage] 今回:` 行を抽出し、Step 2-0C の手前で **そのまま表示**（料金可視化）
- `.status` の値が 0 以外のユニットはエラーとして下記の規約で扱う

**並列時の usage 累計の注意**: `deepseek_coder.py` / `qwen_advisor.py` のセッション累計 JSON は読み書き排他がないため、**同一ツールを複数ユニットで並列起動すると累計が lost update し得る**。今回ユニット単体の usage は `.err` から正確に取れるので集計は問題なし。セッション累計値は参考程度に。

**エラー時の扱い（Lv0 専用ルール・全体規約からの例外）**:

各ユニットの `.status` を見て exit code 別に判定:

| exit code | Lv0 専用ルール | 残り成功ユニットの扱い |
|---|---|---|
| 10 / 20 / 40 (auth / quota / network) | **即中断・全ユニット破棄** | 破棄（仕様の整合が崩れるため・全体規約と一致） |
| 30 / 50 (timeout / invalid input) | **該当ユニットだけ中断** | **Step 2-0C で部分適用可**（ユーザー確認必須・受領表に明示） |
| 1 (その他) | **即中断** + `.err` を Step 2-0C で表示 | ユーザー判断 |

**Lv0 のみ部分適用を許容する理由**: Lv0 の本質は「複数ユニット並列で量産」。1 ユニットの timeout/invalid input で残り全部を捨てると委譲費用が無駄になる。一方 auth/quota/network はシステム的問題なので全破棄が正解（全体規約と一致）。**Lv1-8 では部分適用は許容されない**（後述「認証エラー検出時の挙動」セクション参照）。

### Step 2-0C: 受領 + 適用（Claude 本体）

1. 生成物を `Read` で受領
2. **品質チェック**（受領時点で一読・委譲先のコードを鵜呑みにしない）:
   - 仕様との整合性
   - AGENTS.md / CLAUDE.md 規約準拠（shebang なし / `encoding="utf-8"` / 型ヒント）
   - 不要な抽象化・幻覚インポート・存在しない API 呼び出し
3. 対象ファイルに適用:
   - 既存ファイル編集前は **必ずバックアップ**（`cp file file.bak_$(date +%Y%m%d_%H%M%S)`）
   - 新規ファイルは `900.ClaudeCode/<サブフォルダ>/` 配下（CLAUDE.md フォルダルール厳守）
4. Step B 相当の検証（実 import / パス確認）を実施

### Step 2-0D: 規模依存レビュー（自動判定）

判定は **実際の `git diff` の +追加行数**（intent-to-add 含む・後述）で行う:

| diff +追加行数 | レビュー方針 |
|---|---|
| 100 行以上 | **Codex medium 1 回**で差分レビュー（Lv4-7 共通の Step C と同じ要領）・既定実行 |
| 50〜99 行 | **Claude が判断して既定は実行する**（品質側に倒す・聞かない） |
| 50 行未満 | **省略**（Claude の品質チェックのみ） |

（ちょうど 50 行 → 中段の任意レビュー、ちょうど 100 行 → 上段の Codex 実行）

**重要 1**: `git diff` 単独は **untracked file の中身を出さない**。scaffold で新規ファイル作成が主の Lv0 では、`git add -N` で intent-to-add してから diff を取らないと **Codex に空の差分を渡してレビュー成立しない**。

**重要 2**: Codex は `cwd=C:/tmp-ai` で起動するため、git コマンドは **必ず対象 repo の root を `-C` で明示** する。さもないと「C:/tmp-ai は git repo ではない」エラー or 別 repo の diff を拾う事故が起きる。

```bash
# 0. 対象 repo の root を特定（Step 2-0C で書き込んだ repo の任意のパスから）
REPO_ROOT=$(git -C "<Step 2-0C で作業した repo の任意のパス>" rev-parse --show-toplevel)
echo "REPO_ROOT=$REPO_ROOT"

# 1. 新規ファイルを intent-to-add（scaffold は新規ファイル中心なので必須）
git -C "$REPO_ROOT" add -N "<Step 2-0C で作成した新規ファイル...>"
# untracked を一括で含めたい場合（Windows / 空白入りパスにも安全な -z + -0 形式）:
git -C "$REPO_ROOT" ls-files -z -o --exclude-standard | xargs -0 -r -- git -C "$REPO_ROOT" add -N --

# 2. diff を取る（cwd 非依存）— 空差分は assert
git -C "$REPO_ROOT" diff > "C:/tmp-ai/delegate_diff.patch"
[ -s "C:/tmp-ai/delegate_diff.patch" ] || { echo "ERROR: delegate_diff.patch が空。git add -N の対象と REPO_ROOT を確認してください"; exit 1; }

# 3. Codex にレビューさせる（stdin経由でdiffを直接流し込む。「読ませる」方式は
#    Codex CLI v0.154.0でblocked by policyになり成立しない。INC-20260911-123532bde1cc）
mkdir -p "C:/tmp-ai" && cd "C:/tmp-ai" && set -o pipefail && { printf '%s\n\n' "委譲生成コードの差分レビュー。バグ・設計・規約逸脱を厳密評価。対象ファイルは開けないので、以下のdiff本文だけで判断すること。日本語回答。"; cat "C:/tmp-ai/delegate_diff.patch"; } | codex exec -c model_reasoning_effort="medium" --sandbox read-only --skip-git-repo-check -
```

**git add -N の限界（明示）**:
- バイナリファイルや生成物（lockfile / `.png` / `.jpg` 等）は diff に出ても意味が薄い → `.gitignore` 対象は除外される
- サブモジュール内ファイルは別 repo 扱いなので `-C` のパスを使い分ける必要あり
- 大量の新規ファイル（例: scaffold 50 ファイル）で diff が巨大化したら Codex のコンテキスト制限に当たる → ユニット分割を見直す

**🔴 検出時の自動修正**:
- 主体は **Claude 本体が書き直す**（Lv5 の Step C2 と同仕様）。同じ DS/Qwen への再委譲は **しない**（同じ間違いを繰り返しやすいため）
- 1 周のみ → 改善なし or 新規 🔴 で停止しユーザー判断
- 「Claude が書き直す」分 Lv0 のコスト節約効果は減るが、**品質ゲート優先**で割り切る
- 連続して 🔴 が出る対象は Lv0 が向かない兆候 → 次の Step 2-0E でユーザーに「Lv2 で再実行」を提案する

### Step 2-0E: 最終まとめ（Step D 準拠 + 委譲情報）

通常の Step D に **「委譲サマリ」セクション** を加える:

- 委譲ユニット数: N 件
- 担当内訳: DS X 件 / Qwen Y 件 / Claude 自前 Z 件
- 受領品質問題: あり / なし（あれば内訳）
- 自前で書き直したユニット: あれば一覧
- 費用集計（💰 セクション）は **通常の Step D と同じ**（DS / Qwen の生成 + Codex のレビュー）

```bash
cp <最終報告.md> "C:/tmp-ai/cgd_lv0_$(date +%Y%m%d_%H%M%S).md"
```

### Lv0 のガードレール（Antigravity 4 原則の取り込み）

- **分岐点の上で委譲**: 想定総作業量 < 100 行 **AND** ファイル数 < 2 は Lv1-2 へ誘導（Step 2-0A で警告）
- **コンテキストを薄く保つ**: 入力プロンプトは「仕様 + 関連コード抜粋（要点のみ）」。全ファイルを渡さない（Lv7 の関連関数抽出と同じ要領）
- **単発バッチ**: 1 メッセージで複数ユニットを並列起動（往復削減）
- **差分のみレビュー**: Step 2-0D は `git diff` のみ（`git add -N` で新規ファイルも diff に含める）。全ファイルは渡さない
- **受領物の検証は必須**: 委譲先のコードを鵜呑みにせず、Claude が一読して品質チェック
- **設計判断は委譲しない**: DB 設計・状態管理方式・セキュリティ要件は Claude / Lv4 以上に任せる
- **失敗時は中断 → Lv2 推奨**: 受領品質が連続 NG / 🔴 自動修正 1 周しても解決しない場合、**Lv0 を中断してユーザーに「Lv2 で再実行」を提案**。Claude が裏で延々書き直すフォールバックは **取らない**（取ると Lv0 の意義が消えるため）
