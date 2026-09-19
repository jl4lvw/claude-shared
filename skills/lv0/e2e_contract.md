【自動検査の約束（依頼元が自動で付けた節。仕様と同じ重さで守ること）】
検査は依頼元が機械で実行する。あなたはテストやアプリの起動を実行できないので、コードとテストを**書く**ことに専念する。
検査に失敗すると、失敗の出力が次の依頼に付いて戻ってくる。そのときは**テストではなく実装を直す**。

1. 凍結ファイル: 依頼元が「変更不可」に指定したファイル（すでにあるテスト等）は、変更も削除もしない。新しく足すのは可。
2. e2e スクリプトが仕様で求められていて、まだ無い（足りない）ときは `tests/e2e/` に Python スクリプトとして書く。`python tests/e2e/<名前>.py` で実行される。
   - Playwright の同期 API・Chromium・headless を使う: `from playwright.sync_api import sync_playwright`
   - 接続先は環境変数 `LV0_BASE_URL`（アプリは依頼元が起動する）。スクリーンショットは環境変数 `LV0_SHOT_DIR` に PNG で保存する（主要な状態だけ・最大 6 枚）
   - スマホ幅で確認する: `viewport={"width": 375, "height": 812}`・`device_scale_factor=2`・`is_mobile=True`・`has_touch=True`
   - JS のエラーを拾い、1 件でもあれば失敗にする: `page.on("pageerror", ...)` と `page.on("console", ...)`（type が error のもの）
   - 失敗は `AssertionError` か `sys.exit(1)` で表し、合格したら最後に `print("E2E OK")`
   - 待つときは `page.wait_for_selector` や `expect` を使い、固定の sleep は避ける。1 スクリプトは 60 秒以内
   - 本番のデータには触れない。アプリは検査用の一時データで動くが、書き込みを伴う操作は最小限にする
3. 通常のユニットテスト（node --test / pytest）も、仕様で求められたものは同じ考え方で書く。
