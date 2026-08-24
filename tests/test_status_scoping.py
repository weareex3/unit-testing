"""Scenario statuses must be scoped per workbook, not globally by scenario_id.
Two different test scripts routinely reuse the same author-chosen Script ID
(e.g. "LOGIN-102") - without per-workbook scoping, a brand-new upload that
happens to reuse an id from an unrelated old script would instantly show as
already-tested with zero runs performed."""

import json

import ui.server as server


def test_status_does_not_leak_across_workbooks(monkeypatch, tmp_path):
    status_file = tmp_path / "step_status.json"
    monkeypatch.setattr(server, "STATUS_FILE", status_file)

    server._save_statuses("script_a.xlsx", {"LOGIN-102": {"step1": "pass"}})

    # A different workbook that happens to reuse the same scenario id starts clean.
    assert server._load_statuses("script_b.xlsx").get("LOGIN-102") is None
    assert server._step_status("script_b.xlsx", "LOGIN-102", "step1") == "not_tested"

    # The original workbook's status is untouched.
    assert server._step_status("script_a.xlsx", "LOGIN-102", "step1") == "pass"


def test_scenario_status_scoped_per_workbook(monkeypatch, tmp_path):
    status_file = tmp_path / "step_status.json"
    monkeypatch.setattr(server, "STATUS_FILE", status_file)

    server._save_statuses("script_a.xlsx", {"RCM-RC-101": {"s1": "pass", "s2": "pass"}})
    server._save_statuses("script_b.xlsx", {"RCM-RC-101": {"s1": "fail"}})

    a_status = server._scenario_status("script_a.xlsx", "RCM-RC-101", total_steps=2)
    b_status = server._scenario_status("script_b.xlsx", "RCM-RC-101", total_steps=2)
    fresh_status = server._scenario_status("script_c.xlsx", "RCM-RC-101", total_steps=2)

    assert a_status["status"] == "pass"
    assert b_status["status"] == "fail"
    assert fresh_status["status"] == "not_tested"


def test_reset_scenario_evidence_only_clears_its_own_workbook(monkeypatch, tmp_path):
    status_file = tmp_path / "step_status.json"
    monkeypatch.setattr(server, "STATUS_FILE", status_file)
    runs_dir = tmp_path / "runs"
    runs_dir.mkdir()
    monkeypatch.setattr(server, "RUNS_DIR", runs_dir)

    server._save_statuses("script_a.xlsx", {"LOGIN-102": {"step1": "pass"}})
    server._save_statuses("script_b.xlsx", {"LOGIN-102": {"step1": "fail"}})

    server._reset_scenario_evidence({"LOGIN-102"}, workbook="script_b.xlsx")

    assert server._load_statuses("script_b.xlsx") == {}
    assert server._load_statuses("script_a.xlsx") == {"LOGIN-102": {"step1": "pass"}}


def test_legacy_flat_status_file_is_migrated_not_lost(monkeypatch, tmp_path):
    status_file = tmp_path / "step_status.json"
    status_file.write_text(json.dumps({"LOGIN-102": {"step1": "pass"}}))
    monkeypatch.setattr(server, "STATUS_FILE", status_file)

    # Old data written before per-workbook scoping existed is preserved under
    # the legacy bucket (workbook=None), not silently dropped.
    assert server._load_statuses(None) == {"LOGIN-102": {"step1": "pass"}}
    # A real, named workbook never inherits it.
    assert server._load_statuses("script_a.xlsx") == {}


def test_stats_all_workbooks_sums_scoped_stats(monkeypatch, tmp_path):
    status_file = tmp_path / "step_status.json"
    monkeypatch.setattr(server, "STATUS_FILE", status_file)
    monkeypatch.setattr(server, "_workbooks", lambda: [{"key": "a.xlsx"}, {"key": "b.xlsx"}])
    monkeypatch.setattr(
        server, "_load_scenarios",
        lambda wb=None: {
            "a.xlsx": [_FakeScenario("LOGIN-102", 1)],
            "b.xlsx": [_FakeScenario("LOGIN-102", 1)],
        }.get(wb, []),
    )
    server._save_statuses("a.xlsx", {"LOGIN-102": {"s1": "pass"}})
    server._save_statuses("b.xlsx", {"LOGIN-102": {"s1": "fail"}})

    totals = server._stats(None)

    assert totals["total"] == 2
    assert totals["passing"] == 1
    assert totals["failing"] == 1


class _FakeScenario:
    def __init__(self, scenario_id, step_count):
        self.scenario_id = scenario_id
        self.steps = [object()] * step_count
