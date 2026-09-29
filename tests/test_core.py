import concurrent.futures
import math
import tempfile
import unittest
from pathlib import Path
from handoff.core import Coordinator


class Desk:
    def __init__(self):
        self.tickets = {}
        self.fail = False

    def accept(self, ticket):
        if self.fail:
            raise ConnectionError("desk unavailable")
        self.tickets.setdefault(ticket["id"], ticket)
        return {"ticket_id": ticket["id"], "accepted": True}


class HandoffTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.database = str(Path(self.temp.name) / "handoff.sqlite3")
        self.core = Coordinator(self.database)
        self.desk = Desk()

    def tearDown(self):
        self.temp.cleanup()

    def request(self, event_id="m1", confidence=0.99, intent="request_human", **kwargs):
        return self.core.message("conversation-1", event_id, "please help", intent, confidence, **kwargs)

    def test_explicit_request_is_pending_until_human_acknowledges(self):
        result = self.request()
        self.assertEqual(result["state"], "pending")
        self.assertTrue(result["ticket_id"])
        acknowledged = self.core.dispatch(result["ticket_id"], self.desk)
        self.assertEqual(acknowledged["state"], "human")

    def test_low_confidence_routes_to_human_but_confident_message_stays_bot(self):
        self.assertEqual(self.request(confidence=0.9, intent="order_status")["state"], "bot")
        self.assertEqual(self.request("m2", confidence=0.2, intent="order_status")["state"], "pending")

    def test_duplicate_delivery_is_exact_replay_without_duplicate_ticket(self):
        first = self.request()
        self.core.dispatch(first["ticket_id"], self.desk)
        self.assertEqual(self.request(), first)
        self.assertEqual(len(self.core.tickets()), 1)

    def test_omitted_and_empty_context_cannot_reuse_an_event_id(self):
        self.request()
        with self.assertRaises(ValueError):
            self.request(context=[])

    def test_different_event_while_pending_reuses_ticket(self):
        first = self.request()
        second = self.request("m2")
        self.assertEqual(first["ticket_id"], second["ticket_id"])
        self.assertEqual(len(self.core.tickets()), 1)

    def test_restart_preserves_ticket_and_interrupted_form(self):
        first = self.request(active_form="order_form", context=[{"role": "user", "text": "order 123"}])
        self.core = Coordinator(self.database)
        self.core.dispatch(first["ticket_id"], self.desk)
        result = self.core.complete(first["ticket_id"], "complete-1")
        self.assertEqual(result["state"], "bot")
        self.assertEqual(result["resume_form"], "order_form")
        self.assertEqual(self.desk.tickets[first["ticket_id"]]["context"][0]["text"], "order 123")

    def test_desk_failure_keeps_pending_then_retry_succeeds(self):
        ticket = self.request()["ticket_id"]
        self.desk.fail = True
        with self.assertRaises(ConnectionError):
            self.core.dispatch(ticket, self.desk)
        self.assertEqual(self.core.conversation("conversation-1")["state"], "pending")
        self.desk.fail = False
        self.assertEqual(self.core.dispatch(ticket, self.desk)["state"], "human")

    def test_acknowledgement_must_match_ticket(self):
        ticket = self.request()["ticket_id"]
        class WrongDesk:
            def accept(self, payload):
                return {"accepted": True, "ticket_id": "wrong"}
        with self.assertRaises(ValueError):
            self.core.dispatch(ticket, WrongDesk())
        self.assertEqual(self.core.conversation("conversation-1")["state"], "pending")

    def test_duplicate_dispatch_does_not_create_second_human_ticket(self):
        ticket = self.request()["ticket_id"]
        self.core.dispatch(ticket, self.desk)
        self.core.dispatch(ticket, self.desk)
        self.assertEqual(len(self.desk.tickets), 1)

    def test_human_owned_conversation_suppresses_bot_reply(self):
        ticket = self.request()["ticket_id"]
        self.core.dispatch(ticket, self.desk)
        result = self.request("m2", intent="order_status")
        self.assertEqual(result["state"], "human")
        self.assertIsNone(result["bot_reply"])

    def test_completion_is_idempotent(self):
        ticket = self.request()["ticket_id"]
        self.core.dispatch(ticket, self.desk)
        result = self.core.complete(ticket, "complete-1")
        self.assertEqual(self.core.complete(ticket, "complete-1"), result)
        self.assertEqual(self.core.conversation("conversation-1")["state"], "bot")

    def test_pending_ticket_cannot_complete(self):
        with self.assertRaises(ValueError):
            self.core.complete(self.request()["ticket_id"], "c1")

    def test_failed_resume_callback_keeps_human_ownership(self):
        ticket = self.request()["ticket_id"]
        self.core.dispatch(ticket, self.desk)
        def fail(_):
            raise ConnectionError("Rasa unavailable")
        with self.assertRaises(ConnectionError):
            self.core.complete(ticket, "c1", before_resume=fail)
        self.assertEqual(self.core.conversation("conversation-1")["state"], "human")
        self.core.complete(ticket, "c1")

    def test_completed_ticket_cannot_resume_a_later_handoff(self):
        first = self.request()["ticket_id"]
        self.core.dispatch(first, self.desk)
        self.core.complete(first, "c1")
        second = self.request("m2")["ticket_id"]
        with self.assertRaises(ValueError):
            self.core.complete(first, "different-event")
        self.assertEqual(self.core.conversation("conversation-1")["ticket_id"], second)

    def test_concurrent_messages_only_create_one_ticket(self):
        def send(n):
            return Coordinator(self.database).message("shared", "event-"+str(n), "help", "request_human", .9)
        with concurrent.futures.ThreadPoolExecutor(max_workers=5) as pool:
            results = list(pool.map(send, range(10)))
        self.assertEqual(len({r["ticket_id"] for r in results}), 1)

    def test_second_handoff_uses_fresh_tracker_context(self):
        first = self.request(context=[{"role": "user", "text": "OLD ISSUE"}])["ticket_id"]
        self.core.dispatch(first, self.desk)
        self.core.complete(first, "c1")
        fresh = [{"role": "user", "text": "NEW ISSUE ORDER-9999"}, {"role": "assistant", "text": "Checking new order"}]
        result = self.request("m2", context=fresh)
        self.assertEqual(self.core.ticket(result["ticket_id"])["context"][:-1], fresh)
        self.assertEqual(self.request("m2", context=fresh), result)

    def test_invalid_input_rejected_without_side_effects(self):
        for confidence in [-1, 2, math.nan, math.inf, "0.1", True]:
            with self.subTest(confidence=confidence), self.assertRaises(ValueError):
                self.request(confidence=confidence)
        with self.assertRaises(ValueError):
            self.core.message("", "e1", "hi", "greet", .9)
        with self.assertRaises(ValueError):
            self.request(context=[{"role":"system","text":"bad"}])
        self.assertEqual(len(self.core.tickets()), 0)

    def test_event_id_cannot_be_reused_for_different_payload(self):
        self.request()
        with self.assertRaises(ValueError):
            self.core.message("conversation-1", "m1", "different", "request_human", .99)


if __name__ == "__main__":
    unittest.main()
