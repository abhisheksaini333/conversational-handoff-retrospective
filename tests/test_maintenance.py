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

    def test_persisted_duplicate_fields_are_corruption(self):
        ticket=self.ticket()
        with sqlite3.connect(self.path) as db:
            raw=db.execute("SELECT data FROM tickets WHERE id=?",(ticket,)).fetchone()[0]
            db.execute("UPDATE tickets SET data=? WHERE id=?",(raw[:-1]+',"state":"pending"}',ticket))
        with self.assertRaises(sqlite3.DatabaseError): self.core.ticket(ticket)

    def test_persisted_context_obeys_input_bounds(self):
        ticket=self.ticket()
        self.corrupt(ticket,lambda t:t.update(context=[{"role":"user","text":"x"*2001}]))
        with self.assertRaises(sqlite3.DatabaseError): self.core.ticket(ticket)
        self.corrupt(ticket,lambda t:t.update(context=[{"role":"user","text":"ok","extra":True}]))
        with self.assertRaises(sqlite3.DatabaseError): self.core.ticket(ticket)

    def test_lifecycle_timestamps_cannot_run_backwards(self):
        ticket=self.ticket()
        self.corrupt(ticket,lambda t:t.update(accepted_at=99))
        with self.assertRaises(sqlite3.DatabaseError): self.core.ticket(ticket)
        self.corrupt(ticket,lambda t:t.update(accepted_at=110,completed_at=109))
        with self.assertRaises(sqlite3.DatabaseError): self.core.ticket(ticket)

    def test_persisted_tags_are_unique_normalized_identifiers(self):
        ticket=self.ticket()
        for tags in (["vip","vip"],[" VIP "],["bad\x00tag"]):
            self.corrupt(ticket,lambda t:t.update(tags=tags))
            with self.assertRaises(sqlite3.DatabaseError): self.core.ticket(ticket)

    def test_persisted_notes_require_unique_ids_and_content(self):
        ticket=self.ticket()
        note={"id":"note","actor":"agent","text":"ok","created_at":100}
        self.corrupt(ticket,lambda t:t.update(notes=[note,note]))
        with self.assertRaises(sqlite3.DatabaseError): self.core.ticket(ticket)
        self.corrupt(ticket,lambda t:t.update(notes=[{**note,"text":"   "}]))
        with self.assertRaises(sqlite3.DatabaseError): self.core.ticket(ticket)

    def test_integrity_checks_records_and_active_references(self):
        ticket=self.ticket()
        self.corrupt(ticket,lambda t:t.update(conversation="missing"))
        self.assertFalse(self.core.integrity()["ok"])
        self.corrupt(ticket,lambda t:t.update(conversation="c1",state="completed"))
        self.assertFalse(self.core.integrity()["ok"])
        self.corrupt(ticket,lambda t:t.update(state="pending",context="invalid"))
        self.assertFalse(self.core.integrity()["ok"])

    def test_audit_reads_reject_corrupt_payloads(self):
        ticket=self.ticket()
        with sqlite3.connect(self.path) as db: db.execute("UPDATE audit SET payload=?",('[1]',))
        with self.assertRaises(sqlite3.DatabaseError): self.core.audit_page(ticket_id=ticket)

    def test_replay_rejects_corrupt_receipt_payload(self):
        self.ticket()
        with sqlite3.connect(self.path) as db: db.execute("UPDATE receipts SET result=?",('[]',))
        with self.assertRaises(sqlite3.DatabaseError): self.ticket()
