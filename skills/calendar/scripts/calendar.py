#!/usr/bin/env python3
"""Consulta e altera Google Calendar sem expor credenciais."""

from __future__ import annotations

import sys
from pathlib import Path

# Evitar que este arquivo sombreie o módulo `calendar` da biblioteca padrão.
SCRIPT_DIRECTORY = Path(__file__).resolve().parent
sys.path = [
    item
    for item in sys.path
    if Path(item or ".").resolve() != SCRIPT_DIRECTORY
]

import argparse
import json
import re
import urllib.parse
import uuid
from datetime import datetime
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_CONFIG = PROJECT_ROOT / "data" / "config" / "calendar.toml"
EXAMPLE_CONFIG = PROJECT_ROOT / "config" / "calendar.example.toml"
PROJECT_SCRIPTS = PROJECT_ROOT / "scripts"
if str(PROJECT_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(PROJECT_SCRIPTS))

from credential_vault import VaultToolError  # noqa: E402
from google_accounts import (  # noqa: E402
    GoogleAccess,
    GoogleAccountError,
    load_google_config,
    require_google_scopes,
    refresh_google_access,
)
from google_api import GoogleApiClient, GoogleApiError, paginate  # noqa: E402
from google_service_config import (  # noqa: E402
    GoogleServiceConfig,
    load_service_config,
)


EVENT_ID = re.compile(r"^[A-Za-z0-9_-]+$")
EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
CALENDAR_SCOPES = {
    "https://www.googleapis.com/auth/calendar.calendarlist.readonly",
    "https://www.googleapis.com/auth/calendar.events",
    "https://www.googleapis.com/auth/calendar.freebusy",
}


class CalendarToolError(Exception):
    """Erro seguro da skill Calendar."""


def load_config(path: Path) -> GoogleServiceConfig:
    try:
        config = load_service_config(
            path,
            project_root=PROJECT_ROOT,
            default_path=DEFAULT_CONFIG,
            example_path=EXAMPLE_CONFIG,
            service="Google Calendar",
            api_host="www.googleapis.com",
            api_path="/calendar/v3",
            max_page_size=250,
        )
    except ValueError as exc:
        raise CalendarToolError(str(exc)) from exc
    calendar = str(config.extras.get("default_calendar", "")).strip()
    timezone = str(config.extras.get("default_timezone", "")).strip()
    if not calendar or not timezone:
        raise CalendarToolError(
            "'default_calendar' e 'default_timezone' não podem ficar vazios."
        )
    return config


def _calendar_id(config: GoogleServiceConfig, value: str | None) -> str:
    selected = str(value or config.extras["default_calendar"]).strip()
    if not selected or any(character in selected for character in "\r\n"):
        raise CalendarToolError("Identificador de calendário inválido.")
    return urllib.parse.quote(selected, safe="")


def _event_id(value: str) -> str:
    selected = str(value).strip()
    if not EVENT_ID.fullmatch(selected):
        raise CalendarToolError("Identificador de evento inválido.")
    return selected


def _rfc3339(value: str, field: str) -> str:
    normalized = str(value).strip()
    try:
        datetime.fromisoformat(normalized.replace("Z", "+00:00"))
    except ValueError as exc:
        raise CalendarToolError(
            f"'{field}' deve usar data e hora ISO 8601/RFC3339."
        ) from exc
    return normalized


def _date(value: str, field: str) -> str:
    normalized = str(value).strip()
    try:
        datetime.strptime(normalized, "%Y-%m-%d")
    except ValueError as exc:
        raise CalendarToolError(f"'{field}' deve usar YYYY-MM-DD.") from exc
    return normalized


def _email(value: str) -> str:
    normalized = str(value).strip()
    if not EMAIL.fullmatch(normalized):
        raise CalendarToolError(f"E-mail de participante inválido: '{normalized}'.")
    return normalized


def _validate_range(payload: dict[str, Any]) -> None:
    start = payload.get("start")
    end = payload.get("end")
    if not isinstance(start, dict) or not isinstance(end, dict):
        return
    if "date" in start and "date" in end:
        start_value = datetime.strptime(start["date"], "%Y-%m-%d")
        end_value = datetime.strptime(end["date"], "%Y-%m-%d")
    elif "dateTime" in start and "dateTime" in end:
        start_value = datetime.fromisoformat(start["dateTime"].replace("Z", "+00:00"))
        end_value = datetime.fromisoformat(end["dateTime"].replace("Z", "+00:00"))
        if (start_value.tzinfo is None) != (end_value.tzinfo is None):
            raise CalendarToolError(
                "Start e end devem usar o mesmo padrão de fuso horário."
            )
    else:
        raise CalendarToolError("Start e end devem usar o mesmo tipo de data.")
    if end_value <= start_value:
        raise CalendarToolError("'end' deve ser posterior a 'start'.")


def _reminders(args: argparse.Namespace) -> dict[str, Any] | None:
    raw_reminders = getattr(args, "reminder", None)
    use_default = getattr(args, "use_default_reminders", False)
    clear = getattr(args, "clear_reminders", False)
    selected = int(bool(raw_reminders)) + int(use_default) + int(clear)
    if selected > 1:
        raise CalendarToolError(
            "Use apenas reminders, use-default-reminders ou clear-reminders."
        )
    if use_default:
        return {"useDefault": True}
    if clear:
        return {"useDefault": False, "overrides": []}
    if not raw_reminders:
        return None
    if len(raw_reminders) > 5:
        raise CalendarToolError("Um evento aceita no máximo 5 reminders.")
    overrides = []
    for raw in raw_reminders:
        method, separator, minutes_text = raw.partition(":")
        if (
            separator != ":"
            or method not in {"email", "popup"}
            or not minutes_text.isdigit()
        ):
            raise CalendarToolError(
                "'reminder' deve usar email:MINUTOS ou popup:MINUTOS."
            )
        minutes = int(minutes_text)
        if not 0 <= minutes <= 40320:
            raise CalendarToolError(
                "Minutos de reminder devem ficar entre 0 e 40320."
            )
        overrides.append({"method": method, "minutes": minutes})
    return {"useDefault": False, "overrides": overrides}


def doctor(
    client: GoogleApiClient,
    config: GoogleServiceConfig,
    _args: argparse.Namespace,
) -> Any:
    return client.request(
        "GET",
        "/users/me/calendarList",
        query={"maxResults": 1},
    )


def calendars_list(
    client: GoogleApiClient,
    config: GoogleServiceConfig,
    args: argparse.Namespace,
) -> Any:
    query = {
        "maxResults": config.page_size,
        "minAccessRole": args.min_access_role,
        "showHidden": str(args.show_hidden).lower() if args.show_hidden else None,
        "pageToken": args.page_token,
    }
    return paginate(
        client,
        "/users/me/calendarList",
        "items",
        query,
        all_pages=args.all_pages,
        max_pages=config.max_pages,
    )


def events_list(
    client: GoogleApiClient,
    config: GoogleServiceConfig,
    args: argparse.Namespace,
) -> Any:
    query = {
        "maxResults": config.page_size,
        "timeMin": _rfc3339(args.time_min, "time-min") if args.time_min else None,
        "timeMax": _rfc3339(args.time_max, "time-max") if args.time_max else None,
        "q": args.query,
        "singleEvents": str(args.single_events).lower(),
        "orderBy": args.order_by,
        "showDeleted": str(args.show_deleted).lower() if args.show_deleted else None,
        "timeZone": args.timezone or config.extras["default_timezone"],
        "pageToken": args.page_token,
    }
    return paginate(
        client,
        f"/calendars/{_calendar_id(config, args.calendar_id)}/events",
        "items",
        query,
        all_pages=args.all_pages,
        max_pages=config.max_pages,
    )


def event_show(
    client: GoogleApiClient,
    config: GoogleServiceConfig,
    args: argparse.Namespace,
) -> Any:
    return client.request(
        "GET",
        f"/calendars/{_calendar_id(config, args.calendar_id)}/events/"
        f"{_event_id(args.id)}",
        query={"timeZone": args.timezone or config.extras["default_timezone"]},
    )


def event_instances(
    client: GoogleApiClient,
    config: GoogleServiceConfig,
    args: argparse.Namespace,
) -> Any:
    query = {
        "maxResults": config.page_size,
        "timeMin": _rfc3339(args.time_min, "time-min") if args.time_min else None,
        "timeMax": _rfc3339(args.time_max, "time-max") if args.time_max else None,
        "timeZone": args.timezone or config.extras["default_timezone"],
        "showDeleted": str(args.show_deleted).lower() if args.show_deleted else None,
        "pageToken": args.page_token,
    }
    return paginate(
        client,
        f"/calendars/{_calendar_id(config, args.calendar_id)}/events/"
        f"{_event_id(args.id)}/instances",
        "items",
        query,
        all_pages=args.all_pages,
        max_pages=config.max_pages,
    )


def _event_time(
    value: str,
    *,
    all_day: bool,
    timezone: str,
    field: str,
) -> dict[str, str]:
    if all_day:
        return {"date": _date(value, field)}
    return {"dateTime": _rfc3339(value, field), "timeZone": timezone}


def _event_payload(
    config: GoogleServiceConfig,
    args: argparse.Namespace,
    *,
    partial: bool,
) -> dict[str, Any]:
    payload: dict[str, Any] = {}
    timezone = args.timezone or config.extras["default_timezone"]
    for argument, field in (
        ("summary", "summary"),
        ("description", "description"),
        ("location", "location"),
    ):
        value = getattr(args, argument, None)
        if value is not None:
            payload[field] = value
    if getattr(args, "start", None) is not None:
        payload["start"] = _event_time(
            args.start,
            all_day=args.all_day,
            timezone=timezone,
            field="start",
        )
    if getattr(args, "end", None) is not None:
        payload["end"] = _event_time(
            args.end,
            all_day=args.all_day,
            timezone=timezone,
            field="end",
        )
    attendees = getattr(args, "attendee", None)
    clear_attendees = getattr(args, "clear_attendees", False)
    if attendees and clear_attendees:
        raise CalendarToolError(
            "Não é possível definir e limpar participantes simultaneamente."
        )
    if attendees:
        payload["attendees"] = [{"email": _email(email)} for email in attendees]
    elif clear_attendees:
        payload["attendees"] = []
    recurrence = getattr(args, "recurrence", None)
    clear_recurrence = getattr(args, "clear_recurrence", False)
    if recurrence and clear_recurrence:
        raise CalendarToolError(
            "Não é possível definir e limpar recorrência simultaneamente."
        )
    if recurrence:
        if any(
            not item.startswith(("RRULE:", "RDATE:", "EXDATE:"))
            for item in recurrence
        ):
            raise CalendarToolError(
                "Recorrência deve começar com RRULE:, RDATE: ou EXDATE:."
            )
        payload["recurrence"] = recurrence
    elif clear_recurrence:
        payload["recurrence"] = []
    reminders = _reminders(args)
    if reminders is not None:
        payload["reminders"] = reminders
    if not partial:
        if not payload.get("summary") or "start" not in payload or "end" not in payload:
            raise CalendarToolError(
                "Criação exige summary, start e end."
            )
    elif not payload:
        raise CalendarToolError("Informe ao menos um campo para atualizar.")
    _validate_range(payload)
    return payload


def event_create(
    client: GoogleApiClient,
    config: GoogleServiceConfig,
    args: argparse.Namespace,
) -> Any:
    payload = _event_payload(config, args, partial=False)
    path = f"/calendars/{_calendar_id(config, args.calendar_id)}/events"
    query = {
        "sendUpdates": args.send_updates,
        "conferenceDataVersion": 1 if args.google_meet else None,
    }
    if args.google_meet:
        payload["conferenceData"] = {
            "createRequest": {
                "requestId": f"botina-{uuid.uuid4().hex}",
                "conferenceSolutionKey": {"type": "hangoutsMeet"},
            }
        }
    if args.dry_run:
        return {
            "dry_run": True,
            "request": {"method": "POST", "path": path, "query": query, "payload": payload},
        }
    return client.request("POST", path, query=query, payload=payload)


def event_update(
    client: GoogleApiClient,
    config: GoogleServiceConfig,
    args: argparse.Namespace,
) -> Any:
    payload = _event_payload(config, args, partial=True)
    path = (
        f"/calendars/{_calendar_id(config, args.calendar_id)}/events/"
        f"{_event_id(args.id)}"
    )
    query = {"sendUpdates": args.send_updates}
    if args.dry_run:
        return {
            "dry_run": True,
            "request": {"method": "PATCH", "path": path, "query": query, "payload": payload},
        }
    return client.request("PATCH", path, query=query, payload=payload)


def event_delete(
    client: GoogleApiClient,
    config: GoogleServiceConfig,
    args: argparse.Namespace,
) -> Any:
    path = (
        f"/calendars/{_calendar_id(config, args.calendar_id)}/events/"
        f"{_event_id(args.id)}"
    )
    query = {"sendUpdates": args.send_updates}
    if args.dry_run:
        return {
            "dry_run": True,
            "request": {"method": "DELETE", "path": path, "query": query},
        }
    client.request("DELETE", path, query=query)
    return {"ok": True, "deleted": args.id}


def freebusy(
    client: GoogleApiClient,
    _config: GoogleServiceConfig,
    args: argparse.Namespace,
) -> Any:
    payload = {
        "timeMin": _rfc3339(args.time_min, "time-min"),
        "timeMax": _rfc3339(args.time_max, "time-max"),
        "items": [{"id": item.strip()} for item in args.calendar_id],
    }
    return client.request("POST", "/freeBusy", payload=payload)


def _common_calendar(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--calendar-id")


def _dry_run(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--dry-run", action="store_true")


def _mutation_fields(parser: argparse.ArgumentParser, *, create: bool) -> None:
    parser.add_argument("--summary", required=create)
    parser.add_argument("--start", required=create)
    parser.add_argument("--end", required=create)
    parser.add_argument("--all-day", action="store_true")
    parser.add_argument("--timezone")
    parser.add_argument("--description")
    parser.add_argument("--location")
    parser.add_argument("--attendee", action="append")
    parser.add_argument(
        "--recurrence",
        action="append",
        help="RRULE:, RDATE: ou EXDATE:; pode ser repetido.",
    )
    parser.add_argument(
        "--reminder",
        action="append",
        help="email:MINUTOS ou popup:MINUTOS; pode ser repetido.",
    )
    parser.add_argument("--use-default-reminders", action="store_true")
    if not create:
        parser.add_argument("--clear-attendees", action="store_true")
        parser.add_argument("--clear-recurrence", action="store_true")
        parser.add_argument("--clear-reminders", action="store_true")
    parser.add_argument(
        "--send-updates",
        choices=("all", "externalOnly", "none"),
        default="none",
    )
    _common_calendar(parser)
    _dry_run(parser)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Gerencia Google Calendar.")
    parser.add_argument("--config", default=str(DEFAULT_CONFIG))
    parser.add_argument("--profile")
    commands = parser.add_subparsers(dest="command", required=True)

    check = commands.add_parser("doctor")
    _common_calendar(check)
    check.set_defaults(handler=doctor)

    calendars = commands.add_parser("calendars")
    calendar_commands = calendars.add_subparsers(dest="operation", required=True)
    calendar_list = calendar_commands.add_parser("list")
    calendar_list.add_argument(
        "--min-access-role",
        choices=("freeBusyReader", "reader", "writer", "owner"),
    )
    calendar_list.add_argument("--show-hidden", action="store_true")
    calendar_list.add_argument("--page-token")
    calendar_list.add_argument("--all-pages", action="store_true")
    calendar_list.set_defaults(handler=calendars_list)

    events = commands.add_parser("events")
    event_commands = events.add_subparsers(dest="operation", required=True)
    event_list = event_commands.add_parser("list")
    _common_calendar(event_list)
    event_list.add_argument("--time-min")
    event_list.add_argument("--time-max")
    event_list.add_argument("--query")
    event_list.add_argument("--timezone")
    event_list.add_argument("--single-events", action=argparse.BooleanOptionalAction, default=True)
    event_list.add_argument("--order-by", choices=("startTime", "updated"), default="startTime")
    event_list.add_argument("--show-deleted", action="store_true")
    event_list.add_argument("--page-token")
    event_list.add_argument("--all-pages", action="store_true")
    event_list.set_defaults(handler=events_list)

    show = event_commands.add_parser("show")
    show.add_argument("--id", required=True)
    show.add_argument("--timezone")
    _common_calendar(show)
    show.set_defaults(handler=event_show)

    instances = event_commands.add_parser("instances")
    instances.add_argument("--id", required=True)
    instances.add_argument("--time-min")
    instances.add_argument("--time-max")
    instances.add_argument("--timezone")
    instances.add_argument("--show-deleted", action="store_true")
    instances.add_argument("--page-token")
    instances.add_argument("--all-pages", action="store_true")
    _common_calendar(instances)
    instances.set_defaults(handler=event_instances)

    create = event_commands.add_parser("create")
    _mutation_fields(create, create=True)
    create.add_argument("--google-meet", action="store_true")
    create.set_defaults(handler=event_create)

    update = event_commands.add_parser("update")
    update.add_argument("--id", required=True)
    _mutation_fields(update, create=False)
    update.set_defaults(handler=event_update)

    delete = event_commands.add_parser("delete")
    delete.add_argument("--id", required=True)
    delete.add_argument(
        "--send-updates",
        choices=("all", "externalOnly", "none"),
        default="none",
    )
    _common_calendar(delete)
    _dry_run(delete)
    delete.set_defaults(handler=event_delete)

    availability = commands.add_parser("freebusy")
    availability.add_argument("--time-min", required=True)
    availability.add_argument("--time-max", required=True)
    availability.add_argument("--calendar-id", action="append", required=True)
    availability.set_defaults(handler=freebusy)
    return parser


def print_json(value: Any, *, stream: Any = sys.stdout) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2), file=stream)


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8")
    args = build_parser().parse_args()
    access: GoogleAccess | None = None
    client: GoogleApiClient | None = None
    try:
        config = load_config(Path(args.config).expanduser().resolve())
        google_config = load_google_config(config.google_config)
        access = refresh_google_access(google_config, args.profile)
        require_google_scopes(access, CALENDAR_SCOPES, "Google Calendar")
        client = GoogleApiClient(
            config.api_base,
            access.access_token,
            "Calendar",
            timeout_seconds=config.timeout_seconds,
            max_response_bytes=config.max_response_bytes,
        )
        result = args.handler(client, config, args)
    except (
        CalendarToolError,
        GoogleApiError,
        GoogleAccountError,
        VaultToolError,
        ValueError,
        OSError,
    ) as exc:
        print_json(
            {"ok": False, "error": str(exc), "error_type": type(exc).__name__},
            stream=sys.stderr,
        )
        return 1
    finally:
        if client is not None:
            client.close()
        if access is not None:
            access.close()
    print_json(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
