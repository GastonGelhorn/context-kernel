"""Scoped SQLite state, evidence, proposals, and metadata-only traces."""

import json
import os
from pathlib import Path
import sqlite3

from .common import KernelError, canonical, checked_value, identifier, key, reject_secrets, text, timestamp, timestamp_offset


SCHEMA_VERSION = "5"
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
 trust TEXT NOT NULL DEFAULT 'confirmed' CHECK(trust IN ('confirmed','captured','quarantined')),
 category TEXT, last_confirmed_at TEXT,
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
 provenance TEXT NOT NULL DEFAULT 'declared' CHECK(provenance IN ('declared','inferred')),
 recorded_at TEXT,
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
CREATE TABLE IF NOT EXISTS sessions(
 scope TEXT NOT NULL, session_id TEXT NOT NULL, host TEXT NOT NULL, chain TEXT NOT NULL,
 seen_at TEXT NOT NULL, PRIMARY KEY(scope,session_id));
CREATE TABLE IF NOT EXISTS turns(
 scope TEXT NOT NULL, session_id TEXT NOT NULL, turn_key TEXT NOT NULL, token TEXT NOT NULL UNIQUE,
 origin TEXT NOT NULL CHECK(origin IN ('interactive','continuation','unknown')),
 prompt_digest TEXT NOT NULL, prompt_excerpt TEXT, projection_id TEXT,
 delivered_ids TEXT NOT NULL DEFAULT '[]', captured_ids TEXT NOT NULL DEFAULT '[]',
 gate_count INTEGER NOT NULL DEFAULT 0, flags TEXT NOT NULL DEFAULT '[]', reply_excerpt TEXT, opened_at TEXT NOT NULL, closed_at TEXT,
 notes TEXT,
 expires_at TEXT NOT NULL, PRIMARY KEY(scope,session_id,turn_key));
CREATE TABLE IF NOT EXISTS judgments(
 scope TEXT NOT NULL, kind TEXT NOT NULL, question TEXT NOT NULL, model TEXT NOT NULL,
 state_digest TEXT NOT NULL, candidate_digest TEXT NOT NULL, p REAL NOT NULL, judged_at TEXT NOT NULL,
 PRIMARY KEY(scope,kind,question,model,state_digest,candidate_digest));
CREATE TABLE IF NOT EXISTS captures_log(
 scope TEXT NOT NULL, session_id TEXT NOT NULL, turn_key TEXT, statement_id TEXT,
 outcome TEXT NOT NULL, reason TEXT, recorded_at TEXT NOT NULL, label TEXT);
CREATE TABLE IF NOT EXISTS tombstones(
 scope TEXT NOT NULL, entity_key TEXT NOT NULL, predicate TEXT NOT NULL, at TEXT NOT NULL,
 PRIMARY KEY(scope,entity_key,predicate));
CREATE TABLE IF NOT EXISTS scopes(scope TEXT PRIMARY KEY, policy TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS servers(
 scope TEXT NOT NULL, pid INTEGER NOT NULL, started TEXT NOT NULL, seen_at TEXT NOT NULL,
 PRIMARY KEY(scope,pid,started));
"""

# Columns added after the tables first shipped; a migration adds whichever are missing.
ADDED_COLUMNS = (
    ("statements", "trust", "TEXT NOT NULL DEFAULT 'confirmed' CHECK(trust IN ('confirmed','captured','quarantined'))"),
    ("statements", "category", "TEXT"),
    ("statements", "last_confirmed_at", "TEXT"),
    ("relations", "provenance", "TEXT NOT NULL DEFAULT 'declared' CHECK(provenance IN ('declared','inferred'))"),
    ("relations", "recorded_at", "TEXT"),
    ("turns", "reply_excerpt", "TEXT"),
    ("turns", "notes", "TEXT"),
    ("captures_log", "label", "TEXT"),
)
RELATION_COLUMNS = "id,scope,kind,from_statement,to_statement,from_entity,to_entity"


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
        if version and version[0] in {"1", "2", "3", "4"}:
            self._migrate(version[0])
            version = (SCHEMA_VERSION,)
        if not version or version[0] != SCHEMA_VERSION:
            raise KernelError("Unsupported memory schema.")

    def _migrate(self, version):
        """Copy-based and additive: no row is dropped. v1 -> v2 rebuilds `relations` (SQLite
        cannot alter a CHECK in place); later versions only add tables and columns."""
        with self.db:
            if version == "1":
                self.db.execute("ALTER TABLE relations RENAME TO relations_v1")
                self._schema()
                self.db.execute(f"INSERT INTO relations({RELATION_COLUMNS}) SELECT {RELATION_COLUMNS} FROM relations_v1")
                self.db.execute("DROP TABLE relations_v1")
            self._schema()
            for table, column, ddl in ADDED_COLUMNS:
                if column not in {r[1] for r in self.db.execute(f"PRAGMA table_info({table})")}:
                    self.db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")
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

    def _relation(self, kind, from_statement=None, to_statement=None, from_entity=None, to_entity=None, provenance="declared"):
        relation_id = identifier()
        self.db.execute("""INSERT INTO relations(id,scope,kind,from_statement,to_statement,from_entity,to_entity,provenance,recorded_at)
                           VALUES(?,?,?,?,?,?,?,?,?)""", (relation_id, self.scope, kind, from_statement, to_statement,
                                                        from_entity, to_entity, provenance, self.clock()))
        return relation_id

    def _insert(self, entity, predicate, value, evidence, valid_from=None, valid_until=None,
                assertion_kind="user_statement", source_kind="user_statement", label=None, kind="person", source_ref=None,
                trust="confirmed", category=None):
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
        if trust not in {"confirmed", "captured", "quarantined"}:
            raise KernelError("Invalid trust level.")
        self.db.execute("""INSERT INTO statements(id,scope,entity_id,predicate,value,assertion_kind,evidence_id,valid_from,
                           valid_until,recorded_at,lifecycle,superseded_by,trust,category,last_confirmed_at)
                           VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                        (statement_id, self.scope, entity_id, predicate, encoded, assertion_kind, evidence_id, start, end,
                         self.clock(), "active", None, trust, category, self.clock() if trust == "confirmed" else None))
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

    def records(self, as_of=None, history=False, quarantined=False):
        """Current eligible evidence. Quarantined captures are stored but never delivered; the
        owner sees them in the inventory (`quarantined=True`) or the history."""
        rows = self.db.execute(self.ROWS + " ORDER BY s.recorded_at, s.id", (self.scope, self.scope, self.scope))
        records = [self._decode(r, as_of) for r in rows]
        states = {r["id"]: r for r in records}
        self._attach_assumptions(records, states)
        if history:
            return records
        return [r for r in records if r["effective_state"] == "active"
                and r["assertion_kind"] in {"user_statement", "observed"}
                and (quarantined or r["trust"] != "quarantined")]

    def depend(self, statement_id, assumption_id, provenance="declared"):
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
            if provenance not in {"declared", "inferred"}:
                raise KernelError("Invalid dependency provenance.")
            relation_id = self._relation("depends_on", child["id"], parent["id"], provenance=provenance)
            self._event("depend", {"relation_id": relation_id, "provenance": provenance})
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
                previous = self.db.execute("""SELECT provenance FROM relations WHERE scope=? AND kind='depends_on'
                                              AND from_statement=? AND to_statement=?""", (self.scope, row["id"], assumption["id"])).fetchone()
                self.db.execute("DELETE FROM relations WHERE scope=? AND kind='depends_on' AND from_statement=? AND to_statement=?",
                                (self.scope, row["id"], assumption["id"]))
                self._relation("depends_on", row["id"], successor, provenance=previous[0] if previous else "declared")
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

    def _correct(self, statement_id, value, evidence, valid_from=None, valid_until=None, **origin):
        old = self._row(statement_id)
        if old["lifecycle"] != "active" or self._decode(old)["effective_state"] != "active":
            raise KernelError("Correction target is no longer active.")
        start = timestamp(valid_from) if valid_from else self.clock()
        if start < old["valid_from"]:
            raise KernelError("Correction cannot start before its target.")
        new_id = self._insert(old["entity_key"], old["predicate"], value, evidence,
                              valid_from=start, valid_until=valid_until, **origin)
        end = min(start, old["valid_until"]) if old["valid_until"] else start
        self.db.execute("UPDATE statements SET lifecycle='superseded', valid_until=?, superseded_by=? WHERE id=?", (end, new_id, statement_id))
        self._relation("corrects", new_id, statement_id)
        self._event("correct", {"statement_id": new_id})
        return new_id

    def correct(self, statement_id, value, evidence, valid_from=None, valid_until=None):
        with self.db:
            new_id = self._correct(statement_id, value, evidence, valid_from, valid_until)
        return self.inspect(new_id)

    def revoke(self, statement_id):
        with self.db:
            row = self._row(statement_id)
            self.db.execute("UPDATE statements SET lifecycle='revoked' WHERE id=?", (statement_id,))
            self._tombstone(row["entity_key"], row["predicate"])
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
            relation_id = self._relation("part_of", from_entity=child_id, to_entity=parent_id)
            self._event("relate", {"relation_id": relation_id})
        return {"id": relation_id, "kind": "part_of"}

    def forget(self, statement_id):
        with self.db:
            row = self._row(statement_id)
            ids = [r[0] for r in self.db.execute("SELECT id FROM statements WHERE scope=? AND entity_id=? AND predicate=?",
                                                (self.scope, row["entity_id"], row["predicate"]))]
            # Inferred recommendations that rested on any version are derived text that may quote the
            # forgotten value ("with three months, don't rewrite"): they go too. The user's own
            # statements that were linked to it stay; only their links are removed.
            marks = ",".join("?" * len(ids))
            derived = [r[0] for r in self.db.execute(f"""SELECT DISTINCT s.id FROM relations r JOIN statements s
                ON s.id=r.from_statement WHERE r.scope=? AND r.kind='depends_on' AND s.assertion_kind='inference'
                AND r.to_statement IN ({marks})""", (self.scope, *ids))]
            if derived:
                self.db.execute(f"DELETE FROM statements WHERE scope=? AND id IN ({','.join('?' * len(derived))})",
                                (self.scope, *derived))
            # Remove every version of this property, including revoked versions.
            self.db.execute("UPDATE statements SET superseded_by=NULL WHERE scope=? AND entity_id=? AND predicate=?",
                            (self.scope, row["entity_id"], row["predicate"]))
            self.db.execute("DELETE FROM statements WHERE scope=? AND entity_id=? AND predicate=?",
                            (self.scope, row["entity_id"], row["predicate"]))
            self.db.execute("DELETE FROM evidence WHERE scope=? AND id NOT IN (SELECT evidence_id FROM statements)", (self.scope,))
            self.db.execute("DELETE FROM entities WHERE scope=? AND id NOT IN (SELECT entity_id FROM statements)", (self.scope,))
            # Conservative invalidation prevents forgotten content surviving in derived records,
            # including turn excerpts and cached judgments that embed values.
            for table in ("plans", "projections", "proposals", "operations", "processed_events", "turns", "judgments"):
                self.db.execute(f"DELETE FROM {table} WHERE scope=?", (self.scope,))
            # A tombstone stops any write already in flight (a slow capture or inference) whose
            # evidence predates this request from bringing the property back.
            self._tombstone(row["entity_key"], row["predicate"])
            self._event("forget", {"removed_count": len(ids), "derived_removed": len(derived)})
        return {"status": "forgotten", "removed_count": len(ids), "derived_removed": len(derived),
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

    # Captures, tombstones, policy, sessions, turns, and cached judgments.

    def _tombstone(self, entity_key, predicate):
        self.db.execute("INSERT OR REPLACE INTO tombstones VALUES(?,?,?,?)", (self.scope, entity_key, predicate, self.clock()))

    def tombstoned_since(self, entity_key, predicate, since):
        row = self.db.execute("SELECT at FROM tombstones WHERE scope=? AND entity_key=? AND predicate=?",
                              (self.scope, entity_key, predicate)).fetchone()
        return bool(row and row[0] >= since)

    def policy(self):
        row = self.db.execute("SELECT policy FROM scopes WHERE scope=?", (self.scope,)).fetchone()
        return json.loads(row[0]) if row else None

    def set_policy(self, policy):
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO scopes VALUES(?,?)", (self.scope, checked_value(policy)))
            self._event("policy", {"categories": len(policy.get("categories", {}))})
        return policy

    def register_session(self, session_id, host, chain):
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO sessions VALUES(?,?,?,?,?)",
                            (self.scope, text(session_id, 256), host, canonical(chain), self.clock()))

    def register_server(self, parent):
        """An MCP server of this scope is running under this host process (pid and start time)."""
        if parent:
            with self.db:
                self.db.execute("INSERT OR REPLACE INTO servers VALUES(?,?,?,?)", (self.scope, parent[0], parent[1], self.clock()))

    def tools_available(self, session_id):
        """Whether a memory MCP server runs under the same host process as this session's hooks.
        Without one, nobody can act on capture requests, so none are pushed and none count as missed."""
        row = self.db.execute("SELECT chain FROM sessions WHERE scope=? AND session_id=?", (self.scope, session_id)).fetchone()
        if not row:
            return False
        chain = [tuple(entry) for entry in json.loads(row[0])]
        return any((r[0], r[1]) in chain for r in self.db.execute("SELECT pid, started FROM servers WHERE scope=?", (self.scope,)))

    def sessions_for_parent(self, parent):
        """Sessions whose recorded hook ancestry contains this (pid, start)."""
        if not parent:
            return []
        rows = self.db.execute("SELECT session_id, chain, seen_at FROM sessions WHERE scope=? ORDER BY seen_at DESC", (self.scope,))
        return [r["session_id"] for r in rows if list(parent) in json.loads(r["chain"])]

    def open_turn(self, session_id, turn_key, token, origin, prompt_digest, excerpt, expires_at):
        with self.db:
            self.db.execute("""INSERT OR REPLACE INTO turns(scope,session_id,turn_key,token,origin,prompt_digest,prompt_excerpt,
                               opened_at,expires_at) VALUES(?,?,?,?,?,?,?,?,?)""",
                            (self.scope, session_id, turn_key, token, origin, prompt_digest, excerpt, self.clock(), expires_at))

    def update_turn(self, session_id, turn_key, **fields):
        allowed = {"projection_id", "delivered_ids", "captured_ids", "gate_count", "flags", "closed_at", "prompt_excerpt",
                   "expires_at", "reply_excerpt", "notes"}
        if set(fields) - allowed:
            raise KernelError("Unsupported turn fields.")
        assignments = ",".join(f"{name}=?" for name in fields)
        values = [canonical(v) if isinstance(v, list) else v for v in fields.values()]
        with self.db:
            self.db.execute(f"UPDATE turns SET {assignments} WHERE scope=? AND session_id=? AND turn_key=?",
                            (*values, self.scope, session_id, turn_key))

    def turn(self, session_id, turn_key):
        row = self.db.execute("SELECT * FROM turns WHERE scope=? AND session_id=? AND turn_key=?",
                              (self.scope, session_id, turn_key)).fetchone()
        return self._turn(row)

    def turn_by_token(self, token):
        row = self.db.execute("SELECT * FROM turns WHERE scope=? AND token=?", (self.scope, token)).fetchone()
        return self._turn(row)

    def recent_turns(self, session_id, limit=4):
        return [self._turn(r) for r in self.db.execute(
            "SELECT * FROM turns WHERE scope=? AND session_id=? ORDER BY opened_at DESC LIMIT ?", (self.scope, session_id, limit))]

    @staticmethod
    def _turn(row):
        if not row:
            return None
        turn = dict(row)
        turn["delivered_ids"] = json.loads(turn["delivered_ids"])
        turn["captured_ids"] = json.loads(turn["captured_ids"])
        turn["flags"] = json.loads(turn["flags"])
        return turn

    def expire_turns(self):
        """Excerpts live only until their captures are resolved; rows are kept a day for metrics."""
        now = self.clock()
        with self.db:
            # Facts the gate saw but nobody captured before the excerpt expired count as missed.
            for row in self.db.execute("""SELECT session_id, turn_key, gate_count - json_array_length(captured_ids)
                                          FROM turns WHERE scope=? AND expires_at<=? AND prompt_excerpt IS NOT NULL
                                          AND origin='interactive' AND gate_count > json_array_length(captured_ids)""",
                                       (self.scope, now)).fetchall():
                for _ in range(row[2]):
                    self.db.execute("INSERT INTO captures_log(scope,session_id,turn_key,statement_id,outcome,reason,recorded_at) VALUES(?,?,?,?,?,?,?)",
                                    (self.scope, row[0], row[1], None, "missed", "excerpt_expired", now))
            self.db.execute("UPDATE turns SET prompt_excerpt=NULL WHERE scope=? AND expires_at<=?", (self.scope, now))
            self.db.execute("DELETE FROM turns WHERE scope=? AND expires_at<=? AND opened_at<=?",
                            (self.scope, now, timestamp_offset(now, -86400)))

    def judgments(self, kind, question, model, state_digest, digests):
        if not digests:
            return {}
        marks = ",".join("?" * len(digests))
        rows = self.db.execute(f"""SELECT candidate_digest, p FROM judgments WHERE scope=? AND kind=? AND question=?
                                   AND model=? AND state_digest=? AND candidate_digest IN ({marks})""",
                               (self.scope, kind, question, model, state_digest, *digests))
        return {r[0]: r[1] for r in rows}

    def save_judgments(self, kind, question, model, state_digest, scores):
        with self.db:
            self.db.executemany("INSERT OR REPLACE INTO judgments VALUES(?,?,?,?,?,?,?,?)",
                                [(self.scope, kind, question, model, state_digest, d, p, self.clock()) for d, p in scores.items()])
            count = self.db.execute("SELECT count(*) FROM judgments WHERE scope=?", (self.scope,)).fetchone()[0]
            if count > 20000:
                self.db.execute("""DELETE FROM judgments WHERE rowid IN (SELECT rowid FROM judgments WHERE scope=?
                                   ORDER BY judged_at LIMIT ?)""", (self.scope, count - 20000))

    def log_capture(self, session_id, turn_key, outcome, statement_id=None, reason=None, label=None):
        with self.db:
            self.db.execute("""INSERT INTO captures_log(scope,session_id,turn_key,statement_id,outcome,reason,recorded_at,label)
                               VALUES(?,?,?,?,?,?,?,?)""",
                            (self.scope, session_id, turn_key, statement_id, outcome, reason, self.clock(), label))

    def capture_counts(self, session_id, turn_key):
        def count(where, *args):
            return self.db.execute(f"SELECT count(*) FROM captures_log WHERE scope=? AND outcome IN ('captured','quarantined') {where}",
                                   (self.scope, *args)).fetchone()[0]
        return {"turn": count("AND session_id=? AND turn_key=?", session_id, turn_key),
                "session": count("AND session_id=?", session_id),
                "day": count("AND recorded_at>=?", timestamp_offset(self.clock(), -86400))}

    def pending_proposal_count(self):
        return self.db.execute("SELECT count(*) FROM proposals WHERE scope=? AND status='pending'", (self.scope,)).fetchone()[0]

    def captures_for_turn(self, session_id, turn_key):
        return [dict(r) for r in self.db.execute("""SELECT statement_id, outcome, reason, label FROM captures_log
                    WHERE scope=? AND session_id=? AND turn_key=? ORDER BY recorded_at""", (self.scope, session_id, turn_key))]

    def last_capture(self, session_id):
        for row in self.db.execute("""SELECT statement_id FROM captures_log WHERE scope=? AND session_id=?
                                      AND outcome IN ('captured','quarantined') ORDER BY recorded_at DESC""", (self.scope, session_id)):
            if self.db.execute("SELECT 1 FROM statements WHERE scope=? AND id=?", (self.scope, row[0])).fetchone():
                return row[0]
        return None

    def operations_since(self, since, names):
        marks = ",".join("?" * len(names))
        return self.db.execute(f"SELECT count(*) FROM operations WHERE scope=? AND recorded_at>=? AND operation IN ({marks})",
                               (self.scope, since, *names)).fetchone()[0]

    def capture_metrics(self):
        rows = self.db.execute("SELECT outcome, reason, count(*) FROM captures_log WHERE scope=? GROUP BY outcome, reason", (self.scope,))
        return [{"outcome": r[0], "reason": r[1], "count": r[2]} for r in rows]

    def confirm(self, statement_id):
        """Owner (or a validated chat turn) promotes a capture to confirmed."""
        with self.db:
            row = self._row(statement_id)
            if row["lifecycle"] != "active":
                raise KernelError("Only an active statement can be confirmed.")
            self.db.execute("UPDATE statements SET trust='confirmed', last_confirmed_at=? WHERE id=?", (self.clock(), statement_id))
            self._event("confirm", {"statement_id": statement_id})
        return self.inspect(statement_id)

    def undo_capture(self, statement_id):
        """Remove a captured version entirely and restore the version it replaced, if any."""
        with self.db:
            row = self._row(statement_id)
            if row["trust"] == "confirmed":
                raise KernelError("Only captured or quarantined statements can be undone; use revoke for confirmed facts.")
            previous = self.db.execute("""SELECT to_statement FROM relations WHERE scope=? AND kind='corrects'
                                          AND from_statement=?""", (self.scope, statement_id)).fetchone()
            self.db.execute("UPDATE statements SET superseded_by=NULL WHERE scope=? AND superseded_by=?", (self.scope, statement_id))
            self.db.execute("DELETE FROM statements WHERE scope=? AND id=?", (self.scope, statement_id))
            self.db.execute("DELETE FROM evidence WHERE scope=? AND id=?", (self.scope, row["evidence_id"]))
            restored = None
            if previous:
                old = self._row(previous[0])
                self.db.execute("""UPDATE statements SET lifecycle='active', valid_until=CASE WHEN valid_until=? THEN NULL
                                   ELSE valid_until END WHERE id=? AND lifecycle='superseded'""", (row["valid_from"], old["id"]))
                restored = old["id"]
            self._event("undo", {"statement_id": statement_id})
        return {"status": "undone", "id": statement_id, "restored": restored}

    def status(self):
        return {"scope": self.scope, "current_statements": len(self.records()),
                "stored_statements": self.db.execute("SELECT count(*) FROM statements WHERE scope=?", (self.scope,)).fetchone()[0],
                "pending_proposals": self.db.execute("SELECT count(*) FROM proposals WHERE scope=? AND status='pending'", (self.scope,)).fetchone()[0],
                "quarantined": len([r for r in self.records(quarantined=True) if r["trust"] == "quarantined"])}
