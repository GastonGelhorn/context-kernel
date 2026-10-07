"""Recorded evidence needs: bounded rules and calibrated relevance from the judgment client."""

from dataclasses import asdict, dataclass
import json
import re

from .common import KernelError, canonical, digest, key
from .judge import JevCommand as Jev, JudgeRemote
from .language import ambiguous_entity_reference, fold, mentioned_entities, predicate_name, related_entities


WARNING_CODES = {"clarification_required", "unknown_task", "planner_failed", "inventory_overflow", "jev_unavailable",
                 "jev_inventory_capped", "judge_remote"}
STRATEGIES = {"rules", "fts", "inferred", "jev", "oracle", "recorded"}


@dataclass(frozen=True)
class Need:
    predicates: tuple[str, ...]
    entities: tuple[str, ...] = ()
    critical: bool = True
    unavailable: bool = False


@dataclass(frozen=True)
class NeedPlan:
    needs: tuple[Need, ...] = ()
    strategy: str = "rules"
    warnings: tuple[str, ...] = ()
    version: int = 1

    def to_dict(self):
        return json.loads(canonical(asdict(self)))

    @classmethod
    def from_dict(cls, value):
        if not isinstance(value, dict) or set(value) - {"needs", "strategy", "warnings", "version"}:
            raise KernelError("Invalid need plan fields.")
        if type(value.get("version", 1)) is not int or value.get("version", 1) != 1:
            raise KernelError("Unsupported need plan version.")
        raw_needs = value.get("needs")
        if not isinstance(raw_needs, list) or len(raw_needs) > 16:
            raise KernelError("Need plan must contain at most 16 needs.")
        needs = []
        for item in raw_needs:
            if not isinstance(item, dict) or set(item) - {"predicates", "entities", "critical", "unavailable"}:
                raise KernelError("Invalid evidence need.")
            predicates, entities = item.get("predicates"), item.get("entities", [])
            if not isinstance(predicates, list) or not 1 <= len(predicates) <= 16:
                raise KernelError("A need must specify predicates.")
            if not isinstance(entities, list) or len(entities) > 16:
                raise KernelError("Invalid entity requirements.")
            critical = item.get("critical", True)
            if not isinstance(critical, bool):
                raise KernelError("Need criticality must be boolean.")
            unavailable = item.get("unavailable", False)
            if not isinstance(unavailable, bool):
                raise KernelError("Need availability must be boolean.")
            needs.append(Need(tuple(key(p, "predicate") for p in predicates),
                              tuple(key(e, "entity") for e in entities), critical, unavailable))
        warnings = value.get("warnings", [])
        if not isinstance(warnings, list) or any(not isinstance(w, str) or w not in WARNING_CODES for w in warnings):
            raise KernelError("Invalid plan warning codes.")
        strategy = value.get("strategy", "recorded")
        if not isinstance(strategy, str) or strategy not in STRATEGIES:
            raise KernelError("Invalid plan strategy.")
        return cls(tuple(needs), strategy, tuple(warnings))


def generic_question(query, records=()):
    lowered = fold(query)
    contextual = re.search(r"\b(my|our|mine|we|remember|previous|earlier|this|that|mi|mis|mio|nuestro|nuestra|"
                           r"recuerda|anterior|este|esta|esto|ese|esa|eso)\b", lowered)
    definition = re.search(r"\b(what is|what are|explain|define|how does|how do|write a|show an example|"
                           r"que es|que son|explica|explicame|define|como funciona|escribe un|muestra un ejemplo)\b", lowered)
    if not (definition and not contextual and not mentioned_entities(query, records)):
        return False
    # "What is the checkout deadline?" names a stored key ("checkout_project.delivery_timeline") without
    # naming the entity exactly; that is a question about memory, not a definition.
    known = {t for r in records for t in re.split(r"[_\W]+", fold(r["entity_key"] + " " + r["predicate"])) if len(t) > 3}
    return not (set(re.findall(r"[^\W_]+", lowered)) & known)


def rules_plan(query, records, relations=()):
    if generic_question(query, records):
        return NeedPlan()
    if ambiguous_entity_reference(query, records):
        return NeedPlan(warnings=("clarification_required",))
    lowered = fold(query)
    targets = related_entities(mentioned_entities(query, records), relations)
    family = None
    project_pattern = r"\b(project|architecture|migration|deploy\w*|repository|decision|release|proyecto|arquitectura|migracion|despliegue|despleg\w*|repositorio|lanzamiento)\b"
    project_hint = bool(re.search(project_pattern, lowered)) or any(
        r["entity_key"] in targets and r.get("kind") == "project" for r in records)
    strong_career = bool(re.search(r"\b(job|offer|salary|career|employment|oferta|salario|sueldo|empleo|puesto de trabajo)\b", lowered))
    if (re.search(r"\b(job|offer|salary|career|employment|position|trabajo|oferta|salario|sueldo|empleo|puesto)\b", lowered)
            and (strong_career or not project_hint)):
        family = "career"
        candidates = ("salary", "employment", "work_schedule", "availability", "constraint")
    elif re.search(r"\b(gift|present|cake|chocolate|dinner|restaurant|food|regalo|tarta|pastel|cena|restaurante|comida)\b", lowered):
        family = "food_gift"
        candidates = ("allergy", "dietary_constraint", "constraint")
    elif re.search(r"\b(apartment|housing|house|stairs|elevator|piso|vivienda|casa|escaleras|ascensor)\b", lowered):
        family = "housing"
        candidates = ("mobility_limit", "accessibility", "budget", "constraint")
    elif re.search(r"\b(delivery|package|arriv\w*|ordered|waiting|device|return\w*|entrega|paquete|lleg\w*|pedido|esperando|devolv\w*|devuel\w*)\b", lowered):
        family = "delivery"
        candidates = ("delivery_status", "ownership_status", "open_loop", "constraint")
        if not targets and re.search(r"\b(it|eso|ese|esa|lo)\b", lowered):
            returning = bool(re.search(r"\b(return\w*|devolv\w*|devuel\w*)\b", lowered))
            predicate, value = ("ownership_status", "owned") if returning else ("open_loop", "awaiting_delivery")
            pending = {r["entity_key"] for r in records if r["predicate"] == predicate and r["value"] == value}
            if len(pending) != 1:
                return NeedPlan(warnings=("clarification_required",))
            targets = pending
    elif project_hint:
        family = "project"
        candidates = ("project_status", "decision", "constraint", "goal", "open_loop", "release_approver")
        if not targets and len({r["entity_key"] for r in records if r.get("kind") == "project"}) > 1:
            return NeedPlan(warnings=("clarification_required",))
    else:
        candidates = ()
    pairs = set()
    for row in records:
        if predicate_name(row["predicate"]) not in candidates:
            continue
        if targets and row["entity_key"] not in targets:
            # Personal constraints can still matter for a named employer or home.
            if family not in {"career", "housing"} or row["entity_key"] not in {"user", "usuario"}:
                continue
        pairs.add((row["entity_key"], row["predicate"]))
    needs = tuple(Need((predicate,), (entity,), critical=True) for entity, predicate in sorted(pairs))
    return NeedPlan(needs, warnings=() if needs else ("unknown_task",))


DURABLE_CATEGORIES = {"constraints", "project_decisions"}
DURABLE_WORDS = ("constraint", "deadline", "decision", "policy", "rule", "allergy", "budget", "limit", "approver", "adr_")


def durable(row):
    """Constraints and decisions outlive the chatter around them; they are judged before newer facts."""
    return row.get("category") in DURABLE_CATEGORIES or any(word in row["predicate"] for word in DURABLE_WORDS)


def jev_candidate(entity, predicate, values):
    rendered = " | ".join(v if isinstance(v, str) else canonical(v) for v in values)
    return re.sub(r"\s+", " ", f"{entity} {predicate.replace('_', ' ')}: {rendered}").strip()


# Measured on the local model (tev1-32k, 2026-10-06): one pair costs 0.19 s (short lines) to 0.31 s (the
# facts and questions of fixtures/relevance.json) warm, whatever the batch size and with no gain from
# concurrent requests, and a pair's score does not depend on the other pairs in the batch. 48 pairs took
# 9.5 s even with short lines, more than the prompt hook's whole budget. So the number of pairs judged is
# fitted to the time left, at the pace the last calls measured; what the hook does after the judgment
# (the packet, the trace, a keep-alive request) takes well under RESERVE_SECONDS.
DEFAULT_PAIR_SECONDS = 0.3
RESERVE_SECONDS = 1.0
PACE_KEY = "judge_pair_seconds"
# Pairs the question matches in words, constraints and decisions, and the few most recent are judged first, as
# time allows. The others are judged only with PATIENCE_SECONDS still to spare: in the labelled relevance set,
# 54 of the 60 facts that mattered were matched in words or were constraints or decisions, and 3 of the other
# 6 were among the 4 most recent. With 200 unrelated facts in memory, judging them anyway kept every prompt at
# the full 8 s (tests/bench.py).
RECENT_PAIRS = 4
PATIENCE_SECONDS = 3.0


def affordable_pairs(deadline, pace, limit, reserve=RESERVE_SECONDS):
    if deadline is None:
        return limit
    return max(0, min(limit, int((deadline.remaining() - reserve) / max(pace, 0.01))))


def select(scored, lexical, critical, supporting, band):
    """Needs from judged pairs, best first. Two independent signals agreeing beat one strong one: a pair the
    question matches in words (its key, value or cues) and jev puts at least in the band comes before a
    pair jev alone scores high. On paraphrased questions the judge scored most facts of a busy inventory
    above 0.5 (32 of 40 for "is there room before we ship?"), so jev alone cannot order them; the cues
    the agent wrote when it saved a fact supply the second signal. Returns [(tier, score, entity, predicate, critical)]."""
    chosen = []
    for score, entity, predicate in scored:
        matched = (entity, predicate) in lexical
        if matched and score >= band:
            tier = 0
        elif score >= critical:
            tier = 1
        elif score >= supporting:
            tier = 2
        else:
            continue
        # Two agreeing signals are as strong a reason to deliver as a high score alone: both go first.
        chosen.append((tier, score, entity, predicate, tier == 0 or score >= critical))
    return sorted(chosen, key=lambda c: (c[0], -c[1], c[2], c[3]))


def jev_plan(query, records, jev, relations=(), lexical=(), cache=None, deadline=None, allow_remote=False):
    """The pairs most likely to matter are judged against the question, as many as the time left allows:
    the ones the question matches in words first (strongest match first), then constraints and decisions,
    then the most recent. `lexical` maps a pair to its keyword score (key, value and cues; see
    compiler.lexical_scores); a plain set also works. `select` turns the scores into needs.

    Selection fails open: if the judge is unavailable, out of time, or points to a hosted backend
    this scope has not authorized, the rules plan is used with a visible warning. Judgments are
    cached by (question, candidate line) digests in the store (`cache`), never by text, and the
    judge's own cache is bypassed because the query is the user's prompt."""
    if generic_question(query, records):
        return NeedPlan(strategy="jev"), {"calls": 0}
    if ambiguous_entity_reference(query, records):
        return NeedPlan(strategy="jev", warnings=("clarification_required",)), {"calls": 0}
    pairs, latest, lasting = {}, {}, set()
    for row in records:
        pair = (row["entity_key"], row["predicate"])
        pairs.setdefault(pair, []).append(row["value"])
        latest[pair] = max(latest.get(pair, ""), row["recorded_at"])
        if durable(row):
            lasting.add(pair)
    strength = dict(lexical) if isinstance(lexical, dict) else {pair: 1.0 for pair in lexical}
    # An old deadline matters more than last week's channel name: by recency alone, no old constraint was
    # judged past 48 facts (tests/growth_check.py, v0.6).
    keys = sorted(sorted(pairs, key=lambda k: latest[k], reverse=True),
                  key=lambda k: (k not in strength, -strength.get(k, 0.0), k not in lasting))
    if not keys:
        return NeedPlan(strategy="jev"), {"calls": 0}
    lines = {k: jev_candidate(k[0], k[1], pairs[k]) for k in keys}
    digests = {k: digest(lines[k]) for k in keys}
    usage = {"calls": 0, "cached": 0}
    warnings = ()
    try:
        jev.require_local(allow_remote)
        model = canonical(jev.describe())
        state = digest(query)
        known = cache.judgments("relevance", jev.question, model, state, list(digests.values())) if cache else {}
        pace = float(cache.meta(PACE_KEY) or DEFAULT_PAIR_SECONDS) if cache and hasattr(cache, "meta") else DEFAULT_PAIR_SECONDS
        # Cached pairs cost nothing; the time left buys the next pairs in order, the likely ones first.
        recent = set(sorted(pairs, key=lambda k: latest[k], reverse=True)[:RECENT_PAIRS])
        likely = {k for k in keys if k in strength or k in lasting or k in recent}
        cached_keys = [k for k in keys if digests[k] in known]
        fresh_keys = [k for k in keys if digests[k] not in known and k in likely][:affordable_pairs(deadline, pace, jev.max_pairs)]
        spare = min(jev.max_pairs - len(fresh_keys),
                    affordable_pairs(deadline, pace, jev.max_pairs, RESERVE_SECONDS + PATIENCE_SECONDS) - len(fresh_keys))
        fresh_keys += [k for k in keys if digests[k] not in known and k not in likely][:max(0, spare)]
        judged = cached_keys + fresh_keys
        if len(judged) < len(keys):
            warnings = ("jev_inventory_capped",)
        scores = {k: known[digests[k]] for k in judged if digests[k] in known}
        usage["cached"] = len(scores)
        if fresh_keys:
            timeout = deadline.timeout(jev.timeout) if deadline else None
            fresh, call = jev.rank(query, [lines[k] for k in fresh_keys], no_cache=True, timeout=timeout)
            usage.update(call, calls=1)
            for position, k in enumerate(fresh_keys):
                scores[k] = fresh.get(position, 0.0)
            if cache:
                cache.save_judgments("relevance", jev.question, model, state, {digests[k]: scores[k] for k in fresh_keys})
                # A call also pays a fixed start-up; with a handful of pairs it would read as a slow pace.
                measured = (call.get("latency_ms") or 0) / 1000 / len(fresh_keys)
                if measured > 0 and len(fresh_keys) >= 8 and hasattr(cache, "meta"):
                    cache.meta(PACE_KEY, round(0.7 * pace + 0.3 * measured, 4))
        elif not judged:
            raise KernelError("No time left to judge relevance in this turn.")
    except JudgeRemote as exc:
        fallback = rules_plan(query, records, relations)
        return NeedPlan(fallback.needs, "jev", fallback.warnings + ("judge_remote",)), usage | {"failure": str(exc)}
    except KernelError as exc:
        fallback = rules_plan(query, records, relations)
        return NeedPlan(fallback.needs, "jev", fallback.warnings + ("jev_unavailable",)), usage | {"calls": 1, "failure": str(exc)}
    ranked = sorted(((scores[k], k[0], k[1]) for k in judged), key=lambda t: (-t[0], t[1], t[2]))
    chosen = select(ranked, strength, jev.critical, jev.supporting, jev.band)
    needs = tuple(Need((p,), (e,), critical=crit) for _, _, e, p, crit in chosen)[:16]
    # Scores are keyed by source pair, never by value: the trace stays metadata-only.
    usage.update(scores={f"{e}.{p}": round(score, 3) for score, e, p in ranked},
                 thresholds={"critical": jev.critical, "supporting": jev.supporting, "band": jev.band},
                 lexical_matches=[f"{e}.{p}" for _, _, e, p, _ in chosen if (e, p) in strength],
                 judged_pairs=len(judged), unjudged_pairs=len(pairs) - len(judged))
    return NeedPlan(needs, "jev", warnings), usage
