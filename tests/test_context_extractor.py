"""Tests for the deterministic command/placeholder logic — the bug-prone core
(substitution, the {{today}} token, single->double normalisation)."""
import re
from engine.context_extractor import (
    substitute, normalize_placeholders, resolve_dynamic_tokens,
    extract_from_text, step_produces, step_needs,
)


# ── substitute ──────────────────────────────────────────────────────────────
def test_substitute_double_braces():
    assert substitute("TYPE: {{name}}", {"name": "Esther"}) == "TYPE: Esther"


def test_substitute_single_braces():
    # substitute is intentionally tolerant of single braces too
    assert substitute("TYPE: {name}", {"name": "Esther"}) == "TYPE: Esther"


def test_substitute_leaves_unknown_keys_untouched():
    assert substitute("TYPE: {{missing}}", {"name": "x"}) == "TYPE: {{missing}}"


def test_substitute_multiple_keys():
    out = substitute("FILL: {{field}} | {{value}}", {"field": "Phone", "value": "020"})
    assert out == "FILL: Phone | 020"


# ── normalize_placeholders (the f-string-collapse guard) ──────────────────────
def test_normalize_single_to_double():
    assert normalize_placeholders("TYPE: {target_employee_name}") == "TYPE: {{target_employee_name}}"


def test_normalize_leaves_double_untouched():
    assert normalize_placeholders("TYPE: {{target_employee_name}}") == "TYPE: {{target_employee_name}}"


def test_normalize_ignores_plain_text():
    assert normalize_placeholders("CLICK: Proxy Now") == "CLICK: Proxy Now"


def test_normalize_then_substitute_fills_a_collapsed_token():
    # Reproduces the attempt-path bug: planner emitted single-brace, must still fill.
    cmd = normalize_placeholders("TYPE: {target_employee_name}")
    assert substitute(cmd, {"target_employee_name": "Sharon Perrin"}) == "TYPE: Sharon Perrin"


# ── resolve_dynamic_tokens ({{today}}) ───────────────────────────────────────
def test_today_double_braces_resolves_to_a_date():
    out = resolve_dynamic_tokens("TYPE: {{today}}")
    assert "{{today}}" not in out and "{today}" not in out
    assert re.search(r"\d{1,2} [A-Z][a-z]{2} \d{4}", out)


def test_today_single_braces_also_resolves():
    out = resolve_dynamic_tokens("TYPE: {today}")
    assert "{today}" not in out
    assert re.search(r"\d{1,2} [A-Z][a-z]{2} \d{4}", out)


def test_today_case_insensitive():
    assert "{{TODAY}}" not in resolve_dynamic_tokens("TYPE: {{TODAY}}")


# ── extraction helpers ───────────────────────────────────────────────────────
def test_extract_position_id():
    assert extract_from_text("New position POS100139 created").get("position_id") == "POS100139"


def test_step_produces_and_needs():
    assert "position_id" in step_produces("Copy position in org chart")
    assert "req_id" in step_needs("Open the requisition", "Req ID: JR-1001")
