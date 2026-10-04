# Python Calendar CLI

A small macOS command-line tool that calls Apple's EventKit directly through
[PyObjC](https://pyobjc.readthedocs.io/en/latest/apinotes/EventKit.html).
The command handling, validation, duplicate prevention and compatibility adapter
are Python. PyObjC supplies compiled bindings to the system frameworks; this is
not a wrapper around AppleScript or another calendar executable.

Calendar does not need to be open. The CLI has no activation commands, daemon,
HTTP client or telemetry. EventKit can sync changes through macOS's configured
calendar accounts.

## Setup

Requires macOS Sonoma 14 or newer and Python 3.10–3.14. Development and dependency
imports were checked on macOS 26.5.2 with Python 3.14.3. Earlier supported versions
have not been tested here. Writes were tested only with an offline fake backend.

```sh
git clone https://github.com/maralski/calendar-cli-python.git
cd calendar-cli-python
python3 -m venv .venv
.venv/bin/python -m pip install --only-binary=:all: -r requirements.txt
./calendar-cli --help
./calendar-cli status
```

The three pinned dependencies are PyObjC core, Cocoa and EventKit. No global
installation is required. Alternatively run `.venv/bin/python calendar_cli.py`.

## Calendar permission

`status` checks existing access without reading events. Other commands require
**Full Calendar Access**, because deduplication and safe deletion need reads.
Write-only access is insufficient. macOS grants access to the execution host,
not to a specific calendar or the `.py` source file. For example, running from
Terminal can require a Terminal grant; running under ChatGPT/Codex can use that
host's grant. A grant in one host does not guarantee a grant in another.

The CLI **never requests or accepts permission**. If `status` is not
`full-access`, review the relevant host in System Settings → Privacy & Security →
Calendars and approve Full Access yourself. If the host is not listed, use that
host's supported Calendar access flow to request consent; a bundled app may need
`NSCalendarsFullAccessUsageDescription`. Do not reset privacy settings or assume a
grant to Calendar itself grants this CLI access. Managed restrictions can prevent
access. Apple's permission grant covers all calendars; this CLI narrows each
event query to the calendar you explicitly select.

[Apple's EventKit access documentation](https://developer.apple.com/documentation/eventkit/accessing-calendar-using-eventkit-and-eventkitui)

## Select a calendar and timezone

List metadata, optionally filtered by calendar and source name:

```sh
./calendar-cli calendars --name Home --source icloud
```

Use the returned calendar **and source IDs** for repeatable selection. Names must
resolve to exactly one calendar; duplicated names fail. Source names are compared
case-insensitively, calendar names exactly. IDs can change after a full sync;
reselect explicitly if a pinned calendar disappears.

For convenience, save local defaults outside the repository, for example in
`~/.config/calendar-cli/config.json`:

```json
{
  "calendar_id": "CALENDAR_ID_FROM_YOUR_MAC",
  "source_id": "SOURCE_ID_FROM_YOUR_MAC",
  "timezone": "Australia/Melbourne"
}
```

Pass `--config` before the subcommand. The placeholders above are deliberately
not real IDs. Name selection also works using `--calendar Home --source icloud`.
The default timezone is UTC; choose an IANA zone explicitly for local schedules.
Naive timestamps use that zone. Explicit offsets identify exact instants and are
converted to the selected zone. Ambiguous DST times require an offset; nonexistent
local times are rejected.

## Read events

```sh
./calendar-cli events --calendar Home --source icloud \
  --start 2026-11-02 --end 2026-11-03 --timezone Australia/Melbourne
```

Windows must be positive and at most 31 days. Output is sorted JSON with titles,
times, locations and IDs, capped at 200 events by default (`--limit 1` through
`--limit 1000`). `truncated` reports omitted results. Notes, attendee identities,
management markers and internal fingerprints are not returned or logged.

## Create: preview first

All examples below are synthetic. This command reads JSON from stdin so event
content need not appear in shell arguments:

```sh
./calendar-cli create --calendar Home --source icloud \
  --timezone Australia/Melbourne <<'JSON'
{
  "title": "Example appointment",
  "start": "2026-11-02T18:00:00+11:00",
  "end": "2026-11-02T18:45:00+11:00",
  "key": "example-appointment-001"
}
JSON
```

This is a dry run: it reports `operation` and `dry_run: true` and does not create
events or a state database. Add **`--execute`** to commit. Optional JSON fields
are `location`, `notes` and `timezone`; alternatively use `--input FILE`.
There is no attendee, invitation, recurrence or all-day creation interface.

Keep each `key` stable for an event. Repeated calls find the same marked event.
A matching preexisting unmarked event is preserved and reported as
`existing-unmanaged`. Changed or duplicated marked events fail rather than being
overwritten. Previously recorded but missing events require reconciliation.

Successful executed operations store IDs and minimal metadata in a private local
SQLite database at `~/Library/Application Support/calendar-cli/state.sqlite3`.
`--state PATH`, before the subcommand, selects a different dedicated private
directory. That directory must be owned by you with mode 0700; files must be 0600.
Symlinks, hard-linked databases and unsafe SQLite sidecars are rejected. Keep
state and input/configuration files outside the repository.

## Delete: exact identity, preview first

Use the ID, start and end from your event listing and the original creation key:

```sh
./calendar-cli delete --calendar-id CALENDAR_ID_FROM_YOUR_MAC \
  --source-id SOURCE_ID_FROM_YOUR_MAC --event-id EVENT_ID_FROM_YOUR_MAC \
  --key example-appointment-001 \
  --expect-start 2026-11-02T18:00:00+11:00 \
  --expect-end 2026-11-02T18:45:00+11:00 --timezone Australia/Melbourne
```

Add `--execute` only after reviewing the preview. An absent ID is a no-op.
Deletion requires the exact calendar, source, event ID, management key and times.
Unmarked events, recurring events (including detached occurrences), all-day
events and events with attendees or an organizer are preserved. The backend
re-fetches and compares the event snapshot before deleting only `EKSpanThisEvent`.

## Booking-workflow compatibility

`ensure` and `remove` accept the established fitness confirmation JSON on stdin
or through `--input`. They require explicitly selected **iCloud Home** and use
Monday/Wednesday/Thursday 18:00–18:45 in Australia/Melbourne:

```sh
./calendar-cli ensure --calendar Home --source icloud <<'JSON'
{"date":"2026-11-02","confirmation":{"classId":"synthetic-class-123","status":"booked","newBooking":true}}
JSON
./calendar-cli remove --calendar Home --source icloud <<'JSON'
{"date":"2026-11-02","confirmation":{"classId":"synthetic-class-123","status":"cancelled","requestedCancellation":true}}
JSON
```

These also default to dry runs; add `--execute` only after the caller verifies a
new booking or requested cancellation. The CLI trusts this receipt; it does not
contact or authenticate a booking service. Waitlists are rejected. Legacy
`Managed-Key` and `Class-ID` notes are recognized. Existing unmarked events are
not adopted or deleted. The adapter retains confirmation input and marker
semantics, but emits this CLI's JSON fields (`id`, `external_id`, `dry_run`).
It is not an automatic replacement for another executable. Switch an existing
integration only after validating its output handling and execution flags.

## Tests, troubleshooting and security

```sh
.venv/bin/python -m unittest -v test_calendar_cli.py
./calendar-cli events --help
./calendar-cli create --help
./calendar-cli delete --help
```

Tests use synthetic events and a fake backend; they never access EventKit.
Malformed, duplicate-key, non-finite, over-64-KiB or over-16-level JSON is rejected.
Errors are JSON on stderr with exit code 2, without input bodies, tracebacks or
provider details. Successful commands emit JSON on stdout. Calendar IDs, titles
and locations in successful output remain private data; avoid sharing output.
The program does not create logs. Shell history or terminal capture is controlled
by your host.

If access fails, check `status` from the same execution host. If selection fails,
inspect metadata with `calendars` and update your local configuration. If state is
corrupt or a write outcome is uncertain, inspect the calendar before retrying;
do not blindly delete the state database. There is no automatic network retry.
Local serialization reduces races, but EventKit and iCloud sync are not part of a
single database transaction. See [SECURITY.md](SECURITY.md) for the reviewed scope
and remaining limits.
