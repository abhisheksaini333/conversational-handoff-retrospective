import contextlib
import io
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from handoff.core import Coordinator

class MaintenanceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name)/"state.db"
        self.core = Coordinator(self.path, clock=lambda: 100.0)
    def ticket(self, conversation="c1", event="e1"):
        return self.core.message(conversation,event,"help","request_human",.9)["ticket_id"]
    def corrupt(self, ticket, update):
        with sqlite3.connect(self.path) as db:
            value=json.loads(db.execute("SELECT data FROM tickets WHERE id=?",(ticket,)).fetchone()[0])
            update(value)
            db.execute("UPDATE tickets SET data=? WHERE id=?",(json.dumps(value),ticket))
    def complete(self):
        ticket=self.ticket()
        class Desk:
            def accept(self, value): return {"accepted":True,"ticket_id":value["id"]}
        self.core.dispatch(ticket,Desk())
        self.core.complete(ticket,"done")
        return ticket

    def test_cursor_bounds_fail_before_sqlite(self):
        for method in (self.core.ticket_page,self.core.audit_page):
            with self.assertRaises(ValueError): method(after=2**63)
            self.assertEqual(method(after=2**63-1)["items"],[])
