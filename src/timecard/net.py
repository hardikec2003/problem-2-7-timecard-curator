"""HTTP helper. Remote URLs must be HTTPS. Localhost is allowed for a hook you are testing."""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.parse
import urllib.request
from urllib.parse import urlparse

_SECRET_TEXT = re.compile(
    r"(Bearer\s+)\S+|(client_secret=)[^&\s]+|(eyJ[A-Za-z0-9_\-]{10,})",
    re.IGNORECASE,
)


def _scrub(text: str) -> str:
    return _SECRET_TEXT.sub(lambda match: (match.group(1) or match.group(2) or "") + "***", text)


class HttpError(RuntimeError):
    def __init__(self, status: int, url: str, detail: str):
        self.status = status
        # Drop query strings. They sometimes carry filters, never the secret, but stay tidy.
        clean = url.split("?", 1)[0]
        super().__init__(f"HTTP {status} from {clean}: {_scrub(detail)[:200]}")


def assert_https(url: str) -> None:
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    if parsed.scheme == "https":
        return
    if parsed.scheme == "http" and host in {"localhost", "127.0.0.1"}:
        return
    raise ValueError(f"Refusing non-HTTPS URL ({parsed.scheme or 'missing'}://{host or 'unknown'})")


def request_json(
    method: str,
    url: str,
    *,
    payload: dict | None = None,
    form: dict | None = None,
    headers: dict | None = None,
    timeout: int = 30,
) -> object:
    assert_https(url)
    data = None
    req_headers = {"Accept": "application/json", "User-Agent": "timecard-curator/1.0"}
    if headers:
        req_headers.update(headers)
    if form is not None:
        data = urllib.parse.urlencode(form).encode()
        req_headers["Content-Type"] = "application/x-www-form-urlencoded"
    elif payload is not None:
        data = json.dumps(payload).encode()
        req_headers["Content-Type"] = "application/json"
    request = urllib.request.Request(url, data=data, headers=req_headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read().decode()
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode(errors="replace")
        raise HttpError(exc.code, url, detail) from None
    except urllib.error.URLError as exc:
        raise RuntimeError(f"Could not reach {urlparse(url).netloc}: {exc.reason}") from None
    if not raw:
        return {}
    return json.loads(raw)


def request_bytes(method: str, url: str, *, payload: dict | None = None, headers: dict | None = None, timeout: int = 30) -> tuple[int, dict]:
    """POST and return status plus parsed JSON. Used by webhooks."""
    assert_https(url)
    data = json.dumps(payload or {}).encode()
    req_headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "User-Agent": "timecard-curator/1.0",
    }
    if headers:
        req_headers.update(headers)
    request = urllib.request.Request(url, data=data, headers=req_headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read().decode()
            status = response.status
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode(errors="replace")
        raise HttpError(exc.code, url, detail) from None
    body = json.loads(raw) if raw else {}
    return status, body
