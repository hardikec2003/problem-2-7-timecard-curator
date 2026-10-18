# Operating the curator

Run everything from the repository root.

```bash
python src/main.py --help
python src/main.py run --reasoner local --out out/demo
python src/main.py eval --out out/eval
python src/main.py hooks
```

The demo uses `data/sample_week.json` and does not send anything. Add `--send` when the reminders should actually go out.

## A live week

Jibble (free account at https://www.jibble.io/):

```bash
export JIBBLE_CLIENT_ID=...
export JIBBLE_CLIENT_SECRET=...
export TIMECARD_MANAGER_EMAIL=lead@example.com
python src/main.py run --source jibble --period last-week --notifier webhook --out out/live
```

Kimai (open source, https://github.com/kimai/kimai):

```bash
export KIMAI_BASE_URL=https://time.example.com
export KIMAI_API_TOKEN=...
python src/main.py run --source kimai --period last-week --out out/live
```

Any other system that can serve two GET endpoints:

```bash
export TIMECARD_HTTP_BASE=https://your-system.example/timecard
export TIMECARD_HTTP_TOKEN=...          # optional
python src/main.py run --source http --period last-week --out out/live
```

Reminders: `--notifier file` (default), `webhook` (`NOTIFY_WEBHOOK_URL`), or `smtp` (`SMTP_HOST`, `SMTP_PORT`, `SMTP_USER`, `SMTP_PASSWORD`, `SMTP_FROM`).

Payroll: `--payroll csv` (default) or `webhook` (`PAYROLL_WEBHOOK_URL`). The CSV is always written in the run folder so you can read it first. The webhook is only called with `--send`.

Reasoning: set `CLAUDE_KEY` and use `--reasoner claude` or `--reasoner auto`. The key is handed to `claude -p` as `CLAUDE_CODE_OAUTH_TOKEN`. Do not put an Anthropic API key in `ANTHROPIC_API_KEY`.

Expected hours default to 8h Monday–Friday (`config/policy.json`). Per-person PTO and hour overrides go in `config/schedule.json`, matched by email or id.

## What a run writes

| File | Who it is for |
| --- | --- |
| `manager_review.txt` | The manager. Short days, overtime, and who was held. |
| `reminders.jsonl` | The outbox. One line per reminder, with status `dry_run`, `sent`, or `skipped`. |
| `payroll.csv` | Payroll. People with a clean enough card. |
| `payroll_exceptions.csv` | Who was held, and why. |
| `payroll_hold.txt` | Present only when the whole export was held. |
| `run.json` | Machine-readable summary. |
| `audit.jsonl` | Step log. Emails are shortened. Secrets are not stored. |
| `state.json` | Reminder ledger, so the same gap is not sent twice. |
| `decision_rejected.json` | Present only when the gate stops the run. |

## Deploy

Do one dry run and read the files before you turn on `--send`. A Monday 08:15 cron is enough. Point every run at the same ledger so a retry does not send the same reminder twice:

```cron
15 8 * * 1 cd /opt/timecard-curator && python src/main.py run --source jibble --period last-week --notifier webhook --send --state /var/lib/timecard/state.json --out /var/lib/timecard/$(date +\%F)
```

Keep the secrets in the environment of that job, not in the repo. A Dockerfile is included for the same command in a container. The image does not contain the `claude` binary; install that on the host if you use `--reasoner claude`.

Exit codes: `0` finished (gaps are a normal result), `1` a reminder failed and will be retried next time, `2` the gate rejected the decision and nothing was sent.
