"""Prompt-evaluation pipeline for the vision agent (the course's eval workflow).

For each golden case (screenshot + step + known-good commands) it asks the live
vision model to produce commands, then grades them two ways:
  - code grader: verb-recall + target-token overlap vs the known-good commands
  - model grader: a Haiku call judging whether the commands accomplish the step
Prints a per-case table + average, and writes eval/report.html.

This NEVER touches the live runner, storage, or a browser — it only calls
engine.coach functions read-only and reads files under eval/dataset/.

Run:
  python -m eval.model_eval                         # default model (Opus 4.8)
  python -m eval.model_eval --model claude-fable-5  # compare the toggle
"""

import argparse
import base64
import json
import os
import re
import sys
from pathlib import Path
from statistics import mean

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

try:  # dotenv only needed to actually call the model; skip-path works without it
    from dotenv import load_dotenv
    load_dotenv(ROOT / ".env")
except ImportError:
    pass

from engine.coach import get_vision_commands  # noqa: E402

DATASET = Path(__file__).resolve().parent / "dataset"
VERB_RE = re.compile(r"^\s*([A-Z_]+)\s*:", re.MULTILINE)
WORD_RE = re.compile(r"[a-z0-9]+")


def _verbs(cmds: str) -> list[str]:
    return [v.upper() for v in VERB_RE.findall(cmds or "")]


def _arg_tokens(cmds: str) -> set[str]:
    out: set[str] = set()
    for line in (cmds or "").splitlines():
        if ":" in line:
            arg = line.partition(":")[2]
            out.update(WORD_RE.findall(arg.lower()))
    return out


def grade_code(predicted: str, known_good: str) -> float:
    """0-10 from how well predicted recalls the known-good verbs and targets."""
    if not predicted:
        return 0.0
    pred_v, known_v = _verbs(predicted), _verbs(known_good)
    if known_v:
        verb_recall = sum(1 for v in set(known_v) if v in pred_v) / len(set(known_v))
    else:
        verb_recall = 1.0
    pred_t, known_t = _arg_tokens(predicted), _arg_tokens(known_good)
    if known_t:
        target_jaccard = len(pred_t & known_t) / len(pred_t | known_t)
    else:
        target_jaccard = 1.0
    return round(10 * (0.5 * verb_recall + 0.5 * target_jaccard), 1)


def grade_model(screenshot: Path, action: str, expected: str, predicted: str) -> tuple[float, str]:
    """Ask Haiku whether the predicted commands accomplish the step. (score, reason)."""
    key = os.getenv("ANTHROPIC_API_KEY")
    if not key:
        return (0.0, "no API key for model grader")
    try:
        import anthropic
        client = anthropic.Anthropic(api_key=key)
        img = base64.standard_b64encode(screenshot.read_bytes()).decode()
        prompt = f"""You are grading a SAP SuccessFactors UI automation attempt.

STEP THE AGENT WAS ASKED TO DO: {action}
EXPECTED RESULT: {expected}

COMMANDS THE AGENT PRODUCED (CLICK/TYPE/FILL/etc):
{predicted or "(none)"}

Looking at the screenshot (the screen the agent saw) and the commands, score how
well the commands would accomplish the step. 10 = exactly right; 1 = wrong/unsafe.
Reply with ONLY JSON: {{"score": <1-10>, "reason": "<one short sentence>"}}"""
        msg = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=200,
            messages=[{"role": "user", "content": [
                {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": img}},
                {"type": "text", "text": prompt},
            ]}],
        )
        raw = msg.content[0].text.strip()
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            raw = raw[4:] if raw.startswith("json") else raw
        data = json.loads(raw.strip())
        return (float(data.get("score", 0)), str(data.get("reason", ""))[:200])
    except Exception as exc:
        return (0.0, f"grader error: {exc}")


def run(model: str, cases_path: Path, out_path: Path) -> int:
    cases = json.loads(cases_path.read_text(encoding="utf-8"))
    rows = []
    for case in cases:
        shot = DATASET / case["screenshot"]
        if not shot.exists():
            print(f"  SKIP {case['screenshot']} — screenshot not present (see eval/README.md)")
            continue
        predicted = get_vision_commands(
            str(shot), case["action"], case.get("expected", ""),
            case.get("test_data", ""), case.get("scenario_context", ""),
        ) or ""
        code = grade_code(predicted, case.get("known_good", ""))
        mscore, reason = grade_model(shot, case["action"], case.get("expected", ""), predicted)
        final = round(mean([code, mscore]), 1) if mscore else code
        rows.append({"action": case["action"], "predicted": predicted,
                     "code": code, "model": mscore, "final": final, "reason": reason})
        print(f"  [{final:>4}] code={code} model={mscore}  {case['action'][:60]}")

    if not rows:
        print("\nNo runnable cases (no screenshots present). Add some — see eval/README.md.")
        return 0

    avg = round(mean(r["final"] for r in rows), 2)
    print(f"\nModel: {model}   Cases: {len(rows)}   Average score: {avg}/10")
    _write_report(out_path, model, avg, rows)
    print(f"Report: {out_path}")
    return 0


def _write_report(out_path: Path, model: str, avg: float, rows: list) -> None:
    def esc(s):
        return (str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
                .replace("\n", "<br>"))
    body = "".join(
        f"<tr><td>{r['final']}</td><td>{r['code']}</td><td>{r['model']}</td>"
        f"<td>{esc(r['action'])}</td><td><code>{esc(r['predicted'])}</code></td>"
        f"<td>{esc(r['reason'])}</td></tr>"
        for r in rows
    )
    out_path.write_text(f"""<!doctype html><meta charset=utf-8>
<title>EX3 eval — {esc(model)}</title>
<style>body{{font:14px system-ui;margin:24px}}table{{border-collapse:collapse;width:100%}}
td,th{{border:1px solid #ccc;padding:6px;text-align:left;vertical-align:top}}
code{{font:12px monospace;white-space:pre-wrap}}</style>
<h1>EX3 vision eval</h1><p>Model <b>{esc(model)}</b> — average <b>{avg}/10</b> over {len(rows)} cases.</p>
<table><tr><th>final</th><th>code</th><th>model</th><th>step</th><th>predicted commands</th><th>grader reason</th></tr>
{body}</table>""", encoding="utf-8")


def main() -> int:
    ap = argparse.ArgumentParser(description="EX3 vision-agent prompt eval")
    ap.add_argument("--model", default="claude-opus-4-8",
                    help="agent-brain model id (e.g. claude-opus-4-8, claude-fable-5)")
    ap.add_argument("--cases", default=str(DATASET / "cases.json"))
    ap.add_argument("--out", default=str(Path(__file__).resolve().parent / "report.html"))
    args = ap.parse_args()
    if not os.getenv("ANTHROPIC_API_KEY"):
        print("Warning: ANTHROPIC_API_KEY not set — the vision model can't be called.")
    return run(args.model, Path(args.cases), Path(args.out))


if __name__ == "__main__":
    sys.exit(main())
