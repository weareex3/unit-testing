# EX3 TestOps — Build Handover (post-audit)

Written 2026-07-06 by a full-codebase audit session (Fable 5). You are the implementation
session. Every finding below was verified by reading the actual code — file/line refs are
exact as of commit 817ea52 plus the current working tree. Your job: implement Tasks 1-8 in
order, commit per task, and leave the tree ready for a follow-up audit that will check each
acceptance criterion. Do not deploy — the user runs `railway up` themselves.

## 0. Context — what this product is

Self-teaching UAT automation for SAP SuccessFactors. Loop: upload Excel workbook → parse
scenarios → match against a learned task library → replay known steps deterministically /
AI-vision-attempt unknown steps → human takes over when needed → approve → save commands
back to the library → future runs replay them. Evidence (video + screenshots + audit trail)
backs UAT sign-off. Deployed on Railway; prod uses a `/data` persistent volume.

The product's core asset is the learned-command library. The product's core promise is
"a pass means it really passed". Most tasks below protect one of those two things.

### Architecture map

- `run.py` — CLI entry.
- `engine/parser.py` — Excel workbook → `TestScenario`/`TestStep` (models/dataclasses.py).
- `engine/runner.py` (~1965 lines) — Playwright execution. Three entry points:
  - `run_scenario` (line ~111) — scripted scenario pipeline (batch + single runs).
  - `run_agent_goal` (line ~1678) — free-form Testing Hub agent loop.
  - `run_plan` (line ~1824) — executes a user-approved plan with one vision fallback per step.
  - `_run_direct_commands` (line ~772) — the text-DSL executor. **It never raises** — it
    returns `StepResult(passed=False, error_message=...)` on any failure. Two callers
    wrongly assume it throws; that's Tasks 1-2.
  - `_run_step` (line ~634) — priority ladder: saved commands → vision → keyword dispatch.
- `engine/coach.py` — all AI calls: `get_vision_commands`, `get_agent_actions` (the Hub
  agent brain), `get_task_plan`, `verify_step_result` (legacy, fail-open),
  `verify_expected_result` (oracle, fail-closed), `get_step_guidance` (legacy retry coach).
- `engine/tools.py` — finished but UNWIRED native tool-use schemas + `serialize_tool_calls()`
  (tool_use blocks → text DSL). Task 7 wires it in.
- `engine/visual_verifier.py` — `verify_step`, the scenario-level verification gate used in
  `run_scenario` (inconclusive = don't block, by design).
- `ui/server.py` (~5139 lines, FastAPI) — the whole web app. DO NOT split it. Key regions:
  storage/path setup (~1-360), match cache + `_compute_match_results` (~420-550), git
  persistence (~551-625), auth (~880-1720), uploads (~1830-1996), matching/coverage AI
  (~2128-2400), batch (~2434-3270), Testing Hub `/api/lab/*` (~3325-3517).
- `eval/` — offline eval harness (`dsl_check`, `model_eval`). Never imported by the app.
- `storage/global/step_library.json` — the learned library. `storage/<client>/` — per-client
  feedback/approved/overrides. On Railway these live under `/data/storage/...`.
- `tests/` — 25 passing unit tests (parser, context extractor, tools serializer).

### Environment / rules

- Windows 11, PowerShell. Python: `venv\Scripts\python.exe` (invoke as
  `& "<abs path>\venv\Scripts\python.exe"` in PowerShell).
- Before every commit: `python -m py_compile` on changed .py files AND
  `venv\Scripts\python.exe -m pytest tests/ -q` AND `venv\Scripts\python.exe -m eval.dsl_check`.
- One commit per task, message prefixed `Audit fix N:`. Do not commit the pre-existing
  uncommitted changes as part of your tasks — first make **Commit 0** (see Task 0).
- No code comments unless the WHY is non-obvious. No emojis. Match existing style.
- Never run the app against the live SF tenant; unit-level verification only. The user
  does live verification before the follow-up audit.
- The text DSL stays the canonical stored/executed format. Do not change the library JSON
  schema except additively. Do not touch `eval/dataset/`, the trailer script, or workbooks.

---

## Task 0 — Commit the existing working tree first

The tree already contains coherent, verified work: grounding forced on
(`engine/runner.py`, `ui/server.py`, `ui/templates/lab.html` — 1-line diffs each), new
`engine/tools.py`, `tests/test_tools_serializer.py`, `eval/`, and two sample workbooks
(`scripts/EX3_EC_Sample5_V1.xlsx`, `EX3_EC_Stress50_V1.xlsx`). Commit all of it as one
commit (message: `Precision targeting always-on + eval harness + native tool schemas`)
so audit fixes are cleanly separated.

## Task 1 — Stop failed commands from poisoning the library (CRITICAL)

**Where:** `engine/runner.py` — `run_agent_goal` lines ~1802-1806, `_run_direct_commands`
~772-858, and `StepResult` in `models/dataclasses.py`.

**Problem:** `_run_direct_commands` catches all exceptions and returns
`StepResult(passed=False)`. `run_agent_goal` wraps it in a dead `try/except`, ignores the
returned StepResult, and appends **every** line of `exec_cmds` to `executed_cmds` — including
the failing line and lines after it that never ran. `executed_cmds` becomes
`result.agent_commands` → `_LAB_LEARNED["commands"]` → `/api/lab/save`
(`ui/server.py` ~3484-3510) → `step_library.json`. Broken commands get memorized and — per
the deliberate trust-saved-commands policy at `runner.py` ~383-399 — replay forever
unverified. The agent's `history` also never learns a command failed.

**Fix:**
1. Add `executed_lines: list = field(default_factory=list)` to `StepResult`
   (`models/dataclasses.py` — check existing field style; it's a dataclass).
2. In `_run_direct_commands`, append each command line to a local list **after** that line
   executes without raising, and set it on both the success and failure StepResult returns.
   (On failure, the failing line must NOT be in the list.)
3. In `run_agent_goal`: capture `sr = _run_direct_commands(...)`. Extend `executed_cmds`
   from `sr.executed_lines` only. If `not sr.passed`, append to `history`:
   `f"  (action FAILED: {sr.error_message})"` so the model sees the failure next iteration
   (this feeds the existing ANTI-REPEAT rule). Remove the now-dead try/except.

**Accept when:** a unit test exists that fakes a Page whose click raises on the 2nd of 3
commands and asserts `executed_lines == [first line only]` and `passed is False`. (A
`SimpleNamespace`/stub Page with the few used methods is fine — see how the step objects
are already faked with SimpleNamespace in runner.py.)

## Task 2 — Fix false "recovered with vision" in run_plan (CRITICAL)

**Where:** `engine/runner.py` ~1937-1941.

**Problem:** the vision-fallback path calls `_run_direct_commands` without checking the
returned `StepResult.passed`; the `except e2` branch is unreachable. A failed recovery
reports `"{desc} — recovered with vision"`, marks the step ok=True, and can flip the whole
run to passed — a false pass in a UAT evidence tool. (The happy path at ~1921 checks
`sr.passed` correctly; the fallback forgot.)

**Fix:** capture the StepResult; `ok/note` reflect `sr.passed` (reuse `sr.error_message`
in the failure note). Delete the dead except.

**Accept when:** logic is symmetrical with line ~1921's check. Add a unit test if you can
reuse the Task 1 fake-Page fixture cheaply; otherwise a focused assertion in the same test
module is fine.

## Task 3 — Stop the coverage fallback claiming 100% (HIGH)

**Where:** `ui/server.py` `_coverage_for_scenario`, fallback block ~2379-2392.

**Problem:** when the AI matcher declines, a bag-of-words name-overlap fallback sets
`coverage = {st.step_id: True for all steps}` — full green for a mere name match.
`_compute_match_results` turns that into confidence 100 on the Run All board. This fabricates
the product's headline "more green" signal.

**Fix:** in the fallback, mark covered ONLY steps whose `step_id` is a key in the matched
task's saved `steps` dict (the exact-reuse case); every other step is a gap. Keep
`matched_task`/`reason` as-is (surfacing the suggestion is fine — inflating coverage is not).
Extract the fallback into a small pure function (e.g. `_fallback_name_match(scenario, tasks)`)
so it's unit-testable without mocking anthropic.

**Accept when:** a unit test feeds a scenario with 5 steps and a task named from the
scenario's words but with saved commands for 0 of its step_ids, and asserts covered_count
is 0 while matched_task is still suggested.

## Task 4 — One verifier policy (HIGH)

**Where:** `engine/coach.py:192` (`verify_step_result`, fail-OPEN) used at
`engine/runner.py:685` in `_run_step`'s vision path.

**Problem:** three verifiers, inconsistent failure policy. `verify_step_result` returns
True on any API error/missing key — so on an Anthropic outage, an AI-guessed step's
"did it work" check silently passes. The batch pipeline already uses the fail-closed
`verify_expected_result` (`ui/server.py:2857`); the scenario gate uses
`visual_verifier.verify_step` (inconclusive-doesn't-block, by design — leave it).

**Fix:** at `runner.py:685`, replace `verify_step_result` with
`coach.verify_expected_result`. Mapping: verdict `"pass"` → step stands; `"fail"` → mark
failed exactly as today; `"needs_review"` → do NOT hard-fail the step (the scenario-level
gate and human-approval flow catch it) but log it. Then delete `verify_step_result` from
coach.py and its import in runner.py — no other callers exist (verified by grep; re-verify).

**Accept when:** `verify_step_result` no longer exists anywhere; grep proves it; tests pass.

## Task 5 — Fix learned-data backup, broken in prod (MEDIUM)

**Where:** `ui/server.py` `_git_push_library` ~551-567, `_git_push_approved` ~603-624,
`_git_remote_with_token` ~583-600. Path context: `_DATA_ROOT = Path("/data") if exists
else ROOT` (line ~31); `LIBRARY_FILE = _DATA_ROOT / "storage" / "global" / "step_library.json"`.

**Problem:** on Railway (where `/data` exists), `LIBRARY_FILE.relative_to(ROOT)` raises
`ValueError`, the except swallows it, and the git backup has silently never worked in
production. The `/data` volume is the ONLY copy of the business asset.

**Fix:** replace the git-push mechanism in both functions with an S3 backup:
- Find the existing S3 upload code used for run videos (grep `boto3` / `S3_BUCKET` in the
  repo) and reuse its client/pattern.
- New `_backup_learned_data()`: upload `step_library.json`, plus the current client's
  `step_feedback.json` and `approved.json` (skip missing files) to
  `s3://$S3_BUCKET/backups/<CLIENT_ID>/<UTC timestamp>/<filename>`. Fail-soft with a
  clear log line; never crash a save.
- Call it where `_git_push_library` / `_git_push_approved` are called today (grep call
  sites; they run in daemon threads — keep that). Delete the two git functions and
  `_git_remote_with_token`.
- If no S3 env config is present, log one clear warning and skip.

This also removes the token-leak vector (raw `git push` stderr — which can echo the
token-injected remote URL — was printed to logs).

**Accept when:** no `_git_push_*`/`_git_remote_with_token` remain; `_backup_learned_data`
is fail-soft; a unit test with a stubbed S3 client asserts the right keys are uploaded.

## Task 6 — Wire engine/tools.py into the agent; kill JS generation (MEDIUM)

**Where:** `engine/coach.py` `get_agent_actions` (~299-458) and `get_vision_commands`
prompt (~144-155, lists `JS: javascript expression`); `engine/tools.py` (ready-made).

**Problem:** the agent brain returns free-text JSON — the fragility class behind commit
7973b29 ("Harden agent JSON parsing") and the 45-line `_extract_json_obj` workaround. Worse,
both prompts still offer `JS:` so the model can generate arbitrary JavaScript that
`page.evaluate` executes.

**Fix in `get_agent_actions`:**
- Pass `tools=ACTION_TOOLS + [ASK_USER_TOOL, TASK_COMPLETE_TOOL]` on the `messages.create`
  call. Single-turn: read the one response, do NOT continue the tool loop (no tool_result
  round-trip needed — the runner executes and re-screenshots next iteration).
- Build the return dict: `commands` = `serialize_tool_calls(msg.content)`;
  `ask` = the `ask_user` tool call's `question` input if present; `done` = True if a
  `task_complete` tool call is present; `reasoning` = concatenated text blocks (trimmed,
  same 240-char cap).
- Update the prompt: remove the "Reply with ONLY valid JSON" contract and the
  `Available commands` list (the tool schemas carry that now); keep ALL the behavioural
  rules (they're hard-won — proxy path, pencils, append-vs-replace, anti-repeat,
  ask-don't-guess, preview). Keep `CLICK_MARK` guidance wording aligned with the
  `click_mark` tool.
- Transition safety: if the response contains zero tool_use blocks but has text, fall back
  to the existing `_extract_json_obj` path so an odd reply doesn't stall a run.
- Remove `JS: expression` from BOTH prompts (`get_agent_actions` and
  `get_vision_commands`). The executor keeps replaying legacy `JS:` lines from storage —
  that's deliberate (see tools.py docstring); only generation stops. Also update
  `eval/dsl_check.py` ONLY if it would now flag stored JS — it must keep accepting stored
  JS lines (it checks the executor's DSL, which still supports JS).

**Accept when:** unit tests cover the response-parsing (fake `msg.content` with tool_use
dicts — `serialize_tool_calls` already accepts plain dicts, see
`tests/test_tools_serializer.py` for the pattern): commands serialization order, ask_user
extraction, task_complete → done, text-only JSON fallback. Grep shows no `JS:` in any
prompt string in coach.py.

## Task 7 — Route-level tests for the monolith (MEDIUM)

**Where:** new `tests/test_server_routes.py` using `fastapi.testclient.TestClient`.

Importing `ui.server` runs module-level setup — set required env (e.g. `CLIENT_ID=default`)
and leave auth env (`TESTOPS_PIN`/`TESTOPS_USERS`) unset in one test module so auth is
disabled, and set them in a separate module/fixture to test enforcement (note the module
fails fast on weak `TESTOPS_AUTH_TOKEN` when auth is enabled — set a strong one in tests).
Use `importlib.reload` or subprocess-per-module if module-level state fights you; keep it
pragmatic.

Minimum cases:
1. Auth OFF: `GET /api/match` with no script → graceful JSON error, not 500.
2. Auth ON: unauthenticated `GET /` → 303 to `/login`; unauthenticated `POST /api/...` → 401.
3. Auth ON: forged/garbage auth cookie → still 401/redirect.
4. `POST /api/lab/save` with no learned commands → 400 with the "No confirmed moves" error.
5. `GET /runs/<id>/../../secret` style traversal → 404 (route at ~900-911).
6. Role enforcement: a `viewer` cookie (sign one with the test AUTH_TOKEN via the server's
   own signing helper) hitting a write endpoint → 403.

**Accept when:** these run green in the normal `pytest tests/ -q` invocation without
network, browser, or API key.

## Task 8 — Pre-deploy guard

Add `eval.dsl_check` to the deploy path so a malformed library command can never ship:
simplest is a line in `Procfile`'s release phase if Railway supports it, else a
`scripts/predeploy.ps1` (or extend an existing check script) that runs py_compile on
`engine/ ui/ eval/` + `pytest -q` + `python -m eval.dsl_check`, and a note in this file's
Run section. Keep it dumb and fast.

---

## Explicitly OUT of scope (do not do)

- Splitting/refactoring `ui/server.py` beyond the exact functions named above.
- Any change to `run_scenario`'s trust-saved-commands policy (~383-399) — it's deliberate
  (commit 8c6ff72); Task 1 is what makes it safe.
- Renaming DSL verbs, changing stored-library schema non-additively, or removing legacy
  `JS:` EXECUTION support.
- Touching `visual_verifier.verify_step`'s inconclusive-doesn't-block behaviour.
- New dependencies unless boto3 is genuinely absent (check requirements.txt first).
- Deploying, or running anything against the live SF tenant.

## Definition of done (the follow-up audit will check exactly this)

1. All 8 tasks committed separately, each message `Audit fix N: ...` (Task 0 excepted).
2. `pytest tests/ -q` green (existing 25 + all new tests), `eval.dsl_check` green,
   py_compile clean on every touched file.
3. Greps come back empty: `verify_step_result`, `_git_push_library`, `JS:` inside prompt
   strings in coach.py.
4. `serialize_tool_calls` is reachable from `get_agent_actions` (tools actually wired).
5. No unrelated diffs — `git diff` per commit is scoped to its task.

Known follow-ups that are NOT yours: growing the library with real trained tasks, adding
eval golden screenshots, and live verification on the SF tenant — the user owns those.
