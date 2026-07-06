"""Native tool-use schemas for the SF browser agent, plus a serializer that turns
Claude's structured tool_use blocks back into the text DSL the runner already
executes (engine/runner.py:_run_direct_commands).

The text DSL stays the canonical stored + executed format. These tools only change
how Claude *produces* actions: structured args instead of a free-text blob that has
to be regex/JSON-parsed. `JS` is deliberately NOT a tool — the model can no longer
generate arbitrary JavaScript — while the executor still replays legacy `JS:` lines
already saved in storage.
"""

from typing import Any

# Tool schemas mirror the verbs _run_direct_commands handles. Names map 1:1 to DSL
# verbs in _serialize_one below. Descriptions echo the runner's own guidance so the
# model picks the right action.
ACTION_TOOLS: list[dict] = [
    {
        "name": "click",
        "description": "Click an element by its visible text (button, link, menu item, "
                       "search result). Tries text, button, link, and shadow-DOM matches.",
        "input_schema": {
            "type": "object",
            "properties": {"target": {"type": "string", "description": "The exact visible text to click."}},
            "required": ["target"],
        },
    },
    {
        "name": "click_xy",
        "description": "Click at exact pixel coordinates (screen is 1280x720). Use only "
                       "when there is no readable text to click — e.g. an unlabeled pencil/edit icon.",
        "input_schema": {
            "type": "object",
            "properties": {
                "x": {"type": "number", "description": "X pixel coordinate (0-1280)."},
                "y": {"type": "number", "description": "Y pixel coordinate (0-720)."},
            },
            "required": ["x", "y"],
        },
    },
    {
        "name": "click_mark",
        "description": "Click a numbered element when numbered red badges are shown on the "
                       "screenshot. Pass the number on the element you want.",
        "input_schema": {
            "type": "object",
            "properties": {"number": {"type": "integer", "description": "The badge number on the element."}},
            "required": ["number"],
        },
    },
    {
        "name": "type_text",
        "description": "Type text into the currently focused field (appends; does not clear).",
        "input_schema": {
            "type": "object",
            "properties": {"text": {"type": "string", "description": "Text to type."}},
            "required": ["text"],
        },
    },
    {
        "name": "press_key",
        "description": "Press a keyboard key, e.g. Enter, Tab, Escape, ArrowDown, Control+A.",
        "input_schema": {
            "type": "object",
            "properties": {"key": {"type": "string", "description": "Key name to press."}},
            "required": ["key"],
        },
    },
    {
        "name": "wait",
        "description": "Wait for the given number of milliseconds. Use ~1500 after anything "
                       "that opens a menu/dialog or navigates.",
        "input_schema": {
            "type": "object",
            "properties": {"ms": {"type": "integer", "description": "Milliseconds to wait."}},
            "required": ["ms"],
        },
    },
    {
        "name": "fill",
        "description": "Set a named form field by its visible label (clears the field first). "
                       "Prefer this over click-then-type for replacing a field value.",
        "input_schema": {
            "type": "object",
            "properties": {
                "label": {"type": "string", "description": "The field's visible label."},
                "value": {"type": "string", "description": "The value to put in the field."},
            },
            "required": ["label", "value"],
        },
    },
    {
        "name": "shadow_click",
        "description": "Click an element by text that lives inside a shadow DOM (e.g. some SF "
                       "popups) when a normal click can't find it.",
        "input_schema": {
            "type": "object",
            "properties": {"text": {"type": "string", "description": "Visible text of the shadow-DOM element."}},
            "required": ["text"],
        },
    },
    {
        "name": "navigate",
        "description": "Open a SuccessFactors module via the module picker, e.g. 'Recruiting'. "
                       "For nested paths use 'Module -> Sub-area'.",
        "input_schema": {
            "type": "object",
            "properties": {"module": {"type": "string", "description": "Module name or 'A -> B' path."}},
            "required": ["module"],
        },
    },
    {
        "name": "select_option",
        "description": "Click an option in an open dropdown / listbox by its text.",
        "input_schema": {
            "type": "object",
            "properties": {"option": {"type": "string", "description": "The option text to select."}},
            "required": ["option"],
        },
    },
    {
        "name": "select_option_value",
        "description": "Select an option in a native <select> element by its label.",
        "input_schema": {
            "type": "object",
            "properties": {
                "label": {"type": "string", "description": "The <select> field's visible label."},
                "value": {"type": "string", "description": "The option label to choose."},
            },
            "required": ["label", "value"],
        },
    },
]

# Control tools for the autonomous agent loop (Phase 2). Not part of ACTION_TOOLS so
# the per-step vision producer can't accidentally "finish" or "ask".
ASK_USER_TOOL: dict = {
    "name": "ask_user",
    "description": "Ask the human operator for a value you need to proceed (e.g. which "
                   "person to act on) when it is not in the goal or history. Use sparingly.",
    "input_schema": {
        "type": "object",
        "properties": {"question": {"type": "string", "description": "A short who/what question."}},
        "required": ["question"],
    },
}
TASK_COMPLETE_TOOL: dict = {
    "name": "task_complete",
    "description": "Call when the goal is fully achieved (or, in preview, when you have "
                   "reached the point just before the final commit).",
    "input_schema": {"type": "object", "properties": {}, "required": []},
}


def _fmt_num(n: Any) -> str:
    """Render a coordinate as int when whole (1224 not 1224.0), else as given."""
    try:
        f = float(n)
        return str(int(f)) if f == int(f) else str(f)
    except (TypeError, ValueError):
        return str(n)


def _serialize_one(name: str, inp: dict) -> str | None:
    """Map one tool_use (name + input dict) to its DSL line, or None if unknown."""
    g = lambda k: str(inp.get(k, "")).strip()
    if name == "click":
        return f"CLICK: {g('target')}"
    if name == "click_xy":
        return f"CLICK_XY: {_fmt_num(inp.get('x'))}, {_fmt_num(inp.get('y'))}"
    if name == "click_mark":
        return f"CLICK_MARK: {_fmt_num(inp.get('number'))}"
    if name == "type_text":
        return f"TYPE: {g('text')}"
    if name == "press_key":
        return f"PRESS: {g('key')}"
    if name == "wait":
        return f"WAIT: {_fmt_num(inp.get('ms'))}"
    if name == "fill":
        return f"FILL: {g('label')} | {g('value')}"
    if name == "shadow_click":
        return f"SHADOW_CLICK: {g('text')}"
    if name == "navigate":
        return f"NAVIGATE: {g('module')}"
    if name == "select_option":
        return f"SELECT: {g('option')}"
    if name == "select_option_value":
        return f"SELECT_OPTION: {g('label')} | {g('value')}"
    return None


def serialize_tool_calls(blocks) -> str:
    """Turn an Anthropic response's content blocks into a newline-joined DSL string,
    in the order the model emitted them. Accepts SDK objects or plain dicts. Unknown
    or control tools (ask_user/task_complete) are skipped — callers read those
    separately."""
    lines: list[str] = []
    for b in blocks or []:
        btype = getattr(b, "type", None) or (b.get("type") if isinstance(b, dict) else None)
        if btype != "tool_use":
            continue
        name = getattr(b, "name", None) or (b.get("name") if isinstance(b, dict) else None)
        inp = getattr(b, "input", None) or (b.get("input") if isinstance(b, dict) else None) or {}
        if not isinstance(inp, dict):
            inp = {}
        line = _serialize_one(str(name or ""), inp)
        if line:
            lines.append(line)
    return "\n".join(lines)
