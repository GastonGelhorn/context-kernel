"""Dependencies the user never has to declare: which facts a recommendation rested on.

At the end of a turn the host hands the hook the agent's final reply. If it recommends something,
jev judges which of the turn's premises it rests on: the claims the kernel delivered, plus facts
captured from the user's message in that same turn (a constraint said just now, not yet in memory).
Nothing else is a candidate: not the whole memory, not facts recorded later. The recommendation is
stored as an inference, never delivered as evidence, and linked with provenance `inferred`. When a
premise later changes, the kernel says the recommendation needs review, which is not the same as
saying it is wrong.
"""

import re

from .common import KernelError, canonical
from .language import query_terms
from .judge import JudgeError
from .turns import mask_secrets


RECOMMENDATION = ("Does `reply` recommend, advise, or decide a course of action (including advising against one), "
                  "rather than only reporting, asking a question, or declining to answer?")
# The same question about the reply's opening sentence, where the advice usually is: a caveat that follows
# ("I couldn't open the file, so I'm going on memory's summary") pulled a clear recommendation to 0.71-0.73 as a
# whole, against 0.93 as an opening, and the link was lost in 2 of 4 native runs (tests/compat_matrix.py).
OPENING = RECOMMENDATION.replace("`reply`", "`opening`")
# Measured: recommendations scored 0.80-0.95 as a whole and 0.76-0.97 as an opening; reports, questions, and
# refusals 0.02-0.62 as a whole and 0.39-0.57 as an opening. The higher of the two counts.
RECOMMENDATION_BAR = 0.7
RESTS_ON = ("Does the recommendation or decision in `query` rest on `candidate` being true, so that if "
            "`candidate` changed the recommendation might need to change?")
# Measured on 15 labelled reply/premise pairs with the local model: advice that rests on a premise
# scored 0.87-0.95, or 0.57 when phrased loosely ("hard to estimate in three months"); unrelated
# premises scored 0.08-0.62. No single bar separates them, but every weak true link names the
# premise's value and no false one does. So: link at STRONG, or at WEAK when the reply names it.
STRONG = 0.85
WEAK = 0.5
REPLY_LIMIT = 4000
FENCE = re.compile(r"```.*?(```|$)", re.S)


def mentions(reply, value):
    """The reply names the premise's value: every word of a short value, or two of a longer one."""
    terms = [t for t in query_terms(value if isinstance(value, str) else canonical(value)) if len(t) > 1]
    words = set(query_terms(reply))
    return bool(terms) and (all(t in words for t in terms) if len(terms) <= 3 else len(set(terms) & words) >= 2)


def first_sentence(reply):
    """The advice's opening sentence; a bare "Sí." or "No." opener is joined to the one after it."""
    flat = re.sub(r"\s+", " ", reply).strip()
    match = re.search(r"(.{20,}?[.!?])(\s|$)", flat)
    return (match.group(1) if match else flat)[:300]


def infer(store, judge, turn, reply, allow_remote=False, deadline=None):
    """Record a recommendation and its inferred premises. Returns metadata only."""
    # The agent sometimes repeats the kernel's receipt in its answer; that is not part of the advice.
    reply = "\n".join(line for line in (reply or "").splitlines() if not line.strip().startswith("Memory:")).strip()
    # A command or snippet the user asked for is an answer, not advice: only the prose around code
    # can recommend, and it is what gets stored ("fly deploy -a checkout-stg" is not a decision).
    reply = FENCE.sub(" ", reply).strip()
    if not turn or len(reply) < 40:
        return {"calls": 0, "linked": 0}
    current = {r["id"]: r for r in store.records()}
    premise_ids = [i for i in dict.fromkeys(turn["delivered_ids"] + turn["captured_ids"]) if i in current]
    if not premise_ids:
        return {"calls": 0, "linked": 0}
    lines = [f"{current[i]['entity_key']} {current[i]['predicate'].replace('_', ' ')}: {current[i]['value']}" for i in premise_ids]
    query = mask_secrets(reply)[:REPLY_LIMIT]
    try:
        judge.require_local(allow_remote)
        timeout = deadline.timeout(judge.timeout) if deadline else None
        verdict, _ = judge.ask({"reply": query, "opening": first_sentence(query)},
                               {"recommends": ("noul", RECOMMENDATION), "opening": ("noul", OPENING)}, timeout=timeout)
        recommends = max(verdict["recommends"], verdict.get("opening", 0.0))
        if recommends < RECOMMENDATION_BAR:
            return {"calls": 1, "linked": 0, "recommends": round(recommends, 3)}
        timeout = deadline.timeout(judge.timeout) if deadline else None
        scores, usage = judge.rank(query, [re.sub(r"\s+", " ", l) for l in lines], no_cache=True,
                                   timeout=timeout, question=RESTS_ON)
    except JudgeError as exc:
        return {"calls": 1, "linked": 0, "failure": str(exc)}
    premises = [(scores.get(n, 0.0), i) for n, i in enumerate(premise_ids)
                if scores.get(n, 0.0) >= STRONG or (scores.get(n, 0.0) >= WEAK and mentions(query, current[i]["value"]))]
    if not premises:
        return {"calls": 1, "linked": 0, "scores": {current[i]["entity_key"] + "." + current[i]["predicate"]: round(scores.get(n, 0.0), 3)
                                                     for n, i in enumerate(premise_ids)}}
    premises.sort(reverse=True)
    anchor = current[premises[0][1]]
    # A premise forgotten while the judge ran must not be linked or resurrected.
    live = {r["id"] for r in store.records()}
    premises = [(p, i) for p, i in premises if i in live and not store.tombstoned_since(
        current[i]["entity_key"], current[i]["predicate"], turn["opened_at"])]
    if not premises:
        return {"calls": 1, "linked": 0}
    with store.db:
        recommendation = store._insert(anchor["entity_key"], "recommendation", first_sentence(reply),
                                       first_sentence(reply), assertion_kind="inference", source_kind="agent_reply",
                                       source_ref=f"turn:{turn['token'][:8]}", trust="captured", category="project_decisions",
                                       kind=anchor["kind"])
    for _, premise in premises:
        store.depend(recommendation, premise, provenance="inferred")
    # Decisions the user stated in this turn rest on the same premises.
    for decision in turn["captured_ids"]:
        row = current.get(decision)
        if row and row["predicate"] in {"decision", "plan"}:
            for _, premise in premises:
                if premise != decision:
                    try:
                        store.depend(decision, premise, provenance="inferred")
                    except KernelError:
                        pass  # a cycle or an inactive decision: the link is simply not added
    return {"calls": 1, "linked": len(premises), "recommendation_id": recommendation,
            "latency_ms": usage.get("latency_ms")}


def stale_recommendations(records):
    """Inferred recommendations whose premises changed, with the pairs those premises described and the
    pairs of what replaced them: a decision record superseded by another record lives under another key
    (adr_0003 by adr_0004), and a question about the new one must bring the review too."""
    by_id = {r["id"]: r for r in records}
    found = []
    for row in records:
        if row["assertion_kind"] != "inference" or row["effective_state"] != "active" or not row["stale"]:
            continue
        changed = [a for a in row["assumptions"] if a["effective_state"] != "active"]
        pairs = {(by_id[a["id"]]["entity_key"], by_id[a["id"]]["predicate"]) for a in changed if a["id"] in by_id}
        for assumption in changed:
            successor, seen = assumption.get("superseded_by"), set()
            while successor in by_id and successor not in seen:
                seen.add(successor)
                pairs.add((by_id[successor]["entity_key"], by_id[successor]["predicate"]))
                successor = by_id[successor].get("superseded_by")
        found.append({"id": row["id"], "recorded_at": row["recorded_at"], "changed": [a["id"] for a in changed],
                      "pairs": sorted(pairs)})
    return found
