"""EX3 site acceptance harness — repeatable testing of the TestOps WEBSITE.

Three layers, each runnable on its own:

  ingest    Offline. Parse a workbook through the real parser and report what the
            site would ingest (scenario/step counts, duplicates, empty/edge cases).
            No site, no SF, no cost.

  delivery  Against a RUNNING local site. Log in, upload the workbook, and verify
            the site delivers every scenario through its own endpoints (batch plan,
            scenario pages, coverage). Catches the dup-scenario bug.

  live      Drive ONE scenario's agent against real SF with precision clicking and
            report the evidence (CLICK_MARK usage, steps, target found). Needs SF
            creds + API key — run under `railway run`.

Examples:
  python -m eval.site_test ingest scripts/EX3_EC_Sample5_V1.xlsx
  python -m eval.site_test delivery EX3_EC_Sample5_V1.xlsx --base http://127.0.0.1:8100
  railway run <venv-python> -m eval.site_test live scripts/EX3_EC_Sample5_V1.xlsx EC-SAMP-401 --answer "Esther Loh"

Boot a test site first with:  python -m eval.site_launch   (under `railway run` for live creds)
"""
import argparse
import http.cookiejar
import json
import sys
import urllib.parse
import urllib.request
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

OK, BAD = "[ OK ]", "[FAIL]"


# ───────────────────────────── ingest (offline) ─────────────────────────────
def cmd_ingest(args) -> int:
    from engine.parser import parse_workbook
    scs = parse_workbook(args.workbook)
    ids = [s.scenario_id for s in scs]
    dupes = [i for i, n in Counter(ids).items() if n > 1]
    empty = [s.scenario_id for s in scs if not s.steps]
    no_role = [s.scenario_id for s in scs if not s.role]
    total_steps = sum(len(s.steps) for s in scs)

    print(f"workbook : {args.workbook}")
    print(f"scenarios: {len(scs)}  ({len(set(ids))} unique)")
    print(f"steps    : {total_steps}")
    print(f"step dist: {dict(sorted(Counter(len(s.steps) for s in scs).items()))}")
    print(f"roles    : {sorted({s.role for s in scs})}")
    fails = 0
    for label, bad in (("duplicate scenario IDs", dupes), ("empty scenarios", empty),
                       ("scenarios missing a role", no_role)):
        if bad:
            print(f"{BAD} {label}: {bad}"); fails += 1
        else:
            print(f"{OK} no {label}")
    return 1 if fails else 0


# ──────────────────────────── delivery (HTTP) ───────────────────────────────
class _LaxPolicy(http.cookiejar.DefaultCookiePolicy):
    def return_ok_secure(self, cookie, request):  # send Secure cookie over http://localhost
        return True


def _session(base, user, pw):
    cj = http.cookiejar.CookieJar(policy=_LaxPolicy())
    op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj))
    body = urllib.parse.urlencode({"username": user, "password": pw}).encode()
    op.open(urllib.request.Request(base + "/login", data=body,
            headers={"Content-Type": "application/x-www-form-urlencoded"}), timeout=20).read()
    return op


def _get(op, url):
    return op.open(url, timeout=30)


def cmd_delivery(args) -> int:
    base = args.base.rstrip("/")
    op = _session(base, args.user, args.password)
    script = Path(args.workbook).name
    fails = 0

    # upload (if a local path is given, push it through the real upload route)
    wb_path = ROOT / args.workbook if not Path(args.workbook).is_absolute() else Path(args.workbook)
    if wb_path.exists():
        import uuid
        boundary = uuid.uuid4().hex
        data = wb_path.read_bytes()
        payload = (f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; "
                   f"filename=\"{script}\"\r\nContent-Type: application/vnd.openxmlformats-"
                   f"officedocument.spreadsheetml.sheet\r\n\r\n").encode() + data + f"\r\n--{boundary}--\r\n".encode()
        try:
            op.open(urllib.request.Request(base + "/scripts/upload", data=payload,
                    headers={"Content-Type": f"multipart/form-data; boundary={boundary}"}), timeout=30).read()
            print(f"{OK} upload accepted ({script})")
        except Exception as e:
            print(f"{BAD} upload: {e}"); fails += 1

    # delivery: does the site surface every scenario, with no duplicates?
    try:
        plan = json.loads(_get(op, f"{base}/api/batch/plan?script={urllib.parse.quote(script)}&refresh=1").read())
        items = plan.get("items", [])
        ids = [it.get("scenario_id") for it in items]
        if items and len(ids) == len(set(ids)):
            print(f"{OK} site delivers {len(items)} scenarios, no duplicates")
        else:
            print(f"{BAD} delivery: {len(items)} items / {len(set(ids))} unique (duplication bug?)"); fails += 1
        first = ids[0] if ids else None
    except Exception as e:
        print(f"{BAD} batch plan: {e}"); fails += 1; first = None

    # render + coverage for the first scenario
    if first:
        for label, url in (("scenario page renders", f"{base}/scenario/{first}?script={urllib.parse.quote(script)}"),
                           ("coverage endpoint", f"{base}/api/coverage/{first}?script={urllib.parse.quote(script)}")):
            try:
                code = _get(op, url).getcode()
                print(f"{OK} {label} (HTTP {code})" if code == 200 else f"{BAD} {label} (HTTP {code})")
                fails += code != 200
            except Exception as e:
                print(f"{BAD} {label}: {e}"); fails += 1
    return 1 if fails else 0


# ────────────────────────── live (precision agent) ──────────────────────────
def cmd_live(args) -> int:
    from engine.parser import parse_workbook
    from engine.runner import run_agent_goal
    from engine.context_extractor import substitute

    scs = parse_workbook(args.workbook)
    scen = next((s for s in scs if s.scenario_id == args.scenario), None)
    if not scen:
        print(f"{BAD} scenario {args.scenario} not found in {args.workbook}"); return 1

    parts = []
    for st in scen.steps:
        line = st.action + (f" (data: {st.test_data})" if st.test_data and st.test_data != "—" else "")
        parts.append(line)
    goal = f"{scen.name}. " + " Then ".join(parts) + " Do not save."
    if args.answer:
        goal = substitute(goal, {"target_employee_name": args.answer})

    print(f"scenario : {scen.scenario_id} — {scen.name}")
    print(f"goal     : {goal[:140]}...")
    print(f"target   : {args.answer or '(none)'}\n")

    commands = []

    def _done(step_id, passed, error, shot_url):
        print(f"  {step_id} | {'OK' if passed else 'x'} | {str(error)[:90]}")

    res = run_agent_goal(goal, preview=True, runs_root="runs", max_iters=args.max_iters,
                         step_done_callback=_done, ask_user=lambda q, s: args.answer or None,
                         confirm_step=None, grounding=True)
    cmds = list(getattr(res, "agent_commands", []) or [])
    mark = [c for c in cmds if "CLICK_MARK" in c]
    print(f"\n=== precision evidence ===")
    print(f"run_id        : {res.run_id}")
    print(f"steps taken   : {len(res.steps)}")
    print(f"commands      : {len(cmds)}")
    print(f"{OK if mark else BAD} CLICK_MARK (precision) commands: {len(mark)}/{len(cmds)}")
    for c in cmds:
        print(f"   {c[:70]}")
    print(f"screenshots   : runs/{res.run_id}/")
    return 0 if mark else 1


def main() -> int:
    ap = argparse.ArgumentParser(prog="site_test", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("ingest", help="offline parser/ingestion check")
    p.add_argument("workbook")
    p.set_defaults(fn=cmd_ingest)

    p = sub.add_parser("delivery", help="verify a running site delivers the workbook")
    p.add_argument("workbook")
    p.add_argument("--base", default="http://127.0.0.1:8100")
    p.add_argument("--user", default="louie")
    p.add_argument("--password", default="testpin123")
    p.set_defaults(fn=cmd_delivery)

    p = sub.add_parser("live", help="drive one scenario's precision agent against SF")
    p.add_argument("workbook")
    p.add_argument("scenario")
    p.add_argument("--answer", default="", help="value for {{target_employee_name}}")
    p.add_argument("--max-iters", type=int, default=12)
    p.set_defaults(fn=cmd_live)

    args = ap.parse_args()
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
