"""The tool-use serializer must emit DSL lines that the runner's executor accepts.
Round-trips every action tool and validates the output with the same checker used
by eval/dsl_check (which mirrors _run_direct_commands)."""

from engine.tools import serialize_tool_calls, ACTION_TOOLS
from eval.dsl_check import _check_line, KNOWN_VERBS


def _block(name, **inp):
    return {"type": "tool_use", "name": name, "input": inp}


def test_each_verb_serializes_to_valid_dsl():
    cases = [
        (_block("click", target="Proxy Now"), "CLICK: Proxy Now"),
        (_block("click_xy", x=1224, y=25), "CLICK_XY: 1224, 25"),
        (_block("click_mark", number=7), "CLICK_MARK: 7"),
        (_block("type_text", text="Jane Smith"), "TYPE: Jane Smith"),
        (_block("press_key", key="Enter"), "PRESS: Enter"),
        (_block("wait", ms=1500), "WAIT: 1500"),
        (_block("fill", label="Postal Code", value="W1 1AA"), "FILL: Postal Code | W1 1AA"),
        (_block("shadow_click", text="Public Profile"), "SHADOW_CLICK: Public Profile"),
        (_block("navigate", module="Recruiting"), "NAVIGATE: Recruiting"),
        (_block("select_option", option="United Kingdom"), "SELECT: United Kingdom"),
        (_block("select_option_value", label="Country", value="UK"), "SELECT_OPTION: Country | UK"),
    ]
    for block, expected in cases:
        out = serialize_tool_calls([block])
        assert out == expected, f"{block['name']} -> {out!r} != {expected!r}"
        assert _check_line(out) is None, f"executor would reject: {out!r}"


def test_float_coords_render_as_ints_when_whole():
    assert serialize_tool_calls([_block("click_xy", x=945.0, y=496.0)]) == "CLICK_XY: 945, 496"


def test_multiple_blocks_join_in_order():
    blocks = [_block("click", target="Proxy Now"), _block("wait", ms=3000),
              _block("type_text", text="Jane")]
    assert serialize_tool_calls(blocks) == "CLICK: Proxy Now\nWAIT: 3000\nTYPE: Jane"


def test_non_tool_and_unknown_blocks_skipped():
    blocks = [
        {"type": "text", "text": "I will click Proxy Now"},
        _block("task_complete"),                       # control tool, not an action
        _block("ask_user", question="who?"),           # control tool, not an action
        _block("click", target="Proxy Now"),
    ]
    assert serialize_tool_calls(blocks) == "CLICK: Proxy Now"


def test_empty_returns_empty_string():
    assert serialize_tool_calls([]) == ""
    assert serialize_tool_calls(None) == ""


def test_no_action_tool_emits_js():
    # The whole point: JS must not be a generatable action.
    assert not any(t["name"] == "js" for t in ACTION_TOOLS)
    assert "JS" in KNOWN_VERBS  # executor still replays legacy JS from storage
