"""_parse_agent_response: native tool_use blocks become the DSL commands string;
ask_user/task_complete map to ask/done; text-only JSON replies still parse."""

from engine.coach import _parse_agent_response


def test_tool_calls_serialize_in_order():
    blocks = [
        {"type": "text", "text": "I see the profile; I'll open the address card."},
        {"type": "tool_use", "name": "click_mark", "input": {"number": 7}},
        {"type": "tool_use", "name": "wait", "input": {"ms": 1500}},
        {"type": "tool_use", "name": "fill", "input": {"label": "Postal Code", "value": "WD23"}},
    ]
    out = _parse_agent_response(blocks)
    assert out["commands"] == "CLICK_MARK: 7\nWAIT: 1500\nFILL: Postal Code | WD23"
    assert out["reasoning"].startswith("I see the profile")
    assert out["ask"] == ""
    assert out["done"] is False


def test_ask_user_tool_maps_to_ask():
    blocks = [{"type": "tool_use", "name": "ask_user", "input": {"question": "Who am I editing?"}}]
    out = _parse_agent_response(blocks)
    assert out["ask"] == "Who am I editing?"
    assert out["commands"] == ""


def test_task_complete_maps_to_done():
    blocks = [
        {"type": "text", "text": "All fields filled."},
        {"type": "tool_use", "name": "task_complete", "input": {}},
    ]
    out = _parse_agent_response(blocks)
    assert out["done"] is True
    assert out["commands"] == ""


def test_text_only_json_fallback():
    blocks = [{"type": "text", "text": '{"reasoning": "r", "commands": "CLICK: OK", "done": false}'}]
    out = _parse_agent_response(blocks)
    assert out["commands"] == "CLICK: OK"
    assert out["done"] is False


def test_unparseable_text_returns_none():
    assert _parse_agent_response([{"type": "text", "text": "hello"}]) is None
