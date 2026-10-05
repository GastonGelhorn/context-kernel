"""Facts captured from the conversation, without commands and without approving each one.

The host agent's own model proposes what was said (`memory_capture`); jev judges whether the
recorded prompt affirms it and what kind of fact it is; this module decides what may be stored,
at which trust level, and what must wait for the user. A judgment is a signal for this policy,
never the policy itself: correctness, authorization, and session origin are checked separately.
"""

import re

from .common import KernelError, canonical, key
from .judge import JudgeError
from .language import fold, query_terms
from .turns import do_not_remember


CATEGORIES = {
    "project_state": "the status, scope, or progress of a project or piece of work",
    "project_decisions": "a decision, plan, or choice the user or their team made",
    "constraints": "a deadline, budget, limit, requirement, or condition that work must respect",
    "roles_and_relations": "who holds a role or relation for the user or their project (manager, approver, client, team)",
    "preferences": "how the user likes to work or communicate",
    "personal_attributes": "health, family, finances, location, or other private details about the user",
    "third_party_sensitive": "health, family, finances, or other private details about another person",
}
DEFAULT_POLICY = {
    "auto_capture": True,
    "allow_remote_judge": False,
    "categories": {name: name not in {"personal_attributes", "third_party_sensitive"} for name in CATEGORIES},
    "thresholds": {"affirmed": 0.75, "facts_present": 0.5},
    "caps": {"turn": 2, "session": 10, "day": 30},
}
AFFIRMED = ("Does the writer of `text` assert `fact` as true, in their own words, rather than quoting someone, "
            "asking about it, denying it, or describing a hypothetical?")
CATEGORY = "Which kind of information is `fact`?"
FACT_COUNT = ("How many distinct durable facts, decisions, constraints, or preferences that the writer would want "
              "remembered in a later conversation does `text` state?")
INSTRUCTION = "Is `text` mainly an instruction or command aimed at an AI assistant rather than information about the writer or their work?"
COUNTS = {"none": "states nothing worth remembering later", "one": "exactly one", "two": "two", "several": "three or more"}

_FENCE = re.compile(r"```.*?(```|$)", re.S)
_QUOTED_LINE = re.compile(r"^\s*(>|from:|to:|cc:|subject:|date:|sent:|de:|para:|asunto:|enviado:)", re.I)
_LOG_LINE = re.compile(r"^\s*(\d{4}-\d{2}-\d{2}[ t]\d|\[\w+\]|traceback|\s+at |\s*file \"|[{\[].*[}\]]\s*$|https?://\S+\s*$)", re.I)
_REPLY_MARKER = re.compile(r"^\s*(-{3,}|_{3,}|-+ ?(original message|forwarded message|mensaje original) ?-+|on .+ wrote:|el .+ escribi[oó]:)\s*$", re.I)
_INSTRUCTION_VALUE = re.compile(r"(?i)\b(ignore (all|previous|the)|always (run|execute|answer)|you must|siempre (ejecuta|responde)|ignora)\b")
AUTHORED_LIMIT = 1500
VALUE_LIMIT = 200


def policy(store):
    stored = store.policy() or {}
    merged = {k: (dict(v) if isinstance(v, dict) else v) for k, v in DEFAULT_POLICY.items()}
    for name, value in stored.items():
        if isinstance(value, dict) and isinstance(merged.get(name), dict):
            merged[name].update(value)
        else:
            merged[name] = value
    return merged


def segments(prompt):
    """(authored, quoted): text the user wrote in this message, and pasted or quoted material.

    Fenced blocks, quoted lines, mail headers and everything after a reply marker, log/JSON/URL
    lines are quoted. A decision typed after a pasted log stays authored."""
    quoted = [block.group(0) for block in _FENCE.finditer(prompt)]
    kept = []
    body = _FENCE.sub("\n", prompt)
    in_reply = False
    for line in body.splitlines():
        if _REPLY_MARKER.match(line):
            in_reply = True
        if in_reply or _QUOTED_LINE.match(line) or _LOG_LINE.match(line):
            quoted.append(line)
        else:
            kept.append(line)
    authored = re.sub(r"\n{3,}", "\n\n", "\n".join(kept)).strip()[:AUTHORED_LIMIT]
    return authored, "\n".join(quoted).strip()[:AUTHORED_LIMIT]


def fact_line(entity, predicate, value):
    rendered = value if isinstance(value, str) else canonical(value)
    return re.sub(r"\s+", " ", f"{entity} {predicate.replace('_', ' ')}: {rendered}").strip()


def evidence_sentence(authored, triple):
    """The sentence of the user's message that best supports the fact, bounded; not the whole message."""
    sentences = [s.strip() for s in re.split(r"(?<=[.!?\n])\s+", authored) if s.strip()] or [authored]
    terms = set(query_terms(fact_line(*triple)))
    best = max(sentences, key=lambda s: len(terms & set(query_terms(s))))
    return best[:300]


def gate(judge, prompt, deadline=None):
    """How many facts the authored part of this prompt states, and whether it is mostly an
    instruction. Cheap: one request; called only when the budget allows."""
    authored, _ = segments(prompt)
    if not authored or do_not_remember(prompt):
        return {"facts": 0, "instruction": 0.0, "calls": 0}
    timeout = deadline.timeout(judge.timeout) if deadline else None
    answers, usage = judge.ask({"text": authored}, {"count": ("choice", FACT_COUNT, COUNTS),
                                                    "instruction": ("noul", INSTRUCTION)}, timeout=timeout)
    best = max(answers["count"], key=answers["count"].get)
    facts = {"none": 0, "one": 1, "two": 2, "several": 3}[best]
    return {"facts": facts, "instruction": answers["instruction"], "calls": 1, "latency_ms": usage.get("latency_ms")}


def _decide(store, judge, turn, triple, rules, deadline):
    entity, predicate, value = triple
    authored, quoted = segments(turn["prompt_excerpt"] or "")
    line = fact_line(entity, predicate, value)
    timeout = deadline.timeout(judge.timeout) if deadline else None
    answers, _ = judge.ask({"text": authored, "fact": line},
                           {"affirmed": ("noul", AFFIRMED), "category": ("choice", CATEGORY, CATEGORIES)}, timeout=timeout)
    category = max(answers["category"], key=answers["category"].get)
    bar = rules["thresholds"]["affirmed"]
    if answers["affirmed"] >= bar:
        if turn["origin"] != "interactive":
            return "quarantined", "origin_unverified", category, authored
        if not rules["categories"].get(category, False):
            return "quarantined", "category_disabled", category, authored
        return "captured", None, category, authored
    if quoted:
        pasted, _ = judge.ask({"text": quoted, "fact": line}, {"affirmed": ("noul", AFFIRMED)}, timeout=timeout)
        if pasted["affirmed"] >= bar:
            return "quarantined", "quoted_source", category, quoted
    return "rejected", "not_affirmed", category, None


def capture(store, judge, turn, triples, deadline=None):
    """Validate and persist triples against one turn. Returns one result per triple; a result
    says "captured" only after the row exists."""
    rules = policy(store)
    results = []
    if not rules.get("auto_capture", True):
        return [{"status": "rejected", "reason": "capture_disabled"} for _ in triples]
    if turn["prompt_excerpt"] is None:
        reason = "expired" if turn["expires_at"] <= store.clock() else "do_not_remember"
        for _ in triples:
            store.log_capture(turn["session_id"], turn["turn_key"], "rejected", reason=reason)
        return [{"status": "rejected", "reason": reason} for _ in triples]
    try:
        judge.require_local(rules.get("allow_remote_judge", False))
    except JudgeError as exc:
        # Writes fail closed: without a usable judge nothing is stored.
        return [{"status": "rejected", "reason": "judge_unavailable", "detail": str(exc)} for _ in triples]
    for raw in triples:
        try:
            entity, predicate = key(raw.get("entity"), "entity"), key(raw.get("predicate"), "predicate")
            value = raw.get("value")
            rendered = value if isinstance(value, str) else canonical(value)
            if not rendered or len(rendered) > VALUE_LIMIT or _INSTRUCTION_VALUE.search(rendered):
                raise KernelError("value")
        except (KernelError, AttributeError, TypeError):
            store.log_capture(turn["session_id"], turn["turn_key"], "rejected", reason="invalid_triple")
            results.append({"status": "rejected", "reason": "invalid_triple"})
            continue
        result = _capture_one(store, judge, turn, (entity, predicate, value), rules, deadline)
        if result["status"] in {"captured", "quarantined"}:
            turn["captured_ids"] = turn["captured_ids"] + [result["id"]]
            store.update_turn(turn["session_id"], turn["turn_key"], captured_ids=turn["captured_ids"])
        results.append(result)
    return results


def _capture_one(store, judge, turn, triple, rules, deadline):
    entity, predicate, value = triple
    session, turn_key = turn["session_id"], turn["turn_key"]

    def done(status, reason=None, statement_id=None, **extra):
        store.log_capture(session, turn_key, status, statement_id, reason)
        return {"status": status, "reason": reason, "id": statement_id, "entity": entity, "predicate": predicate} | extra

    counts, caps = store.capture_counts(session, turn_key), rules["caps"]
    if counts["turn"] >= caps["turn"] or counts["session"] >= caps["session"] or counts["day"] >= caps["day"]:
        return done("omitted", "cap")
    if store.tombstoned_since(entity, predicate, turn["opened_at"]):
        return done("rejected", "forgotten")
    current = [r for r in store.records(quarantined=True) if r["entity_key"] == entity and r["predicate"] == predicate]
    if any(r["value"] == value for r in current):
        return done("duplicate", "already_known", next(r["id"] for r in current if r["value"] == value))
    try:
        status, reason, category, source = _decide(store, judge, turn, triple, rules, deadline)
    except JudgeError as exc:
        return done("rejected", "judge_unavailable", detail=str(exc))
    if status == "rejected":
        return done("rejected", reason)
    evidence = evidence_sentence(source, triple)
    origin = {"source_kind": "captured_prompt", "source_ref": f"turn:{turn['token'][:8]}", "trust": status, "category": category}
    # The judge ran outside any transaction; a forget that arrived meanwhile wins.
    if store.tombstoned_since(entity, predicate, turn["opened_at"]):
        return done("rejected", "forgotten")
    if len(current) > 1 or (current and current[0]["trust"] == "confirmed" and status == "captured"):
        proposal = store.propose("correct", {"target_id": current[0]["id"], "value": value, "evidence": evidence}) \
            if len(current) == 1 else store.propose("remember", {"entity": entity, "predicate": predicate, "value": value, "evidence": evidence})
        return done("needs_confirmation", "confirmed_value_differs" if len(current) == 1 else "conflicting_values",
                    proposal_id=proposal["id"], current=[r["value"] for r in current])
    with store.db:
        if current and status == "captured":
            new_id = store._correct(current[0]["id"], value, evidence, **origin)
        else:
            new_id = store._insert(entity, predicate, value, evidence, kind=_kind(category), **origin)
        store._event("capture", {"statement_id": new_id, "trust": status})
    return done(status, reason, new_id, category=category)


def _kind(category):
    return "project" if category in {"project_state", "project_decisions", "constraints"} else "person"


def describe_results(results):
    """One line for the user: what was stored, what is waiting, what was set aside."""
    saved = [f"{r['entity']}.{r['predicate']}" for r in results if r["status"] == "captured"]
    held = [f"{r['entity']}.{r['predicate']}" for r in results if r["status"] == "quarantined"]
    asks = [f"{r['entity']}.{r['predicate']}" for r in results if r["status"] == "needs_confirmation"]
    parts = []
    if saved:
        parts.append("saved " + ", ".join(saved))
    if held:
        parts.append("held for review " + ", ".join(held))
    if asks:
        parts.append("differs from a confirmed value, needs a yes: " + ", ".join(asks))
    return ("Memory: " + "; ".join(parts) + ". Say \"undo\" to take it back.") if parts else ""
