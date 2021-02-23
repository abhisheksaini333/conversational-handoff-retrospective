"""Transactional local coordinator; no network or framework dependency."""
import copy
import hashlib
import json
import math
import sqlite3
import time
import uuid
import unicodedata
from contextlib import contextmanager


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def identifier(value, name):
    if not isinstance(value, str) or not value.strip() or len(value) > 128 or any(unicodedata.category(c).startswith("C") for c in value):
        raise ValueError(name + " must be a nonempty string of at most 128 characters")
    return value


class Coordinator:
    def __init__(self, database, threshold=0.6, busy_timeout=10, clock=None):
        if isinstance(threshold, bool) or not isinstance(threshold, (int, float)) or not 0 <= threshold <= 1:
            raise ValueError("threshold must be between zero and one")
        if str(database) in ("", ":memory:"):
            raise ValueError("database must be a persistent file path")
        self.clock = clock or time.time
        self.database = database
        self.threshold = threshold
        if isinstance(busy_timeout, bool) or not isinstance(busy_timeout, (int, float)) or not math.isfinite(busy_timeout) or not 0 < busy_timeout <= 60:
            raise ValueError("busy_timeout must be finite and between zero and 60 seconds")
        self.busy_timeout = busy_timeout
        with self._transaction() as db:
            db.execute("CREATE TABLE IF NOT EXISTS conversations (id TEXT PRIMARY KEY, data TEXT NOT NULL)")
            db.execute("CREATE TABLE IF NOT EXISTS tickets (id TEXT PRIMARY KEY, data TEXT NOT NULL)")
            db.execute("CREATE TABLE IF NOT EXISTS audit (sequence INTEGER PRIMARY KEY AUTOINCREMENT, ticket TEXT NOT NULL, conversation TEXT NOT NULL, kind TEXT NOT NULL, payload TEXT NOT NULL)")
            db.execute("CREATE TABLE IF NOT EXISTS receipts (scope TEXT, event TEXT, fingerprint TEXT NOT NULL, result TEXT NOT NULL, PRIMARY KEY(scope,event))")

    @contextmanager
    def _transaction(self, readonly=False):
        db = sqlite3.connect(self.database, timeout=self.busy_timeout)
        try:
            db.execute("BEGIN" if readonly else "BEGIN IMMEDIATE")
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    @staticmethod
    def _get(db, table, key):
        # table is always a fixed internal constant, never caller input.
        row = db.execute("SELECT data FROM " + table + " WHERE id=?", (key,)).fetchone()
        return json.loads(row[0]) if row else None

    @staticmethod
    def _put(db, table, key, data):
        previous = Coordinator._get(db, table, key) if table == "tickets" else None
        db.execute("INSERT INTO " + table + "(id,data) VALUES(?,?) ON CONFLICT(id) DO UPDATE SET data=excluded.data", (key, canonical(data)))
        if table == "tickets" and previous != data:
            kind = "created" if previous is None else "state:"+data["state"] if previous["state"] != data["state"] else "delivery_attempt" if previous.get("delivery_attempts") != data.get("delivery_attempts") else "updated"
            payload = {"state": data["state"], "delivery_attempts": data.get("delivery_attempts", 0)}
            db.execute("INSERT INTO audit(ticket,conversation,kind,payload) VALUES(?,?,?,?)", (key, data["conversation"], kind, canonical(payload)))

    @staticmethod
    def _replay(db, scope, event, fingerprint):
        row = db.execute("SELECT fingerprint,result FROM receipts WHERE scope=? AND event=?", (scope, event)).fetchone()
        if row:
            if row[0] != fingerprint:
                raise ValueError("event ID was reused for different content")
            return json.loads(row[1])
        return None

    @staticmethod
    def _receipt(db, scope, event, fingerprint, result):
        db.execute("INSERT INTO receipts VALUES(?,?,?,?)", (scope, event, fingerprint, canonical(result)))

    def conversation(self, conversation):
        identifier(conversation, "conversation")
        with self._transaction(readonly=True) as db:
            return self._get(db, "conversations", conversation)

    def ticket(self, ticket_id):
        identifier(ticket_id, "ticket_id")
        with self._transaction(readonly=True) as db:
            result = self._get(db, "tickets", ticket_id)
            if result is None:
                raise KeyError("ticket not found")
            return result

    def tickets(self):
        with self._transaction(readonly=True) as db:
            return [json.loads(row[0]) for row in db.execute("SELECT data FROM tickets ORDER BY rowid")]

    def message(self, conversation, event_id, text, intent, confidence, active_form=None, context=None):
        identifier(conversation, "conversation")
        identifier(event_id, "event_id")
        identifier(intent, "intent")
        if not isinstance(text, str) or not text.strip() or len(text) > 2000:
            raise ValueError("text must contain 1 to 2000 characters")
        if isinstance(confidence, bool) or not isinstance(confidence, (int, float)) or not math.isfinite(confidence) or not 0 <= confidence <= 1:
            raise ValueError("confidence must be finite and between zero and one")
        confidence = float(confidence)
        if active_form is not None:
            identifier(active_form, "active_form")
        context_provided = context is not None
        context = [] if context is None else context
        if not isinstance(context, list) or len(context) > 20:
            raise ValueError("context must be a list of at most 20 messages")
        for item in context:
            if not isinstance(item, dict) or set(item) != {"role", "text"} or item["role"] not in ("user", "assistant") or not isinstance(item["text"], str) or len(item["text"]) > 2000:
                raise ValueError("invalid context message")
        fingerprint = hashlib.sha256(canonical([text, intent, confidence, active_form, context_provided, context]).encode()).hexdigest()
        with self._transaction() as db:
            prior = self._replay(db, "message:"+conversation, event_id, fingerprint)
            if prior is not None:
                return prior
            state = self._get(db, "conversations", conversation) or {"id": conversation, "state": "bot", "ticket_id": None, "form": None, "context": context}
            if context_provided:
                state["context"] = context
            state["context"] = (state["context"] + [{"role": "user", "text": text}])[-20:]
            if state["state"] == "bot" and (intent in ("request_human", "nlu_fallback") or confidence < self.threshold):
                state.update(state="pending", ticket_id=str(uuid.uuid4()), form=active_form)
                ticket = {"id": state["ticket_id"], "conversation": conversation, "state": "pending", "form": active_form, "context": state["context"], "reason": "explicit" if intent == "request_human" else "low_confidence", "delivery_attempts": 0, "created_at": self._now()}
                self._put(db, "tickets", ticket["id"], ticket)
            elif state["state"] in ("pending", "human"):
                ticket = self._get(db, "tickets", state["ticket_id"])
                ticket["context"] = state["context"]
                self._put(db, "tickets", ticket["id"], ticket)
            reply = {"bot": "I can help with your order.", "pending": "Your request is queued; a person has not accepted it yet.", "human": None}[state["state"]]
            result = {"conversation": conversation, "state": state["state"], "ticket_id": state["ticket_id"], "bot_reply": reply}
            self._put(db, "conversations", conversation, state)
            self._receipt(db, "message:"+conversation, event_id, fingerprint, result)
            return result

    def dispatch(self, ticket_id, desk):
        identifier(ticket_id, "ticket_id")
        with self._transaction() as db:
            ticket = self._get(db, "tickets", ticket_id)
            if ticket is None:
                raise KeyError("ticket not found")
            if ticket["state"] != "pending":
                return self._get(db, "conversations", ticket["conversation"])
            ticket["delivery_attempts"] += 1
            self._put(db, "tickets", ticket_id, ticket)
        # Do not hold the SQLite write lock across human-service I/O.
        ack = desk.accept(copy.deepcopy(ticket))
        if not isinstance(ack, dict) or ack.get("accepted") is not True or ack.get("ticket_id") != ticket_id:
            raise ValueError("human acknowledgement does not match this ticket")
        with self._transaction() as db:
            latest = self._get(db, "tickets", ticket_id)
            state = self._get(db, "conversations", ticket["conversation"])
            if latest["state"] == "pending" and state["ticket_id"] == ticket_id:
                latest["state"] = state["state"] = "human"
                latest["accepted_at"] = self._now()
                self._put(db, "tickets", ticket_id, latest)
                self._put(db, "conversations", state["id"], state)
            return state

    def complete(self, ticket_id, event_id, before_resume=None):
        identifier(ticket_id, "ticket_id")
        identifier(event_id, "event_id")
        with self._transaction() as db:
            prior = self._replay(db, "complete:"+ticket_id, event_id, ticket_id)
            if prior is not None:
                return prior
            ticket = self._get(db, "tickets", ticket_id)
            if ticket is None:
                raise KeyError("ticket not found")
            state = self._get(db, "conversations", ticket["conversation"])
            if ticket["state"] != "human" or state["ticket_id"] != ticket_id:
                raise ValueError("only the active acknowledged ticket may complete")
            result = {"conversation": state["id"], "state": "bot", "ticket_id": ticket_id, "resume_form": ticket["form"]}
            # Bounded callback; a failed resume leaves ownership with the human.
            if before_resume is not None:
                before_resume(copy.deepcopy(result))
            ticket["state"] = "completed"
            ticket["completed_at"] = self._now()
            state.update(state="bot", ticket_id=None, form=ticket["form"])
            self._put(db, "tickets", ticket_id, ticket)
            self._put(db, "conversations", state["id"], state)
            self._receipt(db, "complete:"+ticket_id, event_id, ticket_id, result)
            return result

    def _now(self):
        value = self.clock()
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
            raise ValueError("clock must return finite nonnegative seconds")
        return float(value)

    def ticket_page(self, limit=50, after=0, state=None, conversation=None, reason=None):
        if type(limit) is not int or not 1 <= limit <= 100 or type(after) is not int or after < 0:
            raise ValueError("limit must be 1..100 and cursor nonnegative")
        if state not in (None, "pending", "human", "completed", "cancelled") or reason not in (None, "explicit", "low_confidence"):
            raise ValueError("invalid ticket filter")
        if conversation is not None:
            identifier(conversation, "conversation")
        rows = []
        with self._transaction(readonly=True) as db:
            for row in db.execute("SELECT rowid,data FROM tickets WHERE rowid>? ORDER BY rowid", (after,)):
                ticket = json.loads(row[1])
                if all(value is None or ticket.get(key) == value for key, value in (("state", state), ("conversation", conversation), ("reason", reason))):
                    rows.append(row)
                    if len(rows) > limit:
                        break
        return {"items": [json.loads(row[1]) for row in rows[:limit]],
                "next_cursor": rows[limit-1][0] if len(rows)>limit else None}

    def ticket_summary(self, ticket_id):
        ticket = self.ticket(ticket_id)
        result = {key: ticket.get(key) for key in ("id", "conversation", "state", "reason", "delivery_attempts", "created_at", "accepted_at", "completed_at")}
        result["context_messages"] = len(ticket.get("context", []))
        return result

    def snapshot_counts(self):
        with self._transaction(readonly=True) as db:
            return {table: db.execute("SELECT COUNT(*) FROM " + table).fetchone()[0]
                    for table in ("conversations", "tickets", "receipts")}

    def queue_metrics(self):
        now = self._now()
        tickets = self.tickets()
        pending = [t for t in tickets if t["state"] == "pending"]
        ages = [max(0, now-t["created_at"]) for t in pending if "created_at" in t]
        return {"pending": len(pending), "human": sum(t["state"] == "human" for t in tickets),
                "oldest_pending_seconds": max(ages) if ages else None,
                "untimed_pending": sum("created_at" not in t for t in pending)}

    def retry_metrics(self):
        attempts = [t.get("delivery_attempts", 0) for t in self.tickets()]
        return {"attempts": sum(attempts), "retried_tickets": sum(a > 1 for a in attempts),
                "maximum_attempts": max(attempts, default=0)}

    def resolution_metrics(self):
        samples = [t for t in self.tickets() if t["state"] == "completed" and all(k in t for k in ("created_at", "accepted_at", "completed_at"))]
        if not samples:
            return {"samples": 0, "mean_wait_seconds": None, "mean_handling_seconds": None}
        return {"samples": len(samples),
                "mean_wait_seconds": sum(max(0, t["accepted_at"]-t["created_at"]) for t in samples)/len(samples),
                "mean_handling_seconds": sum(max(0, t["completed_at"]-t["accepted_at"]) for t in samples)/len(samples)}

    def routing_metrics(self):
        result = {"explicit": 0, "low_confidence": 0, "unknown": 0}
        for ticket in self.tickets():
            reason = ticket.get("reason")
            result[reason if reason in result else "unknown"] += 1
        return result

    def integrity(self):
        with self._transaction(readonly=True) as db:
            checks = [row[0] for row in db.execute("PRAGMA quick_check")]
        return {"ok": checks == ["ok"], "checks": checks}

    def backup(self, destination):
        import os
        from pathlib import Path
        path = Path(destination)
        # Reserve the destination exclusively, including against symlink replacement.
        descriptor = os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        os.close(descriptor)
        try:
            with sqlite3.connect(self.database, timeout=self.busy_timeout) as source:
                with sqlite3.connect(str(path)) as target:
                    source.backup(target)
                    if target.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
                        raise sqlite3.DatabaseError("backup integrity verification failed")
        except BaseException:
            path.unlink(missing_ok=True)
            raise
        return {"path": str(path), "verified": True}

    def export_conversation(self, conversation):
        identifier(conversation, "conversation")
        with self._transaction(readonly=True) as db:
            state = self._get(db, "conversations", conversation)
            if state is None:
                raise KeyError("conversation not found")
            tickets = [json.loads(row[0]) for row in db.execute("SELECT data FROM tickets ORDER BY rowid")]
            return {"version": 1, "conversation": state,
                    "tickets": [t for t in tickets if t["conversation"] == conversation]}

    def export_bundle(self, conversation):
        data = self.export_conversation(conversation)
        return {"algorithm": "sha256", "sha256": hashlib.sha256(canonical(data).encode()).hexdigest(), "data": data}

    def cancel(self, ticket_id, event_id, reason):
        identifier(ticket_id, "ticket_id"); identifier(event_id, "event_id")
        if not isinstance(reason, str) or not reason.strip() or len(reason) > 500:
            raise ValueError("cancellation reason must contain 1..500 characters")
        fingerprint = hashlib.sha256(canonical(reason).encode()).hexdigest()
        with self._transaction() as db:
            prior = self._replay(db, "cancel:"+ticket_id, event_id, fingerprint)
            if prior is not None:
                return prior
            ticket = self._get(db, "tickets", ticket_id)
            if ticket is None:
                raise KeyError("ticket not found")
            state = self._get(db, "conversations", ticket["conversation"])
            if ticket["state"] != "pending" or state["ticket_id"] != ticket_id:
                raise ValueError("only the active pending ticket may be cancelled")
            ticket.update(state="cancelled", cancellation_reason=reason, cancelled_at=self._now())
            state.update(state="bot", ticket_id=None)
            result = {"conversation": state["id"], "state": "bot", "ticket_id": ticket_id}
            self._put(db, "tickets", ticket_id, ticket)
            self._put(db, "conversations", state["id"], state)
            self._receipt(db, "cancel:"+ticket_id, event_id, fingerprint, result)
            return result

    def audit_page(self, limit=50, after=0, ticket_id=None):
        if type(limit) is not int or not 1 <= limit <= 100 or type(after) is not int or after < 0:
            raise ValueError("invalid audit cursor or limit")
        if ticket_id is not None:
            identifier(ticket_id, "ticket_id")
        with self._transaction(readonly=True) as db:
            rows = list(db.execute("SELECT sequence,ticket,conversation,kind,payload FROM audit WHERE sequence>? AND (? IS NULL OR ticket=?) ORDER BY sequence LIMIT ?", (after, ticket_id, ticket_id, limit+1)))
        return {"items": [{"sequence": r[0], "ticket_id": r[1], "conversation": r[2], "kind": r[3], "payload": json.loads(r[4])} for r in rows[:limit]],
                "next_cursor": rows[limit-1][0] if len(rows)>limit else None}
