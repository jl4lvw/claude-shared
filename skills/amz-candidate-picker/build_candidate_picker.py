"""
候補セレクター(検索+チェックボックス+DB永続化のArtifact)を生成する。

入力: candidates.json (下記スキーマの配列)
出力: Artifact ツールにそのまま渡せる完成HTML (1ファイル)

candidates.json のスキーマ (各要素、キー名は固定):
    {
        "g": "G2139",              # G番号など、行を一意に識別する文字列(必須)
        "sku": "G2139",            # Eストア SKU 等の表示用コード(必須)
        "name": "PVCワッペン ( 潜水艦せきりゅう )",  # 商品名(必須)
        "price": 990,              # 価格。int(必須)
        "stock": 90,               # 在庫数。int。親SKU等で子側管理の場合は null
        "status": "public",        # "public" / "hidden" のいずれか(必須)
        "thumb": "data:image/jpeg;base64,..."  # サムネイル画像(任意)。無ければ省略可
    }

使い方:
    python build_candidate_picker.py \
        --input candidates.json \
        --output out.html \
        --title "PVCワッペン候補セレクター" \
        --eyebrow "Amazon未登録候補 · PVCワッペン限定" \
        --subtitle "検索で絞り込み、行をクリック(またはチェックボックス)で選択してください。選択内容は下部に一覧され、自動保存されます。" \
        --footer "データは2026-09-10生成のスナップショット。登録前に現況の再確認が必要です。" \
        --slug amz_pvc_wappen_picker \
        --search-placeholder "G番号・SKU・商品名で検索"

出力後、Artifact ツールで capabilities={"db": {}} を指定して publish する。
選択結果は Artifact action="read_db" で collection="candidate_picker/<slug>", doc_id="selection" を
get すれば {"selected": [...], "updated_at": "..."} が読み取れる。
"""

import argparse
import json
from pathlib import Path

SKILL_DIR = Path(__file__).resolve().parent
TEMPLATE_PATH = SKILL_DIR / "template_candidate_picker.html"

REQUIRED_FIELDS = ("g", "sku", "name", "price", "status")


def load_candidates(path: Path) -> list[dict]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, list):
        raise SystemExit(f"[amz-candidate-picker] 入力は配列である必要があります: {path}")
    for i, row in enumerate(data):
        missing = [f for f in REQUIRED_FIELDS if f not in row]
        if missing:
            raise SystemExit(f"[amz-candidate-picker] {i}番目の要素にキーが不足しています: {missing} row={row}")
        row.setdefault("stock", None)
        row.setdefault("thumb", "")
    return data


def build(
    candidates: list[dict],
    title: str,
    eyebrow: str,
    subtitle: str,
    footer: str,
    slug: str,
    search_placeholder: str,
) -> str:
    template = TEMPLATE_PATH.read_text(encoding="utf-8")
    data_json = json.dumps(candidates, ensure_ascii=False)
    # NOTE: db.doc() の第一引数は「collection/doc/collection/doc/...」形式で、
    # 全体のセグメント数が偶数でなければならない(奇数だとコレクション参照になり、
    # doc()が指すドキュメントが存在しない扱いになる)。2026-09-11、3セグメント
    # ("candidate_picker/<slug>/selection")で作った初版のタオルセレクターが
    # 選択を一切保存できていなかった実障害が発生し、2セグメントに修正した。
    db_doc = f"candidate_picker/{slug}"

    out = template
    out = out.replace("__DATA__", data_json)
    # __TITLE__ は <title> タグと <h1> の両方に入るため個別置換ではなく一括置換でよい
    out = out.replace("__TITLE__", title)
    out = out.replace("__EYEBROW__", eyebrow)
    out = out.replace("__SUBTITLE__", subtitle)
    out = out.replace("__FOOTER_NOTE__", footer)
    out = out.replace("__DB_DOC__", db_doc)
    out = out.replace("__SEARCH_PLACEHOLDER__", search_placeholder)
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--input", required=True, type=Path, help="candidates.json のパス")
    ap.add_argument("--output", required=True, type=Path, help="出力HTMLのパス")
    ap.add_argument("--title", required=True, help="ページタイトル(<title>と<h1>に使用)")
    ap.add_argument("--eyebrow", required=True, help="タイトル上の小見出し")
    ap.add_argument("--subtitle", required=True, help="タイトル下の説明文")
    ap.add_argument("--footer", required=True, help="ページ下部の出典・注意書き")
    ap.add_argument("--slug", required=True, help="db保存先の識別子(英数字とアンダースコア推奨、他の候補セレクターと衝突しないユニークな値)")
    ap.add_argument(
        "--search-placeholder",
        default="G番号・SKU・商品名で検索(スペース区切りでAND)",
        help="検索欄のプレースホルダ",
    )
    args = ap.parse_args()

    candidates = load_candidates(args.input)
    html = build(
        candidates=candidates,
        title=args.title,
        eyebrow=args.eyebrow,
        subtitle=args.subtitle,
        footer=args.footer,
        slug=args.slug,
        search_placeholder=args.search_placeholder,
    )
    args.output.write_text(html, encoding="utf-8", newline="")
    print(f"written {args.output} ({len(html)} bytes, {len(candidates)} candidates, db_doc=candidate_picker/{args.slug})")


if __name__ == "__main__":
    main()
