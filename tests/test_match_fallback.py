"""The name-overlap match fallback may suggest a task, but must not claim
coverage for steps the task has no saved commands for."""

from types import SimpleNamespace

from ui.server import _fallback_name_match


def _scenario():
    steps = [SimpleNamespace(step_id=f"EC-SC-001-{i:02d}", action=a) for i, a in enumerate([
        "Log in and proxy as the HR admin",
        "Open the employee's profile",
        "Edit the address card",
        "Change Address Line 2",
        "Verify the new address is shown",
    ], 1)]
    return SimpleNamespace(scenario_id="EC-SC-001", name="Edit employee address", steps=steps)


def test_name_match_does_not_inflate_coverage():
    tasks = {"Edit Address": {"description": "", "steps": {"HUB-01": "CLICK: Edit"}}}
    out = _fallback_name_match(_scenario(), tasks)
    assert out is not None
    assert out["matched_task"] == "Edit Address"
    assert sum(1 for v in out["coverage"].values() if v) == 0


def test_exact_step_reuse_is_covered():
    tasks = {"Edit Address": {"description": "", "steps": {
        "EC-SC-001-03": "CLICK: Edit", "EC-SC-001-04": "FILL: Address Line 2 | x",
    }}}
    out = _fallback_name_match(_scenario(), tasks)
    covered = [sid for sid, v in out["coverage"].items() if v]
    assert covered == ["EC-SC-001-03", "EC-SC-001-04"]


def test_no_overlap_returns_none():
    tasks = {"Copy Position": {"description": "", "steps": {"S1": "CLICK: OK"}}}
    assert _fallback_name_match(_scenario(), tasks) is None
