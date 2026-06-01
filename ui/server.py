"""EX3 TestOps — FastAPI dashboard."""

import base64
import hashlib
import hmac
import json
import os
import secrets
import sys
import threading
from pathlib import Path
from collections import defaultdict
from datetime import datetime

from dotenv import load_dotenv
from fastapi import FastAPI, Request, Form, HTTPException, UploadFile, File
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")
sys.path.insert(0, str(ROOT))
from engine.parser import parse_workbook  # noqa: E402
from engine.runner import run_scenario  # noqa: E402

CLIENT_ID = os.getenv("CLIENT_ID", "default")

SCRIPTS_DIR = ROOT / "scripts"
# Use /data (Railway persistent volume) when available, else local runs/
_DATA_ROOT = Path("/data") if Path("/data").exists() else ROOT
RUNS_DIR = _DATA_ROOT / "runs" / CLIENT_ID
UPLOADED_SCRIPTS_DIR = _DATA_ROOT / "scripts" / CLIENT_ID
# Learned/stateful data (feedback, approved patterns, library, statuses, users)
# lives on the /data volume so it survives redeploys — it used to sit in the app
# image and was wiped on every `railway up`.
STORAGE_DIR = _DATA_ROOT / "storage" / CLIENT_ID
STATUS_FILE = STORAGE_DIR / "step_status.json"
USERS_FILE = STORAGE_DIR / "users.json"

# Passkey / WebAuthn credentials live on the persistent volume (/data) so that
# fingerprint enrolments survive redeploys — storage/ is part of the app image
# and is wiped on every `railway up`.
WEBAUTHN_DIR = _DATA_ROOT / "auth" / CLIENT_ID
WEBAUTHN_CRED_FILE = WEBAUTHN_DIR / "webauthn_credentials.json"

# Companies (clients) registry — deployment-wide, persisted on /data. Each user is
# assigned a company; uploaded scripts are filed under their company in the Vault.
COMPANIES_FILE = _DATA_ROOT / "companies.json"

# The Vault: uploaded UAT scripts organised as <company>/<module>/<file>.xlsx on
# the persistent volume, with metadata in vault.json. Survives redeploys.
VAULT_DIR = _DATA_ROOT / "vault"
VAULT_INDEX = VAULT_DIR / "vault.json"

RUNS_DIR.mkdir(parents=True, exist_ok=True)
UPLOADED_SCRIPTS_DIR.mkdir(parents=True, exist_ok=True)
STORAGE_DIR.mkdir(parents=True, exist_ok=True)
WEBAUTHN_DIR.mkdir(parents=True, exist_ok=True)
VAULT_DIR.mkdir(parents=True, exist_ok=True)
(_DATA_ROOT / "storage" / "global").mkdir(parents=True, exist_ok=True)


def _seed_persistent_storage() -> None:
    """On first boot with a /data volume, copy the bundled/committed storage seed
    onto /data — but never overwrite files already there, so runtime learning that
    has accumulated on the volume is preserved across redeploys."""
    if _DATA_ROOT == ROOT:
        return  # local dev, no volume — nothing to migrate
    import shutil
    src_root = ROOT / "storage"
    if not src_root.exists():
        return
    for src in src_root.rglob("*"):
        if src.is_file():
            dst = _DATA_ROOT / "storage" / src.relative_to(src_root)
            if not dst.exists():
                dst.parent.mkdir(parents=True, exist_ok=True)
                try:
                    shutil.copy2(src, dst)
                except Exception as exc:
                    print(f"[seed] {src.name}: {exc}")


def _repair_library_if_corrupt() -> None:
    """Self-heal: if the on-disk step library has no valid task entries (e.g. it
    got overwritten with a stray API body), restore it from the committed seed so
    learned tasks aren't lost to corruption."""
    lib_file = _DATA_ROOT / "storage" / "global" / "step_library.json"
    seed_file = ROOT / "storage" / "global" / "step_library.json"
    try:
        valid = {}
        if lib_file.exists():
            raw = json.loads(lib_file.read_text())
            if isinstance(raw, dict):
                valid = {k: v for k, v in raw.items() if isinstance(v, dict)}
        if valid:
            # Drop any non-dict junk but keep real tasks.
            if lib_file.exists() and json.loads(lib_file.read_text()) != valid:
                lib_file.write_text(json.dumps(valid, indent=2))
                print(f"[library] cleaned {len(valid)} task(s); dropped corrupt entries")
            return
        # No valid tasks on disk — restore from seed if it has any.
        if seed_file.exists():
            seed = json.loads(seed_file.read_text())
            seed = {k: v for k, v in seed.items() if isinstance(v, dict)} if isinstance(seed, dict) else {}
            if seed:
                lib_file.parent.mkdir(parents=True, exist_ok=True)
                lib_file.write_text(json.dumps(seed, indent=2))
                print(f"[library] corrupt/empty — restored {len(seed)} task(s) from seed")
    except Exception as exc:
        print(f"[library] repair failed: {exc}")


_seed_persistent_storage()
_repair_library_if_corrupt()


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
_BATCH_RUNS: dict[str, dict] = {}

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
    run_user = _ACTIVE_RUNS.get(scenario_id, {}).get("user", {})
    _ACTIVE_RUNS[scenario_id].update({
        "status": "paused",
        "paused_step": step_id,
        "screenshot_url": shot_url,
        "error_message": human_error,
        "raw_error": error_message,
        "live_step": live_step,
    })
    _append_audit(
        run_id,
        "run_paused",
        "Run paused for human input" if not live_step else "Step handed to live control",
        scenario_id=scenario_id,
        user=run_user,
        step_id=step_id,
        status="paused",
        details={"error": human_error, "raw_error": error_message, "screenshot_url": shot_url, "live_step": live_step},
    )

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
        else:
            try:
                page.screenshot(path=str(shot_path))
                print(f"  [live-seed] captured fresh pause screenshot {shot_path.stat().st_size // 1024}KB")
            except Exception as _exc:
                print(f"  [live-seed] fresh screenshot failed: {_exc}")

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
    _append_audit(
        run_id,
        "run_resumed",
        "Run resumed",
        scenario_id=scenario_id,
        user=run_user,
        step_id=step_id,
        status="running",
        details={"has_fix": bool(fix), "commands_saved": bool(fix and fix.get("commands"))},
    )
    return fix


def _confirm_callback(scenario_id: str, step_id: str, screenshot_path: str, run_id: str) -> bool:
    """Called by runner after each successful step in supervised mode.

    Pauses the run and waits for the user to click 'Step Done'.
    Returns True if confirmed (move on), False if user wants to redo.
    """
    shot_url = f"/runs/{run_id}/{Path(screenshot_path).name}" if screenshot_path else None
    run_user = _ACTIVE_RUNS.get(scenario_id, {}).get("user", {})
    _ACTIVE_RUNS[scenario_id].update({
        "status": "confirming",
        "confirming_step": step_id,
        "screenshot_url": shot_url,
    })
    _append_audit(
        run_id,
        "step_confirmation_required",
        "Waiting for step confirmation",
        scenario_id=scenario_id,
        user=run_user,
        step_id=step_id,
        status="confirming",
        details={"screenshot_url": shot_url},
    )

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
LIBRARY_FILE = _DATA_ROOT / "storage" / "global" / "step_library.json"
MATCH_CACHE_FILE = _DATA_ROOT / "storage" / "global" / "match_cache.json"


# ── Global step library ───────────────────────────────────────────────────────

def _clean_library(data) -> dict:
    """Keep only valid task entries (name -> dict). Drops corruption like a stray
    {"ok": true, "error": "..."} API body that must never poison matching."""
    if not isinstance(data, dict):
        return {}
    return {str(k): v for k, v in data.items() if isinstance(v, dict)}


def _load_library() -> dict:
    if LIBRARY_FILE.exists():
        try:
            return _clean_library(json.loads(LIBRARY_FILE.read_text()))
        except Exception:
            return {}
    return {}


def _save_library(data: dict) -> None:
    # Never persist anything that isn't a dict-of-task-dicts.
    LIBRARY_FILE.parent.mkdir(parents=True, exist_ok=True)
    LIBRARY_FILE.write_text(json.dumps(_clean_library(data), indent=2))


def _load_match_cache() -> dict:
    if MATCH_CACHE_FILE.exists():
        try:
            data = json.loads(MATCH_CACHE_FILE.read_text())
            if isinstance(data, dict):
                return data
        except Exception:
            return {}
    return {}


def _save_match_cache(data: dict) -> None:
    MATCH_CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
    MATCH_CACHE_FILE.write_text(json.dumps(data, indent=2))


def _scenario_signature(scenarios) -> str:
    """Stable fingerprint of a script's scenario content for cache safety."""
    try:
        payload = [
            {
                "scenario_id": s.scenario_id,
                "name": s.name,
                "steps": [
                    {
                        "step_id": st.step_id,
                        "action": st.action,
                        "expected_result": st.expected_result,
                    }
                    for st in (s.steps or [])
                ],
            }
            for s in (scenarios or [])
        ]
        blob = json.dumps(payload, sort_keys=True, ensure_ascii=False)
        return hashlib.sha256(blob.encode("utf-8")).hexdigest()
    except Exception:
        return ""


def _normalise_cached_match_results(raw_results: list) -> list[dict]:
    normalised = []
    for row in raw_results or []:
        if not isinstance(row, dict):
            continue
        scenario_id = str(row.get("scenario_id", "")).strip()
        if not scenario_id:
            continue
        total_steps = int(row.get("total_steps") or 0)
        covered_count = int(row.get("covered_count") or 0)
        confidence = int(row.get("confidence") or (round(covered_count / total_steps * 100) if total_steps else 0))
        normalised.append({
            "scenario_id": scenario_id,
            "name": str(row.get("name", "")),
            "matched_to": str(row.get("matched_to", "")).strip() or None,
            "reason": str(row.get("reason", ""))[:240],
            "confidence": max(0, min(100, confidence)),
            "covered_count": max(0, covered_count),
            "total_steps": max(0, total_steps),
        })
    return normalised


def _cached_matches(script_key: str, library_size: int, scenarios) -> list[dict] | None:
    cache = _load_match_cache()
    entry = cache.get(script_key)
    if not isinstance(entry, dict):
        return None
    if int(entry.get("library_size") or -1) != int(library_size):
        return None

    # Prefer strict content signature; fall back to id list for older cache rows.
    sig_now = _scenario_signature(scenarios)
    sig_cached = str(entry.get("scenario_signature", "")).strip()
    if sig_cached:
        if sig_cached != sig_now:
            return None
    else:
        ids_now = [s.scenario_id for s in scenarios]
        ids_cached = entry.get("scenario_ids")
        if not isinstance(ids_cached, list) or [str(v) for v in ids_cached] != ids_now:
            return None

    results = _normalise_cached_match_results(entry.get("results") or [])
    if not results:
        return None
    by_id = {r["scenario_id"]: r for r in results}
    if any(s.scenario_id not in by_id for s in scenarios):
        return None
    return results


def _cache_matches(script_key: str, results: list[dict], library_size: int, scenarios=None) -> None:
    if not script_key:
        return
    cache = _load_match_cache()
    cache[script_key] = {
        "matched_at": _utc_now(),
        "library_size": library_size,
        "matched": sum(1 for r in results if r.get("matched_to")),
        "total": len(results),
        "scenario_ids": [s.scenario_id for s in (scenarios or [])],
        "scenario_signature": _scenario_signature(scenarios),
        "results": results,
    }
    _save_match_cache(cache)


def _compute_match_results(scenarios, library: dict) -> list[dict]:
    matches = _match_script_to_library(scenarios, library)
    results = []
    for scenario in scenarios:
        match = matches.get(scenario.scenario_id) or {}
        cov = _coverage_for_scenario(scenario, library)
        cov_steps = cov.get("coverage", {}) or {}
        total_steps = len(scenario.steps)
        covered_count = sum(1 for st in scenario.steps if cov_steps.get(st.step_id))
        results.append({
            "scenario_id": scenario.scenario_id,
            "name": scenario.name,
            "matched_to": cov.get("matched_task") or match.get("match"),
            "reason": cov.get("reason") or match.get("reason", ""),
            "confidence": round(covered_count / total_steps * 100) if total_steps else 0,
            "covered_count": covered_count,
            "total_steps": total_steps,
        })
    return results


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


def _utc_now() -> str:
    return datetime.utcnow().replace(microsecond=0).isoformat() + "Z"


def _audit_path(run_id: str) -> Path:
    path = RUNS_DIR / run_id / "audit.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def _load_audit(run_id: str) -> list[dict]:
    path = _audit_path(run_id)
    if path.exists():
        try:
            data = json.loads(path.read_text())
            if isinstance(data, list):
                return [e for e in data if isinstance(e, dict)]
        except Exception:
            return []
    return []


def _append_audit(
    run_id: str,
    event: str,
    title: str,
    *,
    scenario_id: str = "",
    user: dict | None = None,
    step_id: str = "",
    status: str = "",
    details: dict | None = None,
) -> None:
    if not run_id:
        return
    try:
        events = _load_audit(run_id)
        events.append({
            "ts": _utc_now(),
            "event": event,
            "title": title,
            "scenario_id": scenario_id,
            "step_id": step_id,
            "status": status,
            "user": {
                "username": (user or {}).get("username", "system"),
                "role": (user or {}).get("role", ""),
            },
            "details": details or {},
        })
        _audit_path(run_id).write_text(json.dumps(events, indent=2))
    except Exception as exc:
        print(f"[audit] write failed: {exc}")


def _scenario_lookup() -> dict[str, object]:
    try:
        return {s.scenario_id: s for s in _load_scenarios()}
    except Exception:
        return {}


def _step_log_for_run(run_id: str) -> dict:
    # Prefer the immutable per-run log; fall back to the legacy scenario-keyed scan.
    per_run = RUNS_DIR / run_id / "steps.json"
    if per_run.exists():
        try:
            return json.loads(per_run.read_text())
        except Exception:
            pass
    for f in RUNS_DIR.glob("*_last_run.json"):
        try:
            data = json.loads(f.read_text())
            if data.get("run_id") == run_id:
                return data
        except Exception:
            continue
    return {}


def _fmt_duration(start: str, end: str) -> str:
    try:
        a = datetime.fromisoformat(start.replace("Z", ""))
        b = datetime.fromisoformat(end.replace("Z", ""))
        seconds = max(0, int((b - a).total_seconds()))
        minutes, sec = divmod(seconds, 60)
        return f"{minutes}m {sec}s" if minutes else f"{sec}s"
    except Exception:
        return ""


def _run_record(run_dir: Path, scenarios: dict[str, object] | None = None) -> dict:
    scenarios = scenarios or _scenario_lookup()
    run_id = run_dir.name
    audit = _load_audit(run_id)
    log = _step_log_for_run(run_id)
    scenario_id = ""
    for event in audit:
        scenario_id = event.get("scenario_id") or scenario_id
        if scenario_id:
            break
    if not scenario_id:
        for sid in scenarios:
            if list(run_dir.glob(f"{sid}-*.png")):
                scenario_id = sid
                break
    scenario = scenarios.get(scenario_id)
    steps = log.get("steps", []) if isinstance(log.get("steps"), list) else []
    failed = [s for s in steps if not s.get("passed")]
    videos = sorted(run_dir.glob("*.webm"))
    screenshots = sorted(run_dir.glob("*.png"))
    terminal_events = [e for e in audit if e.get("event") in ("run_completed", "run_failed", "run_cancelled")]
    status = (terminal_events[-1].get("status") if terminal_events else log.get("status") or "recorded")
    if status == "done" and failed:
        status = "failed"
    elif status == "done":
        status = "passed"
    started = audit[0].get("ts") if audit else datetime.utcfromtimestamp(run_dir.stat().st_mtime).replace(microsecond=0).isoformat() + "Z"
    ended = (terminal_events[-1].get("ts") if terminal_events else (audit[-1].get("ts") if audit else started))
    # Which company this run belongs to — derived from the script it ran (vault
    # keys look like "<company>/<module>/<file>"); legacy/bundled scripts -> internal.
    run_script = ""
    for e in audit:
        if e.get("event") in ("run_started",):
            run_script = str(e.get("details", {}).get("script", "") or "")
            break
    run_company = run_script.split("/", 1)[0] if "/" in run_script else "internal"
    return {
        "id": run_id,
        "scenario_id": scenario_id,
        "script": run_script,
        "company": run_company,
        "scenario_name": getattr(scenario, "name", scenario_id or "Unknown scenario"),
        "module": getattr(scenario, "module", ""),
        "role": getattr(scenario, "role", ""),
        "status": status,
        "started_at": started,
        "ended_at": ended,
        "duration": _fmt_duration(started, ended),
        "user": (audit[0].get("user", {}) if audit else {"username": "unknown", "role": ""}),
        "steps_total": len(getattr(scenario, "steps", []) or steps),
        "steps_logged": len(steps),
        "steps_failed": len(failed),
        "screenshots": len(screenshots),
        "video_url": f"/runs/{run_id}/{videos[0].name}" if videos else "",
        "audit_count": len(audit),
    }


def _company_for_run(run_id: str) -> str:
    """Company a run belongs to — from its run_started script key; else internal."""
    for e in _load_audit(run_id):
        if e.get("event") == "run_started":
            sc = str(e.get("details", {}).get("script", "") or "")
            return sc.split("/", 1)[0] if "/" in sc else "internal"
    return "internal"


def _run_records(company: str | None = None) -> list[dict]:
    """All run records, newest first. If company is given, only that company's
    runs (owner/admin pass None to see everything)."""
    scenarios = _scenario_lookup()
    records = []
    if RUNS_DIR.exists():
        for run_dir in sorted([p for p in RUNS_DIR.iterdir() if p.is_dir()], key=lambda p: p.stat().st_mtime, reverse=True):
            rec = _run_record(run_dir, scenarios)
            if company is None or rec.get("company") == company:
                records.append(rec)
    return records


def _latest_run_id_for_scenario(scenario_id: str) -> str:
    active = _ACTIVE_RUNS.get(scenario_id, {})
    if active.get("run_id"):
        return str(active["run_id"])
    for record in _run_records():
        if record.get("scenario_id") == scenario_id:
            return record["id"]
    return ""

app = FastAPI(title="EX3 TestOps")
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
# Run files (videos/screenshots) are NOT served by an open static mount — they're
# served by the authenticated, company-scoped /runs/{run_id}/{filename} route below
# so a user can only fetch evidence from their own company's runs.
@app.get("/runs/{run_id}/{filename}")
def serve_run_file(request: Request, run_id: str, filename: str):
    """Authenticated, company-scoped delivery of run evidence (video/screenshots).
    A user can only fetch files from runs in their own company; owner/admin any."""
    target = (RUNS_DIR / run_id / filename).resolve()
    if not target.is_relative_to(RUNS_DIR.resolve()) or not target.is_file():
        return HTMLResponse("Not found", status_code=404)
    user = _current_user(request)
    if not _role_at_least(user.get("role", "viewer"), "admin"):
        if _company_for_run(run_id) != (user.get("company") or "internal"):
            return HTMLResponse("Not found", status_code=404)
    return FileResponse(str(target))

AUTH_COOKIE = "ex3_testops_auth"
AUTH_TOKEN = os.getenv("TESTOPS_AUTH_TOKEN", "").strip()
TESTOPS_PIN = os.getenv("TESTOPS_PIN", "")
TESTOPS_USERS = os.getenv("TESTOPS_USERS", "")
ROLE_LEVELS = {"viewer": 0, "tester": 10, "lead": 20, "admin": 30, "owner": 40}
SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}

# The auth cookie is signed with AUTH_TOKEN (HMAC). It is the only thing stopping
# an attacker from forging an owner-role session, so a guessable value is a full
# auth bypass. Reject known-weak secrets outright.
_WEAK_AUTH_TOKENS = {"", "ok", "secret", "changeme", "change-me", "token", "default", "password"}


def _auth_enabled() -> bool:
    return bool(TESTOPS_PIN.strip() or TESTOPS_USERS.strip())


# Fail closed: if logins are configured but the signing secret is missing or weak,
# refuse to boot rather than silently run with a forgeable secret.
if _auth_enabled() and AUTH_TOKEN.lower() in _WEAK_AUTH_TOKENS:
    raise RuntimeError(
        "TESTOPS_AUTH_TOKEN is unset or too weak. The login cookie is signed with "
        "this secret; a guessable value lets anyone forge an owner session. Set a "
        "strong random value and redeploy, e.g.  TESTOPS_AUTH_TOKEN=$(openssl rand -hex 32)"
    )


def _configured_users() -> list[dict]:
    users: list[dict] = []
    if TESTOPS_USERS.strip():
        try:
            env_users = json.loads(TESTOPS_USERS)
            if isinstance(env_users, list):
                users.extend([u for u in env_users if isinstance(u, dict) and u.get("username")])
        except Exception:
            pass
    users.extend(_load_users())
    if TESTOPS_PIN.strip():
        users.append({"username": "louie", "password": TESTOPS_PIN, "role": "owner", "name": "Louie", "company": "internal"})

    deduped: dict[str, dict] = {}
    for user in users:
        key = str(user.get("username", "")).strip().lower()
        if key and key not in deduped:
            deduped[key] = user
    return list(deduped.values())


def _load_users() -> list[dict]:
    if USERS_FILE.exists():
        try:
            data = json.loads(USERS_FILE.read_text())
            if isinstance(data, list):
                return [u for u in data if isinstance(u, dict) and u.get("username")]
        except Exception:
            return []
    return []


def _save_users(users: list[dict]) -> None:
    USERS_FILE.write_text(json.dumps(users, indent=2))


def _public_user(user: dict) -> dict:
    return {
        "username": user.get("username", ""),
        "name": user.get("name") or user.get("username", ""),
        "role": user.get("role", "viewer"),
        "company": user.get("company", "internal"),
    }


_PBKDF2_ITERS = 200_000


def _hash_password(password: str) -> str:
    """Salted PBKDF2-HMAC-SHA256 (stdlib, no extra dependency)."""
    salt = secrets.token_hex(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), bytes.fromhex(salt), _PBKDF2_ITERS)
    return f"pbkdf2${_PBKDF2_ITERS}${salt}${dk.hex()}"


def _password_ok(candidate: str, stored: str) -> bool:
    if not stored:
        return False
    if stored.startswith("pbkdf2$"):
        try:
            _, iters, salt, want = stored.split("$", 3)
            dk = hashlib.pbkdf2_hmac("sha256", candidate.encode("utf-8"), bytes.fromhex(salt), int(iters))
            return secrets.compare_digest(dk.hex(), want)
        except Exception:
            return False
    # Legacy formats still verify (sha256:, or plain for the env PIN).
    if stored.startswith("sha256:"):
        digest = hashlib.sha256(candidate.encode("utf-8")).hexdigest()
        return secrets.compare_digest(stored.removeprefix("sha256:"), digest)
    return secrets.compare_digest(candidate, stored)


# Signed-cookie sessions expire after this long; a fresh login re-issues.
AUTH_TTL_SECONDS = 12 * 3600


def _sign_auth(username: str, role: str) -> str:
    iat = str(int(datetime.utcnow().timestamp()))
    payload = f"{username}|{role}|{iat}"
    sig = hmac.new(AUTH_TOKEN.encode("utf-8"), payload.encode("utf-8"), hashlib.sha256).hexdigest()
    return base64.urlsafe_b64encode(f"{payload}|{sig}".encode("utf-8")).decode("ascii")


def _read_auth_cookie(request: Request) -> dict | None:
    raw = request.cookies.get(AUTH_COOKIE, "")
    if not raw:
        return None
    try:
        decoded = base64.urlsafe_b64decode(raw.encode("ascii")).decode("utf-8")
        parts = decoded.split("|")
        if len(parts) == 4:
            username, role, iat, sig = parts
            payload = f"{username}|{role}|{iat}"
        elif len(parts) == 3:  # legacy cookie (no issued-at) — accept, no expiry
            username, role, sig = parts
            payload, iat = f"{username}|{role}", None
        else:
            return None
        expected = hmac.new(AUTH_TOKEN.encode("utf-8"), payload.encode("utf-8"), hashlib.sha256).hexdigest()
        if not secrets.compare_digest(sig, expected):
            return None
        if iat is not None and (datetime.utcnow().timestamp() - int(iat)) > AUTH_TTL_SECONDS:
            return None  # session expired
        return {"username": username, "role": role if role in ROLE_LEVELS else "viewer"}
    except Exception:
        return None


def _current_user(request: Request) -> dict:
    base = _read_auth_cookie(request)
    if not base:
        return {"username": "guest", "role": "viewer", "company": "internal", "name": "Guest"}
    # The cookie only carries username|role — resolve company + display name from
    # the user's saved record so uploads/vault file under the right company.
    uname = str(base.get("username", "")).strip().lower()
    for u in _configured_users():
        if str(u.get("username", "")).strip().lower() == uname:
            return {
                "username": base["username"],
                "role": base.get("role", u.get("role", "viewer")),
                "company": (u.get("company") or "internal"),
                "name": u.get("name") or base["username"],
            }
    return {**base, "company": base.get("company", "internal")}


def _is_authenticated(request: Request) -> bool:
    return _read_auth_cookie(request) is not None


def _role_at_least(role: str, minimum: str) -> bool:
    return ROLE_LEVELS.get(role, -1) >= ROLE_LEVELS.get(minimum, 999)


def _minimum_role_for_write(path: str, method: str) -> str:
    if path.startswith("/admin") or path.startswith("/api/users"):
        return "admin"
    if path.startswith("/webauthn/"):
        return "viewer"  # any signed-in user may enrol/check their own passkey
    if path.startswith("/vault"):
        return "viewer" if method in SAFE_METHODS else "tester"  # browse: anyone; upload: tester+
    if path.startswith("/api/scripts") or path.startswith("/scripts/upload"):
        return "lead"
    if method in SAFE_METHODS:
        return "viewer"
    if path.startswith("/api/library/rescan"):
        return "lead"
    if path.startswith("/api/scenario/") or path.startswith("/api/library"):
        return "lead"
    if (
        path.startswith("/api/run/")
        or path.startswith("/api/live/")
        or path.startswith("/api/step-")
        or path.startswith("/api/expected-override")
        or path.startswith("/api/interpret-fix")
    ):
        return "tester"
    return "admin"


# ── Login rate-limiting / lockout ───────────────────────────────────────────
_LOGIN_FAILS: dict[str, list[float]] = {}
_LOGIN_LOCK_THRESHOLD = 8       # failures within the window before lockout
_LOGIN_LOCK_WINDOW = 900        # 15 minutes


def _client_ip(request: Request) -> str:
    xff = request.headers.get("x-forwarded-for", "")
    if xff:
        return xff.split(",")[0].strip()
    return request.client.host if request.client else "?"


def _login_locked(key: str) -> bool:
    now = datetime.utcnow().timestamp()
    fails = [t for t in _LOGIN_FAILS.get(key, []) if now - t < _LOGIN_LOCK_WINDOW]
    _LOGIN_FAILS[key] = fails
    return len(fails) >= _LOGIN_LOCK_THRESHOLD


def _record_login_fail(key: str) -> None:
    _LOGIN_FAILS.setdefault(key, []).append(datetime.utcnow().timestamp())


def _clear_login_fails(key: str) -> None:
    _LOGIN_FAILS.pop(key, None)


@app.middleware("http")
async def security_headers(request: Request, call_next):
    resp = await call_next(request)
    resp.headers.setdefault("X-Content-Type-Options", "nosniff")
    resp.headers.setdefault("X-Frame-Options", "DENY")
    resp.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
    resp.headers.setdefault("Strict-Transport-Security", "max-age=31536000; includeSubDomains")
    resp.headers.setdefault("Content-Security-Policy", "frame-ancestors 'none'")
    return resp


@app.middleware("http")
async def require_pin(request: Request, call_next):
    if not _auth_enabled():
        return await call_next(request)

    path = request.url.path
    if (
        path in ("/login", "/logout")
        or path.startswith("/static")
        or path.startswith("/webauthn/authenticate")  # passkey login happens pre-auth
    ):
        return await call_next(request)

    user = _read_auth_cookie(request)
    if user:
        minimum_role = _minimum_role_for_write(path, request.method)
        if _role_at_least(user["role"], minimum_role):
            return await call_next(request)
        if path.startswith("/api/"):
            return JSONResponse({"ok": False, "error": f"{minimum_role} role required"}, status_code=403)
        return HTMLResponse("Permission denied", status_code=403)

    if path.startswith("/api/"):
        return JSONResponse({"ok": False, "error": "Authentication required"}, status_code=401)
    return RedirectResponse("/login", status_code=303)


_LOGIN_FINGERPRINT_JS = """
<script>
(function(){
  function b2b(s){s=s.replace(/-/g,'+').replace(/_/g,'/');var p=s.length%4;if(p)s+='='.repeat(4-p);var bin=atob(s);var u=new Uint8Array(bin.length);for(var i=0;i<bin.length;i++)u[i]=bin.charCodeAt(i);return u.buffer;}
  function buf(b){var u=new Uint8Array(b);var s='';for(var i=0;i<u.length;i++)s+=String.fromCharCode(u[i]);return btoa(s).replace(/\\+/g,'-').replace(/\\//g,'_').replace(/=+$/,'');}
  var btn=document.getElementById('pk-btn'),msg=document.getElementById('pk-msg');
  if(!btn||!window.PublicKeyCredential||!navigator.credentials){return;}
  btn.style.display='flex';
  function say(t,err){msg.textContent=t;msg.style.color=err?'#ff9b8f':'#bcb5a8';}
  btn.addEventListener('click',async function(){
    if(btn.disabled){return;}
    btn.disabled=true;say('Waiting for your fingerprint...',false);
    try{
      var br=await fetch('/webauthn/authenticate/begin',{method:'POST'});
      var bd=await br.json();
      if(!bd.ok){say(bd.error||'Fingerprint sign-in unavailable',true);btn.disabled=false;return;}
      var o=bd.options;
      o.challenge=b2b(o.challenge);
      if(o.allowCredentials){o.allowCredentials=o.allowCredentials.map(function(c){return Object.assign({},c,{id:b2b(c.id)});});}
      var asr=await navigator.credentials.get({publicKey:o});
      var cred={id:asr.id,rawId:buf(asr.rawId),type:asr.type,response:{clientDataJSON:buf(asr.response.clientDataJSON),authenticatorData:buf(asr.response.authenticatorData),signature:buf(asr.response.signature),userHandle:asr.response.userHandle?buf(asr.response.userHandle):null},clientExtensionResults:asr.getClientExtensionResults?asr.getClientExtensionResults():{},authenticatorAttachment:asr.authenticatorAttachment||null};
      var cr=await fetch('/webauthn/authenticate/complete',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({credential:cred})});
      var rd=await cr.json();
      if(rd.ok){window.location.href=rd.redirect||'/';}else{say(rd.error||'Fingerprint sign-in failed',true);btn.disabled=false;}
    }catch(e){say('Fingerprint cancelled',true);btn.disabled=false;}
  });
})();
</script>
"""


@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request):
    error = request.query_params.get("error")
    return HTMLResponse(f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>Login - EX3 TestOps</title>
  <style>
    * {{ box-sizing: border-box; }}
    body {{ margin: 0; min-height: 100vh; display: grid; place-items: center; background: radial-gradient(circle at 18% 12%, rgba(188,153,84,.18), transparent 30%), linear-gradient(135deg, #090908, #161513 58%, #0b0b0c); color: #f7f3ea; font-family: Inter, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; }}
    form {{ width: min(420px, calc(100vw - 32px)); background: rgba(22,21,19,.86); border: 1px solid rgba(226,207,159,.18); border-radius: 18px; padding: 32px; box-shadow: 0 28px 100px rgba(0,0,0,.42); backdrop-filter: blur(22px); }}
    .mark {{ width: 44px; height: 44px; border-radius: 12px; display:grid; place-items:center; margin-bottom: 22px; background: linear-gradient(135deg, #d8ba72, #91713e); color:#0b0b0c; font-weight:800; }}
    h1 {{ margin: 0 0 6px; font-size: 24px; letter-spacing: 0; }}
    p {{ margin: 0 0 22px; color: #bcb5a8; font-size: 13px; }}
    label {{ display:block; color:#d8d1c3; font-size:12px; font-weight:650; margin: 12px 0 6px; }}
    input {{ width: 100%; border: 1px solid rgba(226,207,159,.16); background: rgba(5,5,5,.42); color: #fff; border-radius: 11px; padding: 12px 13px; font-size: 15px; outline: none; }}
    input:focus {{ border-color: #d8ba72; box-shadow: 0 0 0 3px rgba(216,186,114,.15); }}
    button {{ width: 100%; margin-top: 16px; border: 0; border-radius: 11px; background: linear-gradient(135deg, #dfc27b, #a88648); color: #111; padding: 12px; font-weight: 800; cursor: pointer; }}
    .err {{ color: #ff9b8f; margin-top: 12px; font-size: 13px; }}
  </style>
</head>
<body>
  <form method="post" action="/login">
    <div class="mark">EX3</div>
    <h1>Welcome back</h1>
    <p>Sign in to the private TestOps command centre.</p>
    <label for="username">User</label>
    <input id="username" name="username" type="text" placeholder="Username" autocomplete="username" autofocus />
    <label for="password">Password</label>
    <input id="password" name="password" type="password" placeholder="Password" autocomplete="current-password" />
    <button type="submit">Enter workspace</button>
    {('<div class="err">Too many attempts — locked for 15 minutes. Try again later.</div>' if error == 'locked' else ('<div class="err">Incorrect username or password</div>' if error else ''))}
    <button id="pk-btn" type="button" style="display:none;margin-top:12px;background:rgba(255,255,255,.06);color:#f7f3ea;border:1px solid rgba(226,207,159,.22);align-items:center;justify-content:center;gap:9px">
      <svg width="17" height="17" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><path d="M12 10.5c0 3.5-.2 6-1.2 8.5"/><path d="M8.2 10a3.8 3.8 0 0 1 7.6 0c0 4-.7 6.2-1 7"/><path d="M5.2 10.6a6.8 6.8 0 0 1 11.4-4.7"/><path d="M9 18.6c.5-1.3.6-3.4.6-5.1a2.4 2.4 0 0 1 4.8 0"/></svg>
      Sign in with fingerprint
    </button>
    <div id="pk-msg" style="margin-top:10px;font-size:12px;color:#bcb5a8;min-height:14px"></div>
  </form>
""" + _LOGIN_FINGERPRINT_JS + """
</body>
</html>""")


@app.post("/login")
def login(request: Request, username: str = Form(""), password: str = Form("")):
    key = _client_ip(request)
    if _login_locked(key):
        return RedirectResponse("/login?error=locked", status_code=303)
    for user in _configured_users():
        if (
            secrets.compare_digest(username.strip().lower(), str(user.get("username", "")).strip().lower())
            and _password_ok(password, str(user.get("password", "")))
        ):
            role = str(user.get("role", "viewer")).lower()
            _clear_login_fails(key)
            response = RedirectResponse("/", status_code=303)
            response.set_cookie(
                AUTH_COOKIE, _sign_auth(str(user["username"]), role),
                httponly=True, secure=True, samesite="lax", max_age=AUTH_TTL_SECONDS,
            )
            return response
    _record_login_fail(key)
    return RedirectResponse("/login?error=1", status_code=303)


@app.get("/logout")
def logout():
    response = RedirectResponse("/login", status_code=303)
    response.delete_cookie(AUTH_COOKIE)
    return response


# ── WebAuthn / passkeys (fingerprint, Touch ID, Windows Hello) ──────────────
# Fingerprint login is a convenience layer ON TOP of the password: users log in
# with their password, then optionally enrol a passkey on their device. The
# biometric never leaves the device — we only store a public key per credential.
RP_ID = os.getenv("WEBAUTHN_RP_ID", "ex3-testops-next-production.up.railway.app")
RP_NAME = os.getenv("WEBAUTHN_RP_NAME", "EX3 TestOps")
WEBAUTHN_ORIGIN = os.getenv("WEBAUTHN_ORIGIN", f"https://{RP_ID}")
WA_CHALLENGE_COOKIE = "ex3_wa_chal"

try:
    import webauthn as _webauthn
    from webauthn.helpers import bytes_to_base64url, base64url_to_bytes
    from webauthn.helpers.structs import (
        PublicKeyCredentialDescriptor,
        AuthenticatorSelectionCriteria,
        ResidentKeyRequirement,
        UserVerificationRequirement,
    )
    _WEBAUTHN_OK = True
except Exception as _wa_exc:  # library missing → endpoints degrade gracefully
    print(f"[webauthn] library unavailable: {_wa_exc}")
    _WEBAUTHN_OK = False


def _load_webauthn_creds() -> dict:
    if WEBAUTHN_CRED_FILE.exists():
        try:
            data = json.loads(WEBAUTHN_CRED_FILE.read_text())
            if isinstance(data, dict):
                return data
        except Exception:
            return {}
    return {}


def _save_webauthn_creds(data: dict) -> None:
    WEBAUTHN_CRED_FILE.parent.mkdir(parents=True, exist_ok=True)
    WEBAUTHN_CRED_FILE.write_text(json.dumps(data, indent=2))


def _user_creds(username: str) -> list[dict]:
    return _load_webauthn_creds().get(username.strip().lower(), [])


def _role_for_username(username: str) -> str | None:
    target = username.strip().lower()
    for u in _configured_users():
        if str(u.get("username", "")).strip().lower() == target:
            return str(u.get("role", "viewer")).lower()
    return None


def _sign_blob(payload: str) -> str:
    sig = hmac.new(AUTH_TOKEN.encode("utf-8"), payload.encode("utf-8"), hashlib.sha256).hexdigest()
    return base64.urlsafe_b64encode(f"{payload}|{sig}".encode("utf-8")).decode("ascii")


def _read_blob(raw: str) -> str | None:
    try:
        decoded = base64.urlsafe_b64decode(raw.encode("ascii")).decode("utf-8")
        payload, sig = decoded.rsplit("|", 1)
        expected = hmac.new(AUTH_TOKEN.encode("utf-8"), payload.encode("utf-8"), hashlib.sha256).hexdigest()
        return payload if secrets.compare_digest(sig, expected) else None
    except Exception:
        return None


def _set_challenge_cookie(response, kind: str, challenge_b64: str, extra: str = "") -> None:
    payload = f"{kind}|{extra}|{challenge_b64}" if extra else f"{kind}|{challenge_b64}"
    response.set_cookie(
        WA_CHALLENGE_COOKIE, _sign_blob(payload),
        max_age=300, httponly=True, secure=True, samesite="lax", path="/",
    )


@app.get("/webauthn/status")
def webauthn_status(request: Request):
    user = _read_auth_cookie(request)
    if not user:
        return JSONResponse({"ok": False, "available": _WEBAUTHN_OK, "enrolled": False, "count": 0})
    creds = _user_creds(user["username"])
    return JSONResponse({
        "ok": True,
        "available": _WEBAUTHN_OK,
        "enrolled": len(creds) > 0,
        "count": len(creds),
        "passkeys": [
            {"label": c.get("label", ""), "created_at": c.get("created_at", ""), "last_used": c.get("last_used", "")}
            for c in creds
        ],
    })


@app.post("/webauthn/register/begin")
def webauthn_register_begin(request: Request):
    if not _WEBAUTHN_OK:
        return JSONResponse({"ok": False, "error": "Passkeys unavailable on the server"}, status_code=503)
    user = _read_auth_cookie(request)
    if not user:
        return JSONResponse({"ok": False, "error": "Sign in first"}, status_code=401)
    username = user["username"]
    existing = _user_creds(username)
    options = _webauthn.generate_registration_options(
        rp_id=RP_ID,
        rp_name=RP_NAME,
        user_name=username,
        user_id=hashlib.sha256(username.strip().lower().encode("utf-8")).digest(),
        user_display_name=username,
        authenticator_selection=AuthenticatorSelectionCriteria(
            resident_key=ResidentKeyRequirement.PREFERRED,
            user_verification=UserVerificationRequirement.PREFERRED,
        ),
        exclude_credentials=[
            PublicKeyCredentialDescriptor(id=base64url_to_bytes(c["id"])) for c in existing
        ],
    )
    response = JSONResponse({"ok": True, "options": json.loads(_webauthn.options_to_json(options))})
    _set_challenge_cookie(response, "reg", bytes_to_base64url(options.challenge), username.strip().lower())
    return response


@app.post("/webauthn/register/complete")
async def webauthn_register_complete(request: Request):
    if not _WEBAUTHN_OK:
        return JSONResponse({"ok": False, "error": "Passkeys unavailable on the server"}, status_code=503)
    user = _read_auth_cookie(request)
    if not user:
        return JSONResponse({"ok": False, "error": "Sign in first"}, status_code=401)
    username = user["username"]
    payload = _read_blob(request.cookies.get(WA_CHALLENGE_COOKIE, ""))
    parts = payload.split("|", 2) if payload else []
    if len(parts) != 3 or parts[0] != "reg" or parts[1] != username.strip().lower():
        return JSONResponse({"ok": False, "error": "Challenge expired — try again"}, status_code=400)
    challenge_b64 = parts[2]
    body = await request.json()
    credential = body.get("credential") or body
    label = (str(body.get("label") or "").strip() or "Passkey")[:60]
    try:
        verification = _webauthn.verify_registration_response(
            credential=credential,
            expected_challenge=base64url_to_bytes(challenge_b64),
            expected_rp_id=RP_ID,
            expected_origin=WEBAUTHN_ORIGIN,
            require_user_verification=False,
        )
    except Exception as exc:
        return JSONResponse({"ok": False, "error": f"Could not register passkey: {exc}"}, status_code=400)
    all_creds = _load_webauthn_creds()
    user_list = all_creds.setdefault(username.strip().lower(), [])
    new_id = bytes_to_base64url(verification.credential_id)
    if not any(c.get("id") == new_id for c in user_list):
        user_list.append({
            "id": new_id,
            "public_key": bytes_to_base64url(verification.credential_public_key),
            "sign_count": verification.sign_count,
            "label": label,
            "created_at": _utc_now(),
            "last_used": "",
        })
        _save_webauthn_creds(all_creds)
    response = JSONResponse({"ok": True, "count": len(user_list)})
    response.delete_cookie(WA_CHALLENGE_COOKIE, path="/")
    return response


@app.post("/webauthn/authenticate/begin")
def webauthn_authenticate_begin(request: Request):
    if not _WEBAUTHN_OK:
        return JSONResponse({"ok": False, "error": "Passkeys unavailable on the server"}, status_code=503)
    options = _webauthn.generate_authentication_options(
        rp_id=RP_ID,
        user_verification=UserVerificationRequirement.PREFERRED,
    )
    response = JSONResponse({"ok": True, "options": json.loads(_webauthn.options_to_json(options))})
    _set_challenge_cookie(response, "auth", bytes_to_base64url(options.challenge))
    return response


@app.post("/webauthn/authenticate/complete")
async def webauthn_authenticate_complete(request: Request):
    if not _WEBAUTHN_OK:
        return JSONResponse({"ok": False, "error": "Passkeys unavailable on the server"}, status_code=503)
    payload = _read_blob(request.cookies.get(WA_CHALLENGE_COOKIE, ""))
    parts = payload.split("|", 1) if payload else []
    if len(parts) != 2 or parts[0] != "auth":
        return JSONResponse({"ok": False, "error": "Challenge expired — try again"}, status_code=400)
    challenge_b64 = parts[1]
    body = await request.json()
    credential = body.get("credential") or body
    cred_id = credential.get("id") or credential.get("rawId")
    if not cred_id:
        return JSONResponse({"ok": False, "error": "Malformed passkey response"}, status_code=400)
    all_creds = _load_webauthn_creds()
    owner, stored = None, None
    for uname, items in all_creds.items():
        for c in items:
            if c.get("id") == cred_id:
                owner, stored = uname, c
                break
        if owner:
            break
    if not stored:
        return JSONResponse({"ok": False, "error": "This passkey isn't registered"}, status_code=400)
    role = _role_for_username(owner)
    if role is None:
        return JSONResponse({"ok": False, "error": "Account no longer permitted"}, status_code=403)
    try:
        verification = _webauthn.verify_authentication_response(
            credential=credential,
            expected_challenge=base64url_to_bytes(challenge_b64),
            expected_rp_id=RP_ID,
            expected_origin=WEBAUTHN_ORIGIN,
            credential_public_key=base64url_to_bytes(stored["public_key"]),
            credential_current_sign_count=int(stored.get("sign_count", 0)),
            require_user_verification=False,
        )
    except Exception as exc:
        return JSONResponse({"ok": False, "error": f"Fingerprint check failed: {exc}"}, status_code=400)
    stored["sign_count"] = verification.new_sign_count
    stored["last_used"] = _utc_now()
    _save_webauthn_creds(all_creds)
    response = JSONResponse({"ok": True, "redirect": "/"})
    response.set_cookie(AUTH_COOKIE, _sign_auth(owner, role), httponly=True, secure=True, samesite="lax", max_age=AUTH_TTL_SECONDS)
    response.delete_cookie(WA_CHALLENGE_COOKIE, path="/")
    return response


@app.get("/admin/users", response_class=HTMLResponse)
def users_page(request: Request):
    return templates.TemplateResponse(
        request=request,
        name="users.html",
        context={
            "stats": _stats(),
            "client_id": CLIENT_ID,
            "current_user": _current_user(request),
            "users": [_public_user(u) for u in _configured_users()],
            "roles": list(ROLE_LEVELS.keys()),
            "companies": _load_companies(),
            "active": "users",
        },
    )


@app.get("/api/users")
def get_users():
    return JSONResponse({"users": [_public_user(u) for u in _configured_users()], "roles": list(ROLE_LEVELS.keys())})


@app.post("/api/users")
def save_user(
    username: str = Form(...),
    name: str = Form(""),
    role: str = Form("viewer"),
    password: str = Form(""),
    company: str = Form("internal"),
):
    username = username.strip()
    if not username:
        raise HTTPException(400, "Username required")
    if role not in ROLE_LEVELS:
        raise HTTPException(400, "Invalid role")
    company = (company or "internal").strip().lower()
    if company not in _company_keys():
        company = "internal"

    users = _load_users()
    existing = next((u for u in users if str(u.get("username", "")).lower() == username.lower()), None)
    if existing is None:
        if not password:
            raise HTTPException(400, "Password required for new users")
        existing = {"username": username}
        users.append(existing)
    existing["name"] = name.strip() or username
    existing["role"] = role
    existing["company"] = company
    if password:
        existing["password"] = _hash_password(password)
    _save_users(users)
    return RedirectResponse("/admin/users", status_code=303)


@app.post("/api/users/delete")
def delete_user(username: str = Form(...)):
    users = [u for u in _load_users() if str(u.get("username", "")).lower() != username.strip().lower()]
    _save_users(users)
    return RedirectResponse("/admin/users", status_code=303)


# ── Companies / clients ─────────────────────────────────────────────────────
def _slugify_company(name: str) -> str:
    import re
    slug = re.sub(r"[^a-z0-9]+", "_", name.strip().lower()).strip("_")
    return slug or "client"


def _save_companies(companies: list[dict]) -> None:
    COMPANIES_FILE.parent.mkdir(parents=True, exist_ok=True)
    COMPANIES_FILE.write_text(json.dumps(companies, indent=2))


def _load_companies() -> list[dict]:
    data: list[dict] = []
    if COMPANIES_FILE.exists():
        try:
            raw = json.loads(COMPANIES_FILE.read_text())
            if isinstance(raw, list):
                data = [c for c in raw if isinstance(c, dict) and c.get("key")]
        except Exception:
            data = []
    if not data:
        data = [{"key": "internal", "name": "Internal", "created_at": _utc_now(), "created_by": "system"}]
        try:
            _save_companies(data)
        except Exception:
            pass
    return data


def _company_keys() -> list[str]:
    return [c["key"] for c in _load_companies()]


@app.get("/admin/companies", response_class=HTMLResponse)
def companies_page(request: Request):
    return templates.TemplateResponse(
        request=request,
        name="companies.html",
        context={
            "stats": _stats(),
            "client_id": CLIENT_ID,
            "current_user": _current_user(request),
            "companies": _load_companies(),
            "active": "companies",
        },
    )


@app.get("/api/companies")
def get_companies():
    return JSONResponse({"companies": _load_companies()})


@app.post("/api/companies")
def create_company(request: Request, name: str = Form(...)):
    name = name.strip()
    if not name:
        raise HTTPException(400, "Company name required")
    key = _slugify_company(name)
    companies = _load_companies()
    if not any(c["key"] == key for c in companies):
        companies.append({
            "key": key,
            "name": name,
            "created_at": _utc_now(),
            "created_by": _current_user(request).get("username", "owner"),
        })
        _save_companies(companies)
    return RedirectResponse("/admin/companies", status_code=303)


@app.post("/api/companies/delete")
def delete_company(key: str = Form(...)):
    key = key.strip().lower()
    if key == "internal":
        raise HTTPException(400, "The internal company cannot be removed")
    _save_companies([c for c in _load_companies() if c["key"] != key])
    return RedirectResponse("/admin/companies", status_code=303)


# ── Vault (company -> module -> scripts) ────────────────────────────────────
_MODULE_SUGGESTIONS = ["RCM", "EC", "Onboarding", "PMGM", "Compensation", "Recruiting", "LMS", "Succession"]


def _load_vault_index() -> dict:
    if VAULT_INDEX.exists():
        try:
            data = json.loads(VAULT_INDEX.read_text())
            if isinstance(data, dict):
                return data
        except Exception:
            return {}
    return {}


def _save_vault_index(data: dict) -> None:
    VAULT_DIR.mkdir(parents=True, exist_ok=True)
    VAULT_INDEX.write_text(json.dumps(data, indent=2))


def _safe_segment(value: str, fallback: str) -> str:
    import re
    seg = re.sub(r"[^A-Za-z0-9_.-]+", "_", value.strip()).strip("._")
    return seg or fallback


def _vault_view(user: dict) -> list[dict]:
    """Company -> modules -> scripts, filtered to the user's company unless owner/admin."""
    index = _load_vault_index()
    see_all = _role_at_least(user.get("role", "viewer"), "admin")
    my_company = (user.get("company") or "internal").strip().lower()
    out = []
    for co in _load_companies():
        ckey = co["key"]
        if not see_all and ckey != my_company:
            continue
        co_dir = VAULT_DIR / ckey
        modules = []
        if co_dir.exists():
            for mod_dir in sorted([p for p in co_dir.iterdir() if p.is_dir()], key=lambda p: p.name.lower()):
                scripts = []
                for f in sorted(mod_dir.glob("*.xlsx"), key=lambda p: p.name.lower()):
                    rel = f"{ckey}/{mod_dir.name}/{f.name}"
                    meta = index.get(rel, {})
                    scripts.append({
                        "key": rel,
                        "name": meta.get("original_name") or f.stem.replace("_", " "),
                        "uploaded_by": meta.get("uploaded_by", ""),
                        "uploaded_at": meta.get("uploaded_at", ""),
                    })
                modules.append({"module": mod_dir.name, "scripts": scripts, "script_count": len(scripts)})
        out.append({
            "key": ckey,
            "name": co["name"],
            "modules": modules,
            "module_count": len(modules),
            "script_count": sum(m["script_count"] for m in modules),
        })
    return out


@app.get("/vault", response_class=HTMLResponse)
def vault_page(request: Request):
    user = _current_user(request)
    return templates.TemplateResponse(
        request=request,
        name="vault.html",
        context={
            "stats": _stats(),
            "client_id": CLIENT_ID,
            "current_user": user,
            "companies_view": _vault_view(user),
            "active": "vault",
        },
    )


def _company_vault_scenario_ids(company_key: str) -> set:
    """All scenario IDs already filed in a company's Vault (across modules)."""
    ids: set = set()
    co_dir = VAULT_DIR / company_key
    if co_dir.exists():
        for f in co_dir.rglob("*.xlsx"):
            try:
                for s in parse_workbook(str(f)):
                    ids.add(s.scenario_id)
            except Exception:
                pass
    return ids


@app.get("/vault/upload", response_class=HTMLResponse)
def vault_upload_page(request: Request):
    user = _current_user(request)
    company_key = (user.get("company") or "internal").strip().lower()
    company = next((c for c in _load_companies() if c["key"] == company_key), {"key": company_key, "name": company_key})
    return templates.TemplateResponse(
        request=request,
        name="upload.html",
        context={
            "stats": _stats(),
            "client_id": CLIENT_ID,
            "current_user": user,
            "company": company,
            "modules": _MODULE_SUGGESTIONS,
            "dupe": request.query_params.get("dupe", ""),
            "active": "vault",
        },
    )


@app.post("/vault/upload")
async def vault_upload(request: Request, file: UploadFile = File(...), module: str = Form(...)):
    import tempfile
    user = _current_user(request)
    if user.get("username", "guest") == "guest":
        return RedirectResponse("/login", status_code=303)
    if not file.filename or not file.filename.lower().endswith(".xlsx"):
        raise HTTPException(400, "Upload an .xlsx workbook")
    module = module.strip()
    if not module:
        raise HTTPException(400, "Module required")
    company_key = (user.get("company") or "internal").strip().lower()
    if company_key not in _company_keys():
        company_key = "internal"

    data = await file.read()
    # Parse first (to a temp file outside the Vault) and reject if these tasks are
    # already in this company's Vault — stops the same script being added twice.
    tmp = Path(tempfile.gettempdir()) / f"ex3up_{datetime.utcnow().strftime('%Y%m%d%H%M%S%f')}.xlsx"
    tmp.write_bytes(data)
    try:
        new_ids = {s.scenario_id for s in parse_workbook(str(tmp))}
    except Exception:
        new_ids = set()
    finally:
        try:
            tmp.unlink()
        except Exception:
            pass
    dupes = sorted(new_ids & _company_vault_scenario_ids(company_key))
    if dupes:
        return RedirectResponse(f"/vault/upload?dupe={','.join(dupes[:8])}", status_code=303)

    module_seg = _safe_segment(module, "MODULE")
    dest_dir = VAULT_DIR / company_key / module_seg
    dest_dir.mkdir(parents=True, exist_ok=True)
    target = dest_dir / _safe_upload_name(file.filename)
    if target.exists():
        target = dest_dir / f"{target.stem}_{datetime.utcnow().strftime('%Y%m%d%H%M%S')}.xlsx"
    target.write_bytes(data)
    index = _load_vault_index()
    index[f"{company_key}/{module_seg}/{target.name}"] = {
        "company": company_key,
        "module": module,
        "original_name": file.filename,
        "uploaded_by": user.get("username", ""),
        "uploaded_at": _utc_now(),
    }
    _save_vault_index(index)
    return RedirectResponse("/vault", status_code=303)


@app.post("/vault/delete")
def vault_delete(request: Request, key: str = Form(...)):
    user = _current_user(request)
    safe = key.strip().strip("/")
    target = (VAULT_DIR / safe).resolve()
    if not target.is_relative_to(VAULT_DIR.resolve()) or target.suffix != ".xlsx":
        raise HTTPException(400, "Invalid script reference")
    company_key = safe.split("/", 1)[0] if "/" in safe else ""
    if not _role_at_least(user.get("role", "viewer"), "admin") and company_key != (user.get("company") or "internal").strip().lower():
        raise HTTPException(403, "You can only delete your own company's scripts")
    if target.exists():
        target.unlink()
        idx = _load_vault_index()
        idx.pop(safe, None)
        _save_vault_index(idx)
        try:
            parent = target.parent
            if parent.exists() and parent != VAULT_DIR and not any(parent.iterdir()):
                parent.rmdir()
        except Exception:
            pass
    return RedirectResponse("/vault", status_code=303)


def _safe_upload_name(filename: str) -> str:
    import re
    stem = Path(filename).stem
    stem = re.sub(r"[^A-Za-z0-9_.-]+", "_", stem).strip("._") or "uploaded_script"
    return f"{stem}.xlsx"


@app.post("/scripts/upload")
async def upload_script(file: UploadFile = File(...)):
    if not file.filename or not file.filename.lower().endswith(".xlsx"):
        raise HTTPException(400, "Upload an .xlsx workbook")
    UPLOADED_SCRIPTS_DIR.mkdir(parents=True, exist_ok=True)
    target_name = _safe_upload_name(file.filename)
    target = UPLOADED_SCRIPTS_DIR / target_name
    if target.exists():
        target = UPLOADED_SCRIPTS_DIR / f"{target.stem}_{datetime.utcnow().strftime('%Y%m%d%H%M%S')}.xlsx"
    data = await file.read()
    target.write_bytes(data)
    try:
        parse_workbook(str(target))
    except Exception as exc:
        target.unlink(missing_ok=True)
        raise HTTPException(400, f"Workbook could not be parsed: {exc}")
    return RedirectResponse(f"/?script={target.name}", status_code=303)


CATEGORY_RULES = [
    ("Pre-Requisites & System Access", lambda s: s.scenario_id.startswith("LOGIN")),
    ("Recruiting (RCM) — End-to-End Lifecycle", lambda s: s.scenario_id.startswith("RCM")),
    ("Employee Central (EC) — Core HR", lambda s: s.scenario_id.startswith("EC")),
]


def _all_workbook_paths() -> list[Path]:
    """Every workbook the runner can load: legacy script dirs + the Vault tree."""
    return sorted(
        {*SCRIPTS_DIR.glob("*.xlsx"), *UPLOADED_SCRIPTS_DIR.glob("*.xlsx"), *VAULT_DIR.rglob("*.xlsx")},
        key=lambda p: p.name.lower(),
    )


def _key_for_path(p: Path) -> str:
    """Unique address for a workbook: vault-relative path (company/module/file)
    for Vault scripts, bare filename for legacy ones. Prevents two same-named
    Vault files from colliding under one ?script= key."""
    try:
        return p.relative_to(VAULT_DIR).as_posix()
    except ValueError:
        return p.name


def _workbooks() -> list[dict]:
    books = []
    workbook_paths = _all_workbook_paths()
    for path in workbook_paths:
        try:
            scenarios = parse_workbook(str(path))
            step_count = sum(len(s.steps) for s in scenarios)
        except Exception:
            scenarios = []
            step_count = 0
        books.append({
            "key": _key_for_path(path),
            "name": path.stem.replace("_", " ").replace("-", " "),
            "scenario_count": len(scenarios),
            "step_count": step_count,
        })
    return books


def _load_scenarios(workbook: str | None = None):
    workbooks = _all_workbook_paths()
    if workbook:
        workbooks = [p for p in workbooks if _key_for_path(p) == workbook]
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


def _grouped_scenarios(workbook: str | None = None):
    scenarios = _load_scenarios(workbook)
    groups = defaultdict(list)
    OTHER = "Tasks"
    for s in scenarios:
        status = _scenario_status(s.scenario_id, total_steps=len(s.steps))
        entry = {
            "id": s.scenario_id,
            "name": s.name,
            "role": s.role,
            "role_color": _role_color(s.role),
            "step_count": len(s.steps),
            **status,
        }
        # Place into the first matching category, else a catch-all — never drop a scenario.
        label = next((lbl for lbl, pred in CATEGORY_RULES if pred(s)), OTHER)
        groups[label].append(entry)
    order = [lbl for lbl, _ in CATEGORY_RULES] + [OTHER]
    return [
        {
            "label": label,
            "scenarios": groups[label],
            "scenario_count": len(groups[label]),
            "step_count": sum(sc["step_count"] for sc in groups[label]),
        }
        for label in order
        if groups[label]
    ]


def _stats(workbook: str | None = None):
    scenarios = _load_scenarios(workbook)
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


def _load_step_library() -> dict:
    if LIBRARY_FILE.exists():
        try:
            data = _clean_library(json.loads(LIBRARY_FILE.read_text()))
            if isinstance(data, dict):
                return data
        except Exception:
            return {}
    return {}


def _match_script_to_library(scenarios, library: dict) -> dict:
    """Ask Claude which known task each scenario performs — judged by the WHOLE
    process, not shared keywords. Returns
    {scenario_id: {"match": task_name|None, "reason": str}}."""
    result = {s.scenario_id: {"match": None, "reason": ""} for s in scenarios}
    api_key = os.getenv("ANTHROPIC_API_KEY", "")
    tasks = {n: e for n, e in library.items() if isinstance(e, dict) and e.get("steps")}
    if not api_key or not tasks or not scenarios:
        return result
    task_lines = []
    for n, e in tasks.items():
        step_desc = _task_step_descriptions(e)
        known_steps = " | ".join(step_desc[:10]) if step_desc else ""
        saved_count = len(e.get("steps") or {})
        task_lines.append(
            f"- {n}: goal = {e.get('description', n)} | saved commands = {saved_count} step(s)"
            + (f" | step meaning = {known_steps}" if known_steps else "")
        )
    scen_lines = []
    for s in scenarios:
        steps = "; ".join((st.action or "") for st in s.steps[:8])
        scen_lines.append(f"[{s.scenario_id}] {s.name} — steps: {steps}")
    try:
        import anthropic
        client = anthropic.Anthropic(api_key=api_key)
        msg = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=800,
            messages=[{"role": "user", "content": (
                "Decide whether each SAP SuccessFactors test scenario performs the SAME end-to-end "
                "process as one of the known tasks. Judge the WHOLE task and its overall goal — NOT "
                "individual steps. Two tasks that merely share one step (e.g. both pass through the "
                "module picker on the way to somewhere else) are NOT a match. Only match when the "
                "scenario's overall purpose and sequence are essentially the same as the known task. "
                "When unsure, do NOT match.\n\n"
                "Known tasks:\n" + "\n".join(task_lines) + "\n\n"
                "Scenarios:\n" + "\n".join(scen_lines) + "\n\n"
                "Reply with ONLY a JSON object mapping each scenario id to an object "
                '{"match": "<exact known task name>" or null, "reason": "<one short sentence>"}. '
                'Example: {"LOGIN-102": {"match": "Proxy Login", "reason": "Both switch into another '
                'user\'s session via Proxy Now."}, "RCM-RC-101": {"match": null, "reason": "Creating a '
                'position in the org chart is a different process from navigating modules."}}'
            )}],
        )
        raw = msg.content[0].text.strip()
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            raw = raw[4:] if raw.startswith("json") else raw
        parsed = json.loads(raw.strip())
        for sid, val in parsed.items():
            if sid not in result or not isinstance(val, dict):
                continue
            name = val.get("match")
            result[sid] = {
                "match": name if (isinstance(name, str) and name in tasks) else None,
                "reason": str(val.get("reason", ""))[:240],
            }
    except Exception as exc:
        print(f"[match] error: {exc}")
    return result


def _step_descriptions_for_scenario(scenario) -> list:
    """Rich, human-readable description per step: action + expected result."""
    out = []
    for st in getattr(scenario, "steps", []) or []:
        d = st.action or ""
        if st.expected_result:
            d += f" — expected: {st.expected_result}"
        out.append(d)
    return out


def _task_step_descriptions(entry: dict) -> list:
    """Best step-level descriptions for a saved task: stored ones, else step
    actions, else re-derived from the source scenario (backfill for old tasks)."""
    if entry.get("step_descriptions"):
        return entry["step_descriptions"]
    if entry.get("step_actions"):
        return entry["step_actions"]
    sid = entry.get("scenario_id")
    if sid:
        sc = next((s for s in _load_scenarios() if s.scenario_id == sid), None)
        if sc:
            return _step_descriptions_for_scenario(sc)
    return []


def _library_entry_variables(entry: dict) -> list[str]:
    import re
    all_cmds = "\n".join((entry.get("steps") or {}).values()) if isinstance(entry, dict) else ""
    return sorted(set(re.findall(r"\{\{(\w+)\}\}", all_cmds)))


def _enrich_library_entries(library: dict) -> tuple[dict, int]:
    """Backfill older task-library entries with rich step descriptions and
    has_learned_commands so future matching sees the full process."""
    changed = 0
    scenarios = {s.scenario_id: s for s in _load_scenarios()}
    for name, entry in list(library.items()):
        if not isinstance(entry, dict):
            continue
        sid = entry.get("scenario_id")
        scenario = scenarios.get(sid) if sid else None
        if scenario:
            if not entry.get("step_actions"):
                entry["step_actions"] = [st.action for st in scenario.steps]
                changed += 1
            if not entry.get("step_descriptions"):
                entry["step_descriptions"] = _step_descriptions_for_scenario(scenario)
                changed += 1
        if "has_learned_commands" not in entry:
            entry["has_learned_commands"] = bool(entry.get("steps"))
            changed += 1
        library[name] = entry
    return library, changed


def _ai_task_description(scenario, user_note: str) -> str:
    """Write a clear, detailed description of what a task accomplishes, so it can
    be recognised later under different wording. Falls back to the user's note."""
    fallback = (user_note or "").strip() or (getattr(scenario, "name", "") or "")
    api_key = os.getenv("ANTHROPIC_API_KEY", "")
    if not api_key or not getattr(scenario, "steps", None):
        return fallback
    steps_txt = "\n".join(
        f"{i+1}. {st.action}" + (f" (expected: {st.expected_result})" if st.expected_result else "")
        for i, st in enumerate(scenario.steps)
    )
    try:
        import anthropic
        client = anthropic.Anthropic(api_key=api_key)
        msg = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=300,
            messages=[{"role": "user", "content": (
                "Write a clear, detailed 2-4 sentence description of what this SAP SuccessFactors task "
                "accomplishes — its goal and the key actions — so it can be recognised later even when "
                "described in different words. Avoid UI minutiae.\n\n"
                f"Task name: {scenario.name}\n"
                + (f"User note: {user_note}\n" if user_note else "")
                + f"Steps:\n{steps_txt}\n\nReturn only the description."
            )}],
        )
        return msg.content[0].text.strip() or fallback
    except Exception:
        return fallback


def _coverage_for_scenario(scenario, library: dict) -> dict:
    """Best-matching library task + per-step coverage for ONE scenario.
    Returns {"matched_task": name|None, "reason": str, "coverage": {step_id: bool}}.
    Steps the matched task can perform are covered; extra/company-specific steps
    it can't perform are gaps."""
    out = {"matched_task": None, "reason": "", "coverage": {}}
    api_key = os.getenv("ANTHROPIC_API_KEY", "")
    tasks = {n: e for n, e in library.items() if isinstance(e, dict) and e.get("steps")}
    if not api_key or not tasks or not getattr(scenario, "steps", None):
        return out
    task_lines = []
    for n, e in tasks.items():
        sd = _task_step_descriptions(e)
        known = " | ".join(sd) if sd else e.get("description", n)
        n_saved = len(e.get("steps") or {})
        task_lines.append(f"- {n}: goal = {e.get('description', n)} | has saved commands for {n_saved} step(s) | its steps = {known}")
    step_lines = [
        f"[{st.step_id}] {st.action}" + (f" (expected: {st.expected_result})" if st.expected_result else "")
        for st in scenario.steps
    ]
    try:
        import anthropic
        client = anthropic.Anthropic(api_key=api_key)
        msg = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=700,
            messages=[{"role": "user", "content": (
                "A user is about to run this SAP SuccessFactors test scenario. Decide if it is the SAME "
                "end-to-end process as one of the known library tasks — judge by goal/intent, not wording. "
                "If it matches, mark which of the scenario's steps the known task already covers, and treat "
                "the rest (extra or company-specific steps the saved task can't perform) as gaps.\n\n"
                "Known tasks:\n" + "\n".join(task_lines) + "\n\n"
                "Scenario steps:\n" + "\n".join(step_lines) + "\n\n"
                "Reply with ONLY JSON: {\"matched_task\": \"<exact task name>\" or null, "
                "\"reason\": \"<one short sentence>\", \"covered_step_ids\": [\"<step_id>\", ...]}. "
                "covered_step_ids lists only the scenario step ids the matched task can perform."
            )}],
        )
        raw = msg.content[0].text.strip()
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            raw = raw[4:] if raw.startswith("json") else raw
        parsed = json.loads(raw.strip())
        name = parsed.get("matched_task")
        if isinstance(name, str) and name in tasks:
            covered = set(parsed.get("covered_step_ids") or [])
            # Also covered: any step the matched task already has a saved command
            # for (e.g. it was learned from this same scenario). The AI's judgement
            # handles reworded/cross-scenario cases; this handles exact reuse.
            saved_step_ids = set((tasks[name].get("steps") or {}).keys())
            out["matched_task"] = name
            out["reason"] = str(parsed.get("reason", ""))[:240]
            out["coverage"] = {
                st.step_id: (st.step_id in covered or st.step_id in saved_step_ids)
                for st in scenario.steps
            }
    except Exception as exc:
        print(f"[coverage] error: {exc}")
    return out


@app.get("/api/coverage/{scenario_id}")
def api_coverage(request: Request, scenario_id: str, script: str = ""):
    scenario = next((s for s in _load_scenarios(script or None) if s.scenario_id == scenario_id), None)
    if not scenario:
        return JSONResponse({"ok": False, "error": "Scenario not found"})
    library = _load_step_library()
    cov = _coverage_for_scenario(scenario, library)
    steps = [
        {"step_id": st.step_id, "action": st.action, "covered": bool(cov["coverage"].get(st.step_id))}
        for st in scenario.steps
    ]
    covered_count = sum(1 for s in steps if s["covered"])
    total = len(steps)
    # Placeholders ({{name}}) in THIS scenario's own saved commands — so the run
    # prompts for them even when not using a library task.
    import re as _re
    _own_cmds = {}
    _own_cmds.update(_load_feedback().get(scenario_id, {}) or {})
    _own_cmds.update((_load_approved().get(scenario_id, {}) or {}).get("step_commands", {}) or {})
    scenario_variables = sorted({m for c in _own_cmds.values() for m in _re.findall(r"\{\{(\w+)\}\}", c or "")})
    return JSONResponse({
        "ok": True,
        "matched_task": cov["matched_task"],
        "reason": cov["reason"],
        "steps": steps,
        "covered_count": covered_count,
        "total": total,
        "confidence": round(covered_count / total * 100) if total else 0,
        "library_tasks": [n for n, e in library.items() if isinstance(e, dict) and e.get("steps")],
        "scenario_variables": scenario_variables,
        "task_variables": {
            n: _library_entry_variables(e)
            for n, e in library.items()
            if isinstance(e, dict) and e.get("steps")
        },
    })


@app.get("/api/batch/plan")
def api_batch_plan(request: Request, script: str = ""):
    scenarios = _load_scenarios(script or None)
    if not script or not scenarios:
        return JSONResponse({"ok": False, "error": "Select a script before running all."}, status_code=400)
    library = _load_step_library()
    library_size = len([e for e in library.values() if isinstance(e, dict) and e.get("steps")])
    cached = _cached_matches(script or "__all__", library_size, scenarios)
    cache_hit = cached is not None
    if cached is None:
        cached = _compute_match_results(scenarios, library)
        _cache_matches(script or "__all__", cached, library_size, scenarios=scenarios)

    cached_by_id = {row["scenario_id"]: row for row in cached}
    items = []
    variables: dict[str, str] = {}
    for scenario in scenarios:
        row = cached_by_id.get(scenario.scenario_id, {})
        total = len(scenario.steps)
        covered = int(row.get("covered_count") or 0)
        matched = row.get("matched_to")
        task_vars = _library_entry_variables(library.get(matched, {})) if matched else []
        for var in task_vars:
            variables[var] = var.replace("_", " ").title()
        items.append({
            "scenario_id": scenario.scenario_id,
            "name": scenario.name,
            "role": scenario.role,
            "module": scenario.module,
            "steps": total,
            "matched_task": matched,
            "confidence": int(row.get("confidence") or (round(covered / total * 100) if total else 0)),
            "covered_count": covered,
            "reason": str(row.get("reason", ""))[:240],
            "variables": task_vars,
            "status": "queued",
        })
    return JSONResponse({
        "ok": True,
        "script": script,
        "total": len(items),
        "cache_hit": cache_hit,
        "items": items,
        "variables": [{"key": k, "label": v} for k, v in variables.items()],
    })


def _batch_run_record(batch_id: str, item: dict, scenario, script: str, answers: dict, user: dict) -> None:
    run_id = f"{batch_id}_{scenario.scenario_id}"
    if scenario.scenario_id in _PAUSE_EVENTS:
        _PAUSE_FIX[scenario.scenario_id] = None
        _PAUSE_EVENTS[scenario.scenario_id].set()
    _LIVE_SHOT_PATHS.pop(scenario.scenario_id, None)
    _LIVE_QUEUES.pop(scenario.scenario_id, None)

    existing_approved = ((_load_approved().get(scenario.scenario_id, {}) or {}).get("step_commands", {}) or {})
    existing_feedback = _load_feedback().get(scenario.scenario_id, {}) or {}
    pre_saved_commands = {**existing_feedback, **existing_approved}
    trained_step_ids = {
        step_id
        for step_id, commands in pre_saved_commands.items()
        if _has_replay_commands(commands)
    }
    fully_trained = all(st.step_id in trained_step_ids for st in scenario.steps)
    item.update({
        "status": "running",
        "run_id": run_id,
        "started_at": _utc_now(),
        "error": "",
        "trained_to_library": False,
    })
    _ACTIVE_RUNS[scenario.scenario_id] = {
        "status": "running",
        "run_id": run_id,
        "supervised": False,
        "live_mode": False,
        "user": user,
        "script": script,
    }
    _append_audit(
        run_id,
        "run_started",
        "Batch run started",
        scenario_id=scenario.scenario_id,
        user=user,
        status="running",
        details={"script": script, "batch_id": batch_id, "answers": answers, "step_count": len(scenario.steps)},
    )
    step_log_file = RUNS_DIR / f"{scenario.scenario_id}_last_run.json"
    per_run_log = RUNS_DIR / run_id / "steps.json"
    steps_log: list[dict] = []

    def _write_step_log(run_status: str) -> None:
        payload = json.dumps({
            "run_id": run_id, "scenario_id": scenario.scenario_id, "script": script,
            "status": run_status, "steps": steps_log,
        }, indent=2)
        for target in (step_log_file, per_run_log):
            try:
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(payload)
            except Exception:
                pass

    def _step_done(step_id, passed, error, screenshot_url):
        steps_log.append({
            "step_id": step_id,
            "passed": passed,
            "error": error or "",
            "screenshot_url": screenshot_url or "",
        })
        item["steps_logged"] = len(steps_log)
        item["steps_passed"] = sum(1 for s in steps_log if s.get("passed"))
        item["current_step"] = step_id
        _write_step_log("running")
        _append_audit(
            run_id,
            "step_completed" if passed else "step_failed",
            "Step passed" if passed else "Step failed",
            scenario_id=scenario.scenario_id,
            user=user,
            step_id=step_id,
            status="pass" if passed else "fail",
            details={"error": error or "", "screenshot_url": screenshot_url or ""},
        )

    result = run_scenario(
        scenario,
        runs_root=RUNS_DIR,
        headless=True,
        pause_callback=lambda **kw: _pause_callback(**kw),
        initial_context=answers,
        step_done_callback=_step_done,
        check_pause_fn=lambda sid: _FORCE_PAUSE.pop(sid, False),
        live_mode=False,
        # Run All now acts as an assisted agent. It replays learned steps, lets
        # Claude Vision attempt unknown steps, and hands over to the human when
        # it cannot continue or reaches a final Save/Submit.
        use_memory=True,
        manual=False,
        no_guess=fully_trained,
        # Run All is allowed to pause into live control. Unknown steps should be
        # trained in the same browser session so later scenarios can continue.
        unattended=False,
        run_id_override=run_id,
    )
    latest_approved = ((_load_approved().get(scenario.scenario_id, {}) or {}).get("step_commands", {}) or {})
    latest_feedback = _load_feedback().get(scenario.scenario_id, {}) or {}
    merged_commands = {**latest_approved, **latest_feedback}
    if result.passed and merged_commands and merged_commands != pre_saved_commands:
        item["pending_library_review"] = True
    videos = sorted((RUNS_DIR / run_id).glob("*.webm"))
    item["video_url"] = f"/runs/{run_id}/{videos[0].name}" if videos else ""
    item["ended_at"] = _utc_now()
    item["steps_logged"] = len(steps_log)
    item["steps_passed"] = sum(1 for s in steps_log if s.get("passed"))
    item["status"] = "passed" if result.passed else "failed"
    _write_step_log("done" if result.passed else "failed")
    _append_audit(
        run_id,
        "run_completed" if result.passed else "run_failed",
        "Batch scenario completed" if result.passed else "Batch scenario stopped before completion",
        scenario_id=scenario.scenario_id,
        user=user,
        status="passed" if result.passed else "failed",
        details={"batch_id": batch_id, "steps_logged": len(steps_log), "video_url": item["video_url"]},
    )
    _ACTIVE_RUNS[scenario.scenario_id] = {
        "status": "done" if result.passed else "error",
        "run_id": run_id,
        "passed": result.passed,
        "user": user,
        "script": script,
    }


_REPLAY_PREFIXES = ("CLICK:", "CLICK_XY:", "TYPE:", "PRESS:", "WAIT:", "FILL:", "SHADOW_CLICK:", "GOTO:", "NAVIGATE:", "SELECT:", "SELECT_OPTION:", "JS:")


def _has_replay_commands(commands: str) -> bool:
    return any(str(line).strip().upper().startswith(_REPLAY_PREFIXES) for line in (commands or "").splitlines())


def _save_reviewed_task_to_library(scenario, commands: dict, user: dict, run_id: str, task_name: str = "", note: str = "") -> dict:
    """Lock a reviewed task and add it to the reusable library."""
    if not commands:
        return {"ok": False, "error": "No recorded commands to save."}
    approved = _load_approved()
    approved[scenario.scenario_id] = {
        "approved_at": datetime.utcnow().isoformat(),
        "step_commands": commands,
    }
    _save_approved(approved)

    task_name = (task_name or getattr(scenario, "name", "") or scenario.scenario_id).strip()
    note = (note or "Approved after reviewing the recorded run video.").strip()
    library = _load_library()
    library[task_name] = {
        "description": _ai_task_description(scenario, note),
        "note": note,
        "scenario_id": scenario.scenario_id,
        "steps": commands,
        "step_actions": [st.action for st in scenario.steps],
        "step_descriptions": _step_descriptions_for_scenario(scenario),
        "has_learned_commands": True,
    }
    _save_library(library)
    _append_audit(
        run_id,
        "library_task_reviewed_saved",
        "Reviewed run saved to Task Library",
        scenario_id=scenario.scenario_id,
        user=user,
        status="saved",
        details={"task_name": task_name, "steps_saved": len(commands)},
    )
    threading.Thread(target=_git_push_approved, daemon=True).start()
    threading.Thread(target=_git_push_library, daemon=True).start()
    return {"ok": True, "task_name": task_name, "steps_saved": len(commands)}


@app.post("/api/batch/start")
async def api_batch_start(request: Request):
    try:
        body = await request.json()
    except Exception:
        body = {}
    script = str(body.get("script", "")).strip()
    answers = body.get("answers") if isinstance(body.get("answers"), dict) else {}
    scenarios = _load_scenarios(script or None)
    if not script or not scenarios:
        return JSONResponse({"ok": False, "error": "Select a script before running all."}, status_code=400)
    user = _current_user(request)
    batch_id = datetime.utcnow().strftime("B%Y%m%dT%H%M%SZ")
    items = [
        {
            "scenario_id": s.scenario_id,
            "name": s.name,
            "role": s.role,
            "module": s.module,
            "steps": len(s.steps),
            "steps_logged": 0,
            "steps_passed": 0,
            "status": "queued",
            "run_id": "",
            "video_url": "",
            "error": "",
        }
        for s in scenarios
    ]
    _BATCH_RUNS[batch_id] = {
        "id": batch_id,
        "script": script,
        "status": "running",
        "started_at": _utc_now(),
        "ended_at": "",
        "user": user,
        "total": len(items),
        "current_index": 0,
        "items": items,
    }

    def _run_batch():
        batch = _BATCH_RUNS[batch_id]
        try:
            for index, scenario in enumerate(scenarios):
                batch["current_index"] = index
                item = batch["items"][index]
                _batch_run_record(batch_id, item, scenario, script, answers, user)
                if item["status"] != "passed":
                    batch["status"] = "needs_training"
                    batch["ended_at"] = _utc_now()
                    return
            batch["status"] = "completed"
            batch["ended_at"] = _utc_now()
        except Exception as exc:
            batch["status"] = "error"
            batch["ended_at"] = _utc_now()
            batch["error"] = str(exc)

    threading.Thread(target=_run_batch, daemon=True).start()
    return JSONResponse({"ok": True, "batch_id": batch_id})


@app.get("/api/batch/{batch_id}")
def api_batch_status(batch_id: str):
    batch = _BATCH_RUNS.get(batch_id)
    if not batch:
        return JSONResponse({"ok": False, "error": "Batch not found"}, status_code=404)
    for item in batch.get("items", []):
        active = _ACTIVE_RUNS.get(item.get("scenario_id", ""), {})
        if active.get("run_id") == item.get("run_id") and active.get("status") in ("paused", "confirming"):
            item["status"] = active["status"]
            item["paused_step"] = active.get("paused_step") or active.get("confirming_step", "")
            item["screenshot_url"] = active.get("screenshot_url", "")
    return JSONResponse({"ok": True, "batch": batch})


_SHOWREEL_HTML = r"""<!DOCTYPE html>
<html lang="en"><head><meta charset="UTF-8"/><meta name="viewport" content="width=device-width, initial-scale=1.0"/>
<title>EX3 TestOps — Showreel</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800;900&display=swap" rel="stylesheet">
<style>
*{box-sizing:border-box;margin:0;padding:0}
html,body{height:100%}
body{background:#0a0a09;font-family:Inter,sans-serif;display:grid;place-items:center;overflow:hidden}
/* Square stage — ideal for LinkedIn. Screen-record this box. */
.stage{position:relative;width:min(92vw,92vh);aspect-ratio:1/1;background:
  radial-gradient(circle at 20% 12%,rgba(216,186,114,.16),transparent 34%),
  linear-gradient(150deg,#0c0b0a,#161513 60%,#0b0b0c);
  border-radius:24px;overflow:hidden;box-shadow:0 40px 120px rgba(0,0,0,.6);color:#f7f3ea}
.scene{position:absolute;inset:0;display:flex;flex-direction:column;align-items:center;justify-content:center;
  padding:7% 9%;opacity:0;transform:scale(1.02);transition:opacity .6s ease,transform .6s ease;pointer-events:none}
.scene.on{opacity:1;transform:scale(1);pointer-events:auto}
.mark{width:54px;height:54px;border-radius:15px;display:grid;place-items:center;font-weight:900;font-size:15px;
  color:#0b0b0c;background:linear-gradient(135deg,#d8ba72,#91713e);box-shadow:0 12px 40px rgba(216,186,114,.3)}
.big{font-size:clamp(26px,5vw,46px);font-weight:900;letter-spacing:-.02em;line-height:1.05;text-align:center}
.sub{margin-top:14px;font-size:clamp(13px,2.2vw,18px);color:#bcb5a8;text-align:center;max-width:80%}
.cap{position:absolute;left:8%;bottom:8%;font-size:clamp(15px,2.6vw,22px);font-weight:800;
  background:rgba(11,11,12,.55);backdrop-filter:blur(8px);padding:10px 16px;border-radius:12px;
  border:1px solid rgba(226,207,159,.18)}
.kick{color:#d8ba72;font-size:clamp(11px,1.8vw,13px);font-weight:800;letter-spacing:.14em;text-transform:uppercase;margin-bottom:10px}
/* fake app panel */
.panel{width:100%;max-width:640px;background:rgba(255,255,255,.05);border:1px solid rgba(255,255,255,.1);
  border-radius:16px;padding:20px;backdrop-filter:blur(10px)}
.row{display:flex;align-items:center;gap:12px;padding:12px 14px;border-radius:10px;background:rgba(255,255,255,.04);
  border:1px solid rgba(255,255,255,.06);margin-bottom:8px;font-size:14px;font-weight:600;opacity:0;transform:translateY(8px);
  animation:rise .5s forwards}
.row .tick{margin-left:auto;width:22px;height:22px;border-radius:50%;display:grid;place-items:center;
  background:rgba(31,157,90,.18);color:#5fe3a0;font-weight:900;font-size:13px;opacity:0;animation:pop .4s forwards}
.pill{margin-left:auto;font-size:12px;font-weight:800;color:#5fe3a0;background:rgba(31,157,90,.12);
  border:1px solid rgba(31,157,90,.25);border-radius:999px;padding:4px 12px;opacity:0;animation:pop .5s forwards}
@keyframes rise{to{opacity:1;transform:translateY(0)}}
@keyframes pop{to{opacity:1}}
.finger{width:120px;height:120px;border-radius:50%;border:3px solid rgba(216,186,114,.5);display:grid;place-items:center;
  margin-top:24px;animation:pulse 1.4s infinite}
.finger svg{width:54px;height:54px;stroke:#d8ba72}
@keyframes pulse{0%,100%{box-shadow:0 0 0 0 rgba(216,186,114,.4)}50%{box-shadow:0 0 0 22px rgba(216,186,114,0)}}
.cursor{position:absolute;width:18px;height:18px;border-radius:50%;background:#fff;box-shadow:0 0 0 4px rgba(255,255,255,.25);
  left:50%;top:50%;transition:all 1s cubic-bezier(.6,.1,.2,1);z-index:5}
.runbar{width:100%;max-width:560px;height:10px;border-radius:999px;background:rgba(255,255,255,.08);overflow:hidden;margin-top:26px}
.runbar i{display:block;height:100%;width:0;background:linear-gradient(90deg,#d8ba72,#5fe3a0);animation:fill 3.4s forwards}
@keyframes fill{to{width:100%}}
.videobox{width:100%;max-width:560px;aspect-ratio:16/9;border-radius:14px;margin-top:22px;
  background:linear-gradient(135deg,#11100e,#1c1a16);border:1px solid rgba(226,207,159,.18);
  display:grid;place-items:center;color:#8a8478;position:relative;overflow:hidden}
.videobox::after{content:"";position:absolute;inset:0;background:linear-gradient(120deg,transparent 30%,rgba(255,255,255,.06) 50%,transparent 70%);
  animation:sheen 2.2s infinite}
@keyframes sheen{to{transform:translateX(60%)}}
.play{width:60px;height:60px;border-radius:50%;background:rgba(216,186,114,.9);display:grid;place-items:center}
.play svg{width:24px;height:24px;fill:#0b0b0c;margin-left:3px}
.progress{position:absolute;left:0;bottom:0;height:4px;background:#d8ba72;width:0;animation:bar 16s linear forwards}
@keyframes bar{to{width:100%}}
.replay{position:absolute;right:18px;bottom:18px;z-index:9;background:rgba(255,255,255,.08);color:#f7f3ea;border:1px solid rgba(226,207,159,.2);
  border-radius:10px;padding:8px 14px;font:inherit;font-size:13px;font-weight:700;cursor:pointer}
</style></head>
<body>
<div class="stage" id="stage">
  <div class="progress" id="prog"></div>

  <div class="scene" data-t="0">
    <div class="mark">EX3</div>
    <div class="big" style="margin-top:24px">UAT testing for SAP SuccessFactors.<br>Watch it run itself.</div>
  </div>

  <div class="scene" data-t="1">
    <div class="kick">Set up a client</div>
    <div class="panel">
      <div class="row" style="animation-delay:.1s">🏢 Northwind Group <span class="pill" style="animation-delay:1s">company added</span></div>
      <div class="row" style="animation-delay:1.4s">👤 j.shah · Tester <span class="pill" style="animation-delay:2.2s">Northwind</span></div>
    </div>
    <div class="cap">Add a client. Assign a user. Set their access.</div>
  </div>

  <div class="scene" data-t="2">
    <div class="kick">Secure login</div>
    <div class="big">No passwords.<br>Just you.</div>
    <div class="finger"><svg fill="none" viewBox="0 0 24 24" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><path d="M12 10.5c0 3.5-.2 6-1.2 8.5"/><path d="M8.2 10a3.8 3.8 0 0 1 7.6 0c0 4-.7 6.2-1 7"/><path d="M5.2 10.6a6.8 6.8 0 0 1 11.4-4.7"/></svg></div>
    <div class="cap">Sign in with your fingerprint.</div>
  </div>

  <div class="scene" data-t="3">
    <div class="kick">Drop in any script</div>
    <div class="panel">
      <div class="row" style="animation-delay:.1s">📄 EX3_RCM_Workbook.xlsx <span class="pill" style="animation-delay:.9s">uploaded</span></div>
      <div class="row" style="animation-delay:1.3s">📁 Northwind → RCM → filed <span class="pill" style="animation-delay:2.1s">in the Vault</span></div>
    </div>
    <div class="cap">Upload a UAT script. It files itself.</div>
  </div>

  <div class="scene" data-t="4">
    <div class="kick">It already knows</div>
    <div class="panel">
      <div class="row" style="animation-delay:.1s">Navigating Modules <span class="pill" style="animation-delay:.8s">matched · 100%</span></div>
      <div class="row" style="animation-delay:1.1s">How to Proxy <span class="pill" style="animation-delay:1.8s">matched · 100%</span></div>
      <div class="row" style="animation-delay:2.1s">Create a Position <span class="pill" style="animation-delay:2.8s">matched · 100%</span></div>
    </div>
    <div class="cap">It recognises what it's done before.</div>
  </div>

  <div class="scene" data-t="5">
    <div class="kick">The money shot</div>
    <div class="big">Then it runs<br>itself.</div>
    <div class="runbar"><i></i></div>
    <div class="panel" style="max-width:520px;margin-top:22px">
      <div class="row" style="animation-delay:.3s">Step 1 · Open module picker <span class="tick" style="animation-delay:1s">✓</span></div>
      <div class="row" style="animation-delay:1.4s">Step 2 · Proxy as user <span class="tick" style="animation-delay:2.1s">✓</span></div>
      <div class="row" style="animation-delay:2.4s">Step 3 · Create position <span class="tick" style="animation-delay:3.1s">✓</span></div>
    </div>
    <div class="cap">No hands. It drives SuccessFactors.</div>
  </div>

  <div class="scene" data-t="6">
    <div class="kick">Proof &amp; memory</div>
    <div class="videobox"><div class="play"><svg viewBox="0 0 24 24"><path d="M8 5v14l11-7z"/></svg></div></div>
    <div class="cap">Every run is filmed. Every fix, remembered.</div>
  </div>

  <div class="scene" data-t="7">
    <div class="mark">EX3</div>
    <div class="big" style="margin-top:24px">EX3 TestOps</div>
    <div class="sub">SuccessFactors UAT — automated, watched, and learned.</div>
  </div>

  <button class="replay" id="replay">↻ Replay</button>
</div>
<script>
// Scene timeline (seconds into the reel). Total ~32s.
const T = [0, 4, 9.5, 14, 18.5, 23, 28, 31];
const scenes = [...document.querySelectorAll('.scene')];
let timers = [];
function play(){
  timers.forEach(clearTimeout); timers = [];
  scenes.forEach(s=>s.classList.remove('on'));
  const prog = document.getElementById('prog');
  prog.style.animation='none'; void prog.offsetWidth; prog.style.animation='bar 32s linear forwards';
  T.forEach((t,i)=>{
    timers.push(setTimeout(()=>{
      scenes.forEach(s=>s.classList.remove('on'));
      scenes[i].classList.add('on');
      // re-trigger row/tick animations by cloning
      scenes[i].querySelectorAll('.row,.tick,.pill,.runbar i').forEach(el=>{
        const a=el.style.animation; el.style.animation='none'; void el.offsetWidth; el.style.animation=a||'';
      });
    }, t*1000));
  });
}
document.getElementById('replay').addEventListener('click',play);
play();
</script>
</body></html>"""


@app.get("/showreel", response_class=HTMLResponse)
def showreel(request: Request):
    """Owner-only, unlinked animated trailer page. Screen-record it for marketing.
    Not in any nav; 404s for anyone who isn't the owner."""
    user = _current_user(request)
    if not _role_at_least(user.get("role", "viewer"), "owner"):
        return HTMLResponse("Not found", status_code=404)
    return HTMLResponse(_SHOWREEL_HTML)


@app.get("/batch", response_class=HTMLResponse)
def batch_page(request: Request, script: str = ""):
    selected_name = next((w["name"] for w in _workbooks() if w["key"] == script), script or "Run all")
    return templates.TemplateResponse(
        request=request,
        name="batch.html",
        context={
            "script": script,
            "selected_name": selected_name,
            "stats": _stats(script or None),
            "client_id": CLIENT_ID,
            "current_user": _current_user(request),
            "active": "dashboard",
        },
    )


@app.get("/testing-hub", response_class=HTMLResponse)
def testing_hub(request: Request):
    return templates.TemplateResponse(
        request=request,
        name="lab.html",
        context={
            "stats": _stats(),
            "client_id": CLIENT_ID,
            "current_user": _current_user(request),
            "active": "lab",
        },
    )


@app.post("/api/lab/run")
async def api_lab_run(request: Request):
    """Isolated free-form agent: take a plain-English goal and let Opus drive SF
    in preview (no-save) mode. Does NOT touch scenarios, library, or saved data."""
    from engine.runner import run_agent_goal
    try:
        body = await request.json()
    except Exception:
        body = {}
    goal = str(body.get("goal", "")).strip()
    if not goal:
        return JSONResponse({"ok": False, "error": "Describe what to do first."}, status_code=400)
    if _ACTIVE_RUNS.get("agent", {}).get("status") == "running":
        return JSONResponse({"ok": False, "error": "An agent run is already in progress."}, status_code=409)
    user = _current_user(request)
    run_id = datetime.utcnow().strftime("%Y%m%dT%H%M%SZ")
    _ACTIVE_RUNS["agent"] = {"status": "running", "run_id": run_id, "user": user, "goal": goal}
    log_file = RUNS_DIR / "agent_last_run.json"
    steps_log: list[dict] = []

    def _write(status: str):
        try:
            log_file.write_text(json.dumps({"run_id": run_id, "status": status, "steps": steps_log}, indent=2))
        except Exception:
            pass

    def _step_done(step_id, passed, error, screenshot_url):
        steps_log.append({"step_id": step_id, "passed": passed, "error": error or "", "screenshot_url": screenshot_url or ""})
        _write("running")

    _write("running")

    def _run():
        try:
            result = run_agent_goal(goal, preview=True, runs_root=RUNS_DIR, run_id_override=run_id,
                                    step_done_callback=_step_done, max_iters=12)
            _write("done")
            _ACTIVE_RUNS["agent"] = {"status": "done", "run_id": run_id, "passed": result.passed, "user": user}
        except Exception as exc:
            _write("error")
            _ACTIVE_RUNS["agent"] = {"status": "error", "run_id": run_id, "error": str(exc), "user": user}

    threading.Thread(target=_run, daemon=True).start()
    return JSONResponse({"ok": True, "run_id": run_id})


@app.get("/api/lab/video/{run_id}")
def api_lab_video(run_id: str):
    run_dir = RUNS_DIR / run_id
    videos = sorted(run_dir.glob("*.webm")) if run_dir.exists() else []
    return JSONResponse({"ok": True, "video_url": f"/runs/{run_id}/{videos[0].name}" if videos else ""})


@app.get("/api/match")
def api_match(request: Request, script: str = ""):
    scenarios = _load_scenarios(script or None)
    if not scenarios:
        return JSONResponse({"ok": False, "error": "No scenarios found for this script"})
    library = _load_step_library()
    library, enriched = _enrich_library_entries(library)
    if enriched:
        _save_library(library)
    library_size = len([e for e in library.values() if isinstance(e, dict) and e.get("steps")])
    results = _cached_matches(script or "__all__", library_size, scenarios)
    cache_hit = results is not None
    if not results:
        results = _compute_match_results(scenarios, library)
        _cache_matches(script or "__all__", results, library_size, scenarios=scenarios)
    return JSONResponse({
        "ok": True,
        "total": len(results),
        "matched": sum(1 for r in results if r["matched_to"]),
        "library_size": library_size,
        "enriched": enriched,
        "cached": True,
        "cache_hit": cache_hit,
        "results": results,
    })


@app.post("/api/library/rescan")
def rescan_library(request: Request):
    """Refresh library metadata and re-run matching for every existing workbook.

    This makes newly saved library tasks apply to scripts that were uploaded
    before the task existed.
    """
    library = _load_library()
    library, enriched = _enrich_library_entries(library)
    if enriched:
        _save_library(library)
    library_size = len([e for e in library.values() if isinstance(e, dict) and e.get("steps")])
    scanned = []
    total_matched = 0
    for wb in _workbooks():
        key = wb["key"]
        scenarios = _load_scenarios(key)
        if not scenarios:
            continue
        results = _compute_match_results(scenarios, library)
        matched_count = sum(1 for r in results if r["matched_to"])
        total_matched += matched_count
        _cache_matches(key, results, library_size, scenarios=scenarios)
        scanned.append({
            "script": key,
            "name": wb["name"],
            "total": len(results),
            "matched": matched_count,
        })
    return JSONResponse({
        "ok": True,
        "library_size": library_size,
        "enriched": enriched,
        "scripts_scanned": len(scanned),
        "total_matched": total_matched,
        "scanned": scanned,
    })


@app.get("/", response_class=HTMLResponse)
def home(request: Request):
    selected_workbook = request.query_params.get("script") or None
    user = _current_user(request)
    # Bare "/" is the simple post-login landing: upload your own, or open the Vault.
    if not selected_workbook:
        return templates.TemplateResponse(
            request=request,
            name="landing.html",
            context={"current_user": user, "client_id": CLIENT_ID},
        )
    workbooks = _workbooks()
    selected_name = next((w["name"] for w in workbooks if w["key"] == selected_workbook), selected_workbook)
    return templates.TemplateResponse(
        request=request,
        name="index.html",
        context={
            "groups": _grouped_scenarios(selected_workbook),
            "stats": _stats(selected_workbook),
            "all_stats": _stats(),
            "workbooks": workbooks,
            "selected_workbook": selected_workbook,
            "selected_name": selected_name,
            "active": "all",
            "client_id": CLIENT_ID,
            "current_user": user,
        },
    )


def _run_script(run_id: str) -> str:
    """The script (vault key) a run was launched from, recorded at run_started."""
    for e in _load_audit(run_id):
        if e.get("event") == "run_started":
            return str(e.get("details", {}).get("script", "") or "")
    return ""


@app.get("/scenario/{scenario_id}", response_class=HTMLResponse)
def scenario_detail(request: Request, scenario_id: str):
    selected_script = (request.query_params.get("script") or "").strip()
    scenarios = _load_scenarios(selected_script or None)
    scenario = next((s for s in scenarios if s.scenario_id == scenario_id), None)
    if not scenario:
        return HTMLResponse("Scenario not found", status_code=404)

    import re as _re
    runs = sorted([p for p in RUNS_DIR.iterdir() if p.is_dir()], key=lambda p: p.stat().st_mtime, reverse=True) if RUNS_DIR.exists() else []
    latest_run = None

    def _in_scope(run_name: str) -> bool:
        # A fresh script shows only its OWN runs — it never inherits another
        # script's evidence just because they share a scenario id.
        return (not selected_script) or _run_script(run_name) == selected_script

    # Collect the most recent screenshot per step across ALL runs (for click-to-train).
    # Prefer _fail shots — they show exactly where it broke.
    step_screenshots: dict[str, str] = {}
    for run in runs:
        if not run.is_dir() or not _in_scope(run.name):
            continue
        for shot in sorted(run.glob(f"{scenario_id}-*.png")):
            base = _re.sub(r'_(fail|retry\d*)$', '', shot.stem)
            url = f"/runs/{run.name}/{shot.name}"
            if base not in step_screenshots or "_fail" in shot.stem:
                step_screenshots[base] = url

    for run in runs:
        if not run.is_dir() or not _in_scope(run.name):
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
            "selected_script": selected_script,
            "library_tasks": [
                {"name": n, "description": (e.get("description", "") if isinstance(e, dict) else "")}
                for n, e in _load_library().items()
            ],
            "stats": _stats(),
            "step_statuses": step_statuses,
            "step_feedback": feedback,
            "step_screenshots": step_screenshots,
            "approved": approved,
            "client_id": CLIENT_ID,
            "current_user": _current_user(request),
        },
    )


@app.get("/history", response_class=HTMLResponse)
def run_history(request: Request):
    user = _current_user(request)
    # Users see only their own company's runs; owner/admin see everything.
    scope = None if _role_at_least(user.get("role", "viewer"), "admin") else (user.get("company") or "internal")
    records = _run_records(scope)
    return templates.TemplateResponse(
        request=request,
        name="history.html",
        context={
            "runs": records,
            "stats": _stats(),
            "client_id": CLIENT_ID,
            "current_user": _current_user(request),
            "active": "history",
            "summary": {
                "total": len(records),
                "passed": sum(1 for r in records if r["status"] == "passed"),
                "failed": sum(1 for r in records if r["status"] in ("failed", "error")),
                "with_video": sum(1 for r in records if r["video_url"]),
            },
        },
    )


@app.get("/history/{run_id}", response_class=HTMLResponse)
def run_audit_detail(request: Request, run_id: str):
    run_dir = RUNS_DIR / run_id
    if not run_dir.exists() or not run_dir.is_dir():
        return HTMLResponse("Run not found", status_code=404)
    scenarios = _scenario_lookup()
    record = _run_record(run_dir, scenarios)
    # Company scope: a non-admin can only open runs from their own company.
    _viewer = _current_user(request)
    if not _role_at_least(_viewer.get("role", "viewer"), "admin") and record.get("company") != (_viewer.get("company") or "internal"):
        return HTMLResponse("Run not found", status_code=404)
    audit = _load_audit(run_id)
    step_log = _step_log_for_run(run_id)
    if not audit:
        audit = [{
            "ts": record["started_at"],
            "event": "evidence_imported",
            "title": "Historical run evidence imported",
            "scenario_id": record["scenario_id"],
            "step_id": "",
            "status": record["status"],
            "user": record["user"],
            "details": {"note": "This run happened before audit tracking was enabled.", "screenshots": record["screenshots"], "video_url": record["video_url"]},
        }]
        for step in (step_log.get("steps", []) if isinstance(step_log.get("steps"), list) else []):
            audit.append({
                "ts": record["started_at"],
                "event": "historical_step",
                "title": "Historical step result",
                "scenario_id": record["scenario_id"],
                "step_id": step.get("step_id", ""),
                "status": "pass" if step.get("passed") else "fail",
                "user": record["user"],
                "details": {"error": step.get("error", ""), "screenshot_url": step.get("screenshot_url", "")},
            })
    screenshots = [
        {"name": p.name, "url": f"/runs/{run_id}/{p.name}"}
        for p in sorted(run_dir.glob("*.png"))
    ]
    return templates.TemplateResponse(
        request=request,
        name="audit.html",
        context={
            "run": record,
            "audit": audit,
            "step_log": step_log,
            "screenshots": screenshots,
            "stats": _stats(),
            "client_id": CLIENT_ID,
            "current_user": _current_user(request),
            "active": "history",
        },
    )


@app.get("/api/analyse/{scenario_id}")
def analyse_scenario_route(scenario_id: str, script: str = ""):
    """Return pre-run analysis: data dependencies and questions to ask."""
    from engine.scenario_analyst import analyse_scenario
    scenarios = _load_scenarios(script or None)
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
    # Accept optional pre-run answers (e.g. proxy_name, candidate_name) + supervised flag
    try:
        body = await request.json()
        pre_answers = {k: v for k, v in body.items() if k not in ("supervised", "live", "_lib_vars", "script", "force_library_task", "run_fresh")} if isinstance(body, dict) else {}
        supervised = bool(body.get("supervised", False)) if isinstance(body, dict) else False
        live_mode = bool(body.get("live", False)) if isinstance(body, dict) else False
        script_key = str(body.get("script", "")).strip() if isinstance(body, dict) else ""
        force_library_task = str(body.get("force_library_task", "")).strip() if isinstance(body, dict) else ""
        run_fresh = bool(body.get("run_fresh", False)) if isinstance(body, dict) else False
        # Library template variables (e.g. target_employee_name) — merge into context
        lib_vars = body.get("_lib_vars") if isinstance(body, dict) else None
        if isinstance(lib_vars, dict):
            pre_answers.update({k: v for k, v in lib_vars.items() if isinstance(v, str) and v})
    except Exception:
        pre_answers = {}
        supervised = False
        live_mode = False
        lib_vars = None
        script_key = ""
        force_library_task = ""
        run_fresh = False

    scenarios = _load_scenarios(script_key or None)
    scenario = next((s for s in scenarios if s.scenario_id == scenario_id), None)
    if not scenario:
        raise HTTPException(404, "Scenario not found")

    if _ACTIVE_RUNS.get(scenario_id, {}).get("status") == "running":
        return JSONResponse({"ok": False, "reason": "already running"}, status_code=409)

    user = _current_user(request)
    run_id = datetime.utcnow().strftime("%Y%m%dT%H%M%SZ")
    _ACTIVE_RUNS[scenario_id] = {"status": "running", "run_id": run_id, "supervised": supervised, "live_mode": live_mode, "user": user, "script": script_key}
    _append_audit(
        run_id,
        "run_started",
        "Run started",
        scenario_id=scenario_id,
        user=user,
        status="running",
        details={
            "mode": "step-by-step" if supervised else ("live-control" if live_mode else "automated"),
            "answers": pre_answers,
            "library_variables_used": bool(lib_vars),
            "force_library_task": force_library_task,
            "run_fresh": run_fresh,
            "step_count": len(scenario.steps),
            "script": script_key,
        },
    )

    # Live step log. Written to BOTH a scenario-keyed file (legacy, for page
    # reload) AND an immutable per-run file runs/{run_id}/steps.json so each run's
    # evidence is isolated by run_id and can't be overwritten by another run that
    # happens to share the same scenario_id.
    step_log_file = RUNS_DIR / f"{scenario_id}_last_run.json"
    per_run_log = RUNS_DIR / run_id / "steps.json"

    def _write_step_log(steps_so_far: list, run_status: str):
        payload = json.dumps({
            "run_id": run_id, "scenario_id": scenario_id, "script": script_key,
            "status": run_status, "steps": steps_so_far,
        }, indent=2)
        for target in (step_log_file, per_run_log):
            try:
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(payload)
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
                _append_audit(
                    run_id,
                    "step_completed" if passed else "step_failed",
                    "Step passed" if passed else "Step failed",
                    scenario_id=scenario_id,
                    user=user,
                    step_id=step_id,
                    status="pass" if passed else "fail",
                    details={"error": error or "", "screenshot_url": screenshot_url or ""},
                )

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
                use_memory=not (supervised or live_mode or run_fresh),
                manual=(supervised or live_mode),
                no_guess=not (supervised or live_mode),
                unattended=False,
                run_id_override=run_id,
                forced_library_task=force_library_task,
            )
            _write_step_log(steps_log, "done")
            failed_steps = [s for s in steps_log if not s.get("passed")]
            _append_audit(
                result.run_id,
                "run_completed" if result.passed else "run_failed",
                "Run completed" if result.passed else "Run stopped before completion",
                scenario_id=scenario_id,
                user=user,
                status="passed" if result.passed else "failed",
                details={"steps_logged": len(steps_log), "failed_steps": len(failed_steps)},
            )
            _ACTIVE_RUNS[scenario_id] = {
                "status": "done",
                "run_id": result.run_id,
                "passed": result.passed,
                "user": user,
            }
        except Exception as exc:
            import traceback
            print(f"[RUN ERROR] {scenario_id}: {exc}")
            traceback.print_exc()
            _write_step_log(steps_log, "error")
            _append_audit(
                run_id,
                "run_failed",
                "Run errored",
                scenario_id=scenario_id,
                user=user,
                status="error",
                details={"error": str(exc), "steps_logged": len(steps_log)},
            )
            _ACTIVE_RUNS[scenario_id] = {
                "status": "error",
                "run_id": run_id,
                "error": str(exc),
                "user": user,
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
    active = _ACTIVE_RUNS.get(scenario_id, {})
    _append_audit(
        active.get("run_id", ""),
        "step_confirmed" if confirmed else "step_rejected",
        "Step confirmed" if confirmed else "Step sent back for redo",
        scenario_id=scenario_id,
        user=_current_user(request),
        step_id=active.get("confirming_step", ""),
        status="confirmed" if confirmed else "redo",
    )
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
    active = _ACTIVE_RUNS.get(scenario_id, {})
    _append_audit(
        active.get("run_id", ""),
        "run_cancelled",
        "Run cancelled",
        scenario_id=scenario_id,
        user=active.get("user", {}),
        status="cancelled",
    )
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
    active = _ACTIVE_RUNS.get(scenario_id, {})
    _append_audit(
        active.get("run_id", ""),
        "fix_submitted",
        "Human fix submitted",
        scenario_id=scenario_id,
        user=_current_user(request),
        step_id=active.get("paused_step", ""),
        status="fix",
        details={"comment": comment, "commands": commands, "save_feedback": save_feedback},
    )

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

    active = _ACTIVE_RUNS.get(scenario_id, {})
    _append_audit(
        active.get("run_id", ""),
        "live_control_finished",
        "Live control finished",
        scenario_id=scenario_id,
        user=_current_user(request),
        step_id=active.get("paused_step", ""),
        status="resume",
        details={"commands": commands, "saved_feedback": bool(commands)},
    )
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
    user = _current_user(request)

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
        _ACTIVE_RUNS[scenario_id] = {"status": "running", "run_id": run_id, "user": user, "live_mode": True}
        _append_audit(
            run_id,
            "run_started",
            "Run started for live control",
            scenario_id=scenario_id,
            user=user,
            status="running",
            details={"mode": "take-control", "answers": pre_answers, "step_count": len(scenario.steps)},
        )
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
                    _append_audit(
                        run_id,
                        "step_completed" if passed else "step_failed",
                        "Step passed" if passed else "Step failed",
                        scenario_id=scenario_id,
                        user=user,
                        step_id=step_id,
                        status="pass" if passed else "fail",
                        details={"error": error or "", "screenshot_url": screenshot_url or ""},
                    )

                def _check_pause(sid):
                    return _FORCE_PAUSE.pop(sid, False)

                result = run_scenario(scenario, runs_root=RUNS_DIR, headless=True,
                                      pause_callback=lambda **kw: _pause_callback(**kw),
                                      initial_context=pre_answers,
                                      step_done_callback=_step_done,
                                      check_pause_fn=_check_pause,
                                      run_id_override=run_id)
                try:
                    step_log_file.write_text(json.dumps({"run_id": run_id, "status": "done", "steps": steps_log}, indent=2))
                except Exception:
                    pass
                _append_audit(
                    run_id,
                    "run_completed" if result.passed else "run_failed",
                    "Run completed" if result.passed else "Run stopped before completion",
                    scenario_id=scenario_id,
                    user=user,
                    status="passed" if result.passed else "failed",
                    details={"steps_logged": len(steps_log), "failed_steps": len([s for s in steps_log if not s.get("passed")])},
                )
                _ACTIVE_RUNS[scenario_id] = {"status": "done", "run_id": result.run_id, "passed": result.passed, "user": user}
            except Exception as exc:
                _append_audit(run_id, "run_failed", "Run errored", scenario_id=scenario_id, user=user, status="error", details={"error": str(exc)})
                _ACTIVE_RUNS[scenario_id] = {"status": "error", "run_id": run_id, "error": str(exc), "user": user}

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
        runs = sorted([p for p in RUNS_DIR.iterdir() if p.is_dir()], key=lambda p: p.stat().st_mtime, reverse=True) if RUNS_DIR.exists() else []
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
def approve_scenario(request: Request, scenario_id: str):
    """Lock the current feedback as the golden playbook — used on every future run."""
    feedback = _load_feedback().get(scenario_id, {})
    approved = _load_approved()
    approved[scenario_id] = {
        "approved_at": datetime.utcnow().isoformat(),
        "step_commands": feedback,
    }
    _save_approved(approved)
    _append_audit(
        _latest_run_id_for_scenario(scenario_id),
        "scenario_approved",
        "Scenario approved and locked",
        scenario_id=scenario_id,
        user=_current_user(request),
        status="approved",
        details={"steps_locked": len(feedback)},
    )
    threading.Thread(target=_git_push_approved, daemon=True).start()
    return JSONResponse({"ok": True})


@app.post("/api/scenario/{scenario_id}/approve-library")
async def approve_scenario_and_save_library(request: Request, scenario_id: str):
    """After the user reviews the video, lock the run and save it as a library task."""
    try:
        body = await request.json()
    except Exception:
        body = {}
    task_name = str(body.get("task_name", "")).strip() if isinstance(body, dict) else ""
    note = str(body.get("note", "")).strip() if isinstance(body, dict) else ""
    scenario = next((s for s in _load_scenarios() if s.scenario_id == scenario_id), None)
    if not scenario:
        return JSONResponse({"ok": False, "error": "Scenario not found."}, status_code=404)
    commands = _load_feedback().get(scenario_id, {}) or {}
    if not commands:
        return JSONResponse({"ok": False, "error": "There are no recorded step commands to save yet."}, status_code=400)
    result = _save_reviewed_task_to_library(
        scenario,
        commands,
        _current_user(request),
        _latest_run_id_for_scenario(scenario_id),
        task_name=task_name,
        note=note,
    )
    if not result.get("ok"):
        return JSONResponse(result, status_code=400)
    return JSONResponse(result)


@app.post("/api/scenario/{scenario_id}/unapprove")
def unapprove_scenario(request: Request, scenario_id: str):
    """Remove the golden playbook so the scenario goes back to normal mode."""
    approved = _load_approved()
    approved.pop(scenario_id, None)
    _save_approved(approved)
    _append_audit(
        _latest_run_id_for_scenario(scenario_id),
        "scenario_unapproved",
        "Scenario lock removed",
        scenario_id=scenario_id,
        user=_current_user(request),
        status="unapproved",
    )
    threading.Thread(target=_git_push_approved, daemon=True).start()
    return JSONResponse({"ok": True})


@app.post("/api/step-feedback")
def set_step_feedback(
    request: Request,
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
    _append_audit(
        _latest_run_id_for_scenario(scenario_id),
        "step_feedback_saved",
        "Step training saved" if feedback.strip() else "Step training cleared",
        scenario_id=scenario_id,
        user=_current_user(request),
        step_id=step_id,
        status="saved" if feedback.strip() else "cleared",
        details={"feedback": feedback.strip(), "push": push},
    )
    if push.lower() != "false":
        threading.Thread(target=_git_push_feedback, daemon=True).start()
    return JSONResponse({"ok": True})


@app.post("/api/step-status")
def set_step_status(request: Request, scenario_id: str = Form(...), step_id: str = Form(...), status: str = Form(...)):
    if status not in VALID_STATUSES:
        raise HTTPException(400, f"Invalid status; must be one of {VALID_STATUSES}")
    data = _load_statuses()
    data.setdefault(scenario_id, {})[step_id] = status
    if status == "not_tested":
        data[scenario_id].pop(step_id, None)
        if not data[scenario_id]:
            data.pop(scenario_id)
    _save_statuses(data)
    _append_audit(
        _latest_run_id_for_scenario(scenario_id),
        "manual_step_status",
        "Manual step status changed",
        scenario_id=scenario_id,
        user=_current_user(request),
        step_id=step_id,
        status=status,
    )
    return JSONResponse({"ok": True, "scenario_id": scenario_id, "step_id": step_id, "status": status})


# ── Step Library ──────────────────────────────────────────────────────────────

@app.get("/library", response_class=HTMLResponse)
def library_page(request: Request):
    try:
        st = _stats()
    except Exception:
        st = {"total": 0, "passing": 0, "failing": 0, "blocked": 0}
    return templates.TemplateResponse(
        request=request,
        name="library.html",
        context={
            "stats": st,
            "client_id": CLIENT_ID,
            "current_user": _current_user(request),
            "active": "library",
        },
    )
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
        <div class="nav-label">Scripts</div>
        <a href="/vault" class="nav-item"><span class="nav-item-left"><svg class="nav-icon" width="15" height="15" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="1.8"><path stroke-linecap="round" stroke-linejoin="round" d="M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V7z"/></svg>Vault</span></a>
        <a href="/history" class="nav-item"><span class="nav-item-left">Runs</span></a>
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
        <div style="margin-bottom:24px;display:flex;align-items:flex-start;justify-content:space-between;gap:16px;">
          <div>
            <h1 style="font-size:24px;font-weight:700;letter-spacing:-0.03em;color:#1d1d1f;margin-bottom:4px;">Task Library</h1>
            <p style="font-size:13px;color:#6e6e73;">Saved tasks fire automatically on any module, any client. Claude matches by intent — not exact wording.</p>
          </div>
          <button onclick="clearAllTasks()" class="btn btn-danger" style="flex-shrink:0;">Wipe all</button>
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
    return '<div class="task-card"><div style="display:flex;align-items:flex-start;justify-content:space-between;gap:16px;"><div style="flex:1;min-width:0;"><div style="display:flex;align-items:center;gap:10px;margin-bottom:6px;"><span style="font-size:15px;font-weight:700;color:#1d1d1f;">'+name+'</span><span class="badge badge-blue">'+stepIds.length+' steps</span></div><p style="font-size:13px;color:#6e6e73;margin:0 0 10px;line-height:1.5;">'+(entry.description||'')+'</p>'+stepRows+'</div><button onclick="deleteTask('+JSON.stringify(name).replace(/"/g,'&quot;')+')" class="btn btn-danger" style="font-size:11px;padding:4px 12px;flex-shrink:0;">Delete</button></div></div>';
  }}).join('');
}})();
async function deleteTask(name) {{
  if (!confirm('Delete task "'+name+'" from the library?')) return;
  const res = await fetch('/api/library/'+encodeURIComponent(name), {{method:'DELETE'}});
  if (res.ok) window.location.reload();
  else alert('Delete failed');
}}
async function clearAllTasks() {{
  if (!confirm('Wipe ALL saved tasks from the library? This cannot be undone.')) return;
  const res = await fetch('/api/library/clear', {{method:'POST'}});
  if (res.ok) window.location.reload();
  else alert('Wipe failed');
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
            return JSONResponse({"match": task_name, "description": entry.get("description", ""), "variables": _library_entry_variables(entry)})

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
            return JSONResponse({"match": result, "description": library[result].get("description", ""), "variables": _library_entry_variables(library[result])})
    except Exception as exc:
        print(f"[library match] error: {exc}")
    return JSONResponse({"match": None})


@app.post("/api/library/add")
def add_to_library(
    task_name: str = Form(...),
    task_description: str = Form(...),
    scenario_id: str = Form(...),
):
    # Prefer the locked/approved command sequence (best for replay), then learned
    # feedback, then the scenario's own step actions. We never hard-fail: the task
    # MUST land in the library so Claude can recognise it on future uploads —
    # recognition uses name/description/steps; replay sharpens once a run is locked.
    approved = _load_approved().get(scenario_id, {})
    steps = approved.get("step_commands", {}) or _load_feedback().get(scenario_id, {})
    learned = bool(steps)
    scenario = next((s for s in _load_scenarios() if s.scenario_id == scenario_id), None)
    if not steps and scenario:
        steps = {st.step_id: (st.action or "") for st in scenario.steps}
    # Save the human-readable step actions too, so future matching can align a new
    # scenario's steps to this task by MEANING (covered vs gap), not just by name.
    step_actions = [st.action for st in scenario.steps] if scenario else []
    step_descriptions = _step_descriptions_for_scenario(scenario) if scenario else []
    detailed = _ai_task_description(scenario, task_description) if scenario else task_description
    data = _load_library()
    data[task_name] = {
        "description": detailed,
        "note": task_description,
        "scenario_id": scenario_id,
        "steps": steps or {},
        "step_actions": step_actions,
        "step_descriptions": step_descriptions,
        "has_learned_commands": learned,
    }
    _save_library(data)
    threading.Thread(target=_git_push_library, daemon=True).start()
    return JSONResponse({"ok": True, "learned": learned})


@app.delete("/api/library/{task_name}")
def delete_library_task(task_name: str):
    data = _load_library()
    data.pop(task_name, None)
    _save_library(data)
    threading.Thread(target=_git_push_library, daemon=True).start()
    return JSONResponse({"ok": True})


@app.post("/api/library/clear")
def clear_library():
    _save_library({})
    threading.Thread(target=_git_push_library, daemon=True).start()
    return JSONResponse({"ok": True})
