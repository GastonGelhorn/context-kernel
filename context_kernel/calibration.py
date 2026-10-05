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
    for row in rows:
        if kind == "facts_present":
            answers, _ = judge.ask({"text": row["message"]}, {"count": ("choice", FACT_COUNT, COUNTS),
                                                               "instruction": ("noul", INSTRUCTION)}, timeout=60)
            p = 1.0 - answers["count"].get("none", 0.0)
        else:
            question = AFFIRMED if kind == "affirmed" else ASKS["memory_forget"]
            answers, _ = judge.ask({"text": row["message"], "fact": row["fact"]}, {"q": ("noul", question)}, timeout=60)
            p = answers["q"]
        scored.append({"p": round(p, 3), "label": bool(row["label"]), "message": row["message"][:80]})
    sweep = []
    for step in range(5, 96, 5):
        t = step / 100
        tp = sum(1 for r in scored if r["p"] >= t and r["label"])
        fp = sum(1 for r in scored if r["p"] >= t and not r["label"])
        fn = sum(1 for r in scored if r["p"] < t and r["label"])
        tn = len(scored) - tp - fp - fn
        sweep.append({"threshold": t, "accuracy": round((tp + tn) / len(scored), 3),
                      "precision": round(tp / (tp + fp), 3) if tp + fp else None,
                      "recall": round(tp / (tp + fn), 3) if tp + fn else None})
    return {"kind": kind, "rows": sorted(scored, key=lambda r: -r["p"]), "sweep": sweep,
            "note": "facts_present scores 1 - P(none); the kernel's none_bar is 1 minus the chosen threshold"}
