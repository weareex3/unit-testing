"""engine/runner.py has its own INDEPENDENT reader for step_feedback.json /
approved.json (it can't import ui.server without pulling in the whole FastAPI
app) - this is the actual RUNTIME path that decides what commands get
replayed against a live SAP tenant, so it must use the exact same
per-workbook scoping ui/server.py writes, or two unrelated scripts sharing a
Script ID would replay each other's saved commands during a real run."""

import json

import engine.runner as runner


def _write_bucketed(path, workbook, scenario_id, payload):
    path.write_text(json.dumps({workbook: {scenario_id: payload}}))


def test_runner_feedback_scoped_by_workbook(monkeypatch, tmp_path):
    storage_root = tmp_path / "storage"
    client_dir = storage_root / "default"
    client_dir.mkdir(parents=True)
    monkeypatch.setattr(runner, "_storage_root", lambda: storage_root)
    monkeypatch.setenv("CLIENT_ID", "default")

    _write_bucketed(client_dir / "step_feedback.json", "company_a/RCM/script.xlsx", "LOGIN-102",
                     {"step1": "TYPE: company-a-secret"})

    # Different workbook, same scenario id, reads nothing.
    assert runner._load_feedback("LOGIN-102", workbook="company_b/RCM/script.xlsx") == {}
    # Same workbook reads its own trained command.
    assert runner._load_feedback("LOGIN-102", workbook="company_a/RCM/script.xlsx") == {"step1": "TYPE: company-a-secret"}


def test_runner_approved_scoped_by_workbook(monkeypatch, tmp_path):
    storage_root = tmp_path / "storage"
    client_dir = storage_root / "default"
    client_dir.mkdir(parents=True)
    monkeypatch.setattr(runner, "_storage_root", lambda: storage_root)
    monkeypatch.setenv("CLIENT_ID", "default")

    _write_bucketed(client_dir / "approved.json", "company_a/RCM/script.xlsx", "RCM-RC-101",
                     {"approved_at": "2026-01-01", "step_commands": {"s1": "CLICK: A"}})

    assert runner._load_approved_commands("RCM-RC-101", workbook="company_b/RCM/script.xlsx") == {}
    assert runner._load_approved_commands("RCM-RC-101", workbook="company_a/RCM/script.xlsx") == {"s1": "CLICK: A"}


def test_runner_reads_legacy_flat_feedback_file(monkeypatch, tmp_path):
    """Data written before workbook scoping existed (flat {scenario_id: ...})
    must still be found - via the None/legacy bucket - not silently lost."""
    storage_root = tmp_path / "storage"
    client_dir = storage_root / "default"
    client_dir.mkdir(parents=True)
    monkeypatch.setattr(runner, "_storage_root", lambda: storage_root)
    monkeypatch.setenv("CLIENT_ID", "default")

    (client_dir / "step_feedback.json").write_text(json.dumps({"LOGIN-102": {"step1": "TYPE: legacy"}}))

    assert runner._load_feedback("LOGIN-102", workbook=None) == {"step1": "TYPE: legacy"}
    assert runner._load_feedback("LOGIN-102", workbook="some/new/script.xlsx") == {}
