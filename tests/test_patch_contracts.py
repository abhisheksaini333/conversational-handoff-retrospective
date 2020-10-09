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
