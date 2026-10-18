"""Payroll agent. Hours come from the time source, never from the reasoner."""

from __future__ import annotations

from datetime import date

from .models import Gap, Interval, Person
from .policy import iter_days


def format_hours(minutes: int) -> str:
    """Format minutes as hours with two decimals, rounded half up, no float drift."""
    if minutes < 0:
        raise ValueError("minutes cannot be negative")
    hours, mins = divmod(int(minutes), 60)
    hundredths = (mins * 100 + 30) // 60
    if hundredths == 100:
        hours += 1
        hundredths = 0
    return f"{hours}.{hundredths:02d}"


def _day_buckets(actual: int, expected: int, on_pto: bool) -> tuple[int, int, int]:
    """Return regular, overtime, and PTO minutes for one day.

    Pay what was recorded. Do not top a short day up to the schedule.
    On a PTO day, unpaid schedule time becomes PTO, and recorded time is still paid.
    """
    if on_pto:
        if actual >= expected:
            overtime = actual - expected
            return expected, overtime, 0
        return actual, 0, expected - actual
    if actual > expected:
        return expected, actual - expected, 0
    return actual, 0, 0


def build_rows(
    people: list[Person],
    intervals: list[Interval],
    gaps: list[Gap],
    start: date,
    end: date,
    exclude_ids: set[str],
) -> tuple[list[dict], list[dict]]:
    by_person_day: dict[tuple[str, str], list[Interval]] = {}
    for interval in intervals:
        by_person_day.setdefault((interval.person_id, interval.day), []).append(interval)
    gaps_by_person: dict[str, list[Gap]] = {}
    for gap in gaps:
        gaps_by_person.setdefault(gap.person_id, []).append(gap)

    rows: list[dict] = []
    exceptions: list[dict] = []
    for person in sorted(people, key=lambda item: item.id):
        person_gaps = gaps_by_person.get(person.id, [])
        if person.id in exclude_ids:
            reasons = [
                f"{gap.kind} on {gap.day}"
                for gap in person_gaps
                if gap.severity == "block"
            ]
            exceptions.append(
                {
                    "employee_id": person.id,
                    "employee_name": person.name,
                    "reason": "; ".join(reasons) or "held",
                }
            )
            continue
        regular = overtime = pto = 0
        for day in iter_days(start, end):
            key = day.isoformat()
            expected = int(person.expected_minutes_by_day.get(key, 0))
            day_intervals = by_person_day.get((person.id, key), [])
            actual = 0
            # Same merge rules as the gap agent, via Interval.minutes after skipping opens.
            # Recompute with the gap agent's merger by summing non-overlapping closed time.
            actual = _closed_minutes(day_intervals)
            reg, ot, pto_min = _day_buckets(actual, expected, key in set(person.pto_days))
            regular += reg
            overtime += ot
            pto += pto_min
        review = sorted({gap.kind for gap in person_gaps if gap.severity == "review"})
        rows.append(
            {
                "employee_id": person.id,
                "employee_name": person.name,
                "period_start": start.isoformat(),
                "period_end": end.isoformat(),
                "regular_hours": format_hours(regular),
                "overtime_hours": format_hours(overtime),
                "pto_hours": format_hours(pto),
                "review_flags": ";".join(review),
            }
        )
    return rows, exceptions


def _closed_minutes(intervals: list[Interval]) -> int:
    from .gaps import _minutes_on_day

    minutes, _opens, _bad = _minutes_on_day(intervals)
    return minutes
