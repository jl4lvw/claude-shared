# cgd 軽量モード レビュー — `fetch_user` 戻り値変更案

- 対象: `def fetch_user(id: int) -> dict | None:` → `def fetch_user(id: int) -> dict:`（未検出時は `{}` を返す）
- 動機: 呼び出し側の `None` チェック忘れによる `AttributeError` 多発
- モード: 1（軽量・C+G 並列）
- Codex reasoning: medium / Gemini 観点: 比較評価
- 実行: `codex exec`（gpt-5.3-codex, sandbox=read-only） + `gemini -p` を `C:/tmp-ai` から並列起動、両者とも正常終了

---

## 総評

`None` を `{}` に置き換える案は短期的に `AttributeError` を抑える効果はあるものの、**「未検出」と「空データ」の区別が消える**ため Codex / Gemini ともに非推奨で一致。表面的な症状を `KeyError` やサイレント失敗に置き換えるだけで、根本原因（呼び出し規律と契約の曖昧さ）の解決にならない。推奨は **Optional 維持＋型チェッカ厳格化** または **`get_user`(Optional)/`require_user`(例外) の二系統 API** へ。

---

## 5列統合表

| 指摘（重大度＋根拠1行） | Codex | Gemini | Claude採用 | 対応 |
|---|---|---|---|---|
| 🔴 「未検出」と「空データ」の意味論が衝突 — 同じ `{}` でドメイン区別が消える | ✓ 明示 | ✓ 明示（KeyError温床） | ✅採用 | 提案案を不採用にする主因。意味論を分離する設計へ |
| 🔴 既存呼び出し側への破壊的変更 — `is None` 判定が静かに壊れる | ✓ 明示 | ✓ 短評（破壊的変更注意） | ✅採用 | 全呼び出し箇所を grep し、移行計画と段階的リリースを必須化 |
| 🟠 Truthy/Falsy 判定の罠 — `if user:` が「未検出」と「空dict」両方で偽 | ✓ 明示 | — | ✅採用 | `if user is None` 系の意図を持つ既存コードを洗い出して修正 |
| 🟠 型安全性は見かけ上改善・実質後退 — 未検出ケースが型で表現できなくなる | ✓ 明示 | ✓ 暗黙（A案推奨で言及） | ✅採用 | mypy/pyright strict 化で Optional 維持し型で強制する方が筋が良い |
| 🟠 可観測性低下 — `{}` は通常値に紛れて未検出メトリクスが取りづらい | ✓ 明示 | — | 🔄部分採用 | もし採用するなら未検出時のログ/メトリクスを必ず追加 |
| 🟠 推奨代替「例外送出 (`UserNotFound`)」 — Fail Fast で異常系を強制意識 | ✓ 代替案#2 | ✓ 第一推奨 | ✅採用 | ID指定取得で「在ること前提」の API は例外送出が最も安全 |
| 🟠 推奨代替「Optional 維持＋型チェック厳格化」 — Pythonic で破壊的変更ゼロ | ✓ 代替案#1 | ✓ 案A | ✅採用 | 第一選択。mypy strict + ガード関数 / `assert is not None` パターン |
| 🟡 推奨代替「`get_user`/`require_user` 二系統 API」 — 用途で選択可能 | ✓ 代替案#2 | — | 🔄部分採用 | 大規模移行が必要なときの折衷案として有効 |
| 🟡 Result/Maybe 型 — 大規模・堅牢性重視なら有効 | ✓ 提示 | ✓ 提示 | ⏭️スキップ | 学習コスト高。今回の規模では過剰 |
| 🟡 短期的メリット（AttributeError 減少）はある — ただし根治ではない | ✓ 明示 | ✓ 明示 | ✅採用 | 動機自体は正当。解決手段を Optional+strict 型 or 例外に振り替える |

---

## 次アクション

1. **提案された `dict | None` → `dict`（`{}` 返却）への変更は採用しない**。Codex / Gemini の意見が一致し、サイレント失敗のリスクが大きい。
2. **第一選択: `Optional[dict]` を維持しつつ型チェッカを厳格化**
   - `mypy --strict` または `pyright` strict を CI に組み込み、`None` チェック漏れを静的に検出
   - 共通ガード関数（`def required(user: dict | None) -> dict`）を導入し、必須箇所では明示的に剥がす
   - lint ルール（Optional 取り扱い系）を追加
3. **代替: ID指定取得で「在ること前提」の呼び出しが多いなら例外送出版を追加**
   - `def get_user(id: int) -> dict | None:` を残しつつ `def require_user(id: int) -> dict:` を新設し `UserNotFoundError` を送出
   - 既存呼び出しを用途別に書き換え（探索系→`get_user`、必須系→`require_user`）
4. **どうしても `{}` 返却に進める場合のガード**（非推奨だが採用時の最低条件）
   - 未検出時は `logger.warning("user not found: id=%s", id)` を必ず出す
   - `TypedDict` で「空dictは未検出を意味する」契約をドキュメント化
   - 全呼び出し箇所を監査し、`if not user:` の Truthy 判定を `if user == {}:` 等に置換するか禁止
   - メジャーバージョン更新と移行ガイドを必須化
5. **再発防止**: 元の動機（`None` チェック忘れ）に対しては型レベルでの強制が本筋。コードレビュー文化と CI の型ゲートで担保する。

---

## 付録: 各エージェント原文（抜粋）

### Codex（gpt-5.3-codex, reasoning=medium）
- 🔴重大: `None` を `{}` に置き換えるのは意味論が曖昧
- 🔴重大: 破壊的変更（API契約変更）
- 🟠重要: Truthy/Falsy 判定の罠が増える / 型安全性は見かけ上改善だが実質後退 / 可観測性低下
- 推奨: そのまま `{}` 返却は **非推奨**。`Optional` 維持＋型チェック厳格化、または `get_user`/`require_user` 二系統 API

### Gemini（比較評価）
- 結論: 「空辞書 `{}` を返す」案はサイレント失敗のリスクが高く慎重検討必要
- 推奨: **案B（例外 `UserNotFound` 送出）** が第一推奨。「見つからないことが正常系」なら **案A（Optional 維持＋型チェック強制）** が Pythonic でバランス良い
- 重大度: 🟠 重要