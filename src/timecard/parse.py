"""Turn vendor rows into people and intervals. One parser for every connector."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from .models import Interval, Person

MAX_SHIFT_DEFAULT_HOURS = 16


def parse_dt(value: str) -> datetime:
    text = value.strip().replace("Z", "+00:00")
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(timezone.utc).replace(tzinfo=None)
    return parsed


def person_from_record(row: dict, manager_email: str = "") -> Person:
    name = (
        row.get("name")
        or row.get("fullName")
        or row.get("alias")
        or row.get("username")
        or str(row.get("id"))
    )
    email = row.get("email") or row.get("userEmail") or row.get("preferredEmail") or ""
    return Person(
        id=str(row.get("id") or row.get("person_id") or email or name),
        name=str(name),
        email=str(email or ""),
        manager_email=str(row.get("manager_email") or manager_email or ""),
        pto_days=[str(day) for day in (row.get("pto_days") or [])],
        expected_minutes_by_day={
            str(k): int(v) for k, v in (row.get("expected_minutes_by_day") or {}).items()
        },
    )


def _punch_kind(row: dict) -> str:
    raw = str(row.get("type") or row.get("entryType") or "").strip().lower()
    if raw in {"in", "clockin", "clock_in", "start"}:
        return "in"
    if raw in {"out", "clockout", "clock_out", "end"}:
        return "out"
    return ""


def _event_time(row: dict) -> datetime | None:
    for key in ("localTime", "start", "time", "startTime", "begin"):
        if row.get(key):
            return parse_dt(str(row[key]))
    return None


def _end_time(row: dict) -> datetime | None:
    for key in ("end", "endTime"):
        if row.get(key):
            return parse_dt(str(row[key]))
    return None


def entries_to_intervals(entries: list[dict], max_shift_hours: int = MAX_SHIFT_DEFAULT_HOURS) -> list[Interval]:
    """Closed rows become intervals. In/Out punches are paired per person.

    A shift counts on the day it starts. An Out more than ``max_shift_hours``
    after its In is not paired; the In stays an open punch.
    """
    intervals: list[Interval] = []
    punches: dict[str, list[tuple[datetime, str, str]]] = {}

    for index, row in enumerate(entries):
        person_id = str(row.get("person_id") or row.get("personId") or row.get("user") or "")
        if not person_id:
            continue
        entry_id = str(row.get("id") or row.get("entry_id") or f"row-{index}")
        kind = _punch_kind(row)
        end = _end_time(row)
        start = _event_time(row)
        if start is None:
            continue
        # A row with both ends is already an interval, even if it also has a type.
        if end is not None and kind != "out":
            intervals.append(Interval(person_id, entry_id, start, end))
            continue
        if kind in {"in", "out"} or end is None:
            punches.setdefault(person_id, []).append((start, kind or "in", entry_id))

    limit = timedelta(hours=max_shift_hours)
    for person_id, events in punches.items():
        events.sort(key=lambda item: item[0])
        pending: tuple[datetime, str] | None = None
        for moment, kind, entry_id in events:
            if kind == "in":
                if pending is not None:
                    intervals.append(Interval(person_id, pending[1], pending[0], None))
                pending = (moment, entry_id)
                continue
            if pending is None:
                continue
            if moment > pending[0] and (moment - pending[0]) <= limit:
                intervals.append(Interval(person_id, pending[1], pending[0], moment))
                pending = None
            else:
                intervals.append(Interval(person_id, pending[1], pending[0], None))
                pending = None
        if pending is not None:
            intervals.append(Interval(person_id, pending[1], pending[0], None))
    return intervals


def odata_values(payload) -> list:
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        if isinstance(payload.get("value"), list):
            return payload["value"]
        if isinstance(payload.get("items"), list):
            return payload["items"]
        if isinstance(payload.get("people"), list):
            return payload["people"]
        if isinstance(payload.get("entries"), list):
            return payload["entries"]
    raise ValueError("Expected a list or an object with value/items/people/entries")
