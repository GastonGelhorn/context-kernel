"""Scoped SQLite state, evidence, proposals, and metadata-only traces."""

import json
import os
from pathlib import Path
import sqlite3

from .common import KernelError, canonical, checked_value, identifier, key, reject_secrets, text, timestamp


SCHEMA_VERSION = "2"
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
 CHECK((kind IN ('corrects','depends_on') AND from_statement IS NOT NULL AND to_statement IS NOT NULL
        AND from_entity IS NULL AND to_entity IS NULL)
    OR (kind='part_of' AND from_entity IS NOT NULL AND to_entity IS NOT NULL
        AND from_statement IS NULL AND to_statement IS NULL)));
CREATE INDEX IF NOT EXISTS relation_target ON relations(scope,kind,to_statement);
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
            self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.db = sqlite3.connect(self.path, timeout=5)
        try:
            self._configure(create)
        except Exception:
            self.db.close()
            raise

    def _configure(self, create):
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA foreign_keys=ON")
        self.db.execute("PRAGMA secure_delete=ON")
        self.db.execute("PRAGMA journal_mode=DELETE")
        if create:
            with self.db:
                self._schema()
                self.db.execute("INSERT OR IGNORE INTO metadata VALUES('schema_version',?)", (SCHEMA_VERSION,))
            if os.name == "posix":
                self.path.chmod(0o600)
        version = self.db.execute("SELECT value FROM metadata WHERE key='schema_version'").fetchone()
        if version and version[0] == "1":
            self._migrate_v1()
            version = (SCHEMA_VERSION,)
        if not version or version[0] != SCHEMA_VERSION:
            raise KernelError("Unsupported memory schema.")

    def _migrate_v1(self):
        # Version 1 only differs in the relation kinds its CHECK accepts. SQLite cannot
        # alter a CHECK in place, so the table is rebuilt with every row copied; nothing is dropped.
        with self.db:
            self.db.execute("ALTER TABLE relations RENAME TO relations_v1")
            self._schema()
            self.db.execute("INSERT INTO relations SELECT * FROM relations_v1")
            self.db.execute("DROP TABLE relations_v1")
            self.db.execute("UPDATE metadata SET value=? WHERE key='schema_version'", (SCHEMA_VERSION,))

    def _schema(self):
        # Statement by statement: executescript would commit the surrounding transaction.
        for statement in SCHEMA.split(";"):
            if statement.strip():
                self.db.execute(statement)

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

    def resolve_pending_delivery(self, event="arrived"):
        predicate, value = ("ownership_status", "owned") if event == "returned" else ("open_loop", "awaiting_delivery")
        matches = {r["entity_key"] for r in self.records() if r["predicate"] == predicate and r["value"] == value}
        if len(matches) != 1:
            raise KernelError("Delivery reference is unknown or ambiguous. Specify the object.")
        return next(iter(matches))

    def _delivery_records(self, entity):
        return [r for r in self.records() if r["entity_key"] == entity and
                r["predicate"] in {"delivery_status", "ownership_status", "open_loop"}]

    def _transition(self, entity, event, evidence, expected_ids=None):
        entity = key(entity, "entity")
        evidence = text(evidence)
        reject_secrets(evidence)
        states = {
            "ordered": ("pending", "not_received", "awaiting_delivery"),
            "not_arrived": ("pending", "not_received", "awaiting_delivery"),
            "arrived": ("delivered", "owned", "resolved"),
            "returned": ("delivered", "returned", "resolved"),
        }
        if not isinstance(event, str) or event not in states:
            raise KernelError("Unsupported delivery transition.")
        if event != "ordered":
            self.resolve_entity(entity)
        current = self._delivery_records(entity)
        if expected_ids is not None and sorted(r["id"] for r in current) != sorted(expected_ids):
            raise KernelError("Transition proposal is stale; review the current state first.")
        ids = []
        for predicate, value in zip(("delivery_status", "ownership_status", "open_loop"), states[event]):
            targets = [r for r in current if r["predicate"] == predicate]
            if len(targets) > 1:
                raise KernelError("Transition has conflicting targets; resolve them first.")
            if targets and targets[0]["value"] == value:
                ids.append(targets[0]["id"])
            elif targets:
                ids.append(self._correct(targets[0]["id"], value, evidence))
            else:
                ids.append(self._insert(entity, predicate, value, evidence, kind="object"))
        self._event("transition", {"statement_ids": ids, "event": event})
        return ids

    def transition(self, entity, event, evidence):
        with self.db:
            ids = self._transition(entity, event, evidence)
        return {"event": event, "statements": [self.inspect(i) for i in ids]}

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

    ROWS = """SELECT s.*, e.entity_key, e.label, e.kind, e.aliases,
              v.source_kind, v.source_ref, v.source_text
              FROM statements s JOIN entities e ON e.id=s.entity_id
              JOIN evidence v ON v.id=s.evidence_id
              WHERE s.scope=? AND e.scope=? AND v.scope=?"""

    def _row(self, statement_id):
        row = self.db.execute(self.ROWS + " AND s.id=?", (self.scope, self.scope, self.scope, statement_id)).fetchone()
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

    def _assumption_links(self):
        return [(r[0], r[1]) for r in self.db.execute(
            "SELECT from_statement,to_statement FROM relations WHERE scope=? AND kind='depends_on' ORDER BY from_statement,to_statement",
            (self.scope,))]

    def _attach_assumptions(self, records, states):
        """Staleness is computed at query time: an assumption that is no longer active marks its dependents."""
        links = {}
        for child, parent in self._assumption_links():
            links.setdefault(child, []).append(parent)
        for row in records:
            assumptions = []
            for parent in links.get(row["id"], []):
                state = states.get(parent)
                if state is None:
                    continue
                assumptions.append({"id": parent, "effective_state": state["effective_state"],
                                    "superseded_by": state["superseded_by"]})
            row["assumptions"] = assumptions
            row["stale"] = any(a["effective_state"] != "active" for a in assumptions)
        return records

    def inspect(self, statement_id, as_of=None):
        row = self._decode(self._row(statement_id), as_of)
        parents = [p for c, p in self._assumption_links() if c == row["id"]]
        states = {p: self._decode(self._row(p), as_of) for p in parents}
        return self._attach_assumptions([row], states)[0]

    def records(self, as_of=None, history=False):
        rows = self.db.execute(self.ROWS + " ORDER BY s.recorded_at, s.id", (self.scope, self.scope, self.scope))
        records = [self._decode(r, as_of) for r in rows]
        states = {r["id"]: r for r in records}
        self._attach_assumptions(records, states)
        if history:
            return records
        return [r for r in records if r["effective_state"] == "active"
                and r["assertion_kind"] in {"user_statement", "observed"}]

    def depend(self, statement_id, assumption_id):
        """Declare that a statement (a decision, a recommendation) rests on another one.

        The owner declares dependencies explicitly; the kernel never infers them. The link
        targets that exact version: when the assumption is corrected, the dependent becomes stale
        until the owner corrects it, revokes it, or reaffirms it against the new version.
        """
        with self.db:
            child, parent = self._row(statement_id), self._row(assumption_id)
            if child["id"] == parent["id"]:
                raise KernelError("A statement cannot depend on itself.")
            if child["lifecycle"] != "active":
                raise KernelError("Only an active statement can declare a dependency.")
            links = {}
            for c, p in self._assumption_links():
                links.setdefault(c, set()).add(p)
            if (child["id"], parent["id"]) in {(c, p) for c, ps in links.items() for p in ps}:
                return {"id": None, "kind": "depends_on", "status": "exists"}
            frontier, seen = [parent["id"]], set()
            while frontier:
                node = frontier.pop()
                if node == child["id"]:
                    raise KernelError("Dependency would create a cycle.")
                if node not in seen:
                    seen.add(node)
                    frontier.extend(links.get(node, ()))
            relation_id = identifier()
            self.db.execute("INSERT INTO relations VALUES(?,?,?,?,?,?,?)",
                            (relation_id, self.scope, "depends_on", child["id"], parent["id"], None, None))
            self._event("depend", {"relation_id": relation_id})
        return {"id": relation_id, "kind": "depends_on", "status": "added"}

    def dependents(self, statement_id, as_of=None):
        """Current statements that declared this exact version as an assumption."""
        self._row(statement_id)
        return [r for r in self.records(as_of) if any(a["id"] == statement_id for a in r["assumptions"])]

    def stale(self, as_of=None):
        return [r for r in self.records(as_of) if r["stale"]]

    def reaffirm(self, statement_id):
        """Owner decision: the dependent still holds under the assumption's current version.

        Each stale link is moved to the successor of the changed assumption. An assumption that
        was revoked or expired has no successor; the dependent must then be corrected or revoked.
        """
        with self.db:
            row = self.inspect(statement_id)
            if not row["stale"]:
                raise KernelError("Statement has no stale assumptions.")
            moved = []
            for assumption in row["assumptions"]:
                if assumption["effective_state"] == "active":
                    continue
                # Follow the correction chain: the assumption may have changed more than once.
                successor, seen = assumption["superseded_by"], set()
                while successor and successor not in seen and self.inspect(successor)["effective_state"] != "active":
                    seen.add(successor)
                    successor = self.inspect(successor)["superseded_by"]
                if not successor or successor in seen:
                    raise KernelError("A changed assumption has no current successor; correct or revoke the dependent instead.")
                self.db.execute("DELETE FROM relations WHERE scope=? AND kind='depends_on' AND from_statement=? AND to_statement=?",
                                (self.scope, row["id"], assumption["id"]))
                self.db.execute("INSERT INTO relations VALUES(?,?,?,?,?,?,?)",
                                (identifier(), self.scope, "depends_on", row["id"], successor, None, None))
                moved.append({"from": assumption["id"], "to": successor})
            self._event("reaffirm", {"statement_id": row["id"], "moved": len(moved)})
        return {"id": row["id"], "status": "reaffirmed", "moved": moved}

    def context_relations(self, visible_keys):
        rows = self.db.execute("""SELECT c.entity_key AS child, p.entity_key AS parent
            FROM relations r JOIN entities c ON c.id=r.from_entity JOIN entities p ON p.id=r.to_entity
            WHERE r.scope=? AND c.scope=? AND p.scope=? AND r.kind='part_of'
            ORDER BY c.entity_key,p.entity_key""", (self.scope, self.scope, self.scope))
        return [dict(row) for row in rows if row["child"] in visible_keys and row["parent"] in visible_keys]

    def traces(self, limit=20):
        if type(limit) is not int or not 1 <= limit <= 100:
            raise KernelError("Trace limit must be between 1 and 100.")
        return [json.loads(row[0]) for row in self.db.execute(
            "SELECT trace FROM projections WHERE scope=? ORDER BY recorded_at DESC,id DESC LIMIT ?",
            (self.scope, limit))]

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
            ancestors = {parent_id}
            frontier = [parent_id]
            while frontier:
                node = frontier.pop()
                for row in self.db.execute("SELECT to_entity FROM relations WHERE scope=? AND kind='part_of' AND from_entity=?", (self.scope, node)):
                    if row[0] == child_id:
                        raise KernelError("Containment would create a cycle.")
                    if row[0] not in ancestors:
                        ancestors.add(row[0])
                        frontier.append(row[0])
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

    def propose(self, operation, payload, event_id=None):
        if not isinstance(operation, str) or operation not in {"remember", "correct", "transition"} or not isinstance(payload, dict):
            raise KernelError("Unsupported memory proposal.")
        payload = dict(payload)
        contracts = {
            "remember": ({"entity", "predicate", "value", "evidence", "valid_from", "valid_until"}, {"entity", "predicate", "value"}),
            "correct": ({"target_id", "value", "evidence", "valid_from", "valid_until"}, {"target_id", "value"}),
            "transition": ({"entity", "event", "evidence"}, {"entity", "event", "evidence"}),
        }
        allowed, required = contracts[operation]
        if set(payload) - allowed:
            raise KernelError("Proposal has unsupported fields.")
        if not required <= set(payload):
            raise KernelError("Proposal is missing required fields.")
        if operation == "remember":
            key(payload["entity"], "entity")
            key(payload["predicate"], "predicate")
        if operation == "correct":
            self._row(payload.get("target_id"))
        if operation == "transition":
            entity = key(payload["entity"], "entity")
            if not isinstance(payload["event"], str) or payload["event"] not in {"ordered", "not_arrived", "arrived", "returned"}:
                raise KernelError("Unsupported delivery transition.")
            if payload["event"] != "ordered":
                self.resolve_entity(entity)
            payload["entity"] = entity
            payload["expected_ids"] = sorted(r["id"] for r in self._delivery_records(entity))
        encoded = checked_value(payload)
        reject_secrets(encoded)
        proposal_id = identifier()
        with self.db:
            if event_id and self.db.execute("SELECT 1 FROM processed_events WHERE scope=? AND event_id=?", (self.scope, event_id)).fetchone():
                return None
            self.db.execute("INSERT INTO proposals VALUES(?,?,?,?,?,?)",
                            (proposal_id, self.scope, operation, encoded, "pending", self.clock()))
            if event_id:
                self.db.execute("INSERT INTO processed_events VALUES(?,?)", (self.scope, event_id))
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
            elif row["operation"] == "correct":
                new_id = self._correct(payload["target_id"], payload["value"], evidence,
                                       payload.get("valid_from"), payload.get("valid_until"))
            else:
                new_ids = self._transition(payload["entity"], payload["event"], evidence, payload["expected_ids"])
                new_id = new_ids[0]
            self.db.execute("UPDATE proposals SET status='accepted' WHERE id=?", (proposal_id,))
            self._event("approve", {"statement_id": new_id})
        if row["operation"] == "transition":
            return {"event": payload["event"], "statements": [self.inspect(i) for i in new_ids]}
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

    def mark_failed(self, projection_id, reason="state_changed"):
        trace = self.trace(projection_id)
        trace.update(delivery="failed", failure=key(reason, "failure code"))
        with self.db:
            self.db.execute("UPDATE projections SET trace=? WHERE id=? AND scope=?", (canonical(trace), projection_id, self.scope))

    def record_failure(self, operation):
        with self.db:
            self._event("failure", {"operation": key(operation, "operation"), "code": "local_request_failed"})

    def status(self):
        return {"scope": self.scope, "current_statements": len(self.records()),
                "stored_statements": self.db.execute("SELECT count(*) FROM statements WHERE scope=?", (self.scope,)).fetchone()[0],
                "pending_proposals": self.db.execute("SELECT count(*) FROM proposals WHERE scope=? AND status='pending'", (self.scope,)).fetchone()[0]}
