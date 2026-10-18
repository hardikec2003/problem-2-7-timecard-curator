"""Connector tests. Live accounts are not required."""

from __future__ import annotations

import json
import sys
import threading
import unittest
from datetime import date
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from timecard.net import _scrub, assert_https, request_json  # noqa: E402
from timecard.sinks import WebhookNotifier  # noqa: E402
from timecard.sources import HttpTimeSource, JibbleTimeSource  # noqa: E402


class Security(unittest.TestCase):
    def test_remote_http_is_refused_and_localhost_is_allowed(self):
        with self.assertRaises(ValueError):
            request_json("GET", "http://example.com/roster")
        assert_https("http://127.0.0.1:9/roster")
        assert_https("https://time-tracking.prod.jibble.io/v1/People")

    def test_error_text_does_not_keep_secrets(self):
        scrubbed = _scrub("client_secret=super-secret Bearer eyJabcdefghijklmnop")
        self.assertNotIn("super-secret", scrubbed)
        self.assertNotIn("eyJabcdefghijklmnop", scrubbed)

    def test_jibble_secret_is_posted_in_the_body(self):
        captured = {}

        class Fake:
            status = 200

            def read(self):
                return b'{"access_token":"tok"}'

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

        def fake_urlopen(request, timeout=30):
            captured["url"] = request.full_url
            captured["data"] = request.data
            return Fake()

        with mock.patch("timecard.net.urllib.request.urlopen", fake_urlopen):
            JibbleTimeSource("client-id", "super-secret")._token_headers()
        self.assertEqual(urlparse(captured["url"]).path, "/connect/token")
        self.assertNotIn("super-secret", captured["url"])
        self.assertIn(b"client_secret=super-secret", captured["data"])

    def test_jibble_http_base_is_refused(self):
        source = JibbleTimeSource("id", "secret", "http://evil.example")
        source._token = "already-have-one"
        with self.assertRaises(ValueError):
            source.load(date(2026, 3, 2), date(2026, 3, 6), 16)


class JibbleShapes(unittest.TestCase):
    def test_pairs_in_and_out_and_skips_removed_people(self):
        source = JibbleTimeSource("id", "secret")

        def collection(path, params):
            del params
            if path == "/v1/People":
                return [
                    {"id": "ana", "fullName": "Ana Ruiz", "email": "ana@example.com", "status": "Joined"},
                    {"id": "old", "fullName": "Old Person", "email": "old@example.com", "status": "Removed"},
                ]
            return [
                {"id": "1", "personId": "ana", "type": "In", "localTime": "2026-03-02T09:00:00"},
                {"id": "2", "personId": "ana", "type": "Out", "localTime": "2026-03-02T17:00:00"},
                {"id": "3", "personId": "chi", "type": "In", "localTime": "2026-03-03T09:00:00"},
            ]

        source._collection = collection
        people, intervals = source.load(date(2026, 3, 2), date(2026, 3, 6), 16)
        self.assertEqual([person.id for person in people], ["ana"])
        closed = [item for item in intervals if item.end is not None]
        opens = [item for item in intervals if item.end is None]
        self.assertEqual(closed[0].minutes(), 480)
        self.assertEqual(opens[0].person_id, "chi")


class HttpHook(unittest.TestCase):
    def test_local_hook_serves_roster_and_entries(self):
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                path = self.path.split("?", 1)[0]
                if path == "/roster":
                    body = {"people": [{"id": "ana", "name": "Ana Ruiz", "email": "ana@example.com"}]}
                elif path == "/entries":
                    body = {
                        "entries": [
                            {
                                "person_id": "ana",
                                "id": "1",
                                "start": "2026-03-02T09:00:00",
                                "end": "2026-03-02T17:30:00",
                            }
                        ]
                    }
                else:
                    self.send_response(404)
                    self.end_headers()
                    return
                raw = json.dumps(body).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def log_message(self, fmt, *args):
                return

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            port = server.server_address[1]
            source = HttpTimeSource(f"http://127.0.0.1:{port}")
            people, intervals = source.load(date(2026, 3, 2), date(2026, 3, 2), 16)
        finally:
            server.shutdown()
            server.server_close()
        self.assertEqual(people[0].email, "ana@example.com")
        self.assertEqual(intervals[0].minutes(), 510)

    def test_dry_run_webhook_does_not_post(self):
        notifier = WebhookNotifier("https://hooks.slack.com/services/T/B/X")
        message = {"subject": "Timecard", "body": "Wed 4 Mar: no time entered", "person_id": "ben", "to": "b@example.com", "gap_ids": []}
        with mock.patch("timecard.sinks.request_bytes") as post:
            status = notifier.deliver(message, send=False)
            self.assertEqual(status, "dry_run")
            post.assert_not_called()
            notifier.deliver(message, send=True)
        payload = post.call_args.kwargs["payload"]
        self.assertIn("text", payload)
        self.assertNotIn("to", payload)


if __name__ == "__main__":
    unittest.main()
