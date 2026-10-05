"""Scoped SQLite state, evidence, proposals, and metadata-only traces."""

import json
import os
from pathlib import Path
import sqlite3

from .common import KernelError, canonical, checked_value, identifier, key, reject_secrets, text, timestamp


SCHEMA = """
CREATE TABLE IF NOT EXISTS metadata(key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS entities(
 id TEXT PRIMARY KEY, scope TEXT NOT NULL, entity_key TEXT NOT NULL,
 label TEXT NOT NULL, kind TEXT NOT NULL, aliases TEXT NOT NULL,
 UNIQUE(scope,entity_key));
CREATE TABLE IF NOT EXISTS evidence(
 id TEXT PRIMARY KEY, scope TEXT NOT NULL, source_kind TEXT NOT NULL,
 source_ref TEXT, source_text TEXT NOT NULL, recorded_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS statements(
 id TEXT PRIMARY KEY, scope TEXT NOT NULL,
 entity_id TEXT NOT NULL REFERENCES entities(id), predicate TEXT NOT NULL,
 value TEXT NOT NULL, assertion_kind TEXT NOT NULL,
 evidence_id TEXT NOT NULL REFERENCES evidence(id), valid_from TEXT NOT NULL,
 valid_until TEXT, recorded_at TEXT NOT NULL,
 lifecycle TEXT NOT NULL CHECK(lifecycle IN ('active','superseded','revoked')),
 superseded_by TEXT REFERENCES statements(id),
 CHECK(valid_until IS NULL OR valid_until > valid_from
       OR (lifecycle='superseded' AND valid_until=valid_from)));
CREATE INDEX IF NOT EXISTS statement_lookup ON statements(scope,entity_id,predicate,lifecycle);
CREATE INDEX IF NOT EXISTS statement_time ON statements(scope,valid_from,valid_until);
CREATE TABLE IF NOT EXISTS relations(
 id TEXT PRIMARY KEY, scope TEXT NOT NULL, kind TEXT NOT NULL,
 from_statement TEXT REFERENCES statements(id) ON DELETE CASCADE,
 to_statement TEXT REFERENCES statements(id) ON DELETE CASCADE,
 from_entity TEXT REFERENCES entities(id) ON DELETE CASCADE,
 to_entity TEXT REFERENCES entities(id) ON DELETE CASCADE,
 CHECK((kind='corrects' AND from_statement IS NOT NULL AND to_statement IS NOT NULL
        AND from_entity IS NULL AND to_entity IS NULL)
    OR (kind='part_of' AND from_entity IS NOT NULL AND to_entity IS NOT NULL
        AND from_statement IS NULL AND to_statement IS NULL)));
CREATE TABLE IF NOT EXISTS proposals(
 id TEXT PRIMARY KEY, scope TEXT NOT NULL, operation TEXT NOT NULL,
 payload TEXT NOT NULL, status TEXT NOT NULL, recorded_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS plans(
 id TEXT PRIMARY KEY, scope TEXT NOT NULL, plan TEXT NOT NULL, recorded_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS projections(
 id TEXT PRIMARY KEY, scope TEXT NOT NULL, trace TEXT NOT NULL, recorded_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS operations(
 id TEXT PRIMARY KEY, scope TEXT NOT NULL, operation TEXT NOT NULL,
 metadata TEXT NOT NULL, recorded_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS processed_events(
 scope TEXT NOT NULL, event_id TEXT NOT NULL, PRIMARY KEY(scope,event_id));
"""


class Store:
    def __init__(self, path, scope="personal", clock=None, create=False):
        self.scope = key(scope, "scope")
        self.clock = clock or timestamp
        self.path = Path(path).expanduser().resolve()
        if not create and not self.path.is_file():
            raise KernelError("Memory is not initialized. Run memory init first.")
        if create:
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.path, timeout=5)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA foreign_keys=ON")
        self.db.execute("PRAGMA secure_delete=ON")
        self.db.execute("PRAGMA journal_mode=DELETE")
        if create:
            self.db.executescript(SCHEMA)
            self.db.execute("INSERT OR IGNORE INTO metadata VALUES('schema_version','1')")
            self.db.commit()
            if os.name == "posix":
                self.path.chmod(0o600)
        version = self.db.execute("SELECT value FROM metadata WHERE key='schema_version'").fetchone()
        if not version or version[0] != "1":
            raise KernelError("Unsupported memory schema.")

    def close(self):
        self.db.close()

    def _event(self, operation, metadata=None):
        self.db.execute("INSERT INTO operations VALUES(?,?,?,?,?)",
                        (identifier(), self.scope, operation, canonical(metadata or {}), self.clock()))

    def _entity(self, entity_key, label=None, kind="person"):
        entity_key = key(entity_key, "entity")
        row = self.db.execute("SELECT * FROM entities WHERE scope=? AND entity_key=?", (self.scope, entity_key)).fetchone()
        if row:
            return row["id"]
        entity_id = identifier()
        self.db.execute("INSERT INTO entities VALUES(?,?,?,?,?,?)",
                        (entity_id, self.scope, entity_key, text(label or entity_key, 256), key(kind, "entity type"), "[]"))
        return entity_id

    def add_alias(self, entity_key, alias):
        with self.db:
            row = self.db.execute("SELECT * FROM entities WHERE scope=? AND entity_key=?", (self.scope, key(entity_key))).fetchone()
            if not row:
                raise KernelError("Entity not found in this scope.")
            aliases = sorted(set(json.loads(row["aliases"]) + [text(alias, 256)]))
            self.db.execute("UPDATE entities SET aliases=? WHERE id=?", (canonical(aliases), row["id"]))
            self._event("alias", {"entity_id": row["id"]})

    def resolve_entity(self, reference):
        reference = text(reference, 256).casefold()
        matches = [dict(r) for r in self.db.execute("SELECT * FROM entities WHERE scope=?", (self.scope,))
                   if reference in {r["entity_key"].casefold(), r["label"].casefold(), *[a.casefold() for a in json.loads(r["aliases"])]}]
        if len(matches) != 1:
            raise KernelError("Entity is unknown or ambiguous in this scope.")
        return matches[0]

    def _insert(self, entity, predicate, value, evidence, valid_from=None, valid_until=None,
                assertion_kind="user_statement", source_kind="user_statement", label=None, kind="person", source_ref=None):
        predicate = key(predicate, "predicate")
        encoded = checked_value(value)
        evidence = text(evidence)
        reject_secrets(evidence + " " + encoded)
        if assertion_kind not in {"user_statement", "observed", "hypothesis", "inference"}:
            raise KernelError("Invalid assertion kind.")
        start = timestamp(valid_from) if valid_from else self.clock()
        end = timestamp(valid_until) if valid_until else None
        if end and end <= start:
            raise KernelError("Validity must end after it starts.")
        entity_id = self._entity(entity, label, kind)
        statement_id, evidence_id = identifier(), identifier()
        self.db.execute("INSERT INTO evidence VALUES(?,?,?,?,?,?)",
                        (evidence_id, self.scope, source_kind, source_ref, evidence, self.clock()))
        self.db.execute("INSERT INTO statements VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                        (statement_id, self.scope, entity_id, predicate, encoded, assertion_kind,
                         evidence_id, start, end, self.clock(), "active", None))
        return statement_id

    def remember(self, entity, predicate, value, evidence, **kwargs):
        with self.db:
            statement_id = self._insert(entity, predicate, value, evidence, **kwargs)
            self._event("remember", {"statement_id": statement_id})
        return self.inspect(statement_id)

    def _row(self, statement_id):
        row = self.db.execute("""SELECT s.*, e.entity_key, e.label, e.kind, e.aliases,
                            v.source_kind, v.source_ref, v.source_text
                            FROM statements s JOIN entities e ON e.id=s.entity_id
                            JOIN evidence v ON v.id=s.evidence_id
                            WHERE s.id=? AND s.scope=? AND e.scope=? AND v.scope=?""",
                              (statement_id, self.scope, self.scope, self.scope)).fetchone()
        if not row:
            raise KernelError("Statement not found in this scope.")
        return dict(row)

    def _decode(self, row, as_of=None):
        row = dict(row)
        row["value"] = json.loads(row["value"])
        row["aliases"] = json.loads(row["aliases"])
        at = timestamp(as_of) if as_of else self.clock()
        state = row["lifecycle"]
        if state != "revoked":
            if at < row["valid_from"]:
                state = "scheduled"
            elif row["valid_until"] and at >= row["valid_until"]:
                state = "superseded" if row["superseded_by"] else "expired"
            else:
                state = "active"
        row["effective_state"] = state
        return row

    def inspect(self, statement_id, as_of=None):
        return self._decode(self._row(statement_id), as_of)

    def records(self, as_of=None, history=False):
        ids = [r[0] for r in self.db.execute("SELECT id FROM statements WHERE scope=? ORDER BY id", (self.scope,))]
        records = [self.inspect(i, as_of) for i in ids]
        if history:
            return records
        return [r for r in records if r["effective_state"] == "active"
                and r["assertion_kind"] in {"user_statement", "observed"}]

    def _correct(self, statement_id, value, evidence, valid_from=None, valid_until=None):
        old = self._row(statement_id)
        if old["lifecycle"] != "active" or self._decode(old)["effective_state"] != "active":
            raise KernelError("Correction target is no longer active.")
        start = timestamp(valid_from) if valid_from else self.clock()
        if start < old["valid_from"]:
            raise KernelError("Correction cannot start before its target.")
        new_id = self._insert(old["entity_key"], old["predicate"], value, evidence,
                              valid_from=start, valid_until=valid_until)
        end = min(start, old["valid_until"]) if old["valid_until"] else start
        self.db.execute("UPDATE statements SET lifecycle='superseded', valid_until=?, superseded_by=? WHERE id=?", (end, new_id, statement_id))
        self.db.execute("INSERT INTO relations VALUES(?,?,?,?,?,?,?)",
                        (identifier(), self.scope, "corrects", new_id, statement_id, None, None))
        self._event("correct", {"statement_id": new_id})
        return new_id

    def correct(self, statement_id, value, evidence, valid_from=None, valid_until=None):
        with self.db:
            new_id = self._correct(statement_id, value, evidence, valid_from, valid_until)
        return self.inspect(new_id)

    def revoke(self, statement_id):
        with self.db:
            self._row(statement_id)
            self.db.execute("UPDATE statements SET lifecycle='revoked' WHERE id=?", (statement_id,))
            self._event("revoke", {"statement_id": statement_id})
        return {"status": "revoked", "id": statement_id}

    def relate(self, child, parent):
        with self.db:
            child_id = self.resolve_entity(child)["id"]
            parent_id = self.resolve_entity(parent)["id"]
            if child_id == parent_id:
                raise KernelError("An entity cannot contain itself.")
            relation_id = identifier()
            self.db.execute("INSERT INTO relations VALUES(?,?,?,?,?,?,?)",
                            (relation_id, self.scope, "part_of", None, None, child_id, parent_id))
            self._event("relate", {"relation_id": relation_id})
        return {"id": relation_id, "kind": "part_of"}

    def forget(self, statement_id):
        with self.db:
            row = self._row(statement_id)
            ids = [r[0] for r in self.db.execute("SELECT id FROM statements WHERE scope=? AND entity_id=? AND predicate=?",
                                                (self.scope, row["entity_id"], row["predicate"]))]
            # Remove every version of this property, including revoked versions.
            self.db.execute("UPDATE statements SET superseded_by=NULL WHERE scope=? AND entity_id=? AND predicate=?",
                            (self.scope, row["entity_id"], row["predicate"]))
            self.db.execute("DELETE FROM statements WHERE scope=? AND entity_id=? AND predicate=?",
                            (self.scope, row["entity_id"], row["predicate"]))
            self.db.execute("DELETE FROM evidence WHERE scope=? AND id NOT IN (SELECT evidence_id FROM statements)", (self.scope,))
            self.db.execute("DELETE FROM entities WHERE scope=? AND id NOT IN (SELECT entity_id FROM statements)", (self.scope,))
            # Conservative invalidation prevents forgotten content surviving in derived records.
            for table in ("plans", "projections", "proposals", "operations", "processed_events"):
                self.db.execute(f"DELETE FROM {table} WHERE scope=?", (self.scope,))
            self._event("forget", {"removed_count": len(ids)})
        return {"status": "forgotten", "removed_count": len(ids),
                "limits": "Host history, external backups, and forensic disk erasure are not covered."}

    def propose(self, operation, payload):
        if operation not in {"remember", "correct"} or not isinstance(payload, dict):
            raise KernelError("Only remember and correct proposals are supported.")
        allowed = {"entity", "predicate", "value", "evidence", "target_id", "valid_from", "valid_until"}
        if set(payload) - allowed:
            raise KernelError("Proposal has unsupported fields.")
        required = {"entity", "predicate", "value"} if operation == "remember" else {"target_id", "value"}
        if not required <= set(payload):
            raise KernelError("Proposal is missing required fields.")
        if operation == "remember":
            key(payload["entity"], "entity")
            key(payload["predicate"], "predicate")
        if operation == "correct":
            self._row(payload.get("target_id"))
        encoded = checked_value(payload)
        reject_secrets(encoded)
        proposal_id = identifier()
        with self.db:
            self.db.execute("INSERT INTO proposals VALUES(?,?,?,?,?,?)",
                            (proposal_id, self.scope, operation, encoded, "pending", self.clock()))
            self._event("propose", {"proposal_id": proposal_id})
        return {"id": proposal_id, "status": "pending", "operation": operation}

    def proposals(self):
        return [dict(r) | {"payload": json.loads(r["payload"])} for r in self.db.execute(
            "SELECT * FROM proposals WHERE scope=? ORDER BY recorded_at,id", (self.scope,))]

    def reject(self, proposal_id):
        with self.db:
            changed = self.db.execute("UPDATE proposals SET status='rejected' WHERE id=? AND scope=? AND status='pending'",
                                      (proposal_id, self.scope)).rowcount
            if not changed:
                raise KernelError("Pending proposal not found in this scope.")
            self._event("reject", {"proposal_id": proposal_id})
        return {"id": proposal_id, "status": "rejected"}

    def approve(self, proposal_id):
        with self.db:
            row = self.db.execute("SELECT * FROM proposals WHERE id=? AND scope=? AND status='pending'", (proposal_id, self.scope)).fetchone()
            if not row:
                raise KernelError("Pending proposal not found in this scope.")
            payload = json.loads(row["payload"])
            evidence = payload.get("evidence") or "Confirmed by the local owner."
            if row["operation"] == "remember":
                new_id = self._insert(payload["entity"], payload["predicate"], payload["value"], evidence,
                                      valid_from=payload.get("valid_from"), valid_until=payload.get("valid_until"),
                                      source_kind="user_confirmation")
            else:
                new_id = self._correct(payload["target_id"], payload["value"], evidence,
                                       payload.get("valid_from"), payload.get("valid_until"))
            self.db.execute("UPDATE proposals SET status='accepted' WHERE id=?", (proposal_id,))
            self._event("approve", {"statement_id": new_id})
        return self.inspect(new_id)

    def save_plan(self, plan):
        plan_id = identifier()
        with self.db:
            self.db.execute("INSERT INTO plans VALUES(?,?,?,?)", (plan_id, self.scope, canonical(plan), self.clock()))
        return plan_id

    def load_plan(self, plan_id):
        row = self.db.execute("SELECT plan FROM plans WHERE id=? AND scope=?", (plan_id, self.scope)).fetchone()
        if not row:
            raise KernelError("Plan not found in this scope.")
        return json.loads(row[0])

    def save_trace(self, trace):
        with self.db:
            self.db.execute("INSERT INTO projections VALUES(?,?,?,?)", (trace["id"], self.scope, canonical(trace), self.clock()))

    def trace(self, projection_id):
        row = self.db.execute("SELECT trace FROM projections WHERE id=? AND scope=?", (projection_id, self.scope)).fetchone()
        if not row:
            raise KernelError("Projection not found in this scope.")
        return json.loads(row[0])

    def mark_emitted(self, projection_id):
        trace = self.trace(projection_id)
        trace["delivery"] = "emitted"
        with self.db:
            self.db.execute("UPDATE projections SET trace=? WHERE id=? AND scope=?", (canonical(trace), projection_id, self.scope))

    def status(self):
        return {"scope": self.scope, "current_statements": len(self.records()),
                "stored_statements": self.db.execute("SELECT count(*) FROM statements WHERE scope=?", (self.scope,)).fetchone()[0],
                "pending_proposals": self.db.execute("SELECT count(*) FROM proposals WHERE scope=? AND status='pending'", (self.scope,)).fetchone()[0]}
