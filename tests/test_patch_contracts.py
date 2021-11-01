from contextlib import closing
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
        with closing(sqlite3.connect(self.path)) as db, db:
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
        with closing(sqlite3.connect(self.path)) as db, db: db.execute("DROP TABLE conversations")
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
        with closing(sqlite3.connect(self.path)) as db, db:
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
        with closing(sqlite3.connect(self.path)) as db, db: db.execute("UPDATE conversations SET data='[]' WHERE id='broken'")
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

    def test_metrics_endpoint_contains_counts_without_transcripts(self):
        self.accepted()
        code, result, _ = self.http("/metrics", method="GET")
        self.assertEqual(code, 200)
        self.assertEqual(result["queue"]["human"], 1)
        self.assertNotIn("help", json.dumps(result))

    def test_audit_endpoint_uses_bounded_cursor(self):
        ticket = self.accepted()
        code, result, _ = self.http("/audit?limit=1&ticket_id="+ticket, method="GET")
        self.assertEqual(code, 200)
        self.assertEqual(len(result["items"]), 1)
        self.assertEqual(result["items"][0]["kind"], "created")

    def test_metadata_http_commands_preserve_revision_and_ownership(self):
        ticket = self.accepted()
        path = "/tickets/"+ticket
        code, result, _ = self.http(path+"/priority", {"event_id":"p", "priority":"high", "expected_revision":0})
        self.assertEqual(code, 200); self.assertEqual(result["revision"], 1)
        self.assertEqual(self.http(path+"/claim", {"event_id":"claim", "actor":"one"})[0], 200)
        self.assertEqual(self.http(path+"/transfer", {"event_id":"transfer", "actor":"one", "target":"two"})[1]["assignee"], "two")
        self.assertEqual(self.http(path+"/note", {"event_id":"note", "actor":"two", "text":"checked"})[0], 200)
        self.assertEqual(self.http(path+"/tags", {"event_id":"tags", "tags":["Billing"]})[1]["tags"], ["billing"])
        self.assertEqual(self.http(path+"/release", {"event_id":"release", "actor":"two"})[0], 200)
        self.assertEqual(self.http(path+"/claim", {"event_id":"claim2", "actor":"one", "unexpected":True})[0], 400)

    def test_http_cancellation_rejects_human_owned_tickets(self):
        pending = self.request("pending")["ticket_id"]
        self.assertEqual(self.http("/tickets/"+pending+"/cancel", {"event_id":"cancel", "reason":"withdrawn"})[0], 200)
        human = self.accepted()
        self.assertEqual(self.http("/tickets/"+human+"/cancel", {"event_id":"cancel", "reason":"withdrawn"})[0], 400)

    def test_queue_retry_http_dispatches_pending_tickets(self):
        ticket = self.request()["ticket_id"]
        code, result, _ = self.http("/queue/dispatch", {"limit":1})
        self.assertEqual(code, 200)
        self.assertEqual(result["accepted"], [ticket])
        self.assertEqual(self.http("/queue/dispatch", {"limit":1000})[0], 400)

    def test_http_ticket_summary_is_redacted_and_missing_is_404(self):
        ticket = self.request()["ticket_id"]
        code, result, _ = self.http("/tickets/"+ticket+"/summary", method="GET")
        self.assertEqual(code, 200); self.assertNotIn("context", result)
        self.assertEqual(self.http("/tickets/missing/summary", method="GET")[0], 404)

    def test_http_export_includes_hash_and_conversation(self):
        self.accepted()
        code, result, _ = self.http("/exports/c1", method="GET")
        self.assertEqual(code, 200)
        self.assertEqual(len(result["sha256"]), 64)
        self.assertEqual(result["data"]["conversation"]["id"], "c1")

    def test_http_retention_preview_is_read_only(self):
        ticket = self.accepted(); self.core.complete(ticket, "done")
        code, result, _ = self.http("/retention?before=9999999999", method="GET")
        self.assertEqual(code, 200); self.assertEqual(result["ticket_ids"], [ticket])
        self.assertTrue(self.core.ticket(ticket)["context"])

    def test_http_redaction_requires_matching_confirmation(self):
        ticket = self.accepted(); self.core.complete(ticket, "done")
        path = "/tickets/"+ticket+"/redact"
        self.assertEqual(self.http(path, {"confirm_ticket_id":"wrong"})[0], 400)
        self.assertEqual(self.http(path, {"confirm_ticket_id":ticket})[1]["redacted"], True)

    def test_signed_content_length_is_rejected(self):
        raw = b'{"available":true}'
        self.assertEqual(self.http("/demo/desk", raw, {"Content-Length":"+"+str(len(raw))})[0], 400)

    def test_slow_body_receives_request_timeout(self):
        import socket, threading
        from handoff.server import make_server
        server = make_server(self.core, "local-test-credential", port=0, request_timeout=.05)
        thread = threading.Thread(target=lambda: server.serve_forever(poll_interval=.01), daemon=True); thread.start()
        client = socket.create_connection(server.server_address, timeout=2)
        try:
            client.sendall(b"POST /demo/desk HTTP/1.0\r\nAuthorization: Bearer local-test-credential\r\nContent-Type: application/json\r\nContent-Length: 20\r\n\r\n{")
            self.assertIn(b" 408 ", client.recv(2048))
        finally:
            client.close(); server.shutdown(); server.server_close(); thread.join()

    def test_short_valid_json_body_does_not_change_desk_state(self):
        import socket, threading
        from handoff.server import make_server
        server = make_server(self.core, "local-test-credential", port=0)
        thread = threading.Thread(target=lambda: server.serve_forever(poll_interval=.01), daemon=True); thread.start()
        client = socket.create_connection(server.server_address, timeout=2)
        body = b'{"available":true}'
        try:
            client.sendall(b"POST /demo/desk HTTP/1.0\r\nAuthorization: Bearer local-test-credential\r\nContent-Type: application/json\r\nContent-Length: 99\r\n\r\n"+body)
            client.shutdown(socket.SHUT_WR)
            self.assertIn(b" 400 ", client.recv(2048))
        finally:
            client.close(); server.shutdown(); server.server_close(); thread.join()

    def test_duplicate_authorization_headers_are_not_ambiguous(self):
        import http.client, threading
        from handoff.server import make_server
        server = make_server(self.core, "local-test-credential", port=0)
        thread = threading.Thread(target=lambda: server.serve_forever(poll_interval=.01), daemon=True); thread.start()
        client = http.client.HTTPConnection(*server.server_address, timeout=2)
        try:
            client.putrequest("GET", "/tickets")
            client.putheader("Authorization", "Bearer local-test-credential")
            client.putheader("Authorization", "Bearer somebody-else")
            client.endheaders()
            response = client.getresponse(); response.read()
            self.assertEqual(response.status, 401)
        finally:
            client.close(); server.shutdown(); server.server_close(); thread.join()

    def test_unknown_post_route_is_404_without_a_body(self):
        self.assertEqual(self.http("/unknown", method="POST")[0], 404)

    def test_rasa_redirect_is_not_followed(self):
        import threading
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
        from handoff.server import make_server
        import http.client
        class Redirect(BaseHTTPRequestHandler):
            visits = 0
            def log_message(self, *args): pass
            def do_POST(self):
                type(self).visits += 1
                self.send_response(307); self.send_header("Location", "/stolen"); self.end_headers()
            def do_GET(self):
                type(self).visits += 1
                self.send_response(200); self.end_headers()
        target = ThreadingHTTPServer(("127.0.0.1", 0), Redirect)
        tt = threading.Thread(target=lambda: target.serve_forever(poll_interval=.01), daemon=True); tt.start()
        service = make_server(self.core, "local-test-credential", port=0, rasa_url="http://127.0.0.1:"+str(target.server_port), rasa_token="synthetic-token")
        st = threading.Thread(target=lambda: service.serve_forever(poll_interval=.01), daemon=True); st.start()
        ticket = self.accepted()
        client = http.client.HTTPConnection(*service.server_address, timeout=2)
        try:
            client.request("POST", "/tickets/"+ticket+"/complete", json.dumps({"event_id":"done"}), {"Authorization":"Bearer local-test-credential", "Content-Type":"application/json"})
            response = client.getresponse(); response.read()
            self.assertEqual(response.status, 503)
            self.assertEqual(Redirect.visits, 1)
            self.assertEqual(self.core.ticket(ticket)["state"], "human")
        finally:
            client.close(); service.shutdown(); service.server_close(); st.join(); target.shutdown(); target.server_close(); tt.join()

    def test_head_health_has_headers_and_no_payload(self):
        code, body, headers = self.http("/health", method="HEAD")
        self.assertEqual(code, 200); self.assertIsNone(body)
        self.assertGreater(int(headers["Content-Length"]), 0)

    def test_resume_timeout_configuration_is_validated(self):
        from handoff.server import make_server
        for timeout in [0, -1, 31, True, math.inf]:
            with self.subTest(timeout=timeout), self.assertRaises(ValueError):
                make_server(self.core, "local-test-credential", port=0, resume_timeout=timeout)
        server = make_server(self.core, "local-test-credential", port=0, resume_timeout=.25)
        server.server_close()

    def test_configurable_body_limit_rejects_oversized_payload(self):
        from handoff.server import make_server
        for limit in [0, True, 1048577]:
            with self.assertRaises(ValueError): make_server(self.core, "local-test-credential", port=0, body_limit=limit)
        server = make_server(self.core, "local-test-credential", port=0, body_limit=128)
        self.assertEqual(server.body_limit, 128)
        server.server_close()

    def cli(self, *arguments):
        import contextlib, io
        from handoff.cli import main
        out, error = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(error):
            code = main(["--database", self.path, *arguments])
        return code, json.loads(out.getvalue()) if out.getvalue() else None, error.getvalue()

    def test_cli_status_reports_durable_counts(self):
        self.request()
        code, result, error = self.cli("status")
        self.assertEqual(code, 0); self.assertFalse(error)
        self.assertEqual(result["tickets"], 1)

    def test_cli_tickets_supports_state_filter(self):
        self.accepted()
        self.request("pending")
        code, result, _ = self.cli("tickets", "--state", "pending", "--limit", "1")
        self.assertEqual(code, 0)
        self.assertEqual(result["items"][0]["conversation"], "pending")

    def test_cli_integrity_reports_database_check(self):
        code, result, _ = self.cli("integrity")
        self.assertEqual(code, 0); self.assertTrue(result["ok"])

    def test_cli_backup_creates_private_verified_copy(self):
        self.accepted()
        destination = str(Path(self.temp.name) / "cli-backup.db")
        code, result, _ = self.cli("backup", destination)
        self.assertEqual(code, 0); self.assertTrue(result["verified"])
        self.assertEqual(Path(destination).stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.cli("backup", destination)[0], 1)

    def test_desk_and_backup_close_sqlite_connections(self):
        import gc, warnings
        from handoff.server import SimulatedDesk
        gc.collect()
        with warnings.catch_warnings(record=True) as captured:
            warnings.simplefilter("always", ResourceWarning)
            desk = SimulatedDesk(self.path)
            ticket = self.request()["ticket_id"]
            desk.accept(self.core.ticket(ticket))
            self.core.backup(Path(self.temp.name) / "closed.db")
            del desk
            gc.collect()
        self.assertFalse([w for w in captured if "unclosed database" in str(w.message)])

    def test_cli_export_refuses_to_overwrite_an_existing_bundle(self):
        self.accepted()
        destination = str(Path(self.temp.name) / "export.json")
        code, result, _ = self.cli("export", "c1", destination)
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(Path(destination).read_text())["data"]["conversation"]["id"], "c1")
        self.assertEqual(self.cli("export", "c1", destination)[0], 1)


    def test_cli_retention_lists_only_closed_candidates(self):
        ticket = self.accepted(); self.core.complete(ticket, "done")
        code, result, _ = self.cli("retention", "--before", "9999999999")
        self.assertEqual(code, 0); self.assertEqual(result["ticket_ids"], [ticket])
        self.assertTrue(self.core.ticket(ticket)["context"])

    def test_cli_redaction_requires_matching_identifier(self):
        ticket = self.accepted(); self.core.complete(ticket, "done")
        self.assertEqual(self.cli("redact", ticket, "--confirm-ticket", "wrong")[0], 1)
        self.assertEqual(self.cli("redact", ticket, "--confirm-ticket", ticket)[0], 0)
        self.assertEqual(self.core.ticket(ticket)["context"], [])

    def test_audit_ticket_cursor_has_a_covering_lookup_index(self):
        ticket = self.accepted()
        with closing(sqlite3.connect(self.path)) as db, db:
            plan = str(db.execute("EXPLAIN QUERY PLAN SELECT sequence FROM audit WHERE ticket=? AND sequence>? ORDER BY sequence", (ticket,0)).fetchall())
        self.assertIn("audit_ticket_sequence", plan)
        self.assertEqual(len(self.core.audit_page(ticket_id=ticket)["items"]), 3)

    def test_operator_state_survives_restart(self):
        ticket = self.accepted()
        self.core.claim(ticket, "claim", "agent")
        self.core.set_priority(ticket, "priority", "urgent")
        self.core.add_note(ticket, "note", "agent", "Follow up")
        before = self.core.ticket(ticket)
        audit = self.core.audit_page(ticket_id=ticket)
        restored = Coordinator(self.path)
        self.assertEqual(restored.ticket(ticket), before)
        self.assertEqual(restored.audit_page(ticket_id=ticket), audit)

    def test_concurrent_claims_have_exactly_one_winner(self):
        ticket = self.accepted()
        def claim(actor):
            try: return self.core.claim(ticket, "claim-"+actor, actor)["assignee"]
            except ValueError: return None
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(claim, ["one", "two"]))
        winners = [x for x in results if x is not None]
        self.assertEqual(len(winners), 1)
        self.assertEqual(self.core.ticket(ticket)["assignee"], winners[0])

    def test_cancellation_during_dispatch_cannot_restore_human_ownership(self):
        ticket = self.request()["ticket_id"]
        core = self.core
        class RacingDesk:
            def accept(self, value):
                core.cancel(value["id"], "cancel", "withdrawn")
                return {"accepted": True, "ticket_id": value["id"]}
        result = self.core.dispatch(ticket, RacingDesk())
        self.assertEqual(result["state"], "bot")
        self.assertEqual(self.core.ticket(ticket)["state"], "cancelled")

    def test_resume_failure_leaves_no_partial_completion(self):
        ticket = self.accepted()
        audit = self.core.audit_page(ticket_id=ticket)
        counts = self.core.snapshot_counts()
        def failure(_): raise TimeoutError("uncertain response")
        with self.assertRaises(TimeoutError): self.core.complete(ticket, "done", failure)
        self.assertEqual(self.core.snapshot_counts(), counts)
        self.assertEqual(self.core.audit_page(ticket_id=ticket), audit)
        self.assertEqual(Coordinator(self.path).ticket(ticket)["state"], "human")

    def test_restored_backup_preserves_message_replay(self):
        original = self.request()
        self.core.dispatch(original["ticket_id"], Desk())
        destination = Path(self.temp.name) / "restore.db"
        self.core.backup(destination)
        restored = Coordinator(str(destination))
        replay = restored.message("c1", "e1", "help", "request_human", .9)
        self.assertEqual(replay, original)
        self.assertEqual(len(restored.tickets()), 1)
        self.assertEqual(restored.conversation("c1")["state"], "human")

    def test_metadata_replay_returns_its_original_revision(self):
        ticket = self.request()["ticket_id"]
        first = self.core.set_priority(ticket, "first", "low")
        self.core.set_priority(ticket, "second", "urgent")
        self.assertEqual(self.core.set_priority(ticket, "first", "low"), first)
        self.assertEqual(self.core.ticket(ticket)["priority"], "urgent")
        first["priority"] = "corrupted outside"
        self.assertEqual(self.core.set_priority(ticket, "first", "low")["priority"], "low")

    def test_old_cancellation_replay_does_not_clear_new_ticket(self):
        old = self.request()["ticket_id"]
        receipt = self.core.cancel(old, "cancel", "withdrawn")
        newer = self.request(event="new")["ticket_id"]
        self.assertEqual(self.core.cancel(old, "cancel", "withdrawn"), receipt)
        self.assertEqual(self.core.conversation("c1")["ticket_id"], newer)
        self.assertEqual(self.core.ticket(newer)["state"], "pending")

    def test_configured_body_budget_enforced_over_http(self):
        import http.client, threading
        from handoff.server import make_server
        server = make_server(self.core, "local-test-credential", port=0, body_limit=16)
        thread = threading.Thread(target=lambda: server.serve_forever(poll_interval=.01), daemon=True); thread.start()
        client = http.client.HTTPConnection(*server.server_address, timeout=2)
        try:
            client.request("POST", "/handoffs", json.dumps(self.payload()), {"Authorization":"Bearer local-test-credential", "Content-Type":"application/json"})
            response = client.getresponse(); response.read()
            self.assertEqual(response.status, 413)
            self.assertEqual(self.core.snapshot_counts()["tickets"], 0)
        finally:
            client.close(); server.shutdown(); server.server_close(); thread.join()
