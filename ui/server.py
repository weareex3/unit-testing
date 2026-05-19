"""EX3 TestOps — FastAPI dashboard."""

import json
import os
import sys
import threading
from pathlib import Path
from collections import defaultdict
from datetime import datetime

from dotenv import load_dotenv
from fastapi import FastAPI, Request, Form, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")
sys.path.insert(0, str(ROOT))
from engine.parser import parse_workbook  # noqa: E402
from engine.runner import run_scenario  # noqa: E402

CLIENT_ID = os.getenv("CLIENT_ID", "default")

SCRIPTS_DIR = ROOT / "scripts"
RUNS_DIR = ROOT / "runs" / CLIENT_ID
STORAGE_DIR = ROOT / "storage" / CLIENT_ID
STATUS_FILE = STORAGE_DIR / "step_status.json"

RUNS_DIR.mkdir(parents=True, exist_ok=True)
STORAGE_DIR.mkdir(parents=True, exist_ok=True)
(ROOT / "storage" / "global").mkdir(parents=True, exist_ok=True)


def _restore_feedback_from_approved() -> None:
    """If step_feedback.json is missing, rebuild it from approved.json on startup."""
    feedback_file = STORAGE_DIR / "step_feedback.json"
    approved_file = STORAGE_DIR / "approved.json"
    if feedback_file.exists() or not approved_file.exists():
        return
    try:
        approved = json.loads(approved_file.read_text())
        merged: dict = {}
        for _scenario_id, entry in approved.items():
            commands = entry.get("step_commands", {})
            if isinstance(commands, dict):
                merged.setdefault(_scenario_id, {}).update(commands)
        if merged:
            feedback_file.write_text(json.dumps(merged, indent=2))
            print(f"[startup] restored step_feedback.json from approved.json ({len(merged)} scenarios)")
    except Exception as exc:
        print(f"[startup] could not restore step_feedback.json: {exc}")


_restore_feedback_from_approved()

# In-memory run state: scenario_id -> {status, run_id, passed?, error?}
_ACTIVE_RUNS: dict[str, dict] = {}

# Pause/resume state: scenario_id -> {event, fix}
_PAUSE_EVENTS: dict[str, threading.Event] = {}
_PAUSE_FIX: dict[str, dict | None] = {}

# Live control — runner thread owns all Playwright calls while paused.
# Server just reads screenshot files and appends to the action queue.
_LIVE_SHOT_PATHS: dict[str, Path] = {}   # scenario_id -> Path of latest PNG
_LIVE_QUEUES: dict[str, list] = {}       # scenario_id -> list of pending actions

# Force-pause: set by UI to pause the runner before the next step
_FORCE_PAUSE: dict[str, bool] = {}

# Supervised run: pauses after EVERY step for human confirmation
_CONFIRM_EVENTS: dict[str, threading.Event] = {}
_CONFIRM_RESULTS: dict[str, bool] = {}  # True = confirmed, False = redo


def _humanise_error(raw_error: str) -> str:
    """Ask Claude to translate a raw Playwright/Python error into plain English."""
    key = os.getenv("ANTHROPIC_API_KEY")
    if not key or not raw_error:
        return raw_error
    try:
        import anthropic
        client = anthropic.Anthropic(api_key=key)
        msg = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=300,
            system=(
                "You are an expert in SAP SuccessFactors and automated browser testing. "
                "Translate technical Playwright/Python errors into clear, plain-English explanations "
                "that a UAT tester can understand. Be specific about what failed and why."
            ),
            messages=[{"role": "user", "content":
                f"Translate this error into 1-2 plain-English sentences. Say exactly what the automation "
                f"couldn't find or do, and give a likely reason (e.g. page hadn't loaded, element was hidden, "
                f"wrong page was open). No jargon, no code.\n\nError: {raw_error[:600]}"}],
        )
        return msg.content[0].text.strip()
    except Exception:
        return raw_error


def _pause_callback(scenario_id: str, step_id: str, screenshot_path: str, run_id: str, error_message: str = "", page=None, live_step: bool = False):
    """Called by runner when a step fails — pauses and waits for human fix.

    If a live page is provided, runs a screenshot+action loop in the CALLING
    (runner) thread so Playwright is never touched cross-thread.
    """
    import time as _time

    evt = threading.Event()
    _PAUSE_EVENTS[scenario_id] = evt
    _PAUSE_FIX[scenario_id] = None
    shot_url = f"/runs/{run_id}/{Path(screenshot_path).name}" if screenshot_path else None
    # Don't humanise live_step messages — they're step descriptions, not errors
    human_error = error_message if live_step else _humanise_error(error_message)
    _ACTIVE_RUNS[scenario_id].update({
        "status": "paused",
        "paused_step": step_id,
        "screenshot_url": shot_url,
        "error_message": human_error,
        "raw_error": error_message,
        "live_step": live_step,
    })

    if page is not None:
        # Prepare shared paths / queues
        shot_path = RUNS_DIR / f"{scenario_id}_liveshot.png"
        _LIVE_SHOT_PATHS[scenario_id] = shot_path
        _LIVE_QUEUES[scenario_id] = []

        # Seed with the screenshot from the last automated step — always a real
        # page capture taken after browser interactions, never a blank.
        if screenshot_path and Path(screenshot_path).exists():
            import shutil as _shutil
            try:
                _shutil.copy2(screenshot_path, shot_path)
                print(f"  [live-seed] seeded {shot_path.stat().st_size // 1024}KB from {Path(screenshot_path).name}")
            except Exception:
                pass

        print(f"  [pause] {scenario_id} paused on {step_id} — live control active")

        # Run screenshot + action loop in THIS (runner) thread while waiting.
        # We poll evt with a short timeout so we can process queued actions.
        while not evt.wait(timeout=0.8):
            # Process any pending actions from the UI
            queue = _LIVE_QUEUES.get(scenario_id, [])
            while queue:
                action = queue.pop(0)
                try:
                    atype = action.get("type")
                    if atype == "click":
                        page.mouse.click(action["x"], action["y"])
                        page.wait_for_timeout(400)
                    elif atype == "type":
                        page.keyboard.type(action["text"], delay=60)
                        page.wait_for_timeout(300)
                    elif atype == "scroll":
                        px = action.get("px", 400)
                        page.mouse.wheel(0, px)
                        page.wait_for_timeout(400)
                    elif atype == "key":
                        k = action["key"]
                        scroll_map = {
                            "PageDown": 600, "PageUp": -600,
                            "ArrowDown": 120, "ArrowUp": -120,
                        }
                        if k in scroll_map:
                            page.mouse.wheel(0, scroll_map[k])
                        else:
                            page.keyboard.press(k)
                        page.wait_for_timeout(400)
                except Exception as _e:
                    print(f"  [live-action] {_e}")
            # Take a fresh screenshot
            try:
                page.screenshot(path=str(shot_path))
            except Exception:
                pass

        _LIVE_SHOT_PATHS.pop(scenario_id, None)
        _LIVE_QUEUES.pop(scenario_id, None)
    else:
        print(f"  [pause] {scenario_id} paused on {step_id} — waiting up to 10 min for human fix")
        evt.wait(timeout=600)

    fix = _PAUSE_FIX.pop(scenario_id, None)
    _PAUSE_EVENTS.pop(scenario_id, None)
    _ACTIVE_RUNS[scenario_id]["status"] = "running"
    return fix


def _confirm_callback(scenario_id: str, step_id: str, screenshot_path: str, run_id: str) -> bool:
    """Called by runner after each successful step in supervised mode.

    Pauses the run and waits for the user to click 'Step Done'.
    Returns True if confirmed (move on), False if user wants to redo.
    """
    shot_url = f"/runs/{run_id}/{Path(screenshot_path).name}" if screenshot_path else None
    _ACTIVE_RUNS[scenario_id].update({
        "status": "confirming",
        "confirming_step": step_id,
        "screenshot_url": shot_url,
    })

    evt = threading.Event()
    _CONFIRM_EVENTS[scenario_id] = evt
    _CONFIRM_RESULTS[scenario_id] = True  # default: confirmed

    print(f"  [supervised] waiting for user to confirm step {step_id}")
    evt.wait(timeout=600)

    confirmed = _CONFIRM_RESULTS.pop(scenario_id, True)
    _CONFIRM_EVENTS.pop(scenario_id, None)
    _ACTIVE_RUNS[scenario_id]["status"] = "running"
    return confirmed


VALID_STATUSES = {"pass", "fail", "blocked", "not_tested"}
FEEDBACK_FILE = STORAGE_DIR / "step_feedback.json"
APPROVED_FILE = STORAGE_DIR / "approved.json"
LIBRARY_FILE = ROOT / "storage" / "global" / "step_library.json"


# ── Global step library ───────────────────────────────────────────────────────

def _load_library() -> dict:
    if LIBRARY_FILE.exists():
        try:
            return json.loads(LIBRARY_FILE.read_text())
        except Exception:
            return {}
    return {}


def _save_library(data: dict) -> None:
    LIBRARY_FILE.write_text(json.dumps(data, indent=2))


def _git_push_library():
    import subprocess
    remote = _git_remote_with_token()
    try:
        lib_path = str(LIBRARY_FILE.relative_to(ROOT))
        subprocess.run(["git", "-C", str(ROOT), "add", lib_path], check=True, capture_output=True)
        result = subprocess.run(["git", "-C", str(ROOT), "commit", "-m", "Update step library [auto]"], capture_output=True)
        if result.returncode != 0 and b"nothing to commit" not in result.stdout + result.stderr:
            return
        subprocess.run(["git", "-C", str(ROOT), "pull", "--rebase", remote, "master"], capture_output=True)
        push = subprocess.run(["git", "-C", str(ROOT), "push", remote, "master"], capture_output=True)
        if push.returncode == 0:
            print("[library] pushed to GitHub")
        else:
            print(f"[library] push failed: {push.stderr.decode()}")
    except Exception as exc:
        print(f"[library] git push error: {exc}")


def _load_approved() -> dict:
    if APPROVED_FILE.exists():
        try:
            return json.loads(APPROVED_FILE.read_text())
        except Exception:
            return {}
    return {}


def _save_approved(data: dict) -> None:
    APPROVED_FILE.write_text(json.dumps(data, indent=2))


def _git_remote_with_token() -> str:
    """Return the git remote URL with GITHUB_TOKEN injected for auth."""
    token = os.getenv("GITHUB_TOKEN", "")
    if not token:
        return "origin"
    try:
        import subprocess
        result = subprocess.run(
            ["git", "-C", str(ROOT), "remote", "get-url", "origin"],
            capture_output=True, text=True
        )
        url = result.stdout.strip()
        # inject token: https://github.com/... → https://token@github.com/...
        if url.startswith("https://") and "@" not in url:
            url = url.replace("https://", f"https://{token}@")
        return url
    except Exception:
        return "origin"


def _git_push_approved():
    import subprocess
    remote = _git_remote_with_token()
    try:
        paths = [
            str(APPROVED_FILE.relative_to(ROOT)),
            str(FEEDBACK_FILE.relative_to(ROOT)),
        ]
        for p in paths:
            subprocess.run(["git", "-C", str(ROOT), "add", p], check=True, capture_output=True)
        result = subprocess.run(["git", "-C", str(ROOT), "commit", "-m", "Update approved playbook [auto]"], capture_output=True)
        if result.returncode != 0 and b"nothing to commit" not in result.stdout + result.stderr:
            print(f"[approved] commit failed: {result.stderr.decode()}")
            return
        subprocess.run(["git", "-C", str(ROOT), "pull", "--rebase", remote, "master"], capture_output=True)
        push = subprocess.run(["git", "-C", str(ROOT), "push", remote, "master"], capture_output=True)
        if push.returncode == 0:
            print("[approved] pushed to GitHub")
        else:
            print(f"[approved] push failed: {push.stderr.decode()}")
    except Exception as exc:
        print(f"[approved] git push error: {exc}")


def _load_feedback() -> dict:
    if FEEDBACK_FILE.exists():
        try:
            return json.loads(FEEDBACK_FILE.read_text())
        except Exception:
            return {}
    return {}


def _save_feedback(data: dict) -> None:
    FEEDBACK_FILE.write_text(json.dumps(data, indent=2))


def _load_statuses() -> dict:
    if STATUS_FILE.exists():
        try:
            return json.loads(STATUS_FILE.read_text())
        except Exception:
            return {}
    return {}


def _save_statuses(data: dict) -> None:
    STATUS_FILE.write_text(json.dumps(data, indent=2))


def _step_status(scenario_id: str, step_id: str) -> str:
    return _load_statuses().get(scenario_id, {}).get(step_id, "not_tested")

app = FastAPI(title="EX3 TestOps")
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
app.mount("/runs", StaticFiles(directory=str(RUNS_DIR)), name="runs")


CATEGORY_RULES = [
    ("Pre-Requisites & System Access", lambda s: s.scenario_id.startswith("LOGIN")),
    ("Recruiting (RCM) — End-to-End Lifecycle", lambda s: s.scenario_id.startswith("RCM")),
    ("Employee Central (EC) — Core HR", lambda s: s.scenario_id.startswith("EC")),
]


def _load_scenarios():
    workbooks = sorted(SCRIPTS_DIR.glob("EX3_*_Workbook*.xlsx"))
    if not workbooks:
        return []
    scenarios = []
    for wb in workbooks:
        try:
            scenarios.extend(parse_workbook(str(wb)))
        except Exception as e:
            print(f"[loader] skipped {wb.name}: {e}")
    return scenarios


def _scenario_status(scenario_id: str, total_steps: int = 0) -> dict:
    """Return scenario-level status combining manual step marks + latest run."""
    manual = _load_statuses().get(scenario_id, {})
    statuses = list(manual.values())

    if "fail" in statuses:
        status = "fail"
    elif "blocked" in statuses:
        status = "blocked"
    elif statuses and all(s == "pass" for s in statuses) and len(statuses) >= total_steps and total_steps > 0:
        status = "pass"
    else:
        status = "not_tested"

    return {
        "status": status,
        "passed_steps": sum(1 for s in statuses if s == "pass"),
    }


def _role_color(role: str) -> str:
    palette = {
        "Recruiter": "blue",
        "Originator": "emerald",
        "Hiring Manager": "amber",
        "Candidate": "violet",
        "Approver": "rose",
    }
    return palette.get(role, "slate")


def _grouped_scenarios():
    scenarios = _load_scenarios()
    groups = defaultdict(list)
    for s in scenarios:
        for label, predicate in CATEGORY_RULES:
            if predicate(s):
                status = _scenario_status(s.scenario_id, total_steps=len(s.steps))
                groups[label].append({
                    "id": s.scenario_id,
                    "name": s.name,
                    "role": s.role,
                    "role_color": _role_color(s.role),
                    "step_count": len(s.steps),
                    **status,
                })
                break
    return [
        {
            "label": label,
            "scenarios": groups[label],
            "scenario_count": len(groups[label]),
            "step_count": sum(sc["step_count"] for sc in groups[label]),
        }
        for label, _ in CATEGORY_RULES
        if groups[label]
    ]


def _stats():
    scenarios = _load_scenarios()
    statuses = [
        _scenario_status(s.scenario_id, total_steps=len(s.steps))["status"]
        for s in scenarios
    ]
    return {
        "total": len(scenarios),
        "passing": statuses.count("pass"),
        "failing": statuses.count("fail"),
        "blocked": statuses.count("blocked"),
    }


@app.get("/", response_class=HTMLResponse)
def home(request: Request):
    return templates.TemplateResponse(
        request=request,
        name="index.html",
        context={
            "groups": _grouped_scenarios(),
            "stats": _stats(),
            "active": "all",
            "client_id": CLIENT_ID,
        },
    )


@app.get("/scenario/{scenario_id}", response_class=HTMLResponse)
def scenario_detail(request: Request, scenario_id: str):
    scenarios = _load_scenarios()
    scenario = next((s for s in scenarios if s.scenario_id == scenario_id), None)
    if not scenario:
        return HTMLResponse("Scenario not found", status_code=404)

    import re as _re
    runs = sorted(RUNS_DIR.iterdir(), reverse=True) if RUNS_DIR.exists() else []
    latest_run = None

    # Collect the most recent screenshot per step across ALL runs (for click-to-train).
    # Prefer _fail shots — they show exactly where it broke.
    step_screenshots: dict[str, str] = {}
    for run in runs:
        if not run.is_dir():
            continue
        for shot in sorted(run.glob(f"{scenario_id}-*.png")):
            base = _re.sub(r'_(fail|retry\d*)$', '', shot.stem)
            url = f"/runs/{run.name}/{shot.name}"
            if base not in step_screenshots or "_fail" in shot.stem:
                step_screenshots[base] = url

    for run in runs:
        if not run.is_dir():
            continue
        shots = sorted(run.glob(f"{scenario_id}-*.png"))
        if not shots:
            continue
        videos = sorted(run.glob("*.webm"))
        # Skip runs with no video AND no non-fail screenshots (incomplete abandoned runs)
        non_fail_shots = [s for s in shots if "_fail" not in s.stem]
        if not videos and not non_fail_shots:
            continue
        trace = run / "trace.zip"
        latest_run = {
            "id": run.name,
            "video_url": f"/runs/{run.name}/{videos[0].name}" if videos else None,
            "trace_url": f"/runs/{run.name}/trace.zip" if trace.exists() else None,
            "screenshots": [
                {
                    "url": f"/runs/{run.name}/{s.name}",
                    "step_id": s.stem,
                    "passed": True,
                }
                for s in shots
            ],
        }
        break

    statuses = _load_statuses().get(scenario_id, {})
    step_statuses = {step.step_id: statuses.get(step.step_id, "not_tested") for step in scenario.steps}

    feedback = _load_feedback().get(scenario_id, {})
    approved = _load_approved().get(scenario_id)

    return templates.TemplateResponse(
        request=request,
        name="scenario.html",
        context={
            "scenario": scenario,
            "role_color": _role_color(scenario.role),
            "run": latest_run,
            "stats": _stats(),
            "step_statuses": step_statuses,
            "step_feedback": feedback,
            "step_screenshots": step_screenshots,
            "approved": approved,
            "client_id": CLIENT_ID,
        },
    )


@app.get("/api/analyse/{scenario_id}")
def analyse_scenario_route(scenario_id: str):
    """Return pre-run analysis: data dependencies and questions to ask."""
    from engine.scenario_analyst import analyse_scenario
    scenarios = _load_scenarios()
    scenario = next((s for s in scenarios if s.scenario_id == scenario_id), None)
    if not scenario:
        raise HTTPException(404, "Scenario not found")
    analysis = analyse_scenario(scenario)

    # Filter out questions for steps that already have complete feedback written,
    # unless that feedback contains a {{placeholder}} (meaning the answer is still needed).
    existing_feedback = _load_feedback().get(scenario_id, {})
    def _needs_question(q: dict) -> bool:
        step_id = q.get("step_id", "")
        fb = existing_feedback.get(step_id, "")
        if not fb:
            return True  # no feedback written — question is relevant
        placeholder = "{{" + q.get("key", "") + "}}"
        return placeholder in fb  # only ask if feedback uses this placeholder
    analysis["questions"] = [q for q in analysis.get("questions", []) if _needs_question(q)]

    # Bulletproof fallback: scan every step's feedback for {{placeholder}} markers
    # and ensure each one has a question. If Claude's analyser missed it (or named
    # the key slightly differently), we still ask. Without this, a step with
    # TYPE: {{target_employee_name}} would type the literal placeholder text.
    import re as _re
    covered_keys = {q.get("key") for q in analysis["questions"]}
    for step_id, fb in existing_feedback.items():
        for match in _re.findall(r"\{\{(\w+)\}\}", fb or ""):
            if match in covered_keys:
                continue
            covered_keys.add(match)
            # Generate a friendly question from the key name
            human = match.replace("_", " ").strip().capitalize()
            analysis["questions"].append({
                "step_id": step_id,
                "key": match,
                "question": f"{human}?",
                "default": "",
            })

    return JSONResponse(analysis)


@app.post("/api/run/{scenario_id}")
async def trigger_run(scenario_id: str, request: Request):
    scenarios = _load_scenarios()
    scenario = next((s for s in scenarios if s.scenario_id == scenario_id), None)
    if not scenario:
        raise HTTPException(404, "Scenario not found")

    if _ACTIVE_RUNS.get(scenario_id, {}).get("status") == "running":
        return JSONResponse({"ok": False, "reason": "already running"}, status_code=409)

    # Accept optional pre-run answers (e.g. proxy_name, candidate_name) + supervised flag
    try:
        body = await request.json()
        pre_answers = {k: v for k, v in body.items() if k not in ("supervised", "live")} if isinstance(body, dict) else {}
        supervised = bool(body.get("supervised", False)) if isinstance(body, dict) else False
        live_mode = bool(body.get("live", False)) if isinstance(body, dict) else False
    except Exception:
        pre_answers = {}
        supervised = False
        live_mode = False

    run_id = datetime.utcnow().strftime("%Y%m%dT%H%M%SZ")
    _ACTIVE_RUNS[scenario_id] = {"status": "running", "run_id": run_id, "supervised": supervised, "live_mode": live_mode}

    # Live step log written to disk so it survives page reload
    step_log_file = RUNS_DIR / f"{scenario_id}_last_run.json"

    def _write_step_log(steps_so_far: list, run_status: str):
        try:
            step_log_file.write_text(json.dumps({
                "run_id": run_id,
                "status": run_status,
                "steps": steps_so_far,
            }, indent=2))
        except Exception:
            pass

    def _run():
        steps_log = []
        try:
            def _step_done_callback(step_id, passed, error, screenshot_url):
                steps_log.append({
                    "step_id": step_id,
                    "passed": passed,
                    "error": error or "",
                    "screenshot_url": screenshot_url or "",
                })
                _write_step_log(steps_log, "running")

            def _check_pause(sid):
                return _FORCE_PAUSE.pop(sid, False)

            def _step_confirm(step_id, screenshot_path):
                return _confirm_callback(scenario_id, step_id, screenshot_path, run_id)

            result = run_scenario(
                scenario, runs_root=RUNS_DIR, headless=True,
                pause_callback=lambda **kw: _pause_callback(**kw),
                initial_context=pre_answers,
                step_done_callback=_step_done_callback,
                check_pause_fn=_check_pause,
                step_confirm_callback=_step_confirm if supervised else None,
                live_mode=live_mode,
            )
            _write_step_log(steps_log, "done")
            _ACTIVE_RUNS[scenario_id] = {
                "status": "done",
                "run_id": result.run_id,
                "passed": result.passed,
            }
        except Exception as exc:
            import traceback
            print(f"[RUN ERROR] {scenario_id}: {exc}")
            traceback.print_exc()
            _write_step_log(steps_log, "error")
            _ACTIVE_RUNS[scenario_id] = {
                "status": "error",
                "run_id": run_id,
                "error": str(exc),
            }

    threading.Thread(target=_run, daemon=True).start()
    return JSONResponse({"ok": True, "run_id": run_id, "status": "running", "supervised": supervised})


@app.get("/api/run/{scenario_id}/status")
def run_status(scenario_id: str):
    return JSONResponse(_ACTIVE_RUNS.get(scenario_id, {"status": "idle"}))


@app.get("/api/run/{scenario_id}/steps")
def run_steps(scenario_id: str):
    """Return step-by-step results from the last run (persisted to disk)."""
    f = RUNS_DIR / f"{scenario_id}_last_run.json"
    if f.exists():
        try:
            return JSONResponse(json.loads(f.read_text()))
        except Exception:
            pass
    return JSONResponse({"steps": [], "status": "idle"})


@app.post("/api/run/{scenario_id}/confirm-step")
async def confirm_step(scenario_id: str, request: Request):
    """Supervised mode: user confirms the current step is done."""
    try:
        body = await request.json()
        confirmed = body.get("confirmed", True)
    except Exception:
        confirmed = True

    evt = _CONFIRM_EVENTS.get(scenario_id)
    if evt is None:
        return JSONResponse({"ok": False, "reason": "no step awaiting confirmation"}, status_code=400)

    _CONFIRM_RESULTS[scenario_id] = bool(confirmed)
    evt.set()
    return JSONResponse({"ok": True, "confirmed": confirmed})


EXPECTED_OVERRIDES_FILE = STORAGE_DIR / "expected_overrides.json"


def _load_overrides() -> dict:
    if EXPECTED_OVERRIDES_FILE.exists():
        try:
            return json.loads(EXPECTED_OVERRIDES_FILE.read_text())
        except Exception:
            return {}
    return {}


def _save_overrides(data: dict) -> None:
    EXPECTED_OVERRIDES_FILE.write_text(json.dumps(data, indent=2))


@app.get("/api/expected-override/{scenario_id}/{step_id}")
def get_expected_override(scenario_id: str, step_id: str):
    return JSONResponse({"override": _load_overrides().get(scenario_id, {}).get(step_id, "")})


@app.post("/api/expected-override")
def set_expected_override(scenario_id: str = Form(...), step_id: str = Form(...), override: str = Form(...)):
    data = _load_overrides()
    if override.strip():
        data.setdefault(scenario_id, {})[step_id] = override.strip()
    else:
        data.get(scenario_id, {}).pop(step_id, None)
        if scenario_id in data and not data[scenario_id]:
            data.pop(scenario_id)
    _save_overrides(data)
    return JSONResponse({"ok": True})


@app.post("/api/interpret-fix/{scenario_id}")
async def interpret_fix(scenario_id: str, request: Request):
    """Use Claude Vision to turn a plain-English description into runner commands."""
    body = await request.json()
    description = body.get("description", "").strip()
    screenshot_url = body.get("screenshot_url", "")
    step_id = body.get("step_id", "")

    key = os.getenv("ANTHROPIC_API_KEY")
    if not key or not description:
        return JSONResponse({"commands": "", "note": "no key or description"})

    try:
        import anthropic, base64
        client = anthropic.Anthropic(api_key=key)

        # Load the full scenario so Claude understands what the test is trying to do
        scenario_context = ""
        try:
            scenarios = _load_scenarios()
            scenario = next((s for s in scenarios if s.scenario_id == scenario_id), None)
            if scenario:
                lines = [f"Scenario: {scenario.scenario_id} — {scenario.name}"]
                lines.append(f"Role: {scenario.role}  |  Module: {scenario.module}")
                lines.append("")
                lines.append("Full test steps:")
                for i, step in enumerate(scenario.steps, 1):
                    marker = ">>> FAILED HERE <<<" if step.step_id == step_id else ""
                    lines.append(
                        f"  {i}. [{step.step_id}] {step.action}"
                        + (f"\n     Data: {step.test_data}" if step.test_data and step.test_data != "—" else "")
                        + f"\n     Expected: {step.expected_result}"
                        + (f"  {marker}" if marker else "")
                    )
                scenario_context = "\n".join(lines)
        except Exception:
            pass

        content: list = []

        # Attach screenshot if available
        if screenshot_url:
            parts = screenshot_url.strip("/").split("/")
            if len(parts) >= 3 and parts[0] == "runs":
                shot_path = RUNS_DIR / parts[1] / parts[2]
                if shot_path.exists():
                    img_b64 = base64.standard_b64encode(shot_path.read_bytes()).decode()
                    content.append({
                        "type": "image",
                        "source": {"type": "base64", "media_type": "image/png", "data": img_b64},
                    })

        content.append({
            "type": "text",
            "text": f"""You are an expert SAP SuccessFactors automation engineer.
You control a SuccessFactors browser (1280x720) via Playwright and must fix a failed test step.

{scenario_context}

The step that failed: {step_id}
The tester's fix description: {description}

Generate ONLY the Playwright commands to carry out this fix. Available commands (one per line):
  GOTO: /sf/start#...
  CLICK: button text or visible label
  CLICK_XY: x, y  (pixel coords on 1280x720)
  TYPE: text to type
  PRESS: Key (Enter, ArrowDown, Escape, Tab)
  WAIT: milliseconds
  JS: javascript expression
  FILL: selector | value
  SHADOW_CLICK: visible label (for elements inside Shadow DOM)
  NAVIGATE: module name (e.g. Recruiting, Compensation)

Rules:
- Use the full scenario context to understand what page should be open and what the step is trying to do.
- If the screenshot shows a specific UI state, use it to inform exact coordinates.
- If the tester mentions a position ("top right", "bottom of the popup"), use CLICK_XY with coords estimated from the screenshot.
- If they name a button or link, use CLICK: that exact label.
- Add WAIT: 1000 after clicks that open dialogs or trigger navigation.
- Output ONLY commands, no explanations, no markdown fences.
- Maximum 8 commands.""",
        })

        msg = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=1024,
            system=(
                "You are an expert in SAP SuccessFactors Recruiting & Compensation modules and Playwright automation. "
                "You have deep knowledge of the SuccessFactors UI — its navigation, shadow DOM structure, iframes, "
                "and common interaction patterns. When given a failed test step and a human description of the fix, "
                "you generate precise, working Playwright commands. You understand SuccessFactors well enough to "
                "reason about what is likely on screen even without a screenshot."
            ),
            messages=[{"role": "user", "content": content}],
        )
        commands = msg.content[0].text.strip()
        if commands.startswith("```"):
            commands = "\n".join(commands.split("\n")[1:-1]).strip()
        return JSONResponse({"commands": commands})
    except Exception as exc:
        print(f"[interpret-fix] error: {exc}")
        return JSONResponse({"commands": "", "error": str(exc)})


@app.post("/api/run/{scenario_id}/cancel")
def cancel_run(scenario_id: str):
    """Force-reset a stuck or paused run."""
    if scenario_id in _PAUSE_EVENTS:
        _PAUSE_FIX[scenario_id] = None
        _PAUSE_EVENTS[scenario_id].set()
    # Also release any pending supervised confirmation
    if scenario_id in _CONFIRM_EVENTS:
        _CONFIRM_RESULTS[scenario_id] = False
        _CONFIRM_EVENTS[scenario_id].set()
    _ACTIVE_RUNS.pop(scenario_id, None)
    return JSONResponse({"ok": True})


@app.post("/api/run/{scenario_id}/resume")
async def resume_run(scenario_id: str, request: Request):
    body = await request.json()
    commands = body.get("commands", "").strip()
    comment = body.get("comment", "").strip()
    save_feedback = body.get("save_feedback", True)

    if scenario_id not in _PAUSE_EVENTS:
        raise HTTPException(400, "No paused run for this scenario")

    _PAUSE_FIX[scenario_id] = {"commands": commands, "comment": comment}

    # Only save as step feedback if there was NO existing feedback for this step.
    # If there WAS feedback and it still failed, the resume fix is a one-off correction —
    # don't overwrite the stored sequence or future runs will lose the full command set.
    if save_feedback and commands:
        paused_step = _ACTIVE_RUNS.get(scenario_id, {}).get("paused_step")
        if paused_step:
            data = _load_feedback()
            existing = data.get(scenario_id, {}).get(paused_step, "")
            if not existing:
                data.setdefault(scenario_id, {})[paused_step] = commands
                _save_feedback(data)

    _PAUSE_EVENTS[scenario_id].set()
    return JSONResponse({"ok": True})


# ── Live control endpoints ──────────────────────────────────────────────────────

@app.get("/api/live/{scenario_id}/screenshot")
def live_screenshot(scenario_id: str):
    """Serve the latest screenshot written by the runner's live-control loop."""
    from fastapi.responses import Response
    shot_path = _LIVE_SHOT_PATHS.get(scenario_id)
    if not shot_path or not shot_path.exists():
        raise HTTPException(404, "No live screenshot available — is the run paused?")
    return Response(content=shot_path.read_bytes(), media_type="image/png")


@app.post("/api/live/{scenario_id}/click")
async def live_click(scenario_id: str, request: Request):
    """Queue a click for the runner's live loop to execute."""
    if scenario_id not in _LIVE_QUEUES:
        raise HTTPException(404, "No live session for this scenario")
    body = await request.json()
    _LIVE_QUEUES[scenario_id].append({"type": "click", "x": int(body["x"]), "y": int(body["y"])})
    return JSONResponse({"ok": True})


@app.post("/api/live/{scenario_id}/type")
async def live_type(scenario_id: str, request: Request):
    """Queue a type action for the runner's live loop."""
    if scenario_id not in _LIVE_QUEUES:
        raise HTTPException(404, "No live session for this scenario")
    body = await request.json()
    _LIVE_QUEUES[scenario_id].append({"type": "type", "text": body.get("text", "")})
    return JSONResponse({"ok": True})


@app.post("/api/live/{scenario_id}/key")
async def live_key(scenario_id: str, request: Request):
    """Queue a key press for the runner's live loop."""
    if scenario_id not in _LIVE_QUEUES:
        raise HTTPException(404, "No live session for this scenario")
    body = await request.json()
    _LIVE_QUEUES[scenario_id].append({"type": "key", "key": body.get("key", "")})
    return JSONResponse({"ok": True})


@app.post("/api/live/{scenario_id}/scroll")
async def live_scroll(scenario_id: str, request: Request):
    """Queue a scroll action for the runner's live loop."""
    if scenario_id not in _LIVE_QUEUES:
        raise HTTPException(404, "No live session for this scenario")
    body = await request.json()
    _LIVE_QUEUES[scenario_id].append({"type": "scroll", "px": int(body.get("px", 400))})
    return JSONResponse({"ok": True})


@app.post("/api/live/{scenario_id}/done")
async def live_done(scenario_id: str, request: Request):
    """User finished live control — save recorded commands and resume runner."""
    body = await request.json()
    commands = body.get("commands", "").strip()

    if scenario_id not in _PAUSE_EVENTS:
        raise HTTPException(400, "No paused run for this scenario")

    # Save as step feedback if we recorded anything
    if commands:
        paused_step = _ACTIVE_RUNS.get(scenario_id, {}).get("paused_step")
        if paused_step:
            data = _load_feedback()
            data.setdefault(scenario_id, {})[paused_step] = commands
            _save_feedback(data)
            import threading as _t
            _t.Thread(target=_git_push_feedback, daemon=True).start()

    _PAUSE_FIX[scenario_id] = {"skip": True}
    _PAUSE_EVENTS[scenario_id].set()
    return JSONResponse({"ok": True})


@app.post("/api/live/{scenario_id}/request-control")
async def request_control(scenario_id: str, request: Request):
    """Set force-pause flag so runner pauses before the next step.
    If no run is active, starts one first.
    """
    body = await request.json()
    pre_answers = body.get("answers", {})

    status = _ACTIVE_RUNS.get(scenario_id, {}).get("status", "idle")

    if status == "paused":
        # Already paused — nothing to do, UI will open live control directly
        return JSONResponse({"ok": True, "status": "paused"})

    # Set the flag — runner will pause before the next step
    _FORCE_PAUSE[scenario_id] = True

    if status not in ("running",):
        # Not running — start a fresh run
        scenarios = _load_scenarios()
        scenario = next((s for s in scenarios if s.scenario_id == scenario_id), None)
        if not scenario:
            raise HTTPException(404, "Scenario not found")

        # Cancel any stuck run first
        if scenario_id in _PAUSE_EVENTS:
            _PAUSE_FIX[scenario_id] = None
            _PAUSE_EVENTS[scenario_id].set()

        run_id = datetime.utcnow().strftime("%Y%m%dT%H%M%SZ")
        _ACTIVE_RUNS[scenario_id] = {"status": "running", "run_id": run_id}
        step_log_file = RUNS_DIR / f"{scenario_id}_last_run.json"

        def _run():
            steps_log = []
            try:
                def _step_done(step_id, passed, error, screenshot_url):
                    steps_log.append({"step_id": step_id, "passed": passed,
                                      "error": error or "", "screenshot_url": screenshot_url or ""})
                    try:
                        step_log_file.write_text(json.dumps({"run_id": run_id, "status": "running", "steps": steps_log}, indent=2))
                    except Exception:
                        pass

                def _check_pause(sid):
                    return _FORCE_PAUSE.pop(sid, False)

                result = run_scenario(scenario, runs_root=RUNS_DIR, headless=True,
                                      pause_callback=lambda **kw: _pause_callback(**kw),
                                      initial_context=pre_answers,
                                      step_done_callback=_step_done,
                                      check_pause_fn=_check_pause)
                try:
                    step_log_file.write_text(json.dumps({"run_id": run_id, "status": "done", "steps": steps_log}, indent=2))
                except Exception:
                    pass
                _ACTIVE_RUNS[scenario_id] = {"status": "done", "run_id": result.run_id, "passed": result.passed}
            except Exception as exc:
                _ACTIVE_RUNS[scenario_id] = {"status": "error", "run_id": run_id, "error": str(exc)}

        import threading as _t
        _t.Thread(target=_run, daemon=True).start()

    return JSONResponse({"ok": True, "status": "starting"})


def _git_push_feedback():
    """Commit and push feedback file to GitHub in the background."""
    import subprocess
    remote = _git_remote_with_token()
    try:
        feedback_path = str(FEEDBACK_FILE.relative_to(ROOT))
        subprocess.run(["git", "-C", str(ROOT), "add", feedback_path], check=True, capture_output=True)
        result = subprocess.run(["git", "-C", str(ROOT), "commit", "-m", "Update step feedback [auto]"], capture_output=True)
        if result.returncode != 0 and b"nothing to commit" not in result.stdout + result.stderr:
            print(f"[feedback] commit failed: {result.stderr.decode()}")
            return
        # Pull remote changes first so our push isn't rejected
        subprocess.run(["git", "-C", str(ROOT), "pull", "--rebase", remote, "master"], capture_output=True)
        push = subprocess.run(["git", "-C", str(ROOT), "push", remote, "master"], capture_output=True)
        if push.returncode == 0:
            print("[feedback] pushed to GitHub")
        else:
            print(f"[feedback] push failed: {push.stderr.decode()}")
    except Exception as exc:
        print(f"[feedback] git push error: {exc}")


@app.get("/click/{scenario_id}/{step_id}", response_class=HTMLResponse)
def click_trainer(scenario_id: str, step_id: str, live: bool = False):
    """Full-screen click trainer. In live=True mode the screenshot streams from
    the running browser and clicks are sent to it in real time."""
    import re as _re

    # Get step action text for the header
    step_action = ""
    try:
        scenarios = _load_scenarios()
        sc = next((s for s in scenarios if s.scenario_id == scenario_id), None)
        if sc:
            st = next((s for s in sc.steps if s.step_id == step_id), None)
            if st:
                step_action = st.action
    except Exception:
        pass

    feedback_data = _load_feedback()
    # In live mode start with a blank slate — old commands may be broken (that's
    # why we're here). Only pre-fill in static/teach mode.
    current_feedback = "" if live else feedback_data.get(scenario_id, {}).get(step_id, "")

    # Static mode: find latest screenshot from a past run
    img_url = f"/api/live/{scenario_id}/screenshot" if live else ""
    if not live:
        runs = sorted(RUNS_DIR.iterdir(), reverse=True) if RUNS_DIR.exists() else []
        for run in runs:
            if not run.is_dir():
                continue
            for shot in sorted(run.glob(f"{step_id}*.png"), reverse=True):
                img_url = f"/runs/{run.name}/{shot.name}"
                break
            if img_url:
                break
        if not img_url:
            return HTMLResponse(f"<h2>No screenshot found for {step_id}</h2>", status_code=404)

    is_done_step = step_id == "__done__"
    live_js = ""
    if live:
        live_js = f"""
  // ── Live mode ──────────────────────────────────────────────────────────────
  let _refreshTimer = null;
  function _refreshShot() {{
    fetch(`/api/live/{scenario_id}/screenshot?t=${{Date.now()}}`)
      .then(r => {{ if (!r.ok) throw r.status; return r.blob(); }})
      .then(b => {{ shot.src = URL.createObjectURL(b); }})
      .catch(() => {{}});
  }}
  _refreshShot();
  _refreshTimer = setInterval(_refreshShot, 5000);

  // In live mode clicks ALSO fire on the real browser
  const _origClick = wrap.onclick;
  wrap.addEventListener('click', async (e) => {{
    const rect = shot.getBoundingClientRect();
    const x = Math.round((e.clientX - rect.left) * (1280 / rect.width));
    const y = Math.round((e.clientY - rect.top)  * (720  / rect.height));
    await fetch(`/api/live/{scenario_id}/click`, {{
      method:'POST', headers:{{'Content-Type':'application/json'}},
      body: JSON.stringify({{x, y}}),
    }});
    setTimeout(_refreshShot, 600);
  }});

  // Mouse wheel → scroll live browser
  wrap.addEventListener('wheel', async (e) => {{
    e.preventDefault();
    const key = e.deltaY > 0 ? 'PageDown' : 'PageUp';
    await fetch(`/api/live/{scenario_id}/key`, {{
      method:'POST', headers:{{'Content-Type':'application/json'}},
      body: JSON.stringify({{key}}),
    }});
    setTimeout(_refreshShot, 600);
  }}, {{passive: false}});

  // Arrow / Page keys scroll live browser (only when not typing in textarea)
  document.addEventListener('keydown', async (e) => {{
    if (document.activeElement === cmdOut) return;
    if (!['ArrowDown','ArrowUp','PageDown','PageUp'].includes(e.key)) return;
    e.preventDefault();
    await fetch(`/api/live/{scenario_id}/key`, {{
      method:'POST', headers:{{'Content-Type':'application/json'}},
      body: JSON.stringify({{key: e.key}}),
    }});
    setTimeout(_refreshShot, 600);
  }});

  async function saveAndContinue() {{
    const btn = document.getElementById('continue-btn');
    btn.disabled = true; btn.textContent = 'Saving…';
    const text = cmdOut.value.trim();
    // Save permanently
    if (text) {{
      const fd = new FormData();
      fd.append('scenario_id', '{scenario_id}');
      fd.append('step_id', '{step_id}');
      fd.append('feedback', text);
      await fetch('/api/step-feedback', {{method:'POST', body:fd}});
    }}
    // Signal runner to advance
    await fetch(`/api/live/{scenario_id}/done`, {{
      method:'POST', headers:{{'Content-Type':'application/json'}},
      body: JSON.stringify({{commands: text}}),
    }});
    btn.textContent = 'Waiting…';
    // Poll for next paused live step or completion
    const poll = setInterval(async () => {{
      try {{
        const d = await (await fetch(`/api/run/{scenario_id}/status`)).json();
        if (d.status === 'paused' && d.live_step) {{
          clearInterval(poll);
          if (d.paused_step === '__done__') {{
            // All scripted steps done — stay open, let user keep clicking
            _showFinishState();
          }} else {{
            window.location.href = `/click/{scenario_id}/${{d.paused_step}}?live=1`;
          }}
        }} else if (d.status === 'paused' && !d.live_step) {{
          clearInterval(poll); clearInterval(_refreshTimer);
          window.location.href = '/scenario/{scenario_id}';
        }} else if (d.status === 'done' || d.status === 'error') {{
          // Should only happen if runner finishes without the __done__ hold
          clearInterval(poll); clearInterval(_refreshTimer);
          window.location.href = '/scenario/{scenario_id}';
        }}
      }} catch(e) {{}}
    }}, 1500);
  }}

  // Scroll buttons
  async function scrollPage(px) {{
    await fetch(`/api/live/{scenario_id}/scroll`, {{
      method:'POST', headers:{{'Content-Type':'application/json'}},
      body: JSON.stringify({{px}}),
    }});
    setTimeout(_refreshShot, 600);
  }}

  // Type into live browser
  async function sendType() {{
    const inp = document.getElementById('type-input');
    const text = inp.value;
    if (!text) return;
    await fetch(`/api/live/{scenario_id}/type`, {{
      method:'POST', headers:{{'Content-Type':'application/json'}},
      body: JSON.stringify({{text}}),
    }});
    // Also record it in the commands log
    const cur = cmdOut.value.trim();
    cmdOut.value = cur ? cur + '\\nTYPE: ' + text : 'TYPE: ' + text;
    inp.value = '';
    setTimeout(_refreshShot, 700);
  }}

  // Enter in type input fires sendType
  document.getElementById('type-input')?.addEventListener('keydown', (e) => {{
    if (e.key === 'Enter') {{ e.preventDefault(); sendType(); }}
  }});

  function _showFinishState() {{
    const h1 = document.querySelector('#header h1');
    if (h1) h1.innerHTML = 'All steps complete ✔ &nbsp;<span style="color:#9ca3af;font-weight:normal;font-size:11px;">Keep clicking if needed, then click Finish</span>';
    const btn = document.getElementById('continue-btn');
    if (btn) {{
      btn.textContent = 'Finish →';
      btn.style.background = '#7c3aed';
      btn.disabled = false;
      btn.onclick = async () => {{
        btn.disabled = true; btn.textContent = 'Saving…';
        await fetch(`/api/live/{scenario_id}/done`, {{
          method:'POST', headers:{{'Content-Type':'application/json'}},
          body: JSON.stringify({{commands: ''}}),
        }});
        // Lock all recorded commands as the permanent approved playbook
        await fetch(`/api/scenario/{scenario_id}/approve`, {{method:'POST'}});
        window.location.href = '/scenario/{scenario_id}';
      }};
    }}
  }}
"""

    if not live:
        save_btn = ('<button id="save-btn" onclick="saveCommands()" '
                    'style="background:#7c3aed;color:#fff;border:none;padding:6px 14px;border-radius:4px;cursor:pointer;font-size:12px;font-family:monospace;">Save &amp; close</button>')
    elif is_done_step:
        save_btn = ('<button id="continue-btn" onclick="saveAndContinue()" '
                    'style="background:#7c3aed;color:#fff;border:none;padding:6px 18px;border-radius:4px;cursor:pointer;font-size:13px;font-weight:bold;">Finish →</button>')
    else:
        save_btn = ('<button id="continue-btn" onclick="saveAndContinue()" '
                    'style="background:#16a34a;color:#fff;border:none;padding:6px 18px;border-radius:4px;cursor:pointer;font-size:13px;font-weight:bold;">Save &amp; Continue →</button>')

    step_label = ('All steps complete ✔' if is_done_step
                  else f'Teach Step {step_id}' if not live
                  else f'Step {step_id}')
    banner_extra = (
        ' <span style="color:#9ca3af;font-weight:normal;font-size:11px;">Keep clicking if needed, then Finish</span>'
        if is_done_step else
        f' — <span style="color:#9ca3af;font-weight:normal;">{step_action[:100]}</span>' if step_action else ''
    )

    return HTMLResponse(f"""<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<title>{'Live — ' if live else ''}{step_id}</title>
<style>
  * {{ margin:0; padding:0; box-sizing:border-box; }}
  body {{ background:#111; color:#fff; font-family:monospace; display:flex; flex-direction:column; height:100vh; }}
  #header {{ background:#1a1a1a; border-bottom:1px solid #333; padding:8px 16px; display:flex; align-items:center; gap:12px; flex-shrink:0; flex-wrap:wrap; }}
  #header h1 {{ font-size:13px; color:#fff; font-weight:bold; }}
  #coords {{ background:#000; color:#0f0; font-size:13px; font-weight:bold; padding:3px 8px; border-radius:4px; min-width:120px; text-align:center; }}
  #img-wrap {{ flex:1; overflow:auto; display:flex; align-items:flex-start; justify-content:center; padding:8px; position:relative; cursor:crosshair; }}
  #shot {{ max-width:100%; display:block; user-select:none; }}
  .dot {{ position:absolute; width:18px; height:18px; background:#ef4444; border:2px solid #fff; border-radius:50%; transform:translate(-50%,-50%); pointer-events:none; }}
  .dot-label {{ position:absolute; background:#ef4444; color:#fff; font-size:10px; padding:1px 4px; border-radius:3px; transform:translate(8px,-50%); pointer-events:none; white-space:nowrap; }}
  #cmd-panel {{ background:#1a1a1a; border-top:1px solid #333; padding:8px 16px; flex-shrink:0; display:flex; align-items:flex-start; gap:8px; }}
  #cmd-out {{ flex:1; background:#000; color:#0f0; font-size:12px; padding:6px 10px; border-radius:4px; border:1px solid #333; min-height:40px; resize:vertical; font-family:monospace; }}
  #status {{ font-size:11px; color:#aaa; }}
  {'#live-badge{background:#16a34a;color:#fff;font-size:10px;padding:2px 7px;border-radius:10px;font-weight:bold;letter-spacing:.05em;} #type-row{display:flex;align-items:center;gap:6px;background:#0d1117;border-top:1px solid #333;padding:5px 16px;flex-shrink:0;} #type-input{flex:1;background:#000;color:#0f0;font-size:12px;padding:4px 8px;border-radius:4px;border:1px solid #555;font-family:monospace;} #type-input::placeholder{color:#555;}' if live else ''}
</style>
</head>
<body>
<div id="header">
  {'<span id="live-badge">● LIVE</span>' if live else ''}
  <h1>{step_label}{banner_extra}</h1>
  <div id="coords">click image</div>
  <button onclick="clearDots()" style="background:#374151;color:#fff;border:none;padding:3px 10px;border-radius:4px;cursor:pointer;font-size:11px;">Clear dots</button>
  <span id="status" style="font-size:11px;color:#aaa;"></span>
  {'<div style="flex:1"></div>' if live else ''}
  {save_btn}
  {'<button onclick="window.location.href=\'/scenario/' + scenario_id + '\'" style="background:#374151;color:#fff;border:none;padding:6px 12px;border-radius:4px;cursor:pointer;font-size:12px;">← Back</button>' if live else ''}
</div>
{'<div id="type-row"><span style="color:#9ca3af;font-size:11px;white-space:nowrap;">Type:</span><input id="type-input" type="text" placeholder="click a field first, then type here and press Enter" autocomplete="off" /><button onclick="sendType()" style="background:#2563eb;color:#fff;border:none;padding:4px 12px;border-radius:4px;cursor:pointer;font-size:11px;font-family:monospace;white-space:nowrap;">Type →</button><button onclick="scrollPage(-400)" style="background:#374151;color:#fff;border:none;padding:4px 14px;border-radius:4px;cursor:pointer;font-size:14px;" title="Scroll up">↑</button><button onclick="scrollPage(400)" style="background:#374151;color:#fff;border:none;padding:4px 14px;border-radius:4px;cursor:pointer;font-size:14px;" title="Scroll down">↓</button><button onclick="scrollPage(800)" style="background:#374151;color:#fff;border:none;padding:4px 10px;border-radius:4px;cursor:pointer;font-size:11px;white-space:nowrap;" title="Scroll down a lot">↓↓</button></div>' if live else ''}
<div id="img-wrap">
  <img id="shot" src="{img_url}" draggable="false" />
</div>
<div id="cmd-panel">
  <textarea id="cmd-out" rows="3" spellcheck="false">{current_feedback or ""}</textarea>
  {save_btn if not live else ""}
</div>

<script>
  const SCENARIO_ID = "{scenario_id}";
  const STEP_ID = "{step_id}";
  let lastX = 0, lastY = 0;
  const wrap = document.getElementById('img-wrap');
  const shot = document.getElementById('shot');
  const coords = document.getElementById('coords');
  const cmdOut = document.getElementById('cmd-out');

  wrap.addEventListener('click', (e) => {{
    if (e.target !== shot) return;
    const rect = shot.getBoundingClientRect();
    const x = Math.round((e.clientX - rect.left) * (1280 / rect.width));
    const y = Math.round((e.clientY - rect.top)  * (720  / rect.height));
    lastX = x; lastY = y;
    coords.textContent = x + ', ' + y;
    const dot = document.createElement('div'); dot.className = 'dot';
    dot.style.left = (e.clientX - wrap.getBoundingClientRect().left) + 'px';
    dot.style.top  = (e.clientY - wrap.getBoundingClientRect().top)  + 'px';
    const lbl = document.createElement('div'); lbl.className = 'dot-label';
    lbl.style.left = dot.style.left; lbl.style.top = dot.style.top;
    lbl.textContent = x + ',' + y;
    wrap.appendChild(dot); wrap.appendChild(lbl);
    // Append click to commands textarea
    const cur = cmdOut.value.trim();
    cmdOut.value = cur ? cur + '\\nCLICK_XY: ' + x + ', ' + y : 'CLICK_XY: ' + x + ', ' + y;
  }});

  function clearDots() {{
    wrap.querySelectorAll('.dot,.dot-label').forEach(el => el.remove());
  }}

  async function saveCommands() {{
    const text = cmdOut.value.trim();
    if (!text) return;
    const fd = new FormData();
    fd.append('scenario_id', SCENARIO_ID);
    fd.append('step_id', STEP_ID);
    fd.append('feedback', text);
    const res = await fetch('/api/step-feedback', {{ method: 'POST', body: fd }});
    if (res.ok) {{
      document.getElementById('status').textContent = 'saved!';
      setTimeout(() => window.close(), 800);
    }}
  }}

  {live_js}
</script>
</body>
</html>""")


@app.post("/api/scenario/{scenario_id}/approve")
def approve_scenario(scenario_id: str):
    """Lock the current feedback as the golden playbook — used on every future run."""
    feedback = _load_feedback().get(scenario_id, {})
    approved = _load_approved()
    approved[scenario_id] = {
        "approved_at": datetime.utcnow().isoformat(),
        "step_commands": feedback,
    }
    _save_approved(approved)
    threading.Thread(target=_git_push_approved, daemon=True).start()
    return JSONResponse({"ok": True})


@app.post("/api/scenario/{scenario_id}/unapprove")
def unapprove_scenario(scenario_id: str):
    """Remove the golden playbook so the scenario goes back to normal mode."""
    approved = _load_approved()
    approved.pop(scenario_id, None)
    _save_approved(approved)
    threading.Thread(target=_git_push_approved, daemon=True).start()
    return JSONResponse({"ok": True})


@app.post("/api/step-feedback")
def set_step_feedback(
    scenario_id: str = Form(...),
    step_id: str = Form(...),
    feedback: str = Form(...),
    push: str = Form("true"),   # "false" = save locally only, no git push
):
    data = _load_feedback()
    if feedback.strip():
        data.setdefault(scenario_id, {})[step_id] = feedback.strip()
    else:
        data.get(scenario_id, {}).pop(step_id, None)
    _save_feedback(data)
    if push.lower() != "false":
        threading.Thread(target=_git_push_feedback, daemon=True).start()
    return JSONResponse({"ok": True})


@app.post("/api/step-status")
def set_step_status(scenario_id: str = Form(...), step_id: str = Form(...), status: str = Form(...)):
    if status not in VALID_STATUSES:
        raise HTTPException(400, f"Invalid status; must be one of {VALID_STATUSES}")
    data = _load_statuses()
    data.setdefault(scenario_id, {})[step_id] = status
    if status == "not_tested":
        data[scenario_id].pop(step_id, None)
        if not data[scenario_id]:
            data.pop(scenario_id)
    _save_statuses(data)
    return JSONResponse({"ok": True, "scenario_id": scenario_id, "step_id": step_id, "status": status})


# ── Step Library ──────────────────────────────────────────────────────────────

@app.get("/library", response_class=HTMLResponse)
def library_page(request: Request):
    try:
        st = _stats()
    except Exception:
        st = {"total": 0, "passing": 0, "failing": 0, "blocked": 0}
    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8"/>
  <meta name="viewport" content="width=device-width,initial-scale=1.0"/>
  <title>Task Library — EX3 TestOps</title>
  <link rel="preconnect" href="https://fonts.googleapis.com">
  <link href="https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700&display=swap" rel="stylesheet">
  <style>
    *,*::before,*::after{{box-sizing:border-box;margin:0;padding:0}}
    html,body{{height:100%}}
    body{{font-family:'Inter',-apple-system,sans-serif;background:#f5f5f7;color:#1d1d1f;font-size:14px;line-height:1.5;-webkit-font-smoothing:antialiased}}
    a{{text-decoration:none;color:inherit}}
    .app{{display:flex;min-height:100vh}}
    .sidebar{{width:230px;flex-shrink:0;background:#1d1d1f;display:flex;flex-direction:column;position:sticky;top:0;height:100vh}}
    .sidebar-brand{{padding:22px 20px 18px;border-bottom:1px solid rgba(255,255,255,0.07);display:flex;align-items:center;gap:12px}}
    .brand-icon{{width:32px;height:32px;background:#0071e3;border-radius:8px;display:flex;align-items:center;justify-content:center;flex-shrink:0}}
    .brand-name{{font-size:14px;font-weight:600;color:#f5f5f7}}
    .brand-sub{{font-size:11px;color:rgba(255,255,255,0.35);margin-top:1px}}
    .client-chip{{margin:12px 20px 0;display:inline-flex;align-items:center;gap:6px;background:rgba(255,255,255,0.06);border:1px solid rgba(255,255,255,0.1);border-radius:6px;padding:4px 10px;font-size:11px;font-family:monospace;color:rgba(255,255,255,0.45)}}
    .client-chip::before{{content:'';width:6px;height:6px;border-radius:50%;background:#34c759;flex-shrink:0}}
    .sidebar-nav{{flex:1;overflow-y:auto;padding:16px 12px}}
    .nav-label{{font-size:10px;font-weight:600;letter-spacing:.08em;text-transform:uppercase;color:rgba(255,255,255,0.25);padding:0 8px 7px}}
    .nav-section{{margin-bottom:22px}}
    .nav-item{{display:flex;align-items:center;justify-content:space-between;padding:8px 10px;border-radius:8px;font-size:13px;color:rgba(255,255,255,0.55);transition:all .15s;margin-bottom:1px}}
    .nav-item:hover{{background:rgba(255,255,255,0.07);color:rgba(255,255,255,0.85)}}
    .nav-item.active{{background:rgba(0,113,227,0.25);color:#fff;font-weight:500}}
    .nav-item-left{{display:flex;align-items:center;gap:9px}}
    .nav-icon{{opacity:.6;flex-shrink:0}}
    .nav-count{{font-size:11px;color:rgba(255,255,255,0.3)}}
    .nav-dot{{width:7px;height:7px;border-radius:50%;flex-shrink:0}}
    .dot-green{{background:#34c759}}.dot-red{{background:#ff3b30}}.dot-orange{{background:#ff9500}}
    .main{{flex:1;display:flex;flex-direction:column;min-width:0}}
    .topbar{{background:rgba(255,255,255,0.85);backdrop-filter:blur(20px);border-bottom:1px solid rgba(0,0,0,0.07);padding:0 28px;height:52px;display:flex;align-items:center;position:sticky;top:0;z-index:100}}
    .breadcrumb{{display:flex;align-items:center;gap:6px;font-size:12px}}
    .breadcrumb a{{color:#6e6e73}}.breadcrumb a:hover{{color:#1d1d1f}}
    .breadcrumb-sep{{color:#c7c7cc;font-size:11px}}
    .breadcrumb-current{{color:#1d1d1f;font-weight:500}}
    .content{{flex:1;overflow-y:auto;padding:32px}}
    .content-inner{{max-width:880px;margin:0 auto}}
    .badge{{display:inline-flex;align-items:center;padding:2px 8px;border-radius:5px;font-size:11px;font-weight:500}}
    .badge-blue{{background:#e8f1fb;color:#0050a0;border:1px solid rgba(0,113,227,0.2)}}
    .btn{{display:inline-flex;align-items:center;gap:6px;padding:7px 16px;border-radius:8px;font-size:13px;font-weight:500;border:none;cursor:pointer;font-family:inherit}}
    .btn-danger{{background:rgba(255,59,48,0.1);color:#d70015;border:1px solid rgba(255,59,48,0.2)}}
    .btn-danger:hover{{background:rgba(255,59,48,0.15)}}
    .task-card{{background:#fff;border-radius:14px;box-shadow:0 1px 3px rgba(0,0,0,0.06);padding:22px 24px;margin-bottom:12px}}
  </style>
</head>
<body>
<div class="app">
  <aside class="sidebar">
    <div class="sidebar-brand">
      <div class="brand-icon"><svg width="16" height="16" fill="none" viewBox="0 0 24 24" stroke="#fff" stroke-width="2.5"><path stroke-linecap="round" stroke-linejoin="round" d="M9 12l2 2 4-4m6 2a9 9 0 11-18 0 9 9 0 0118 0z"/></svg></div>
      <div><div class="brand-name">EX3 TestOps</div><div class="brand-sub">SuccessFactors UAT</div></div>
    </div>
    <div class="client-chip">{CLIENT_ID}</div>
    <nav class="sidebar-nav">
      <div class="nav-section">
        <div class="nav-label">Overview</div>
        <a href="/" class="nav-item"><span class="nav-item-left"><svg class="nav-icon" width="15" height="15" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="1.8"><rect x="3" y="3" width="7" height="7" rx="1.5"/><rect x="14" y="3" width="7" height="7" rx="1.5"/><rect x="3" y="14" width="7" height="7" rx="1.5"/><rect x="14" y="14" width="7" height="7" rx="1.5"/></svg>Dashboard</span></a>
      </div>
      <div class="nav-section">
        <div class="nav-label">Test Scripts</div>
        <a href="/" class="nav-item"><span class="nav-item-left"><svg class="nav-icon" width="15" height="15" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="1.8"><path stroke-linecap="round" stroke-linejoin="round" d="M9 5H7a2 2 0 00-2 2v12a2 2 0 002 2h10a2 2 0 002-2V7a2 2 0 00-2-2h-2M9 5a2 2 0 002 2h2a2 2 0 002-2M9 5a2 2 0 012-2h2a2 2 0 012 2"/></svg>All Test Cases</span><span class="nav-count">{st['total']}</span></a>
        <a href="/?filter=pass" class="nav-item"><span class="nav-item-left"><span class="nav-dot dot-green"></span>Passing</span><span class="nav-count">{st['passing']}</span></a>
        <a href="/?filter=fail" class="nav-item"><span class="nav-item-left"><span class="nav-dot dot-red"></span>Failing</span><span class="nav-count">{st['failing']}</span></a>
        <a href="/?filter=blocked" class="nav-item"><span class="nav-item-left"><span class="nav-dot dot-orange"></span>Blocked</span><span class="nav-count">{st['blocked']}</span></a>
      </div>
      <div class="nav-section">
        <div class="nav-label">Global</div>
        <a href="/library" class="nav-item active"><span class="nav-item-left"><svg class="nav-icon" width="15" height="15" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="1.8"><path stroke-linecap="round" stroke-linejoin="round" d="M4 6h16M4 10h16M4 14h10M4 18h10"/></svg>Task Library</span></a>
      </div>
    </nav>
  </aside>
  <main class="main">
    <div class="topbar">
      <nav class="breadcrumb">
        <a href="/">Test Hub</a><span class="breadcrumb-sep">/</span>
        <span class="breadcrumb-current">Task Library</span>
      </nav>
    </div>
    <div class="content">
      <div class="content-inner">
        <div style="margin-bottom:24px;">
          <h1 style="font-size:24px;font-weight:700;letter-spacing:-0.03em;color:#1d1d1f;margin-bottom:4px;">Task Library</h1>
          <p style="font-size:13px;color:#6e6e73;">Saved tasks fire automatically on any module, any client. Claude matches by intent — not exact wording.</p>
        </div>
        <div id="lib-body"><p style="color:#aeaeb2;font-size:13px;">Loading…</p></div>
      </div>
    </div>
  </main>
</div>
<script>
(async () => {{
  const res = await fetch('/api/library');
  const raw = await res.json();
  const keys = Object.keys(raw);
  const el = document.getElementById('lib-body');
  if (!keys.length) {{
    el.innerHTML = '<div style="padding:48px;text-align:center;background:#fff;border-radius:14px;box-shadow:0 1px 3px rgba(0,0,0,0.06);"><p style="font-size:14px;color:#6e6e73;margin:0 0 6px;font-weight:500;">Library is empty</p><p style="font-size:13px;color:#aeaeb2;margin:0;">Open a locked scenario and click <strong>+ Task Library</strong> to save a task here.</p></div>';
    return;
  }}
  el.innerHTML = keys.map(name => {{
    const entry = raw[name] || {{}};
    const steps = entry.steps || {{}};
    const stepIds = Object.keys(steps);
    const stepRows = stepIds.map(sid => {{
      const firstLine = (steps[sid] || '').split('\\n')[0];
      return '<div style="display:flex;align-items:baseline;gap:8px;margin-bottom:4px;"><span style="font-size:11px;font-family:monospace;color:#aeaeb2;flex-shrink:0;">'+sid+'</span><span style="font-size:11px;color:#6e6e73;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;">'+firstLine+'</span></div>';
    }}).join('');
    return '<div class="task-card"><div style="display:flex;align-items:flex-start;justify-content:space-between;gap:16px;"><div style="flex:1;min-width:0;"><div style="display:flex;align-items:center;gap:10px;margin-bottom:6px;"><span style="font-size:15px;font-weight:700;color:#1d1d1f;">'+name+'</span><span class="badge badge-blue">'+stepIds.length+' steps</span></div><p style="font-size:13px;color:#6e6e73;margin:0 0 10px;line-height:1.5;">'+(entry.description||'')+'</p>'+stepRows+'</div><button onclick="deleteTask('+JSON.stringify(name)+')" class="btn btn-danger" style="font-size:11px;padding:4px 12px;flex-shrink:0;">Delete</button></div></div>';
  }}).join('');
}})();
async function deleteTask(name) {{
  if (!confirm('Delete task "'+name+'" from the library?')) return;
  const res = await fetch('/api/library/'+encodeURIComponent(name), {{method:'DELETE'}});
  if (res.ok) window.location.reload();
  else alert('Delete failed');
}}
</script>
</body>
</html>"""
    return HTMLResponse(html)


@app.get("/api/library")
def get_library():
    return JSONResponse(_load_library())


@app.get("/api/library/match/{scenario_id}")
def library_match(scenario_id: str):
    """Check if this scenario matches a saved library task. Used for pre-run confirmation."""
    library = _load_library()
    if not library:
        return JSONResponse({"match": None})
    scenarios = _load_scenarios()
    scenario = next((s for s in scenarios if s.scenario_id == scenario_id), None)
    if not scenario:
        return JSONResponse({"match": None})

    all_text = " ".join(s.action.lower() for s in scenario.steps)

    # Keyword match first — fast and reliable for demo
    for task_name, entry in library.items():
        if not isinstance(entry, dict) or not entry.get("steps"):
            continue
        keywords = entry.get("keywords", [])
        if any(kw.lower() in all_text for kw in keywords):
            return JSONResponse({"match": task_name, "description": entry.get("description", "")})

    # Claude fallback for tasks without keywords
    task_lines = "\n".join(
        f"- {name}: {entry.get('description', name)}"
        for name, entry in library.items()
        if isinstance(entry, dict) and entry.get("steps")
    )
    summary = "\n".join(f"  {i+1}. {s.action}" for i, s in enumerate(scenario.steps[:8]))
    api_key = os.getenv("ANTHROPIC_API_KEY", "")
    if not api_key:
        return JSONResponse({"match": None})
    try:
        import anthropic
        client = anthropic.Anthropic(api_key=api_key)
        msg = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=40,
            messages=[{"role": "user", "content": (
                f"Match this SAP SuccessFactors test scenario to a saved task library.\n\n"
                f"Scenario steps:\n{summary}\n\nSaved tasks:\n{task_lines}\n\n"
                f"If this scenario performs one of the saved tasks, reply with ONLY the exact task name. "
                f"Otherwise reply with ONLY: NO_MATCH"
            )}],
        )
        result = msg.content[0].text.strip()
        if result != "NO_MATCH" and result in library:
            return JSONResponse({"match": result, "description": library[result].get("description", "")})
    except Exception as exc:
        print(f"[library match] error: {exc}")
    return JSONResponse({"match": None})


@app.post("/api/library/add")
def add_to_library(
    task_name: str = Form(...),
    task_description: str = Form(...),
    scenario_id: str = Form(...),
):
    # Pull the full locked step sequence for this scenario
    approved = _load_approved().get(scenario_id, {})
    steps = approved.get("step_commands", {})
    if not steps:
        # Fall back to regular feedback if not formally approved
        steps = _load_feedback().get(scenario_id, {})
    if not steps:
        raise HTTPException(400, "No locked steps found for this scenario — approve it first")
    data = _load_library()
    data[task_name] = {
        "description": task_description,
        "steps": steps,  # {step_id: commands, ...} — full task sequence
    }
    _save_library(data)
    threading.Thread(target=_git_push_library, daemon=True).start()
    return JSONResponse({"ok": True})


@app.delete("/api/library/{task_name}")
def delete_library_task(task_name: str):
    data = _load_library()
    data.pop(task_name, None)
    _save_library(data)
    threading.Thread(target=_git_push_library, daemon=True).start()
    return JSONResponse({"ok": True})
