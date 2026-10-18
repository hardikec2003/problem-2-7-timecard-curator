"""Wording that leaves the building. Rendered from facts, not from free-form hours."""

from __future__ import annotations

from datetime import date

from .models import Gap, Person
from .payroll import format_hours


def pretty_day(iso: str) -> str:
    day = date.fromisoformat(iso)
    return f"{day.strftime('%a')} {day.day} {day.strftime('%b')}"


def pretty_period(start: date, end: date) -> str:
    if start.year == end.year and start.month == end.month:
        return f"{start.day}–{end.day} {start.strftime('%b %Y')}"
    return f"{start.isoformat()} to {end.isoformat()}"


def reminder_text(person: Person, gaps: list[Gap], start: date, end: date) -> tuple[str, str]:
    lines = []
    for gap in sorted(gaps, key=lambda item: item.day):
        if gap.kind == "missing_day":
            lines.append(f"- {pretty_day(gap.day)}: no time entered")
        elif gap.kind == "open_punch":
            lines.append(f"- {pretty_day(gap.day)}: clock-in with no clock-out")
        else:
            lines.append(f"- {pretty_day(gap.day)}: {gap.detail}")
    label = pretty_period(start, end)
    subject = f"Timecard needs an update ({label})"
    body = (
        f"Hi {person.first_name},\n\n"
        f"Your timecard for {label} is incomplete:\n"
        + "\n".join(lines)
        + "\n\nPlease add the missing time in the time-tracking tool. "
        "Those days will not go to payroll until they are fixed.\n"
    )
    return subject, body


def manager_report(
    people: list[Person],
    gaps: list[Gap],
    start: date,
    end: date,
) -> str:
    by_id = {person.id: person for person in people}
    label = pretty_period(start, end)
    lines = [f"Manager review for {label}", ""]
    reviews = [gap for gap in gaps if gap.severity == "review"]
    blocks = [gap for gap in gaps if gap.severity == "block"]
    if reviews:
        lines.append("Included in payroll, please glance at these:")
        for gap in sorted(reviews, key=lambda item: (item.person_id, item.day)):
            person = by_id.get(gap.person_id)
            name = person.name if person else gap.person_id
            lines.append(
                f"- {name} — {pretty_day(gap.day)} — {gap.kind.replace('_', ' ')} — "
                f"worked {format_hours(gap.actual_minutes)}h, expected {format_hours(gap.expected_minutes)}h"
            )
        lines.append("")
    else:
        lines.append("No review items.")
        lines.append("")
    lines.append("Held out of payroll until the timecard is fixed:")
    if blocks:
        for gap in sorted(blocks, key=lambda item: (item.person_id, item.day)):
            person = by_id.get(gap.person_id)
            name = person.name if person else gap.person_id
            lines.append(f"- {name} — {pretty_day(gap.day)} — {gap.kind.replace('_', ' ')}")
    else:
        lines.append("- none")
    lines.append("")
    lines.append("Hours in the payroll file are the hours that were recorded. Nothing was filled in.")
    lines.append("")
    return "\n".join(lines)
