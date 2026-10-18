"""Eval runner.

The gates below are the bar for this workflow. They run on a fixed week with
planted mistakes, plus a handful of bad decisions the gate must refuse.
No network and no Claude call. That keeps the score repeatable.
"""

from __future__ import annotations

import json
from pathlib import Path

from .guardrails import check_decision
from .policy import load_policy
from .workflow import Settings, run_workflow

GATES = [
    ("gap_recall", "1.00", "every planted gap is found"),
    ("gap_precision", "1.00", "every found gap was planted"),
    ("severity_accuracy", "1.00", "block vs review matches the plant"),
    ("payroll_exact", "true", "exported hours match the source, held people stay out"),
    ("reminder_violations", "0", "no reminder to the wrong person, no leaked name"),
    ("duplicate_sends", "0", "a second run does not send the same reminder again"),
    ("guardrail_catch_rate", "1.00", "unsafe decisions are refused"),
    ("manager_review_recall", "1.00", "the manager file lists every flag"),
]


def _load(path: str) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _reminder_problems(result, expected: dict) -> int:
    problems = 0
    wanted = set(expected["reminder_person_ids"])
    got = []
    for message in result.messages:
        if message["status"] not in {"sent", "dry_run"}:
            continue
        got.append(message["person_id"])
        if message["person_id"] not in wanted:
            problems += 1
        body = (message.get("body") or "").lower()
        for person in result.people:
            if person.id == message["person_id"]:
                continue
            if person.email and person.email.lower() in body:
                problems += 1
            if person.name and person.name.lower() in body:
                problems += 1
    if set(got) != wanted:
        problems += 1
    if any(message["status"] == "error" for message in result.messages):
        problems += 1
    return problems


def _payroll_exact(result, expected: dict) -> bool:
    got = {row["employee_id"]: row for row in result.payroll_rows}
    wanted = expected["payroll"]
    if set(got) != set(wanted):
        return False
    if set(result.excluded) != set(expected["excluded"]):
        return False
    for person_id, spec in wanted.items():
        row = got[person_id]
        for key in ("regular_hours", "overtime_hours", "pto_hours", "review_flags"):
            if row.get(key, "") != spec[key]:
                return False
    return True


def _manager_recall(result) -> float:
    text = Path(result.out_dir, "manager_review.txt").read_text(encoding="utf-8").lower()
    by_id = {person.id: person for person in result.people}
    if not result.gaps:
        return 1.0
    hits = 0
    for gap in result.gaps:
        name = by_id[gap.person_id].name.lower()
        if name in text and gap.kind.replace("_", " ") in text:
            hits += 1
    return hits / len(result.gaps)


def _guardrail_catch_rate(result, policy: dict) -> float:
    people = result.people
    gaps = result.gaps
    blocking = sorted({gap.person_id for gap in gaps if gap.severity == "block"})
    ben_gap = next(gap.id for gap in gaps if gap.person_id == "ben" and gap.severity == "block")
    bad = [
        {
            "reminders": [{"person_id": "ana", "gap_ids": [], "subject": "Hi", "body": "Hi"}],
            "manager_reviews": [],
            "exclude_person_ids": blocking,
            "payroll_allow": True,
            "reason": "reminding a clean timecard",
        },
        {
            "reminders": [],
            "manager_reviews": [],
            "exclude_person_ids": [],
            "payroll_allow": True,
            "reason": "paying blocked people",
        },
        {
            "reminders": [],
            "manager_reviews": [],
            "exclude_person_ids": blocking,
            "payroll_allow": True,
            "reason": "trying to set hours",
            "regular_hours": {"ana": 40},
        },
        {
            "reminders": [],
            "manager_reviews": [{"person_id": "ana", "gap_ids": [], "note": "nothing wrong"}],
            "exclude_person_ids": blocking,
            "payroll_allow": True,
            "reason": "review for a clean person",
        },
    ]
    good = {
        "reminders": [
            {"person_id": "ben", "gap_ids": [ben_gap], "subject": "Update", "body": "Wed 4 Mar: no time entered"}
        ],
        "manager_reviews": [],
        "exclude_person_ids": blocking,
        "payroll_allow": True,
        "reason": "baseline",
    }
    caught = sum(1 for decision in bad if check_decision(decision, people, gaps, policy))
    good_ok = not check_decision(good, people, gaps, policy)
    if not good_ok:
        return 0.0
    return caught / len(bad)


def run_eval(fixture: str, expected_path: str, out_dir: str, policy_path: str = "config/policy.json") -> dict:
    expected = _load(expected_path)
    policy = load_policy(policy_path)
    folder = Path(out_dir)
    folder.mkdir(parents=True, exist_ok=True)
    state_file = folder / "state.json"
    if state_file.exists():
        state_file.unlink()
    first = run_workflow(
        Settings(
            source_name="file",
            fixture=fixture,
            reasoner_name="local",
            out_dir=str(folder / "run1"),
            send=True,
            notifier_name="file",
            payroll_name="csv",
            state_path=str(folder / "state.json"),
            policy_path=policy_path,
        )
    )
    if first.exit_code != 0:
        raise RuntimeError(first.summary)
    second = run_workflow(
        Settings(
            source_name="file",
            fixture=fixture,
            reasoner_name="local",
            out_dir=str(folder / "run2"),
            send=True,
            notifier_name="file",
            payroll_name="csv",
            state_path=str(folder / "state.json"),
            policy_path=policy_path,
        )
    )

    found = {gap.id: gap for gap in first.gaps}
    planted = {item["id"]: item for item in expected["gaps"]}
    overlap = set(found) & set(planted)
    recall = len(overlap) / len(planted) if planted else 1.0
    precision = len(overlap) / len(found) if found else 1.0
    severity_hits = sum(1 for gap_id in overlap if found[gap_id].severity == planted[gap_id]["severity"])
    severity = severity_hits / len(overlap) if overlap else 0.0
    duplicate_sends = sum(1 for message in second.messages if message["status"] == "sent")

    metrics = {
        "gap_recall": round(recall, 2),
        "gap_precision": round(precision, 2),
        "severity_accuracy": round(severity, 2),
        "payroll_exact": _payroll_exact(first, expected),
        "reminder_violations": _reminder_problems(first, expected),
        "duplicate_sends": duplicate_sends,
        "guardrail_catch_rate": round(_guardrail_catch_rate(first, policy), 2),
        "manager_review_recall": round(_manager_recall(first), 2),
    }
    checks = {
        "gap_recall": metrics["gap_recall"] >= 1.0,
        "gap_precision": metrics["gap_precision"] >= 1.0,
        "severity_accuracy": metrics["severity_accuracy"] >= 1.0,
        "payroll_exact": metrics["payroll_exact"] is True,
        "reminder_violations": metrics["reminder_violations"] == 0,
        "duplicate_sends": metrics["duplicate_sends"] == 0,
        "guardrail_catch_rate": metrics["guardrail_catch_rate"] >= 1.0,
        "manager_review_recall": metrics["manager_review_recall"] >= 1.0,
    }
    report = {"passed": all(checks.values()), "metrics": metrics, "checks": checks}
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "eval_report.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    return report


def format_report(report: dict) -> str:
    lines = ["Eval gates", ""]
    descriptions = {name: text for name, _need, text in GATES}
    needs = {name: need for name, need, _text in GATES}
    for name, _need, _text in GATES:
        value = report["metrics"][name]
        if value is True:
            shown = "true"
        elif value is False:
            shown = "false"
        elif isinstance(value, float):
            shown = f"{value:.2f}"
        else:
            shown = str(value)
        mark = "pass" if report["checks"][name] else "FAIL"
        lines.append(f"  {name:<24} {shown:<8} need {needs[name]:<6} {mark}  {descriptions[name]}")
    lines.append("")
    lines.append("All gates passed." if report["passed"] else "One or more gates failed.")
    return "\n".join(lines)
