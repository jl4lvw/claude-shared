---
name: g-cmp
description: 三方比較スキル。`.claude/` (作業) ↔ `claude-shared/` (Git WD) ↔ `origin` (GitHub) の差分を一覧表示する。Option C ミラー方式。`/g-ul` や `/g-dl` の前の確認用。読み取り専用。
---

# /g-cmp — three-way compare (mirror approach)

`.claude/` (作業), `%USERPROFILE%\claude-shared\` (Git WD), `origin/main` (GitHub) の三方差分を表示。

`/g-ul`（push）や `/g-dl`（pull）を実行する前の確認用。読み取り専用、いかなる変更も加えない（fetch のみ）。

## 使い方

```
/g-cmp
```

引数なし。

## 手順

### Step 1: Bash で実行

**Bash ツール**を使うこと（git-bash 経由）。

過去に PowerShell ツール経由で exit code 1 / 出力なしで失敗する環境が確認されたため、本スキルは Bash 単独運用に統一している。Windows ネイティブ実行が必要な場合のみ、後述の PowerShell 版（参考）を流用すること。

```bash
SHARED="$USERPROFILE/claude-shared"
# git-bash で USERPROFILE は "C:\Users\name" 形式。/c/Users/name 形式に正規化
SHARED_BASH=$(cygpath -u "$SHARED" 2>/dev/null || echo "${SHARED//\\//}" | sed 's|^C:|/c|')
CLAUDE_DIR="$(pwd)/.claude"

if [ ! -d "$CLAUDE_DIR" ]; then
    echo "CWD に .claude/ が見つかりません: $(pwd)"
    exit 1
fi
if [ ! -d "$SHARED_BASH" ]; then
    echo "claude-shared not found: $SHARED_BASH"
    echo "新PC は git clone https://github.com/jl4lvw/claude-shared.git \"\$USERPROFILE/claude-shared\""
    exit 1
fi

echo "=== three-way compare ==="
echo "PC:        $(hostname)"
echo "ClaudeDir: $CLAUDE_DIR"
echo "Shared:    $SHARED_BASH"
echo ""

# ----- [1/3] .claude/ vs claude-shared/ (mirror gap) -----
echo "--- [1/3] .claude/ vs claude-shared/ (ミラー差分) ---"
TARGETS="skills commands tools rules memory"
DIFF_FOUND=0
# /g-ul / /g-dl と同じ除外パターンに揃える
# robocopy /XD __pycache__ .bootstrap-bak-* .migrate-pending-*
# robocopy /XF *.bak_* *.pyc .deepseek_usage_session.json
for t in $TARGETS; do
    src="$CLAUDE_DIR/$t"
    dst="$SHARED_BASH/$t"
    if [ ! -d "$src" ] && [ ! -d "$dst" ]; then continue; fi
    if [ ! -d "$src" ]; then
        echo "  $t : .claude 側欠落 (claude-shared 側のみ存在)"
        DIFF_FOUND=1
        continue
    fi
    if [ ! -d "$dst" ]; then
        echo "  $t : claude-shared 側欠落 (.claude 側のみ存在)"
        DIFF_FOUND=1
        continue
    fi
    diff_out=$(diff -rq "$src" "$dst" 2>/dev/null \
        | grep -v '__pycache__' \
        | grep -v '\.bootstrap-bak-' \
        | grep -v '\.migrate-pending-' \
        | grep -v '\.bak_' \
        | grep -v '\.pyc' \
        | grep -v 'deepseek_usage_session\.json')
    if [ -n "$diff_out" ]; then
        count=$(echo "$diff_out" | wc -l)
        echo "  $t : $count entries differ"
        echo "$diff_out" | head -5 | sed 's/^/      /'
        if [ "$count" -gt 5 ]; then
            echo "      ... ($((count - 5)) more)"
        fi
        DIFF_FOUND=1
    fi
done
if [ $DIFF_FOUND -eq 0 ]; then
    echo "  (差分なし: .claude/ と claude-shared/ は同期済み)"
else
    echo ""
    echo "  -> /g-ul で .claude -> claude-shared に反映できます。"
fi

# ----- [2/3] & [3/3] claude-shared vs origin (Git) -----
cd "$SHARED_BASH" || exit 1
echo ""
echo "--- [2/3] claude-shared (ローカル Git WD) 状態 ---"

REMOTE_URL=$(git remote get-url origin 2>/dev/null)
if [ -z "$REMOTE_URL" ]; then
    echo "  origin remote 未設定"
    exit 0
fi
echo "  remote: $REMOTE_URL"

if ! git rev-parse --verify HEAD >/dev/null 2>&1; then
    echo "  ローカルに commit なし（空リポジトリ）"
    exit 0
fi

BRANCH=$(git branch --show-current 2>/dev/null)
if [ -z "$BRANCH" ]; then
    echo "  detached HEAD"
    exit 0
fi
UPSTREAM=$(git rev-parse --abbrev-ref --symbolic-full-name '@{u}' 2>/dev/null)
if [ -z "$UPSTREAM" ]; then
    echo "  upstream 未設定 (branch=$BRANCH)"
    exit 0
fi
echo "  branch: $BRANCH (upstream: $UPSTREAM)"

FETCH_OK=1
if ! git fetch --prune >/dev/null 2>&1; then
    FETCH_OK=0
    echo "  fetch 失敗（キャッシュ参照で続行）"
fi

DIRTY=$(git status --porcelain)
if [ -z "$DIRTY" ]; then
    echo "  uncommitted: なし (clean)"
else
    DIRTY_COUNT=$(echo "$DIRTY" | wc -l)
    echo "  uncommitted: $DIRTY_COUNT entries"
    echo "$DIRTY" | sed 's/^/    /'
fi

echo ""
echo "--- [3/3] claude-shared vs $UPSTREAM (Git 同期) ---"
COUNT=$(git rev-list --left-right --count "$UPSTREAM...HEAD" 2>/dev/null)
REV_OK=$?
if [ $REV_OK -eq 0 ] && [ -n "$COUNT" ]; then
    BEHIND=$(echo "$COUNT" | awk '{print $1}')
    AHEAD=$(echo "$COUNT" | awk '{print $2}')
    echo "  ahead: $AHEAD (push 待ち) / behind: $BEHIND (pull 待ち)"
else
    BEHIND=0
    AHEAD=0
    echo "  rev-list 失敗"
fi

if [ "${AHEAD:-0}" -gt 0 ]; then
    echo "  未push commits:"
    git log --oneline "$UPSTREAM..HEAD" 2>/dev/null | sed 's/^/    /'
fi
if [ "${BEHIND:-0}" -gt 0 ]; then
    echo "  未取込 commits:"
    git log --oneline "HEAD..$UPSTREAM" 2>/dev/null | sed 's/^/    /'
fi

echo ""
echo "  最終コミット:"
echo "    ローカル HEAD     : $(git log -1 --format='%h %ad %s' --date=short HEAD 2>/dev/null)"
LAST_REMOTE=$(git log -1 --format='%h %ad %s' --date=short "$UPSTREAM" 2>/dev/null)
if [ -n "$LAST_REMOTE" ]; then
    echo "    リモート $UPSTREAM : $LAST_REMOTE"
fi

# ----- 推奨アクション -----
echo ""
echo "--- 推奨アクション ---"
HAD=0
if [ $DIFF_FOUND -eq 1 ]; then
    echo "  - .claude/ と claude-shared/ がズレています: /g-ul で push (ミラー含む)"
    HAD=1
fi
if [ -n "$DIRTY" ]; then
    echo "  - claude-shared 側 uncommitted 変更あり: /g-ul で push"
    HAD=1
fi
if [ "${AHEAD:-0}" -gt 0 ] && [ "${BEHIND:-0}" -eq 0 ]; then
    echo "  - 未push commit $AHEAD 件: /g-ul"
    HAD=1
elif [ "${AHEAD:-0}" -eq 0 ] && [ "${BEHIND:-0}" -gt 0 ]; then
    echo "  - 未取込 commit $BEHIND 件: /g-dl"
    HAD=1
elif [ "${AHEAD:-0}" -gt 0 ] && [ "${BEHIND:-0}" -gt 0 ]; then
    echo "  - state=diverged (ahead $AHEAD / behind $BEHIND)"
    echo "    git stash 退避 → git pull --rebase か手動マージが必要"
    HAD=1
fi
if [ $FETCH_OK -eq 0 ]; then
    echo "  - fetch 失敗（最新リモート未反映）"
    HAD=1
fi
if [ $HAD -eq 0 ]; then
    echo "  - 完全同期 (.claude == claude-shared == origin/main)"
fi
```

### Step 2: 結果の解釈

3 つのレベルでの差分を表示:
1. **`.claude/` vs `claude-shared/`** — ミラー差分（`/g-ul` で同期）
2. **`claude-shared/` 状態** — uncommitted 変更
3. **`claude-shared` vs `origin/main`** — Git push/pull 差分

完全同期 = (1) 差分なし + (2) clean + (3) ahead 0 / behind 0

### Step 3: 完了報告

「完全同期 / mirror ズレ / push 待ち / pull 待ち / diverged」のいずれかを 1 行で報告。

## エラー時

- `CWD に .claude/ が見つかりません` → プロジェクトルートで実行
- `claude-shared not found` → 新PC は git clone
- `origin remote 未設定` / `upstream 未設定` → /g-ul で初回 push
- `git fetch 失敗` → 認証 / ネットワーク確認

## 実装メモ

- 差分検出は `diff -rq` + grep 除外で行う。robocopy `/L /MIR` と機能的に等価（`__pycache__`, `.bootstrap-bak-*`, `.migrate-pending-*`, `*.bak_*`, `*.pyc`, `.deepseek_usage_session.json` を除外）
- `cygpath -u` が利用できない環境向けに sed フォールバックを用意
- PowerShell ツールが exit code 1 で出力を返さない環境（Claude Code の特定セッション）が報告されているため、本スキルは Bash 単独運用に統一
