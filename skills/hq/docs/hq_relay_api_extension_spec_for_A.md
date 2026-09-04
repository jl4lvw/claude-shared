# 依頼: `/claude-sessions` に司令塔(HQ)拡張項目を追加してほしい

依頼元: TK / 宛先: A（中継API 041 の実装担当）
起票日: 2026-09-04
関連: 2026-09-01 起票「Claude Codeセッション死活監視 + 自動remote-control起動 + 管理PWA」(A側実装済みの `/claude-sessions` を前提にする)

## 背景

TK側で「司令塔セッション」`/hq` を作った。複数の Claude Code セッションを並行運用していると
「どのセッションが確認待ちで止まっているか」を見失うため、各セッションのフックが
状態(作業中 / 許可待ち / 質問待ち / 回答待ち / 完了)をローカルのボードに書き、
`/hq` がそれを一覧して「何で止まっているか・次の一手」を出す仕組み。

TK と A の両PCのセッションを同じ状況板に載せたい。A側が実装済みの `/claude-sessions`
(登録・ハートビート・終了・一覧、A-desktop から 122 件登録済みを確認) にそのまま乗り、
登録本文に **HQ 拡張項目 `hq`** を同梱する形にした。**TK側の送信は 2026-09-04 から始めている**。
サーバーが `hq` を未知項目として捨てる/422 で拒否する間は、TK側クライアントが自動で
`hq` 無しにフォールバックするので、既存の死活監視は壊れない。

## 依頼内容 (優先順)

### 1. `ClaudeSessionRegister` に任意項目 `hq` (JSON object) を追加し、保存して GET で返す

`PUT /claude-sessions/{session_id}` の本文例 (TK側が現在送っているもの):

```json
{
  "pc_label": "TK-desktop",
  "cwd": "C:\\ClaudeCode\\900.ClaudeCode",
  "hq": {
    "schema": 1,
    "status": "waiting_answer",
    "reason": "応答が質問で終了",
    "since": "2026-09-04T22:31:05+09:00",
    "title": "■外注手配",
    "last_prompt": "刺繍依頼書を作って",
    "last_assistant_tail": "納期はどれにしますか。 1. 9/10 2. 9/17 番号で教えてください。",
    "question": null,
    "pending_tool": {"name": "Bash", "detail": "git push origin master", "at": "2026-09-04T22:30:58+09:00"},
    "permission_mode": "auto",
    "updated_at": "2026-09-04T22:31:05+09:00",
    "last_event": "Stop"
  }
}
```

- `hq.status` の取りうる値: `running` / `waiting_permission` / `waiting_question` / `waiting_answer` / `idle` / `done` / `ended`
- `hq.since` は **その状態になった時刻** (司令塔は「止まっている時間が長い順」に並べる)
- 文字列項目はすべて TK側で伏字化済み (PIN / パスワード / API キーは送らない)。長さは 300 字以内
- `hq` は **丸ごと JSON として保存・返却** してほしい (項目を個別カラムに分解しなくてよい。将来 `schema` を上げて項目を足す)
- `GET /claude-sessions` の各行に `hq` をそのまま付けて返す (無い行は `null`)
- `hq` が無い PUT (従来の死活監視フック) は従来どおり動くこと。**`hq` 有りでも 422 にしない**ことが最重要

### 2. `GET /claude-sessions` に `hq_status` 絞り込み (任意)

`?hq_status=waiting_permission,waiting_question,waiting_answer` のようにカンマ区切りで受けられると
司令塔の見張り番 (数分おきのポーリング) が軽くなる。無ければ TK側で全件取得して絞るので必須ではない。

### 3. 一覧の閲覧範囲の制限 (推奨)

`hq.last_assistant_tail` 等の抜粋が入るため、`GET /claude-sessions` は
**呼び出し元の actor 自身の行 + fleet(運用管理者 A) だけ全件** に制限してほしい
(`/mobile/terminals` と同じ考え方)。現状 TK の鍵で A-desktop の全行が見えているので、
B / RC の鍵でも同様に見えるなら絞ってほしい。

### 4. A側の登録フックを `hq_board.py` に置き換え (A側の作業)

TK側のフック `.claude/hooks/hq_board.py` (+ `hq_push.py` / `hq_relay.py`) は
**死活監視のハートビート (SessionStart / Stop / SessionEnd の PUT・POST /end) を内包している**。
`/g-ul` → `/g-dl` で A側に届いたら、A側の `settings.local.json` で
`session_watchdog_hook.py` の登録を `hq_board.py` に置き換えると、A-desktop の行にも `hq` が乗る。

登録するイベント (すべて `python "$CLAUDE_PROJECT_DIR/.claude/hooks/hq_board.py"`、timeout 5):
`Stop` / `SessionStart` / `SessionEnd` / `UserPromptSubmit` / `PreToolUse` / `PostToolUse` / `Notification` / `PermissionRequest`

- 中継APIの設定は既存の `relay_local/.env` (RELAY_BASE_URL / RELAY_API_KEY / RELAY_SELF_USER_ID) をそのまま使う
- PC 名は `RELAY_SELF_USER_ID` + `-desktop` (= `A-desktop`)。変えたければ環境変数 `CLAUDE_HQ_PC_LABEL`
- ローカルボードは `C:\ClaudeCode\.hq\board\` (Git 管理外)。送信を止めるときは `CLAUDE_HQ_NO_PUSH=1`
- 送信はフックから切り離した別プロセスで行うので、ツール実行や応答停止は待たされない
- **フック側で `session_watchdog_hook.py` の役割 (heartbeat) と重複しないよう、置き換え (併存させない)**

### 5. 058 PWA に HQ 状態列 (任意)

一覧に `hq.status` (許可待ち/質問待ち/回答待ち) と `hq.since` からの経過を出せると、
スマホからも「どのPCのどのセッションが止まっているか」が分かる。

## 互換性・注意

- `hq` は任意項目。無い PUT は従来どおり。既存の PWA / watchdog は影響を受けない
- TK側は `hq` 付き PUT が 422 なら `hq` 無しで再送し、6 時間は `hq` を付けない。
  A側が対応したら **TK側の `C:\ClaudeCode\.hq\hq_rejected.json` を消す** か 6 時間待てば `hq` 付きに戻る
- A側の `/claude-sessions` 実装ソースは TK 端末に無いため、この依頼は仕様のみ。実装・再起動は A側で

## 受け入れ確認 (TK側で行う)

1. `PUT /claude-sessions/<id>` に `hq` を付けて 200 が返る
2. `GET /claude-sessions` の当該行に `hq` が入っている
3. TK の `/hq` 状況板に A-desktop の行が「💬回答待ち」等の状態付きで並ぶ
