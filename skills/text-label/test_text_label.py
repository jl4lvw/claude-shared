import json
import sys
from argparse import Namespace
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))
import text_label as tl

G = tl.geometry({"offset_x_mm": 11.0, "offset_y_mm": 24.0, "gutter_x_mm": 2, "gutter_y_mm": 0, "scale": 1.0})


def write_spec(tmp_path: Path, data: dict) -> Path:
    path = tmp_path / "labels.json"
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return path


def test_cells_fill_left_column_first() -> None:
    assert tl.cell_xy(0, G) == (11.0, 24.0)
    assert tl.cell_xy(4, G) == (11.0, 216.0)
    assert tl.cell_xy(5, G) == (102.0, 24.0)


def test_geometry_uses_layout_default_when_calibration_is_empty() -> None:
    g = tl.geometry({})
    assert (g["ox"], g["oy"], g["w"], g["h"]) == (16, 9, 89, 48)


def test_sheets_split_every_ten_faces() -> None:
    assert [len(s) for s in tl.split_sheets(list(range(23)))] == [10, 10, 3]


def test_load_spec_keeps_blank_face(tmp_path: Path) -> None:
    spec = tl.load_spec(write_spec(tmp_path, {"labels": [["あか", "山田　一郎"], None]}))
    assert spec.name == "文字ラベル"
    assert spec.labels[1] is None


@pytest.mark.parametrize("labels", [[], [None], [[]], [["あか", " "]], "あか"])
def test_load_spec_rejects_bad_labels(tmp_path: Path, labels: object) -> None:
    with pytest.raises(ValueError):
        tl.load_spec(write_spec(tmp_path, {"labels": labels}))


def test_pt_shrinks_for_long_line_and_all_faces_share_it() -> None:
    short = tl.choose_pt(tl.Spec("t", [["あか"]]))
    long = tl.choose_pt(tl.Spec("t", [["あか"], ["あ" * 12]]))
    assert short == tl.MAX_PT
    assert long < short
    assert tl.fits(["あ" * 12], [long])


def test_too_long_line_is_an_error() -> None:
    with pytest.raises(ValueError):
        tl.choose_pt(tl.Spec("t", [["あ" * 40]]))


def test_line_pt_reuses_last_value_and_checks_fit() -> None:
    assert tl.pts_for(["a", "b", "c"], [22, 30], 0) == [22.0, 30.0, 30.0]
    with pytest.raises(ValueError):
        tl.choose_pt(tl.Spec("t", [["あ" * 12]], [40]))


def test_html_has_only_filled_faces() -> None:
    label = tl.make_label(["あか", "山田　一郎"], [22.0, 30.0])
    html = tl.sheet_html([label, None, label], G)
    assert html.count('class="label') == 2
    assert "top:120mm;left:11mm" in html


def test_print_without_apply_sends_nothing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def fail(*_: object) -> None:
        raise AssertionError("送ってはいけない")

    monkeypatch.setattr(tl.lp, "enqueue", fail)
    path = write_spec(tmp_path, {"labels": [["あか", "山田　一郎"]]})
    assert tl.cmd_print(Namespace(json=path, place="倉庫3階", apply=False)) == 0


def test_print_stops_at_failed_sheet(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    sent: list[str] = []

    def enqueue(_printer: str, _html: str, title: str) -> dict:
        sent.append(title)
        return {"job_id": "j"}

    monkeypatch.setattr(tl.lp, "enqueue", enqueue)
    monkeypatch.setattr(tl.lp, "get_calibration", lambda *_: {})
    monkeypatch.setattr(tl, "wait_job", lambda _: {"job_status": "error", "message": "紙切れ"})
    path = write_spec(tmp_path, {"labels": [["あ"]] * 11})
    assert tl.cmd_print(Namespace(json=path, place="倉庫3階", apply=True)) == 1
    assert len(sent) == 1
