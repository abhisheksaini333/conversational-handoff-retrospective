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
