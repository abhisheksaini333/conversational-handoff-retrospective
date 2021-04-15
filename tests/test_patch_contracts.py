import concurrent.futures
import copy
import json
import math
import sqlite3
import tempfile
import unittest
from pathlib import Path
from handoff.core import Coordinator, identifier

class Desk:
    def accept(self, ticket):
        return {"accepted": True, "ticket_id": ticket["id"]}

class PatchContracts(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = str(Path(self.temp.name) / "state.sqlite3")
        self.core = Coordinator(self.path)
    def request(self, conversation="c1", event="e1", **kw):
        return self.core.message(conversation, event, "help", "request_human", .9, **kw)
    def accepted(self, **kw):
        ticket = self.request(**kw)["ticket_id"]
        self.core.dispatch(ticket, Desk())
        return ticket

    def test_identifier_rejects_controls_and_surrogates(self):
        for value in ["x\n", "x\x00", "x\ud800", "x\u202e"]:
            with self.subTest(value=repr(value)), self.assertRaises(ValueError):
                identifier(value, "id")
        self.assertEqual(identifier("conversation-東京", "id"), "conversation-東京")

    def test_public_lookups_and_transitions_validate_identifiers(self):
        calls = [lambda: self.core.conversation(None), lambda: self.core.ticket(""),
                 lambda: self.core.dispatch([], Desk()), lambda: self.core.complete(None, "e")]
        for call in calls:
            with self.assertRaises(ValueError): call()

    def test_integral_confidence_replays_as_float(self):
        first = self.core.message("c", "e", "help", "request_human", 1)
        self.assertEqual(first, self.core.message("c", "e", "help", "request_human", 1.0))

    def test_adapter_cannot_mutate_dispatch_identity(self):
        ticket = self.request()["ticket_id"]
        class MutatingDesk:
            def accept(self, payload):
                answer = {"accepted": True, "ticket_id": payload["id"]}
                payload["conversation"] = "somebody-else"
                payload["context"].clear()
                return answer
        result = self.core.dispatch(ticket, MutatingDesk())
        self.assertEqual(result["state"], "human")
        self.assertTrue(self.core.ticket(ticket)["context"])

    def test_resume_callback_cannot_poison_completion_receipt(self):
        ticket = self.accepted()
        def mutate(result): result.update(conversation="other", state="human")
        answer = self.core.complete(ticket, "done", before_resume=mutate)
        self.assertEqual(answer["conversation"], "c1")
        self.assertEqual(answer["state"], "bot")
        self.assertEqual(self.core.complete(ticket, "done"), answer)

    def test_lookup_does_not_acquire_a_writer_lock(self):
        ticket = self.request()["ticket_id"]
        with sqlite3.connect(self.path) as db:
            db.execute("BEGIN IMMEDIATE")
            self.assertEqual(self.core.ticket(ticket)["id"], ticket)
            self.assertEqual(self.core.conversation("c1")["state"], "pending")
            self.assertEqual(len(self.core.tickets()), 1)

    def test_ephemeral_database_configuration_is_rejected(self):
        for database in ["", ":memory:"]:
            with self.subTest(database=database), self.assertRaises(ValueError):
                Coordinator(database)

    def test_busy_timeout_is_configurable_and_validated(self):
        core = Coordinator(self.path, busy_timeout=0.01)
        self.assertEqual(core.busy_timeout, 0.01)
        for invalid in [-1, 0, 61, True, math.inf, math.nan, "10"]:
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                Coordinator(self.path, busy_timeout=invalid)

    def http(self, path, body=None, headers=None, method="POST"):
        import http.client
        import threading
        from handoff.server import make_server
        token = "local-test-credential-only"
        server = make_server(self.core, token, port=0)
        thread = threading.Thread(target=lambda: server.serve_forever(poll_interval=.01), daemon=True)
        thread.start()
        client = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=2)
        actual = {"Authorization": "Bearer " + token, "Content-Type": "application/json"}
        actual.update(headers or {})
        try:
            raw = body if isinstance(body, bytes) else json.dumps(body).encode() if body is not None else None
            client.request(method, path, raw, actual)
            response = client.getresponse()
            payload = response.read()
            return response.status, json.loads(payload) if payload else None, dict(response.getheaders())
        finally:
            client.close(); server.shutdown(); server.server_close(); thread.join()
    def payload(self):
        return dict(conversation="web", event_id="web-1", text="help", intent="request_human", confidence=.9)

    def test_duplicate_json_fields_are_not_silently_overwritten(self):
        self.assertEqual(self.http("/demo/desk", b'{"available":false,"available":true}')[0], 400)
        self.assertEqual(self.http("/demo/desk", b'{"available":NaN}')[0], 400)

    def test_unsupported_media_type_and_charset_are_415(self):
        for content_type in ["text/plain", "application/json; charset=latin-1"]:
            self.assertEqual(self.http("/demo/desk", {"available": True}, {"Content-Type": content_type})[0], 415)
        self.assertEqual(self.http("/demo/desk", {"available": True}, {"Content-Type": "application/json; charset=utf-8"})[0], 200)

    def test_ambiguous_body_framing_is_rejected(self):
        status, _, _ = self.http("/demo/desk", {"available": True}, {"Transfer-Encoding": "chunked"})
        self.assertEqual(status, 400)

    def test_acknowledged_http_response_has_no_stale_bot_reply(self):
        status, result, _ = self.http("/handoffs", self.payload())
        self.assertEqual(status, 200)
        self.assertEqual(result["state"], "human")
        self.assertIsNone(result["bot_reply"])

    def test_unsupported_method_returns_json_405(self):
        code, body, headers = self.http("/handoffs", method="PUT")
        self.assertEqual(code, 405)
        self.assertIn("POST", headers["Allow"])
        self.assertEqual(body["error"], "method not allowed")

    def test_server_rejects_nonstring_or_multiline_credentials(self):
        from handoff.server import make_server
        for token in [12345678901234567, b"sixteencharacters", "line\nbreak-token-long", " " * 20]:
            with self.subTest(token=repr(token)), self.assertRaises(ValueError):
                make_server(self.core, token, port=0)

    def test_ticket_list_storage_failure_is_json_503(self):
        def unavailable(): raise sqlite3.OperationalError("private database filename")
        self.core.tickets = unavailable
        code, body, _ = self.http("/tickets", method="GET")
        self.assertEqual(code, 503)
        self.assertNotIn("private", str(body))

    def test_readiness_detects_missing_schema(self):
        self.assertEqual(self.http("/ready", method="GET")[0], 200)
        with sqlite3.connect(self.path) as db: db.execute("DROP TABLE conversations")
        self.assertEqual(self.http("/ready", method="GET")[0], 503)
        self.assertEqual(self.http("/health", method="GET")[0], 200)

    def test_temporary_storage_failure_includes_retry_after(self):
        def unavailable(): raise sqlite3.OperationalError("busy")
        self.core.tickets = unavailable
        self.assertEqual(self.http("/tickets", method="GET")[2]["Retry-After"], "1")

    def test_rasa_url_rejects_credentials_and_non_http_schemes(self):
        from handoff.server import make_server
        for url in ["file:///tmp/a", "http://user:pass@localhost", "http://localhost?token=x", "http://localhost/#frag", "relative"]:
            with self.subTest(url=url), self.assertRaises(ValueError):
                make_server(self.core, "local-test-credential", port=0, rasa_url=url)

    def test_lifecycle_clock_is_persisted(self):
        clock = [100.0]
        core = Coordinator(self.path, clock=lambda: clock[0])
        ticket = core.message("time", "e", "help", "request_human", .9)["ticket_id"]
        self.assertEqual(core.ticket(ticket)["created_at"], 100)
        clock[0] = 105
        core.dispatch(ticket, Desk())
        self.assertEqual(core.ticket(ticket)["accepted_at"], 105)
        clock[0] = 110
        core.complete(ticket, "done")
        self.assertEqual(core.ticket(ticket)["completed_at"], 110)

    def test_ticket_pages_have_no_overlap(self):
        ids = [self.request("c" + str(i))["ticket_id"] for i in range(3)]
        first = self.core.ticket_page(limit=2)
        second = self.core.ticket_page(limit=2, after=first["next_cursor"])
        self.assertEqual([t["id"] for t in first["items"] + second["items"]], ids)
        self.assertIsNone(second["next_cursor"])
        with self.assertRaises(ValueError): self.core.ticket_page(limit=True)

    def test_ticket_page_filters_do_not_skip_matching_rows(self):
        self.request("first")
        selected = self.request("second")["ticket_id"]
        self.core.dispatch(selected, Desk())
        result = self.core.ticket_page(state="human", conversation="second", reason="explicit", limit=1)
        self.assertEqual([t["id"] for t in result["items"]], [selected])
        with self.assertRaises(ValueError): self.core.ticket_page(state="invalid")

    def test_ticket_summary_omits_transcript_text(self):
        ticket = self.request()["ticket_id"]
        result = self.core.ticket_summary(ticket)
        self.assertEqual(result["context_messages"], 1)
        self.assertNotIn("context", result)
        self.assertEqual(result["state"], "pending")

    def test_snapshot_counts_durable_records(self):
        self.accepted()
        counts = self.core.snapshot_counts()
        self.assertEqual(counts, {"conversations": 1, "tickets": 1, "receipts": 1})

    def test_queue_metrics_separate_active_states(self):
        self.core = Coordinator(self.path, clock=lambda: 100)
        self.request("pending")
        self.accepted(conversation="human")
        self.core.clock = lambda: 125
        metrics = self.core.queue_metrics()
        self.assertEqual(metrics["pending"], 1)
        self.assertEqual(metrics["human"], 1)
        self.assertEqual(metrics["oldest_pending_seconds"], 25)

    def test_retry_metrics_count_failed_attempts(self):
        class FailedDesk:
            def accept(self, _): raise ConnectionError("offline")
        ticket = self.request()["ticket_id"]
        with self.assertRaises(ConnectionError): self.core.dispatch(ticket, FailedDesk())
        self.core.dispatch(ticket, Desk())
        self.assertEqual(self.core.retry_metrics(), {"attempts": 2, "retried_tickets": 1, "maximum_attempts": 2})

    def test_resolution_metrics_have_explicit_sample_counts(self):
        clock = [10]
        self.core = Coordinator(self.path, clock=lambda: clock[0])
        ticket = self.request()["ticket_id"]
        clock[0] = 15; self.core.dispatch(ticket, Desk())
        clock[0] = 23; self.core.complete(ticket, "done")
        metrics = self.core.resolution_metrics()
        self.assertEqual(metrics, {"samples": 1, "mean_wait_seconds": 5.0, "mean_handling_seconds": 8.0})

    def test_routing_metrics_distinguish_escalation_reasons(self):
        self.request()
        self.core.message("low", "e", "hi", "greet", .1)
        self.assertEqual(self.core.routing_metrics(), {"explicit": 1, "low_confidence": 1, "unknown": 0})

    def test_integrity_report_checks_database_pages(self):
        self.request()
        self.assertEqual(self.core.integrity(), {"ok": True, "checks": ["ok"]})

    def test_online_backup_restores_ownership_and_refuses_overwrite(self):
        ticket = self.accepted()
        path = Path(self.temp.name) / "backup.sqlite3"
        self.core.backup(path)
        restored = Coordinator(str(path))
        self.assertEqual(restored.ticket(ticket)["state"], "human")
        with self.assertRaises(FileExistsError): self.core.backup(path)
        self.assertEqual(restored.integrity()["ok"], True)

    def test_conversation_export_is_complete_and_missing_is_explicit(self):
        ticket = self.accepted()
        exported = self.core.export_conversation("c1")
        self.assertEqual(exported["conversation"]["state"], "human")
        self.assertEqual(exported["tickets"][0]["id"], ticket)
        exported["tickets"].clear()
        self.assertEqual(len(self.core.tickets()), 1)
        with self.assertRaises(KeyError): self.core.export_conversation("missing")

    def test_export_digest_detects_modified_content(self):
        import hashlib
        from handoff.core import canonical
        self.request()
        result = self.core.export_bundle("c1")
        self.assertEqual(result["sha256"], hashlib.sha256(canonical(result["data"]).encode()).hexdigest())
        self.assertEqual(result["algorithm"], "sha256")

    def test_pending_cancellation_is_replay_safe(self):
        ticket = self.request()["ticket_id"]
        first = self.core.cancel(ticket, "cancel-1", "user withdrew")
        self.assertEqual(first["state"], "bot")
        self.assertEqual(self.core.ticket(ticket)["state"], "cancelled")
        self.assertEqual(self.core.cancel(ticket, "cancel-1", "user withdrew"), first)
        with self.assertRaises(ValueError): self.core.cancel(ticket, "cancel-1", "different")
        other = self.accepted(conversation="other")
        with self.assertRaises(ValueError): self.core.cancel(other, "cancel-2", "user withdrew")

    def test_ticket_writes_leave_redacted_atomic_audit_entries(self):
        ticket = self.accepted()
        self.core.complete(ticket, "done")
        with sqlite3.connect(self.path) as db:
            rows = db.execute("SELECT kind,payload FROM audit ORDER BY sequence").fetchall()
        self.assertEqual([r[0] for r in rows], ["created", "delivery_attempt", "state:human", "state:completed"])
        self.assertNotIn("help", str(rows))
        self.assertNotIn("context", str(rows))

    def test_audit_cursor_is_stable_and_ticket_scoped(self):
        first = self.accepted()
        self.request("other")
        page = self.core.audit_page(ticket_id=first, limit=1)
        next_page = self.core.audit_page(ticket_id=first, after=page["next_cursor"])
        self.assertEqual(len(page["items"] + next_page["items"]), 3)
        self.assertTrue(all(r["ticket_id"] == first for r in next_page["items"]))

    def test_audit_summary_counts_state_changes(self):
        self.accepted()
        self.assertEqual(self.core.audit_summary(), {"created": 1, "delivery_attempt": 1, "state:human": 1})

    def test_priority_updates_are_revision_checked_and_replayable(self):
        ticket = self.request()["ticket_id"]
        result = self.core.set_priority(ticket, "p1", "urgent", expected_revision=0)
        self.assertEqual(result["priority"], "urgent")
        self.assertEqual(result["revision"], 1)
        self.assertEqual(self.core.set_priority(ticket, "p1", "urgent", expected_revision=0), result)
        with self.assertRaises(ValueError): self.core.set_priority(ticket, "p2", "low", expected_revision=0)
        with self.assertRaises(ValueError): self.core.set_priority(ticket, "p2", "invalid")

    def test_tags_are_deduplicated_and_bounded(self):
        ticket = self.request()["ticket_id"]
        result = self.core.set_tags(ticket, "tags", ["Billing", "billing", " urgent "])
        self.assertEqual(result["tags"], ["billing", "urgent"])
        with self.assertRaises(ValueError): self.core.set_tags(ticket, "bad", ["x" * 33])

    def test_note_retry_does_not_append_twice(self):
        ticket = self.request()["ticket_id"]
        result = self.core.add_note(ticket, "note1", "agent-a", "Checked the order")
        self.core.add_note(ticket, "note1", "agent-a", "Checked the order")
        self.assertEqual(len(self.core.ticket(ticket)["notes"]), 1)
        self.assertEqual(result["notes"][0]["actor"], "agent-a")
        with self.assertRaises(ValueError): self.core.add_note(ticket, "note1", "agent-b", "Changed")

    def test_claim_requires_acknowledged_unassigned_ticket(self):
        pending = self.request()["ticket_id"]
        with self.assertRaises(ValueError): self.core.claim(pending, "a", "operator")
        self.core.dispatch(pending, Desk())
        self.assertEqual(self.core.claim(pending, "a", "operator")["assignee"], "operator")
        with self.assertRaises(ValueError): self.core.claim(pending, "b", "another")

    def test_assignment_release_checks_current_actor(self):
        ticket = self.accepted()
        self.core.claim(ticket, "claim", "one")
        with self.assertRaises(ValueError): self.core.release(ticket, "release", "two")
        self.assertIsNone(self.core.release(ticket, "release", "one")["assignee"])
        self.assertEqual(self.core.conversation("c1")["state"], "human")

    def test_transfer_preserves_human_ownership(self):
        ticket = self.accepted()
        self.core.claim(ticket, "claim", "one")
        result = self.core.transfer(ticket, "transfer", "one", "two")
        self.assertEqual(result["assignee"], "two")
        self.assertEqual(self.core.conversation("c1")["state"], "human")
        with self.assertRaises(ValueError): self.core.transfer(ticket, "again", "one", "three")

    def test_pending_selection_prioritizes_urgency(self):
        first = self.request("first")["ticket_id"]
        urgent = self.request("urgent")["ticket_id"]
        self.core.set_priority(urgent, "priority", "urgent")
        self.assertEqual([t["id"] for t in self.core.pending_queue(limit=2)], [urgent, first])

    def test_dispatch_batch_continues_after_one_desk_failure(self):
        first = self.request("first")["ticket_id"]
        second = self.request("second")["ticket_id"]
        class PartialDesk:
            def accept(self, ticket):
                if ticket["id"] == first: raise ConnectionError("private endpoint")
                return {"accepted": True, "ticket_id": ticket["id"]}
        result = self.core.dispatch_pending(PartialDesk())
        self.assertEqual(result["accepted"], [second])
        self.assertEqual(result["failed"], [first])
        self.assertNotIn("private", str(result))

    def test_retention_preview_only_selects_old_closed_tickets(self):
        self.core = Coordinator(self.path, clock=lambda: 100)
        closed = self.accepted()
        self.core.complete(closed, "done")
        self.request("active")
        self.assertEqual(self.core.retention_preview(101)["ticket_ids"], [closed])
        self.assertEqual(self.core.retention_preview(99)["ticket_ids"], [])
        self.assertTrue(self.core.ticket(closed)["context"])

    def test_redaction_keeps_receipts_and_clears_closed_context(self):
        ticket = self.accepted()
        self.core.complete(ticket, "done")
        result = self.core.redact_ticket(ticket)
        self.assertTrue(result["redacted"])
        self.assertEqual(self.core.ticket(ticket)["context"], [])
        self.assertEqual(self.core.complete(ticket, "done")["state"], "bot")
        active = self.request("active")["ticket_id"]
        with self.assertRaises(ValueError): self.core.redact_ticket(active)

    def test_redacting_an_old_ticket_preserves_newer_conversation_context(self):
        first = self.accepted()
        self.core.complete(first, "done-first")
        second = self.request(event="second")["ticket_id"]
        self.core.dispatch(second, Desk()); self.core.complete(second, "done-second")
        prior = self.core.conversation("c1")["context"]
        self.core.redact_ticket(first)
        self.assertEqual(self.core.conversation("c1")["context"], prior)

    def test_corrupt_storage_is_reported_as_unavailable(self):
        self.request("broken")
        with sqlite3.connect(self.path) as db: db.execute("UPDATE conversations SET data='[]' WHERE id='broken'")
        with self.assertRaises(sqlite3.DatabaseError): self.core.conversation("broken")
        self.assertEqual(self.http("/conversations/broken", method="GET")[0], 503)

    def test_query_parameters_do_not_become_conversation_ids(self):
        self.accepted(conversation="query")
        code, result, _ = self.http("/conversations/query?view=state", method="GET")
        self.assertEqual(code, 200)
        self.assertEqual(result["state"], "human")
        self.assertEqual(self.http("/tickets?limit=1&limit=2", method="GET")[0], 400)

    def test_http_ticket_pages_apply_filters_and_limits(self):
        self.request("one"); self.request("two")
        code, result, _ = self.http("/tickets/page?limit=1&state=pending", method="GET")
        self.assertEqual(code, 200)
        self.assertEqual(len(result["items"]), 1)
        self.assertIsNotNone(result["next_cursor"])
        self.assertEqual(self.http("/tickets/page?limit=0", method="GET")[0], 400)
