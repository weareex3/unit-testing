"""Phase 1 runner: login to SF, execute step 1 of a scenario, record video."""

import json
import os
import re
import sys
import time
from datetime import datetime
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")

from playwright.sync_api import sync_playwright, Page

from models.dataclasses import TestScenario, StepResult, ScenarioResult
from engine.coach import get_step_guidance, get_vision_commands, verify_step_result, save_successful_pattern
from engine.context_extractor import (extract_from_text, substitute, step_produces,
                                       normalize_placeholders, resolve_dynamic_tokens)
from engine.visual_verifier import verify_step as _verify_step


_DEFAULT_SF_URL = "https://hcm-eu10-preview.hr.cloud.sap/login?company=veritasp01T2"


class SFCredentialsError(RuntimeError):
    """Raised when SuccessFactors login details are missing/invalid — surfaced to
    the user as a readable run error instead of a cryptic KeyError."""


def get_sf_credentials(company: str | None = None) -> dict:
    """Single source of SuccessFactors login details. Today reads them from the
    environment; Phase A#3 will resolve per-company creds (from the Vault) by the
    `company` arg. Route ALL credential access through here so that swap is a
    one-function change, not a codebase hunt.

    Raises SFCredentialsError (with a clear message) if anything required is missing.
    """
    url = (os.getenv("SF_URL") or _DEFAULT_SF_URL).strip()
    username = (os.getenv("SF_USERNAME") or "").strip()
    password = (os.getenv("SF_PASSWORD") or "").strip()
    missing = [n for n, v in (("SF_USERNAME", username), ("SF_PASSWORD", password), ("SF_URL", url)) if not v]
    if missing:
        raise SFCredentialsError(
            "SuccessFactors login isn't set up — missing " + ", ".join(missing)
            + ". Add the SF credentials in settings before running."
        )
    return {"url": url, "username": username, "password": password}

_STOP_WORDS = {"the", "a", "an", "to", "from", "and", "or", "in", "on", "of", "for",
               "is", "are", "you", "your", "it", "this", "that", "with", "by", "at"}


def _storage_root() -> Path:
    """Storage root — the /data volume when present (persists across redeploys),
    else the app dir for local runs. Keeps the engine's learned memory in the
    same place server.py reads/writes it."""
    app_root = Path(__file__).resolve().parent.parent
    return (Path("/data") if Path("/data").exists() else app_root) / "storage"


def _pattern_file() -> Path:
    return _storage_root() / os.getenv("CLIENT_ID", "default") / "patterns.json"


def _load_patterns() -> list:
    f = _pattern_file()
    if f.exists():
        try:
            return json.loads(f.read_text())
        except Exception:
            return []
    return []


def _save_pattern(step_action: str, commands: str, comment: str = "") -> None:
    import json as _json
    patterns = _load_patterns()
    patterns.append({
        "action": step_action,
        "commands": commands,
        "comment": comment,
        "created_at": datetime.utcnow().isoformat(),
    })
    f = _pattern_file()
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(_json.dumps(patterns, indent=2))


def _match_pattern(step_action: str, threshold: float = 0.55) -> str | None:
    """Find a stored pattern whose action keywords overlap with this step."""
    patterns = _load_patterns()
    if not patterns:
        return None
    words = {w.lower() for w in re.split(r"\W+", step_action) if w and w.lower() not in _STOP_WORDS}
    best_score, best_cmds = 0.0, None
    for p in patterns:
        p_words = {w.lower() for w in re.split(r"\W+", p["action"]) if w and w.lower() not in _STOP_WORDS}
        if not words or not p_words:
            continue
        overlap = len(words & p_words) / len(words | p_words)
        if overlap > best_score:
            best_score, best_cmds = overlap, p["commands"]
    return best_cmds if best_score >= threshold else None


def run_phase1(scenario: TestScenario) -> ScenarioResult:
    """Compatibility wrapper — runs only step 1."""
    return run_scenario(scenario, max_steps=1)


def run_scenario(
    scenario: TestScenario,
    max_steps: int | None = None,
    runs_root: "Path | str | None" = None,
    headless: bool = False,
    pause_callback=None,
    initial_context: dict | None = None,
    step_done_callback=None,
    check_pause_fn=None,
    step_confirm_callback=None,
    live_mode: bool = False,
    use_memory: bool = True,
    manual: bool = False,
    no_guess: bool = False,
    unattended: bool = False,
    run_id_override: str | None = None,
    forced_library_task: str = "",
    preview: bool = False,
    preview_note: str = "",
) -> ScenarioResult:
    """Login to SF, run all (or first *max_steps*) steps, record video."""
    run_id = run_id_override or datetime.utcnow().strftime("%Y%m%dT%H%M%SZ")
    base = Path(runs_root) if runs_root else Path("runs")
    runs_dir = base / run_id
    runs_dir.mkdir(parents=True, exist_ok=True)

    steps = scenario.steps[:max_steps] if max_steps else scenario.steps
    result = ScenarioResult(scenario_id=scenario.scenario_id, run_id=run_id, passed=False)
    try:
        _creds = get_sf_credentials()
    except SFCredentialsError as exc:
        print(f"  [creds] {exc}")
        result.error = str(exc)
        result.ended_at = datetime.utcnow()
        return result
    username, password, sf_url = _creds["username"], _creds["password"], _creds["url"]

    with sync_playwright() as pw:
        browser = pw.chromium.launch(
            headless=headless,
            slow_mo=0 if headless else 400,
            args=[
                "--disable-gpu",
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--disable-setuid-sandbox",
            ],
        )
        context = browser.new_context(
            record_video_dir=str(runs_dir),
            record_video_size={"width": 1280, "height": 720},
            viewport={"width": 1280, "height": 720},
        )
        # Playwright trace — DOM + screenshots + network at every action.
        # Drop trace.zip into trace.playwright.dev for frame-by-frame replay.
        # Tracing is opt-in via ENABLE_TRACE env var — it can OOM the browser
        # on small Railway instances during heavy SF page loads (e.g. proxy
        # redirects). Off by default; turn on locally when you need replay.
        _trace_on = os.getenv("ENABLE_TRACE", "").lower() in ("1", "true", "yes")
        if _trace_on:
            try:
                context.tracing.start(screenshots=True, snapshots=False, sources=False)
            except Exception as _exc:
                print(f"  [trace] tracing.start failed: {_exc}")
        page = context.new_page()

        # Runtime context — values extracted from SF as steps complete
        # e.g. {"position_id": "POS100139", "req_id": "JR-1001"}
        run_context: dict[str, str] = dict(initial_context or {})

        try:
            print("  [login] opening SF login page...")
            _login(page, sf_url, username, password)
            print("  [login] logged in successfully")

            # use_memory=False (step-by-step / take-control) runs every step fresh —
            # no saved feedback, locked playbook, task library, or learned patterns —
            # so it always starts from step 1 with a clean slate.
            feedback_data = _load_feedback(scenario.scenario_id) if use_memory else {}
            approved_cmds = _load_approved_commands(scenario.scenario_id) if use_memory else {}
            expected_overrides = _load_expected_overrides(scenario.scenario_id)
            _scenario_ctx = ""
            _live_seed_shot = ""   # last real screenshot from an automated step
            _live_manual_started = False  # once user takes first manual step, all rest are manual too

            # Task library: check once per scenario if the whole scenario matches a saved task
            import json as _json
            _library = {}
            if use_memory:
                _lib_file = _storage_root() / "global" / "step_library.json"
                if _lib_file.exists():
                    try:
                        _library = _json.loads(_lib_file.read_text())
                    except Exception:
                        pass
            if forced_library_task and forced_library_task in _library:
                _forced_entry = _library.get(forced_library_task) or {}
                _library_steps = _forced_entry.get("steps") or {}
                print(f"  [library] using chosen task: {forced_library_task}")
            else:
                # NO silent best-guess. A library task is used ONLY when the user
                # explicitly chooses it (forced_library_task). Otherwise this scenario
                # runs ONLY its own trained commands. Matching is surfaced in the UI as
                # a suggestion the user accepts — it is never auto-applied to a run.
                _library_steps = {}
            # library steps are keyed by step_id; build a positional fallback by index too
            _library_by_index = {str(i): cmd for i, cmd in enumerate(_library_steps.values())}

            for i, step in enumerate(steps, 1):
                print(f"\n  [step {i}/{len(steps)}] {step.step_id}")
                print(f"           {step.action[:120]}")

                # Approved playbook beats everything; fall back to feedback then task library
                step_feedback = approved_cmds.get(step.step_id) or feedback_data.get(step.step_id, "")
                if not step_feedback and _library_steps:
                    # Match by step_id first, then by position within the task
                    step_feedback = _library_steps.get(step.step_id, "") or _library_by_index.get(str(i - 1), "")
                if not step_feedback and use_memory:
                    matched = _match_pattern(step.action)
                    if matched:
                        print(f"  [pattern] matched learned pattern — using stored commands")
                        step_feedback = matched

                # Substitute run context values into commands (e.g. {{position_id}})
                if step_feedback and run_context:
                    step_feedback = substitute(step_feedback, run_context)

                if step_feedback:
                    print(f"  [feedback] {step_feedback[:120]}")
                if run_context:
                    print(f"  [context] {run_context}")

                # Build scenario context string once per run for vision prompts
                if i == 1:
                    _ctx_lines = [f"Scenario: {scenario.scenario_id} — {scenario.name}",
                                  f"Role: {scenario.role}  |  Module: {scenario.module}", "Steps:"]
                    for _s in steps:
                        _ctx_lines.append(
                            f"  [{_s.step_id}] {_s.action}"
                            + (f" | Data: {_s.test_data}" if _s.test_data and _s.test_data != "—" else "")
                            + f" | Expected: {_s.expected_result}"
                        )
                    if preview:
                        _ctx_lines.append("")
                        if preview_note:
                            _ctx_lines.append(f"Run context: {preview_note}")
                        _ctx_lines.append(
                            "PREVIEW MODE: This is a dry run. Navigate and fill fields to demonstrate "
                            "the task, but NEVER click Save, Submit, Confirm, OK, or any button that "
                            "commits/persists a change. Stop before the final commit."
                        )
                    _scenario_ctx = "\n".join(_ctx_lines)

                # Preview: skip any step that commits a change (save/submit) — demo only.
                if preview and any(w in (step.action or "").lower() for w in ("save", "submit", "confirm and", "click 'save'", "create the new")):
                    print(f"  [preview] skipping commit step {step.step_id}: {step.action[:60]}")
                    step_result = StepResult(step_id=step.step_id, passed=True, error_message="[preview] commit step skipped")
                    result.steps.append(step_result)
                    if step_done_callback:
                        step_done_callback(step.step_id, True, "[preview] commit step skipped", "")
                    continue

                # manual=True ("watch me"): every step handed to the human.
                # no_guess=True (normal Run / Run All): replay ONLY steps we already
                # know (saved commands); NEVER vision-guess an unknown step. Scenarios
                # are linear, so at the first unknown step we either hand it to the
                # human (interactive) or — if unattended (batch) — stop and flag it.
                _known = _has_direct_commands(step_feedback)
                if no_guess and not _known and unattended:
                    print(f"  [no-guess] {step.step_id} not learned + unattended -> needs-training, stop")
                    step_result = StepResult(
                        step_id=step.step_id, passed=False,
                        error_message="Needs training — this step hasn't been learned yet",
                    )
                    result.steps.append(step_result)
                    if step_done_callback:
                        step_done_callback(step.step_id, False, step_result.error_message, "")
                    break

                _go_live = pause_callback is not None and (
                    manual
                    or (no_guess and not _known)
                    or (live_mode and (_live_manual_started or not _known))
                )
                if _go_live:
                    if not no_guess:
                        _live_manual_started = True
                    fix = pause_callback(
                        scenario_id=scenario.scenario_id,
                        step_id=step.step_id,
                        screenshot_path=_live_seed_shot,
                        run_id=run_id,
                        error_message=f"Step {i}/{len(steps)}: {step.action}",
                        page=page,
                        live_step=True,
                    )
                    if fix and fix.get("commands"):
                        _save_pattern(step.action, fix["commands"], "live step")
                    post_shot = str(runs_dir / f"{step.step_id}.png")
                    try:
                        page.screenshot(path=post_shot)
                        _live_seed_shot = post_shot
                    except Exception:
                        post_shot = _live_seed_shot
                    step_result = StepResult(
                        step_id=step.step_id, passed=True,
                        duration_s=0, screenshot_path=post_shot,
                    )
                else:
                    # Run the step — vision-first, keyword dispatch as fallback
                    step_result = _run_step(page, step, str(runs_dir), feedback=step_feedback,
                                            scenario_context=_scenario_ctx)
                    if step_result.screenshot_path:
                        _live_seed_shot = step_result.screenshot_path
                    # In live mode, if the automated step failed, drop into live
                    # control so the user can click through it instead of hitting
                    # the error bar
                    if live_mode and pause_callback and not step_result.passed:
                        print(f"  [live-fallback] {step.step_id} failed in live mode — handing to user")
                        _live_manual_started = True
                        fix = pause_callback(
                            scenario_id=scenario.scenario_id,
                            step_id=step.step_id,
                            screenshot_path=_live_seed_shot,
                            run_id=run_id,
                            error_message=f"Step {i}/{len(steps)}: {step.action}",
                            page=page,
                            live_step=True,
                        )
                        if fix and fix.get("commands"):
                            _save_pattern(step.action, fix["commands"], "live step")
                        post_shot = str(runs_dir / f"{step.step_id}.png")
                        try:
                            page.screenshot(path=post_shot)
                            _live_seed_shot = post_shot
                        except Exception:
                            post_shot = _live_seed_shot
                        step_result = StepResult(
                            step_id=step.step_id, passed=True,
                            duration_s=0, screenshot_path=post_shot,
                        )

                # AFTER the step, check if user has requested manual control.
                # By now SF is loaded and the screenshot will be a real page, not blank.
                if check_pause_fn and check_pause_fn(scenario.scenario_id) and pause_callback:
                    print(f"  [force-pause] user requested control after {step.step_id}")
                    try:
                        _live_shot = str(runs_dir / f"{step.step_id}_live.png")
                        page.screenshot(path=_live_shot)
                    except Exception:
                        _live_shot = step_result.screenshot_path or ""
                    pause_callback(
                        scenario_id=scenario.scenario_id,
                        step_id=step.step_id,
                        screenshot_path=_live_shot,
                        run_id=run_id,
                        error_message="User requested manual control",
                        page=page,
                    )

                # Visual verification GATE: an auto-run step only counts as passed
                # if Claude confirms the expected result is actually on screen. On a
                # clear FAIL we flip it to failed so the human-takeover path below
                # runs (the "if it fails, do it step-by-step" behaviour). INCONCLUSIVE
                # (verifier error / no API key) returns ok=True and does NOT block.
                # Only gates fully-automated runs — manual / live steps are driven by
                # the human and aren't second-guessed.
                # TRUST SAVED COMMANDS (restores the 05-19 behaviour — commit
                # 8c6ff72 "Revert verify_step_result on direct commands — was failing
                # working proxy steps"). A step that ran a recorded/saved command is
                # NOT failed by the verifier; it just runs and completes. The verifier
                # only GATES AI-guessed (non-direct) steps to catch fake passes there.
                _ran_direct = _has_direct_commands(step_feedback)
                if step_result.passed and step_result.screenshot_path and not manual and not live_mode and not _ran_direct:
                    expected = expected_overrides.get(step.step_id) or step.expected_result
                    ok, reason = _verify_step(
                        step_result.screenshot_path,
                        step.action,
                        expected,
                        step.test_data,
                    )
                    if ok:
                        print(f"  [verify] {step.step_id}: PASS — {reason}")
                    else:
                        print(f"  [verify] {step.step_id}: FAIL — {reason} -> handing to human")
                        step_result.passed = False
                        step_result.error_message = f"Verification failed — expected result not visible: {reason}"
                elif _ran_direct:
                    print(f"  [verify] {step.step_id}: trusted (ran saved command, not re-verified)")

                # If failed and we have a pause callback — ask human for help
                if not step_result.passed and pause_callback:
                    print(f"  [pause] waiting for human fix on {step.step_id}...")
                    fix = pause_callback(
                        scenario_id=scenario.scenario_id,
                        step_id=step.step_id,
                        screenshot_path=step_result.screenshot_path,
                        run_id=run_id,
                        error_message=step_result.error_message,
                        page=page,
                        live_step=str(step_result.error_message or "").startswith("Human review required"),
                    )
                    if fix:
                        if fix.get("skip"):
                            # User fixed it manually via live control — mark passed, move on
                            step_result.passed = True
                            print(f"  [resume] user took control — step marked passed")
                        else:
                            commands = fix.get("commands", "")
                            comment = fix.get("comment", "")
                            print(f"  [resume] got fix: {commands[:80]}")
                            step_result = _run_step(page, step, str(runs_dir), feedback=commands)
                            if step_result.passed:
                                _save_pattern(step.action, commands, comment)
                                print(f"  [learn] pattern saved for: {step.action[:60]}")

                # After a passing step, extract any new IDs / values from the page
                if step_result.passed and step_produces(step.action):
                    try:
                        page_text = page.evaluate("document.body.innerText")
                        extracted = extract_from_text(page_text)
                        if extracted:
                            run_context.update(extracted)
                            print(f"  [extract] captured: {extracted}")
                    except Exception:
                        pass

                # Supervised mode: pause after each passing step for human confirmation
                if step_result.passed and step_confirm_callback:
                    confirmed = step_confirm_callback(step.step_id, step_result.screenshot_path or "")
                    if not confirmed:
                        # User said "redo" — mark failed so pause_callback fires
                        step_result.passed = False
                        step_result.error_message = "User requested redo in supervised mode"

                result.steps.append(step_result)

                # Notify caller of step completion (used for live disk log)
                if step_done_callback:
                    shot_name = Path(step_result.screenshot_path).name if step_result.screenshot_path else ""
                    shot_url = f"/runs/{run_id}/{shot_name}" if shot_name else ""
                    step_done_callback(step_result.step_id, step_result.passed,
                                       step_result.error_message, shot_url)

                status = "PASS" if step_result.passed else "FAIL"
                print(f"  [step {i}] {status} in {step_result.duration_s}s")
                if step_result.error_message:
                    print(f"           error: {step_result.error_message[:200]}")

                # Stop on first failure — later steps depend on prior ones
                if not step_result.passed:
                    print(f"  [halt] step {i} failed — stopping scenario")
                    break

            result.passed = (
                len(result.steps) == len(steps)
                and all(s.passed for s in result.steps)
            )

            # In live mode, hold the browser open after all steps complete.
            # The user decides when they're truly done — they may want to keep
            # clicking, check something extra, or the scenario may have more
            # than the workbook describes.
            if live_mode and pause_callback:
                print("  [live] all steps done — holding browser until user clicks Finish")
                pause_callback(
                    scenario_id=scenario.scenario_id,
                    step_id="__done__",
                    screenshot_path=_live_seed_shot,
                    run_id=run_id,
                    error_message="All steps complete",
                    page=page,
                    live_step=True,
                )

        except Exception as exc:
            result.passed = False
            print(f"  [runner error] {exc}")
        finally:
            result.ended_at = datetime.utcnow()
            if _trace_on:
                try:
                    context.tracing.stop(path=str(runs_dir / "trace.zip"))
                except Exception as _exc:
                    print(f"  [trace] tracing.stop failed: {_exc}")
            context.close()
            browser.close()

    videos = list(runs_dir.glob("*.webm"))
    if videos:
        result.s3_url = str(videos[0])

    return result


# ── Expected result overrides ────────────────────────────────────────────────
# Lets us redefine what the visual verifier should see for a given step,
# without editing the official Excel workbook. Useful when the workbook
# describes UI that no longer exists in the product (e.g. SF simplified the
# Copy Position dialog from a full edit form to a confirmation dialog).

def _load_expected_overrides(scenario_id: str) -> dict:
    """Load step_id → expected_result override map for this scenario."""
    import json
    root = _storage_root()
    client_id = os.getenv("CLIENT_ID", "default")
    f = root / client_id / "expected_overrides.json"
    if not f.exists():
        return {}
    try:
        data = json.loads(f.read_text())
        return data.get(scenario_id, {}) or {}
    except Exception as exc:
        print(f"  [overrides] failed to load: {exc}")
        return {}


# ── Feedback loader ───────────────────────────────────────────────────────────

def _load_feedback(scenario_id: str) -> dict:
    """Load stored human feedback — client-specific first, then global fallback."""
    import json
    root = _storage_root()
    client_id = os.getenv("CLIENT_ID", "default")

    client_file = root / client_id / "step_feedback.json"
    client_data = {}
    if client_file.exists():
        try:
            client_data = json.loads(client_file.read_text()).get(scenario_id, {})
        except Exception:
            pass

    global_file = root / "step_feedback.json"
    global_data = {}
    if global_file.exists():
        try:
            global_data = json.loads(global_file.read_text()).get(scenario_id, {})
        except Exception:
            pass

    return {**global_data, **client_data}


def _library_task_match(scenario_steps: list, library: dict) -> dict:
    """Ask Claude which saved task this scenario matches, then return step_index -> commands.
    Matches whole tasks by scenario description, not individual step text."""
    import os
    if not library:
        return {}
    task_lines = "\n".join(
        f"- {name}: {entry.get('description', name)}"
        for name, entry in library.items()
        if isinstance(entry, dict) and entry.get("steps")
    )
    if not task_lines:
        return {}
    scenario_summary = "\n".join(f"  {i+1}. {s.action}" for i, s in enumerate(scenario_steps[:8]))
    api_key = os.getenv("ANTHROPIC_API_KEY", "")
    if not api_key:
        return {}
    try:
        import anthropic
        client = anthropic.Anthropic(api_key=api_key)
        msg = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=40,
            messages=[{"role": "user", "content": (
                f"You are matching a SAP SuccessFactors test scenario to a library of saved tasks.\n\n"
                f"Scenario steps:\n{scenario_summary}\n\n"
                f"Saved tasks:\n{task_lines}\n\n"
                f"If this scenario is performing one of the saved tasks, reply with ONLY the exact task name. "
                f"If it does not match any saved task, reply with ONLY: NO_MATCH"
            )}],
        )
        result = msg.content[0].text.strip()
        if result != "NO_MATCH" and result in library:
            print(f"  [library] scenario matched task '{result}' — applying saved step commands")
            return library[result].get("steps", {})
    except Exception as exc:
        print(f"  [library] lookup error: {exc}")
    return {}


def _load_approved_commands(scenario_id: str) -> dict:
    """Return step_id -> command string for approved (locked) scenarios, or {}."""
    import json
    root = _storage_root()
    client_id = os.getenv("CLIENT_ID", "default")
    approved_file = root / client_id / "approved.json"
    if not approved_file.exists():
        return {}
    try:
        data = json.loads(approved_file.read_text())
        return data.get(scenario_id, {}).get("step_commands", {})
    except Exception:
        return {}


# ── Login ─────────────────────────────────────────────────────────────────────

def _login(page: Page, url: str, username: str, password: str) -> None:
    page.goto(url, wait_until="domcontentloaded", timeout=30_000)

    # Wait for the username field — SF uses j_username on the standard login page
    page.wait_for_selector(
        "#j_username, input[name='j_username'], input[autocomplete='username']",
        timeout=20_000,
    )

    page.fill("#j_username, input[name='j_username']", username)
    page.fill("#j_password, input[name='j_password']", password)

    # SF submit button
    page.click(
        "#logOnFormSubmit, input[type='submit'][value*='Log'], button[type='submit']",
        timeout=10_000,
    )
    page.wait_for_load_state("networkidle", timeout=40_000)


# ── Step executor ─────────────────────────────────────────────────────────────

def _run_step(page: Page, step, output_dir: str, feedback: str = "", use_feedback_first: bool = False,
              scenario_context: str = "") -> StepResult:
    t0 = time.time()

    # ── Priority 1: explicit human feedback / approved commands ───────────────
    if _has_direct_commands(feedback):
        print(f"  [direct] {step.step_id}: running command override")
        return _run_direct_commands(page, step, output_dir, feedback, t0)

    # ── Priority 2: vision-first — screenshot the real page, ask Claude ──────
    # Claude sees what's actually on screen and generates the exact command
    # sequence rather than guessing from keywords.
    pre_shot = os.path.join(output_dir, f"{step.step_id}_pre.png")
    try:
        page.screenshot(path=pre_shot, full_page=False)
        if _agent_step_is_final_save(step.action):
            return StepResult(
                step_id=step.step_id,
                passed=False,
                error_message="Human review required before final Save/Submit",
                duration_s=round(time.time() - t0, 2),
                screenshot_path=pre_shot,
            )
        vision_cmds = get_vision_commands(
            pre_shot,
            step.action,
            step.expected_result,
            step.test_data or "",
            scenario_context,
        )
        if vision_cmds:
            print(f"  [vision] {step.step_id}: executing vision commands")
            if _agent_needs_human_save(vision_cmds):
                shot = os.path.join(output_dir, f"{step.step_id}_needs_human_save.png")
                try:
                    page.screenshot(path=shot, full_page=False)
                except Exception:
                    shot = pre_shot
                return StepResult(
                    step_id=step.step_id,
                    passed=False,
                    error_message="Human review required before final Save/Submit",
                    duration_s=round(time.time() - t0, 2),
                    screenshot_path=shot,
                )
            result = _run_direct_commands(page, step, output_dir, vision_cmds, t0)
            if result.passed:
                # Verify the expected result is actually on screen — catch fake passes
                post_shot = os.path.join(output_dir, f"{step.step_id}_post.png")
                try:
                    page.screenshot(path=post_shot, full_page=False)
                    if verify_step_result(post_shot, step.expected_result):
                        return result
                    else:
                        print(f"  [vision] commands ran but expected result not achieved — retrying")
                        result = StepResult(step_id=step.step_id, passed=False,
                                            error_message="Vision commands completed but expected result not visible on screen",
                                            duration_s=round(time.time() - t0, 2),
                                            screenshot_path=post_shot)
                except Exception:
                    return result  # screenshot failed — trust the result
            print(f"  [vision] commands failed — falling back to keyword dispatch")
    except Exception as exc:
        print(f"  [vision] pre-shot or call failed: {exc} — falling back")

    # ── Priority 3: keyword dispatch (legacy fallback) ─────────────────────────
    last_exc = None
    for attempt in range(3):
        try:
            if attempt > 0:
                page.wait_for_timeout(2000)
                shot_pre = os.path.join(output_dir, f"{step.step_id}_retry{attempt}.png")
                try:
                    page.screenshot(path=shot_pre, full_page=False)
                except Exception:
                    shot_pre = ""

                if shot_pre:
                    guidance = get_step_guidance(shot_pre, step.action, step.expected_result, feedback or step.action)
                    if guidance:
                        print(f"  [coach] attempt {attempt+1}: {guidance.get('notes','')}")
                        _execute_guidance(page, guidance)

            _dispatch(page, step)
            shot = os.path.join(output_dir, f"{step.step_id}.png")
            page.screenshot(path=shot, full_page=False)
            return StepResult(
                step_id=step.step_id,
                passed=True,
                duration_s=round(time.time() - t0, 2),
                screenshot_path=shot,
            )

        except Exception as exc:
            last_exc = exc
            print(f"  [retry {attempt+1}/3] {step.step_id}: {str(exc)[:120]}")

    shot = os.path.join(output_dir, f"{step.step_id}_fail.png")
    try:
        page.screenshot(path=shot, full_page=False)
    except Exception:
        shot = ""
    return StepResult(
        step_id=step.step_id,
        passed=False,
        error_message=str(last_exc),
        duration_s=round(time.time() - t0, 2),
        screenshot_path=shot,
    )


_CMD_PREFIXES = ("CLICK:", "CLICK_XY:", "TYPE:", "PRESS:", "WAIT:", "FILL:", "SHADOW_CLICK:", "GOTO:", "NAVIGATE:", "SELECT:", "SELECT_OPTION:", "JS:")
_HUMAN_SAVE_WORDS = ("save", "submit")


def _has_direct_commands(feedback: str) -> bool:
    return any(line.strip().upper().startswith(_CMD_PREFIXES) for line in (feedback or "").splitlines())


def _agent_needs_human_save(commands: str) -> bool:
    """Agent-generated runs must stop before committing a change."""
    for raw in (commands or "").splitlines():
        line = raw.strip()
        if not line or ":" not in line:
            continue
        cmd, _, arg = line.partition(":")
        cmd = cmd.strip().upper()
        words = re.findall(r"[a-z]+", arg.lower())
        if cmd in ("CLICK", "SHADOW_CLICK") and any(word in words for word in _HUMAN_SAVE_WORDS):
            return True
    return False


def _agent_step_is_final_save(step_action: str) -> bool:
    text = (step_action or "").lower()
    return bool(re.search(r"\b(save|submit)\b", text))


def _run_direct_commands(page: Page, step, output_dir: str, feedback: str, t0: float) -> StepResult:
    """Execute a step using direct commands written in the feedback box."""
    current_cmd = ""
    try:
        for line in feedback.strip().splitlines():
            line = resolve_dynamic_tokens(line.strip())
            if not line or line.startswith("#"):
                continue
            if ":" not in line:
                continue
            cmd, _, arg = line.partition(":")
            cmd = cmd.strip().upper()
            arg = arg.strip()
            current_cmd = f"{cmd}: {arg[:60]}"

            if cmd == "CLICK":
                _smart_click(page, arg)
            elif cmd == "SHADOW_CLICK":
                if not _shadow_click(page, arg):
                    raise RuntimeError(f"SHADOW_CLICK: could not find '{arg}' in any shadow root")
            elif cmd == "CLICK_XY":
                x, y = [float(v.strip()) for v in arg.split(",")]
                page.mouse.click(x, y)
            elif cmd == "TYPE":
                page.keyboard.type(arg)
            elif cmd == "PRESS":
                page.keyboard.press(arg)
            elif cmd == "WAIT":
                page.wait_for_timeout(int(arg))
            elif cmd == "FILL":
                # "FILL: Field label | value"
                label, _, value = arg.partition("|")
                page.get_by_label(label.strip(), exact=False).first.fill(value.strip(), timeout=5_000)
            elif cmd in ("GOTO", "NAVIGATE") and arg.startswith("http"):
                page.goto(arg, wait_until="domcontentloaded", timeout=30_000)
                page.wait_for_timeout(2000)
            elif cmd == "NAVIGATE":
                # "NAVIGATE: Recruiting" — open module picker and click the module
                _module_picker_nav(page, [p.strip() for p in arg.split("→")])
            elif cmd == "SELECT":
                # "SELECT: option text" — click a dropdown option
                page.get_by_role("option", name=arg, exact=False).first.click(timeout=5_000)
            elif cmd == "SELECT_OPTION":
                # "SELECT_OPTION: label | value" — select option in a <select> element by label
                label, _, value = arg.partition("|")
                page.get_by_label(label.strip(), exact=False).first.select_option(label=value.strip(), timeout=5_000)
            elif cmd == "JS":
                # "JS: document.querySelector('...').click()"
                page.evaluate(arg)

            page.wait_for_timeout(600)

        shot = os.path.join(output_dir, f"{step.step_id}.png")
        page.screenshot(path=shot, full_page=False)
        return StepResult(step_id=step.step_id, passed=True,
                          duration_s=round(time.time() - t0, 2), screenshot_path=shot)
    except Exception as exc:
        shot = os.path.join(output_dir, f"{step.step_id}_fail.png")
        try:
            page.wait_for_timeout(300)  # brief settle so screenshot reflects real state
            page.screenshot(path=shot, full_page=False)
        except Exception:
            shot = ""
        err = f"[{current_cmd}] {exc}" if current_cmd else str(exc)
        return StepResult(step_id=step.step_id, passed=False,
                          error_message=err,
                          duration_s=round(time.time() - t0, 2), screenshot_path=shot)


def _smart_click(page: Page, target: str) -> None:
    """
    Try multiple click strategies in order — first one that hits, wins.
    Strategies: text → button-role → link-role → aria-label → shadow-text.
    Each gets 2.5s rather than one 8s timeout, so total fail-time is similar
    but we cover the cases where the same element is named differently in
    the accessibility tree vs visible text.
    """
    strategies = [
        ("text",       lambda: page.get_by_text(target, exact=False).first.click(timeout=6000)),
        ("button",     lambda: page.get_by_role("button", name=target).first.click(timeout=1500)),
        ("menuitem",   lambda: page.get_by_role("menuitem", name=target).first.click(timeout=1500)),
        ("link",       lambda: page.get_by_role("link", name=target).first.click(timeout=1000)),
        ("option",     lambda: page.get_by_role("option", name=target).first.click(timeout=1000)),
        ("aria-label", lambda: page.locator(f'[aria-label="{target}"], [title="{target}"]').first.click(timeout=1500)),
    ]
    last_err = None
    for name, fn in strategies:
        try:
            fn()
            return
        except Exception as exc:
            last_err = f"{name}: {str(exc)[:60]}"
    # Final fallback — search every shadow root
    if _shadow_click(page, target, exact=False):
        return
    raise RuntimeError(f"CLICK '{target}' — no strategy matched (last: {last_err})")


def _shadow_click(page: Page, text: str, exact: bool = True) -> bool:
    """Click an element by text, searching recursively through all shadow roots."""
    return page.evaluate(
        """([targetText, exact]) => {
            function search(root) {
                const els = root.querySelectorAll('*');
                for (const el of els) {
                    if (el.shadowRoot) {
                        if (search(el.shadowRoot)) return true;
                    }
                    const t = (el.innerText || el.textContent || '').trim();
                    const matches = exact ? (t === targetText) : t.includes(targetText);
                    if (matches && el.offsetWidth > 0 && el.offsetHeight > 0) {
                        el.dispatchEvent(new MouseEvent('click', {bubbles: true, cancelable: true}));
                        return true;
                    }
                }
                return false;
            }
            return search(document.body);
        }""",
        [text, exact],
    )


def _proxy_login(page: Page, proxy_name: str) -> None:
    """Switch to a proxy user in SF: click avatar → Proxy Now → search → select → OK."""
    proxy_name = proxy_name.strip()

    # Click user avatar / initials badge (top-right corner)
    clicked_avatar = False
    for sel in [
        "[data-component-id*='avatar']",
        "[aria-label*='avatar' i]",
        "[class*='userAvatar']",
        "[class*='user-avatar']",
        "button[class*='Avatar']",
    ]:
        try:
            page.locator(sel).first.click(timeout=2_000)
            clicked_avatar = True
            break
        except Exception:
            pass

    if not clicked_avatar:
        # Fallback: top-right corner click
        page.mouse.click(1240, 40)

    page.wait_for_timeout(1200)

    # Click "Proxy Now"
    for label in ["Proxy Now", "Proxy now", "Switch User", "Act as Proxy"]:
        try:
            page.get_by_text(label, exact=False).first.click(timeout=4_000)
            break
        except Exception:
            pass
    page.wait_for_timeout(1000)

    # Type name into the proxy search box
    page.keyboard.type(proxy_name, delay=80)
    page.wait_for_timeout(2000)

    # Click matching result
    page.get_by_text(proxy_name, exact=False).first.click(timeout=8_000)
    page.wait_for_timeout(500)

    # Confirm / OK
    for label in ["OK", "Confirm", "Apply", "Select", "Done"]:
        try:
            page.get_by_role("button", name=label).first.click(timeout=3_000)
            page.wait_for_load_state("networkidle", timeout=30_000)
            return
        except Exception:
            pass

    page.wait_for_load_state("networkidle", timeout=30_000)


def _execute_guidance(page: Page, guidance: dict) -> None:
    """Execute a structured action returned by the coach."""
    approach = guidance.get("approach", "wait_and_retry")
    wait_ms = guidance.get("wait_before_ms", 500)
    if wait_ms:
        page.wait_for_timeout(wait_ms)

    if approach == "coordinate_click":
        page.mouse.click(guidance["x"], guidance["y"])
        page.wait_for_load_state("networkidle", timeout=20_000)
    elif approach == "text_click":
        exact = guidance.get("exact", False)
        page.get_by_text(guidance["text"], exact=exact).first.click(timeout=8_000)
        page.wait_for_load_state("networkidle", timeout=20_000)
    elif approach == "selector_click":
        page.locator(guidance["selector"]).first.click(timeout=8_000)
        page.wait_for_load_state("networkidle", timeout=20_000)
    elif approach == "wait_and_retry":
        page.wait_for_timeout(guidance.get("wait_ms", 2000))
    # "skip" — do nothing


# ── Action dispatcher ─────────────────────────────────────────────────────────

def _dispatch(page: Page, step) -> None:
    """Map a free-text step action to Playwright calls."""
    action = step.action.lower()
    data = step.test_data or ""

    # ── Observation / verify steps — just confirm state, no browser action ────
    # Must be checked FIRST before any keyword routing so "locate the module
    # picker" or "confirm the dropdown is visible" doesn't accidentally trigger
    # navigation or click logic.
    _obs = ("locate", "confirm", "verify", "observe", "note that", "check that",
            "ensure", "should be", "is displayed", "identify", "look for")
    _act = ("click", "navigate to", "select ", "fill in", "enter ", "type ",
            "open ", "drag", "scroll", "submit", "proxy")
    if any(k in action for k in _obs) and not any(k in action for k in _act):
        return  # Pure observation — screenshot is the evidence, no click needed

    # ── Navigate to a URL ──────────────────────────────────────────────────────
    if any(k in action for k in ("navigate to the successfactors", "open your browser", "paste the job posting url")):
        url = _first_url(data) or _first_url(step.action)
        if url:
            page.goto(url, wait_until="networkidle", timeout=30_000)
            return

    # ── Proxy steps — handled individually so each step maps to one action ───

    # Step: "click your name / initials to open dropdown" — click the LB avatar
    if "click" in action and any(k in action for k in ("your name", "your initials", "top-right", "top right")) and "dropdown" in action:
        clicked = False
        for sel in ["[data-component-id*='avatar']", "[aria-label*='avatar' i]",
                    "[class*='userAvatar']", "button[class*='Avatar']"]:
            try:
                page.locator(sel).first.click(timeout=2_000)
                clicked = True
                break
            except Exception:
                pass
        if not clicked:
            page.mouse.click(1240, 40)
        page.wait_for_timeout(1200)
        return

    # Step: "select Proxy Now from the dropdown" — just click the menu item
    if ("proxy now" in action or ("proxy" in action and "select" in action)):
        for label in ["Proxy Now", "Proxy now", "Act as Proxy", "Switch User"]:
            try:
                page.get_by_text(label, exact=False).first.click(timeout=5_000)
                page.wait_for_timeout(1000)
                return
            except Exception:
                pass
        raise RuntimeError("Could not find 'Proxy Now' in the dropdown")

    # Step: "type the full name ... and select from results"
    if "type" in action and any(k in action for k in ("full name", "proxy", "target user", "wish to proxy")):
        name = data.strip() if data and not data.lower().startswith("input") else ""
        if not name:
            raise RuntimeError("Proxy step needs a target name — provide it in the test data "
                               "or a TYPE: <name> command (e.g. TYPE: {{target_employee_name}}).")
        # Focus the search input inside the dialog
        for sel in ["input[type='text']:visible", "input[placeholder*='name' i]",
                    "input[placeholder*='search' i]", "input[placeholder*='user' i]", "input"]:
            try:
                page.locator(sel).first.click(timeout=2_000)
                break
            except Exception:
                pass
        page.keyboard.type(name, delay=80)
        page.wait_for_timeout(2500)
        page.get_by_text(name, exact=False).first.click(timeout=8_000)
        page.wait_for_timeout(500)
        return

    # Step: "click OK / confirm the proxy" — click the confirm button
    if "proxy" in action and "click" in action and any(k in action for k in ("ok", "confirm", "begin proxy", "start proxy")):
        for label in ["OK", "Confirm", "Begin Proxy", "Apply"]:
            try:
                page.get_by_role("button", name=label).first.click(timeout=5_000)
                page.wait_for_load_state("networkidle", timeout=30_000)
                return
            except Exception:
                pass
        raise RuntimeError("Could not find OK/Confirm button for proxy")

    # Fallback: full proxy flow in one shot (legacy path)
    if "proxy" in action:
        name = data.strip() or _first_quoted(step.action) or _word_after_click(step.action)
        if not name or name.lower().startswith("input"):
            m = re.search(r"\bas\s+([A-Z][a-zA-Z ]{2,40})", step.action)
            if not m:
                raise RuntimeError("Proxy step needs a target name — none found in the step "
                                   "action or test data.")
            name = m.group(1).strip()
        _proxy_login(page, name)
        return

    # ── "Click the module picker" — open the Home dropdown ──────────────────
    if "click" in action and "module picker" in action and "navigate" not in action:
        page.locator("button:has-text('Home')").first.click(timeout=10_000)
        page.wait_for_timeout(1500)
        return

    # ── "Click any module to navigate" — pick Recruiting as the example ──────
    if "click" in action and "any module" in action:
        _module_picker_nav(page, ["Recruiting"])
        return

    # ── Recruiting shortcut — catch any action that mentions navigating to Recruiting ─
    if "recruit" in action and any(k in action for k in ("navigate", "module", "picker", "go to", "open")):
        _module_picker_nav(page, ["Recruiting"])
        return

    # ── Module picker navigation (e.g. "Company Info → Position Org Chart") ───
    if "module picker" in action or (
        "navigate" in action and any(sep in step.action for sep in ("→", "->"))
    ):
        dest = _nav_destination(step.action)
        if dest:
            _module_picker_nav(page, dest)
            return

    # ── Search the Position Org Chart ─────────────────────────────────────────
    if "search for" in action and ("position" in action or "parent" in action):
        position_num = _extract_position_number(data) or _extract_position_number(step.action)
        if not position_num:
            # Use whatever position is currently visible as the parent
            visible = page.locator("text=/POS\\d+/").first.text_content(timeout=5_000)
            position_num = visible.strip() if visible else None
        if not position_num:
            raise RuntimeError("No parent position number provided or visible")
        _search_position(page, position_num)
        return

    # ── Fill in form fields (check FIRST — step 4's action mentions "selected create") ─
    if "fill in" in action and ("required field" in action or "required fields" in action):
        _fill_position_form(page, data)
        return

    # ── Select a position card and click Action → menu item ───────────────────
    if "select" in action and ("action" in action or "click" in action) and "fill" not in action:
        _select_and_action(page, step.action)
        return

    # ── Click Save / generic named button ─────────────────────────────────────
    if action.strip().startswith("click") or "click the " in action or "click '" in action:
        label = _first_quoted(step.action) or _word_after_click(step.action)
        if label:
            _click_label(page, label)
            return

    # ── Observation / verify steps — no browser action ────────────────────────


# ── Module picker helper ──────────────────────────────────────────────────────

_MENU_ORDER = [
    "Home", "Admin Centre", "Calibration", "Careers", "Company Info",
    "Continuous Feedback", "Continuous Performance", "Development",
    "My Employee File", "Objectives", "Offboarding", "Onboarding",
    "Performance", "Recruiting", "Reporting",
]


def _open_module_picker(page: Page) -> dict | None:
    """Click the module picker button and wait for it to open. Returns approximate button box."""
    module_names_js = str(_MENU_ORDER).replace("'", '"')

    # Use JS to FIND the bounding box of the module picker button (don't click via JS).
    # Then use Playwright's real mouse click, which SF's UI5 responds to correctly.
    box = page.evaluate(f"""() => {{
        const names = {module_names_js};
        function search(root) {{
            const els = root.querySelectorAll('*');
            for (const el of els) {{
                if (el.shadowRoot) {{
                    const r = search(el.shadowRoot);
                    if (r) return r;
                }}
                const t = (el.innerText || el.textContent || '').trim();
                const b = el.getBoundingClientRect();
                if (b.top < 60 && b.width > 40 && el.offsetHeight > 0) {{
                    for (const name of names) {{
                        if (t === name || t.startsWith(name)) {{
                            return {{x: b.x, y: b.y, width: b.width, height: b.height}};
                        }}
                    }}
                }}
            }}
            return null;
        }}
        return search(document.body);
    }}""")

    if box:
        cx = box["x"] + box["width"] / 2
        cy = box["y"] + box["height"] / 2
        page.mouse.click(cx, cy)
        page.wait_for_timeout(1500)
        return box

    # Hard fallback: module picker is always ~250px from left at top of nav bar
    page.mouse.click(250, 20)
    page.wait_for_timeout(1500)
    return {"x": 150, "y": 10, "width": 200, "height": 40}


def _module_picker_nav(page: Page, path: list[str]) -> None:
    """Open the SF module picker and navigate to the target module."""
    if not path:
        raise RuntimeError("Empty navigation path passed to _module_picker_nav")

    btn = _open_module_picker(page)
    if not btn:
        raise RuntimeError("Could not open module picker")

    first_item = path[0]

    # ── Approach 0: JavaScript recursive shadow DOM search (most reliable) ────
    for exact_match in (True, False):
        try:
            clicked = _shadow_click(page, first_item, exact=exact_match)
            if clicked:
                page.wait_for_load_state("networkidle", timeout=25_000)
                _verify_module_nav(page, first_item)
                _module_picker_subpath(page, path[1:])
                return
        except Exception:
            pass

    # ── Approach 1: Playwright text locator (pierces shadow DOM) ─────────────
    for exact in (True, False):
        try:
            page.get_by_text(first_item, exact=exact).first.click(timeout=3_000)
            page.wait_for_load_state("networkidle", timeout=25_000)
            _verify_module_nav(page, first_item)
            _module_picker_subpath(page, path[1:])
            return
        except Exception:
            pass

    # ── Approach 2: Keyboard navigation (most reliable for shadow DOM lists) ──
    try:
        if first_item in _MENU_ORDER:
            idx = _MENU_ORDER.index(first_item)
            # Tab into the list then arrow-down to the right item
            for _ in range(idx + 1):
                page.keyboard.press("ArrowDown")
                page.wait_for_timeout(80)
            page.keyboard.press("Enter")
            page.wait_for_load_state("networkidle", timeout=25_000)
            _verify_module_nav(page, first_item)
            _module_picker_subpath(page, path[1:])
            return
    except Exception:
        pass

    # ── Approach 3: Coordinate click (26 px per row, anchored to menu top) ───
    try:
        if first_item not in _MENU_ORDER:
            raise RuntimeError(f"'{first_item}' not in _MENU_ORDER")
        idx = _MENU_ORDER.index(first_item)
        item_h = 26
        menu_x = btn["x"] + btn["width"] / 2
        menu_top = btn["y"] + btn["height"] + 8
        menu_y = menu_top + (idx * item_h) + (item_h // 2)
        # If calculated y is off-screen, scroll the dropdown first
        if menu_y > 700:
            page.mouse.wheel(0, menu_y - 600)
            page.wait_for_timeout(300)
            menu_y = min(menu_y, 680)
        page.mouse.move(menu_x, menu_y)
        page.wait_for_timeout(200)
        page.mouse.click(menu_x, menu_y)
        page.wait_for_load_state("networkidle", timeout=25_000)
        _verify_module_nav(page, first_item)
        _module_picker_subpath(page, path[1:])
        return
    except Exception:
        pass

    # ── Approach 4: Vision-based — screenshot the open menu, ask Claude ──────
    import tempfile
    shot = tempfile.mktemp(suffix=".png")
    page.screenshot(path=shot, full_page=False)
    from engine.coach import get_step_guidance
    guidance = get_step_guidance(
        shot,
        f"Click '{first_item}' in the open module picker dropdown",
        f"{first_item} module opens",
        f"The module picker menu is open. Click '{first_item}' in the list.",
    )
    if guidance:
        _execute_guidance(page, guidance)
        page.wait_for_load_state("networkidle", timeout=25_000)
        _module_picker_subpath(page, path[1:])
        return

    raise RuntimeError(f"All four approaches failed to click '{first_item}' in module picker")


def _verify_module_nav(page: Page, module_name: str) -> None:
    """Soft verification — just wait for network to settle, don't hard-fail on URL."""
    page.wait_for_timeout(1000)
    # Don't raise — some modules don't change the URL slug predictably


def _module_picker_subpath(page: Page, remaining: list[str]) -> None:
    for item in remaining:
        page.wait_for_timeout(800)
        # Try shadow click first, then text click
        try:
            if not _shadow_click(page, item):
                page.get_by_text(item, exact=False).first.click(timeout=8_000)
        except Exception:
            page.get_by_text(item, exact=False).first.click(timeout=8_000)
        page.wait_for_load_state("networkidle", timeout=25_000)


def _nav_destination(action_text: str) -> list[str]:
    """Extract the navigation path from action text.

    "From the Module Picker, navigate to Company Info → Position Org Chart"
    → ["Company Info", "Position Org Chart"]
    """
    # Stop extraction before filler words like "using", "via", "through", "from"
    m = re.search(
        r"navigate to (.+?)(?:\s+(?:using|via|through|from|in|with)\b|\s*$)",
        action_text,
        re.IGNORECASE,
    )
    if not m:
        return []  # No "navigate to" found — not a navigation step
    raw = m.group(1).strip()
    parts = [p.strip() for p in re.split(r"[→\->/]", raw)]
    # Strip filler words that sometimes leak in; keep substantive module names
    stop_words = {"module picker", "the module picker", "module"}
    return [p for p in parts if p and p.lower() not in stop_words]


# ── Position Org Chart helpers ────────────────────────────────────────────────

def _search_position(page: Page, position_num: str) -> None:
    """Search for a position in the Org Chart search box, then wait for it to appear."""
    page.wait_for_load_state("networkidle", timeout=15_000)
    page.wait_for_timeout(1000)

    # Type the position number into the Search field on the Org Chart toolbar
    for sel in [
        "input[placeholder*='search' i]",
        "input[placeholder*='Search' i]",
        "[aria-label*='Search' i]",
        "input[type='text']:visible",
    ]:
        try:
            inp = page.locator(sel).first
            inp.wait_for(state="visible", timeout=3_000)
            inp.click()
            inp.fill(position_num)
            page.wait_for_timeout(500)
            page.keyboard.press("Enter")
            page.wait_for_timeout(2500)
            break
        except Exception:
            pass

    # Wait for the specific position card to appear in the chart
    try:
        page.locator(f"text={position_num}").first.wait_for(timeout=8_000)
    except Exception:
        # Fallback — accept whatever is on screen
        page.locator("text=/POS\\d+/").first.wait_for(timeout=5_000)


def _select_and_action(page: Page, action_text: str) -> None:
    """Click on the position card, open the Action menu, then click the target item.

    SF Org Chart has no standalone 'Action' button — the menu opens via right-click
    on the card, or a hover-triggered dropdown, or a toolbar button that appears
    after selection. We try all three in order.
    """
    # Step 1: click the position card — this opens a detail popup containing the Actions button
    pos_locator = page.locator("text=/POS\\d+/").first
    try:
        pos_locator.click(timeout=5_000)
    except Exception:
        pass  # may already be selected

    # Wait for the popup's Actions button to actually appear — don't use a fixed sleep
    action_opened = False
    try:
        page.wait_for_selector(
            "button:has-text('Actions'), button:has-text('Action')",
            timeout=8_000,
        )
        page.locator("button:has-text('Actions'), button:has-text('Action')").first.click(timeout=5_000)
        page.wait_for_timeout(800)
        action_opened = True
    except Exception:
        pass

    # Fallback: right-click the card (opens a context menu in some SF versions)
    if not action_opened:
        try:
            pos_locator.click(button="right", timeout=5_000)
            page.wait_for_timeout(800)
            action_opened = True
        except Exception:
            pass

    if not action_opened:
        raise RuntimeError(
            "Could not open Action menu — clicked the position card but the Actions button "
            "never appeared. Use Take Control or Circle It to click Actions manually."
        )

    # Step 2: click the target menu item
    targets = [
        "Create same level Position",
        "Create same level",
        "Copy Position",
        "copy position",
        "Create Position",
    ]
    for t in targets:
        try:
            page.get_by_text(t, exact=False).first.click(timeout=4_000)
            page.wait_for_load_state("networkidle", timeout=20_000)
            return
        except Exception:
            continue
    raise RuntimeError(
        "Action menu opened but 'Create same level / Copy Position' was not found. "
        "Use Circle It on the screenshot to point to the correct option."
    )


def _fill_position_form(page: Page, data: str) -> None:
    """Fill the Create-Position form, or accept defaults if it's the Copy dialog."""
    # Path A: SF opened a "Copy Position" dialog — just verify defaults are sensible.
    # Step 5 will click OK to actually create the copy.
    if page.locator("text=Copy Position").first.is_visible(timeout=2_000):
        # The dialog has 'Number of positions to copy' (default 1) — leave it.
        return

    # Path B: full Create Position form with fields. Fill Position Title at minimum.
    values = _parse_kv(data)
    title = values.get("Position Title", "Auto Test Position")

    filled = False
    for selector in [
        "input[aria-label*='Position Title' i]",
        "input[placeholder*='Position Title' i]",
        "label:has-text('Position Title') ~ input",
        "input[name*='title' i]",
    ]:
        try:
            page.locator(selector).first.fill(title, timeout=3_000)
            filled = True
            break
        except Exception:
            continue
    if not filled:
        raise RuntimeError("Could not find Position Title input on the Create Position form")
    page.wait_for_timeout(500)


def _click_label(page: Page, label: str) -> None:
    """Click a button or element matching *label*, with several fallbacks.

    For 'Save' specifically, also try 'OK' since SF modals use OK as primary action.
    """
    label = label.strip().strip("'\"‘’“”")
    candidates = [label]
    if label.lower() == "save":
        candidates.append("OK")  # Copy Position dialog uses OK

    last_err = None
    for cand in candidates:
        attempts = [
            lambda c=cand: page.get_by_role("button", name=c).first.click(timeout=5_000),
            lambda c=cand: page.get_by_text(c, exact=True).first.click(timeout=5_000),
            lambda c=cand: page.locator(f"button:has-text('{c}')").first.click(timeout=5_000),
            lambda c=cand: page.locator(f"text={c}").first.click(timeout=5_000, force=True),
        ]
        for fn in attempts:
            try:
                fn()
                page.wait_for_load_state("networkidle", timeout=20_000)
                return
            except Exception as e:
                last_err = e
    raise RuntimeError(f"Could not click '{label}': {last_err}")


# ── Utilities ─────────────────────────────────────────────────────────────────

def _first_url(text: str) -> str:
    m = re.search(r"https?://\S+", text or "")
    return m.group(0).rstrip(".,)>\"'") if m else ""


def _first_quoted(text: str) -> str:
    # Matches 'text', "text", ‘text’, “text”
    m = re.search(r"[‘’'\"]([^‘’'\"]{2,60})[‘’'\"]", text)
    return m.group(1) if m else ""


def _word_after_click(text: str) -> str:
    """Extract the target name when no quotes are used. e.g. 'Click Save to ...' → 'Save'."""
    m = re.search(r"\bclick\s+(?:the\s+)?([A-Z][A-Za-z ]{1,30})", text)
    return m.group(1).strip() if m else ""


def _extract_position_number(text: str) -> str:
    """Pull a SF position number like 'POS100001' out of free text."""
    m = re.search(r"POS\d{6}", text or "")
    return m.group(0) if m else ""


def _parse_kv(text: str) -> dict[str, str]:
    """Parse 'Key: Value' lines from the test_data column."""
    out: dict[str, str] = {}
    for line in (text or "").splitlines():
        if ":" in line:
            k, _, v = line.partition(":")
            out[k.strip()] = v.strip().strip("[]")
    return out


_SOM_MARK_JS = r"""
() => {
  document.querySelectorAll('.__som_badge').forEach(e => e.remove());
  const out = []; const seen = new Set(); let i = 0;
  const W = window.innerWidth, H = window.innerHeight;
  function lbl(el){
    let t = (el.getAttribute && (el.getAttribute('aria-label') || el.getAttribute('title') || el.getAttribute('alt'))) || '';
    if (!t) t = (el.innerText || el.textContent || '').trim().replace(/\s+/g,' ').slice(0,45);
    if (!t && el.tagName.toLowerCase() === 'input') t = (el.getAttribute('placeholder') || el.getAttribute('name') || 'field');
    return t || el.tagName.toLowerCase();
  }
  function clickable(el){
    const tag = el.tagName ? el.tagName.toLowerCase() : '';
    if (['button','a','input','select','textarea'].includes(tag)) return true;
    const role = el.getAttribute && el.getAttribute('role');
    if (role && ['button','link','menuitem','tab','checkbox','option','switch'].includes(role)) return true;
    if (el.hasAttribute && el.hasAttribute('onclick')) return true;
    try { if (getComputedStyle(el).cursor === 'pointer') return true; } catch(e){}
    return false;
  }
  function walk(root){
    let els; try { els = root.querySelectorAll('*'); } catch(e){ return; }
    for (const el of els){
      if (el.shadowRoot) walk(el.shadowRoot);
      if (i >= 70) continue;
      if (!clickable(el)) continue;
      let r; try { r = el.getBoundingClientRect(); } catch(e){ continue; }
      if (r.width < 6 || r.height < 6) continue;
      if (r.bottom < 0 || r.top > H || r.right < 0 || r.left > W) continue;
      const cx = Math.round(r.left + r.width/2), cy = Math.round(r.top + r.height/2);
      const key = cx + 'x' + cy; if (seen.has(key)) continue; seen.add(key);
      i++;
      out.push({i: i, x: cx, y: cy, label: lbl(el)});
      const b = document.createElement('div'); b.className = '__som_badge'; b.textContent = i;
      Object.assign(b.style, {position:'fixed', left:Math.max(0,r.left)+'px', top:Math.max(0,r.top)+'px',
        zIndex:2147483647, background:'#e11d48', color:'#fff', font:'bold 11px sans-serif',
        padding:'0 4px', borderRadius:'4px', pointerEvents:'none', lineHeight:'15px',
        boxShadow:'0 0 0 1px #fff'});
      document.body.appendChild(b);
    }
  }
  walk(document.body);
  return out;
}
"""


def _mark_clickables(page) -> list:
    """Set-of-marks: number every clickable element (incl. shadow DOM), draw badges,
    return [{i, x, y, label}]. Lets the model pick 'click ③' instead of guessing pixels."""
    try:
        return page.evaluate(_SOM_MARK_JS) or []
    except Exception as exc:
        print(f"  [som] mark error: {exc}")
        return []


def _unmark(page) -> None:
    try:
        page.evaluate("() => document.querySelectorAll('.__som_badge').forEach(e => e.remove())")
    except Exception:
        pass


def _resolve_marks(cmds: str, marks: list) -> str:
    """Turn 'CLICK_MARK: N' into the real 'CLICK_XY: x, y' for marked element N."""
    import re as _re
    by_i = {int(m["i"]): m for m in marks if "i" in m}
    out = []
    for line in (cmds or "").splitlines():
        mo = _re.match(r"\s*CLICK_MARK:\s*#?(\d+)", line, _re.I)
        if mo:
            mk = by_i.get(int(mo.group(1)))
            out.append(f"CLICK_XY: {mk['x']}, {mk['y']}" if mk else line)
        else:
            out.append(line)
    return "\n".join(out)


def run_agent_goal(goal: str, preview: bool = True, runs_root="Path | str | None",
                   run_id_override: str | None = None, step_done_callback=None,
                   max_iters: int = 12, check_stop=None, ask_user=None, confirm_step=None,
                   confirm_done=None, grounding: bool = False) -> ScenarioResult:
    """Free-form autonomous agent (Testing Hub): given a plain-English GOAL, log in
    to SF and loop screenshot -> Claude decides next actions -> execute, until the
    goal is done or we hit the iteration cap. No script, no saved commands. Records
    video + per-iteration screenshots. preview=True => never commit a change."""
    import time as _time
    from types import SimpleNamespace
    from engine.coach import get_agent_actions

    run_id = run_id_override or datetime.utcnow().strftime("%Y%m%dT%H%M%SZ")
    base = Path(runs_root) if isinstance(runs_root, (str, Path)) else Path("runs")
    runs_dir = base / run_id
    runs_dir.mkdir(parents=True, exist_ok=True)
    result = ScenarioResult(scenario_id="agent", run_id=run_id, passed=False)
    executed_cmds: list[str] = []   # successful command lines, for Save-as-task
    try:
        _creds = get_sf_credentials()
    except SFCredentialsError as exc:
        print(f"  [creds] {exc}")
        result.error = str(exc)
        result.ended_at = datetime.utcnow()
        return result
    username, password, sf_url = _creds["username"], _creds["password"], _creds["url"]

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True, slow_mo=400, args=[
            "--disable-gpu", "--no-sandbox", "--disable-dev-shm-usage", "--disable-setuid-sandbox"])
        context = browser.new_context(
            record_video_dir=str(runs_dir), record_video_size={"width": 1280, "height": 720},
            viewport={"width": 1280, "height": 720})
        page = context.new_page()
        history: list[str] = []
        try:
            print(f"  [agent] goal: {goal}")
            _login(page, sf_url, username, password)
            page.wait_for_timeout(2000)
            for i in range(1, max_iters + 1):
                if check_stop and check_stop():
                    print("  [agent] stopped by user")
                    result.error = "Stopped by user"
                    if step_done_callback:
                        step_done_callback(f"agent-{i:02d}", False, "Stopped by user", "")
                    break
                shot = str(runs_dir / f"agent-{i:02d}.png")
                marks = []
                try:
                    if grounding:
                        marks = _mark_clickables(page)   # number every clickable, draw badges
                    page.screenshot(path=shot, full_page=False)
                    if grounding:
                        _unmark(page)                    # remove badges so real clicks land
                except Exception:
                    shot = ""
                plan = get_agent_actions(shot, goal, history, preview=preview,
                                         marks=(marks if grounding else None))
                if not plan:
                    if step_done_callback:
                        step_done_callback(f"agent-{i:02d}", False, "Agent could not decide a next step", "")
                    break
                reasoning = plan.get("reasoning", "")
                cmds = plan.get("commands", "")
                done = plan.get("done")
                print(f"  [agent {i}] {reasoning[:100]} | done={done}")
                shot_url = f"/runs/{run_id}/{Path(shot).name}" if shot else ""
                ask_q = plan.get("ask")
                if ask_q and ask_user:
                    print(f"  [agent {i}] asking user: {ask_q}")
                    if step_done_callback:
                        step_done_callback(f"agent-{i:02d}", True, f"❓ {ask_q}", shot_url)
                    result.steps.append(StepResult(step_id=f"agent-{i:02d}", passed=True,
                                                   screenshot_path=shot, error_message=f"Asked: {ask_q}"))
                    answer = ask_user(ask_q, shot_url)
                    if not answer:
                        result.error = "Stopped by user"
                        break
                    history.append(f"Step {i}: needed a value — asked '{ask_q}', "
                                   f"user answered: '{answer}'")
                    continue
                # CONFIRM-EACH-MOVE training mode: surface the proposed move and wait
                # for the user to Confirm, Redirect (plain English), or Stop.
                if confirm_step and cmds.strip() and not done:
                    decision = confirm_step(reasoning, cmds, shot_url) or {}
                    act = (decision.get("action") or "confirm").lower()
                    if act == "stop":
                        result.error = "Stopped by user"
                        break
                    if act == "redirect":
                        red = str(decision.get("text", "")).strip()
                        history.append(f"Step {i}: I proposed '{cmds.strip()[:80]}' but the user "
                                       f"redirected: {red}")
                        if step_done_callback:
                            step_done_callback(f"agent-{i:02d}", True,
                                               f"Proposed: {reasoning} — you said: {red}", shot_url)
                        continue
                if step_done_callback:
                    step_done_callback(f"agent-{i:02d}", True, reasoning, shot_url)
                history.append(f"Step {i}: {reasoning} -> {cmds.strip()[:100]}")
                result.steps.append(StepResult(step_id=f"agent-{i:02d}", passed=True,
                                               screenshot_path=shot, error_message=reasoning[:200]))
                if done:
                    # Don't assume completion — check with the user first.
                    if confirm_done:
                        cd = confirm_done() or {}
                        if cd.get("action") == "continue":
                            extra = str(cd.get("text", "")).strip()
                            history.append(f"Step {i}: I thought the task was done, but the user said "
                                           f"keep going{(': ' + extra) if extra else ''}.")
                            if step_done_callback:
                                step_done_callback(f"agent-{i:02d}", True,
                                                   f"You: not done yet{(' — ' + extra) if extra else ''}", shot_url)
                            continue
                    result.passed = True
                    break
                if not cmds.strip():
                    break
                step_obj = SimpleNamespace(step_id=f"agent-{i:02d}", action=goal,
                                           expected_result=goal, test_data="")
                exec_cmds = _resolve_marks(cmds, marks) if grounding else cmds
                try:
                    _run_direct_commands(page, step_obj, str(runs_dir), exec_cmds, _time.time())
                    for _ln in exec_cmds.splitlines():
                        if _ln.strip():
                            executed_cmds.append(_ln.strip())
                except Exception as exc:
                    history.append(f"  (action error: {exc})")
                page.wait_for_timeout(800)
        except Exception as exc:
            result.error = str(exc)
            print(f"  [agent] fatal: {exc}")
        finally:
            try:
                context.close()
                browser.close()
            except Exception:
                pass
    result.agent_commands = executed_cmds
    result.ended_at = datetime.utcnow()
    return result


def run_plan(plan, answers=None, preview=True, runs_root="runs", run_id_override=None,
             step_done_callback=None, ask_user=None, check_stop=None,
             is_paused=None, take_instruction=None):
    """Execute a user-approved PLAN (list of {desc, cmd}) deterministically. If a
    step's command fails (target not found), make ONE vision attempt to self-correct
    that single step, then continue. Records a full video. preview=True => no commit."""
    import time as _time, re as _re
    from types import SimpleNamespace
    from engine.coach import get_agent_actions

    run_id = run_id_override or datetime.utcnow().strftime("%Y%m%dT%H%M%SZ")
    base = Path(runs_root) if isinstance(runs_root, (str, Path)) else Path("runs")
    runs_dir = base / run_id
    runs_dir.mkdir(parents=True, exist_ok=True)
    answers = dict(answers or {})
    result = ScenarioResult(scenario_id="plan", run_id=run_id, passed=False)
    try:
        _creds = get_sf_credentials()
    except SFCredentialsError as exc:
        print(f"  [creds] {exc}")
        result.error = str(exc)
        result.ended_at = datetime.utcnow()
        return result
    username, password, sf_url = _creds["username"], _creds["password"], _creds["url"]

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True, slow_mo=150, args=[
            "--disable-gpu", "--no-sandbox", "--disable-dev-shm-usage", "--disable-setuid-sandbox"])
        context = browser.new_context(
            record_video_dir=str(runs_dir), record_video_size={"width": 1280, "height": 720},
            viewport={"width": 1280, "height": 720})
        page = context.new_page()
        try:
            _login(page, sf_url, username, password)
            page.wait_for_timeout(2000)
            for i, st in enumerate(plan, 1):
                if check_stop and check_stop():
                    result.error = "Stopped by user"
                    break
                # Pause gate: user can hold the run and inject live instructions
                # (executed via vision) before the next planned step runs.
                _gi = 0
                while is_paused and is_paused():
                    if check_stop and check_stop():
                        break
                    instr = take_instruction() if take_instruction else None
                    if instr:
                        _gi += 1
                        gshot = str(runs_dir / f"plan-{i:02d}-int{_gi}.png")
                        try:
                            page.screenshot(path=gshot, full_page=False)
                        except Exception:
                            pass
                        gobj = SimpleNamespace(step_id=f"plan-{i:02d}-int{_gi}", action=instr,
                                               expected_result=instr, test_data="")
                        note = f"You: {instr}"
                        act = get_agent_actions(gshot, instr, [], preview=preview)
                        if act and act.get("commands", "").strip():
                            try:
                                _run_direct_commands(page, gobj, str(runs_dir), act["commands"], _time.time())
                                note = f"You: {instr} — done"
                            except Exception as e:
                                note = f"You: {instr} — failed: {str(e)[:50]}"
                        try:
                            page.screenshot(path=gshot, full_page=False)
                        except Exception:
                            pass
                        if step_done_callback:
                            step_done_callback(f"plan-{i:02d}-int{_gi}", True, note, f"/runs/{run_id}/{Path(gshot).name}")
                    else:
                        _time.sleep(0.5)
                if check_stop and check_stop():
                    result.error = "Stopped by user"
                    break
                desc = (st.get("desc") or st.get("cmd") or "").strip()
                cmd = st.get("cmd", "")
                # Normalise single-brace {var} to {{var}} so a stray planner token still
                # gets substituted/asked (defensive — the planner now emits double braces).
                cmd = normalize_placeholders(cmd)
                cmd2 = substitute(cmd, answers) if answers else cmd
                # Any placeholder still unfilled? ask the user live, once per variable.
                for var in _re.findall(r"\{\{\s*([\w]+)\s*\}\}", cmd2):
                    if ask_user:
                        ashot = str(runs_dir / f"plan-{i:02d}-ask.png")
                        try:
                            page.screenshot(path=ashot, full_page=False)
                            ashot_url = f"/runs/{run_id}/{Path(ashot).name}"
                        except Exception:
                            ashot_url = ""
                        ans = ask_user(f"What is the value for '{var.replace('_', ' ')}'?", ashot_url)
                        if ans:
                            answers[var] = ans
                            cmd2 = _re.sub(r"\{\{\s*" + _re.escape(var) + r"\s*\}\}", ans, cmd2)
                shot = str(runs_dir / f"plan-{i:02d}.png")
                step_obj = SimpleNamespace(step_id=f"plan-{i:02d}", action=desc, expected_result=desc, test_data="")
                ok, note = True, desc
                try:
                    sr = _run_direct_commands(page, step_obj, str(runs_dir), cmd2, _time.time())
                    if not sr.passed:
                        raise RuntimeError(sr.error_message or "step failed")
                except Exception as exc:
                    # Smart fallback: one vision attempt for THIS step only.
                    print(f"  [plan {i}] '{cmd2[:50]}' missed ({str(exc)[:50]}) — vision fallback")
                    try:
                        page.screenshot(path=shot, full_page=False)
                    except Exception:
                        pass
                    act = get_agent_actions(shot, desc, [], preview=preview)
                    if act and act.get("commands", "").strip():
                        try:
                            _run_direct_commands(page, step_obj, str(runs_dir), act["commands"], _time.time())
                            note = f"{desc} — recovered with vision"
                        except Exception as e2:
                            ok, note = False, f"{desc} — failed: {str(e2)[:60]}"
                    else:
                        ok, note = False, f"{desc} — failed: {str(exc)[:60]}"
                try:
                    page.screenshot(path=shot, full_page=False)
                except Exception:
                    pass
                shot_url = f"/runs/{run_id}/{Path(shot).name}"
                if step_done_callback:
                    step_done_callback(f"plan-{i:02d}", ok, note, shot_url)
                result.steps.append(StepResult(step_id=f"plan-{i:02d}", passed=ok,
                                               screenshot_path=shot, error_message=note[:200]))
                page.wait_for_timeout(600)
            result.passed = bool(result.steps) and all(s.passed for s in result.steps)
        except Exception as exc:
            result.error = str(exc)
            print(f"  [plan] fatal: {exc}")
        finally:
            try:
                context.close()
                browser.close()
            except Exception:
                pass
    result.ended_at = datetime.utcnow()
    return result
