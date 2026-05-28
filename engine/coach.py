"""AI coach — uses Claude vision to guide the runner past failures.

Successful coaching patterns are saved to storage/global/sf_notes.md
so every new client deployment starts with accumulated SF knowledge.
"""

import base64
import json
import os
from datetime import datetime
from pathlib import Path

_APP_ROOT = Path(__file__).resolve().parent.parent
# Learned SF navigation knowledge lives on the /data volume when present so it
# survives redeploys; falls back to the app dir for local runs.
_STORAGE_ROOT = (Path("/data") if Path("/data").exists() else _APP_ROOT) / "storage"
_GLOBAL_NOTES = _STORAGE_ROOT / "global" / "sf_notes.md"


def _load_global_notes() -> str:
    if _GLOBAL_NOTES.exists():
        return _GLOBAL_NOTES.read_text(encoding="utf-8")
    return ""


def save_successful_pattern(step_action: str, feedback: str, guidance: dict) -> None:
    """Append a successful coaching result to the global SF knowledge base."""
    _GLOBAL_NOTES.parent.mkdir(parents=True, exist_ok=True)
    approach = guidance.get("approach", "unknown")
    notes = guidance.get("notes", "")
    entry = (
        f"\n### {datetime.utcnow().date()} — {step_action[:80]}\n"
        f"- **Feedback given:** {feedback}\n"
        f"- **Solution:** `{approach}` — {notes}\n"
        f"- **Full guidance:** `{json.dumps(guidance)}`\n"
    )
    with open(_GLOBAL_NOTES, "a", encoding="utf-8") as f:
        f.write(entry)


def get_vision_commands(
    screenshot_path: str,
    step_action: str,
    step_expected: str,
    step_data: str = "",
    scenario_context: str = "",
) -> str | None:
    """Primary vision step: look at the screen and return the exact command sequence.

    Called BEFORE keyword dispatch so Claude sees the real page and decides
    what to do, rather than guessing from text keywords.

    Returns a commands string (CLICK:, CLICK_XY:, TYPE:, WAIT:, etc.) or
    None if no API key / screenshot missing (falls back to keyword dispatch).
    """
    key = os.getenv("ANTHROPIC_API_KEY")
    if not key:
        return None
    shot = Path(screenshot_path)
    if not shot.exists():
        return None

    global_notes = _load_global_notes()
    notes_section = (
        f"\n\nAccumulated SF navigation knowledge:\n{global_notes}"
        if global_notes else ""
    )

    try:
        import anthropic
        client = anthropic.Anthropic(api_key=key)
        img_data = base64.standard_b64encode(shot.read_bytes()).decode()

        prompt = f"""You are an expert SAP SuccessFactors automation engineer controlling a real browser (1280x720).

{scenario_context}

Current step to execute:
  Action: {step_action}
  Test data: {step_data or '—'}
  Expected result: {step_expected}
{notes_section}

Look at the screenshot carefully. It shows the CURRENT state of the browser right now.

CRITICAL RULES:
- You must generate ALL commands needed to FULLY complete this step end-to-end.
- Do NOT stop halfway. If the step says click a card, then click Actions, then click Copy Position — generate ALL of those commands.
- The step is only complete when the EXPECTED RESULT is achieved: "{step_expected}"
- If a popup or panel is open, work through it completely — open menus, click options, confirm dialogs.
- If a button is visible at a specific pixel location, use CLICK_XY with exact coordinates from the screenshot.
- Add WAIT: 1500 after any click that opens a menu, dialog, or triggers navigation.
- Only output WAIT: 500 alone if this step is genuinely observation-only (no UI action whatsoever).
- Never mark a step done until the expected result would actually be visible on screen.

Available commands (one per line):
  CLICK: visible button or link text
  CLICK_XY: x, y
  TYPE: text to type
  PRESS: Key (Enter, ArrowDown, Tab, Escape)
  WAIT: milliseconds
  FILL: field label | value
  SHADOW_CLICK: text in shadow DOM
  NAVIGATE: Module Name
  JS: javascript expression

Output ONLY commands. No explanation, no markdown, no blank lines between commands."""

        msg = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=512,
            system=(
                "You are an expert in SAP SuccessFactors UI automation. You have deep knowledge of "
                "SuccessFactors navigation patterns, shadow DOM structure, popup behaviours, and the "
                "Position Org Chart, Recruiting, and Compensation modules. When shown a screenshot, "
                "you identify exactly what is on screen and generate precise, working commands."
            ),
            messages=[{
                "role": "user",
                "content": [
                    {
                        "type": "image",
                        "source": {"type": "base64", "media_type": "image/png", "data": img_data},
                    },
                    {"type": "text", "text": prompt},
                ],
            }],
        )

        raw = msg.content[0].text.strip()
        # Strip any accidental markdown fences
        if raw.startswith("```"):
            raw = "\n".join(raw.split("\n")[1:])
            if raw.endswith("```"):
                raw = raw[:-3].strip()
        print(f"  [vision] commands: {raw[:120]}")
        return raw if raw else None

    except Exception as exc:
        print(f"  [vision] error: {exc}")
        return None


def verify_step_result(screenshot_path: str, step_expected: str) -> bool:
    """Ask Claude to check if the expected result is actually visible on screen.

    Returns True if the expected result is achieved, False if not.
    Falls back to True (don't block) if no API key or screenshot missing.
    """
    key = os.getenv("ANTHROPIC_API_KEY")
    if not key:
        return True
    shot = Path(screenshot_path)
    if not shot.exists():
        return True

    try:
        import anthropic
        client = anthropic.Anthropic(api_key=key)
        img_data = base64.standard_b64encode(shot.read_bytes()).decode()

        msg = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=50,
            messages=[{
                "role": "user",
                "content": [
                    {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": img_data}},
                    {"type": "text", "text": (
                        f"Look at this screenshot of SAP SuccessFactors.\n"
                        f"Expected result: {step_expected}\n\n"
                        f"Does the screenshot show this expected result has been achieved? "
                        f"Answer only YES or NO."
                    )},
                ],
            }],
        )
        answer = msg.content[0].text.strip().upper()
        print(f"  [verify] expected='{step_expected[:60]}' → {answer}")
        return answer.startswith("YES")

    except Exception as exc:
        print(f"  [verify] error: {exc} — defaulting to pass")
        return True


def get_step_guidance(screenshot_path: str, step_action: str, step_expected: str, feedback: str) -> dict | None:
    """Legacy retry coach — used when a step has already failed once.

    Returns a single structured action dict. Kept for backwards compatibility
    with the retry loop in runner.py.
    """
    key = os.getenv("ANTHROPIC_API_KEY")
    if not key:
        return None
    if not Path(screenshot_path).exists():
        return None

    global_notes = _load_global_notes()
    notes_section = f"\n\nPrevious SF navigation learnings:\n{global_notes}" if global_notes else ""

    try:
        import anthropic
        client = anthropic.Anthropic(api_key=key)
        img_data = base64.standard_b64encode(Path(screenshot_path).read_bytes()).decode()

        prompt = f"""You are helping an automated Playwright test runner navigate SAP SuccessFactors.

Step action: {step_action}
Expected result: {step_expected}
Human feedback about what went wrong: {feedback}{notes_section}

Look at the screenshot carefully. Decide the single best next action for Playwright.

Return ONLY valid JSON:
{{
  "approach": "coordinate_click" | "text_click" | "selector_click" | "wait_and_retry" | "skip",
  "x": <integer, only if coordinate_click>,
  "y": <integer, only if coordinate_click>,
  "text": "<text to click, only if text_click>",
  "exact": <true|false, only if text_click>,
  "selector": "<CSS selector, only if selector_click>",
  "wait_before_ms": <ms to wait before acting, default 500>,
  "notes": "<one sentence reasoning>"
}}"""

        msg = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=300,
            messages=[{
                "role": "user",
                "content": [
                    {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": img_data}},
                    {"type": "text", "text": prompt},
                ],
            }],
        )

        raw = msg.content[0].text.strip()
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
        return json.loads(raw.strip())

    except Exception as exc:
        return {"approach": "wait_and_retry", "wait_before_ms": 2000, "notes": f"coach error: {exc}"}
