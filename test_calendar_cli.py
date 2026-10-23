"""Offline tests. FakeBackend never imports EventKit or accesses the Mac calendar."""
from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import io
import json
import os
from pathlib import Path
import sqlite3
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import calendar_cli as cli


HOME = cli.Calendar("synthetic-home-id", "synthetic-source-id", "Home", "icloud", True)


class FakeBackend:
    def __init__(self, events=(), calendars=(HOME,)):
        self.items = list(events)
        self.calendar_items = list(calendars)
        self.created = []
        self.deleted = []
        self.queries = []

    def status(self):
        return {"authorization": "full-access", "full_access": True, "backend": "fake"}

    def calendars(self):
        return self.calendar_items

    def events(self, c, start, end):
        self.queries.append((c.id, start, end))
        return [e for e in self.items if e.calendar_id == c.id and e.start.timestamp() < end.timestamp() and e.end.timestamp() > start.timestamp()]

    def get(self, c, event_id):
        e = next((e for e in self.items if e.id == event_id), None)
        if e and e.calendar_id != c.id:
            raise cli.CalendarError("Wrong calendar")
        return e

    def create(self, c, spec):
        e = cli.Event("synthetic-created-id", "synthetic-external-id", c.id, spec["title"], spec["start"], spec["end"], spec.get("location", ""), spec["key"], spec.get("class_id", ""))
        self.items.append(e)
        self.created.append(e)
        return e

    def delete(self, c, expected):
        current = self.get(c, expected.id)
        if current != expected:
            raise cli.CalendarError("Changed since preview")
        self.deleted.append(expected)
        self.items.remove(expected)


class Tests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()
        self.state_path = self.root / "private" / "state.sqlite3"
        self.backend = FakeBackend()
        self.service = cli.Service(self.backend, cli.State(self.state_path))
        self.spec = cli.spec_from_json({"title": "Example appointment", "start": "2026-11-02T18:00:00+11:00", "end": "2026-11-02T18:45:00+11:00", "key": "example-appointment-001"}, "Australia/Melbourne")

    def tearDown(self):
        self.temp.cleanup()

    def event(self, **changes):
        value = cli.Event("synthetic-event-id", "synthetic-external-id", HOME.id, self.spec["title"], self.spec["start"], self.spec["end"], "", self.spec["key"])
        return replace(value, **changes)

    def args(self, command, extra=()):
        return cli.parser().parse_args(["--state", str(self.state_path), command, "--calendar-id", HOME.id, "--source-id", HOME.source_id, *extra])

    def test_create_is_preview_by_default_and_writes_no_state(self):
        result = self.service.apply(HOME, self.spec, False)
        self.assertTrue(result["dry_run"])
        self.assertEqual(result["operation"], "create")
        self.assertEqual(self.backend.created, [])
        self.assertFalse(self.state_path.parent.exists())

    def test_execute_creates_once_and_repeat_finds_same_event(self):
        self.service.apply(HOME, self.spec, True)
        again = self.service.apply(HOME, self.spec, True)
        self.assertEqual(again["operation"], "exists")
        self.assertEqual(len(self.backend.created), 1)
        self.assertEqual(self.state_path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.state_path.parent.stat().st_mode & 0o777, 0o700)

    def test_marker_recovers_after_id_change(self):
        self.service.apply(HOME, self.spec, True)
        self.backend.items[0] = replace(self.backend.items[0], id="synthetic-new-sync-id")
        self.assertEqual(self.service.apply(HOME, self.spec, True)["operation"], "exists")
        self.assertEqual(len(self.backend.created), 1)

    def test_missing_saved_event_is_not_recreated(self):
        self.service.apply(HOME, self.spec, True)
        self.backend.items.clear()
        with self.assertRaises(cli.CalendarError):
            self.service.apply(HOME, self.spec, True)
        self.assertEqual(len(self.backend.created), 1)

    def test_unmanaged_existing_event_is_preserved(self):
        self.backend.items = [self.event(key="")]
        self.assertEqual(self.service.apply(HOME, self.spec, False)["operation"], "existing-unmanaged")
        with self.assertRaises(cli.CalendarError):
            self.service.apply(HOME, self.spec, True, remove=True)
        self.assertEqual(self.backend.deleted, [])

    def test_duplicates_or_changed_events_fail_without_mutation(self):
        fixtures = [[self.event(), self.event(id="synthetic-second-id")], [self.event(title="Renamed")], [self.event(end=self.spec["end"] + timedelta(minutes=1))], [self.event(recurring=True)], [self.event(attendees=True)], [self.event(all_day=True)]]
        for events in fixtures:
            with self.subTest(events=events):
                self.backend.items = events
                with self.assertRaises(cli.CalendarError):
                    self.service.apply(HOME, self.spec, True, remove=True)
                self.assertEqual(self.backend.deleted, [])

    def test_readonly_calendar_cannot_mutate(self):
        with self.assertRaises(cli.CalendarError):
            self.service.apply(replace(HOME, writable=False), self.spec, True)
        self.assertFalse(self.state_path.exists())

    def test_delete_dryrun_then_execute_and_repeat(self):
        self.backend.items = [self.event()]
        self.assertTrue(self.service.apply(HOME, self.spec, False, True)["dry_run"])
        self.assertEqual(self.backend.deleted, [])
        self.service.apply(HOME, self.spec, True, True)
        self.assertEqual(len(self.backend.deleted), 1)
        self.assertEqual(self.service.apply(HOME, self.spec, True, True)["operation"], "absent")

    def test_explicit_event_id_cannot_switch_to_same_key_replacement(self):
        original = self.event(id="synthetic-requested-id")
        replacement = self.event(id="synthetic-replacement-id")
        self.backend.items = [replacement]
        # First read returns the original, subsequent decision sees replacement.
        self.backend.get = lambda c, identifier: original if identifier == original.id else None
        args = self.args("delete", ["--event-id", original.id, "--key", original.key, "--expect-start", original.start.isoformat(), "--expect-end", original.end.isoformat(), "--execute"])
        with self.assertRaises(cli.CalendarError):
            cli.run(args, self.backend)
        self.assertEqual(self.backend.deleted, [])

    def test_changed_between_decision_and_backend_commit_is_rejected(self):
        self.backend.items = [self.event()]
        original_delete = self.backend.delete
        def race(c, expected):
            self.backend.items[0] = replace(expected, revision="changed-private-notes")
            original_delete(c, expected)
        self.backend.delete = race
        with self.assertRaises(cli.CalendarError):
            self.service.apply(HOME, self.spec, True, True)
        self.assertEqual(self.backend.deleted, [])

    def test_wrong_calendar_or_ambiguous_names_are_rejected(self):
        for selector in ({"calendar_id": HOME.id, "source_id": "wrong-source"}, {"calendar": "Home"}, {"calendar": "Home", "source": 42}):
            with self.assertRaises(cli.CalendarError):
                cli.select([HOME], selector)
        with self.assertRaises(cli.CalendarError):
            cli.select([HOME, replace(HOME, id="synthetic-other-home")], {"calendar": "Home", "source": "iCloud"})

    def test_date_windows_and_dst_are_validated(self):
        tz = cli.zone("Australia/Melbourne")
        self.assertEqual(cli.parse_time("2026-10-05T18:00:00", tz).utcoffset(), timedelta(hours=11))
        for value in ("2026-10-04T02:30:00", "2027-04-04T02:30:00", "garbage", 42):
            with self.assertRaises(cli.CalendarError):
                cli.parse_time(value, tz)
        cli.parse_time("2027-04-04T02:30:00+11:00", tz)
        for seconds in (0, -1, 32 * 86400):
            with self.assertRaises(cli.CalendarError):
                cli.bounded(self.spec["start"], self.spec["start"] + timedelta(seconds=seconds))

    def test_iso_offset_equality_at_ambiguous_dst_time(self):
        local = cli.parse_time("2027-04-04T02:30:00+11:00", cli.zone("Australia/Melbourne"))
        self.assertTrue(cli.same_time(local, local.astimezone(timezone.utc)))

    def test_adversarial_json_is_bounded_and_duplicate_keys_rejected(self):
        for raw in ('[]', '{"key":1,"key":2}', '{"x":NaN}', '{"x":' + '[' * 2000 + '0' + ']' * 2000 + '}', '{"x":"' + 'a' * cli.MAX_INPUT + '"}'):
            with self.subTest(raw=raw[:30]), self.assertRaises(cli.CalendarError):
                cli.json_object(raw)

    def test_create_rejects_invites_recurrence_reserved_markers_and_controls(self):
        value = {"title": "Example", "start": "2026-11-02T18:00:00+11:00", "end": "2026-11-02T18:45:00+11:00", "key": "demo"}
        for bad in ({"attendees": ["test@example.invalid"]}, {"recurrence": "weekly"}, {"notes": "Managed-Key: forged"}, {"title": "line\nbreak"}, {"key": "nul\x00"}, {"location": 42}, {"notes": "x" * 32769}):
            with self.assertRaises(cli.CalendarError):
                cli.spec_from_json(dict(value, **bad), "UTC")

    def test_symlink_and_fifo_input_are_rejected(self):
        target = self.root / "target.json"
        target.write_text('{}')
        link = self.root / "link.json"
        link.symlink_to(target)
        fifo = self.root / "fifo"
        os.mkfifo(fifo)
        for path in (link, fifo):
            with self.assertRaises(cli.CalendarError):
                cli.input_object(path)

    def test_state_symlinks_hardlinks_sidecars_and_public_permissions_fail(self):
        private = self.root / "private"
        private.mkdir(mode=0o700)
        target = self.root / "target"
        target.write_text("sentinel")
        self.state_path.symlink_to(target)
        with self.assertRaises(cli.CalendarError):
            self.service.apply(HOME, self.spec, True)
        self.state_path.unlink()
        os.link(target, self.state_path)
        with self.assertRaises(cli.CalendarError):
            self.service.apply(HOME, self.spec, True)
        self.state_path.unlink()
        Path(str(self.state_path) + "-journal").symlink_to(target)
        with self.assertRaises(cli.CalendarError):
            self.service.apply(HOME, self.spec, True)
        Path(str(self.state_path) + "-journal").unlink()
        private.chmod(0o755)
        with self.assertRaises(cli.CalendarError):
            self.service.apply(HOME, self.spec, True)
        self.assertEqual(target.read_text(), "sentinel")
        self.assertEqual(self.backend.created, [])

    def test_state_parent_symlink_and_traversal_are_rejected(self):
        self.state_path.parent.symlink_to(self.root)
        with self.assertRaises(cli.CalendarError):
            self.service.apply(HOME, self.spec, True)
        with self.assertRaises(cli.CalendarError):
            cli.State(self.root / "child/../state.sqlite3").validate()

    def test_foreign_owned_ancestor_even_sticky_is_rejected(self):
        real_lstat = Path.lstat
        for mode in (0o40755, 0o41777):
            def lstat(path, *, follow_symlinks=False):
                if path == self.root:
                    return SimpleNamespace(st_mode=mode, st_uid=os.getuid() + 1, st_nlink=1)
                return real_lstat(path)
            with patch.object(Path, "lstat", lstat), self.assertRaises(cli.CalendarError):
                self.service.apply(HOME, self.spec, True)
        self.assertEqual(self.backend.created, [])

    def test_corrupt_state_is_not_overwritten(self):
        self.state_path.parent.mkdir(mode=0o700)
        self.state_path.write_text("not a database")
        self.state_path.chmod(0o600)
        with self.assertRaises(cli.CalendarError):
            self.service.apply(HOME, self.spec, True)
        self.assertEqual(self.backend.created, [])

    def test_permission_boundary_never_requests_access(self):
        backend = cli.EventKitBackend.__new__(cli.EventKitBackend)
        backend._store = None
        backend.status = lambda: {"full_access": False}
        with self.assertRaises(cli.CalendarError):
            _ = backend.store

    def test_event_output_excludes_bodies_markers_and_private_fingerprint(self):
        output = cli.public(self.event(revision="private-hash"), cli.zone("UTC"))
        self.assertNotIn("revision", output)
        self.assertNotIn("key", output)
        self.assertNotIn("notes", output)

    def test_class_compatibility_and_waitlist_safety(self):
        value = {"date": "2026-11-02", "confirmation": {"classId": "synthetic-class-123", "status": "booked", "newBooking": True}}
        spec = cli.legacy_spec(value, False)
        self.assertEqual((spec["end"] - spec["start"]).total_seconds(), 2700)
        self.assertEqual(spec["timezone"], "Australia/Melbourne")
        self.assertTrue(spec["key"].startswith("class-calendar:v1|"))
        for bad in ({"status": "waitlisted"}, {"newBooking": False}, {"classId": "bad\nmarker"}):
            with self.assertRaises(cli.CalendarError):
                cli.legacy_spec(dict(value, confirmation=dict(value["confirmation"], **bad)), False)
        for date in (42, "2026-11-03", "2026-02-30"):
            with self.assertRaises(cli.CalendarError):
                cli.legacy_spec(dict(value, date=date), False)
        for requested in (False, True):
            receipt = dict(value, confirmation={"classId": "synthetic-class-123", "status": "cancelled", "requestedCancellation": requested})
            if requested:
                cli.legacy_spec(receipt, True)
            else:
                with self.assertRaises(cli.CalendarError):
                    cli.legacy_spec(receipt, True)

    def test_provider_errors_are_redacted_by_cli(self):
        secret = "private-event-body-example"
        with patch.object(cli, "run", side_effect=RuntimeError(secret)), patch("sys.stderr", new_callable=io.StringIO) as error:
            self.assertEqual(cli.main(["status"]), 2)
            self.assertNotIn(secret, error.getvalue())
            self.assertNotIn("Traceback", error.getvalue())

    def test_native_record_flags_detached_invited_events_and_private_note_changes(self):
        backend = cli.EventKitBackend.__new__(cli.EventKitBackend)
        calendar = SimpleNamespace(calendarIdentifier=lambda: HOME.id)
        instant = SimpleNamespace(timeIntervalSince1970=lambda: self.spec["start"].timestamp())
        fields = {"notes": "Calendar-CLI-Key: demo\nprivate body", "URL": "https://example.invalid/private", "lastModifiedDate": instant,
                  "eventIdentifier": "synthetic-event-id", "calendarItemExternalIdentifier": "synthetic-external-id", "calendar": calendar,
                  "title": "Example", "startDate": instant, "endDate": instant, "location": "", "hasRecurrenceRules": False,
                  "isDetached": True, "occurrenceDate": None, "attendees": [], "organizer": object(), "isAllDay": False}
        event = SimpleNamespace(**{k: (lambda value=v: value) for k, v in fields.items()})
        record = backend._record(event)
        self.assertTrue(record.recurring)
        self.assertTrue(record.attendees)
        event.notes = lambda: "Calendar-CLI-Key: demo\nchanged private body"
        self.assertNotEqual(record.revision, backend._record(event).revision)
        self.assertNotIn("private body", json.dumps(cli.public(record, cli.zone("UTC"))))

    def test_native_record_treats_a_plain_event_with_an_occurrence_date_as_standalone(self):
        # EventKit returns the start date as occurrenceDate for every event, recurring or not.
        backend = cli.EventKitBackend.__new__(cli.EventKitBackend)
        calendar = SimpleNamespace(calendarIdentifier=lambda: HOME.id)
        instant = SimpleNamespace(timeIntervalSince1970=lambda: self.spec["start"].timestamp())
        fields = {"notes": "Calendar-CLI-Key: demo", "URL": None, "lastModifiedDate": instant,
                  "eventIdentifier": "synthetic-event-id", "calendarItemExternalIdentifier": "synthetic-external-id", "calendar": calendar,
                  "title": "Example", "startDate": instant, "endDate": instant, "location": "", "hasRecurrenceRules": False,
                  "isDetached": False, "occurrenceDate": instant, "attendees": [], "organizer": None, "isAllDay": False}
        event = SimpleNamespace(**{k: (lambda value=v: value) for k, v in fields.items()})
        self.assertFalse(backend._record(event).recurring)
        event.hasRecurrenceRules = lambda: True
        self.assertTrue(backend._record(event).recurring)

    def test_native_delete_does_not_accept_changed_snapshot_or_wrong_calendar(self):
        backend = cli.EventKitBackend.__new__(cli.EventKitBackend)
        backend._calendar = lambda c: SimpleNamespace(allowsContentModifications=lambda: True)
        changed = SimpleNamespace(refresh=lambda: True)
        backend._store = SimpleNamespace(eventWithIdentifier_=lambda identifier: changed)
        backend.status = lambda: {"full_access": True}
        backend._record = lambda e: self.event(revision="changed-notes")
        with self.assertRaises(cli.CalendarError):
            backend.delete(HOME, self.event())

    def test_oversized_stdin_and_malformed_configuration_do_not_mutate(self):
        with patch("sys.stdin", io.StringIO("x" * (cli.MAX_INPUT + 1))):
            with self.assertRaises(cli.CalendarError):
                cli.run(self.args("create"), self.backend)
        config = self.root / "bad-config.json"
        config.write_text('{"source":42,"calendar":"Home"}')
        args = cli.parser().parse_args(["--config", str(config), "events", "--start", "2026-11-02", "--end", "2026-11-03"])
        with self.assertRaises(cli.CalendarError):
            cli.run(args, self.backend)
        self.assertEqual(self.backend.created, [])
        self.assertEqual(self.backend.deleted, [])

    def test_documented_command_shapes_use_fake_backend(self):
        args = cli.parser().parse_args(["status"])
        self.assertTrue(cli.run(args, self.backend)["full_access"])
        args = cli.parser().parse_args(["calendars", "--name", "Home", "--source", "icloud"])
        self.assertEqual(len(cli.run(args, self.backend)["calendars"]), 1)
        args = self.args("events", ["--start", "2026-11-02", "--end", "2026-11-03", "--timezone", "Australia/Melbourne"])
        self.assertEqual(cli.run(args, self.backend)["events"], [])
        payload = {"title": "Example appointment", "start": "2026-11-02T18:00:00+11:00", "end": "2026-11-02T18:45:00+11:00", "key": "example-appointment-001"}
        for command, body in (("create", payload), ("ensure", {"date": "2026-11-02", "confirmation": {"classId": "synthetic-class-123", "status": "booked", "newBooking": True}}), ("remove", {"date": "2026-11-02", "confirmation": {"classId": "synthetic-class-123", "status": "cancelled", "requestedCancellation": True}})):
            with patch("sys.stdin", io.StringIO(json.dumps(body))):
                self.assertTrue(cli.run(self.args(command), self.backend)["dry_run"])
        self.assertEqual(self.backend.created, [])
        self.assertEqual(self.backend.deleted, [])


if __name__ == "__main__":
    unittest.main()
