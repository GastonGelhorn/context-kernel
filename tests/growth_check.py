"""Opt-in measurement: does an old, important fact still reach a paraphrased question when hundreds of
newer facts compete for the judge's 48 pairs?

Seeds a throwaway database with a few old constraints, then N newer unrelated facts, and asks
questions that name none of the old facts' words. Uses the real `jev` on its configured backend
(local by default). Reports, per N, whether each old fact was delivered, how many pairs were judged,
and the projection's latency. Nothing is sent anywhere jev is not already configured to send.
"""

import argparse
import json
import random
import shutil
import statistics
import tempfile
from pathlib import Path

from shelflife_context.common import timestamp, timestamp_offset
from shelflife_context.compiler import Compiler
from shelflife_context.judge import JevCommand
from shelflife_context.store import Store


OLD = [  # (entity, predicate, value, kind, paraphrased question that shares none of its words)
    ("checkout", "delivery_deadline", "three weeks from 2026-10-06", "project",
     "Is there room to squeeze the payments refactor in before we ship?"),
    ("aurora", "constraint", "customer data must stay in EU regions", "project",
     "Can we spin up the new replicas in us-east-1 to save money?"),
    ("user", "allergy", "cannot eat peanuts", "person",
     "What snack should I bring to the team offsite for myself?"),
]
SUBJECTS = ["billing", "search", "mobile", "auth", "reports", "inbox", "onboarding", "analytics", "exports", "chat",
            "calendar", "invoices", "uploads", "notifications", "admin", "pricing", "catalog", "reviews", "maps", "feeds"]
PREDICATES = [("owner", ["Ana", "Luis", "Marta", "Kenji", "Priya", "Tom"]),
              ("status", ["in progress", "blocked on design", "in QA", "shipped", "paused"]),
              ("framework", ["React", "Vue", "SwiftUI", "Jetpack Compose", "Django", "Rails"]),
              ("ci_provider", ["GitHub Actions", "CircleCI", "Buildkite"]),
              ("database", ["Postgres", "MySQL", "DynamoDB", "SQLite"]),
              ("review_day", ["Monday", "Wednesday", "Friday"]),
              ("slack_channel", ["#team-a", "#team-b", "#ops", "#launch"]),
              ("on_call", ["Ana", "Luis", "Kenji"]),
              ("coverage_target", ["70%", "80%", "90%"]),
              ("feature_flag", ["enabled for staff", "enabled for 10%", "off"])]


def seed(store, n, rng, clock):
    for entity, predicate, value, kind, _ in OLD:
        store.remember(entity, predicate, value, value, kind=kind)
    clock["now"] = timestamp_offset(clock["now"], 30 * 86400)  # a month later, the inventory grows
    pairs = [(s, p) for s in SUBJECTS for p, _ in PREDICATES]
    rng.shuffle(pairs)
    for subject, predicate in pairs[:n]:
        clock["now"] = timestamp_offset(clock["now"], 60)
        value = rng.choice(dict(PREDICATES)[predicate])
        store.remember(subject, predicate, value, value, kind="project")


def measure(jev_command, sizes, repetitions, max_pairs):
    rows = []
    for n in sizes:
        for repetition in range(repetitions):
            folder = Path(tempfile.mkdtemp(prefix="ck-growth-"))
            clock = {"now": "2026-09-01T09:00:00+00:00"}
            store = Store(folder / "memory.sqlite", clock=lambda: timestamp(clock["now"]), create=True)
            try:
                seed(store, n, random.Random(n * 100 + repetition), clock)
                judge = JevCommand(jev_command, timeout=60, max_pairs=max_pairs)
                compiler = Compiler(store, jev=judge)
                for entity, predicate, _, _, question in OLD:
                    target = next(r["id"] for r in store.records() if (r["entity_key"], r["predicate"]) == (entity, predicate))
                    projection = compiler.project(question, strategy="jev")
                    usage = projection.trace["usage"]
                    rows.append({"facts": n + len(OLD), "repetition": repetition + 1, "fact": f"{entity}.{predicate}",
                                 "delivered": target in projection.trace["selected"],
                                 "judged_pairs": usage.get("judged_pairs"), "unjudged_pairs": usage.get("unjudged_pairs"),
                                 "score": (usage.get("scores") or {}).get(f"{entity}.{predicate}"),
                                 "capped": "jev_inventory_capped" in projection.trace["warnings"],
                                 "latency_ms": projection.trace["latency_ms"]})
            finally:
                store.close()
                shutil.rmtree(folder)
    return rows


def summarize(rows):
    out = {}
    for n in sorted({r["facts"] for r in rows}):
        mine = [r for r in rows if r["facts"] == n]
        out[n] = {"delivered": f"{sum(r['delivered'] for r in mine)}/{len(mine)}",
                  "not_judged": sum(r["score"] is None for r in mine),
                  "median_latency_ms": round(statistics.median(r["latency_ms"] for r in mine)),
                  "max_latency_ms": round(max(r["latency_ms"] for r in mine))}
    return out


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--jev-command", default=shutil.which("jev") or "jev")
    parser.add_argument("--sizes", type=int, nargs="*", default=[10, 45, 100, 200])
    parser.add_argument("--repetitions", type=int, default=2)
    parser.add_argument("--max-pairs", type=int, default=48)
    args = parser.parse_args()
    rows = measure(args.jev_command, args.sizes, args.repetitions, args.max_pairs)
    print(json.dumps({"summary": summarize(rows), "rows": rows}, indent=2))


if __name__ == "__main__":
    raise SystemExit(main())
