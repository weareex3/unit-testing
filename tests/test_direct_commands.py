"""_run_direct_commands must report exactly which command lines executed, so a
failed action can never be saved to the library as a working command."""

from types import SimpleNamespace

from engine.runner import _durable_commands, _run_direct_commands


class _FakeKeyboard:
    def __init__(self, fail_on_press: bool):
        self.fail_on_press = fail_on_press
        self.typed = []
        self.pressed = []

    def type(self, text):
        self.typed.append(text)

    def press(self, key):
        if self.fail_on_press:
            raise RuntimeError("element not focused")
        self.pressed.append(key)


class _FakePage:
    def __init__(self, fail_on_press: bool = False):
        self.keyboard = _FakeKeyboard(fail_on_press)

    def wait_for_timeout(self, ms):
        pass

    def screenshot(self, path=None, full_page=False):
        pass


def _step():
    return SimpleNamespace(step_id="t-01", action="test", expected_result="", test_data="")


CMDS = "TYPE: hello\nPRESS: Enter\nWAIT: 100"


def test_failure_excludes_failing_and_later_lines(tmp_path):
    sr = _run_direct_commands(_FakePage(fail_on_press=True), _step(), str(tmp_path), CMDS, 0.0)
    assert sr.passed is False
    assert sr.executed_lines == ["TYPE: hello"]
    assert "PRESS" in sr.error_message


def test_success_records_all_lines(tmp_path):
    sr = _run_direct_commands(_FakePage(), _step(), str(tmp_path), CMDS, 0.0)
    assert sr.passed is True
    assert sr.executed_lines == ["TYPE: hello", "PRESS: Enter", "WAIT: 100"]


def test_unresolved_placeholder_executes_nothing(tmp_path):
    sr = _run_direct_commands(_FakePage(), _step(), str(tmp_path),
                              "TYPE: {{target_employee_name}}", 0.0)
    assert sr.passed is False
    assert sr.executed_lines == []


def test_durable_commands_replace_marks_with_labels():
    marks = [{"i": 7, "x": 640, "y": 320, "label": "Edit Addresses"},
             {"i": 9, "x": 100, "y": 50, "label": ""}]
    lines = ["CLICK_MARK: 7 | 640, 320", "WAIT: 1500", "CLICK_MARK: 9 | 100, 50", "TYPE: hi"]
    out = _durable_commands(lines, marks)
    assert out == ["CLICK: Edit Addresses", "WAIT: 1500", "CLICK_MARK: 9 | 100, 50", "TYPE: hi"]


def test_durable_commands_no_marks_is_identity():
    lines = ["CLICK: OK", "WAIT: 500"]
    assert _durable_commands(lines, []) == lines
