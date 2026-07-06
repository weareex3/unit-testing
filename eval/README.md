# EX3 eval harness

Offline quality checks for the AI layer. **Never imported by the app** — these
only call `engine.coach` read-only and read files under `eval/dataset/`. Nothing
here can affect a live run, storage, or production.

Run everything with the project venv:

```
venv/Scripts/python.exe -m eval.dsl_check
venv/Scripts/python.exe -m eval.model_eval --model claude-opus-4-8
```

## 1. dsl_check — command-library regression guard (free, instant)

Parses every stored command in `storage/global/step_library.json`,
`storage/*/step_feedback.json`, and `storage/*/approved.json` against the exact
DSL the runner executes (`_run_direct_commands` in `engine/runner.py`). Flags
unknown verbs, malformed `CLICK_XY`/`CLICK_MARK`/`WAIT`, and `FILL`/`SELECT_OPTION`
missing their `|`. No API key, no cost. Exits non-zero on any malformed command,
so it works as a pre-deploy / CI check.

## 2. model_eval — vision-agent scoring (needs API key + screenshots)

For each golden case it asks the live vision model for commands, then grades them:
- **code grader** — verb recall + target-token overlap vs the known-good commands.
- **model grader** — a Haiku call judging whether the commands accomplish the step.

Prints a per-case table + average and writes `eval/report.html`. Use `--model` to
compare the Testing Hub's Opus-vs-Fable toggle objectively:

```
venv/Scripts/python.exe -m eval.model_eval --model claude-opus-4-8
venv/Scripts/python.exe -m eval.model_eval --model claude-fable-5
```

### Adding golden cases

`dataset/cases.json` is a list of:

```json
{
  "screenshot": "mystep_01.png",
  "action": "what the step should do",
  "expected": "the expected result",
  "test_data": "",
  "scenario_context": "Scenario: ... | Role: ... | Module: ...",
  "known_good": "CLICK: Proxy Now\nWAIT: 3000"
}
```

`screenshot` is a PNG you drop into `eval/dataset/`. The best source is a real
`*_pre.png` from a run on the Railway `/data` volume (`/data/runs/<run_id>/`) — it
is the exact screen the agent saw. `known_good` is the command string that actually
worked for that step (copy it from `step_library.json` / `step_feedback.json`).

Cases whose screenshot is missing are **skipped** with a note, so the harness runs
out of the box; it becomes meaningful once you add 3-5 real screenshots.
