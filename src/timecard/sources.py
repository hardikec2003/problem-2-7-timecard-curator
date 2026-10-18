"""Time-source connectors.

Each one returns people and common entry rows. The rest of the workflow
does not know which product they came from.

Hook contract (any other system):
  GET {base}/roster?from=YYYY-MM-DD&to=YYYY-MM-DD
  GET {base}/entries?from=YYYY-MM-DD&to=YYYY-MM-DD
Optional header: Authorization: Bearer $TIMECARD_HTTP_TOKEN
"""

from __future__ import annotations

import json
import os
from datetime import date, datetime, timedelta
from pathlib import Path
from urllib.parse import urlencode, urlparse

from .models import Interval, Person
from .net import HttpError, request_json
from .parse import entries_to_intervals, odata_values, person_from_record


def _in_period(start_iso: str, period_start: date, period_end: date) -> bool:
    day = start_iso[:10]
    return period_start.isoformat() <= day <= period_end.isoformat()


class FileTimeSource:
    name = "file"

    def __init__(self, path: str):
        self.path = Path(path)
        self.data = json.loads(self.path.read_text(encoding="utf-8"))

    def suggested_period(self) -> tuple[date, date] | None:
        period = self.data.get("period") or {}
        if "start" in period and "end" in period:
            return date.fromisoformat(period["start"]), date.fromisoformat(period["end"])
        return None

    def load(self, start: date, end: date, max_shift_hours: int) -> tuple[list[Person], list[Interval]]:
        people = [person_from_record(row) for row in self.data.get("people") or []]
        entries = []
        for row in self.data.get("entries") or []:
            stamp = str(row.get("start") or row.get("localTime") or row.get("time") or "")
            if stamp and _in_period(stamp, start, end):
                entries.append(row)
        return people, entries_to_intervals(entries, max_shift_hours)


class HttpTimeSource:
    """Generic hook. Point this at any service that speaks the roster/entries contract."""

    name = "http"

    def __init__(self, base_url: str, token: str = ""):
        self.base_url = base_url.rstrip("/")
        self.token = token

    def suggested_period(self):
        return None

    def _get(self, path: str, start: date, end: date):
        query = urlencode({"from": start.isoformat(), "to": end.isoformat()})
        headers = {"Authorization": f"Bearer {self.token}"} if self.token else {}
        return request_json("GET", f"{self.base_url}{path}?{query}", headers=headers)

    def load(self, start: date, end: date, max_shift_hours: int) -> tuple[list[Person], list[Interval]]:
        roster = self._get("/roster", start, end)
        punched = self._get("/entries", start, end)
        people_rows = roster.get("people") if isinstance(roster, dict) else roster
        entry_rows = punched.get("entries") if isinstance(punched, dict) else punched
        people = [person_from_record(row) for row in people_rows or []]
        entries = []
        for row in entry_rows or []:
            stamp = str(row.get("start") or "")
            if not stamp or _in_period(stamp, start, end):
                entries.append(row)
        return people, entries_to_intervals(entries, max_shift_hours)


def _skip_person(row: dict) -> bool:
    status = str(row.get("status") or "").lower()
    return status in {"removed", "inactive", "archived"}


class JibbleTimeSource:
    """Jibble free account. Org owner creates a key under Organization settings → API credentials.

    Auth is OAuth client-credentials. The secret stays in the POST body and is not logged.
    Docs: https://docs.api.jibble.io/
    """

    name = "jibble"
    token_url = "https://identity.prod.jibble.io/connect/token"

    def __init__(self, client_id: str, client_secret: str, api_base: str | None = None):
        if not client_id or not client_secret:
            raise RuntimeError("Set JIBBLE_CLIENT_ID and JIBBLE_CLIENT_SECRET.")
        self.client_id = client_id
        self.client_secret = client_secret
        self.api_base = (api_base or "https://time-tracking.prod.jibble.io").rstrip("/")
        self._token = ""

    def suggested_period(self):
        return None

    def _token_headers(self) -> dict:
        if not self._token:
            payload = request_json(
                "POST",
                self.token_url,
                form={
                    "grant_type": "client_credentials",
                    "client_id": self.client_id,
                    "client_secret": self.client_secret,
                },
            )
            if not isinstance(payload, dict) or "access_token" not in payload:
                raise RuntimeError("Jibble token response did not include an access token.")
            self._token = str(payload["access_token"])
        return {"Authorization": f"Bearer {self._token}"}

    def _collection(self, path: str, params: dict) -> list:
        headers = self._token_headers()
        url = f"{self.api_base}{path}"
        try:
            payload = request_json("GET", f"{url}?{urlencode(params)}", headers=headers)
        except HttpError as exc:
            if exc.status != 400 or "$filter" not in params:
                raise
            reduced = {key: value for key, value in params.items() if key != "$filter"}
            payload = request_json("GET", f"{url}?{urlencode(reduced)}", headers=headers)
        rows = list(odata_values(payload))
        # Follow @odata.nextLink, capped so a bad link cannot loop forever.
        seen = 0
        while isinstance(payload, dict) and payload.get("@odata.nextLink") and seen < 20:
            seen += 1
            next_url = str(payload["@odata.nextLink"])
            parsed = urlparse(next_url)
            if parsed.scheme == "":
                next_url = self.api_base + next_url
            payload = request_json("GET", next_url, headers=headers)
            rows.extend(odata_values(payload))
        return rows

    def load(self, start: date, end: date, max_shift_hours: int) -> tuple[list[Person], list[Interval]]:
        people_rows = self._collection(
            "/v1/People",
            {"$select": "id,fullName,email,status"},
        )
        people = [person_from_record(row) for row in people_rows if not _skip_person(row)]
        start_stamp = datetime.combine(start, datetime.min.time()).isoformat() + "Z"
        end_stamp = datetime.combine(end + timedelta(days=1), datetime.min.time()).isoformat() + "Z"
        entry_rows = self._collection(
            "/v1/TimeEntries",
            {
                "$select": "id,personId,type,time,localTime",
                "$filter": f"time ge {start_stamp} and time lt {end_stamp}",
            },
        )
        common = []
        for row in entry_rows:
            stamp = str(row.get("localTime") or row.get("time") or "")
            if stamp and not _in_period(stamp, start, end):
                continue
            common.append(
                {
                    "person_id": row.get("personId") or row.get("person_id"),
                    "id": row.get("id"),
                    "type": row.get("type"),
                    "localTime": row.get("localTime"),
                    "time": row.get("time"),
                    "start": row.get("start"),
                    "end": row.get("end"),
                }
            )
        return people, entries_to_intervals(common, max_shift_hours)


class KimaiTimeSource:
    """Kimai is the open-source time tracker. https://github.com/kimai/kimai

    The API token needs permission to see other people's timesheets.
    Create it in Kimai under the user profile → API.
    """

    name = "kimai"

    def __init__(self, base_url: str, token: str):
        if not base_url or not token:
            raise RuntimeError("Set KIMAI_BASE_URL and KIMAI_API_TOKEN.")
        self.base_url = base_url.rstrip("/")
        self.token = token

    def suggested_period(self):
        return None

    def _get(self, path: str, params: dict | None = None):
        query = f"?{urlencode(params)}" if params else ""
        return request_json(
            "GET",
            f"{self.base_url}{path}{query}",
            headers={"Authorization": f"Bearer {self.token}"},
        )

    def load(self, start: date, end: date, max_shift_hours: int) -> tuple[list[Person], list[Interval]]:
        users = self._get("/api/users")
        people = []
        for row in users if isinstance(users, list) else odata_values(users):
            people.append(
                person_from_record(
                    {
                        "id": row.get("id"),
                        "name": row.get("alias") or row.get("username"),
                        "email": row.get("email") or "",
                    }
                )
            )
        entries = []
        page = 1
        while page <= 50:
            batch = self._get(
                "/api/timesheets",
                {
                    "begin": f"{start.isoformat()}T00:00:00",
                    "end": f"{end.isoformat()}T23:59:59",
                    "page": page,
                    "size": 100,
                },
            )
            rows = batch if isinstance(batch, list) else odata_values(batch)
            if not rows:
                break
            for row in rows:
                user = row.get("user")
                if isinstance(user, dict):
                    user = user.get("id")
                entries.append(
                    {
                        "person_id": user,
                        "id": row.get("id"),
                        "start": row.get("begin") or row.get("start"),
                        "end": row.get("end"),
                    }
                )
            if len(rows) < 100:
                break
            page += 1
        kept = []
        for row in entries:
            stamp = str(row.get("start") or "")
            if stamp and not _in_period(stamp, start, end):
                continue
            kept.append(row)
        return people, entries_to_intervals(kept, max_shift_hours)


def build_source(name: str, fixture: str):
    if name == "file":
        return FileTimeSource(fixture)
    if name == "http":
        base = os.environ.get("TIMECARD_HTTP_BASE", "")
        if not base:
            raise RuntimeError("Set TIMECARD_HTTP_BASE to your roster/entries service.")
        return HttpTimeSource(base, os.environ.get("TIMECARD_HTTP_TOKEN", ""))
    if name == "jibble":
        return JibbleTimeSource(
            os.environ.get("JIBBLE_CLIENT_ID", ""),
            os.environ.get("JIBBLE_CLIENT_SECRET", ""),
            os.environ.get("JIBBLE_API_BASE"),
        )
    if name == "kimai":
        return KimaiTimeSource(
            os.environ.get("KIMAI_BASE_URL", ""),
            os.environ.get("KIMAI_API_TOKEN", ""),
        )
    raise RuntimeError(f"Unknown time source {name}")
