"""Guards that keep replay honest: positional step-mapping only when shapes
agree, fragility warnings on stored commands, and upload sanity checks."""

import openpyxl

from engine.runner import _positional_library_map
from eval.dsl_check import _warn_line
from ui.server import _workbook_sanity


def test_positional_map_same_count():
    steps = {"A-01": "CLICK: X", "A-02": "CLICK: Y"}
    assert _positional_library_map(steps, 2) == {"0": "CLICK: X", "1": "CLICK: Y"}


def test_positional_map_single_blob_applies_at_step_one():
    steps = {"step-1": "CLICK: X\nWAIT: 500"}
    assert _positional_library_map(steps, 5) == {"0": "CLICK: X\nWAIT: 500"}


def test_positional_map_mismatch_returns_nothing():
    steps = {"A-01": "CLICK: X", "A-02": "CLICK: Y", "A-03": "CLICK: Z"}
    assert _positional_library_map(steps, 5) == {}


def test_warn_on_coordinate_clicks_and_js():
    assert "coordinate click" in _warn_line("CLICK_XY: 1224, 25")
    assert "coordinate click" in _warn_line("CLICK_MARK: 7 | 640, 320")
    assert "JS" in _warn_line("JS: document.title")
    assert _warn_line("CLICK_MARK: 7") is None
    assert _warn_line("CLICK: Proxy Now") is None


def test_warn_on_stale_literals_but_not_tokens():
    assert "date" in _warn_line("TYPE: 01/07/2026")
    assert "numeric" in _warn_line("FILL: Requisition | 100139")
    assert _warn_line("TYPE: {{today}}") is None
    assert _warn_line("FILL: Effective Date | {{today}}") is None


def _make_workbook(path, rows):
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Test Scripts"
    ws.append(["#", "Script ID", "Scenario", "Role", "Step",
               "Action", "Test Data", "Expected Result", "Status", "Comments"])
    for r in rows:
        ws.append(r)
    wb.save(path)


def test_sanity_accepts_normal_workbook(tmp_path):
    p = tmp_path / "ok.xlsx"
    _make_workbook(p, [
        [1, "EC-01", "Edit address", "Admin", 1, "Open profile", "", "Profile shown", "", ""],
        [2, "", "", "", 2, "Edit the address", "x", "Address editable", "", ""],
    ])
    assert _workbook_sanity(str(p)) is None


def test_sanity_rejects_empty_workbook(tmp_path):
    p = tmp_path / "empty.xlsx"
    _make_workbook(p, [])
    assert "no scenarios" in _workbook_sanity(str(p))


def test_sanity_rejects_shifted_columns(tmp_path):
    p = tmp_path / "shifted.xlsx"
    _make_workbook(p, [
        [1, "EC-01", "Edit address", "Admin", 1, "", "", "something in wrong col", "", ""],
        [2, "", "", "", 2, "", "", "another wrong col", "", ""],
        [3, "", "", "", 3, "only one real action", "", "ok", "", ""],
    ])
    assert "no action text" in _workbook_sanity(str(p))
