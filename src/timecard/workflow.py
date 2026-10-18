"""Orchestrator.

Order is fixed on purpose:

1. Roster agent loads people and punches from one connector.
2. Gap agent turns punches into facts.
3. Curator agent is the only step that reasons. It returns a decision.
4. Guardrails reject a decision that contradicts the facts.
5. Reminder agent and payroll agent then act. They do not invent hours.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from .gaps import detect_gaps
from .guardrails import check_decision, copy_problems
from .models import RunResult
from .payroll import build_rows
from .policy import fill_schedule, last_work_week, load_json, load_policy
from .reasoner import build_case, select_reasoner
from .redact import redact
from .render import manager_report, pretty_day, pretty_period, reminder_text
from .sinks import build_notifier, build_payroll_sink
from .sources import build_source


@dataclass
class Settings:
    source_name: str = "file"
    fixture: str = "data/sample_week.json"
    reasoner_name: str = "local"
    out_dir: str = "out/run"
    start: date | None = None
    end: date | None = None
    period_mode: str = "auto"  # auto | last-week
    send: bool = False
    notifier_name: str = "file"
    payroll_name: str = "csv"
    schedule_path: str = "config/schedule.json"
    policy_path: str = "config/policy.json"
    state_path: str = ""
    today: date | None = None


def _audit(path: Path, event: str, **fields) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    record = redact({"event": event, **fields})
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record) + "\n")


def _load_state(path: Path) -> set[str]:
    if not path.exists():
        return set()
    data = json.loads(path.read_text(encoding="utf-8"))
    return set(data.get("keys") or [])


def _save_state(path: Path, keys: set[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"keys": sorted(keys)}, indent=2) + "\n", encoding="utf-8")


def _resolve_period(settings: Settings, source) -> tuple[date, date]:
    if settings.start and settings.end:
        return settings.start, settings.end
    if settings.period_mode == "last-week":
        return last_work_week(settings.today or date.today())
    suggested = source.suggested_period() if hasattr(source, "suggested_period") else None
    if suggested and settings.period_mode == "auto":
        return suggested
    return last_work_week(settings.today or date.today())


def run_workflow(settings: Settings, reasoner=None) -> RunResult:
    out = Path(settings.out_dir)
    out.mkdir(parents=True, exist_ok=True)
    audit_path = out / "audit.jsonl"
    policy = load_policy(settings.policy_path)
    schedule = load_json(settings.schedule_path)
    manager_env = os.environ.get("TIMECARD_MANAGER_EMAIL", "")
    source = build_source(settings.source_name, settings.fixture)
    start, end = _resolve_period(settings, source)
    if end < start:
        raise RuntimeError("Period end is before period start.")

    reasoner = reasoner or select_reasoner(settings.reasoner_name)
    people, intervals = source.load(start, end, int(policy["max_shift_hours"]))
    for person in people:
        fill_schedule(person, start, end, policy, schedule)
        if not person.manager_email:
            person.manager_email = manager_env
    _audit(
        audit_path,
        "roster",
        source=settings.source_name,
        people=len(people),
        intervals=len(intervals),
        period_start=start.isoformat(),
        period_end=end.isoformat(),
    )

    gaps = detect_gaps(people, intervals, start, end, policy)
    _audit(audit_path, "gaps", count=len(gaps), ids=[gap.id for gap in gaps])

    case = build_case(people, gaps, start, end, policy)
    decision = reasoner.decide(case, people, gaps, start, end, policy)
    _audit(audit_path, "decision", reasoner=reasoner.name, payroll_allow=decision.get("payroll_allow"))

    violations = check_decision(decision, people, gaps, policy)
    if violations:
        (out / "decision_rejected.json").write_text(
            json.dumps({"violations": violations, "reasoner": reasoner.name}, indent=2) + "\n",
            encoding="utf-8",
        )
        _audit(audit_path, "guardrail_rejected", violations=violations)
        summary = "Stopped before sending anything.\n" + "\n".join(f"- {item}" for item in violations)
        return RunResult(
            ok=False,
            exit_code=2,
            reasoner=reasoner.name,
            period_start=start,
            period_end=end,
            people=people,
            gaps=gaps,
            messages=[],
            payroll_rows=[],
            excluded=[],
            violations=violations,
            summary=summary,
            out_dir=str(out),
        )

    by_id = {person.id: person for person in people}
    gaps_by_id = {gap.id: gap for gap in gaps}
    state_path = Path(settings.state_path) if settings.state_path else out / "state.json"
    sent_keys = _load_state(state_path)
    notifier = build_notifier(settings.notifier_name, str(out))
    messages = []
    send_errors = []

    for reminder in decision["reminders"]:
        person = by_id[reminder["person_id"]]
        reminder_gaps = [gaps_by_id[gap_id] for gap_id in reminder["gap_ids"]]
        subject = reminder.get("subject") or ""
        body = reminder.get("body") or ""
        problems = copy_problems(f"{subject}\n{body}", person.id, people)
        used_template = False
        if problems or not body.strip():
            subject, body = reminder_text(person, reminder_gaps, start, end)
            used_template = True
        key = f"{start.isoformat()}:{end.isoformat()}:{person.id}:{','.join(sorted(reminder['gap_ids']))}"
        base = {
            "person_id": person.id,
            "name": person.name,
            "to": person.email,
            "subject": subject,
            "body": body,
            "gap_ids": reminder["gap_ids"],
            "template": used_template,
        }
        if not person.email:
            messages.append({**base, "status": "no_email"})
            continue
        if settings.send and key in sent_keys:
            messages.append({**base, "status": "skipped"})
            continue
        try:
            status = notifier.deliver(
                {"person_id": person.id, "to": person.email, "subject": subject, "body": body, "gap_ids": reminder["gap_ids"]},
                settings.send,
            )
        except Exception as exc:  # a down mail server should not take payroll with it
            send_errors.append(f"{person.id}: {exc}")
            messages.append({**base, "status": "error"})
            continue
        if settings.send and status == "sent":
            sent_keys.add(key)
            _save_state(state_path, sent_keys)
        messages.append({**base, "status": status})

    _audit(audit_path, "reminders", messages=[{"person_id": m["person_id"], "status": m["status"]} for m in messages])

    exclude = set(decision["exclude_person_ids"])
    allow = bool(decision["payroll_allow"])
    rows, exceptions = build_rows(people, intervals, gaps, start, end, exclude if allow else {
        gap.person_id for gap in gaps if gap.severity == "block"
    })
    if not allow:
        # Hold means nobody gets a payroll file, including people with clean cards.
        rows_to_write: list[dict] = []
        exceptions = [
            {
                "employee_id": person.id,
                "employee_name": person.name,
                "reason": "payroll held for the whole period",
            }
            for person in sorted(people, key=lambda item: item.id)
        ]
    else:
        rows_to_write = rows
    report = manager_report(people, gaps, start, end)
    (out / "manager_review.txt").write_text(report, encoding="utf-8")
    sink = build_payroll_sink(settings.payroll_name)
    sink.write(str(out), rows_to_write, exceptions, allow, decision.get("reason") or "Payroll held.", settings.send)
    _audit(audit_path, "payroll", allow=allow, rows=len(rows_to_write), held=len(exceptions))

    run_record = {
        "period": {"start": start.isoformat(), "end": end.isoformat()},
        "source": settings.source_name,
        "reasoner": reasoner.name,
        "dry_run": not settings.send,
        "people": len(people),
        "gaps": [
            {
                "id": gap.id,
                "person_id": gap.person_id,
                "day": gap.day,
                "kind": gap.kind,
                "severity": gap.severity,
            }
            for gap in gaps
        ],
        "reminders": [
            {"person_id": message["person_id"], "status": message["status"], "subject": message["subject"]}
            for message in messages
        ],
        "payroll_rows": len(rows_to_write),
        "excluded": sorted(exclude),
        "violations": [],
        "send_errors": send_errors,
        "steps": ["roster", "gaps", "curator", "guardrails", "reminders", "payroll"],
    }
    (out / "run.json").write_text(json.dumps(run_record, indent=2) + "\n", encoding="utf-8")

    label = pretty_period(start, end)
    lines = [
        f"Timecard Curator — {label}",
        f"Source: {settings.source_name}    Reasoner: {reasoner.name}    "
        + ("Sent for real" if settings.send else "Dry run (nothing was sent)"),
        "",
        "Gaps",
    ]
    if gaps:
        for gap in gaps:
            name = by_id[gap.person_id].name if gap.person_id in by_id else gap.person_id
            lines.append(f"  {name:<16} {pretty_day(gap.day):<12} {gap.kind.replace('_', ' '):<14} {gap.severity}")
    else:
        lines.append("  none")
    reminded = [message["name"] for message in messages if message["status"] in {"sent", "dry_run"}]
    lines.append("")
    lines.append(f"Reminders: {len(reminded)}" + (f" ({', '.join(reminded)})" if reminded else ""))
    if allow:
        lines.append(f"Payroll rows: {len(rows_to_write)}    Held back: {len(exceptions)}")
    else:
        lines.append("Payroll: held for the whole period")
    if send_errors:
        lines.append("Some reminders failed and will be retried next run.")
    lines.append(f"Wrote {out}/")
    summary = "\n".join(lines)
    exit_code = 1 if send_errors else 0
    return RunResult(
        ok=exit_code == 0,
        exit_code=exit_code,
        reasoner=reasoner.name,
        period_start=start,
        period_end=end,
        people=people,
        gaps=gaps,
        messages=messages,
        payroll_rows=rows_to_write,
        excluded=sorted(exclude),
        violations=[],
        summary=summary,
        out_dir=str(out),
    )
