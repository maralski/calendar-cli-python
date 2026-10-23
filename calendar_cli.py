#!/usr/bin/env python3
"""An on-demand macOS EventKit CLI. Writes require --execute; no permission prompts."""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import stat
import sys
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


class CalendarError(Exception):
    pass


MAX_INPUT = 65536


def text(value: Any, label: str, limit: int = 512, empty: bool = False) -> str:
    # splitlines() also breaks on U+2028/U+2029/U+0085/U+001C-1E, which could otherwise forge a notes marker line.
    if (not isinstance(value, str) or len(value) > limit or (not empty and not value.strip()) or (value and value.splitlines() != [value])
            or any(ord(c) < 32 or ord(c) == 127 for c in value)):
        raise CalendarError(f"Invalid {label}; use a bounded single-line string.")
    return value


def json_object(raw: str) -> dict:
    if len(raw.encode("utf-8")) > MAX_INPUT:
        raise CalendarError("JSON input exceeds the 64 KiB limit.")
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise CalendarError("Duplicate JSON object keys are not accepted.")
            result[key] = value
        return result
    try:
        value = json.loads(raw, object_pairs_hook=unique, parse_constant=lambda _: (_ for _ in ()).throw(CalendarError("Non-finite JSON numbers are not accepted.")))
    except (ValueError, RecursionError):
        raise CalendarError("Malformed or excessively nested JSON input.") from None
    if not isinstance(value, dict):
        raise CalendarError("Input must be a JSON object.")
    stack = [(value, 0)]
    while stack:
        item, depth = stack.pop()
        if depth > 16:
            raise CalendarError("JSON nesting exceeds the 16-level limit.")
        if isinstance(item, dict):
            stack.extend((child, depth + 1) for child in item.values())
        elif isinstance(item, list):
            stack.extend((child, depth + 1) for child in item)
    return value


def input_object(path: Path | None) -> dict:
    if path is None:
        try:
            raw = sys.stdin.read(MAX_INPUT + 1)
        except UnicodeError:
            raise CalendarError("Input must be UTF-8 JSON.") from None
        return json_object(raw)
    try:
        fd = os.open(path.expanduser(), os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                raise CalendarError("JSON input/configuration must be a regular file.")
            data = os.read(fd, MAX_INPUT + 1)
        finally:
            os.close(fd)
        return json_object(data.decode("utf-8"))
    except (OSError, UnicodeError):
        raise CalendarError("Cannot read a regular UTF-8 JSON file; symlink files are rejected.") from None


@dataclass(frozen=True)
class Calendar:
    id: str
    source_id: str
    name: str
    source: str
    writable: bool


@dataclass(frozen=True)
class Event:
    id: str
    external_id: str
    calendar_id: str
    title: str
    start: datetime
    end: datetime
    location: str = ""
    key: str = ""
    class_id: str = ""
    recurring: bool = False
    attendees: bool = False
    all_day: bool = False
    revision: str = ""  # Private in-memory fingerprint, never emitted or persisted.


def zone(name: str) -> ZoneInfo:
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError, TypeError):
        raise CalendarError("Use a valid IANA timezone, such as Australia/Melbourne.") from None


def parse_time(text: str, tz: ZoneInfo) -> datetime:
    if not isinstance(text, str) or len(text) > 128:
        raise CalendarError("Use a bounded ISO date/time string.")
    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        raise CalendarError("Use an ISO date/time, optionally with a UTC offset.") from None
    if dt.tzinfo is not None:
        return dt.astimezone(tz)
    choices = [dt.replace(tzinfo=tz, fold=f) for f in (0, 1)]
    valid = [c for c in choices if c.astimezone(timezone.utc).astimezone(tz).replace(tzinfo=None) == dt]
    if not valid:
        raise CalendarError("This local time does not exist during a DST transition.")
    if len({c.utcoffset() for c in valid}) > 1:
        raise CalendarError("This local time is ambiguous; supply an explicit UTC offset.")
    return valid[0]


def bounded(start: datetime, end: datetime) -> None:
    seconds = (end.astimezone(timezone.utc) - start.astimezone(timezone.utc)).total_seconds()
    if not 0 < seconds <= 31 * 86400:
        raise CalendarError("The time window must be positive and at most 31 days.")


def same_time(a: datetime, b: datetime) -> bool:
    return a.timestamp() == b.timestamp()


def select(calendars: list[Calendar], selector: dict[str, Any]) -> Calendar:
    cid, sid = selector.get("calendar_id"), selector.get("source_id")
    name, source = selector.get("calendar"), selector.get("source")
    for label, value in selector.items():
        if value is not None:
            text(value, label)
    if cid or sid:
        if not cid or not sid or name or source:
            raise CalendarError("Select with both calendar-id and source-id, or with both calendar and source names.")
        matches = [c for c in calendars if c.id == cid and c.source_id == sid]
    elif name and source:
        matches = [c for c in calendars if c.name == name and c.source.casefold() == source.casefold()]
    else:
        raise CalendarError("A calendar and its source must be explicitly selected.")
    if len(matches) != 1:
        raise CalendarError("Calendar selection is missing or ambiguous; use calendars to obtain exact IDs.")
    return matches[0]


def public(event: Event, tz: ZoneInfo) -> dict[str, Any]:
    # Notes, attendee identities and raw EventKit objects are never returned.
    return {"id": event.id, "external_id": event.external_id, "calendar_id": event.calendar_id,
            "title": event.title, "start": event.start.astimezone(tz).isoformat(),
            "end": event.end.astimezone(tz).isoformat(), "timezone": tz.key,
            "location": event.location, "managed": bool(event.key),
            "recurring": event.recurring, "has_attendees": event.attendees, "all_day": event.all_day}


class EventKitBackend:
    """Direct PyObjC bindings, not an osascript/native-helper subprocess wrapper."""
    def __init__(self) -> None:
        try:
            import EventKit
            from Foundation import NSDate, NSTimeZone
        except ImportError:
            raise CalendarError("PyObjC is missing. Install the pinned requirements in a macOS virtual environment.") from None
        self.ek, self.date, self.tz = EventKit, NSDate, NSTimeZone
        self._store = None

    def status(self) -> dict[str, Any]:
        value = int(self.ek.EKEventStore.authorizationStatusForEntityType_(self.ek.EKEntityTypeEvent))
        names = {0: "not-determined", 1: "restricted", 2: "denied", 3: "full-access", 4: "write-only"}
        return {"authorization": names.get(value, "unknown"), "full_access": value == 3,
                "backend": "PyObjC/EventKit", "prompts": False}

    @property
    def store(self):
        if not self.status()["full_access"]:
            raise CalendarError("Calendar Full Access is not granted to this execution host. No permission request was made.")
        if self._store is None:
            self._store = self.ek.EKEventStore.alloc().init()
        return self._store

    def calendars(self) -> list[Calendar]:
        return [Calendar(str(c.calendarIdentifier()), str(c.source().sourceIdentifier()), str(c.title()),
                         str(c.source().title()), bool(c.allowsContentModifications()))
                for c in self.store.calendarsForEntityType_(self.ek.EKEntityTypeEvent)]

    def _calendar(self, selected: Calendar):
        c = self.store.calendarWithIdentifier_(selected.id)
        if c is None or str(c.source().sourceIdentifier()) != selected.source_id:
            raise CalendarError("Calendar identity changed; select it again before proceeding.")
        return c

    def _record(self, e) -> Event:
        key = class_id = ""
        for line in str(e.notes() or "").splitlines():
            if line.startswith(("Calendar-CLI-Key: ", "Managed-Key: ")):
                key = line.split(": ", 1)[1]
            elif line.startswith("Class-ID: "):
                class_id = line[10:]
        to_dt = lambda d: datetime.fromtimestamp(float(d.timeIntervalSince1970()), timezone.utc)
        modified = e.lastModifiedDate()
        fingerprint = hashlib.sha256(json.dumps([str(e.notes() or ""), str(e.URL() or ""),
                                                  float(modified.timeIntervalSince1970()) if modified else None],
                                                 ensure_ascii=False).encode()).hexdigest()
        return Event(str(e.eventIdentifier()), str(e.calendarItemExternalIdentifier() or ""),
                     str(e.calendar().calendarIdentifier()), str(e.title() or ""), to_dt(e.startDate()),
                     to_dt(e.endDate()), str(e.location() or ""), key, class_id,
                     bool(e.hasRecurrenceRules() or e.isDetached()),
                     bool(e.attendees() or e.organizer()), bool(e.isAllDay()), fingerprint)

    def events(self, selected: Calendar, start: datetime, end: datetime) -> list[Event]:
        bounded(start, end)
        pred = self.store.predicateForEventsWithStartDate_endDate_calendars_(
            self.date.dateWithTimeIntervalSince1970_(start.timestamp()),
            self.date.dateWithTimeIntervalSince1970_(end.timestamp()), [self._calendar(selected)])
        return [self._record(e) for e in self.store.eventsMatchingPredicate_(pred)]

    def get(self, selected: Calendar, event_id: str) -> Event | None:
        e = self.store.eventWithIdentifier_(event_id)
        if e is None:
            return None
        if str(e.calendar().calendarIdentifier()) != selected.id:
            raise CalendarError("Event belongs to another calendar; no change.")
        self._calendar(selected)
        return self._record(e)

    def create(self, selected: Calendar, spec: dict[str, Any]) -> Event:
        c = self._calendar(selected)
        if not c.allowsContentModifications():
            raise CalendarError("Calendar is read-only.")
        e = self.ek.EKEvent.eventWithEventStore_(self.store)
        e.setCalendar_(c)
        e.setTitle_(spec["title"])
        e.setStartDate_(self.date.dateWithTimeIntervalSince1970_(spec["start"].timestamp()))
        e.setEndDate_(self.date.dateWithTimeIntervalSince1970_(spec["end"].timestamp()))
        e.setTimeZone_(self.tz.timeZoneWithName_(spec["timezone"]))
        e.setLocation_(spec.get("location", ""))
        e.setAllDay_(False)
        marker = "Managed-Key" if spec.get("class_id") else "Calendar-CLI-Key"
        lines = [f"{marker}: {spec['key']}"]
        if spec.get("class_id"):
            lines.append(f"Class-ID: {spec['class_id']}")
        if spec.get("notes"):
            lines.append(spec["notes"])
        e.setNotes_("\n".join(lines))
        ok, error = self.store.saveEvent_span_commit_error_(e, self.ek.EKSpanThisEvent, True, None)
        if not ok:
            # Do not print arbitrary provider error text which may contain private data.
            raise CalendarError("EventKit save failed; inspect the selected calendar before retrying.")
        return self._record(e)

    def delete(self, selected: Calendar, expected: Event) -> None:
        if not self._calendar(selected).allowsContentModifications():
            raise CalendarError("Calendar is read-only.")
        e = self.store.eventWithIdentifier_(expected.id)
        if e is None or not e.refresh() or self._record(e) != expected:
            raise CalendarError("Deletion target changed since preview; no change.")
        if expected.calendar_id != selected.id or expected.recurring or expected.attendees:
            raise CalendarError("Only standalone events without attendees can be deleted.")
        ok, error = self.store.removeEvent_span_commit_error_(e, self.ek.EKSpanThisEvent, True, None)
        if not ok:
            raise CalendarError("EventKit deletion failed; inspect the selected calendar before retrying.")


class State:
    """Local metadata outside the repository. Dry runs never create a file."""
    def __init__(self, path: Path) -> None:
        self.path = path.expanduser().absolute()
        self.db = None

    def validate(self, creating: bool = False) -> None:
        # Reject existing symlink components. A private final directory prevents
        # replacement by other users; hostile processes under the same UID are
        # outside this boundary (they already have this user's Calendar access).
        if ".." in self.path.parts:
            raise CalendarError("State paths may not contain parent traversal components.")
        for part in [*reversed(self.path.parents), self.path]:
            try:
                info = part.lstat()
            except FileNotFoundError:
                continue
            if stat.S_ISLNK(info.st_mode):
                raise CalendarError("State paths may not contain symlinks.")
            if info.st_uid not in (0, os.getuid()):
                raise CalendarError("State ancestors must be owned by you or trusted root.")
            if part != self.path and info.st_mode & 0o022 and not info.st_mode & stat.S_ISVTX:
                raise CalendarError("State ancestors may not be writable by other users.")
        if creating:
            self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if self.path.parent.exists():
            info = self.path.parent.lstat()
            if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
                raise CalendarError("State directory must be owned by you with mode 0700; use a dedicated private directory.")
        if self.path.exists():
            info = self.path.lstat()
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_nlink != 1 or info.st_mode & 0o077:
                raise CalendarError("State database must be a private, owned regular file with mode 0600 and no hard links.")
        for suffix in ("-journal", "-wal", "-shm"):
            extra = Path(str(self.path) + suffix)
            try:
                info = extra.lstat()
            except FileNotFoundError:
                continue
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_nlink != 1 or info.st_mode & 0o077:
                raise CalendarError("SQLite sidecars must be private owned regular files without links.")

    def get(self, calendar: Calendar, key: str) -> dict[str, Any] | None:
        self.validate()
        if self.db is None and not self.path.exists():
            return None
        db = self.db or sqlite3.connect(self.path.resolve().as_uri() + "?mode=ro", uri=True)
        try:
            row = db.execute("SELECT payload FROM events WHERE scope = ?", (calendar.id + "|" + key,)).fetchone()
            value = json_object(row[0]) if row else None
            if value is not None and (value.get("state") not in ("active", "cancelled") or not isinstance(value.get("event_id"), str) or not isinstance(value.get("class_id"), str)):
                raise CalendarError("Invalid local state record; no change.")
            return value
        except (sqlite3.Error, ValueError):
            raise CalendarError("Local state cannot be read; no calendar change.") from None
        finally:
            if db is not self.db:
                db.close()

    @contextmanager
    def transaction(self):
        self.validate(creating=True)
        if not self.path.exists():
            fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_RDWR, 0o600)
            os.close(fd)
        # Directories created above were not checked by the first pass.
        self.validate()
        # SQLite serializes CLI operations; EventKit itself is not part of this transaction.
        self.db = sqlite3.connect(self.path, timeout=1)
        try:
            self.db.execute("CREATE TABLE IF NOT EXISTS events (scope TEXT PRIMARY KEY, payload TEXT NOT NULL)")
            self.db.execute("BEGIN IMMEDIATE")
            yield
            self.db.commit()
        except sqlite3.Error:
            self.db.rollback()
            raise CalendarError("Local state update failed. Calendar outcome may be uncertain; inspect before retrying.") from None
        finally:
            self.db.close()
            self.db = None

    def put(self, calendar: Calendar, key: str, event: Event, status: str) -> None:
        value = {"event_id": event.id, "external_id": event.external_id, "class_id": event.class_id, "state": status}
        self.db.execute("INSERT OR REPLACE INTO events VALUES (?, ?)", (calendar.id + "|" + key, json.dumps(value)))


class Service:
    def __init__(self, backend, state: State) -> None:
        self.backend, self.state = backend, state

    def candidates(self, c: Calendar, spec: dict[str, Any]) -> tuple[list[Event], dict | None]:
        start = spec["start"].replace(hour=0, minute=0, second=0, microsecond=0)
        end = spec["end"].replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)
        events = self.backend.events(c, start, end)
        prior = self.state.get(c, spec["key"])
        if prior and prior.get("event_id"):
            known = self.backend.get(c, prior["event_id"])
            if known and all(e.id != known.id for e in events):
                events.append(known)
        return events, prior

    def decide(self, c: Calendar, spec: dict[str, Any], remove: bool = False) -> tuple[str, Event | None]:
        events, prior = self.candidates(c, spec)
        marked = [e for e in events if e.key == spec["key"]]
        def similar(e):
            name = (e.title.casefold().startswith("example-class") or e.title.casefold().startswith("example class")) if spec.get("class_id") else e.title == spec["title"]
            place = "example" in e.location.casefold() if spec.get("class_id") else True
            return name and place and same_time(e.start, spec["start"]) and e.key != spec["key"]
        unmarked = [e for e in events if similar(e)]
        if prior and prior.get("class_id", "") != spec.get("class_id", ""):
            raise CalendarError("Saved class identity differs from confirmation; no change.")
        if len(marked) > 1 or len(unmarked) > 1 or (marked and unmarked):
            raise CalendarError("Multiple matching events; no change.")
        if marked:
            e = marked[0]
            if prior and e.id != prior.get("event_id") and not (e.external_id and e.external_id == prior.get("external_id")):
                raise CalendarError("Marked event is not the one recorded in local state; reconcile before retrying.")
            if (not same_time(e.start, spec["start"]) or not same_time(e.end, spec["end"]) or e.title != spec["title"] or e.class_id != spec.get("class_id", "") or e.recurring or e.attendees or e.all_day or (spec.get("class_id") and "example" not in e.location.casefold())):
                raise CalendarError("Marked event changed or is not a safe standalone event; no change.")
            if spec.get("expected_id") and e.id != spec["expected_id"]:
                raise CalendarError("The exact requested event ID is no longer the deletion target; no change.")
            return ("delete" if remove else "exists"), e
        if unmarked:
            if remove:
                raise CalendarError("Matching event was not marked by this tool; it will not be deleted.")
            return "existing-unmanaged", unmarked[0]
        if prior and prior.get("state") == "active":
            raise CalendarError("Previously managed event is missing or moved; reconcile before retrying.")
        return ("absent" if remove else "create"), None

    def apply(self, c: Calendar, spec: dict, execute: bool, remove: bool = False) -> dict:
        if not c.writable:
            raise CalendarError("Calendar is read-only.")
        if execute:
            with self.state.transaction():
                op, event = self.decide(c, spec, remove)
                if op == "create":
                    event = self.backend.create(c, spec)
                    self.state.put(c, spec["key"], event, "active")
                elif op == "delete":
                    # The backend re-fetches and validates the exact snapshot before removal.
                    self.backend.delete(c, event)
                    self.state.put(c, spec["key"], event, "cancelled")
                elif op == "exists":
                    self.state.put(c, spec["key"], event, "active")
        else:
            op, event = self.decide(c, spec, remove)
        return {"operation": op, "dry_run": not execute, "calendar": asdict(c),
                "event": public(event, zone(spec["timezone"])) if event else {},
                "planned": {"title": spec["title"], "start": spec["start"].isoformat(), "end": spec["end"].isoformat(), "timezone": spec["timezone"]}}


def spec_from_json(value: dict, timezone_name: str) -> dict:
    if not isinstance(value, dict):
        raise CalendarError("Input must be a JSON object.")
    forbidden = set(value) - {"title", "start", "end", "location", "key", "notes", "timezone"}
    if forbidden:
        raise CalendarError("Unsupported create fields; attendees, recurrence and invitations are not supported.")
    tz = zone(value.get("timezone", timezone_name))
    text(value.get("title"), "title", 1024)
    text(value.get("key"), "idempotency key")
    text(value.get("location", ""), "location", 2048, empty=True)
    if not isinstance(value.get("notes", ""), str) or len(value.get("notes", "")) > 32768 or "\x00" in value.get("notes", ""):
        raise CalendarError("Notes must be a string of at most 32768 characters without NUL.")
    notes = value.get("notes", "")
    if any(line.startswith(("Calendar-CLI-Key:", "Managed-Key:", "Class-ID:")) for line in notes.splitlines()):
        raise CalendarError("Notes may not contain reserved management markers.")
    start, end = parse_time(value.get("start"), tz), parse_time(value.get("end"), tz)
    bounded(start, end)
    # Candidate lookup covers complete local days and must also remain bounded.
    if (end.date() - start.date()).days > 29:
        raise CalendarError("A created event must span fewer than 30 local days.")
    return dict(value, start=start, end=end, timezone=tz.key)


def legacy_spec(value: dict, remove: bool) -> dict:
    if not isinstance(value, dict) or not isinstance(value.get("confirmation"), dict):
        raise CalendarError("The established confirmation JSON object is required.")
    receipt = value["confirmation"]
    status = "cancelled" if remove else "booked"
    required = "requestedCancellation" if remove else "newBooking"
    if receipt.get("status") != status or receipt.get(required) is not True:
        raise CalendarError("Only a NEW confirmed booking or confirmed requested cancellation is accepted; waitlists are excluded.")
    class_id = receipt.get("classId")
    if not isinstance(class_id, str) or not class_id or len(class_id) > 200 or any(c not in "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789._:-" for c in class_id):
        raise CalendarError("A valid confirmed classId is required.")
    date = text(value.get("date"), "date", 10)
    start = parse_time(date + "T18:00:00", zone("Australia/Melbourne"))
    if start.strftime("%Y-%m-%d") != date or start.weekday() not in (0, 2, 3):
        raise CalendarError("Use a Monday, Wednesday or Thursday class date as YYYY-MM-DD.")
    key = f"class-calendar:v1|example|example-class|{date}|18:00|Australia/Melbourne"
    return {"title": "Example class", "start": start, "end": start + timedelta(minutes=45),
            "timezone": "Australia/Melbourne", "location": "1 Example Street, Example VIC 3000", "key": key, "class_id": class_id}


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--config", type=Path, help="Local JSON defaults outside the repository")
    p.add_argument("--state", type=Path, default=Path.home() / "Library/Application Support/calendar-cli/state.sqlite3", help="Local metadata database; never created by dry runs")
    sub = p.add_subparsers(dest="command", required=True)
    sub.add_parser("status", help="Existing EventKit permission status; no prompts")
    calendars = sub.add_parser("calendars", help="Calendar/source metadata only")
    calendars.add_argument("--name")
    calendars.add_argument("--source")
    for name in ("events", "create", "delete", "ensure", "remove"):
        s = sub.add_parser(name)
        s.add_argument("--calendar-id")
        s.add_argument("--source-id")
        s.add_argument("--calendar", help="Exact calendar name, requires --source")
        s.add_argument("--source", help="Account/source name")
        s.add_argument("--timezone", default=None, help="IANA timezone; default UTC, class adapter uses Melbourne")
        if name == "events":
            s.add_argument("--start", required=True)
            s.add_argument("--end", required=True)
            s.add_argument("--limit", type=int, default=200)
        else:
            s.add_argument("--execute", action="store_true", help="Commit the operation; omitted means preview only")
            if name in ("create", "ensure", "remove"):
                s.add_argument("--input", type=Path, help="JSON input file; omitted reads stdin")
            else:
                s.add_argument("--event-id", required=True)
                s.add_argument("--expect-start", required=True, help="Expected event start time, checked before deletion")
                s.add_argument("--expect-end", required=True, help="Expected event end time, checked before deletion")
                s.add_argument("--key", required=True, help="Exact tool management key; unmarked events are preserved")
    return p


def run(args, backend=None) -> dict:
    backend = backend or EventKitBackend()
    if args.command == "status":
        return backend.status()
    settings = {}
    if args.config:
        settings = input_object(args.config)
        if set(settings) - {"calendar_id", "source_id", "calendar", "source", "timezone"}:
            raise CalendarError("Unknown local configuration fields.")
    calendars = backend.calendars()
    if args.command == "calendars":
        for label, value in (("name", args.name), ("source", args.source)):
            if value is not None:
                text(value, label)
        return {"calendars": [asdict(c) for c in calendars if (not args.name or c.name == args.name) and (not args.source or c.source.casefold() == args.source.casefold())]}
    selector = {key: getattr(args, key, None) or settings.get(key) for key in ("calendar_id", "source_id", "calendar", "source")}
    # Explicit ID or name flags replace the opposite configuration selector.
    if args.calendar_id or args.source_id:
        selector["calendar"] = selector["source"] = None
    elif args.calendar or args.source:
        selector["calendar_id"] = selector["source_id"] = None
    c = select(calendars, selector)
    timezone_name = args.timezone or settings.get("timezone", "UTC")
    tz = zone(timezone_name)
    if args.command == "events":
        if not 1 <= args.limit <= 1000:
            raise CalendarError("Event limit must be 1–1000.")
        start, end = parse_time(args.start, tz), parse_time(args.end, tz)
        bounded(start, end)
        events = sorted(backend.events(c, start, end), key=lambda e: (e.start.timestamp(), e.id))
        return {"calendar": asdict(c), "timezone": tz.key, "events": [public(e, tz) for e in events[:args.limit]], "truncated": len(events) > args.limit}
    service = Service(backend, State(args.state))
    if args.command in ("create", "ensure", "remove"):
        value = input_object(args.input)
        spec = spec_from_json(value, timezone_name) if args.command == "create" else legacy_spec(value, args.command == "remove")
        if args.command in ("ensure", "remove") and (c.name != "Home" or c.source.casefold() != "icloud"):
            raise CalendarError("The compatibility adapter requires the exact selected iCloud Home calendar.")
        return service.apply(c, spec, args.execute, args.command == "remove")
    text(args.event_id, "event ID")
    text(args.key, "management key")
    event = backend.get(c, args.event_id)
    if event is None:
        return {"operation": "absent", "dry_run": not args.execute}
    if not c.writable or event.key != args.key or not event.key or event.recurring or event.attendees or event.all_day:
        raise CalendarError("Only an exact marked standalone event in a writable calendar can be deleted.")
    start, end = parse_time(args.expect_start, tz), parse_time(args.expect_end, tz)
    if not same_time(event.start, start) or not same_time(event.end, end):
        raise CalendarError("Event times differ from the requested deletion; no change.")
    spec = {"title": event.title, "start": start, "end": end, "timezone": tz.key, "key": event.key, "class_id": event.class_id, "expected_id": args.event_id}
    return service.apply(c, spec, args.execute, True)


def main(argv=None) -> int:
    args = parser().parse_args(argv)
    try:
        result = run(args)
        print(json.dumps(result, ensure_ascii=True, indent=2))
        return 0
    except CalendarError as error:
        print(json.dumps({"error": str(error)}, ensure_ascii=True), file=sys.stderr)
        return 2
    except (OSError, sqlite3.Error):
        print(json.dumps({"error": "Local file/state operation failed; inspect before retrying any executed write."}), file=sys.stderr)
        return 2
    except Exception:
        # No provider exception repr/traceback: these can contain event bodies,
        # account names and local paths. Tests expose bugs by calling run directly.
        print(json.dumps({"error": "Operation failed. No provider details were logged; inspect before retrying an executed write."}), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
