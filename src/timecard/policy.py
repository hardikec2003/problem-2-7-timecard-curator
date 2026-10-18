"""Business rules live in one file so the local reasoner and Claude share them."""

from __future__ import annotations

import json
from datetime import date, timedelta
from pathlib import Path

WEEKDAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]

DEFAULT_POLICY = {
    "workdays": ["mon", "tue", "wed", "thu", "fri"],
    "default_expected_minutes": 480,
    # A day is "short" when recorded time is above zero and under this share of expected.
    "short_day_below_ratio": 0.75,
    # Overtime starts only after this many minutes past the expected day.
    "overtime_grace_minutes": 60,
    # These kinds hold the person out of this period's payroll export.
    "blocking_kinds": ["missing_day", "open_punch"],
    # Pay everyone else. Do not hold the whole company for one missing card.
    "partial_payroll": True,
    "reminder_tone": "State the date and what is missing. No blame, no threats.",
    # A clock-in left open longer than this is not paired with a later clock-out.
    "max_shift_hours": 16,
}


def load_json(path: str | None) -> dict:
    if not path:
        return {}
    file = Path(path)
    if not file.exists():
        return {}
    data = json.loads(file.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"{path} must be a JSON object")
    return data


def load_policy(path: str | None) -> dict:
    policy = dict(DEFAULT_POLICY)
    policy.update(load_json(path))
    return policy


def last_work_week(today: date) -> tuple[date, date]:
    """Most recent Monday–Friday that has already finished.

    On Saturday 17 Oct 2026 this is Mon 12 Oct–Fri 16 Oct.
    On the following Monday it is still that same week, not the new one.
    """
    weekday = today.weekday()  # Monday is 0
    if weekday >= 5:
        friday = today - timedelta(days=weekday - 4)
    else:
        friday = today - timedelta(days=weekday + 3)
    monday = friday - timedelta(days=4)
    return monday, friday


def iter_days(start: date, end: date):
    day = start
    while day <= end:
        yield day
        day += timedelta(days=1)


def fill_schedule(person, start: date, end: date, policy: dict, schedule: dict):
    """Write a concrete expected-minutes map for the period onto the person."""
    overrides = {}
    people_overrides = schedule.get("people") or {}
    overrides = people_overrides.get(person.email) or people_overrides.get(person.id) or {}
    extra_pto = list(overrides.get("pto_days") or [])
    person.pto_days = sorted(set(list(person.pto_days) + extra_pto))

    explicit = dict(person.expected_minutes_by_day or {})
    explicit.update(overrides.get("expected_minutes_by_day") or {})
    workdays = set(policy["workdays"])
    default = int(policy["default_expected_minutes"])
    filled: dict[str, int] = {}
    for day in iter_days(start, end):
        key = day.isoformat()
        if key in explicit:
            filled[key] = int(explicit[key])
        elif WEEKDAYS[day.weekday()] in workdays:
            filled[key] = default
        else:
            filled[key] = 0
    person.expected_minutes_by_day = filled
    if not person.manager_email:
        person.manager_email = schedule.get("manager_email") or ""
    return person
