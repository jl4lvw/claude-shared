---
name: handoff
description: セッション引継ぎスキル。コンテキストが重くなったときに現在の作業状態を **固定パス `C:\ClaudeCode\.handoff\`** へMarkdown保存し、次セッションで復元できるようにする。`/handoff save` で保存（3桁ID発行）、`/handoff load [NNN]` で読込、`/handoff list` で一覧。保存先・読込先はプロジェクトを問わず常に固定。
trigger: ユーザーが /handoff を実行したとき
---

# セッション引継ぎスキル

## 概要
長時間作業や次セッションまたぎのために、**現在の作業状態を固定パス `C:\ClaudeCode\.handoff\` に保存**する。
memory（永続的な好み・事実）とは棲み分け、**進行中タスクの state snapshot** を扱う。

**厳守**: 保存先・読込先はどのプロジェクト・どの cwd から呼んでも常に `C:\ClaudeCode\.handoff\` 固定。作業フォルダ判定や AskUserQuestion での確認は行わない。サブプロジェクトの `.handoff/` には保存しない (見落としと分断の原因になるため)。

各 handoff には **3 桁の ID（000〜999）** が割り振られ、ファイル名と Markdown 冒頭に記録される。
load 時にこの ID を渡すと特定の handoff をピンポイントで復元できる。

## サブコマンド（引数で分岐）

| 引数 | 動作 |
|---|---|
| `save` | 現在の作業状態を `.handoff/SESSION_YYYYMMDD_HHMM_NNN.md` に保存（NNN は 3 桁 ID） |
| `load` | 最新のhandoffファイルを読み込んで要約表示 |
| `load NNN` | ID が NNN（**3桁完全一致のみ**）の handoff を読み込んで要約表示 |
| `list` | `.handoff/` 内のhandoffファイル一覧（ID付き）を表示 |

引数なしの挙動:
- **新セッション**（会話の冒頭、作業文脈がない）→ `load` として自動実行
- **作業中**（すでに作業文脈がある）→ AskUserQuestion で「save / load / list のどれを実行しますか？」と確認してから実行する

引数が **ちょうど 3 桁の数字**（正規表現 `^\d{3}$`）の場合のみ `load NNN` として扱う。1〜2 桁や 4 桁以上は誤入力として扱い、`list` を案内する（前方一致は誤ヒットの温床になるため**採用しない**）。

## ファイル名共通フォーマット（重要）

**正規表現**: `^SESSION_(\d{8})_(\d{4})(?:_(\d{3}))?\.md$`

- グループ 1: `YYYYMMDD`（必須）
- グループ 2: `HHMM`（必須）
- グループ 3: `NNN`（旧形式では空）

list の日時表示・load の最新判定・save の重複チェックは **すべてこの正規表現でパース**し、`(YYYYMMDD, HHMM)` で昇順ソート（旧形式 = ID 無し も同じソートキーで比較できる）。`rsplit('_', 1)` のような曖昧な分割は使わない（`_427_backup` など将来の拡張命名で誤判定する）。

## 保存先ルール

- **保存先は `C:\ClaudeCode\.handoff\` 固定**。cwd / プロジェクトに依らず常にこのパスを使う
- ディレクトリが無ければ作成 (`Path(r'C:/ClaudeCode/.handoff').mkdir(exist_ok=True)`)
- ファイル名: `SESSION_YYYYMMDD_HHMM_NNN.md`
  - 日時は必ず `date` コマンドで実時刻を取得
  - `NNN` は 000〜999 の 3 桁 ID（既存ファイルと重複しないランダム値）
- **禁止**: サブプロジェクトの `<proj>/.handoff/` への保存・読込 (固定パスに統一)
- 過去にサブプロジェクト配下に保存された旧 handoff (例: `inventory-replenishment-pwa/.handoff/`) は **load 対象外**。必要なら手動で `C:\ClaudeCode\.handoff\` に移動して使う

## 3 桁 ID の発番ルール

- 範囲: `000` 〜 `999`（ゼロパディング 3 桁固定）
- **重複禁止**: 既存 `.handoff/SESSION_*_NNN.md` の ID を全部抽出して、未使用値の中から選ぶ
- **ランダム**: 連番にしない（`random.choice(pool)` で十分。暗号学的乱数は不要）
- **TOCTOU 対策**: 候補 ID 選定後、**ファイル作成時に同名存在を再チェックして衝突したら別 ID で 1 回リトライ**（並行 save 時の race を実質防止）
- **1000 件埋まり時**: エラーで停止し、ユーザーに `/handoff list` と古い handoff の削除を案内

### 発番サンプル（Python・推奨実装）

ファイル名パースは前述の正規表現を使う。`rsplit('_', 1)` は使わない。

```python
import random
import re
from pathlib import Path

NAME_RE = re.compile(r"^SESSION_(\d{8})_(\d{4})(?:_(\d{3}))?\.md$")

def collect_used_ids(handoff_dir: Path) -> set[str]:
    used: set[str] = set()
    for p in handoff_dir.glob("SESSION_*.md"):
        m = NAME_RE.match(p.name)
        if m and m.group(3):
            used.add(m.group(3))
    return used

def issue_new_id(handoff_dir: Path) -> str:
    used = collect_used_ids(handoff_dir)
    pool = [f"{i:03d}" for i in range(1000) if f"{i:03d}" not in used]
    if not pool:
        raise RuntimeError(
            ".handoff が 1000 件埋まっています。/handoff list で古いものを確認・削除してください。"
        )
    return random.choice(pool)

handoff_dir = Path(".handoff")
handoff_dir.mkdir(exist_ok=True)
new_id = issue_new_id(handoff_dir)
# print(new_id)
```

### 保存時の TOCTOU 対策

候補 ID を取得しても保存直前に並行 save が同じ ID を取りに来る可能性があるため、**ファイル作成は排他モードで**行う:

```python
from datetime import datetime

stamp = datetime.now().strftime("%Y%m%d_%H%M")
target = handoff_dir / f"SESSION_{stamp}_{new_id}.md"

# 排他作成（既存なら FileExistsError）。失敗したら ID を採り直して 1 回だけリトライ。
for attempt in range(2):
    try:
        with open(target, "x", encoding="utf-8") as f:
            f.write(content)
        break
    except FileExistsError:
        if attempt == 1:
            raise
        new_id = issue_new_id(handoff_dir)
        target = handoff_dir / f"SESSION_{stamp}_{new_id}.md"
```

## 保存フロー（save）

### Step 1: 保存先確定 (固定)
保存先は **`C:\ClaudeCode\.handoff\` 固定**。会話コンテキスト判定・AskUserQuestion 確認は **不要かつ禁止**。
`Path(r'C:/ClaudeCode/.handoff').mkdir(exist_ok=True)` でディレクトリ確保のみ実施。

### Step 2: 日時取得
```bash
date +"%Y%m%d_%H%M"
```
**厳守**: 手入力禁止。必ず `date` コマンドで取得すること。

### Step 3: 3 桁 ID 発番
上記「発番サンプル」を実行して未使用の `NNN` を 1 つ取得する。

### Step 4: テンプレート埋めて保存

ファイル名: `SESSION_{YYYYMMDD_HHMM}_{NNN}.md`

```markdown
# セッション引継ぎ — {YYYY-MM-DD HH:MM} — ID: {NNN}

## 🎯 作業のゴール
（このセッションで達成しようとしていること／達成したいこと）

## ✅ やったこと
- …

## 🚧 未完了タスク
- [ ] …

## 👉 次にやること（優先順）
1. …

## 🧠 重要な判断と理由
- 判断: …
  - 理由: …

## 📂 触ったファイル
- `path/to/file.py` — 何を変えたか

## 🔍 動作確認状況
- [ ] 構文チェック: OK / NG / 未
- [ ] import チェック: OK / NG / 未
- [ ] 実動作確認: OK / NG / 未

## ⚠️ ハマりポイント・注意
- …

## 💬 次セッションへのメモ
（次のClaudeが読んだら真っ先に知るべきこと）
```

### Step 5: 保存完了メッセージ
保存パスと **発番した 3 桁 ID** を表示し、次セッションで使う `/handoff load NNN` を **Claude Code UI のコピーボタンで一発取得できるよう、単独のフェンスドコードブロック** で出力する。

**厳守**: `/handoff load NNN` の行は説明文・矢印・絵文字などと同じコードブロックに入れない。コードブロック右上のコピーボタンはブロック全体をコピーするため、**ロードコマンドだけが入った 1 行のコードブロック** にする。これを怠るとユーザーが結局範囲選択するハメになり、本スキルの価値が損なわれる。

例（このフォーマットでそのまま出力する）:

````markdown
保存しました: `.handoff/SESSION_20260506_1430_427.md`
ID: **427**

次セッションでこれを実行:

```
/handoff load 427
```
````

## 読込フロー（load）

### Step 1: 読込先確定 (固定)
読込先は **`C:\ClaudeCode\.handoff\` 固定**。save と同じく、判定なし。

### Step 2: 対象ファイル特定

`.handoff/SESSION_*.md` を Glob し、共通正規表現でパースしてから判定する:

- **引数なし** → 全件を `(YYYYMMDD, HHMM)` で降順ソートし、先頭を選ぶ（旧形式・新形式の混在でも日時で正しく最新が決まる）
- **引数が `^\d{3}$`（例: `load 427`）** → ID が完全一致するエントリを抽出
  - **0 件**: 「ID 427 の handoff は見つかりません」と告げて `list` 結果を表示
  - **1 件**: そのままそれを読み込む
  - **2 件以上**（通常起きないが、過去エクスポートの再配置等で発生し得る）: 候補に **連番 `[1]` `[2]` ...** を振り、`日時 / タイトル / フルファイル名` を表示。「番号で選んでください」と聞き、ユーザーが返した連番で確定
- **引数が 3 桁以外の数字や混在文字** → 誤入力扱いで `list` を案内（前方一致は採用しない）
- **旧フォーマット（ID 無し）の特定読込**: load NNN では引けないため、`list` で日時を見て対象を選ぶよう案内

### Step 3: 読み込み＆要約
Read で読み込み、以下を箇条書きで表示:
- ID（旧形式は「ID なし（旧形式）」）と保存日時
- 前回のゴール
- 未完了タスク
- 次にやること
- 注意事項

その上で「この続きから進めますか？」と確認。

## 一覧フロー（list）

`.handoff/SESSION_*.md` を Glob → 共通正規表現で `(YYYYMMDD, HHMM, NNN?)` を抽出 → `(YYYYMMDD, HHMM)` 降順ソートして 1 行ずつ表示:

```
ID    日時              タイトル
427   2026-05-06 14:30  PWA 同期機能 — UI 実装途中
312   2026-05-05 22:08  在庫CSV取込スクリプト
---   2026-05-04 10:15  （旧フォーマット, ID無し）
```

- ID が無い旧ファイルは `---` で表示
- 「タイトル」は Markdown 1 行目（`# セッション引継ぎ — ...`）から抽出
- 正規表現に**マッチしない壊れたファイル名**はリスト末尾に「⚠️ 不明形式」として表示し、無視はしない

## memory との棲み分け

| | memory | handoff |
|---|---|---|
| 対象 | 永続的な好み・事実・ルール | 進行中作業のstate snapshot |
| 寿命 | 長期 | セッション間の短期 |
| 保存先 | `~/.claude/.../memory/` | `C:\ClaudeCode\.handoff\` (固定) |
| 例 | 「デプロイ前に構文チェック」 | 「今PWA同期機能の途中、UIだけ残」 |

**handoffに書くべきでないもの**（memoryへ）: ユーザーの恒常的な好み、プロジェクト不変の事実、ルール。
**memoryに書くべきでないもの**（handoffへ）: 今日の作業進捗、一時的なTODO、次セッションの申し送り。

## 注意事項

- 日本語パスがあるので Write ツールで失敗する場合は CLAUDE.md のルールに従い Python 経由で書き込む
- ファイルは `encoding="utf-8"` 明示
- `.handoff/` は基本 `.gitignore` に入れる想定（ユーザーが共有したい場合は別途判断）
