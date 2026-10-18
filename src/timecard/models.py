"""Shared records. Connectors produce these. Agents only pass these around."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime


@dataclass
class Person:
    id: str
    name: str
    email: str = ""
    manager_email: str = ""
    pto_days: list[str] = field(default_factory=list)
    # Filled by the roster step for every date in the period, including zeros.
    expected_minutes_by_day: dict[str, int] = field(default_factory=dict)

    @property
    def first_name(self) -> str:
        return self.name.split()[0] if self.name else self.id


@dataclass
class Interval:
    person_id: str
    entry_id: str
    start: datetime
    end: datetime | None  # None means the person clocked in and never clocked out.

    @property
    def day(self) -> str:
        return self.start.date().isoformat()

    @property
    def open(self) -> bool:
        return self.end is None

    def minutes(self) -> int:
        if self.end is None or self.end < self.start:
            return 0
        return int((self.end - self.start).total_seconds() // 60)


@dataclass
class Gap:
    id: str
    person_id: str
    day: str
    kind: str
    severity: str  # "block" holds the person out of payroll. "review" does not.
    expected_minutes: int
    actual_minutes: int
    detail: str


@dataclass
class RunResult:
    ok: bool
    exit_code: int
    reasoner: str
    period_start: date
    period_end: date
    people: list[Person]
    gaps: list[Gap]
    messages: list[dict]
    payroll_rows: list[dict]
    excluded: list[str]
    violations: list[str]
    summary: str
    out_dir: str
