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
    return bool(definition and not contextual and not mentioned_entities(query, records))


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


def jev_candidate(entity, predicate, values):
    rendered = " | ".join(v if isinstance(v, str) else canonical(v) for v in values)
    return re.sub(r"\s+", " ", f"{entity} {predicate.replace('_', ' ')}: {rendered}").strip()


def jev_plan(query, records, jev, relations=(), lexical=(), cache=None, deadline=None, allow_remote=False):
    """Every authorized entity/property pair is judged against the question; the plan keeps the
    pairs above the supporting threshold, critical above the critical one. A pair that lands in
    the uncertain band below the supporting bar is kept as supporting only when the question
    lexically matches it (`lexical` holds those pairs): the band is decided by other evidence, not
    by lowering the bar.

    Selection fails open: if the judge is unavailable, out of time, or points to a hosted backend
    this scope has not authorized, the rules plan is used with a visible warning. Judgments are
    cached by (question, candidate line) digests in the store (`cache`), never by text, and the
    judge's own cache is bypassed because the query is the user's prompt."""
    if generic_question(query, records):
        return NeedPlan(strategy="jev"), {"calls": 0}
    if ambiguous_entity_reference(query, records):
        return NeedPlan(strategy="jev", warnings=("clarification_required",)), {"calls": 0}
    pairs, latest = {}, {}
    for row in records:
        pair = (row["entity_key"], row["predicate"])
        pairs.setdefault(pair, []).append(row["value"])
        latest[pair] = max(latest.get(pair, ""), row["recorded_at"])
    lexical = set(lexical)
    keys = sorted(pairs)
    warnings = ()
    if len(keys) > jev.max_pairs:
        # A local model answers about 50 ms per pair once warm and several seconds cold; the hook has
        # ten seconds. Judge the pairs the question mentions first, then the most recently recorded.
        keys = sorted(sorted(keys, key=lambda k: latest[k], reverse=True), key=lambda k: k not in lexical)[:jev.max_pairs]
        warnings = ("jev_inventory_capped",)
    if not keys:
        return NeedPlan(strategy="jev"), {"calls": 0}
    lines = [jev_candidate(e, p, pairs[(e, p)]) for e, p in keys]
    digests = [digest(line) for line in lines]
    usage = {"calls": 0, "cached": 0}
    try:
        jev.require_local(allow_remote)
        model = canonical(jev.describe())
        state = digest(query)
        known = cache.judgments("relevance", jev.question, model, state, digests) if cache else {}
        misses = [i for i, d in enumerate(digests) if d not in known]
        scores = {i: known[d] for i, d in enumerate(digests) if d in known}
        usage["cached"] = len(scores)
        if misses:
            timeout = deadline.timeout(jev.timeout) if deadline else None
            fresh, call = jev.rank(query, [lines[i] for i in misses], no_cache=True, timeout=timeout)
            usage.update(call, calls=1)
            for position, index in enumerate(misses):
                scores[index] = fresh.get(position, 0.0)
            if cache:
                cache.save_judgments("relevance", jev.question, model, state,
                                     {digests[i]: scores[i] for i in misses})
    except JudgeRemote as exc:
        fallback = rules_plan(query, records, relations)
        return NeedPlan(fallback.needs, "jev", fallback.warnings + ("judge_remote",)), usage | {"failure": str(exc)}
    except KernelError as exc:
        fallback = rules_plan(query, records, relations)
        return NeedPlan(fallback.needs, "jev", fallback.warnings + ("jev_unavailable",)), usage | {"calls": 1, "failure": str(exc)}
    ranked = sorted(((scores.get(i, 0.0), e, p) for i, (e, p) in enumerate(keys)), key=lambda t: (-t[0], t[1], t[2]))
    rescued = [f"{e}.{p}" for score, e, p in ranked if jev.band <= score < jev.supporting and (e, p) in lexical]
    needs = tuple(Need((p,), (e,), critical=score >= jev.critical) for score, e, p in ranked
                  if score >= jev.supporting or (jev.band <= score and (e, p) in lexical))[:16]
    # Scores are keyed by source pair, never by value: the trace stays metadata-only.
    usage.update(scores={f"{e}.{p}": round(score, 3) for score, e, p in ranked},
                 thresholds={"critical": jev.critical, "supporting": jev.supporting, "band": jev.band},
                 lexical_rescues=rescued, judged_pairs=len(keys), unjudged_pairs=len(pairs) - len(keys))
    return NeedPlan(needs, "jev", warnings), usage
