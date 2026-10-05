"""Authorized retrieval and bounded, explainable projections."""

from collections import defaultdict
from contextlib import closing
from dataclasses import dataclass
import json
import re
import sqlite3
import time

from .common import KernelError, canonical, digest, identifier, text, timestamp
from .planner import NeedPlan, infer_plan, rules_plan


POLICY_VERSION = "1"


def lexical_scores(query, records):
    tokens = list(dict.fromkeys(re.findall(r"[^\W_]+", query.lower(), re.UNICODE)))[:32]
    if not tokens or not records:
        return {}
    expression = " OR ".join('"' + token + '"' for token in tokens)
    # Corpus statistics are built only from already-authorized current records.
    with closing(sqlite3.connect(":memory:")) as index:
        index.execute("CREATE VIRTUAL TABLE candidates USING fts5(id UNINDEXED, body)")
        index.executemany("INSERT INTO candidates VALUES(?,?)", [(r["id"], canonical({
            "entity": r["entity_key"], "label": r["label"], "aliases": r["aliases"],
            "predicate": r["predicate"].replace("_", " "), "value": r["value"]})) for r in records])
        return {r[0]: -r[1] for r in index.execute("SELECT id,bm25(candidates) FROM candidates WHERE candidates MATCH ?", (expression,))}


@dataclass
class Projection:
    id: str
    content: str
    trace: dict
    plan: NeedPlan

    def to_dict(self):
        return {"id": self.id, "content": self.content, "trace": self.trace, "plan": self.plan.to_dict()}


class Compiler:
    def __init__(self, store, budget=2048, ollama=None):
        if not 256 <= budget <= 16384:
            raise KernelError("Projection byte budget must be between 256 and 16384.")
        self.store, self.budget, self.ollama = store, budget, ollama

    def project(self, query, strategy="rules", plan=None, as_of=None):
        started = time.perf_counter()
        query = text(query, 16384)
        at = timestamp(as_of) if as_of else self.store.clock()
        records = self.store.records(at)
        usage = {"calls": 0}
        if plan is None:
            if strategy == "inferred":
                if self.ollama is None:
                    raise KernelError("Inferred planning requires a local Ollama client.")
                plan, usage = infer_plan(query, records, self.ollama)
            elif strategy == "fts":
                plan = NeedPlan(strategy="fts")
            elif strategy == "rules":
                plan = rules_plan(query, records)
            else:
                raise KernelError("Unsupported selection strategy.")
        plan_id = self.store.save_plan(plan.to_dict())
        scores = lexical_scores(query, records)
        selected, critical, missing, reasons = set(), set(), [], {}
        for index, need in enumerate(plan.needs):
            matches = [r for r in records if not need.unavailable and r["predicate"] in need.predicates
                       and (not need.entities or r["entity_key"] in need.entities)]
            if not matches and need.critical:
                missing.append(index)
            for row in matches:
                selected.add(row["id"])
                if need.critical:
                    critical.add(row["id"])
                reasons[row["id"]] = "critical_need" if need.critical else "supporting_need"
        allow_lexical = plan.strategy == "fts" or "unknown_task" in plan.warnings
        if allow_lexical:
            for record_id in scores:
                selected.add(record_id)
                reasons.setdefault(record_id, "lexical_match")
        by_id = {r["id"]: r for r in records}
        ordered = sorted(selected, key=lambda i: (i not in critical, -scores.get(i, 0), i))
        conflicts = defaultdict(set)
        for i in ordered:
            row = by_id[i]
            conflicts[(row["entity_key"], row["predicate"])].add(canonical(row["value"]))
        conflict = any(len(values) > 1 for values in conflicts.values())
        snapshot = digest({"policy": POLICY_VERSION, "records": records})
        warnings = list(plan.warnings)
        if missing:
            warnings.append("missing_critical_evidence")
        if conflict:
            warnings.append("conflicting_claims")
        packet = {"type": "context_data", "policy": POLICY_VERSION, "as_of": at,
                  "warnings": warnings, "claims": []}

        def entry(row):
            return {"id": row["id"], "entity": row["entity_key"], "predicate": row["predicate"],
                    "value": row["value"], "attribution": row["source_kind"], "evidence_id": row["evidence_id"],
                    "valid_from": row["valid_from"], "valid_until": row["valid_until"]}

        included, excluded = [], {}
        required_packet = packet | {"claims": [entry(by_id[i]) for i in ordered if i in critical]}
        status = "ok"
        if len(canonical(required_packet).encode()) > self.budget:
            status = "insufficient_context"
            warnings.append("critical_budget_overflow")
            excluded.update({i: "critical_budget_overflow" for i in ordered})
        else:
            for i in ordered:
                candidate = entry(by_id[i])
                trial = packet | {"claims": packet["claims"] + [candidate]}
                if len(canonical(trial).encode()) <= self.budget:
                    packet["claims"].append(candidate)
                    included.append(i)
                else:
                    excluded[i] = "optional_budget"
        if not included and status == "ok":
            status = "empty"
        if missing and status in {"ok", "empty"}:
            status = "incomplete"
        content = canonical(packet) if included or warnings else ""
        if len(content.encode()) > self.budget:
            content = ""
            status = "insufficient_context"
        trace = {"id": identifier(), "scope": self.store.scope, "snapshot": snapshot,
                 "policy": POLICY_VERSION, "plan_id": plan_id, "status": status,
                 "as_of": at, "historical": as_of is not None,
                 "selected": included, "reasons": {i: reasons[i] for i in included},
                 "excluded": excluded, "missing_needs": missing, "warnings": warnings,
                 "bytes": len(content.encode()), "delivery": "prepared", "host_attachment": "unknown",
                 "observation": "projection_only", "usage": usage,
                 "latency_ms": round((time.perf_counter() - started) * 1000, 3)}
        self.store.save_trace(trace)
        return Projection(trace["id"], content, trace, plan)

    def revalidate(self, projection):
        at = projection.trace["as_of"] if projection.trace["historical"] else None
        records = self.store.records(at)
        visible = {r["id"] for r in records}
        if not set(projection.trace["selected"]) <= visible:
            raise KernelError("Context changed before delivery; regenerate the projection.")
        if digest({"policy": POLICY_VERSION, "records": records}) != projection.trace["snapshot"]:
            raise KernelError("Context changed before delivery; regenerate the projection.")
