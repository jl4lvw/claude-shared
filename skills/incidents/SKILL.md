---
name: incidents
description: 外部AI(Codex/DeepSeek/Qwen/Gemini)と Claude Code ハーネスの不具合を蓄積・解析する台帳スキル。作業中のセッションは気づいた不具合を `incident_log.py add` で1行記録するだけにして先へ進み、後日この専用セッションで台帳と自動テレメトリ(トークン数・出力サイズ)をまとめて読み取り、傾向分析・根本原因の仮説立て・対策提案までを行う。「不具合台帳」「インシデント」「AIの調子が悪い」「Codexのトークンが多い」「まとめて解析」「incidents」などで起動。
---
<!-- SKILL_VERSION: 2026-08-05_211729 -->

# incidents — 外部AI/ハーネス不具合の蓄積と解析

## なぜ必要か（2026-08-05 の実例）

同じ日に独立した 2 セッションが、どちらも「cgd Lv7 を Workflow ではなく inline で実行する」という同じ間違いをした。片方が気づいて対処しても、**もう片方には伝わらない**。Codex が 1 回 8 万トークン使っていた事実も、たまたま人が数えたから判明した。

→ **その場で対処して終わり、にしない。** 気づいた側は記録だけ残し、解析はこのスキルの専用セッションでまとめて行う。

## 2 つの記録先

| ファイル | 誰が書くか | 共有 | 中身 |
|---|---|---|---|
| `.claude/incidents/incidents.jsonl` | **人（Claude）が `incident_log.py add`** | ✅ `/g-ul` で同期 | 事象・再現条件・仮説・証拠パス |
| `.claude/incidents/telemetry.jsonl` | **hook が自動**（`ai_telemetry.py`） | ❌ 端末ローカル | 外部AI呼出のトークン数・出力サイズ・flags |

JSONL を選んだ理由: append なので並行書込・複数拠点マージに強く、git diff が人間可読で、壊れても 1 行単位で復旧できる。

**同期の仕掛け（2026-08-05 追加）**: `incidents` は `/g-ul` `/g-cmp` `verify_sync.py` のミラー対象リストに入っている（`skills commands tools rules memory hooks incidents`）。`telemetry.jsonl` は robocopy の `//XF` と `verify_sync.EXCLUDE_FILES` の両方で除外しているので、端末ローカルに留まる。**この 2 つは必ずセットで維持すること** — 片方だけ直すと「毎回不一致」になって検証が形骸化する。

---

## 記録する（作業中のセッション向け・1 コマンド）

```bash
python "C:/ClaudeCode/.claude/tools/incident_log.py" add \
  --tool codex --category token --severity high \
  --title "Lv7 の Codex が入力 5.4KB で 103,686 tokens 消費" \
  --detail "入力サイズとトークン消費に相関なし。sandbox read-only での repo 探索が主因と推定" \
  --evidence "C:/Users/user/.claude/projects/.../tool-results/xxx.txt"
```

- `--tool`: `codex` / `deepseek` / `qwen` / `gemini` / `harness`（Claude Code 側の挙動） / `claude-code` / `other`
- `--category`: `token` / `output` / `auth` / `empty` / `wrong` / `workflow` / `perf` / `other`
- `--severity`: `low` / `mid` / `high`（既定 mid）

**記録の粒度**: 「もう一度同じことが起きたら判別できるか」で決める。トークン数・出力サイズは hook が自動で持っているので**手写ししない**。書くべきは *条件* と *仮説*。

**記録すべきでないもの**:
- 自分のコードのバグ（それは普通に直す）
- API キー・PIN・パスワードの値（「口頭確認済」等の事実だけ）
- そのセッション限りの状態（→ `/ctx` の文脈台帳へ）

---

## 解析する（専用セッションの手順）

### Step 1: 全体像を掴む

```bash
python "C:/ClaudeCode/.claude/tools/incident_log.py" report
python "C:/ClaudeCode/.claude/tools/incident_log.py" report --since 2026-08-01   # 期間を切る場合
```

台帳（tool別/category別/severity別・未対応 open 一覧）と、テレメトリ（呼出数・flags 集計・codex トークン統計・WF/inline 内訳・消費上位）が 1 画面で出る。

### Step 2: 未対応を読む

```bash
python "C:/ClaudeCode/.claude/tools/incident_log.py" list --status open
python "C:/ClaudeCode/.claude/tools/incident_log.py" show INC-20260805-a3f2
```

### Step 3: 台帳とテレメトリを突き合わせる

report だけで足りない時は生 JSONL を直接読む（`.claude/incidents/*.jsonl`）。有用な切り口:

- **flags で絞る** — `tokens_high`（8万tok超）/ `output_huge`（200KB超）/ `empty_output` / `harness_truncated` / `interrupted`
- **input_bytes と tokens_used の相関を見る** — 相関がなければ膨張は入力側でなく探索側
- **`via_workflow` の比率** — WF 必須のはずのレベルで inline が混ざっていないか
- **時系列** — 特定の日付以降に増えた flags があれば、その日の変更（スキル改修・ライブラリ更新）が疑わしい

### Step 4: 分類して報告する

事象を「**個別の不具合**」「**繰り返す不具合**」「**仕組みの欠陥**」に分ける。3 つ目が最も価値が高い:

> 例: 「Lv7 を inline で回した」は個別ミスに見えるが、2 セッションで再発 + スキル本文の導線が inline に誘導していた → **仕組みの欠陥**。対策は注意喚起ではなく hook による機械的強制（`cgd_wf_gate.py`）。

対策案を出すときは **「文章で注意する」で終わらせない**。同じ失敗が 2 回以上出ているなら、機械的に防げないかを必ず検討する（hook / CLI の既定値 / 選択肢から消す）。

### Step 5: 対応済みにする

```bash
python "C:/ClaudeCode/.claude/tools/incident_log.py" resolve INC-20260805-a3f2 \
  --resolution "cgd_wf_gate.py で inline codex を遮断。SKILL.md の Lv6/Lv7 節を WF 必須に改訂"
```

恒久的な教訓になったものは **memory へ昇格**させる（台帳は「起きたこと」、memory は「今後どうするか」）。逆に memory に書くほどでない一過性の事象は台帳に置いたままでよい。

---

## 注意事項

- **telemetry.jsonl は端末ローカル**。他拠点の実測値は入っていない。拠点をまたぐ傾向を見たい場合は各端末で report を取って持ち寄る
- テレメトリは**出力本文を保存しない**（サイズと抽出値のみ）。生ログはハーネスの `tool-results/` 側にある
- hook が動いていないと telemetry は増えない。`python "C:/ClaudeCode/.claude/tools/install_hooks.py" --check` で登録状況を確認できる（**登録の反映には Claude Code の再起動が必要**）
- 台帳が肥大したら、resolved を別ファイルへ退避してよい（JSONL なので行単位で移せる）
