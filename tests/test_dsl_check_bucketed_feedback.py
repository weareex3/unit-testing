"""eval.dsl_check._iter_commands reads step_feedback.json/approved.json
directly off disk, independently of ui/server.py's readers - it must
understand the same {workbook_key: {scenario_id: ...}} shape those write,
or once real bucketed data exists it would misread workbook keys as
scenario ids and stop linting stored commands correctly."""

import json

import eval.dsl_check as dsl_check


def test_iter_commands_reads_bucketed_feedback(monkeypatch, tmp_path):
    storage = tmp_path / "storage"
    client_dir = storage / "acme"
    client_dir.mkdir(parents=True)
    (client_dir / "step_feedback.json").write_text(json.dumps({
        "company_a/RCM/script.xlsx": {"LOGIN-102": {"step1": "TYPE: hello"}},
    }))
    monkeypatch.setattr(dsl_check, "STORAGE", storage)

    results = list(dsl_check._iter_commands())
    sources = [r[0] for r in results]
    assert any("company_a/RCM/script.xlsx/LOGIN-102" in s for s in sources)
    assert results[0][1] == "step1"
    assert results[0][2] == "TYPE: hello"


def test_iter_commands_reads_legacy_flat_feedback(monkeypatch, tmp_path):
    storage = tmp_path / "storage"
    client_dir = storage / "acme"
    client_dir.mkdir(parents=True)
    (client_dir / "step_feedback.json").write_text(json.dumps({
        "LOGIN-102": {"step1": "TYPE: legacy"},
    }))
    monkeypatch.setattr(dsl_check, "STORAGE", storage)

    results = list(dsl_check._iter_commands())
    assert results == [("feedback:acme/LOGIN-102", "step1", "TYPE: legacy")]


def test_iter_commands_reads_bucketed_approved(monkeypatch, tmp_path):
    storage = tmp_path / "storage"
    client_dir = storage / "acme"
    client_dir.mkdir(parents=True)
    (client_dir / "approved.json").write_text(json.dumps({
        "company_a/RCM/script.xlsx": {
            "RCM-RC-101": {"approved_at": "2026-01-01", "step_commands": {"s1": "CLICK: A"}},
        },
    }))
    monkeypatch.setattr(dsl_check, "STORAGE", storage)

    results = list(dsl_check._iter_commands())
    sources = [r[0] for r in results]
    assert any("company_a/RCM/script.xlsx/RCM-RC-101" in s for s in sources)
    assert results[0][2] == "CLICK: A"
