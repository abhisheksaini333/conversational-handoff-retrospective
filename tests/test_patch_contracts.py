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
