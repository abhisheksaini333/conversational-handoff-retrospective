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

    def test_ticket_summary_has_operator_metadata_without_content(self):
        ticket=self.ticket()
        self.core.set_priority(ticket,"priority","high")
        self.core.cancel(ticket,"cancel","private cancellation")
        summary=self.core.ticket_summary(ticket)
        self.assertEqual(summary["priority"],"high")
        self.assertEqual(summary["revision"],1)
        self.assertEqual(summary["cancelled_at"],100)
        self.assertNotIn("private cancellation",json.dumps(summary))

    def test_queue_metrics_break_down_priorities(self):
        ticket=self.ticket()
        self.core.set_priority(ticket,"priority","urgent")
        self.core.clock=lambda:125
        metrics=self.core.queue_metrics()["by_priority"]
        self.assertEqual(metrics["urgent"],{"pending":1,"oldest_seconds":25,"untimed":0})
        self.assertEqual(metrics["normal"]["pending"],0)

    def test_ownership_metrics_include_only_active_human_tickets(self):
        ticket=self.ticket()
        class Desk:
            def accept(self,value):return {"accepted":True,"ticket_id":value["id"]}
        self.core.dispatch(ticket,Desk())
        self.assertEqual(self.core.ownership_metrics(),{"assigned":0,"unassigned":1,"by_operator":{}})
        self.core.claim(ticket,"claim","agent")
        self.assertEqual(self.core.ownership_metrics(),{"assigned":1,"unassigned":0,"by_operator":{"agent":1}})
        self.core.complete(ticket,"done")
        self.assertEqual(self.core.ownership_metrics()["assigned"],0)

    def test_resolution_percentiles_use_nearest_rank(self):
        self.assertIsNone(self.core.resolution_metrics()["wait_seconds"]["p95"])
        self.complete()
        self.assertEqual(self.core.resolution_metrics()["wait_seconds"],{"p50":0,"p95":0})

    def test_lifecycle_metrics_keep_cancellations_distinct(self):
        ticket=self.ticket()
        self.core.cancel(ticket,"cancel","withdrawn")
        self.ticket("c2")
        self.assertEqual(self.core.lifecycle_metrics(),{"pending":1,"human":0,"completed":0,"cancelled":1})

    def test_retention_preview_skips_already_redacted_tickets(self):
        ticket=self.complete()
        self.assertEqual(self.core.retention_preview(101)["notes"],0)
        self.core.redact_ticket(ticket)
        self.assertEqual(self.core.retention_preview(101)["ticket_ids"],[])

    def test_redaction_reports_whether_it_changed_content(self):
        ticket=self.complete()
        self.assertTrue(self.core.redact_ticket(ticket)["changed"])
        count=len(self.core.audit_page()["items"])
        self.assertFalse(self.core.redact_ticket(ticket)["changed"])
        self.assertEqual(len(self.core.audit_page()["items"]),count)

    def test_export_can_be_verified_without_database(self):
        from handoff.cli import main
        self.ticket()
        path=Path(self.temp.name)/"export.json"
        bundle=self.core.export_bundle("c1")
        path.write_text(json.dumps(bundle))
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(main(["verify-export",str(path)]),0)
        bundle["data"]["conversation"]["state"]="human"
        path.write_text(json.dumps(bundle))
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(main(["verify-export",str(path)]),1)

    def test_failed_cli_export_removes_partial_destination(self):
        from handoff.cli import main
        self.ticket()
        destination=Path(self.temp.name)/"partial.json"
        with patch("handoff.cli.json.dump",side_effect=OSError("disk full")),contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(main(["--database",str(self.path),"export","c1",str(destination)]),1)
        self.assertFalse(destination.exists())

    def test_cli_filters_ticket_routing_reason(self):
        from handoff.cli import main
        self.ticket()
        self.core.message("low","low","help","greet",.1)
        output=io.StringIO()
        with contextlib.redirect_stdout(output):
            self.assertEqual(main(["--database",str(self.path),"tickets","--reason","low_confidence"]),0)
        self.assertEqual(len(json.loads(output.getvalue())["items"]),1)
        self.assertEqual(json.loads(output.getvalue())["items"][0]["conversation"],"low")

    def test_server_configuration_rejects_invalid_bind_and_upstream_ports(self):
        from handoff.server import make_server
        for kwargs in ({"port":True},{"host":" "},{"port":65536},{"rasa_url":"http://localhost:99999"}):
            with self.assertRaises(ValueError):
                server=make_server(self.core,"local-test-credential",**kwargs)
                server.server_close()

    def test_http_query_budget_and_escape_validation(self):
        import http.client,threading
        from handoff.server import make_server
        server=make_server(self.core,"local-test-credential",port=0)
        thread=threading.Thread(target=lambda:server.serve_forever(poll_interval=.01),daemon=True);thread.start()
        try:
            for target in ("/health?bad=%GG","/health?"+"&".join(f"k{i}=v" for i in range(33))):
                connection=http.client.HTTPConnection(*server.server_address,timeout=2)
                try:
                    connection.request("GET",target);response=connection.getresponse();response.read()
                    self.assertEqual(response.status,400)
                finally:connection.close()
        finally:server.shutdown();server.server_close();thread.join()

    def test_routing_evaluation_rejects_invalid_samples(self):
        from scripts.evaluate import score
        for rows,threshold in (([],.5),([{"prediction":"greet","confidence":float("nan"),"needs_human":False}],.5),([{"prediction":"greet","confidence":.9,"needs_human":False}],True)):
            with self.assertRaises(ValueError):score(rows,threshold)

    def test_evaluation_routes_fallback_like_the_coordinator(self):
        from scripts.evaluate import score
        result=score([{"prediction":"nlu_fallback","confidence":.99,"needs_human":True}],.6)
        self.assertEqual(result["missed_handoffs"],0)
        self.assertEqual(result["correct_routing_rate"],1)
