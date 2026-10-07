"""Reproducible benchmark: the multi-session scenarios in fixtures/bench, replayed through the kernel's own hooks.

Each scenario starts from an empty memory (plus `--filler N` unrelated facts that compete with its own) and
replays its sessions turn by turn: the prompt hook, what a competent agent would do (the captures and tool
calls the scenario lists, with the cues in fixtures/bench/cues.json), and the Stop hook with the agent's
reply. Then it checks the scenario's expectations: what each packet delivered or flagged for review, and what
memory holds after each turn. The agent's part is fixed, so this measures the kernel and its judge, not an
agent's extraction; nothing calls a generative model and no account is needed.

    python3 -m tests.bench                         # the real jev, as configured (local by default)
    python3 -m tests.bench --filler 0 100 500 1000 # the same scenarios among more and more unrelated facts
    python3 -m tests.bench --judge fake            # a smoke run of the runner itself, no model needed

Prompt hooks run with the production budget (8 s; `--budget none` lifts it to see quality without the time
limit). The report gives pass rates per kind of check and per scenario tag, and hook latencies.
"""

import argparse
from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import shutil
import statistics
import subprocess
import tempfile
import time

from shelflife_context.adapters import hook_response, packet_of, session_start_response, stop_response
from shelflife_context.budget import Budget
from shelflife_context.common import canonical, digest, timestamp, timestamp_offset
from shelflife_context.compiler import Compiler
from shelflife_context.judge import JevCommand, JudgeError
from shelflife_context.language import fold
from shelflife_context.mcp import Server
from shelflife_context.repository import learn
from shelflife_context.store import Store


HOST = [4242, "Mon Oct  5 12:00:00 2026"]
BENCH = Path(__file__).resolve().parent.parent / "fixtures" / "bench"
KINDS = ("delivered", "not_delivered", "review_true", "review_false", "current", "absent", "held", "standing")


def scenarios(selected=()):
    paths = sorted(p for p in BENCH.glob("[0-9][0-9]-*.json"))
    return [p for p in paths if not selected or any(p.name.startswith(s) for s in selected)]


def same(stored, expected):
    if isinstance(stored, str) and isinstance(expected, str):
        return fold(stored).strip(" .") == fold(expected).strip(" .")
    return canonical(stored) == canonical(expected)


def key_of(row):
    return f"{row['entity_key']}.{row['predicate']}"


def id_of(store, key):
    rows = [r for r in store.records(quarantined=True) if key_of(r) == key]
    return rows[-1]["id"] if rows else None


def recommendation_of(store, key):
    rows = [r for r in store.records(history=True) if key_of(r) == key and r["effective_state"] == "active"]
    return rows[-1]["id"] if rows else None


def git(root, *args, date=None):
    env = {"GIT_AUTHOR_NAME": "Bench", "GIT_AUTHOR_EMAIL": "bench@example.com", "GIT_COMMITTER_NAME": "Bench",
           "GIT_COMMITTER_EMAIL": "bench@example.com", "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1",
           "PATH": "/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin"}
    if date:
        env.update(GIT_AUTHOR_DATE=date, GIT_COMMITTER_DATE=date)
    subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True, text=True, env=env)


class FakeBenchJudge:
    """Fixed answers, no model: checks that the runner and the scenarios still fit the kernel."""
    question = JevCommand.RELEVANCE
    timeout, critical, supporting, band, max_pairs = 10, 0.6, 0.5, 0.35, 48
    command = "fake"

    def describe(self):
        return {"url": "http://localhost:0/fake", "model": "fake", "local": True}

    def require_local(self, allow_remote=False):
        return None

    def rank(self, query, candidates, no_cache=False, timeout=None, question=None):
        words = set(fold(query).split())
        return {i: (0.7 if words & set(fold(c).split()) else 0.2) for i, c in enumerate(candidates)}, {"latency_ms": 1}

    def ask(self, state, questions, no_cache=True, timeout=None):
        out = {}
        for qid, spec in questions.items():
            if spec[0] == "choice":
                out[qid] = {o: (1.0 if n == 0 else 0.0) for n, o in enumerate(spec[2])}
                if qid == "count":
                    out[qid] = {"none": 0.02, "one": 0.9, "two": 0.05, "several": 0.03}
            else:
                out[qid] = {"affirmed": 0.9, "recommends": 0.9, "asked": 0.9}.get(qid, 0.1)
        return out, {"latency_ms": 1}


FAILED = {"failed": True}


def call_key(kind, payload):
    return f"{kind}:{digest(payload)[:32]}"


class RecordingJudge:
    """The real judge, with every answer kept so a later run can replay it without a model (`--record`)."""

    def __init__(self, judge):
        self.judge, self.calls = judge, {}
        self.question, self.timeout, self.command = judge.question, judge.timeout, judge.command
        self.critical, self.supporting, self.band, self.max_pairs = judge.critical, judge.supporting, judge.band, judge.max_pairs

    def describe(self):
        return self.judge.describe()

    def require_local(self, allow_remote=False):
        return self.judge.require_local(allow_remote)

    def rank(self, query, candidates, no_cache=False, timeout=None, question=None):
        key = call_key("rank", [query, candidates, question])
        try:
            scores, usage = self.judge.rank(query, candidates, no_cache=no_cache, timeout=timeout, question=question)
        except JudgeError:
            self.calls[key] = FAILED  # a judge that did not answer is part of the run too
            raise
        self.calls[key] = {str(i): p for i, p in scores.items()}
        return scores, usage

    def ask(self, state, questions, no_cache=True, timeout=None):
        key = call_key("ask", [state, {k: list(v) for k, v in questions.items()}])
        try:
            answers, usage = self.judge.ask(state, questions, no_cache=no_cache, timeout=timeout)
        except JudgeError:
            self.calls[key] = FAILED
            raise
        self.calls[key] = answers
        return answers, usage


class ReplayJudge:
    """Answers recorded by `--record`, so CI measures the kernel's logic on a real model's answers. A request
    the recording does not hold (the kernel now asks something else) counts as the judge not answering."""
    timeout, critical, supporting, band, max_pairs, command = 10, 0.6, 0.5, 0.35, 48, "replay"

    def __init__(self, recording):
        self.recorded, self.missing = recording["calls"], 0
        self.description = recording["judge"]
        self.question = JevCommand.RELEVANCE

    def describe(self):
        return self.description

    def require_local(self, allow_remote=False):
        return None

    def lookup(self, key):
        found = self.recorded.get(key)
        if found is None:
            self.missing += 1
            raise JudgeError("not in the recording")
        if found == FAILED:
            raise JudgeError("the judge did not answer when this was recorded")
        return found

    def rank(self, query, candidates, no_cache=False, timeout=None, question=None):
        found = self.lookup(call_key("rank", [query, candidates, question]))
        return {int(i): p for i, p in found.items()}, {"latency_ms": 0}

    def ask(self, state, questions, no_cache=True, timeout=None):
        return self.lookup(call_key("ask", [state, {k: list(v) for k, v in questions.items()}])), {"latency_ms": 0}


def tool(server, name, arguments):
    response = server.dispatch({"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": name, "arguments": arguments}})
    return response["result"]["structuredContent"], response["result"]["isError"]


def run(path, judge, filler, budget, cues, filler_facts):
    spec = json.loads(path.read_text(encoding="utf-8"))
    folder = Path(tempfile.mkdtemp(prefix="ck-bench-"))
    clock = {"now": "2026-09-01T09:00:00+00:00"}
    store = Store(folder / "memory.sqlite", scope="bench", clock=lambda: timestamp(clock["now"]), create=True)
    root = folder / "benchrepo"
    root.mkdir()
    checks, latency, nudges, bytes_sent = [], {"prompt": [], "stop": []}, 0, []
    try:
        if any(s.get("repository_commits") for s in spec["sessions"]):
            git(root, "init", "-q", "-b", "main")
        for fact in filler_facts[:filler]:
            clock["now"] = timestamp_offset(clock["now"], 30)
            store.remember(fact["entity"], fact["predicate"], fact["value"], fact["value"], kind="project", cues=fact["cues"] or None)
        compiler = Compiler(store, jev=judge)
        server = Server(store, compiler, strategy="jev", judge=judge, parent=HOST)
        server.dispatch({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
            "protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {}}})
        server.dispatch({"jsonrpc": "2.0", "method": "notifications/initialized"})
        for si, session in enumerate(spec["sessions"]):
            clock["now"] = timestamp_offset(clock["now"], max(3600, 86400 * session.get("days_later", 0)))
            for commit in session.get("repository_commits") or []:
                for name, content in commit.get("files", {}).items():
                    target = root / name
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_text(content, encoding="utf-8")
                git(root, "add", "-A")
                git(root, "commit", "-q", "--allow-empty", "-m", commit["message"], date=clock["now"])
            sid = f"{spec['id']}-s{si}"
            store.register_session(sid, "claude", [HOST])
            session_start_response({"cwd": str(root), "hook_event_name": "SessionStart", "session_id": sid}, root, store,
                                   learn=lambda cwd, s: learn(store, judge, str(root), Budget(90), s))
            for ti, turn in enumerate(session["turns"]):
                clock["now"] = timestamp_offset(clock["now"], 60)
                deadline = Budget(8) if budget == "real" else None
                compiler.deadline = deadline
                event = {"cwd": str(root), "hook_event_name": "UserPromptSubmit", "prompt": turn["user"], "session_id": sid,
                         "prompt_id": f"{si}-{ti}"}
                started = time.perf_counter()
                response, _ = hook_response(event, root, store, compiler, strategy="jev", judge=judge, deadline=deadline)
                latency["prompt"].append((time.perf_counter() - started) * 1000)
                # The hook records this process's real ancestry; the replayed MCP server stands for the host's,
                # so the session is bound to it as a host would bind it (the unit tests do the same).
                store.register_session(sid, "claude", [HOST])
                context = (response.get("hookSpecificOutput") or {}).get("additionalContext", "")
                bytes_sent.append(len(context.encode()))
                packet = packet_of(context) or {}
                delivered = {f"{c['entity']}.{c['predicate']}" for c in packet.get("claims", [])}
                token = (packet.get("turn") or {}).get("token")
                where = f"s{si}t{ti}"

                def check(kind, ok, key):
                    checks.append({"kind": kind, "ok": bool(ok), "key": key, "at": where})

                expect = turn.get("expect_packet") or {}
                for key in expect.get("delivered", []):
                    check("delivered", key in delivered, key)
                for key in expect.get("not_delivered", []):
                    check("not_delivered", key not in delivered, key)
                if "review" in expect:
                    check("review_true" if expect["review"] else "review_false", bool(packet.get("review")) == expect["review"], "review")
                facts = []
                for ci, capture in enumerate(turn.get("agent_captures") or []):
                    fact = {k: capture[k] for k in ("entity", "predicate", "value")}
                    if capture.get("quote"):
                        fact["quote"] = capture["quote"]
                    written = cues.get(f"{spec['id']}/{si}/{ti}/{ci}")
                    if written:
                        fact["cues"] = [c[:40] for c in written[:8]]
                    if capture.get("replaces"):
                        target = id_of(store, capture["replaces"])
                        if target:
                            fact["replaces"] = target
                    facts.append(fact)
                if facts and token:
                    for start in range(0, len(facts), 4):
                        tool(server, "memory_capture", {"token": token, "facts": facts[start:start + 4]})
                for call in turn.get("agent_calls") or []:
                    if not token:
                        break
                    if call["tool"] == "memory_undo":
                        tool(server, "memory_undo", {"token": token})
                    elif call["tool"] == "memory_reaffirm":
                        target = recommendation_of(store, call["key"]) or id_of(store, call["key"])
                        if target:
                            tool(server, "memory_reaffirm", {"token": token, "id": target})
                    else:
                        target = id_of(store, call["key"])
                        if target:
                            arguments = {"token": token, "id": target} | ({"standing": call["standing"]} if "standing" in call else {})
                            tool(server, call["tool"], arguments)
                clock["now"] = timestamp_offset(clock["now"], 20)
                started = time.perf_counter()
                stop = stop_response({"cwd": str(root), "hook_event_name": "Stop", "session_id": sid, "prompt_id": f"{si}-{ti}",
                                      "last_assistant_message": turn.get("agent_reply", "")}, root, store, judge,
                                     Budget(15) if budget == "real" else None)
                latency["stop"].append((time.perf_counter() - started) * 1000)
                nudges += stop.get("decision") == "block"
                after = turn.get("expect_after") or {}
                current = {}
                for row in store.records():
                    current[key_of(row)] = row["value"]
                held = {key_of(r) for r in store.records(quarantined=True) if r["trust"] == "quarantined"}
                standing = {f"{e}.{p}" for e, p in store.standing()}
                for key, value in after.get("current", {}).items():
                    check("current", key in current and same(current[key], value), key)
                for key in after.get("absent", []):
                    check("absent", key not in current, key)
                for key in after.get("held", []):
                    check("held", key in held, key)
                for key in after.get("standing", []):
                    check("standing", key in standing, key)
    finally:
        store.close()
        shutil.rmtree(folder, ignore_errors=True)
    return {"id": spec["id"], "tags": spec.get("tags", []), "checks": checks, "latency": latency, "nudges": nudges,
            "bytes": bytes_sent}


def rate(items):
    return {"passed": sum(c["ok"] for c in items), "total": len(items),
            "rate": round(sum(c["ok"] for c in items) / len(items), 3) if items else None}


def percentile(values, q):
    ordered = sorted(values)
    return round(ordered[min(len(ordered) - 1, int(q * len(ordered)))]) if ordered else None


def summarize(results):
    checks = [c for r in results for c in r["checks"]]
    tags = sorted({t for r in results for t in r["tags"]})
    prompt = [v for r in results for v in r["latency"]["prompt"]]
    stop = [v for r in results for v in r["latency"]["stop"]]
    return {"checks": rate(checks),
            "by_kind": {k: rate([c for c in checks if c["kind"] == k]) for k in KINDS},
            "by_tag": {t: rate([c for r in results if t in r["tags"] for c in r["checks"]]) for t in tags},
            "prompt_ms": {"median": round(statistics.median(prompt)) if prompt else None, "p90": percentile(prompt, 0.9),
                          "max": round(max(prompt)) if prompt else None},
            "stop_ms": {"median": round(statistics.median(stop)) if stop else None, "p90": percentile(stop, 0.9)},
            "packet_bytes_max": max((b for r in results for b in r["bytes"]), default=0),
            "nudges": sum(r["nudges"] for r in results),
            "failed": [{"scenario": r["id"], **c} for r in results for c in r["checks"] if not c["ok"]]}


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--judge", choices=("jev", "fake"), default="jev")
    parser.add_argument("--jev-command", default=shutil.which("jev") or "jev")
    parser.add_argument("--filler", type=int, nargs="*", default=[0])
    parser.add_argument("--scenarios", nargs="*", default=[], help="Scenario number prefixes, e.g. 01 17")
    parser.add_argument("--budget", choices=("real", "none"), default="real")
    parser.add_argument("--json", help="Write the full report here")
    parser.add_argument("--record", help="Keep every judge answer in this file (one filler size)")
    parser.add_argument("--replay", help="Answer from a recording instead of jev: deterministic, no model needed")
    parser.add_argument("--min-rate", type=float, help="Exit 1 when the share of checks passed is below this")
    args = parser.parse_args()
    cues = json.loads((BENCH / "cues.json").read_text(encoding="utf-8"))["cues"]
    filler_facts = json.loads((BENCH / "filler.json").read_text(encoding="utf-8"))["facts"]
    recording = json.loads(Path(args.replay).read_text(encoding="utf-8")) if args.replay else None
    report, lowest = {}, 1.0
    for size in args.filler:
        if recording:
            judge = ReplayJudge(recording)
        elif args.judge == "fake":
            judge = FakeBenchJudge()
        else:
            judge = JevCommand(args.jev_command, timeout=10)
            judge = RecordingJudge(judge) if args.record else judge
        results = []
        for path in scenarios(args.scenarios):
            with redirect_stdout(io.StringIO()):
                # A recording is made and replayed without the time limit: what gets judged must not depend on
                # how fast the machine was.
                results.append(run(path, judge, size, "none" if recording or args.record else args.budget, cues, filler_facts))
            print(f"filler={size} {path.stem}: " + "".join("." if c["ok"] else "F" for c in results[-1]["checks"]), flush=True)
        summary = summarize(results) | ({"replay_missing": judge.missing} if recording else {})
        report[str(size)] = {"summary": summary, "scenarios": results}
        lowest = min(lowest, summary["checks"]["rate"] or 0.0)
        print(json.dumps({"filler": size, **{k: v for k, v in summary.items() if k != "failed"}}, indent=2))
        if args.record and isinstance(judge, RecordingJudge):
            Path(args.record).write_text(json.dumps({"judge": judge.describe(), "filler": size, "calls": judge.calls},
                                                    ensure_ascii=False, sort_keys=True), encoding="utf-8")
    if args.json:
        Path(args.json).write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
    if args.min_rate is not None and lowest < args.min_rate:
        print(f"pass rate {lowest} is below {args.min_rate}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
