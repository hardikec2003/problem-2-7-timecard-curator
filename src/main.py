#!/usr/bin/env python3
"""Timecard Curator command line.

Run from the repository root. Secrets come from the environment, never from flags.
"""

from __future__ import annotations

import argparse
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from timecard.evals import format_report, run_eval  # noqa: E402
from timecard.workflow import Settings, run_workflow  # noqa: E402

HOOKS = """
Open hooks
==========

Time source (read)
  file     data/sample_week.json shape: period, people, entries
  http     GET $TIMECARD_HTTP_BASE/roster?from=YYYY-MM-DD&to=YYYY-MM-DD
           GET $TIMECARD_HTTP_BASE/entries?from=YYYY-MM-DD&to=YYYY-MM-DD
           Optional: Authorization: Bearer $TIMECARD_HTTP_TOKEN
  jibble   Free Jibble account. JIBBLE_CLIENT_ID, JIBBLE_CLIENT_SECRET
  kimai    Open-source Kimai. KIMAI_BASE_URL, KIMAI_API_TOKEN

People fields: id, name, email, manager_email, pto_days, expected_minutes_by_day
Entry fields:  person_id, id, start, end
               end may be null for an open punch
               or type=In / type=Out with localTime, which is how Jibble sends punches

Notifier (reminders)
  file      JSONL outbox in the run folder. No account.
  webhook   POST $NOTIFY_WEBHOOK_URL
            Slack incoming webhooks receive {"text": "..."} .
            Anything else receives {"event": "timecard.reminder", ...}
  smtp      SMTP_HOST, SMTP_PORT, SMTP_USER, SMTP_PASSWORD, SMTP_FROM

Payroll
  csv       payroll.csv and payroll_exceptions.csv in the run folder
  webhook   same files, plus POST $PAYROLL_WEBHOOK_URL
            {"event": "timecard.payroll", "rows": [...], "exceptions": [...]}

Remote URLs must be HTTPS. http://localhost and http://127.0.0.1 are allowed
so you can test a hook on your own machine.

Reasoning
  CLAUDE_KEY is passed only to `claude -p` as CLAUDE_CODE_OAUTH_TOKEN.
  It is not an Anthropic API key. Do not set ANTHROPIC_API_KEY for this.

Columns in payroll.csv
  employee_id, employee_name, period_start, period_end,
  regular_hours, overtime_hours, pto_hours, review_flags
"""


def _date(value: str) -> date:
    return date.fromisoformat(value)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="timecard-curator",
        description="Flag missing timecards, remind people, and export payroll.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    commands = parser.add_subparsers(dest="command", required=True)

    run = commands.add_parser("run", help="Run one pay period", formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    run.add_argument("--source", default="file", choices=["file", "jibble", "kimai", "http"])
    run.add_argument("--fixture", default="data/sample_week.json")
    run.add_argument("--reasoner", default="auto", choices=["auto", "local", "claude"])
    run.add_argument("--out", default="out/demo")
    run.add_argument("--from", dest="start", type=_date, default=None)
    run.add_argument("--to", dest="end", type=_date, default=None)
    run.add_argument("--period", default="auto", choices=["auto", "last-week"])
    run.add_argument("--send", action="store_true", help="Send reminders. Without this, dry-run.")
    run.add_argument("--notifier", default="file", choices=["file", "webhook", "smtp"])
    run.add_argument("--payroll", default="csv", choices=["csv", "webhook"])
    run.add_argument("--schedule", default="config/schedule.json")
    run.add_argument("--policy", default="config/policy.json")
    run.add_argument("--state", default="", help="Reminder ledger. Defaults to state.json inside --out.")

    evaluate = commands.add_parser("eval", help="Score the workflow on the sample week")
    evaluate.add_argument("--fixture", default="data/sample_week.json")
    evaluate.add_argument("--expected", default="data/sample_week.expected.json")
    evaluate.add_argument("--out", default="out/eval")
    evaluate.add_argument("--policy", default="config/policy.json")

    commands.add_parser("hooks", help="Print the connector contract")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == "hooks":
        print(HOOKS.strip())
        return 0
    try:
        if args.command == "eval":
            report = run_eval(args.fixture, args.expected, args.out, args.policy)
            print(format_report(report))
            return 0 if report["passed"] else 1
        if (args.start is None) ^ (args.end is None):
            print("Pass both --from and --to, or neither.", file=sys.stderr)
            return 1
        result = run_workflow(
            Settings(
                source_name=args.source,
                fixture=args.fixture,
                reasoner_name=args.reasoner,
                out_dir=args.out,
                start=args.start,
                end=args.end,
                period_mode=args.period,
                send=args.send,
                notifier_name=args.notifier,
                payroll_name=args.payroll,
                schedule_path=args.schedule,
                policy_path=args.policy,
                state_path=args.state,
            )
        )
    except (RuntimeError, ValueError, OSError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print(result.summary)
    return result.exit_code


if __name__ == "__main__":
    sys.exit(main())
