# Support operations

All routes except `/health` and `/ready` require the configured bearer credential. This is one administrative credential, not per-agent or tenant authorization. `actor` and `target` identify the simulated operator; they do not authenticate a person.

## Inspect and recover

| Request | Behavior |
|---|---|
| `GET /tickets/page?limit=20&after=0&state=pending` | Bounded insertion-order pagination; optional `conversation` and `reason` filters |
| `GET /tickets/{id}/summary` | Lifecycle metadata without transcript text |
| `GET /metrics` | Record counts, pending age, ownership, retries, routing and resolution times |
| `GET /audit?limit=20&after=0&ticket_id={id}` | Stable audit cursor with optional ticket filter; no transcript or note text |
| `POST /queue/dispatch` with `{"limit":20}` | Retry an urgency-ordered pending batch; continue after individual failures |
| `GET /exports/{conversation}` | Conversation and ticket export with SHA-256 of canonical data |
| `GET /retention?before={unix_seconds}` | Preview closed-ticket retention candidates; no deletion |

`GET /tickets` retains its original unbounded response. Prefer pagination for operator tooling; limits are 1–100. Older records with no lifecycle timestamps remain unknown. Resolution means include only completed tickets with all timing fields. Aggregate metrics use separate reads, not a globally atomic snapshot.

## Ticket commands

Use `POST /tickets/{id}/{operation}` with UTF-8 JSON. Supply `event_id` for replay-safe commands. Reusing that ID with a different operation or payload fails. Optional `expected_revision` rejects stale metadata writes; revision starts at zero. Replay returns the original result even after later revisions.

| Operation | Additional fields | Requirement |
|---|---|---|
| `priority` | `priority`: `low`, `normal`, `high`, `urgent` | Active ticket |
| `tags` | `tags`: up to 10 strings | Active; normalized and deduplicated |
| `note` | `actor`, `text` | Active; at most 100 notes, 2,000 characters each |
| `claim` | `actor` | Acknowledged and unclaimed, or already assigned to this actor |
| `release` | `actor` | Current assignee |
| `transfer` | `actor`, `target` | Current assignee; different target |
| `cancel` | `reason` | Active pending ticket; no revision field |
| `complete` | No additional fields | Existing acknowledged completion protocol; no revision field |

Assignment never changes bot/human ownership. A late desk acknowledgement cannot revive a cancelled ticket.

`POST /tickets/{id}/redact` instead requires `{"confirm_ticket_id":"same-id"}` and a closed ticket. It clears transcript, notes and cancellation text from that ticket, its simulated-desk copy and its metadata-replay results, preserving identities, fingerprints and audit records. It clears conversation context only for the newest ticket when the bot owns the conversation. Backups, exports and the separate Rasa tracker are not redacted by this operation.

## Local CLI

The CLI requires an existing database and never contacts remote services:

```sh
python3 -m handoff.cli --database var/handoff.sqlite3 status
python3 -m handoff.cli --database var/handoff.sqlite3 tickets --state pending --limit 20
python3 -m handoff.cli --database var/handoff.sqlite3 integrity
python3 -m handoff.cli --database var/handoff.sqlite3 backup /absolute/path/new-backup.sqlite3
python3 -m handoff.cli --database var/handoff.sqlite3 export CONVERSATION /absolute/path/new-export.json
python3 -m handoff.cli --database var/handoff.sqlite3 retention --before UNIX_SECONDS
python3 -m handoff.cli --database var/handoff.sqlite3 redact TICKET --confirm-ticket TICKET
```

Backup/export destinations must not exist and are created with owner-only permissions. Online backups use SQLite's backup API and verify integrity. They include coordinator records and the simulated desk, but not the separate Rasa tracker database or Docker infrastructure. Test recovery under a new coordinator before replacing a live database.

## Transport and storage

Mutation bodies require a decimal `Content-Length`. Transfer encoding, ambiguous headers, duplicate JSON keys, nonfinite JSON values and unsupported content types are rejected. A total body-read deadline bounds trickling clients; request headers also have a socket inactivity timeout. Callback redirects are disabled; failed callbacks preserve human ownership. Temporary 503 responses include `Retry-After: 1`.

`make_server` accepts `request_timeout` (default 5 seconds), `resume_timeout` (default 3 seconds) and `body_limit` (default 65,536 bytes). Timeouts must be positive and at most 30 seconds; size limits are 1–1,048,576 bytes. `Coordinator` accepts `busy_timeout` (default 10 seconds, maximum 60) and an injectable clock for deterministic tests. Read-only queries do not reserve SQLite's writer lock.

`/health` reports process liveness. `/ready` reads coordinator tables; it does not establish that Rasa or another service is available. SQLite still serializes writers. Queue scans and retention previews remain local demo operations, not a scalable support platform.

Integrity checks now validate persisted records and active conversation/ticket links as well as SQLite pages. Closed historical tickets remain valid when their conversation has a newer handoff.

Ticket summaries include priority, revision, assignee and cancellation time. Free-text cancellation reasons, notes and transcripts remain excluded.
