from __future__ import annotations

import argparse
import importlib.util
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory


PROJECT_ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = PROJECT_ROOT / "skills" / "calendar" / "scripts" / "calendar.py"
SPEC = importlib.util.spec_from_file_location("calendar_skill", MODULE_PATH)
calendar = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = calendar
SPEC.loader.exec_module(calendar)


class FakeClient:
    def __init__(self):
        self.requests = []

    def request(self, method, path, *, query=None, payload=None):
        self.requests.append((method, path, query, payload))
        return {"items": []}


class CalendarTests(unittest.TestCase):
    def config(self):
        return calendar.GoogleServiceConfig(
            "https://www.googleapis.com/calendar/v3",
            PROJECT_ROOT / "data" / "config" / "google.toml",
            30,
            100,
            20,
            5_242_880,
            {
                "default_calendar": "primary",
                "default_timezone": "America/Sao_Paulo",
            },
        )

    def test_load_config(self):
        with TemporaryDirectory() as temporary:
            path = Path(temporary) / "calendar.toml"
            path.write_text(
                'api_base = "https://www.googleapis.com/calendar/v3"\n'
                'google_config = "data/config/google.toml"\n'
                "timeout_seconds = 20\npage_size = 100\nmax_pages = 5\n"
                "max_response_bytes = 1000000\n"
                'default_calendar = "primary"\n'
                'default_timezone = "America/Sao_Paulo"\n',
                encoding="utf-8",
            )
            config = calendar.load_config(path)
        self.assertEqual("primary", config.extras["default_calendar"])

    def test_create_dry_run_is_closed_operation(self):
        args = argparse.Namespace(
            summary="Reunião",
            start="2026-08-01T10:00:00-03:00",
            end="2026-08-01T11:00:00-03:00",
            all_day=False,
            timezone=None,
            description=None,
            location=None,
            attendee=["a@example.com"],
            calendar_id=None,
            send_updates="none",
            google_meet=False,
            dry_run=True,
        )
        result = calendar.event_create(None, self.config(), args)
        self.assertEqual("POST", result["request"]["method"])
        self.assertEqual("primary", result["request"]["path"].split("/")[2])
        self.assertEqual("none", result["request"]["query"]["sendUpdates"])

    def test_all_day_requires_plain_dates(self):
        args = argparse.Namespace(
            summary="Feriado",
            start="2026-08-01T10:00:00-03:00",
            end="2026-08-02",
            all_day=True,
            timezone=None,
            description=None,
            location=None,
            attendee=[],
            calendar_id=None,
            send_updates="none",
            google_meet=False,
            dry_run=True,
        )
        with self.assertRaises(calendar.CalendarToolError):
            calendar.event_create(None, self.config(), args)

    def test_parser_never_accepts_token_or_arbitrary_method(self):
        help_text = calendar.build_parser().format_help()
        self.assertIn("--profile", help_text)
        self.assertNotIn("--token", help_text)
        self.assertNotIn("--method", help_text)

    def test_doctor_uses_calendar_list_scope(self):
        client = FakeClient()
        result = calendar.doctor(client, self.config(), argparse.Namespace())
        self.assertEqual({"items": []}, result)
        self.assertEqual(
            ("GET", "/users/me/calendarList", {"maxResults": 1}, None),
            client.requests[0],
        )

    def test_recurrence_and_reminders_are_bounded(self):
        args = argparse.Namespace(
            summary="Rotina",
            start="2026-08-01T10:00:00-03:00",
            end="2026-08-01T11:00:00-03:00",
            all_day=False,
            timezone=None,
            description=None,
            location=None,
            attendee=None,
            clear_attendees=False,
            recurrence=["RRULE:FREQ=WEEKLY;COUNT=4"],
            clear_recurrence=False,
            reminder=["popup:30"],
            use_default_reminders=False,
            clear_reminders=False,
            calendar_id=None,
            send_updates="none",
            google_meet=False,
            dry_run=True,
        )
        result = calendar.event_create(None, self.config(), args)
        payload = result["request"]["payload"]
        self.assertEqual(["RRULE:FREQ=WEEKLY;COUNT=4"], payload["recurrence"])
        self.assertEqual(30, payload["reminders"]["overrides"][0]["minutes"])

    def test_event_range_must_be_increasing(self):
        args = argparse.Namespace(
            summary="Inválido",
            start="2026-08-01T11:00:00-03:00",
            end="2026-08-01T10:00:00-03:00",
            all_day=False,
            timezone=None,
            description=None,
            location=None,
            attendee=None,
            recurrence=None,
            reminder=None,
            use_default_reminders=False,
            calendar_id=None,
            send_updates="none",
            google_meet=False,
            dry_run=True,
        )
        with self.assertRaises(calendar.CalendarToolError):
            calendar.event_create(None, self.config(), args)


if __name__ == "__main__":
    unittest.main()
