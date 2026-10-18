"""Gap agent. Computes facts. Does not decide who to email or what to pay."""

from __future__ import annotations

from datetime import date

from .models import Gap, Interval, Person
from .policy import iter_days


def _minutes_on_day(intervals: list[Interval]) -> tuple[int, int, int]:
    """Return closed minutes, open-punch count, and bad-range count.

    Overlapping closed intervals are merged so the same hour is not counted twice.
    """
    closed: list[tuple] = []
    opens = 0
    bad = 0
    for interval in intervals:
        if interval.end is None:
            opens += 1
            continue
        if interval.end < interval.start:
            bad += 1
            continue
        closed.append((interval.start, interval.end))
    closed.sort()
    merged: list[list] = []
    for start, end in closed:
        if not merged or start > merged[-1][1]:
            merged.append([start, end])
        else:
            merged[-1][1] = max(merged[-1][1], end)
    minutes = 0
    for start, end in merged:
        minutes += int((end - start).total_seconds() // 60)
    return minutes, opens, bad


def detect_gaps(
    people: list[Person],
    intervals: list[Interval],
    start: date,
    end: date,
    policy: dict,
) -> list[Gap]:
    by_person_day: dict[tuple[str, str], list[Interval]] = {}
    for interval in intervals:
        by_person_day.setdefault((interval.person_id, interval.day), []).append(interval)

    blocking = set(policy["blocking_kinds"])
    # Compare in integers. 0.75 means "under 75 percent", so 6.0h of an 8.0h day is not short.
    ratio_percent = int(round(float(policy["short_day_below_ratio"]) * 100))
    grace = int(policy["overtime_grace_minutes"])
    found: list[Gap] = []

    for person in people:
        for day in iter_days(start, end):
            key = day.isoformat()
            expected = int(person.expected_minutes_by_day.get(key, 0))
            day_intervals = by_person_day.get((person.id, key), [])
            actual, opens, bad = _minutes_on_day(day_intervals)
            on_pto = key in set(person.pto_days)

            def add(kind: str, detail: str) -> None:
                found.append(
                    Gap(
                        id=f"{person.id}:{key}:{kind}",
                        person_id=person.id,
                        day=key,
                        kind=kind,
                        severity="block" if kind in blocking else "review",
                        expected_minutes=expected,
                        actual_minutes=actual,
                        detail=detail,
                    )
                )

            if bad:
                add("bad_range", "A time entry ends before it starts.")
            if opens:
                add("open_punch", "Clock-in with no clock-out.")
                continue
            # A backwards entry is not the same as a forgotten day.
            if bad and actual == 0:
                continue
            if on_pto:
                continue
            if expected <= 0:
                if actual > grace:
                    add("overtime", "Time recorded on a day that was not scheduled.")
                continue
            if actual == 0:
                add("missing_day", "No time entered.")
                continue
            if actual * 100 < expected * ratio_percent:
                add("short_day", "Recorded time is under 75% of the expected day.")
                continue
            if actual > expected + grace:
                add("overtime", "Recorded time is more than 60 minutes over the expected day.")
    return found
