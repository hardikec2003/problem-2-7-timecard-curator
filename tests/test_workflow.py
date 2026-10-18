"""Unit and workflow tests. They stay offline: no Jibble, no Claude, no SMTP."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from datetime import date, datetime
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from timecard.gaps import detect_gaps  # noqa: E402
from timecard.models import Interval, Person  # noqa: E402
from timecard.parse import entries_to_intervals  # noqa: E402
from timecard.payroll import build_rows, format_hours  # noqa: E402
from timecard.policy import fill_schedule, last_work_week, load_policy  # noqa: E402
from timecard.reasoner import ClaudeReasoner, extract_json  # noqa: E402
from timecard.workflow import Settings, run_workflow  # noqa: E402


def _settings(out: Path, **overrides) -> Settings:
    values = dict(
        source_name="file",
        fixture=str(ROOT / "data" / "sample_week.json"),
        reasoner_name="local",
        out_dir=str(out),
        send=False,
        policy_path=str(ROOT / "config" / "policy.json"),
        schedule_path=str(ROOT / "config" / "schedule.json"),
    )
    values.update(overrides)
    return Settings(**values)


class HoursAndDates(unittest.TestCase):
    def test_hours_round_half_up_without_float_drift(self):
        self.assertEqual(format_hours(0), "0.00")
        self.assertEqual(format_hours(30), "0.50")
        self.assertEqual(format_hours(480), "8.00")
        self.assertEqual(format_hours(2250), "37.50")

    def test_last_finished_work_week(self):
        # 17 Oct 2026 is a Saturday. 19 Oct is the next Monday.
        self.assertEqual(last_work_week(date(2026, 10, 17)), (date(2026, 10, 12), date(2026, 10, 16)))
        self.assertEqual(last_work_week(date(2026, 10, 19)), (date(2026, 10, 12), date(2026, 10, 16)))


class GapRules(unittest.TestCase):
    def setUp(self):
        self.policy = load_policy(str(ROOT / "config" / "policy.json"))

    def _person(self, day: date, **kwargs) -> Person:
        person = Person(id="sam", name="Sam Lee", email="sam@example.com", **kwargs)
        fill_schedule(person, day, day, self.policy, {})
        return person

    def test_overlapping_punches_are_not_double_counted(self):
        day = date(2026, 3, 2)
        person = self._person(day)
        intervals = entries_to_intervals(
            [
                {"person_id": "sam", "id": "1", "start": "2026-03-02T09:00:00", "end": "2026-03-02T12:00:00"},
                {"person_id": "sam", "id": "2", "start": "2026-03-02T11:00:00", "end": "2026-03-02T17:00:00"},
            ]
        )
        gaps = detect_gaps([person], intervals, day, day, self.policy)
        self.assertEqual(gaps, [])
        rows, _ = build_rows([person], intervals, gaps, day, day, set())
        self.assertEqual(rows[0]["regular_hours"], "8.00")

    def test_pto_day_with_some_work_is_not_a_gap(self):
        day = date(2026, 3, 4)
        person = self._person(day, pto_days=["2026-03-04"])
        intervals = entries_to_intervals(
            [{"person_id": "sam", "id": "1", "start": "2026-03-04T09:00:00", "end": "2026-03-04T11:00:00"}]
        )
        gaps = detect_gaps([person], intervals, day, day, self.policy)
        self.assertEqual(gaps, [])
        rows, _ = build_rows([person], intervals, gaps, day, day, set())
        self.assertEqual(rows[0]["regular_hours"], "2.00")
        self.assertEqual(rows[0]["pto_hours"], "6.00")

    def test_backwards_entry_is_not_called_a_missing_day(self):
        day = date(2026, 3, 2)
        person = self._person(day)
        intervals = [
            Interval("sam", "bad", datetime(2026, 3, 2, 17, 0), datetime(2026, 3, 2, 9, 0))
        ]
        kinds = [gap.kind for gap in detect_gaps([person], intervals, day, day, self.policy)]
        self.assertEqual(kinds, ["bad_range"])

    def test_a_shift_longer_than_16_hours_stays_open(self):
        intervals = entries_to_intervals(
            [
                {"person_id": "sam", "id": "in", "type": "In", "localTime": "2026-03-02T09:00:00"},
                {"person_id": "sam", "id": "out", "type": "Out", "localTime": "2026-03-03T09:00:00"},
            ]
        )
        self.assertEqual(len(intervals), 1)
        self.assertIsNone(intervals[0].end)


class SampleWeek(unittest.TestCase):
    def test_dry_run_writes_payroll_and_does_not_mark_reminders_sent(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "dry"
            result = run_workflow(_settings(out, send=False))
            self.assertEqual(result.exit_code, 0, result.summary)
            self.assertEqual({gap.id for gap in result.gaps}, {
                "ben:2026-03-04:missing_day",
                "chi:2026-03-03:open_punch",
                "dana:2026-03-05:short_day",
                "eli:2026-03-06:overtime",
            })
            self.assertTrue((out / "payroll.csv").exists())
            self.assertFalse((out / "state.json").exists())
            self.assertTrue(all(message["status"] == "dry_run" for message in result.messages))
            audit = (out / "audit.jsonl").read_text(encoding="utf-8")
            self.assertNotIn("ben@example.com", audit)
            self.assertNotIn("lead@example.com", audit)

    def test_second_send_does_not_duplicate(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = str(Path(tmp) / "state.json")
            first = run_workflow(_settings(Path(tmp) / "a", send=True, state_path=state))
            second = run_workflow(_settings(Path(tmp) / "b", send=True, state_path=state))
            self.assertEqual(sorted(m["status"] for m in first.messages), ["sent", "sent"])
            self.assertEqual(sorted(m["status"] for m in second.messages), ["skipped", "skipped"])

    def test_unsafe_decision_sends_nothing(self):
        class Bad:
            name = "bad"

            def decide(self, case, people, gaps, start, end, policy):
                return {
                    "reminders": [{"person_id": "ana", "gap_ids": [], "subject": "Hi", "body": "Hi Ana and Ben Cole"}],
                    "manager_reviews": [],
                    "exclude_person_ids": [],
                    "payroll_allow": True,
                    "reason": "no",
                    "regular_hours": {"ana": "40.00"},
                }

        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "bad"
            result = run_workflow(_settings(out), reasoner=Bad())
            self.assertEqual(result.exit_code, 2)
            self.assertFalse((out / "payroll.csv").exists())
            self.assertFalse((out / "reminders.jsonl").exists())
            self.assertTrue((out / "decision_rejected.json").exists())

    def test_hold_entire_payroll_when_policy_says_so(self):
        with tempfile.TemporaryDirectory() as tmp:
            policy = Path(tmp) / "policy.json"
            policy.write_text(json.dumps({"partial_payroll": False}), encoding="utf-8")
            out = Path(tmp) / "hold"
            result = run_workflow(_settings(out, policy_path=str(policy), send=True))
            self.assertEqual(result.exit_code, 0, result.summary)
            self.assertFalse((out / "payroll.csv").exists())
            self.assertTrue((out / "payroll_hold.txt").exists())
            self.assertEqual(sorted(m["person_id"] for m in result.messages), ["ben", "chi"])

    def test_missing_email_is_not_invented(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = Path(tmp) / "week.json"
            fixture.write_text(
                json.dumps(
                    {
                        "period": {"start": "2026-03-02", "end": "2026-03-02"},
                        "people": [{"id": "sam", "name": "Sam Lee", "email": ""}],
                        "entries": [],
                    }
                ),
                encoding="utf-8",
            )
            out = Path(tmp) / "out"
            result = run_workflow(_settings(out, fixture=str(fixture), send=True))
            self.assertEqual(result.messages[0]["status"], "no_email")
            self.assertEqual(result.messages[0]["to"], "")
            self.assertEqual(result.excluded, ["sam"])
            self.assertFalse((out / "state.json").exists())


class ReasonerContract(unittest.TestCase):
    def test_extracts_json_from_cli_envelope_and_fences(self):
        decision = {
            "reminders": [],
            "manager_reviews": [],
            "exclude_person_ids": ["ben"],
            "payroll_allow": True,
            "reason": "ok",
        }
        fenced = "```json\n" + json.dumps(decision) + "\n```"
        envelope = json.dumps({"type": "result", "result": fenced, "is_error": False})
        self.assertEqual(extract_json(envelope)["exclude_person_ids"], ["ben"])
        self.assertTrue(extract_json(json.dumps({**decision, "regular_hours": {"a": 1}})).get("regular_hours"))

    def test_claude_key_is_passed_only_as_oauth_token(self):
        decision = {
            "reminders": [],
            "manager_reviews": [],
            "exclude_person_ids": [],
            "payroll_allow": True,
            "reason": "ok",
        }
        stdout = json.dumps({"type": "result", "result": json.dumps(decision), "is_error": False})
        with mock.patch.dict(os.environ, {"CLAUDE_KEY": "oat-test", "ANTHROPIC_API_KEY": "sk-should-not-pass"}):
            with mock.patch("timecard.reasoner.shutil.which", return_value="/usr/bin/claude"):
                with mock.patch("timecard.reasoner.subprocess.run") as run:
                    run.return_value = subprocess.CompletedProcess([], 0, stdout, "")
                    ClaudeReasoner().decide({}, [], [], date(2026, 3, 2), date(2026, 3, 6), {})
        env = run.call_args.kwargs["env"]
        command = run.call_args.args[0]
        self.assertEqual(command[0], "claude")
        self.assertIn("-p", command)
        self.assertIn("--output-format", command)
        self.assertEqual(env["CLAUDE_CODE_OAUTH_TOKEN"], "oat-test")
        self.assertNotIn("ANTHROPIC_API_KEY", env)
        self.assertNotIn("oat-test", " ".join(command))


class Commands(unittest.TestCase):
    def test_help_run_and_eval_match_the_grader(self):
        help_run = subprocess.run(
            [sys.executable, "src/main.py", "--help"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(help_run.returncode, 0)
        self.assertIn("eval", help_run.stdout)
        with tempfile.TemporaryDirectory() as tmp:
            demo = str(Path(tmp) / "demo")
            report = str(Path(tmp) / "eval")
            run = subprocess.run(
                [sys.executable, "src/main.py", "run", "--reasoner", "local", "--out", demo],
                cwd=ROOT,
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(run.returncode, 0, run.stderr + run.stdout)
            self.assertIn("Dry run", run.stdout)
            self.assertTrue((Path(demo) / "payroll.csv").exists())
            evaluate = subprocess.run(
                [sys.executable, "src/main.py", "eval", "--out", report],
                cwd=ROOT,
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(evaluate.returncode, 0, evaluate.stdout + evaluate.stderr)
            self.assertIn("All gates passed", evaluate.stdout)


if __name__ == "__main__":
    unittest.main()
