# EX3 TestOps — Handover

Written 2026-08-24 for whoever inherits this from Louie. This is the practical
"how do I actually run/own this" doc. For the codebase architecture and the
last engineering audit, see `HANDOVER.md`.

## What this is

Self-teaching UAT automation for SAP SuccessFactors. You upload an Excel test
workbook, it matches scenarios against a learned library of recorded steps,
replays known steps automatically, and asks a human to take over (or attempts
an AI-vision guess) for anything it hasn't seen before. Every approved run
produces video/screenshot evidence for UAT sign-off. The learned library
(`storage/global/step_library.json`) is the product's core asset — it's what
makes runs get faster and more reliable over time.

## Where things live

- **Live app**: https://ex3-testops-next-production.up.railway.app
- **Code**: `github.com/weareex3/unit-testing` (company-owned org, not a
  personal account — this is fine, no action needed there)
- **Hosting**: Railway, project `ex3-testops-next`
- **Code on this laptop**:
  `Documents/Codex/2026-05-27/whats-my-usage-limits/ex3-testops-next`

## ⚠ Do this first: move the Railway project

As of writing, the Railway project sits under a **personal** workspace
("louiebond1's Projects"), not a company Railway team. If that account gets
closed or loses access, whoever's left cannot deploy, view logs, or change
config — regardless of how good the code is. Before Louie's last day:

1. In the Railway dashboard, transfer the `ex3-testops-next` project to a
   company/team workspace (Settings → Transfer Project), or
2. If a transfer isn't possible in time, create a new Railway project under
   the company account and redeploy from the GitHub repo (`railway link` then
   `railway up`, or connect the GitHub repo directly in the Railway UI for
   auto-deploy on push).

Also confirm who owns the API keys funding this:
- `ANTHROPIC_API_KEY` — should be on a company billing account, not personal.
- `AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY` — same check, these back the
  S3 bucket that stores run evidence and library backups.

If either is personal, rotate to company-owned keys and update the Railway
variables before handover — otherwise the app silently breaks (AI matching /
vision fallback, or evidence backup) the day that account is closed.

## Logging in / managing users

Login is at `/login`. As of this handover, three people have been invited
(Lewis, Anil, Andy) via the Admin > Users > Invite flow — each got a one-time
link to set their own password. That's the right way to add anyone else too:

1. Log in with an account that has `admin` or `owner` role.
2. Go to **Admin → Users → Invite**.
3. Enter their email, name, and role. This generates a `/set-password` link
   (shown on screen, not auto-emailed — there's no email provider wired up
   yet, so you copy/paste the link to them yourself, e.g. via Slack or email).
4. They open the link and set their own password. Done.

Roles, low to high: `viewer` (read-only) → `tester` → `lead` → `admin` →
`owner`. Give at least one successor `owner` so they aren't blocked later.

There's also a single fallback login baked into the code: username `louie`,
password whatever `TESTOPS_PIN` is set to in Railway. This exists so there's
always *a* way in even if the users file is wiped. Once real named accounts
with `owner`/`admin` roles are set up and confirmed working, it's worth
clearing `TESTOPS_PIN` in Railway so there's no login still tied to Louie's
name.

## Environment variables

See `.env.example` in this repo — it's fully documented and current as of
this handover (previously it was missing the auth variables entirely).

Two categories:
- **Client config**: `CLIENT_ID`, `SF_URL`, `SF_USERNAME`, `SF_PASSWORD` — the
  SuccessFactors tenant this instance tests against.
- **Infra**: `ANTHROPIC_API_KEY` (AI matching/vision/agent), `AWS_*` +
  `S3_BUCKET` (evidence storage + learned-library backup), `TESTOPS_*` /
  `WEBAUTHN_*` (auth — see above).

To see the actual live values, check Railway → the service → Variables tab.
(Nothing sensitive is committed to git — confirmed clean.)

## Day-to-day operation

- Upload a workbook, it parses into scenarios, shows match confidence against
  the learned library, run it.
- Steps the library already knows replay automatically. Unknown steps either
  get an AI-vision attempt or hand control to a human, who does it once and
  the tool learns the commands for next time (via the Testing Hub / agent
  flow) — that's how the library grows.
- Runs produce video + screenshots as evidence, stored to S3 and linked from
  the run page.

## Before deploying a code change

Always run `predeploy.ps1` first (or the app itself won't start if something's
broken — this isn't a suggestion, it's the same gate CI would run):

```powershell
.\predeploy.ps1
```

This compiles everything, runs the 58 unit tests, and checks the learned
library still parses correctly. If it prints "predeploy OK", then:

```powershell
railway up
```

deploys straight to production (there's no staging environment). Watch
`railway logs` after deploying to confirm it booted cleanly.

## State of the code (as of this handover)

Good shape. A full audit (`HANDOVER.md`) found and fixed 8 real issues in
early July 2026 — things like failed commands silently poisoning the learned
library, a coverage check that could falsely claim 100%, and a broken backup
mechanism for the library's only copy. All 8 are fixed, verified, and
deployed to production (confirmed current as of 2026-08-24). Two more
hardening passes happened after that. 58 tests pass, nothing uncommitted of
substance.

Known non-blocking rough edges (not urgent, just worth knowing):
- A handful of stored commands in the library use coordinate-based clicks
  (`CLICK_XY`) or legacy inline JavaScript — both still work, but are more
  fragile to SF layout changes than the newer text-based commands. Retrain
  these opportunistically when you happen to hit them in a run.
- No automated email sending yet for user invites — links are generated but
  must be sent manually (see "Logging in" above).

## Who to ask

If something in `engine/` or `ui/server.py` doesn't make sense, `HANDOVER.md`
has file/line-level architecture notes from the last deep audit — read that
before assuming something's broken; a lot of behavior that looks odd at first
(e.g. "why doesn't a failed step raise an exception") is deliberate and
explained there.
