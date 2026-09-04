---
name: wear-image
description: Tシャツ等の平置き写真からモデル着用イメージを作る作業の標準手順。Gemini Web UI(Pro契約、gemini.google.com)をユーザーが手動操作し、Claudeは素材整理・英語プロンプト作成・Downloadsへの3点セット出力・結果記録を担当する(API自動化・ブラウザ自動操作はしない)。「着用イメージ」「着用画像」「モデル合成」「Geminiに着せて」「wear-image」で起動。
trigger: 商品の平置き写真を渡されて着用イメージ(モデル着用合成画像)を作りたいとき。Gemini貼り付け用プロンプトを作ってほしいとき。生成結果を記録してほしいとき。
---

<!-- SKILL_VERSION: 2026-09-04_170000 -->

# wear-image — 着用イメージ作成(Gemini Web UI 手動運用)

作業フォルダ: `046.商品着用イメージ作成/`(プロジェクト直下)。
ユーザーが Gemini Web UI を操作し、Claude はそれ以外を全部やる。**Claude in Chrome / Browser pane で Gemini を代行操作しない**(明示依頼時のみ)。

## 入力(ユーザーから受け取るもの)
1. 平置き写真(Downloads のパス、またはチャット添付。添付だけの場合はパスを依頼する)
2. 品番(G番号)と商品ページURL(あればWebFetchでボディ素材・カラー・プリント説明を取得)
3. 着用条件の変更点(既定: 短髪男性・背面・直立・スタジオ背景・**茶色系チノパン**。2026-09-04以降デニムは使わない)

## 手順
1. `046.商品着用イメージ作成/<品番>_<名前>/{input,reference,prompts,output}` を作る(初回のみ)。
   `input/` に平置き写真をコピー、`reference/` に共通ポーズ写真 `046.../reference/G2190_02_back_pose.jpg` をコピー。
2. 平置き写真を Read で確認し、英語プロンプトを書く(下の骨格を使う)。`prompts/<名前>_prompt.md` に保存。
   同一デザインの色違いは既存 md を複製してプリント色の記述だけ変える。
3. **Downloads へ3点セットを出力**(必須。ユーザーは最新3件をまとめて Gemini にドラッグ&ドロップする):
   ```
   python .claude/skills/wear-image/scripts/prepare_set.py <reference/ポーズ.jpg> <input/平置き.jpg> <prompts/xxx_prompt.md>
   ```
   → `YYYYMMDD_HHMM_01_..jpg` / `_02_..jpg` / `_03_..md` の3ファイルが Downloads に並ぶ。応答にこの3ファイル名と、md 内の英語プロンプト全文を提示する。
   ユーザー提供の写真が既に Downloads にあっても、改めてプレフィックス付きで複製する(元ファイルは触らない)。
4. ユーザーから生成結果(`Gemini_Generated_Image_*.jfif`)のパスを受け取ったら:
   - PIL で縮小プレビューを作って Read し、図柄・文字・艦番号・袖・ズボン・地色を平置き写真と照合する(jfif 原寸は Read の256KB上限を超える)
   - `output/<名前>_final.jpg` にリネームコピー、Downloads にも同名コピー
   - プロンプト md 末尾に「生成結果」欄を追記(採用版ファイル名・元ファイル名・修正の有無・軽微な差と再生成時の追加指示文)

## プロンプト骨格(加賀 G0942 で完成した逆生成プロンプトの構造)
原本: `046.商品着用イメージ作成/prompts/kaga_back_prompt.md`。固定部分はそのまま、`[ ]` を書き換える。

```
A detailed, high-resolution photo of the upper body of a short-haired man, viewed from the back, standing straight in a studio setting. The man is wearing a [ボディ色], [生地: quick-drying polyester dry T-shirt (smooth, lightweight athletic knit) / performance mesh T-shirt] and brown chino trousers. The back of the [ボディ色] T-shirt features [プリント全体: 単色/多色、色名、線画/筆文字などの様式]. [図柄を上→下、左→右の順に位置・被写体・向き・艦番号/文字を1文ずつ]. [文字ブロック: フォント様式と行ごとの文字列を "..." で列挙]. Every print element is [色の制約: the same X color on Y, with no other colors]. [袖・腰など背面以外のプリント: 位置(as seen from the back)と内容]. The man stands with his arms relaxed at his sides against a clean, plain, bright studio backdrop (light grey/white). The lighting is even and detailed, capturing the fine texture of the [生地] and the [質感: flat, matte finish of the printed graphics (no metallic sheen) / metallic sheen of the foil prints]. Sharp focus.
```

書き方のコツ:
- 文字列は必ず二重引用符で囲み、行単位で列挙する(Gemini は文字を落としやすい)
- 艦番号・ロゴなど写真から読み取れない部分は自己判断せず「要確認」として応答に併記する
- 袖プリントは「背面から見て左/右」を明示。生成で袖口に回り込む場合は `the sleeve print sits fully on the outer face of the sleeve, not wrapping around the hem` を追加

## 決定事項
- 2026-09-01: API(google-genai)自動生成は無料枠429で不採用。Web UI 手動操作に固定
- 2026-09-04: 画像をWebに置いてURLで渡す方式は不採用(画像編集の入力として読まれない)。ドラッグ&ドロップ運用
- 2026-09-04: 素材は日時プレフィックス付き3点セットで Downloads へ。ズボンは茶色系チノパン

## 実績
| 品番 | デザイン | フォルダ | 結果 |
|---|---|---|---|
| G0942 | 加賀 DDH184 背面(黒・箔) | 046 直下(input/output/prompts) | 採用 |
| G2193 | IPD26 背面 グレー/オレンジ | `G2193_IPD26/` | 両色とも1回目で採用 |
