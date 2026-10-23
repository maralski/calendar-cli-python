# Security scope

This is a focused application review with offline adversarial tests and an
independent static reviewer. It is not an exhaustive audit or a guarantee of
security. No real calendar writes were used for validation.

## Boundaries and controls

- The invoking user and macOS execution host are trusted. Calendar content,
  JSON input, local configuration and saved metadata are treated as potentially
  malformed. Dependencies, macOS and the user's already granted privacy access
  are outside the application boundary.
- No shell evaluation, external helper invocation, HTTP client, credential
  handling, permission requests or telemetry exist in the Python application.
  PyObjC and the system EventKit framework provide native access. macOS may sync
  calendar changes through configured accounts.
- Writes require `--execute`. Dry runs read but do not write calendar/state data.
  Selection requires an exact calendar/source ID pair or a unique name/source
  match. Event queries are limited to one selected calendar and a 31-day window.
- Creation adds no attendees, organizer, recurrence or all-day data. It writes a
  management marker; reserved marker injection in user notes is rejected.
  Generic deletion requires exact event ID, key and expected times. Recurring
  instances, organizer/attendee events and unmarked events are rejected.
- Deletion re-fetches and refreshes the EventKit object. Its private snapshot
  includes a hash of notes, URL and modification time, plus exposed event fields.
  The hash is neither printed nor persisted. An explicit deletion ID cannot be
  replaced by a same-key event that appears during the operation.
- Input is capped at 64 KiB, JSON nesting at 16 levels, notes at 32768 characters,
  titles at 1024 characters and keys at 512 characters. Duplicate keys,
  non-finite numbers, unsupported fields and invalid types/times are rejected.
  JSON files must be regular files; final-component symlinks and FIFOs fail.
- State is outside the source tree by default. A private owned 0700 directory
  contains a 0600 SQLite database. Symlink path components, parent traversal,
  hard-linked databases, unsafe sidecars, ancestors not owned by the user or
  trusted root, and writable-by-other-user ancestors
  (except sticky temporary roots) fail. SQL uses parameters. SQLite serializes
  executing CLI operations using the same state database.
- Errors contain fixed explanations, not event bodies or provider exception
  text. Notes and attendee identities are omitted from successful output. IDs,
  titles and locations are still sensitive output; users control terminal logs.
- Dependencies are limited to the pinned PyObjC core, Cocoa and EventKit wheels.
  No private IDs, event exports, local state, environments or credentials belong
  in source control. The ignore file is a convenience, not a secret scanner.

## Findings addressed

The independent review identified an exact-ID deletion race and unsafe state
symlink handling as high priority. Both received regression tests and fixes.
Additional hardening covers bounded/typed JSON, detached recurring instances,
organizer-only invitation state, DST ambiguity and private change fingerprints.

## Remaining limits

- A hostile process under the same user account can already read that user's
  files and may share the host's Calendar grant. Private directory checks do not
  defeat a malicious process running under the same UID, root or a compromised
  OS; SQLite cannot open via an anchored directory descriptor in this design.
- EventKit writes and the local state transaction are separate. A crash or state
  write failure after a successful calendar commit can leave an uncertain
  outcome. Markers aid recovery; inspect before retrying. Other devices and
  clients do not share the local lock. A change after the final refresh and before
  EventKit commit remains a race; there is no server compare-and-swap.
- IDs may change during sync. Markers and stored IDs help recover within the
  original time window. Missing/moved events fail closed rather than being
  recreated automatically. Configured calendar IDs must be reverified if lost.
- Management markers are ownership conventions, not cryptographic signatures.
  Anyone able to edit the calendar can copy one. Duplicate checks stop ambiguous
  matches, but cannot authenticate a forged standalone copy after the original
  disappears. Exact-ID deletion remains preferable.
- Full Calendar Access is an OS-wide calendar grant to the host. The CLI narrows
  its own queries; it cannot convert that grant into a Home-only permission.
- Compatibility across all documented macOS/Python versions and live write
  behavior have not been exhaustively tested. Dependency updates need renewed
  tests and review.

Report issues privately to the repository owner. Do not attach real event
content, account identifiers, tokens or terminal logs containing private data.
