# claude-shared (Git 共有スキル) — Option C ミラー方式

> このファイルはプロジェクトメモリ（`.claude/memory/`）に置かれ、Git で同期されます。全 PC の Claude Code が同じ情報を参照します。
> 
> 個別 PC のユーザーローカルメモリ（`%USERPROFILE%\.claude\projects\...\memory\MEMORY.md`）にも同じ内容を持たせるのが理想ですが、ユーザーローカルメモリは OneDrive・Git のどちらでも同期されないため、PC ごとの手動更新が必要です。本ファイルはその「Git 経由で確実に届く版」です。

## 経緯（重要）

2026/04 まで `.claude/{skills,commands,tools,rules,memory}` をジャンクションで `claude-shared/...` に張り替える方式（**junction 方式**）を採用していたが、**OneDrive がジャンクション越しに rename/delete を伝播し、claude-shared/skills/ の実体ファイルを物理削除する災害が発生**。  
→ junction 方式を全廃し、`.claude/` を物理コピー、`claude-shared/` を別物理 Git WD として、`/g-ul, /g-dl, /g-cmp` スクリプトが robocopy /MIR でブリッジする **Option C ミラー方式** に移行した。

## アーキテクチャ

```
[OneDrive 同期領域]
900.ClaudeCode\.claude\         ← 全て physical (junction なし)
├── skills\, commands\, tools\, rules\, memory\
├── launch.json, settings.local*.json (PC固有・Git除外)

[Git 同期領域 — OneDrive 外]
%USERPROFILE%\claude-shared\
├── .git\, .gitignore
├── skills\, commands\, tools\, rules\, memory\

[ブリッジ]
/g-ul: .claude/* → claude-shared/* (robocopy /MIR) → git push
/g-dl: git pull → claude-shared/* → .claude/* (robocopy /MIR)
/g-cmp: 三方比較 (.claude/ ↔ claude-shared/ ↔ origin/main, 読み取り専用)
```

## 同期コマンド

| コマンド | 動作 |
|---|---|
| `/g-ul "msg"` | `.claude/* → claude-shared/* → git commit → git push`。msg 任意（未指定で `sync: <PC> <date>` 自動生成） |
| `/g-dl` | `git fetch → git pull --ff-only → claude-shared/* → .claude/*`。OneDrive の conflict copy を /MIR で自動掃除 |
| `/g-cmp` | 3 階層差分表示。読み取り専用、いかなる変更も加えない |

## 新 PC セットアップ（現行手順）

```powershell
# 1. PowerShell 7+ 必須
winget install --id Microsoft.PowerShell --source winget

# 2. Git for Windows をインストール（Git Credential Manager 推奨）

# 3. claude-shared を clone（OneDrive 外、ユーザーホーム直下）
git clone https://github.com/jl4lvw/claude-shared.git $env:USERPROFILE\claude-shared

# 4. safe.directory を設定（"dubious ownership" エラー回避）
git config --global --add safe.directory ($env:USERPROFILE -replace '\\','/').TrimEnd('/') + '/claude-shared'

# 5. claude-shared/* → .claude/* に反転ミラー（初回のみ）
cd "$HOME\OneDrive\デスクトップ\0.フジ\900.ClaudeCode"
$Targets = @('skills','commands','tools','rules','memory')
foreach ($t in $Targets) {
    $src = Join-Path $env:USERPROFILE "claude-shared\$t"
    $dst = Join-Path "$PWD\.claude" $t
    if (Test-Path -LiteralPath $src) {
        & robocopy $src $dst /MIR /NFL /NDL /NP /R:2 /W:1 /XD __pycache__ /XF '*.bak_*' '*.pyc' | Out-Null
    }
}

# 6. Claude Code 起動 → /skills 確認 → /g-cmp で完全同期確認
```

## 重要な禁則事項

- 🚫 **`_claude-shared/migrate.ps1`, `bootstrap.ps1` 実行禁止** — junction 方式の旧スクリプト。災害再発の可能性あり。`-IKnowThisIsObsolete` ガード付き
- 🚫 **`.claude/` 配下に junction を作らない** — OneDrive が junction 越しに claude-shared/ を破壊する
- ✅ /g-ul には sanity check（`.claude/skills/` ファイル数が claude-shared 側の半分未満なら abort）

## トラブル対応

### conflict copy 発生（`SKILL-DESKTOP-XXX.md` 等）
→ `/g-dl` で claude-shared 側を正として /MIR、conflict copy が自動削除される

### `dubious ownership` エラー
```powershell
git config --global --add safe.directory C:/Users/<実ユーザー名>/claude-shared
```

### diverged（両 PC で同時 push）
`/g-dl` がガイダンス表示。安全な順で対処：
1. `git stash push -m "pre-gdl" ; git pull --ff-only ; git stash pop`
2. `git branch backup-...` 退避 → `git pull --rebase`
3. 最終手段: `git reset --hard origin/main`（コミット未push分は失われる）

## 関連ファイル

- `_claude-shared/README.md` — 詳細ドキュメント（OneDrive 同期で全PC 参照可）
- `claude-shared/skills/g-ul/SKILL.md` — push スクリプト
- `claude-shared/skills/g-dl/SKILL.md` — pull スクリプト
- `claude-shared/skills/g-cmp/SKILL.md` — 三方比較スクリプト
