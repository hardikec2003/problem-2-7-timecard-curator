"""The gate between a decision and any reminder or payroll file.

The reasoner may be more conservative than the policy (hold everyone).
It may not be looser: no reminder to a clean person, no pay for a blocked person,
no invented employee, no edited hours.
"""

from __future__ import annotations

from .models import Gap, Person

BLAME_PHRASES = (
    "you forgot",
    "you failed",
    "lazy",
    "disciplinary",
    "or else",
    "unacceptable",
)

FORBIDDEN_FACT_KEYS = {
    "hours",
    "regular_hours",
    "overtime_hours",
    "pto_hours",
    "entries",
    "minutes",
}


def _text_mentions_others(text: str, person_id: str, people: list[Person]) -> bool:
    lowered = text.lower()
    for person in people:
        if person.id == person_id:
            continue
        if person.email and person.email.lower() in lowered:
            return True
        if person.name and len(person.name) > 2 and person.name.lower() in lowered:
            return True
    return False


def copy_problems(text: str, person_id: str, people: list[Person]) -> list[str]:
    problems = []
    if _text_mentions_others(text, person_id, people):
        problems.append("mentions another employee")
    lowered = text.lower()
    for phrase in BLAME_PHRASES:
        if phrase in lowered:
            problems.append(f"blame language: {phrase}")
            break
    if len(text) > 1200:
        problems.append("too long")
    return problems


def check_decision(decision: dict, people: list[Person], gaps: list[Gap], policy: dict) -> list[str]:
    violations: list[str] = []
    people_by_id = {person.id: person for person in people}
    gaps_by_id = {gap.id: gap for gap in gaps}
    blocking_ids = {gap.person_id for gap in gaps if gap.severity == "block"}
    review_or_block = {gap.person_id for gap in gaps}

    for key in FORBIDDEN_FACT_KEYS:
        if key in decision:
            violations.append(f"decision tries to set {key}; hours stay with the payroll step")

    for reminder in decision.get("reminders") or []:
        person_id = str(reminder.get("person_id") or "")
        if person_id not in people_by_id:
            violations.append(f"reminder for unknown person {person_id}")
            continue
        if person_id not in blocking_ids:
            violations.append(f"reminder for {person_id}, who has no blocking gap")
        for gap_id in reminder.get("gap_ids") or []:
            gap = gaps_by_id.get(gap_id)
            if gap is None or gap.person_id != person_id or gap.severity != "block":
                violations.append(f"reminder gap {gap_id} is not a blocking gap for {person_id}")
        text = f"{reminder.get('subject') or ''}\n{reminder.get('body') or ''}"
        # Copy problems are reported separately so the workflow can swap in the template.
        # They are not structural violations.

    for review in decision.get("manager_reviews") or []:
        person_id = str(review.get("person_id") or "")
        if person_id not in people_by_id:
            violations.append(f"review for unknown person {person_id}")
        elif person_id not in review_or_block:
            violations.append(f"review for {person_id}, who has no gap")

    excluded = {str(item) for item in (decision.get("exclude_person_ids") or [])}
    unknown = excluded - set(people_by_id)
    if unknown:
        violations.append("exclusion lists unknown people: " + ", ".join(sorted(unknown)))
    if decision.get("payroll_allow") and not blocking_ids <= excluded:
        missing = ", ".join(sorted(blocking_ids - excluded))
        violations.append(f"payroll would include blocked people: {missing}")
    if not policy.get("partial_payroll") and decision.get("payroll_allow") and blocking_ids:
        violations.append("policy says to hold the whole payroll when anyone is blocked")
    return violations
