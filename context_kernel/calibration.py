"""Labelled fixtures in the shape `jev tune` reads, so thresholds come from measurements.

The kernel asks its questions about two fields (`text`, `fact`); `jev tune` grades one `candidate`.
Each fixture row is rendered as one candidate holding both, and the questions are restated to point
at that candidate. Tune with `jev tune --labels rows.jsonl --questions-file questions.txt`, then store
the chosen bar with `memory policy --threshold affirmed=VALUE`. Fixtures are fictional or authored
for calibration; no prompt is retained for this purpose.
"""

import json
from pathlib import Path

from .common import KernelError
from .judge import JudgeError, model_key


# Fourteen rows the kernel's thresholds must still separate with whatever model jev runs. Taken from
# fixtures/calibration.jsonl, where the shipped local model (tev1-32k) puts each one well clear of its bar:
# true statements at 0.86-0.97 against 0.75, non-statements at 0.02-0.25 against 0.6, the joke at 0.73
# against 0.6, a past and a current state at 0.97 and 0.01 against 0.7. A model that gets one of them
# wrong does not get to save anything until it is tuned.
CANARY = [
    ("affirmed", {"text": "Mi manager es Dani.", "fact": "user manager: Dani"}, True),
    ("affirmed", {"text": "Our budget for the migration is 20k EUR.", "fact": "migration budget: 20000 EUR"}, True),
    ("affirmed", {"text": "We decided to deploy only in the EU region.", "fact": "platform deploy region: EU only"}, True),
    ("affirmed", {"text": "Siempre usamos UTC en los logs.", "fact": "logging convention: UTC timestamps"}, True),
    ("affirmed", {"text": "Ana dijo que su manager es Dani.", "fact": "user manager: Dani"}, False),
    ("affirmed", {"text": "No estoy seguro de si el presupuesto es 20k.", "fact": "migration budget: 20000 EUR"}, False),
    ("affirmed", {"text": "The deadline might move to three weeks, not sure yet.", "fact": "checkout deadline: three weeks"}, False),
    ("affirmed", {"text": "Ya no trabajo en el proyecto aurora.", "fact": "user employment: aurora"}, False),
    ("sarcasm", {"text": "lol, our tests always pass on the first try", "fact": "test suite status: always passes"}, True),
    ("sarcasm", {"text": "Our on-call week starts on Tuesdays.", "fact": "on-call week start: Tuesday"}, False),
    ("past", {"text": "I used to work nights until last year.", "fact": "user work schedule: nights"}, True),
    ("past", {"text": "Mi manager es Dani.", "fact": "user manager: Dani"}, False),
    ("facts", {"text": "El plazo cambió: tenemos tres semanas."}, True),
    ("facts", {"text": "Thanks!"}, False),
]


def judge_state(store, judge):
    """(state, model key) of the judge for this scope: `verified` (passed the canary), `failed`, `changed`
    (another judge was verified here and this one not yet) or `unverified` (first use: the shipped
    thresholds apply until the background check runs)."""
    try:
        key = model_key(judge.describe())
    except (JudgeError, KernelError):
        return "unknown", None
    record = store.judge_record(key)
    if record and record["status"] in {"passed", "failed"}:
        return ("verified" if record["status"] == "passed" else "failed"), key
    if any(r["status"] == "passed" and r["model_key"] != key for r in store.judge_records()):
        return "changed", key
    return "unverified", key


def check(store, judge, timeout=60):
    """Run the canary with the scope's thresholds and record the verdict for this judge."""
    from .capture import AFFIRMED, CATEGORIES, CATEGORY, COUNTS, FACT_COUNT, PAST, SARCASM, policy
    thresholds = policy(store)["thresholds"]
    description = judge.describe()
    key = model_key(description)
    store.set_judge_record(key, "running", {"model": description.get("model"), "url": description.get("url")})
    rows, wrong = [], []
    try:
        for kind, state, expected in CANARY:
            if kind == "facts":
                answers, _ = judge.ask(state, {"count": ("choice", FACT_COUNT, COUNTS)}, timeout=timeout)
                p = answers["count"].get("none", 0.0)
                right = (p < thresholds["none_bar"]) == expected
            else:
                answers, _ = judge.ask(state, {"affirmed": ("noul", AFFIRMED), "category": ("choice", CATEGORY, CATEGORIES),
                                               "sarcasm": ("noul", SARCASM), "past": ("noul", PAST)}, timeout=timeout)
                p = answers[kind]
                bar = {"affirmed": thresholds["affirmed"], "sarcasm": thresholds.get("sarcasm", 0.6),
                       "past": thresholds.get("past", 0.7)}[kind]
                # A non-statement must not even reach the band that is held for review.
                right = p >= bar if expected else p < (thresholds.get("uncertain", bar) if kind == "affirmed" else bar)
            rows.append({"kind": kind, "expected": expected, "p": round(p, 3)})
            if not right:
                wrong.append(rows[-1] | {"text": state["text"][:60]})
    except (JudgeError, KernelError) as exc:
        # The judge did not answer: no verdict either way, so the next warm-up tries again.
        store.clear_judge_records(key)
        return {"status": "error", "model_key": key, "error": str(exc)[:200]}
    status = "failed" if wrong else "passed"
    detail = {"model": description.get("model"), "url": description.get("url"), "weights": description.get("weights"),
              "rows": len(rows), "wrong": wrong}
    store.set_judge_record(key, status, detail)
    return {"status": status, "model_key": key} | detail


QUESTIONS = {
    "affirmed": [
        "Does the writer of the message in `candidate` assert the fact in `candidate` as true, in their own words, rather than quoting someone, asking about it, denying it, or describing a hypothetical?",
        "Does the message in `candidate` state that the fact in `candidate` is true?",
    ],
    "facts_present": [
        "Does the message in `candidate` state at least one durable fact, decision, constraint, or preference that its writer would want remembered in a later conversation?",
        "Would a careful assistant save something from the message in `candidate` to memory?",
    ],
    "forget_asked": [
        "Does the writer of the message in `candidate` ask to forget, delete, or stop remembering the fact in `candidate`?",
    ],
    "standing_on": [
        "Does the writer of the message in `candidate` ask to always keep the fact in `candidate` in mind, in every conversation?",
    ],
    "standing_off": [
        "Does the writer of the message in `candidate` ask to stop always keeping the fact in `candidate` in mind?",
    ],
    "close_asked": [
        "Does the writer of the message in `candidate` say that the fact in `candidate` has already been done?",
        "Does the writer of the message in `candidate` say that the fact in `candidate` was cancelled, dropped, or has ended?",
    ],
    "decision_replaces": [
        "Does the later decision in `candidate` replace or reverse the earlier decision in `candidate`, so that the earlier one no longer holds?",
    ],
    "decision_commit": [
        "Does the commit message in `candidate` state a project-wide choice (a technology, tool, platform, policy, or convention that all later work must follow), rather than a change to one place in the code?",
    ],
}


def questions(kind):
    return QUESTIONS[kind]


def export(kind, fixtures):
    rows = []
    for line in Path(fixtures).read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        item = json.loads(line)
        if item.get("kind") != kind:
            continue
        candidate = "message: " + item["message"] + ("\nfact: " + item["fact"] if item.get("fact") else "")
        rows.append(json.dumps({"text": candidate, "label": "yes" if item["label"] else "no"}, ensure_ascii=False))
    if not rows:
        raise KernelError(f"No fixtures of kind {kind}.")
    return "\n".join(rows)


def score(kind, fixtures, judge):
    """Measure the kernel's own question (two fields, as captured) over the fixtures and sweep the
    threshold. Returns per-row scores and the sweep; nothing is stored."""
    from .capture import AFFIRMED, COUNTS, FACT_COUNT, INSTRUCTION
    from .mcp import ASKS
    rows = [json.loads(line) for line in Path(fixtures).read_text(encoding="utf-8").splitlines() if line.strip()]
    rows = [r for r in rows if r.get("kind") == kind]
    if not rows:
        raise KernelError(f"No fixtures of kind {kind}.")
    scored = []
    if kind == "decision_commit":
        # As the repository pass does: routine subjects and subjects without the language of a decision
        # never reach the judge (p 0); the rest are judged one per request.
        from .repository import DECISION_COMMIT, decision_language, routine
        for row in rows:
            filtered = routine(row["message"]) or not decision_language(row["message"])
            p = 0.0
            if not filtered:
                answers, _ = judge.ask({"commit": row["message"]}, {"decision": ("noul", DECISION_COMMIT)}, timeout=60)
                p = answers["decision"]
            scored.append({"p": round(p, 3), "label": bool(row["label"]), "message": row["message"][:80], "filtered": filtered})
    if kind == "decision_replaces":
        # One pair per request, as the repository pass asks it: the later decision is the query.
        from .repository import REPLACES
        for row in rows:
            result, _ = judge.rank(row["message"], [row["fact"]], no_cache=True, timeout=60, question=REPLACES)
            scored.append({"p": round(result.get(0, 0.0), 3), "label": bool(row["label"]), "message": row["message"][:80]})
    for row in rows if kind not in {"decision_commit", "decision_replaces"} else ():
        if kind == "facts_present":
            answers, _ = judge.ask({"text": row["message"]}, {"count": ("choice", FACT_COUNT, COUNTS),
                                                               "instruction": ("noul", INSTRUCTION)}, timeout=60)
            p = 1.0 - answers["count"].get("none", 0.0)
        else:
            question = {"affirmed": AFFIRMED, "forget_asked": ASKS["memory_forget"], "standing_on": ASKS["memory_standing_on"],
                        "standing_off": ASKS["memory_standing_off"], "close_asked": ASKS["memory_close"]}[kind]
            # An action asked with several questions is decided by the highest, as the server does.
            asked = question if isinstance(question, tuple) else (question,)
            answers, _ = judge.ask({"text": row["message"], "fact": row["fact"]},
                                   {f"q{i}": ("noul", q) for i, q in enumerate(asked)}, timeout=60)
            p = max(answers.values())
        scored.append({"p": round(p, 3), "label": bool(row["label"]), "message": row["message"][:80]})
    sweep = []
    judged = [r for r in scored if not r.get("filtered")]
    for step in range(5, 96, 5):
        t = step / 100
        tp = sum(1 for r in judged if r["p"] >= t and r["label"])
        fp = sum(1 for r in judged if r["p"] >= t and not r["label"])
        fn = sum(1 for r in scored if (r["p"] < t or r.get("filtered")) and r["label"])
        tn = len(scored) - tp - fp - fn
        sweep.append({"threshold": t, "accuracy": round((tp + tn) / len(scored), 3),
                      "precision": round(tp / (tp + fp), 3) if tp + fp else None,
                      "recall": round(tp / (tp + fn), 3) if tp + fn else None})
    return {"kind": kind, "rows": sorted(scored, key=lambda r: -r["p"]), "sweep": sweep,
            "note": "facts_present scores 1 - P(none); the kernel's none_bar is 1 minus the chosen threshold"}
