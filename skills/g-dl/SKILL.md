---
name: g-dl
description: claude-shared をリモート Git から pull した後、`%USERPROFILE%\claude-shared\` の内容を `.claude/{skills,commands,tools,rules,memory,hooks,incidents}` にミラーコピーする。Option C ミラー方式。
---

# /g-dl — claude-shared download (mirror approach)

`%USERPROFILE%\claude-shared\` をリモートから fast-forward pull → プロジェクトの `.claude/` にミラーコピー。

**Option C ミラー方式**: ジャンクション不使用。pull 後 `robocopy //MIR` で `.claude/` を更新する。

## 使い方

```
/g-dl
```

引数なし。

## 手順

### Step 1: Bash で実行

**Bash ツール**を使うこと（git-bash 経由）。

過去に PowerShell ツール経由で exit code 1 / 出力なしで失敗する環境が確認されたため、本スキルは Bash 単独運用に統一している。

```bash
# 注意: set -e は使わない。robocopy は「成功」でも非ゼロ終了する（0-7=ok, 8+=error）ため、
# set -e があると正常コピー直後に中断する。各ステップで明示的に終了コードを判定する。

CLAUDE_DIR="$(pwd)/.claude"
if [ ! -d "$CLAUDE_DIR" ]; then
    echo "CWD に .claude/ が見つかりません: $(pwd)" >&2
    exit 1
fi

SHARED_WIN_RAW="$USERPROFILE/claude-shared"
SHARED_BASH=$(cygpath -u "$SHARED_WIN_RAW" 2>/dev/null || echo "${SHARED_WIN_RAW//\\//}" | sed 's|^C:|/c|')
if [ ! -d "$SHARED_BASH" ]; then
    echo "claude-shared not found: $SHARED_BASH (新PC は git clone が必要)" >&2
    exit 1
fi
echo "ClaudeDir: $CLAUDE_DIR"
echo "Shared:    $SHARED_BASH"

# Step A: claude-shared を Git から pull
cd "$SHARED_BASH" || exit 1

REMOTE_URL=$(git remote get-url origin 2>/dev/null)
if [ -z "$REMOTE_URL" ]; then
    echo "origin remote 未設定" >&2
    exit 1
fi
echo "remote: $REMOTE_URL"

# ローカル dirty チェック
DIRTY=$(git status --porcelain)
if [ -n "$DIRTY" ]; then
    echo "ローカル(claude-shared) に未コミット変更があります:"
    git status --short
    echo ""
    echo "先に /g-ul で push してから /g-dl を実行してください。"
    exit 1
fi

echo ""
echo "=== fetch ==="
git fetch --prune

# デフォルトブランチ動的取得
DEFAULT_BRANCH=""
HEAD_REF=$(git symbolic-ref --short refs/remotes/origin/HEAD 2>/dev/null)
if [ -n "$HEAD_REF" ]; then
    DEFAULT_BRANCH="${HEAD_REF#origin/}"
fi
if [ -z "$DEFAULT_BRANCH" ]; then
    HEAD_REF=$(git rev-parse --abbrev-ref origin/HEAD 2>/dev/null)
    if [ -n "$HEAD_REF" ] && [ "$HEAD_REF" != "origin/HEAD" ]; then
        DEFAULT_BRANCH="${HEAD_REF#origin/}"
    fi
fi
if [ -z "$DEFAULT_BRANCH" ]; then
    REMOTES=$(git branch -r 2>/dev/null)
    for candidate in main master; do
        if echo "$REMOTES" | grep -qE "origin/$candidate(\s|$)"; then
            DEFAULT_BRANCH="$candidate"
            break
        fi
    done
fi
if [ -z "$DEFAULT_BRANCH" ]; then
    DEFAULT_BRANCH=$(git branch --show-current)
fi
[ -z "$DEFAULT_BRANCH" ] && DEFAULT_BRANCH="main"
echo "default branch: $DEFAULT_BRANCH"

UPSTREAM=$(git rev-parse --abbrev-ref --symbolic-full-name '@{u}' 2>/dev/null)
if [ -z "$UPSTREAM" ]; then
    echo "upstream 未設定です。次を実行してから再試行: git branch --set-upstream-to=origin/$DEFAULT_BRANCH"
    exit 1
fi

echo ""
echo "=== upcoming changes ($UPSTREAM vs HEAD) ==="
UPCOMING=$(git log --oneline "HEAD..$UPSTREAM" 2>/dev/null)
if [ -n "$UPCOMING" ]; then
    echo "$UPCOMING"
    echo ""
    git diff --stat "HEAD..$UPSTREAM"
    echo ""
    echo "=== pull --ff-only ==="
    if ! git pull --ff-only; then
        echo ""
        echo "ff-only pull 失敗。履歴分岐。" >&2
        STAMP=$(date +"%Y%m%d%H%M%S")
        echo "  1. stash 退避: git stash push -m 'pre-gdl' ; git pull --ff-only ; git stash pop"
        echo "  2. ブランチ退避+rebase: git branch backup-$STAMP ; git pull --rebase"
        echo "  3. 最終手段 ローカル破棄: git reset --hard $UPSTREAM"
        exit 1
    fi
    echo "OK: claude-shared pulled."
else
    echo "claude-shared 側更新なし（ミラーは続行）。"
fi

# Step B: claude-shared/* -> .claude/* ミラー
# TARGETS は pull 直後の claude-shared 直下ディレクトリ一覧から動的に決める(固定リストにしない)。
# 理由: このBashブロックは「呼び出された時点で読み込まれたSKILL.md」の内容で実行される。
# もし今回の pull で /g-dl 自身の更新(ミラー対象フォルダの追加など)が含まれていても、
# 実行中のこのブロックは古い版のまま=固定リストだと新フォルダを取りこぼす
# (次回 /g-dl まで気付かれずに放置されるバグが過去にあった: 2026-07-31 B端末報告)。
# pull 後にディレクトリ一覧から都度算出すれば、1回目の実行から最新状態を反映できる。
TARGETS=$(cd "$SHARED_BASH" && for d in */; do d="${d%/}"; [ "$d" = ".git" ] && continue; echo "$d"; done)
echo ""
echo "targets (claude-shared直下から動的算出): $TARGETS"
cd /
echo ""
echo "=== Mirror claude-shared/ -> .claude/ ==="
MIRROR_OK=1
for t in $TARGETS; do
    SRC_BASH="$SHARED_BASH/$t"
    DST_BASH="$CLAUDE_DIR/$t"
    if [ ! -d "$SRC_BASH" ]; then
        echo "  $t : claude-shared 側になし、skip"
        continue
    fi
    # robocopy に渡すパスは Windows 形式 (C:\...) に変換する
    SRC_W=$(cygpath -w "$SRC_BASH")
    DST_W=$(cygpath -w "$DST_BASH")
    echo "  mirror $t ..."
    # git-bash では robocopy のスラッシュオプションが MSYS パス変換に巻き込まれるため //OPT で escape
    #
    # //XF は g-ul と**同じ一覧に揃える**（2026-08-12 修正）。こちらは共有 → 手元の
    # 向きなので、除外を漏らすと //MIR が**自分の端末の計測を消す**。実際 g-dl 側は
    # telemetry.jsonl / Qwen セッション / usage DB のどれも除外していなかった。
    robocopy "$SRC_W" "$DST_W" //MIR //NFL //NDL //NP //R:2 //W:1 \
        //XD __pycache__ ".bootstrap-bak-*" ".migrate-pending-*" \
        //XF "*.bak_*" "*.pyc" "*.migrated" \
             ".deepseek_usage_session.json" ".qwen_usage_session.json" ".gemini_usage_session.json" \
             "telemetry.jsonl" "pv_usage_*.sqlite3" "cgd_usage*.sqlite3" > /dev/null 2>&1
    EXIT=$?
    if [ $EXIT -ge 8 ]; then
        echo "    robocopy ERROR (exit=$EXIT) for $t" >&2
        MIRROR_OK=0
    fi
done

echo ""
if [ $MIRROR_OK -eq 1 ]; then
    echo "OK: .claude/ updated from claude-shared."
else
    echo "ミラー中にエラーあり。.claude/ 状態を確認してください。" >&2
    exit 1
fi
```

### Step 2: hook の登録（必須・ミラー成功時のみ）

hook スクリプト本体 (`.claude/hooks/*.py`) はミラーで配布されるが、**登録先の
`settings.local.json` は PC ごとのローカル設定**なので共有されない。配布された
hook をこの PC で有効化するために、必ず次を実行する:

```bash
python "C:/ClaudeCode/.claude/tools/install_hooks.py"
```

- **冪等**（登録済みなら「変更なし」で終了。何度実行しても重複しない）
- 既存の permissions / 他イベントの hook は保持、実行前に `.bak_` 退避
- 未登録が追加された場合は「Claude Code を再起動すると有効になります」と表示される → ユーザーに再起動を案内する
- 事前に差分だけ見たい場合は `--check`（書き込まない。未登録があれば exit 1）

### Step 3: 完了報告

取り込んだコミット数（あれば）、ミラー成否、hook 登録結果（変更なし / N 件登録＋要再起動）を 1〜2 行で報告。

## エラー時

- `CWD に .claude/ が見つかりません` → プロジェクトルートで実行
- `claude-shared not found` → 新PC は `git clone https://github.com/jl4lvw/claude-shared.git "$USERPROFILE/claude-shared"`
- ローカル(claude-shared) に未コミット変更 → 先に `/g-ul`
- ff-only pull 失敗 → ガイダンスで対処
- `robocopy ERROR (exit>=8)` → ファイルロック・権限。Claude Code 終了して再試行
- `install_hooks.py` が `[SKIP] … が見つかりません` → hooks がミラーされていない。`/g-cmp` で hooks の差分を確認
- `install_hooks.py` が `[ERROR] settings.local.json が壊れています` → JSON 構文エラー。`.bak_*` から復元して再実行

## 実装メモ

- git-bash 環境では `robocopy /MIR` を呼ぶと MSYS パス変換で `/MIR` → `C:/Program Files/Git/MIR` に化けて exit 16 で失敗する
- 対策: すべてのスラッシュオプションを `//MIR //NFL //XD ...` のダブルスラッシュで escape する
- パスは `cygpath -w` で Windows 形式（`C:\path\to`）に変換してから robocopy に渡す
- 過去に PowerShell ツールが exit code 1 で出力ゼロになる環境が確認されたため、本スキルは Bash 単独運用に統一

## 取り込み後の検証（推奨）

`/g-dl` の後も、取り込みが実際に効いたかは `verify_sync.py` で確認できる。

```
python "C:/ClaudeCode/.claude/tools/verify_sync.py"
```
