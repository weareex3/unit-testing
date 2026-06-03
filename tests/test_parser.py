"""Tests for the Excel UAT workbook parser — the front door of the whole product.
Builds a small workbook in a temp file and asserts scenarios/steps parse correctly."""
import openpyxl
import pytest
from engine.parser import parse_workbook

HEADER = ["#", "Script ID", "Scenario", "Role", "Step", "Action",
          "Test Data", "Expected Result", "Status", "Comments"]


def _write_workbook(path, rows):
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "End-to-End Test"
    ws.append(HEADER)
    for r in rows:
        ws.append(r)
    wb.save(path)
    return str(path)


def test_parses_two_scenarios_with_steps(tmp_path):
    rows = [
        [1, "NAV-100", "Navigate", "Admin", 1, "Open Employee Files", "", "Module loads", "To Be Tested", ""],
        [1, "PXY-200", "Proxy", "Admin", 1, "Proxy as another user", "{{target_employee_name}}", "Now proxied", "To Be Tested", ""],
        [2, "", "", "", 2, "Search the employee", "{{target_employee_name}}", "Profile shown", "To Be Tested", ""],
    ]
    scenarios = parse_workbook(_write_workbook(tmp_path / "t.xlsx", rows))
    assert [s.scenario_id for s in scenarios] == ["NAV-100", "PXY-200"]
    nav, pxy = scenarios
    assert nav.name == "Navigate" and len(nav.steps) == 1
    assert len(pxy.steps) == 2  # blank Script ID row carried forward into PXY-200


def test_carries_scenario_name_and_role_forward(tmp_path):
    rows = [
        [1, "PXY-200", "Proxy flow", "Recruiter", 1, "Proxy as user", "Esther", "ok", "To Be Tested", ""],
        [2, "", "", "", 2, "Open profile", "", "ok", "To Be Tested", ""],
    ]
    scenarios = parse_workbook(_write_workbook(tmp_path / "t.xlsx", rows))
    assert scenarios[0].name == "Proxy flow"
    assert scenarios[0].role == "Recruiter"
    assert scenarios[0].steps[1].step_id == "PXY-200-02"


def test_module_derived_from_script_id_prefix(tmp_path):
    rows = [[1, "RCM-RC-101", "Create req", "Recruiter", 1, "Create requisition", "", "Created", "To Be Tested", ""]]
    scenarios = parse_workbook(_write_workbook(tmp_path / "t.xlsx", rows))
    assert scenarios[0].module == "RCM"


def test_skips_section_headers_and_blank_rows(tmp_path):
    rows = [
        ["► SECTION: Setup", "", "", "", "", "", "", "", "", ""],   # section divider
        [None, None, None, None, None, None, None, None, None, None],    # fully blank
        [1, "NAV-100", "Navigate", "Admin", 1, "Open module", "", "Loads", "To Be Tested", ""],
    ]
    scenarios = parse_workbook(_write_workbook(tmp_path / "t.xlsx", rows))
    assert len(scenarios) == 1
    assert scenarios[0].scenario_id == "NAV-100"
    assert len(scenarios[0].steps) == 1


def test_row_with_no_action_expected_or_data_is_not_a_step(tmp_path):
    rows = [
        [1, "NAV-100", "Navigate", "Admin", 1, "Open module", "", "Loads", "To Be Tested", ""],
        [2, "", "", "", 2, "", "", "", "", "just a comment"],   # no action/expected/data
    ]
    scenarios = parse_workbook(_write_workbook(tmp_path / "t.xlsx", rows))
    assert len(scenarios[0].steps) == 1


def test_missing_file_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        parse_workbook(str(tmp_path / "does_not_exist.xlsx"))
