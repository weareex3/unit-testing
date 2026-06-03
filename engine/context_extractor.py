"""
Extract runtime values from SF pages (position IDs, req IDs, etc.)
and substitute them into later step commands as {{position_id}}, {{req_id}}, etc.
"""

import re
from datetime import datetime

# Patterns applied to visible page text after each step.
# Order matters — more specific patterns first.
_EXTRACT_PATTERNS = [
    # SF Position IDs: POS100139
    ("position_id",      r"\bPOS\d{5,}\b"),
    # Job Req IDs — various SF formats: JR-1001, JR 1001, Req-1001
    ("req_id",           r"\bJR[-\s]?\d{3,}\b"),
    ("req_id",           r"\bReq[-\s]?\d{3,}\b"),
    # Offer ID
    ("offer_id",         r"\bOFF[-\s]?\d{3,}\b"),
    # Application ID
    ("application_id",   r"\bAPP[-\s]?\d{3,}\b"),
    # Generic "was created" pattern — grab whatever code follows
    ("created_id",       r"(?:created|created:\s*)([\w-]{4,})"),
]

# Keywords in step actions that suggest this step PRODUCES a value
_PRODUCE_KEYWORDS = {
    "position_id": ["copy position", "create position", "add position"],
    "req_id":      ["create job req", "create requisition", "add requisition", "initiate req"],
    "offer_id":    ["create offer", "generate offer", "send offer"],
}

# Keywords that suggest a step CONSUMES a value (needs carry-forward)
_CONSUME_KEYWORDS = {
    "position_id": ["position id", "pos", "position created"],
    "req_id":      ["req id", "requisition id", "job req"],
}


def extract_from_text(page_text: str) -> dict:
    """
    Run all extraction patterns against page text.
    Returns a dict of newly found values.
    """
    found = {}
    for key, pattern in _EXTRACT_PATTERNS:
        if key in found:
            continue  # first match wins
        hits = re.findall(pattern, page_text, re.IGNORECASE)
        if hits:
            # Take the last hit (most recently created item on screen)
            val = hits[-1].strip() if isinstance(hits[-1], str) else hits[-1]
            found[key] = val
    return found


def substitute(text: str, context: dict) -> str:
    """Replace {{key}} or {key} placeholders in a command string."""
    for key, value in context.items():
        text = text.replace(f"{{{{{key}}}}}", str(value))
        text = text.replace(f"{{{key}}}", str(value))
    return text


def normalize_placeholders(text: str) -> str:
    """Convert a stray single-brace {var} to {{var}} (leaves {{var}} untouched).
    Guards against planner prompts whose f-strings collapsed {{var}} -> {var}."""
    return re.sub(r"(?<!\{)\{\s*([A-Za-z_][\w]*)\s*\}(?!\})", r"{{\1}}", text)


def resolve_dynamic_tokens(text: str) -> str:
    """Resolve run-time tokens. {{today}}/{today} -> current date as 'D Mon YYYY'
    (e.g. 2 Jun 2026). Accepts one or two braces."""
    d = datetime.now()
    today = f"{d.day} {d.strftime('%b')} {d.year}"
    return re.sub(r"\{{1,2}\s*today\s*\}{1,2}", today, text, flags=re.IGNORECASE)


def step_produces(step_action: str) -> list[str]:
    """Return list of context keys this step is likely to produce."""
    action_lower = step_action.lower()
    return [key for key, kws in _PRODUCE_KEYWORDS.items() if any(kw in action_lower for kw in kws)]


def step_needs(step_action: str, test_data: str) -> list[str]:
    """Return list of context keys this step is likely to need."""
    combined = (step_action + " " + test_data).lower()
    return [key for key, kws in _CONSUME_KEYWORDS.items() if any(kw in combined for kw in kws)]
