# Site acceptance harness

A repeatable way to test the TestOps **website** (the middle-man between a user and
SuccessFactors) — from script ingestion all the way to live precision execution.
Nothing here is imported by the app; it only drives the site from the outside.

All commands run with the project venv (`venv/Scripts/python.exe`).

## The three layers

### 1. `ingest` — offline, free, instant
Parses a workbook through the site's real parser and reports exactly what the site
would ingest. Catches duplicate scenario IDs, empty scenarios, missing roles.
```
python -m eval.site_test ingest scripts/EX3_EC_Sample5_V1.xlsx
```

### 2. `delivery` — against a running site
Logs in, uploads the workbook, and verifies the **site** surfaces every scenario via
its own endpoints (batch plan, scenario pages, coverage) with no duplicates.
First boot a test site (see below), then:
```
python -m eval.site_test delivery EX3_EC_Sample5_V1.xlsx --base http://127.0.0.1:8100
```

### 3. `live` — drive one scenario's precision agent against real SF
Builds the goal from a scenario's steps, runs the agent with **precision clicking
(set-of-marks / CLICK_MARK)**, and reports the evidence: how many commands used
CLICK_MARK, the steps taken, and where the screenshots are. Needs SF creds + API
key, so run it under `railway run`:
```
railway run "C:\...\venv\Scripts\python.exe" -m eval.site_test live scripts/EX3_EC_Sample5_V1.xlsx EC-SAMP-401 --answer "Esther Loh"
```
Use a real tenant employee for `--answer` so data-dependent steps find their target.
Runs in preview (no-save) by design.

## Booting a test site (`site_launch`)
Boots the app on :8100 with real SF creds (from Railway) but a throwaway local login
and a `sitetest` client id, so test runs never touch production data:
```
railway run "C:\...\venv\Scripts\python.exe" -m eval.site_launch
# log in at http://127.0.0.1:8100 with louie / testpin123
```

## Generating test scripts
```
python -m eval.gen_ec_sample5    # 5 scripts x 5 steps, real users (Esther Loh, Alex Brackley, you)
python -m eval.gen_ec_scripts    # ~48 EC scenarios + parser edge cases (stress test)
```

## What this method has already established
- Ingestion/delivery: solid — 48-scenario stress set incl. unicode/edge cases, no parser failures.
- Live pipeline: site → real SF login → vision agent → real screenshots, working.
- Precision clicking: verified live (CLICK_MARK on every move; 94 marks found on the SF home).
- Cache quirk (NOT a confirmed bug): the batch plan was once seen returning doubled
  scenarios with `cache_hit: true`; a `refresh=1` request returns the correct unique
  set. With both a canonical and an uploaded copy present, delivery still reports 5
  unique — so the earlier "duplication" was a stale-cache artifact, not a dir merge.
- Real finding: the agent loop swallows screenshot/grounding exceptions silently
  (`except: shot=""`), which made a transient batch failure very hard to diagnose.
  Worth surfacing those errors to the run log.
