"""Outbound connectors. Reminders and payroll leave through here, and nowhere else."""

from __future__ import annotations

import csv
import json
import os
import smtplib
import ssl
from email.message import EmailMessage
from pathlib import Path
from urllib.parse import urlparse

from .net import request_bytes

PAYROLL_COLUMNS = [
    "employee_id",
    "employee_name",
    "period_start",
    "period_end",
    "regular_hours",
    "overtime_hours",
    "pto_hours",
    "review_flags",
]


class FileNotifier:
    """Writes the reminder to a JSONL outbox. Works with no account."""

    name = "file"

    def __init__(self, directory: str):
        self.path = Path(directory) / "reminders.jsonl"

    def deliver(self, message: dict, send: bool) -> str:
        status = "sent" if send else "dry_run"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps({**message, "status": status}) + "\n")
        return status


class WebhookNotifier:
    """POST JSON to NOTIFY_WEBHOOK_URL.

    A Slack incoming webhook (free Slack workspace) gets {"text": "..."}.
    Any other URL gets the full event, so another system can subscribe.
    """

    name = "webhook"

    def __init__(self, url: str):
        if not url:
            raise RuntimeError("Set NOTIFY_WEBHOOK_URL or use --notifier file.")
        self.url = url

    def deliver(self, message: dict, send: bool) -> str:
        if not send:
            return "dry_run"
        host = (urlparse(self.url).hostname or "").lower()
        if host.endswith("slack.com"):
            payload = {"text": f"{message['subject']}\n{message['body']}"}
        else:
            payload = {"event": "timecard.reminder", **message}
        request_bytes("POST", self.url, payload=payload)
        return "sent"


class SmtpNotifier:
    """Email via SMTP. Brevo's free tier, or any company mail server, fits here."""

    name = "smtp"

    def __init__(self, host: str, port: int, user: str, password: str, sender: str):
        if not host or not sender:
            raise RuntimeError("Set SMTP_HOST and SMTP_FROM, or use --notifier file.")
        self.host = host
        self.port = port
        self.user = user
        self.password = password
        self.sender = sender

    def deliver(self, message: dict, send: bool) -> str:
        if not send:
            return "dry_run"
        email = EmailMessage()
        email["From"] = self.sender
        email["To"] = message["to"]
        email["Subject"] = message["subject"]
        email.set_content(message["body"])
        context = ssl.create_default_context()
        if self.port == 465:
            with smtplib.SMTP_SSL(self.host, self.port, timeout=30, context=context) as client:
                if self.user:
                    client.login(self.user, self.password)
                client.send_message(email)
        else:
            with smtplib.SMTP(self.host, self.port, timeout=30) as client:
                client.starttls(context=context)
                if self.user:
                    client.login(self.user, self.password)
                client.send_message(email)
        return "sent"


def build_notifier(name: str, directory: str):
    if name == "file":
        return FileNotifier(directory)
    if name == "webhook":
        return WebhookNotifier(os.environ.get("NOTIFY_WEBHOOK_URL", ""))
    if name == "smtp":
        return SmtpNotifier(
            os.environ.get("SMTP_HOST", ""),
            int(os.environ.get("SMTP_PORT", "587")),
            os.environ.get("SMTP_USER", ""),
            os.environ.get("SMTP_PASSWORD", ""),
            os.environ.get("SMTP_FROM", ""),
        )
    raise RuntimeError(f"Unknown notifier {name}")


class CsvPayrollSink:
    """CSV a payroll clerk can open, or import into the payroll tool you already use."""

    name = "csv"

    def write(self, directory: str, rows: list[dict], exceptions: list[dict], allow: bool, reason: str, send: bool) -> None:
        del send  # The CSV is the review artifact. --send does not change it.
        self._write_files(directory, rows, exceptions, allow, reason)

    def _write_files(self, directory, rows, exceptions, allow, reason) -> None:
        folder = Path(directory)
        folder.mkdir(parents=True, exist_ok=True)
        payroll = folder / "payroll.csv"
        hold = folder / "payroll_hold.txt"
        if allow:
            with payroll.open("w", encoding="utf-8", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=PAYROLL_COLUMNS, lineterminator="\n")
                writer.writeheader()
                for row in rows:
                    writer.writerow({key: row.get(key, "") for key in PAYROLL_COLUMNS})
            if hold.exists():
                hold.unlink()
        else:
            hold.write_text(reason.strip() + "\n", encoding="utf-8")
            if payroll.exists():
                payroll.unlink()
        with (folder / "payroll_exceptions.csv").open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(
                handle,
                fieldnames=["employee_id", "employee_name", "reason"],
                lineterminator="\n",
            )
            writer.writeheader()
            for row in exceptions:
                writer.writerow(row)


class WebhookPayrollSink(CsvPayrollSink):
    """Writes the same CSV, and POSTs the rows when --send is set.

    The body is {"event": "timecard.payroll", "rows": [...], "exceptions": [...]}.
    Point PAYROLL_WEBHOOK_URL at whatever should receive it.
    """

    name = "webhook"

    def __init__(self, url: str):
        if not url:
            raise RuntimeError("Set PAYROLL_WEBHOOK_URL or use --payroll csv.")
        self.url = url

    def write(self, directory: str, rows: list[dict], exceptions: list[dict], allow: bool, reason: str, send: bool) -> None:
        self._write_files(directory, rows, exceptions, allow, reason)
        if send and allow:
            request_bytes(
                "POST",
                self.url,
                payload={"event": "timecard.payroll", "rows": rows, "exceptions": exceptions},
            )


def build_payroll_sink(name: str):
    if name == "csv":
        return CsvPayrollSink()
    if name == "webhook":
        return WebhookPayrollSink(os.environ.get("PAYROLL_WEBHOOK_URL", ""))
    raise RuntimeError(f"Unknown payroll sink {name}")
