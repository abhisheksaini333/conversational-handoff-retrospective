# Local operating guide

1. Run `make test`, then `make train`. The model artifact is ignored because it is reproducible generated state.
2. Run `make up`. Use `docker compose ps` and `docker compose logs --tail=30 rasa actions coordinator` to diagnose startup. Do not print resolved environment variables or `.env` in shared logs.
3. Run `make demo`. It uses random synthetic conversation IDs so repeated demonstrations do not share trackers.
4. Run `make evaluate`. Keep the development and held-out examples distinct. A low model score is an observed result, not a protocol failure.
5. Stop with `make down`. This does not delete persistent state. Do not use blanket Docker prune commands.

## API

Every endpoint other than `/health` requires `Authorization: Bearer <HANDOFF_TOKEN>`. Read the token privately from `.env`; do not paste it into repository files.

- `POST /handoffs`: conversation, event_id, text, intent, confidence, optional active_form and bounded context.
- `GET /tickets`: inspect synthetic tickets.
- `GET /conversations/{id}`: inspect ownership (`bot`, `pending` or `human`).
- `POST /tickets/{id}/complete`: `{"event_id":"a-stable-completion-id"}`.
- `POST /demo/desk`: `{"available":false}` injects an unavailable desk; restore with `true`.

Confidence must be finite and between zero and one. Message text is limited to 2,000 characters, IDs to 128, context to 20 messages, and HTTP bodies to 64 KiB. Unknown fields are rejected.

## Failure matrix

| Failure | Observable result | Recovery |
|---|---|---|
| Desk unavailable | 202 pending; Rasa stays unpaused | Restore desk; repeat the human request |
| Duplicate message | Stored receipt; same active ticket | No operator action |
| Mismatched human acknowledgement | Rejected; remains pending | Fix receiving-service protocol |
| Rasa unavailable during completion | 503; remains human-owned | Restore Rasa; retry same completion ID |
| Stale completion | 400; newer ticket preserved | Complete the active ticket |
| Missing/invalid credential | 401 | Use current private local token |
| Coordinator restart | SQLite state and receipts survive | Restart service with same volume |
| Rasa restart | Paused state, slots and form survive | Restart with the same tracker volume |
| User `/restart` during handoff | Guard refuses reset while human/pending ownership exists | Complete active ticket before restarting |

Automatic session expiry is disabled to prevent an idle timeout from resetting ownership. The demo deliberately restarts its Rasa container to verify recovery. After editing custom actions, run `docker compose restart actions`; their bind-mounted Python code is loaded at process startup.

This lab has no real customer traffic, provider calls, or production deployment. Historical container dependencies are intentionally isolated. For clean experiments use a different Compose project and named volume; preserve existing state unless its deletion is explicitly intended.
