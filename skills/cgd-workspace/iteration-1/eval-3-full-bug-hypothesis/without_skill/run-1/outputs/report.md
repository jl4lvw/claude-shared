# Tkinter Treeview ヘッダークリックソート 稀なクリック無視 — 原因仮説と検証手順

## 症状の整理
- 約10回に1回、ヘッダークリックソートが無視される
- 列に偏らない／他機能は正常／再現手順不明 → **タイミング・イベント配送・状態に絡む間欠問題** を疑う

## 仮説一覧（優先度順）

### H1. ヘッダークリックではなく「列境界（separator）領域」のクリックになっている（最有力）
列境界 ±数ピクセルでは `identify_region` が `"separator"` を返し、`heading` の `command` は発火しない。**列に偏らない／約10%** の症状と整合する。
**検証:** `tree.bind("<Button-1>", ...)` で `tree.identify_region(event.x, event.y)` と `identify_column(event.x)` をログ。無視瞬間に `"separator"` が返っていれば確定。境界線狙いで連打して再現するか確認。

### H2. ソート処理が長く UI スレッドをブロックし、追加クリックが圧縮・破棄されている
ソート中はメインループが止まり、その間のクリックがイベント合成で消える。
**検証:** ソートコールバック先頭末尾を `time.perf_counter()` で計測。100ms 超があるか。行数を10倍にして再現率が上がるか確認。

### H3. ダブルクリック判定との競合
Windows 既定 500ms 以内の2回目クリックは `<Double-Button-1>` 側に流れシングルが抑制される。「ソート→すぐ逆順に」の運用で起こりやすい。
**検証:** `<Button-1>`/`<Double-Button-1>` 両方に時刻ロガーを追加。OS のダブルクリック速度を最遅にして再現率上昇を確認。

### H4. ソートコールバックが例外でサイレント終了している
`None` 混在・型混在で `TypeError` が出ると Tkinter は stderr に流すだけで UI は止まらない。データの状態次第で列も日も変わるため症状と整合。
**検証:** `root.report_callback_exception` を差し替えて `logging.exception()` でフルトレース化。`try/except` でソート関数を包んでログ確認。

### H5. lambda クロージャ／heading 再設定の競合でコールバックが消える
ソート毎に `tree.heading(col, command=...)` を差し替えていると、競合で稀に `command` が None 化することがある。
**検証:** ソート直後に `tree.heading(col, "command")` をロギング、None 化していないか確認。

### H6. 別の `<Button-1>` バインドが条件付きで `return "break"` を返している
選択処理・ドラッグ準備・コンテキストメニューなど別 bind が `"break"` を返すと heading の `command` まで届かない。
**検証:** `bind`/`bind_class`/`bind_all` の `<Button-1>` を grep。`"break"` を返す枝を一時的に潰して症状が消えるか。

### H7. フォーカス・grab 系の競合
モーダル／トーストの `grab_release` 直後のクリックが握り潰される。
**検証:** クリックロガーに `focus_get()` と `tk.call("grab","current")` を併記、無視瞬間に grab が残っていないか確認。

### H8. OS／HW 側のイベント取りこぼし
マウスチャタリング・Bluetooth ロス・RDP 越しなど。
**検証:** 別マウス／有線／ローカル直接で再現するか比較。

## 推奨検証順序
**H1 → H4 → H6 → H3 → H2 → H5 → H7 → H8**
理由: H1 は症状と最も整合かつ検証が数行で済む。H4 は `report_callback_exception` 差し替えだけで網に掛かる。H6 は grep で一次切り分け可。

## 共通診断ハーネス（最初に丸ごと仕込む）
```python
import logging, time
logging.basicConfig(level=logging.DEBUG,
    format="%(asctime)s.%(msecs)03d [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S", encoding="utf-8")

def _diag_button1(event):
    tree = event.widget
    logging.debug("click region=%s col=%s xy=(%d,%d) grab=%s",
        tree.identify_region(event.x, event.y),
        tree.identify_column(event.x),
        event.x, event.y,
        tree.tk.call("grab","current"))
tree.bind("<Button-1>", _diag_button1, add="+")

def _tk_excepthook(exc, val, tb):
    import traceback
    logging.error("Tk callback error:\n%s",
        "".join(traceback.format_exception(exc, val, tb)))
root.report_callback_exception = _tk_excepthook
```
このまま100クリックほど運用ログを取れば H1〜H4 のどれが当たりかほぼ切り分け可能。
