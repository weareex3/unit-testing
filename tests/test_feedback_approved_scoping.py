"""Trained step commands and approved 'golden playbook' commands used to be
keyed globally by scenario_id - the same bug already fixed for statuses, but
worse: this data is REPLAYED by the runner, so a collision doesn't just show
a wrong badge, it makes the tool execute another script's (or another
company's) saved commands against a live SAP tenant. Scoped per workbook,
exactly like statuses."""

import json

import ui.server as server


def test_feedback_does_not_leak_across_workbooks(monkeypatch, tmp_path):
    feedback_file = tmp_path / "step_feedback.json"
    monkeypatch.setattr(server, "FEEDBACK_FILE", feedback_file)

    server._save_feedback("company_a/RCM/script.xlsx", {"LOGIN-102": {"step1": "TYPE: a-secret"}})

    # A DIFFERENT workbook reusing the same scenario id starts clean.
    assert server._load_feedback("company_b/RCM/script.xlsx").get("LOGIN-102") is None
    # The original workbook's trained command is untouched.
    assert server._load_feedback("company_a/RCM/script.xlsx")["LOGIN-102"]["step1"] == "TYPE: a-secret"


def test_approved_does_not_leak_across_workbooks(monkeypatch, tmp_path):
    approved_file = tmp_path / "approved.json"
    monkeypatch.setattr(server, "APPROVED_FILE", approved_file)

    server._save_approved("company_a/RCM/script.xlsx", {
        "RCM-RC-101": {"approved_at": "2026-01-01", "step_commands": {"s1": "CLICK: A"}},
    })

    assert server._load_approved("company_b/RCM/script.xlsx").get("RCM-RC-101") is None
    assert server._load_approved("company_a/RCM/script.xlsx")["RCM-RC-101"]["step_commands"]["s1"] == "CLICK: A"


def test_legacy_flat_feedback_file_is_migrated_not_lost(monkeypatch, tmp_path):
    feedback_file = tmp_path / "step_feedback.json"
    feedback_file.write_text(json.dumps({"LOGIN-102": {"step1": "TYPE: old"}}))
    monkeypatch.setattr(server, "FEEDBACK_FILE", feedback_file)

    assert server._load_feedback(None) == {"LOGIN-102": {"step1": "TYPE: old"}}
    assert server._load_feedback("some/new/script.xlsx") == {}


def test_legacy_flat_approved_file_is_migrated_not_lost(monkeypatch, tmp_path):
    approved_file = tmp_path / "approved.json"
    approved_file.write_text(json.dumps({
        "RCM-RC-101": {"approved_at": "2026-01-01", "step_commands": {"s1": "CLICK: old"}},
    }))
    monkeypatch.setattr(server, "APPROVED_FILE", approved_file)

    assert server._load_approved(None)["RCM-RC-101"]["step_commands"]["s1"] == "CLICK: old"
    assert server._load_approved("some/new/script.xlsx") == {}


def test_library_save_does_not_silently_overwrite_different_scenario(monkeypatch, tmp_path):
    """Two DIFFERENT scenarios saved under the same human-typed task name must
    not destroy each other - the second save should auto-disambiguate."""
    library_file = tmp_path / "step_library.json"
    monkeypatch.setattr(server, "LIBRARY_FILE", library_file)
    approved_file = tmp_path / "approved.json"
    monkeypatch.setattr(server, "APPROVED_FILE", approved_file)
    monkeypatch.setattr(server, "_backup_learned_data", lambda: None)
    monkeypatch.setattr(server.threading, "Thread", lambda *a, **k: type("T", (), {"start": lambda self: None})())

    from types import SimpleNamespace
    scenario_a = SimpleNamespace(
        scenario_id="RCM-RC-101", name="Hire a new employee",
        steps=[SimpleNamespace(step_id="s1", action="Open form", expected_result="")],
    )
    scenario_b = SimpleNamespace(
        scenario_id="EC-SC-777", name="Hire a new employee",
        steps=[SimpleNamespace(step_id="s1", action="Different form entirely", expected_result="")],
    )
    user = {"name": "tester", "company": "internal"}

    r1 = server._save_reviewed_task_to_library(scenario_a, {"s1": "CLICK: A"}, user, "run1",
                                                task_name="Hire a new employee")
    r2 = server._save_reviewed_task_to_library(scenario_b, {"s1": "CLICK: B"}, user, "run2",
                                                task_name="Hire a new employee")

    assert r1["task_name"] == "Hire a new employee"
    assert r2["task_name"] != "Hire a new employee"  # auto-disambiguated, not clobbered

    library = server._load_library()
    assert library["Hire a new employee"]["scenario_id"] == "RCM-RC-101"
    assert library[r2["task_name"]]["scenario_id"] == "EC-SC-777"


def test_library_save_updates_same_scenario_in_place(monkeypatch, tmp_path):
    """Re-saving the SAME scenario under the same name is a normal retrain,
    not a collision - it should update, not disambiguate."""
    library_file = tmp_path / "step_library.json"
    monkeypatch.setattr(server, "LIBRARY_FILE", library_file)
    approved_file = tmp_path / "approved.json"
    monkeypatch.setattr(server, "APPROVED_FILE", approved_file)
    monkeypatch.setattr(server, "_backup_learned_data", lambda: None)
    monkeypatch.setattr(server.threading, "Thread", lambda *a, **k: type("T", (), {"start": lambda self: None})())

    from types import SimpleNamespace
    scenario = SimpleNamespace(
        scenario_id="RCM-RC-101", name="Hire a new employee",
        steps=[SimpleNamespace(step_id="s1", action="Open form", expected_result="")],
    )
    user = {"name": "tester", "company": "internal"}

    server._save_reviewed_task_to_library(scenario, {"s1": "CLICK: A"}, user, "run1", task_name="Hire a new employee")
    r2 = server._save_reviewed_task_to_library(scenario, {"s1": "CLICK: A-updated"}, user, "run2",
                                                task_name="Hire a new employee")

    assert r2["task_name"] == "Hire a new employee"
    library = server._load_library()
    assert library["Hire a new employee"]["steps"]["s1"] == "CLICK: A-updated"
