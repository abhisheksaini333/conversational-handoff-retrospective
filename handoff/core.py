"""Transactional local coordinator; no network or framework dependency."""
import copy
import hashlib
import json
import math
import sqlite3
import time
import uuid
import unicodedata
from contextlib import contextmanager, closing
from .http_contracts import strict_json


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
            db.execute("CREATE INDEX IF NOT EXISTS audit_ticket_sequence ON audit(ticket,sequence)")
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
        if not row:
            return None
        return Coordinator._decode(table, key, row[0])

    @staticmethod
    def _decode(table, key, raw):
        try:
            result = strict_json(raw)
            states = ("bot", "pending", "human") if table == "conversations" else ("pending", "human", "completed", "cancelled")
            if not isinstance(result, dict) or result.get("id") != key or result.get("state") not in states:
                raise ValueError("invalid persisted state")
            if not isinstance(result.get("context"), list) or len(result["context"]) > 20:
                raise ValueError("invalid persisted context")
            for item in result["context"]:
                if not isinstance(item, dict) or set(item) != {"role", "text"} or item.get("role") not in ("user", "assistant") or not isinstance(item.get("text"), str) or len(item["text"]) > 2000:
                    raise ValueError("invalid persisted message")
            if result.get("form") is not None:
                identifier(result["form"], "form")
            if table == "tickets":
                identifier(result.get("conversation"), "conversation")
                if type(result.get("delivery_attempts")) is not int or result["delivery_attempts"] < 0:
                    raise ValueError("invalid delivery count")
                if "reason" in result and not isinstance(result["reason"], str):
                    raise ValueError("invalid routing reason")
                if "priority" in result and result["priority"] not in ("low", "normal", "high", "urgent"):
                    raise ValueError("invalid stored priority")
                if "revision" in result and (type(result["revision"]) is not int or result["revision"] < 0):
                    raise ValueError("invalid stored revision")
                if result.get("assignee") is not None:
                    identifier(result["assignee"], "assignee")
                if "redacted" in result and type(result["redacted"]) is not bool:
                    raise ValueError("invalid redaction flag")
                if "tags" in result and (not isinstance(result["tags"], list) or len(result["tags"]) > 10 or any(not isinstance(tag, str) or not tag.strip() or len(tag) > 32 for tag in result["tags"])):
                    raise ValueError("invalid stored tags")
                if "tags" in result:
                    if len(set(result["tags"])) != len(result["tags"]) or any(tag != tag.strip().lower() for tag in result["tags"]):
                        raise ValueError("invalid stored tag normalization")
                    for tag in result["tags"]:
                        identifier(tag, "tag")
                if "notes" in result:
                    if not isinstance(result["notes"], list) or len(result["notes"]) > 100:
                        raise ValueError("invalid stored notes")
                    note_ids = set()
                    for note in result["notes"]:
                        if not isinstance(note, dict) or not isinstance(note.get("text"), str) or not note["text"].strip() or len(note["text"]) > 2000:
                            raise ValueError("invalid stored note")
                        identifier(note.get("id"), "note ID")
                        if note["id"] in note_ids:
                            raise ValueError("duplicate stored note ID")
                        note_ids.add(note["id"])
                        identifier(note.get("actor"), "note actor")
                        timestamp = note.get("created_at")
                        if type(timestamp) not in (int, float) or not math.isfinite(timestamp) or timestamp < 0:
                            raise ValueError("invalid note timestamp")
                for name in ("created_at", "accepted_at", "completed_at", "cancelled_at"):
                    value = result.get(name)
                    if name in result and (type(value) not in (int, float) or not math.isfinite(value) or value < 0):
                        raise ValueError("invalid lifecycle time")
                for earlier, later in (("created_at", "accepted_at"), ("created_at", "completed_at"), ("created_at", "cancelled_at"), ("accepted_at", "completed_at")):
                    if earlier in result and later in result and result[earlier] > result[later]:
                        raise ValueError("lifecycle timestamps are out of order")
            elif result.get("ticket_id") is not None:
                identifier(result["ticket_id"], "ticket_id")
            return result
        except (ValueError, TypeError) as error:
            raise sqlite3.DatabaseError("invalid persisted state") from error

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
            try:
                result = strict_json(row[1])
                if scope.startswith("metadata:"):
                    return Coordinator._decode("tickets", scope[len("metadata:"):], row[1])
                if not isinstance(result, dict) or result.get("state") not in ("bot", "pending", "human"):
                    raise ValueError("invalid receipt state")
                identifier(result.get("conversation"), "receipt conversation")
                if result.get("ticket_id") is not None:
                    identifier(result["ticket_id"], "receipt ticket")
                return result
            except (ValueError, TypeError) as error:
                raise sqlite3.DatabaseError("invalid persisted receipt") from error
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
            return [self._decode("tickets", row[0], row[1]) for row in db.execute("SELECT id,data FROM tickets ORDER BY rowid")]

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
        if type(limit) is not int or not 1 <= limit <= 100 or type(after) is not int or not 0 <= after <= 2**63-1:
            raise ValueError("limit must be 1..100 and cursor nonnegative")
        if state not in (None, "pending", "human", "completed", "cancelled") or reason not in (None, "explicit", "low_confidence"):
            raise ValueError("invalid ticket filter")
        if conversation is not None:
            identifier(conversation, "conversation")
        rows = []
        with self._transaction(readonly=True) as db:
            for row in db.execute("SELECT rowid,id,data FROM tickets WHERE rowid>? ORDER BY rowid", (after,)):
                ticket = self._decode("tickets", row[1], row[2])
                if all(value is None or ticket.get(key) == value for key, value in (("state", state), ("conversation", conversation), ("reason", reason))):
                    rows.append((row[0], ticket))
                    if len(rows) > limit:
                        break
        return {"items": [row[1] for row in rows[:limit]],
                "next_cursor": rows[limit-1][0] if len(rows)>limit else None}

    def ticket_summary(self, ticket_id):
        ticket = self.ticket(ticket_id)
        result = {key: ticket.get(key) for key in ("id", "conversation", "state", "reason", "delivery_attempts", "created_at", "accepted_at", "completed_at", "cancelled_at", "assignee")}
        result.update(priority=ticket.get("priority", "normal"), revision=ticket.get("revision", 0))
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
        by_priority = {}
        for priority in ("low", "normal", "high", "urgent"):
            group = [t for t in pending if t.get("priority", "normal") == priority]
            known = [max(0, now-t["created_at"]) for t in group if "created_at" in t]
            by_priority[priority] = {"pending": len(group), "oldest_seconds": max(known) if known else None, "untimed": sum("created_at" not in t for t in group)}
        return {"by_priority": by_priority, "pending": len(pending), "human": sum(t["state"] == "human" for t in tickets),
                "oldest_pending_seconds": max(ages) if ages else None,
                "untimed_pending": sum("created_at" not in t for t in pending)}

    def ownership_metrics(self):
        human = [t for t in self.tickets() if t["state"] == "human"]
        operators = {}
        for ticket in human:
            actor = ticket.get("assignee")
            if actor is not None:
                operators[actor] = operators.get(actor, 0) + 1
        assigned = sum(operators.values())
        return {"assigned": assigned, "unassigned": len(human)-assigned, "by_operator": dict(sorted(operators.items()))}

    def retry_metrics(self):
        attempts = [t.get("delivery_attempts", 0) for t in self.tickets()]
        return {"attempts": sum(attempts), "retried_tickets": sum(a > 1 for a in attempts),
                "maximum_attempts": max(attempts, default=0)}

    def resolution_metrics(self):
        samples = [t for t in self.tickets() if t["state"] == "completed" and all(k in t for k in ("created_at", "accepted_at", "completed_at"))]
        def percentiles(values):
            ordered = sorted(values)
            return {name: ordered[math.ceil(len(ordered)*fraction)-1] if ordered else None for name, fraction in (("p50", .5), ("p95", .95))}
        distribution = {"wait_seconds": percentiles([t["accepted_at"]-t["created_at"] for t in samples]),
                        "handling_seconds": percentiles([t["completed_at"]-t["accepted_at"] for t in samples])}
        if not samples:
            return {**distribution, "samples": 0, "mean_wait_seconds": None, "mean_handling_seconds": None}
        return {**distribution, "samples": len(samples),
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
            try:
                conversations = {row[0]: self._decode("conversations", *row) for row in db.execute("SELECT id,data FROM conversations")}
                tickets = {row[0]: self._decode("tickets", *row) for row in db.execute("SELECT id,data FROM tickets")}
                for conversation in conversations.values():
                    active = conversation["state"] != "bot"
                    ticket = tickets.get(conversation.get("ticket_id"))
                    if (active and (ticket is None or ticket["conversation"] != conversation["id"] or ticket["state"] != conversation["state"])) or (not active and conversation.get("ticket_id") is not None):
                        raise ValueError("invalid active ticket reference")
                for ticket in tickets.values():
                    conversation = conversations.get(ticket["conversation"])
                    if conversation is None or (ticket["state"] in ("pending", "human") and conversation.get("ticket_id") != ticket["id"]):
                        raise ValueError("orphan ticket")
            except (ValueError, sqlite3.DatabaseError):
                checks.append("logical coordinator state is inconsistent")
        return {"ok": checks == ["ok"], "checks": checks}

    def backup(self, destination):
        import os
        from pathlib import Path
        path = Path(destination)
        # Reserve the destination exclusively, including against symlink replacement.
        descriptor = os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        os.close(descriptor)
        try:
            with closing(sqlite3.connect(self.database, timeout=self.busy_timeout)) as source, source:
                with closing(sqlite3.connect(str(path))) as target, target:
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
            tickets = [self._decode("tickets", row[0], row[1]) for row in db.execute("SELECT id,data FROM tickets ORDER BY rowid")]
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
        if type(limit) is not int or not 1 <= limit <= 100 or type(after) is not int or not 0 <= after <= 2**63-1:
            raise ValueError("invalid audit cursor or limit")
        if ticket_id is not None:
            identifier(ticket_id, "ticket_id")
        with self._transaction(readonly=True) as db:
            if ticket_id is None:
                rows = list(db.execute("SELECT sequence,ticket,conversation,kind,payload FROM audit WHERE sequence>? ORDER BY sequence LIMIT ?", (after, limit+1)))
            else:
                rows = list(db.execute("SELECT sequence,ticket,conversation,kind,payload FROM audit WHERE ticket=? AND sequence>? ORDER BY sequence LIMIT ?", (ticket_id, after, limit+1)))
        items = []
        try:
            for sequence, ticket, conversation, kind, raw in rows[:limit]:
                payload = strict_json(raw)
                identifier(ticket, "audit ticket"); identifier(conversation, "audit conversation")
                if not isinstance(payload, dict) or payload.get("state") not in ("pending", "human", "completed", "cancelled") or type(payload.get("delivery_attempts")) is not int or payload["delivery_attempts"] < 0 or not isinstance(kind, str) or not kind:
                    raise ValueError("invalid audit record")
                items.append({"sequence": sequence, "ticket_id": ticket, "conversation": conversation, "kind": kind, "payload": payload})
        except (ValueError, TypeError) as error:
            raise sqlite3.DatabaseError("invalid persisted audit") from error
        return {"items": items, "next_cursor": rows[limit-1][0] if len(rows)>limit else None}

    def audit_summary(self):
        with self._transaction(readonly=True) as db:
            return dict(db.execute("SELECT kind,COUNT(*) FROM audit GROUP BY kind ORDER BY kind"))

    def _metadata(self, ticket_id, event_id, operation, payload, edit, expected_revision=None):
        identifier(ticket_id, "ticket_id"); identifier(event_id, "event_id")
        if expected_revision is not None and (type(expected_revision) is not int or expected_revision < 0):
            raise ValueError("revision must be a nonnegative integer")
        fingerprint = hashlib.sha256(canonical([operation, payload, expected_revision]).encode()).hexdigest()
        with self._transaction() as db:
            prior = self._replay(db, "metadata:"+ticket_id, event_id, fingerprint)
            if prior is not None:
                return prior
            ticket = self._get(db, "tickets", ticket_id)
            if ticket is None:
                raise KeyError("ticket not found")
            if ticket["state"] not in ("pending", "human"):
                raise ValueError("ticket is closed")
            revision = ticket.get("revision", 0)
            if expected_revision is not None and expected_revision != revision:
                raise ValueError("ticket revision changed")
            edit(ticket)
            ticket["revision"] = revision + 1
            self._put(db, "tickets", ticket_id, ticket)
            self._receipt(db, "metadata:"+ticket_id, event_id, fingerprint, ticket)
            return ticket

    def set_priority(self, ticket_id, event_id, priority, expected_revision=None):
        if priority not in ("low", "normal", "high", "urgent"):
            raise ValueError("invalid priority")
        return self._metadata(ticket_id, event_id, "priority", priority,
                              lambda t: t.update(priority=priority), expected_revision)

    def set_tags(self, ticket_id, event_id, tags, expected_revision=None):
        if not isinstance(tags, list) or len(tags)>10 or any(not isinstance(t, str) or not t.strip() or len(t)>32 for t in tags):
            raise ValueError("tags must contain at most ten nonempty strings of 32 characters")
        normalized = sorted(set(identifier(t.strip().lower(), "tag") for t in tags))
        return self._metadata(ticket_id, event_id, "tags", normalized,
                              lambda t: t.update(tags=normalized), expected_revision)

    def add_note(self, ticket_id, event_id, actor, text, expected_revision=None):
        identifier(actor, "actor")
        if not isinstance(text, str) or not text.strip() or len(text)>2000:
            raise ValueError("note must contain 1..2000 characters")
        def edit(ticket):
            notes = ticket.setdefault("notes", [])
            if len(notes)>=100:
                raise ValueError("ticket note limit reached")
            notes.append({"id": event_id, "actor": actor, "text": text, "created_at": self._now()})
        return self._metadata(ticket_id, event_id, "note", [actor, text], edit, expected_revision)

    def claim(self, ticket_id, event_id, actor, expected_revision=None):
        identifier(actor, "actor")
        def edit(ticket):
            if ticket["state"] != "human" or ticket.get("assignee") not in (None, actor):
                raise ValueError("ticket must be acknowledged and unclaimed")
            ticket["assignee"] = actor
        return self._metadata(ticket_id, event_id, "claim", actor, edit, expected_revision)

    def release(self, ticket_id, event_id, actor, expected_revision=None):
        identifier(actor, "actor")
        def edit(ticket):
            if ticket["state"] != "human" or ticket.get("assignee") != actor:
                raise ValueError("only the assigned operator may release")
            ticket["assignee"] = None
        return self._metadata(ticket_id, event_id, "release", actor, edit, expected_revision)

    def transfer(self, ticket_id, event_id, actor, target, expected_revision=None):
        identifier(actor, "actor"); identifier(target, "target")
        if actor == target:
            raise ValueError("transfer target must be different")
        def edit(ticket):
            if ticket["state"] != "human" or ticket.get("assignee") != actor:
                raise ValueError("only the assigned operator may transfer")
            ticket["assignee"] = target
        return self._metadata(ticket_id, event_id, "transfer", [actor, target], edit, expected_revision)

    def pending_queue(self, limit=20):
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("limit must be 1..100")
        priorities = {"urgent": 0, "high": 1, "normal": 2, "low": 3}
        # Stable sort retains insertion order for equal priority and legacy timestamps.
        tickets = [t for t in self.tickets() if t["state"] == "pending"]
        return sorted(tickets, key=lambda t: (priorities.get(t.get("priority", "normal"), 2), t.get("created_at", 0)))[:limit]

    def dispatch_pending(self, desk, limit=20):
        result = {"accepted": [], "failed": []}
        for ticket in self.pending_queue(limit):
            try:
                state = self.dispatch(ticket["id"], desk)
                result["accepted" if state["state"] == "human" else "failed"].append(ticket["id"])
            except (ConnectionError, TimeoutError, ValueError):
                result["failed"].append(ticket["id"])
        return result

    def retention_preview(self, before):
        if isinstance(before, bool) or not isinstance(before, (int, float)) or not math.isfinite(before) or before < 0:
            raise ValueError("retention cutoff must be finite nonnegative seconds")
        tickets = [t for t in self.tickets() if t["state"] in ("completed", "cancelled") and t.get("completed_at", t.get("cancelled_at", float("inf"))) < before]
        return {"before": before, "ticket_ids": [t["id"] for t in tickets],
                "context_messages": sum(len(t.get("context", [])) for t in tickets)}

    def redact_ticket(self, ticket_id):
        identifier(ticket_id, "ticket_id")
        with self._transaction() as db:
            ticket = self._get(db, "tickets", ticket_id)
            if ticket is None:
                raise KeyError("ticket not found")
            if ticket["state"] not in ("completed", "cancelled"):
                raise ValueError("active transcripts cannot be redacted")
            if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='desk_tickets'").fetchone():
                stored = db.execute("SELECT payload FROM desk_tickets WHERE id=?", (ticket_id,)).fetchone()
                if stored:
                    payload = json.loads(stored[0])
                    payload.update(context=[], notes=[], redacted=True)
                    payload.pop("cancellation_reason", None)
                    db.execute("UPDATE desk_tickets SET payload=? WHERE id=?", (canonical(payload), ticket_id))
            ticket.update(context=[], notes=[], redacted=True)
            ticket.pop("cancellation_reason", None)
            self._put(db, "tickets", ticket_id, ticket)
            state = self._get(db, "conversations", ticket["conversation"])
            related = [self._decode("tickets", row[0], row[1]) for row in db.execute("SELECT id,data FROM tickets ORDER BY rowid")]
            newest = next((t["id"] for t in reversed(related) if t["conversation"] == ticket["conversation"]), None)
            if newest == ticket_id and state and state["state"] == "bot" and state["ticket_id"] is None:
                state["context"] = []
                self._put(db, "conversations", state["id"], state)
            # Metadata receipts may contain historical notes/context. Preserve fingerprints
            # and stable operation identity while removing the content from replay payloads.
            for event, raw in db.execute("SELECT event,result FROM receipts WHERE scope=?", ("metadata:"+ticket_id,)).fetchall():
                receipt = json.loads(raw)
                receipt.update(context=[], notes=[], redacted=True)
                receipt.pop("cancellation_reason", None)
                db.execute("UPDATE receipts SET result=? WHERE scope=? AND event=?", (canonical(receipt), "metadata:"+ticket_id, event))
            return {"ticket_id": ticket_id, "redacted": True}
