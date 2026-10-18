"""Curator. The only module allowed to call Claude.

CLAUDE_KEY is a Claude Code login (`claude -p`), not an Anthropic API key.
It is passed as CLAUDE_CODE_OAUTH_TOKEN for that one subprocess and is not logged.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from datetime import date

from .models import Gap, Person
from .render import reminder_text

CASE_CHAR_LIMIT = 100_000


def build_case(people: list[Person], gaps: list[Gap], start: date, end: date, policy: dict) -> dict:
    """What the reasoner is allowed to see. No email addresses, no raw punches."""
    gaps_by_person: dict[str, list[str]] = {}
    for gap in gaps:
        gaps_by_person.setdefault(gap.person_id, []).append(gap.id)
    case = {
        "period": {"start": start.isoformat(), "end": end.isoformat()},
        "rules": {
            "blocking_kinds": list(policy["blocking_kinds"]),
            "partial_payroll": bool(policy["partial_payroll"]),
            "tone": policy["reminder_tone"],
        },
        "people": [
            {
                "id": person.id,
                "first_name": person.first_name,
                "gap_ids": gaps_by_person.get(person.id, []),
            }
            for person in people
            if person.id in gaps_by_person
        ],
        "gaps": [
            {
                "id": gap.id,
                "person_id": gap.person_id,
                "day": gap.day,
                "kind": gap.kind,
                "severity": gap.severity,
                "expected_minutes": gap.expected_minutes,
                "actual_minutes": gap.actual_minutes,
            }
            for gap in gaps
        ],
    }
    blob = json.dumps(case)
    if len(blob) > CASE_CHAR_LIMIT:
        raise RuntimeError("Case file is too large for one reasoning call. Narrow the period.")
    return case


def normalize_decision(raw: dict) -> dict:
    def as_bool(value) -> bool:
        if isinstance(value, str):
            return value.strip().lower() in {"true", "yes", "1"}
        return bool(value)

    def as_list(value) -> list:
        if value is None:
            return []
        if isinstance(value, list):
            return value
        return [value]

    reminders = []
    for item in as_list(raw.get("reminders")):
        if not isinstance(item, dict):
            continue
        reminders.append(
            {
                "person_id": str(item.get("person_id") or ""),
                "gap_ids": [str(gap_id) for gap_id in as_list(item.get("gap_ids"))],
                "subject": str(item.get("subject") or ""),
                "body": str(item.get("body") or ""),
            }
        )
    reviews = []
    for item in as_list(raw.get("manager_reviews")):
        if not isinstance(item, dict):
            continue
        reviews.append(
            {
                "person_id": str(item.get("person_id") or ""),
                "gap_ids": [str(gap_id) for gap_id in as_list(item.get("gap_ids"))],
                "note": str(item.get("note") or ""),
            }
        )
    return {
        "reminders": reminders,
        "manager_reviews": reviews,
        "exclude_person_ids": [str(item) for item in as_list(raw.get("exclude_person_ids"))],
        "payroll_allow": as_bool(raw.get("payroll_allow")),
        "reason": str(raw.get("reason") or ""),
        **{key: raw[key] for key in raw if key in {"hours", "regular_hours", "overtime_hours", "pto_hours", "entries", "minutes"}},
    }


def extract_json(text: str) -> dict:
    """Pull the decision object out of a `claude -p --output-format json` reply."""
    text = text.strip()
    if not text:
        raise ValueError("empty reasoner output")
    try:
        outer = json.loads(text)
    except json.JSONDecodeError:
        outer = None
    if isinstance(outer, dict) and "reminders" not in outer and "result" in outer:
        if outer.get("is_error"):
            raise RuntimeError("claude -p returned an error result")
        result = outer["result"]
        if isinstance(result, dict):
            return normalize_decision(result)
        text = str(result)
    fenced = re.search(r"```(?:json)?\s*(\{.*\})\s*```", text, re.DOTALL)
    if fenced:
        text = fenced.group(1)
    else:
        start = text.find("{")
        end = text.rfind("}")
        if start >= 0 and end > start:
            text = text[start : end + 1]
    parsed = json.loads(text)
    if not isinstance(parsed, dict):
        raise ValueError("reasoner output is not an object")
    return normalize_decision(parsed)


PROMPT = """You are the reasoning step in a weekly timecard check.
You do not change hours and you do not invent employees.
Reply with one JSON object and no other text.

Schema:
{{
  "reminders": [{{"person_id": "", "gap_ids": [], "subject": "", "body": ""}}],
  "manager_reviews": [{{"person_id": "", "gap_ids": [], "note": ""}}],
  "exclude_person_ids": [],
  "payroll_allow": true,
  "reason": ""
}}

Rules:
- Send a reminder only to a person who has a block gap (severity "block").
- exclude_person_ids must include every person who has a block gap.
- payroll_allow may be true only when every blocked person is excluded.
- You may set payroll_allow to false to hold the whole export. That is allowed.
- In a reminder body, mention only that person's own dates. Do not name anyone else.
- Be short and factual. No blame and no threats.
- Do not include hours fields. Payroll math is done elsewhere.

Case:
{case}
"""


class LocalReasoner:
    """The written policy, applied exactly. Used for evals and when Claude is not configured."""

    name = "local"

    def decide(self, case: dict, people: list[Person], gaps: list[Gap], start: date, end: date, policy: dict) -> dict:
        by_id = {person.id: person for person in people}
        blocking = [gap for gap in gaps if gap.severity == "block"]
        reviews = [gap for gap in gaps if gap.severity == "review"]
        by_person: dict[str, list[Gap]] = {}
        for gap in blocking:
            by_person.setdefault(gap.person_id, []).append(gap)
        reminders = []
        for person_id, person_gaps in sorted(by_person.items()):
            subject, body = reminder_text(by_id[person_id], person_gaps, start, end)
            reminders.append(
                {
                    "person_id": person_id,
                    "gap_ids": [gap.id for gap in person_gaps],
                    "subject": subject,
                    "body": body,
                }
            )
        manager_reviews = [
            {
                "person_id": gap.person_id,
                "gap_ids": [gap.id],
                "note": gap.detail,
            }
            for gap in reviews
        ]
        excluded = sorted({gap.person_id for gap in blocking})
        allow = bool(policy.get("partial_payroll")) or not excluded
        if not policy.get("partial_payroll") and excluded:
            allow = False
        return {
            "reminders": reminders,
            "manager_reviews": manager_reviews,
            "exclude_person_ids": excluded,
            "payroll_allow": allow,
            "reason": "Baseline policy: remind blocked timecards, pay everyone else the hours they recorded.",
        }


class ClaudeReasoner:
    """One non-interactive `claude -p` call. No tools, no API key."""

    name = "claude"

    def __init__(self, timeout: int = 120):
        self.timeout = timeout

    def decide(self, case: dict, people: list[Person], gaps: list[Gap], start: date, end: date, policy: dict) -> dict:
        key = os.environ.get("CLAUDE_KEY", "").strip()
        if not key:
            raise RuntimeError("CLAUDE_KEY is not set. It is required for --reasoner claude.")
        if shutil.which("claude") is None:
            raise RuntimeError("The claude command is not on PATH.")
        prompt = PROMPT.format(case=json.dumps(case, indent=2))
        env = os.environ.copy()
        env["CLAUDE_CODE_OAUTH_TOKEN"] = key
        env.pop("ANTHROPIC_API_KEY", None)
        completed = subprocess.run(
            ["claude", "-p", prompt, "--output-format", "json", "--max-turns", "1"],
            capture_output=True,
            text=True,
            env=env,
            timeout=self.timeout,
            check=False,
        )
        if completed.returncode != 0:
            detail = (completed.stderr or completed.stdout or "claude failed").strip()
            detail = detail[-400:]
            raise RuntimeError(f"claude -p failed: {detail}")
        return extract_json(completed.stdout)


def select_reasoner(name: str):
    if name == "local":
        return LocalReasoner()
    if name == "claude":
        return ClaudeReasoner()
    if os.environ.get("CLAUDE_KEY", "").strip() and shutil.which("claude"):
        return ClaudeReasoner()
    return LocalReasoner()
