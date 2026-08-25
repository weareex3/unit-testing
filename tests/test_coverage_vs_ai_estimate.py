""""confidence" used to mean two different things depending on the code path:
sometimes the literal known/total step ratio (a fact), sometimes silently
nothing at all since Claude was never actually asked for a confidence score.
That's now split into two honest, separately-named signals:
  - coverage_pct: deterministic, computed from covered_count/total_steps.
  - ai_estimate: Claude's own genuine judgement call, or None if unavailable
    (no API key, or the AI matcher didn't return one) - never silently
    replaced by the coverage number."""

from types import SimpleNamespace

import ui.server as server


def _scenario(n_steps=5):
    steps = [
        SimpleNamespace(step_id=f"EC-SC-001-{i:02d}", action=f"step {i}", expected_result="", test_data="")
        for i in range(1, n_steps + 1)
    ]
    return SimpleNamespace(scenario_id="EC-SC-001", name="Edit employee address", steps=steps,
                            role="HR Administrator", module="EC")


def test_coverage_for_scenario_without_api_key_has_no_fake_ai_estimate(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    out = server._coverage_for_scenario(_scenario(), {})
    assert out["ai_estimate"] is None
    assert out["coverage"] == {}


def test_compute_match_results_coverage_pct_is_exact_ratio(monkeypatch):
    # API key present but the call itself fails -> exercises the name-match
    # fallback path (a key must be set to even reach it; see the bug noted
    # separately about the no-key-at-all case never reaching the fallback).
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    monkeypatch.setattr(
        "anthropic.Anthropic",
        lambda **kw: (_ for _ in ()).throw(RuntimeError("no network in tests")),
        raising=False,
    )
    tasks = {"Edit Address": {"description": "", "steps": {
        "EC-SC-001-01": "CLICK: Edit",
    }}}
    results = server._compute_match_results([_scenario()], tasks)
    row = results[0]
    # Fallback name-match path -> 1 of 5 steps covered -> 20%, exactly.
    assert row["coverage_pct"] == 20
    assert row["covered_count"] == 1
    assert row["total_steps"] == 5
    # And critically: no invented AI estimate when the model call never succeeded.
    assert row["ai_estimate"] is None


def test_normalise_cached_results_migrates_legacy_confidence_field():
    legacy_row = {
        "scenario_id": "EC-SC-001", "name": "x", "matched_to": "Edit Address",
        "reason": "", "confidence": 20, "covered_count": 1, "total_steps": 5,
    }
    out = server._normalise_cached_match_results([legacy_row])
    assert out[0]["coverage_pct"] == 20
    assert out[0]["ai_estimate"] is None
    assert "confidence" not in out[0]


def test_normalise_cached_results_keeps_new_fields_when_present():
    row = {
        "scenario_id": "EC-SC-001", "name": "x", "matched_to": "Edit Address",
        "reason": "", "coverage_pct": 20, "covered_count": 1, "total_steps": 5,
        "ai_estimate": 65, "ai_estimate_reason": "Similar navigation pattern to a known task.",
    }
    out = server._normalise_cached_match_results([row])
    assert out[0]["coverage_pct"] == 20
    assert out[0]["ai_estimate"] == 65
    assert out[0]["ai_estimate_reason"] == "Similar navigation pattern to a known task."


def test_batch_start_stores_coverage_not_confidence(monkeypatch, tmp_path):
    scenarios_dir = tmp_path / "scripts"
    scenarios_dir.mkdir()
    monkeypatch.setattr(server, "_load_scenarios", lambda workbook=None: [_scenario()])
    from fastapi.testclient import TestClient
    client = TestClient(server.app)
    res = client.post("/api/batch/start", json={
        "script": "fake.xlsx", "answers": {}, "modes": {}, "lib_tasks": {},
        "coverage": {"EC-SC-001": 40},
    })
    assert res.status_code == 200
    data = res.json()
    assert data["ok"] is True
    batch = server._BATCH_RUNS[data["batch_id"]]
    item = batch["items"][0]
    assert item["coverage_pct"] == 40
    assert "confidence" not in item
