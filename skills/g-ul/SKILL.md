---
name: g-ul
description: プロジェクトの `.claude/{skills,commands,tools,rules,memory}` を `%USERPROFILE%\claude-shared\` にミラーコピーしてから Git push する。Option C ミラー方式。コミットメッセージは引数任意（未指定時は自動生成）。
---

# /g-ul — claude-shared upload (mirror approach)

プロジェクトの `.claude/{skills,commands,tools,rules,memory}` を `%USERPROFILE%\claude-shared\` にミラーコピー (`robocopy //MIR`) してから Git に push する。

**Option C ミラー方式**: ジャンクションを使わない。OneDrive が `.claude/` をジャンクション越しに破壊する事故を構造的に回避。

## 使い方

```
/g-ul                                   # 自動メッセージで push
/g-ul "skill: refactor handoff"         # メッセージ指定で push
```

## 手順

### Step 1: 引数からコミットメッセージを取り出す

ユーザー引数があればそれをメッセージに使う。未指定なら `sync: <hostname> <yyyy-MM-dd HH:mm>` を自動生成。

### Step 2: Bash で実行

**Bash ツール**を使うこと（git-bash 経由）。

過去に PowerShell ツール経由で exit code 1 / 出力なしで失敗する環境が確認されたため、本スキルは Bash 単独運用に統一している。

`<USER_ARGS_OR_EMPTY>` プレースホルダは、Claude が引数の有無で置換:
- 引数あり → そのテキストを埋め込む（シングルクォート内で `'` は `'\''` にエスケープ）
- 引数なし → **必ず空文字列 `''` に置換**

```bash
# 注意: set -e は使わない。robocopy は「成功」でも非ゼロ終了する（0-7=ok, 8+=error）ため、
# set -e があると正常コピー直後に中断する。各ステップで明示的に終了コードを判定する。

# プロジェクトルート（CWD = .claude の親想定）
CLAUDE_DIR="$(pwd)/.claude"
if [ ! -d "$CLAUDE_DIR" ]; then
    echo "CWD に .claude/ が見つかりません: $(pwd)" >&2
    exit 1
fi

# claude-shared 位置（Bash 形式と Windows 形式の両方持つ）
SHARED_WIN_RAW="$USERPROFILE/claude-shared"
SHARED_BASH=$(cygpath -u "$SHARED_WIN_RAW" 2>/dev/null || echo "${SHARED_WIN_RAW//\\//}" | sed 's|^C:|/c|')
if [ ! -d "$SHARED_BASH" ]; then
    echo "claude-shared not found: $SHARED_BASH (新PC は git clone が必要)" >&2
    exit 1
fi
TARGETS="skills commands tools rules memory"

echo "ClaudeDir: $CLAUDE_DIR"
echo "Shared:    $SHARED_BASH"

# Sanity: .claude/skills と claude-shared/skills のファイル数を比較
# .claude が極端に少ない場合は abort（OneDrive 同期途中などで全消し事故防止）
SRC_SKILLS="$CLAUDE_DIR/skills"
DST_SKILLS="$SHARED_BASH/skills"
if [ -d "$SRC_SKILLS" ] && [ -d "$DST_SKILLS" ]; then
    SRC_COUNT=$(find "$SRC_SKILLS" -type f 2>/dev/null | wc -l)
    DST_COUNT=$(find "$DST_SKILLS" -type f 2>/dev/null | wc -l)
    if [ "$DST_COUNT" -gt 5 ] && [ "$SRC_COUNT" -lt $((DST_COUNT / 2)) ]; then
        echo ""
        echo "Sanity check FAIL: .claude/skills=$SRC_COUNT files but claude-shared/skills=$DST_COUNT" >&2
        echo "ミラーすると大量削除になります。OneDrive 同期途中？ 中断します。" >&2
        exit 1
    fi
fi

# Step A: .claude/* -> claude-shared/* ミラー
echo ""
echo "=== Mirror .claude/ -> claude-shared/ ==="
for t in $TARGETS; do
    SRC_BASH="$CLAUDE_DIR/$t"
    DST_BASH="$SHARED_BASH/$t"
    if [ ! -d "$SRC_BASH" ]; then
        echo "  $t : .claude/ 側になし、skip"
        continue
    fi
    # robocopy に渡すパスは Windows 形式 (C:\...) に変換する
    SRC_W=$(cygpath -w "$SRC_BASH")
    DST_W=$(cygpath -w "$DST_BASH")
    echo "  mirror $t ..."
    # git-bash では robocopy のスラッシュオプションが MSYS パス変換に巻き込まれるため //OPT で escape
    robocopy "$SRC_W" "$DST_W" //MIR //NFL //NDL //NP //R:2 //W:1 \
        //XD __pycache__ ".bootstrap-bak-*" ".migrate-pending-*" \
        //XF "*.bak_*" "*.pyc" ".deepseek_usage_session.json" > /dev/null 2>&1
    EXIT=$?
    # robocopy 終了コード: 0-7 = 成功（差分の有無）、8+ = エラー
    if [ $EXIT -ge 8 ]; then
        echo "    robocopy ERROR (exit=$EXIT) for $t" >&2
        exit 1
    fi
done
echo "  mirror done."

# Step B: claude-shared 側で git add / commit / push
cd "$SHARED_BASH" || exit 1

REMOTE_URL=$(git remote get-url origin 2>/dev/null)
if [ -z "$REMOTE_URL" ]; then
    echo "origin remote 未設定" >&2
    exit 1
fi
echo ""
echo "remote: $REMOTE_URL"

STATUS=$(git status --porcelain)
if [ -z "$STATUS" ]; then
    echo "変更なし。push 不要。"
    exit 0
fi

echo ""
echo "=== 変更ファイル ==="
git status --short

git add -A

echo ""
echo "=== 差分サマリ ==="
git diff --cached --stat

# コミットメッセージ
USER_MSG='<USER_ARGS_OR_EMPTY>'
if [ "$USER_MSG" = '<USER_ARGS_OR_EMPTY>' ] || [ -z "$USER_MSG" ]; then
    STAMP=$(date +"%Y-%m-%d %H:%M")
    HOST=$(hostname)
    MSG="sync: $HOST $STAMP"
else
    MSG="$USER_MSG"
fi
echo ""
echo "=== commit ==="
echo "message: $MSG"
git commit -m "$MSG"

UPSTREAM=$(git rev-parse --abbrev-ref --symbolic-full-name '@{u}' 2>/dev/null)
echo ""
echo "=== push ==="
if [ -z "$UPSTREAM" ]; then
    BRANCH=$(git branch --show-current)
    echo "upstream 未設定。--set-upstream で push ($BRANCH)"
    git push --set-upstream origin "$BRANCH"
else
    git push
fi

echo ""
echo "OK: pushed."
echo ""
echo "=== 最終 HEAD ==="
git log -1 --format='%h %ad %s' --date=short HEAD
```

### Step 3: 完了報告

push の commit hash と short stat を 1〜2 行で報告。

## エラー時

- `CWD に .claude/ が見つかりません` → プロジェクトルートで実行する
- `claude-shared not found` → 新PC は `git clone https://github.com/jl4lvw/claude-shared.git "$USERPROFILE/claude-shared"`
- `Sanity check FAIL` → .claude/ が極端に少ない（OneDrive 同期途中の可能性）。少し待って再試行
- `robocopy ERROR (exit>=8)` → ファイルロック / 権限。Claude Code を完全終了して再試行
- `git push failed` → 認証エラー。`git remote -v` で確認

## 実装メモ

- git-bash 環境では `robocopy /MIR` を呼ぶと MSYS パス変換で `/MIR` → `C:/Program Files/Git/MIR` に化けて exit 16 で失敗する
- 対策: すべてのスラッシュオプションを `//MIR //NFL //XD ...` のダブルスラッシュで escape する
- パスは `cygpath -w` で Windows 形式（`C:\path\to`）に変換してから robocopy に渡す
- 過去に PowerShell ツールが exit code 1 で出力ゼロになる環境が確認されたため、本スキルは Bash 単独運用に統一
