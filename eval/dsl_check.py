"""Offline grader for the learned-command library.

Parses every stored command string against the same DSL the runner's
_run_direct_commands actually executes, and flags anything malformed. No API
calls, no browser, no cost — safe to run anywhere and use as a regression guard.

Run:  python -m eval.dsl_check
Exits non-zero if any command is malformed.
"""

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
STORAGE = ROOT / "storage"

# The verbs _run_direct_commands (engine/runner.py) actually handles. This is the
# source of truth for "valid" — NOT _CMD_PREFIXES, which omits CLICK_MARK.
KNOWN_VERBS = {
    "CLICK", "SHADOW_CLICK", "CLICK_XY", "CLICK_MARK", "TYPE", "PRESS",
    "WAIT", "FILL", "GOTO", "NAVIGATE", "SELECT", "SELECT_OPTION", "JS",
}
NEED_PIPE = {"FILL", "SELECT_OPTION"}
NEED_TEXT = {"CLICK", "SHADOW_CLICK", "TYPE", "PRESS", "SELECT", "NAVIGATE", "JS"}
TOKEN_RE = re.compile(r"\{\{\s*([\w]+)\s*\}\}")


def _check_line(line: str) -> str | None:
    """Return an error string if the command line is malformed, else None.

    Mirrors _run_direct_commands: blank lines, '#' comments, and lines with no
    ':' are skipped by the executor, so they're never errors here.
    """
    line = line.strip()
    if not line or line.startswith("#") or ":" not in line:
        return None
    cmd, _, arg = line.partition(":")
    cmd = cmd.strip().upper()
    arg = arg.strip()

    if cmd not in KNOWN_VERBS:
        return f"unknown verb '{cmd}'"
    if cmd == "CLICK_XY":
        parts = [p.strip() for p in arg.split(",")]
        if len(parts) != 2 or not all(_is_float(p) for p in parts):
            return f"CLICK_XY expects 'x, y' floats, got '{arg}'"
    elif cmd == "CLICK_MARK":
        num = arg.partition("|")[0].strip()
        if not num.isdigit():
            return f"CLICK_MARK expects a number first, got '{arg}'"
    elif cmd == "WAIT":
        if not arg.isdigit():
            return f"WAIT expects an integer, got '{arg}'"
    elif cmd in NEED_PIPE:
        if "|" not in arg:
            return f"{cmd} expects 'label | value', missing '|' in '{arg}'"
    elif cmd in NEED_TEXT:
        # A {{token}} resolves at runtime, so a token-only arg is fine.
        if not arg:
            return f"{cmd} has an empty argument"
    return None


def _is_float(s: str) -> bool:
    try:
        float(s)
        return True
    except ValueError:
        return False


_DATE_RE = re.compile(r"\b\d{1,2}[/.-]\d{1,2}[/.-]\d{2,4}\b")
_ID_RE = re.compile(r"\b\d{5,}\b")


def _warn_line(line: str) -> str | None:
    """Return a non-blocking warning for commands that replay but are fragile or
    embed instance-specific data. These don't fail the check — they tell you
    which stored tasks are worth retraining to durable form."""
    line = line.strip()
    if not line or line.startswith("#") or ":" not in line:
        return None
    cmd, _, arg = line.partition(":")
    cmd = cmd.strip().upper()
    arg = arg.strip()

    if cmd == "CLICK_XY" or (cmd == "CLICK_MARK" and "|" in arg):
        return "coordinate click — session-bound; can silently misclick after an SF layout change"
    if cmd == "JS":
        return "legacy JS line — still replays, but can no longer be generated; retrain to durable commands"
    if cmd in ("TYPE", "FILL"):
        value = arg.rpartition("|")[2].strip() if cmd == "FILL" else arg
        if TOKEN_RE.search(value):
            return None
        if _DATE_RE.search(value):
            return "literal date — will go stale; use {{today}} or a placeholder"
        if _ID_RE.search(value):
            return "long numeric literal — looks instance-specific; consider a {{placeholder}}"
    return None


def _iter_commands():
    """Yield (source, key, command_string) from every stored command map."""
    lib = STORAGE / "global" / "step_library.json"
    if lib.exists():
        data = json.loads(lib.read_text(encoding="utf-8"))
        for task, entry in (data.items() if isinstance(data, dict) else []):
            steps = entry.get("steps", {}) if isinstance(entry, dict) else {}
            for step_id, cmds in steps.items():
                yield f"library:{task}", step_id, cmds

    # step_feedback.json/approved.json are now bucketed {workbook_key: {scenario_id:
    # ...}} to stop two unrelated scripts sharing a Script ID from replaying each
    # other's saved commands (see ui/server.py's _load_bucketed). A file written
    # before that scoping existed is still flat ({scenario_id: ...}) - detect which
    # shape we're looking at the same way the app does, so this linter keeps
    # checking every stored command instead of silently misreading workbook keys
    # as scenario ids once real bucketed data exists.
    def _is_legacy_flat_step_map(raw):
        return any(
            isinstance(v, dict) and v and all(isinstance(inner, dict) is False for inner in v.values())
            for v in raw.values()
        )

    def _is_legacy_flat_approved_map(raw):
        return any(isinstance(v, dict) and "step_commands" in v for v in raw.values())

    for fb in STORAGE.glob("*/step_feedback.json"):
        raw = json.loads(fb.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            continue
        buckets = {"": raw} if _is_legacy_flat_step_map(raw) else raw
        for workbook, data in buckets.items():
            for scenario, steps in (data.items() if isinstance(data, dict) else []):
                if isinstance(steps, dict):
                    for step_id, cmds in steps.items():
                        label = f"{workbook}/{scenario}" if workbook else scenario
                        yield f"feedback:{fb.parent.name}/{label}", step_id, cmds

    for ap in STORAGE.glob("*/approved.json"):
        raw = json.loads(ap.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            continue
        buckets = {"": raw} if _is_legacy_flat_approved_map(raw) else raw
        for workbook, data in buckets.items():
            for scenario, entry in (data.items() if isinstance(data, dict) else []):
                steps = entry.get("step_commands", {}) if isinstance(entry, dict) else {}
                for step_id, cmds in steps.items():
                    label = f"{workbook}/{scenario}" if workbook else scenario
                    yield f"approved:{ap.parent.name}/{label}", step_id, cmds


def main() -> int:
    errors: list[str] = []
    warnings: list[str] = []
    tokens: set[str] = set()
    n_steps = 0
    n_lines = 0

    for source, step_id, cmds in _iter_commands():
        if not isinstance(cmds, str):
            errors.append(f"[{source}] {step_id}: command value is not a string")
            continue
        n_steps += 1
        tokens.update(TOKEN_RE.findall(cmds))
        for line in cmds.splitlines():
            stripped = line.strip()
            if stripped and not stripped.startswith("#") and ":" in stripped:
                n_lines += 1
            err = _check_line(line)
            if err:
                errors.append(f"[{source}] {step_id}: {err}  ::  {line.strip()[:80]}")
            else:
                warn = _warn_line(line)
                if warn:
                    warnings.append(f"[{source}] {step_id}: {warn}  ::  {line.strip()[:80]}")

    print(f"Checked {n_steps} stored steps, {n_lines} command lines.")
    if tokens:
        print(f"Runtime tokens in use (filled at run time): {', '.join(sorted(tokens))}")

    if warnings:
        print(f"\n{len(warnings)} fragility warning(s) (non-blocking — retrain these when convenient):")
        for w in warnings:
            print(f"  ~ {w}")

    if errors:
        print(f"\n{len(errors)} malformed command(s):")
        for e in errors:
            print(f"  - {e}")
        return 1
    print("All stored commands parse cleanly against the runner DSL.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
