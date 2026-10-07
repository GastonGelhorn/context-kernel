"""Owner CLI; agents get a separate read/propose-only MCP interface."""

import argparse
import json
import re
import sqlite3
import statistics
import sys

import shutil
from pathlib import Path

from .adapters import HookBlock, block, configuration, envelope, hook_response, session_start_response, stop_response
from .budget import Budget
from .capture import policy as capture_policy
from .common import KernelError, canonical, quantity
from .compiler import Compiler
from .judge import JevCommand
from .planner import NeedPlan
from .protocol import parse_json, read_event
from .store import Store


class DeliveryError(KernelError):
    """The hook envelope was written, but its trace update failed."""


def parser():
    root = argparse.ArgumentParser(prog="memory", description="Local, scoped, correctable context.")
    root.add_argument("--db", default=".context-kernel/memory.sqlite", help="SQLite path (default: workspace-local)")
    root.add_argument("--scope", default="personal", help="Owner-selected scope; not supplied by an agent tool")
    root.add_argument("--pretty", action="store_true", help="Print readable JSON for owner commands")
    commands = root.add_subparsers(dest="command", required=True)
    for name in ("init", "status", "proposals", "inventory", "metrics"):
        commands.add_parser(name)
    undo = commands.add_parser("undo", help="Remove a captured statement and restore the version it replaced")
    undo.add_argument("id")
    policy = commands.add_parser("policy", help="Show or change which kinds of facts are captured automatically")
    policy.add_argument("--enable", nargs="*", default=[])
    policy.add_argument("--disable", nargs="*", default=[])
    policy.add_argument("--allow-remote-judge", choices=("yes", "no"))
    policy.add_argument("--auto-capture", choices=("on", "off"))
    policy.add_argument("--repository", choices=("on", "off"), help="Read decision records of the session's repository")
    policy.add_argument("--repository-commits", choices=("on", "off"), help="Also read decisions from commit messages (off by default)")
    policy.add_argument("--keep-alive", help="How long the local judge's model stays loaded after a turn, e.g. 30m; off for Ollama's default")
    policy.add_argument("--threshold", action="append", default=[], help="name=value, e.g. affirmed=0.72 after jev tune")
    calibrate = commands.add_parser("calibrate", help="Check the judge against the thresholds, or export labelled fixtures for `jev tune`")
    calibrate.add_argument("kind", nargs="?", choices=("affirmed", "facts_present", "forget_asked", "standing_on", "standing_off",
                                                       "decision_commit", "decision_replaces"))
    calibrate.add_argument("--check", action="store_true",
                           help="Run the canary with this scope's thresholds and record whether this judge may save facts")
    calibrate.add_argument("--fixtures", default=str(Path(__file__).resolve().parent.parent / "fixtures" / "calibration.jsonl"))
    calibrate.add_argument("--questions", action="store_true", help="Print the candidate questions instead of the rows")
    calibrate.add_argument("--score", action="store_true", help="Run the kernel's own question over the fixtures with jev and sweep thresholds")
    calibrate.add_argument("--jev-command", default="jev")
    calibrate.add_argument("--jev-timeout", type=float, default=60, help="A ranked fixture set is one long request")
    serve = commands.add_parser("serve", help="Stdio MCP server with a fixed scope")
    warm = commands.add_parser("warm", help="Load the local judge, check its calibration if needed, read the repository (background)")
    warm.add_argument("--workspace", help="A directory inside the session's git repository")
    warm.add_argument("--session", help="Session that receives the one-line note of what was learned")
    warm.add_argument("--learn", action="store_true", help="Also read the repository's decision records")
    learn = commands.add_parser("learn", help="Read decision records (and, if enabled, decision commits) of a repository now")
    learn.add_argument("--workspace", required=True, help="A directory inside the git repository")
    learn.add_argument("--session", help="Session that receives the one-line note of what was learned")
    learn.add_argument("--if-changed", action="store_true", help="Return at once when the repository did not change")
    standing = commands.add_parser("standing", help="Keep a fact in mind in every session, or stop")
    standing.add_argument("id")
    standing.add_argument("state", choices=("on", "off"))
    traces = commands.add_parser("traces")
    traces.add_argument("--limit", type=int, default=20, help="Latest scoped projection metadata, 1-100")
    listing = commands.add_parser("list")
    listing.add_argument("--history", action="store_true")
    listing.add_argument("--as-of")
    remember = commands.add_parser("remember")
    remember.add_argument("entity")
    remember.add_argument("predicate")
    remember.add_argument("value", help="JSON value; quote strings as JSON")
    remember.add_argument("--evidence", required=True)
    remember.add_argument("--kind", choices=("person", "project", "object", "organization"), default="person")
    remember.add_argument("--label")
    remember.add_argument("--assertion-kind", choices=("user_statement", "observed", "hypothesis", "inference"), default="user_statement")
    remember.add_argument("--cues", help="Comma-separated words a later question would use when this fact matters")
    correct = commands.add_parser("correct")
    correct.add_argument("id")
    correct.add_argument("value", help="JSON value")
    correct.add_argument("--evidence", required=True)
    for command in (remember, correct):
        command.add_argument("--valid-from")
        command.add_argument("--valid-until")
        command.add_argument("--unit", help="Explicit quantity unit; absent means unknown")
        command.add_argument("--currency", help="Explicit three-letter uppercase currency label")
        command.add_argument("--period", help="Explicit quantity period, for example year or month")
    for name in ("inspect", "revoke", "forget", "approve", "reject", "why", "dependents", "reaffirm", "confirm"):
        item = commands.add_parser(name)
        item.add_argument("id")
        if name in {"inspect", "dependents"}:
            item.add_argument("--as-of")
    stale = commands.add_parser("stale", help="Current statements whose declared assumptions changed")
    stale.add_argument("--as-of")
    depend = commands.add_parser("depend", help="Declare that a decision rests on another statement")
    depend.add_argument("id")
    depend.add_argument("assumption_id")
    alias = commands.add_parser("alias")
    alias.add_argument("entity")
    alias.add_argument("alias")
    relate = commands.add_parser("relate")
    relate.add_argument("child")
    relate.add_argument("parent")
    resolve = commands.add_parser("resolve")
    resolve.add_argument("reference")
    transition = commands.add_parser("transition")
    transition.add_argument("entity")
    transition.add_argument("event", choices=("ordered", "not_arrived", "arrived", "returned"))
    transition.add_argument("--evidence", required=True)
    proposal = commands.add_parser("propose")
    proposal.add_argument("operation", choices=("remember", "correct", "transition"))
    proposal.add_argument("payload", help="JSON object")
    project = commands.add_parser("project")
    project.add_argument("query")
    project.add_argument("--plan-id")
    project.add_argument("--as-of")
    project.add_argument("--budget", type=int, default=2048, help="UTF-8 bytes, not tokens")
    hook = commands.add_parser("hook")
    hook.add_argument("--client", choices=("codex", "claude"), required=True)
    hook.add_argument("--workspace", required=True)
    hook.add_argument("--event", choices=("prompt", "stop", "session-start"), default="prompt")
    hook.add_argument("--proposals", action="store_true", help="Opt in to bounded command proposals, never automatic approval")
    hook.add_argument("--fail-closed", action="store_true", help="Block the prompt when memory is unavailable (default: proceed without memory)")
    for item in (project, hook, serve, learn, warm):
        item.add_argument("--strategy", choices=("rules", "fts", "jev"), default="rules")
        item.add_argument("--jev-command", default="jev", help="jev executable; its own config decides local or hosted")
        # The background passes have no user waiting on them and may meet a cold model.
        item.add_argument("--jev-timeout", type=float, default=30 if item in (learn, warm) else 10)
        item.add_argument("--jev-critical", type=float, default=0.6, help="P at or above which a pair is critical")
        item.add_argument("--jev-supporting", type=float, default=0.5, help="P at or above which a pair is supporting")
        item.add_argument("--jev-band", type=float, default=0.35, help="Uncertain band floor: a pair between band and supporting is kept only when the question mentions it")
        item.add_argument("--jev-max-pairs", type=int, default=48, help="Pairs judged per call; beyond it, mentioned and most recent pairs first")
        item.add_argument("--jev-question", help="Override the relevance question (name `candidate` and `query`)")
    adapter = commands.add_parser("adapter")
    adapter.add_argument("client", choices=("codex", "claude", "antigravity"))
    adapter.add_argument("--workspace", required=True)
    adapter.add_argument("--mode", choices=("hook", "mcp"), default="hook")
    adapter.add_argument("--proposals", action="store_true")
    adapter.add_argument("--strategy", choices=("rules", "fts", "jev"), default="rules")
    adapter.add_argument("--fail-closed", action="store_true")
    adapter.add_argument("--jev-command", default="jev", help="Resolved to an absolute path in the generated configuration")
    adapter.add_argument("--python")
    adapter.add_argument("--raw", action="store_true", help="Print only the configuration content for manual merging")
    return root


def execute(args, store):
    command = args.command
    value = None
    if command in {"remember", "correct"}:
        value = parse_json(args.value)
        if any(v is not None for v in (args.unit, args.currency, args.period)):
            value = quantity(value, args.unit, args.currency, args.period)
    if command in {"init", "status"}:
        return store.status()
    if command == "remember":
        from .capture import clean_cues
        return store.remember(args.entity, args.predicate, value, args.evidence,
                              kind=args.kind, label=args.label, assertion_kind=args.assertion_kind,
                              valid_from=args.valid_from, valid_until=args.valid_until,
                              cues=clean_cues([c for c in (args.cues or "").split(",") if c.strip()]) or None)
    if command == "correct":
        return store.correct(args.id, value, args.evidence, args.valid_from, args.valid_until)
    if command == "inspect":
        return store.inspect(args.id, args.as_of)
    if command == "list":
        return store.records(args.as_of, args.history)
    if command in {"revoke", "forget", "approve", "reject", "reaffirm", "confirm"}:
        return getattr(store, command)(args.id)
    if command == "undo":
        return store.undo_capture(args.id)
    if command == "inventory":
        from .mcp import Server
        return Server(store, parent=[]).inventory()
    if command == "calibrate":
        from .calibration import check, export, questions, score
        if args.check:
            judge = make_judge(args)
            if not judge:
                raise KernelError("The check needs jev on the PATH or --jev-command.")
            return check(store, judge)
        if not args.kind:
            raise KernelError("Name a fixture kind (affirmed, facts_present, …) or pass --check.")
        if args.score:
            judge = make_judge(args)
            if not judge:
                raise KernelError("Scoring needs jev on the PATH or --jev-command.")
            print(json.dumps(score(args.kind, args.fixtures, judge), ensure_ascii=False, indent=2 if args.pretty else None), flush=True)
            return None
        print("\n".join(questions(args.kind)) if args.questions else export(args.kind, args.fixtures), flush=True)
        return None
    if command == "metrics":
        return {"precision": {"last_30_days": store.regret_metrics(30), "all_time": store.regret_metrics()},
                "latency": latency_metrics(store), "judges": [{k: r[k] for k in ("model_key", "status", "checked_at")}
                                                               | {"model": r["detail"].get("model")} for r in store.judge_records()],
                "captures": store.capture_metrics(), "status": store.status()}
    if command == "policy":
        stored = store.policy() or {}
        if args.enable or args.disable:
            stored["categories"] = dict(stored.get("categories", {}), **{c: True for c in args.enable}, **{c: False for c in args.disable})
        if args.allow_remote_judge:
            stored["allow_remote_judge"] = args.allow_remote_judge == "yes"
        if args.auto_capture:
            stored["auto_capture"] = args.auto_capture == "on"
        if args.repository:
            stored["repository"] = args.repository == "on"
        if args.repository_commits:
            stored["repository_commits"] = args.repository_commits == "on"
        if args.keep_alive:
            if not re.fullmatch(r"(off|0|\d{1,4}[smh])", args.keep_alive):
                raise KernelError("keep-alive is a duration such as 30m or 2h, or off.")
            stored["keep_alive"] = args.keep_alive
        for item in args.threshold:
            name, _, value = item.partition("=")
            if name not in {"affirmed", "uncertain", "none_bar", "sarcasm", "past", "task", "unprompted"} or not 0 < float(value) < 1:
                raise KernelError("Thresholds: affirmed, uncertain, none_bar, sarcasm, past, task or unprompted, between 0 and 1.")
            stored["thresholds"] = dict(stored.get("thresholds", {}), **{name: float(value)})
        if args.enable or args.disable or args.allow_remote_judge or args.auto_capture or args.threshold or args.repository \
                or args.repository_commits or args.keep_alive:
            store.set_policy(stored)
            if args.threshold:
                # Verdicts were about the old thresholds; the next session's warm-up checks the new ones.
                store.clear_judge_records()
        return capture_policy(store)
    if command == "standing":
        row = store.inspect(args.id)
        store.set_standing(row["entity_key"], row["predicate"], args.state, "owner")
        return {"id": row["id"], "standing": args.state == "on"}
    if command == "learn":
        from .repository import learn, root_of
        root = root_of(args.workspace)
        if not root:
            raise KernelError("No git repository at that workspace.")
        return learn(store, make_judge(args), root, Budget(90), args.session, args.if_changed)
    if command == "warm":
        from .warm import warm
        return warm(store, make_judge(args), args.workspace, args.session, args.learn)
    if command == "dependents":
        return store.dependents(args.id, args.as_of)
    if command == "stale":
        return store.stale(args.as_of)
    if command == "depend":
        return store.depend(args.id, args.assumption_id)
    if command == "alias":
        store.add_alias(args.entity, args.alias)
        return {"status": "added"}
    if command == "relate":
        return store.relate(args.child, args.parent)
    if command == "resolve":
        return store.resolve_entity(args.reference)
    if command == "transition":
        return store.transition(args.entity, args.event, args.evidence)
    if command == "propose":
        return store.propose(args.operation, parse_json(args.payload))
    if command == "proposals":
        return store.proposals()
    if command == "traces":
        return store.traces(args.limit)
    if command == "why":
        return store.trace(args.id)
    if args.strategy == "jev" and command == "hook" and args.jev_timeout > 10:
        raise KernelError("Prompt hooks allow a jev timeout of at most 10 seconds.")
    judge = make_judge(args)
    rules = capture_policy(store)
    budget = Budget(8) if command == "hook" else None
    compiler = Compiler(store, getattr(args, "budget", 2048), judge if args.strategy == "jev" else None,
                        deadline=budget, allow_remote=rules["allow_remote_judge"])
    if command == "serve":
        from .mcp import serve
        serve(store, sys.stdin.buffer, sys.stdout, compiler, args.strategy, judge)
        return None
    if command == "project":
        plan = NeedPlan.from_dict(store.load_plan(args.plan_id)) if args.plan_id else None
        return compiler.project(args.query, args.strategy, plan, args.as_of).to_dict()
    event = read_event(sys.stdin.buffer)
    from .repository import start_background, warm_command
    from .warm import mark_started, recently_started
    if args.event == "stop":
        print(canonical(stop_response(event, args.workspace, store, judge, Budget(15))), flush=True)
        return None
    if args.event == "session-start":
        def warm_in_background(cwd, session, learn_repository):
            if judge is None and not learn_repository:
                return
            mark_started(store)
            start_background(*warm_command(store.path, store.scope, cwd, session, judge.command if judge else None,
                                           learn_repository))

        print(canonical(session_start_response(event, args.workspace, store, args.client, warm=warm_in_background)), flush=True)
        return None

    def warm_judge():
        # A cold model: load it for the next prompt, once per minute at most.
        if judge is not None and not recently_started(store):
            mark_started(store)
            start_background(*warm_command(store.path, store.scope, jev=judge.command))

    response, projection_id = hook_response(event, args.workspace, store, compiler, args.strategy, args.proposals,
                                            args.fail_closed, args.client, judge, budget, warm=warm_judge)
    # "Emitted" means this process wrote the envelope, never host acknowledgement.
    print(canonical(response), flush=True)
    if projection_id is None:
        return None
    try:
        store.mark_emitted(projection_id)
    except (KernelError, sqlite3.Error, OSError) as exc:
        raise DeliveryError("Memory trace update failed after output. Delivery confirmation is unavailable; retry after checking local storage.") from exc
    return None


def latency_metrics(store, limit=100):
    """What the last projections cost: time to select, how often jev answered, cached or fell back to rules."""
    traces = store.traces(limit)
    if not traces:
        return {"projections": 0}
    times = sorted(t.get("latency_ms") or 0 for t in traces)
    fallbacks = sum(1 for t in traces if "jev_unavailable" in t.get("warnings", []))
    judged = [t for t in traces if (t.get("usage") or {}).get("calls")]
    return {"projections": len(traces), "median_ms": round(statistics.median(times)),
            "p90_ms": round(times[min(len(times) - 1, int(0.9 * len(times)))]),
            "judged_by_jev": len(judged), "fell_back_to_rules": fallbacks,
            "cached_pairs": sum((t.get("usage") or {}).get("cached", 0) for t in traces),
            "pace_seconds_per_pair": store.meta("judge_pair_seconds")}


def make_judge(args):
    """The jev client when it is installed; capture, inference, and owner actions need it even when
    selection uses rules. Missing jev is not an error here: those features report it when used."""
    command = getattr(args, "jev_command", "jev")
    resolved = command if Path(command).is_absolute() else shutil.which(command)
    if not resolved and getattr(args, "strategy", "rules") != "jev":
        return None
    return JevCommand(resolved or command, getattr(args, "jev_timeout", 10), getattr(args, "jev_critical", 0.6),
                      getattr(args, "jev_supporting", 0.5), getattr(args, "jev_question", None),
                      getattr(args, "jev_band", 0.35), getattr(args, "jev_max_pairs", 48))


def main(argv=None):
    args = parser().parse_args(argv)
    store = None
    try:
        if args.command == "calibrate" and not args.check:
            execute(args, None)
            return 0
        if args.command == "adapter":
            result = configuration(args.client, args.workspace, args.db, args.scope, args.python, args.mode,
                                   args.proposals, args.strategy, args.fail_closed, args.jev_command)
            if args.raw:
                print(result["content"] if "content" in result else canonical(result["config"]))
                return 0
        else:
            store = Store(args.db, args.scope, create=args.command == "init")
            result = execute(args, store)
        if result is not None:
            print(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False) if args.pretty else canonical(result))
        return 0
    except (KernelError, sqlite3.Error, OSError) as exc:
        message = str(exc) if isinstance(exc, KernelError) else "Local storage or process I/O failed."
        if isinstance(exc, DeliveryError):
            print(message, file=sys.stderr)
            return 2  # Block visibly without corrupting stdout with a second JSON envelope.
        if store is not None:
            try:
                store.record_failure(args.command)
            except (KernelError, sqlite3.Error, OSError):
                pass  # The visible response below does not depend on a working log.
        if args.command == "hook":
            if args.event != "prompt":
                print(canonical({"systemMessage": "Context Kernel: " + message}), flush=True)
            elif isinstance(exc, HookBlock) or args.fail_closed:
                print(canonical(block("Context Kernel: " + message)), flush=True)
            else:
                # Fail open: a memory add-on must not stop the prompt; the host hears why there is no context.
                print(canonical(envelope("", ["Context Kernel unavailable: " + message])), flush=True)
            return 0
        print(canonical({"error": message}), file=sys.stderr)
        return 1
    finally:
        if store is not None:
            store.close()


if __name__ == "__main__":
    raise SystemExit(main())
