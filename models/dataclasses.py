from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import List, Optional


class ErrorType(Enum):
    NOT_FOUND = "NOT_FOUND"
    TIMEOUT = "TIMEOUT"
    WRONG_VALUE = "WRONG_VALUE"
    STALE_ELEMENT = "STALE_ELEMENT"
    NAVIGATION_FAIL = "NAVIGATION_FAIL"
    UNKNOWN = "UNKNOWN"


@dataclass
class TestStep:
    step_id: str
    action: str
    target: str
    expected_result: str
    test_data: str
    notes: str


@dataclass
class TestScenario:
    scenario_id: str
    name: str
    module: str
    role: str
    steps: List[TestStep] = field(default_factory=list)


@dataclass
class StepResult:
    step_id: str
    passed: bool
    actual_value: str = ""
    error_type: Optional[str] = None
    error_message: str = ""
    duration_s: float = 0.0
    screenshot_path: str = ""
    self_healed: bool = False
    recovery_attempts: int = 0
    # Command lines that actually executed without error — on failure the failing
    # line is excluded, so callers never save/replay a command that didn't work.
    executed_lines: List[str] = field(default_factory=list)


@dataclass
class ScenarioResult:
    scenario_id: str
    run_id: str
    passed: bool
    steps: List[StepResult] = field(default_factory=list)
    started_at: datetime = field(default_factory=datetime.utcnow)
    ended_at: Optional[datetime] = None
    s3_url: str = ""
