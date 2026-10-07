"""Opt-in measurement of selection on the labelled relevance set (fixtures/relevance.json).

Four workspaces of 30 facts and 60 questions (55% paraphrased: no word in common with the facts that
matter), labelled with the facts a careful colleague would take into account. Each question goes through
the kernel's own Compiler with the real jev scores, so the packet budget, the order of judging and the
selection rule are the ones a prompt gets. The cues are fixtures/relevance_cues.json, written blind to the
questions. `saas` and `freelance` are where the rule was chosen; `platform` and `personal` are held out.

    python3 -m tests.relevance_check                     # judge every pair once with jev (cached in --scores)
    python3 -m tests.relevance_check --scores s.json     # re-run the analysis from saved scores

Reports, per variant: share of relevant facts delivered, questions with every relevant fact delivered,
precision of what was delivered, and generic questions that got nothing.
"""

import argparse
import json
from pathlib import Path
import shutil
import tempfile

from context_kernel import planner
from context_kernel.common import timestamp, timestamp_offset
from context_kernel.compiler import Compiler
from context_kernel.judge import JevCommand
from context_kernel.planner import jev_candidate
from context_kernel.store import Store

ROOT = Path(__file__).resolve().parent.parent
TUNE, HELD_OUT = ("saas", "freelance"), ("platform", "personal")
NOW = "2026-10-06T12:00:00+00:00"


def lines(workspace):
    return {jev_candidate(f["entity"], f["predicate"], [f["value"]]): f["id"] for f in workspace["facts"]}


def score_all(data, jev_command):
    judge = JevCommand(jev_command, timeout=60)
    scores = {}
    for workspace in data["workspaces"]:
        candidates = list(lines(workspace))
        by_line = lines(workspace)
        for query in workspace["queries"]:
            found, _ = judge.rank(query["text"], candidates, no_cache=True)
            scores[query["id"]] = {by_line[c]: round(found.get(i, 0.0), 4) for i, c in enumerate(candidates)}
            print(query["id"], flush=True)
    return {"judge": judge.describe(), "plain": scores}


class Replay:
    """A judge that answers from saved scores: the same numbers jev gave, without the wait."""
    question = JevCommand.RELEVANCE
    timeout, critical, supporting, band = 10, 0.6, 0.5, 0.35

    def __init__(self, scores, by_line, query_id, max_pairs):
        self.scores, self.by_line, self.query_id, self.max_pairs = scores, by_line, query_id, max_pairs

    def describe(self):
        return {"url": "http://localhost/replay", "model": "replay", "local": True}

    def require_local(self, allow_remote=False):
        return None

    def rank(self, query, candidates, no_cache=False, timeout=None, question=None):
        return {i: self.scores[self.query_id].get(self.by_line.get(c), 0.0) for i, c in enumerate(candidates)}, {"latency_ms": 0}


def old_select(scored, lexical, critical, supporting, band):
    """v0.7: score order; the band kept only with a keyword match; critical at 0.6."""
    return [(0, s, e, p, s >= critical) for s, e, p in scored if s >= supporting or (band <= s and (e, p) in lexical)]


def evaluate(data, scores, variant, workspaces, max_pairs, use_cues, rule, strategy="jev"):
    cues = json.loads((ROOT / "fixtures" / "relevance_cues.json").read_text(encoding="utf-8"))["cues"]
    rows = []
    original = planner.select
    planner.select = old_select if rule == "v0.7" else original
    try:
        for workspace in [w for w in data["workspaces"] if w["id"] in workspaces]:
            folder = Path(tempfile.mkdtemp(prefix="ck-relevance-"))
            clock = {"now": NOW}
            store = Store(folder / "memory.sqlite", scope="rel", clock=lambda: timestamp(clock["now"]), create=True)
            try:
                ids = {}
                for fact in sorted(workspace["facts"], key=lambda f: -f["age_days"]):
                    clock["now"] = timestamp_offset(NOW, -fact["age_days"] * 86400)
                    row = store.remember(fact["entity"], fact["predicate"], fact["value"], fact["value"], kind="project",
                                         cues=(cues.get(fact["id"]) if use_cues else None) or None)
                    ids[row["id"]] = fact["id"]
                    with store.db:  # a category the agent's capture would have recorded
                        store.db.execute("UPDATE statements SET category=? WHERE id=?", (fact["category"], row["id"]))
                clock["now"] = NOW
                if not use_cues:
                    # No cues at all, not even the kind defaults: what v0.7 matched on.
                    import context_kernel.compiler as compiler_module
                    saved, compiler_module.concept_cues = compiler_module.concept_cues, lambda predicate: []
                try:
                    for query in workspace["queries"]:
                        judge = Replay(scores, lines(workspace), query["id"], max_pairs)
                        projection = Compiler(store, jev=judge).project(query["text"], strategy=strategy)
                        delivered = [ids[i] for i in projection.trace["selected"] if i in ids]
                        relevant = set(query["relevant"])
                        rows.append({"query": query["id"], "style": query["style"], "relevant": sorted(relevant),
                                     "delivered": delivered, "hit": len(relevant & set(delivered)),
                                     "judged": projection.trace["usage"].get("judged_pairs")})
                finally:
                    if not use_cues:
                        compiler_module.concept_cues = saved
            finally:
                store.close()
                shutil.rmtree(folder)
    finally:
        planner.select = original
    return summarize(rows, variant)


def summarize(rows, variant):
    needed = [r for r in rows if r["relevant"]]
    generic = [r for r in rows if not r["relevant"]]
    delivered = sum(len(r["delivered"]) for r in needed)
    out = {"variant": variant,
           "relevant_delivered": f"{sum(r['hit'] for r in needed)}/{sum(len(r['relevant']) for r in needed)}",
           "questions_fully_served": f"{sum(r['hit'] == len(r['relevant']) for r in needed)}/{len(needed)}",
           "precision": round(sum(r["hit"] for r in needed) / delivered, 3) if delivered else None,
           "generic_quiet": f"{sum(not r['delivered'] for r in generic)}/{len(generic)}",
           "mean_delivered": round(delivered / len(needed), 1) if needed else 0}
    for style in ("paraphrase", "direct"):
        mine = [r for r in needed if r["style"] == style]
        out[style] = f"{sum(r['hit'] for r in mine)}/{sum(len(r['relevant']) for r in mine)}"
    return out


def auc(data, scores, workspaces, style=None):
    """How well the judge's score alone separates relevant from other facts: the chance that a relevant fact
    of a question outscores an irrelevant one of the same question."""
    wins = total = 0
    for workspace in [w for w in data["workspaces"] if w["id"] in workspaces]:
        for query in workspace["queries"]:
            if not query["relevant"] or (style and query["style"] != style) or query["id"] not in scores:
                continue
            mine = scores[query["id"]]
            good = [mine[f] for f in query["relevant"] if f in mine]
            bad = [p for f, p in mine.items() if f not in query["relevant"]]
            for g in good:
                for b in bad:
                    wins += 1 if g > b else 0.5 if g == b else 0
                    total += 1
    return round(wins / total, 3) if total else None


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--jev-command", default=shutil.which("jev") or "jev")
    parser.add_argument("--scores", help="Saved scores (written here after judging when the file does not exist)")
    parser.add_argument("--split", choices=("tune", "held_out", "all"), default="all")
    parser.add_argument("--min-delivered", type=float, help="Exit 1 when the shipped rule (v0.8, cues, 12 pairs) delivers a "
                                                             "smaller share of the relevant facts than this, on any split")
    args = parser.parse_args()
    data = json.loads((ROOT / "fixtures" / "relevance.json").read_text(encoding="utf-8"))
    if args.scores and Path(args.scores).exists():
        saved = json.loads(Path(args.scores).read_text(encoding="utf-8"))
    else:
        saved = score_all(data, args.jev_command)
        if args.scores:
            Path(args.scores).write_text(json.dumps(saved), encoding="utf-8")
    scores = saved["plain"]
    splits = {"tune": TUNE, "held_out": HELD_OUT, "all": TUNE + HELD_OUT}
    chosen = [args.split] if args.split != "all" else ["tune", "held_out"]
    report = {"judge": saved.get("judge"), "results": {}, "auc": {}}
    failed = False
    for split in chosen:
        # A pair costs about 0.46 s with these facts and questions (0.19 s with the short lines of growth_check),
        # so the 8 s hook judges about 12: v0.7's 48 ran out of time and fell back to keyword rules.
        report["results"][split] = [
            evaluate(data, scores, "v0.7 as it ran: jev out of time, keyword rules", splits[split], 48, False, "v0.7", "rules"),
            evaluate(data, scores, "v0.7 rule with time for 48 pairs (not reachable)", splits[split], 48, False, "v0.7"),
            evaluate(data, scores, "v0.8 rule, no cues, 12 pairs", splits[split], 12, False, "v0.8"),
            evaluate(data, scores, "v0.8: cues, 12 pairs (what the hook affords)", splits[split], 12, True, "v0.8"),
            evaluate(data, scores, "v0.8: cues, 24 pairs (a faster judge)", splits[split], 24, True, "v0.8"),
        ]
        for mode in [m for m in ("plain", "cues") if m in saved]:
            report["auc"].setdefault(split, {})[mode] = {style or "all": auc(data, saved[mode], splits[split], style)
                                                         for style in (None, "paraphrase", "direct")}
        shipped = report["results"][split][3]["relevant_delivered"].split("/")
        if args.min_delivered is not None and int(shipped[0]) / int(shipped[1]) < args.min_delivered:
            failed = True
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
