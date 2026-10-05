"""Owner CLI; agents get a separate read/propose-only MCP interface."""

import argparse
import json
import sqlite3
import sys

from .adapters import HookBlock, configuration, envelope, hook_response
from .common import KernelError, canonical, quantity
from .compiler import Compiler
from .planner import Jev, NeedPlan, Ollama
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
    for name in ("init", "status", "proposals"):
        commands.add_parser(name)
    serve = commands.add_parser("serve", help="Stdio MCP server with a fixed scope")
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
    for name in ("inspect", "revoke", "forget", "approve", "reject", "why", "dependents", "reaffirm"):
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
    hook.add_argument("--proposals", action="store_true", help="Opt in to bounded command proposals, never automatic approval")
    hook.add_argument("--fail-closed", action="store_true", help="Block the prompt when memory is unavailable (default: proceed without memory)")
    for item in (project, hook, serve):
        item.add_argument("--strategy", choices=("rules", "fts", "inferred", "jev"), default="rules")
        item.add_argument("--ollama-url", default="http://127.0.0.1:11434")
        item.add_argument("--model", default="qwen3.5:9b")
        item.add_argument("--ollama-timeout", type=float, default=45 if item is project else 10,
                          help="Local model timeout in seconds (hooks capped at 10)")
        item.add_argument("--jev-command", default="jev", help="jev executable; its own config decides local or hosted")
        item.add_argument("--jev-timeout", type=float, default=10)
        item.add_argument("--jev-critical", type=float, default=0.6, help="P at or above which a pair is critical")
        item.add_argument("--jev-supporting", type=float, default=0.5, help="P at or above which a pair is supporting")
        item.add_argument("--jev-question", help="Override the relevance question (name `candidate` and `query`)")
    adapter = commands.add_parser("adapter")
    adapter.add_argument("client", choices=("codex", "claude", "antigravity"))
    adapter.add_argument("--workspace", required=True)
    adapter.add_argument("--mode", choices=("hook", "mcp"), default="hook")
    adapter.add_argument("--proposals", action="store_true")
    adapter.add_argument("--strategy", choices=("rules", "fts", "inferred", "jev"), default="rules")
    adapter.add_argument("--fail-closed", action="store_true")
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
        return store.remember(args.entity, args.predicate, value, args.evidence,
                              kind=args.kind, label=args.label, assertion_kind=args.assertion_kind,
                              valid_from=args.valid_from, valid_until=args.valid_until)
    if command == "correct":
        return store.correct(args.id, value, args.evidence, args.valid_from, args.valid_until)
    if command == "inspect":
        return store.inspect(args.id, args.as_of)
    if command == "list":
        return store.records(args.as_of, args.history)
    if command in {"revoke", "forget", "approve", "reject", "reaffirm"}:
        return getattr(store, command)(args.id)
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
    if args.strategy == "inferred" and (not 0 < args.ollama_timeout <= 60 or (command == "hook" and args.ollama_timeout > 10)):
        raise KernelError("Ollama timeout must be 1-60 seconds; prompt hooks allow at most 10.")
    if args.strategy == "jev" and command == "hook" and args.jev_timeout > 10:
        raise KernelError("Prompt hooks allow a jev timeout of at most 10 seconds.")
    ollama = Ollama(args.ollama_url, args.model, args.ollama_timeout) if args.strategy == "inferred" else None
    jev = Jev(args.jev_command, args.jev_timeout, args.jev_critical, args.jev_supporting, args.jev_question) if args.strategy == "jev" else None
    compiler = Compiler(store, getattr(args, "budget", 2048), ollama, jev)
    if command == "serve":
        from .mcp import serve
        serve(store, sys.stdin.buffer, sys.stdout, compiler, args.strategy)
        return None
    if command == "project":
        plan = NeedPlan.from_dict(store.load_plan(args.plan_id)) if args.plan_id else None
        return compiler.project(args.query, args.strategy, plan, args.as_of).to_dict()
    event = read_event(sys.stdin.buffer)
    response, projection_id = hook_response(event, args.workspace, store, compiler, args.strategy, args.proposals, args.fail_closed)
    # "Emitted" means this process wrote the envelope, never host acknowledgement.
    print(canonical(response), flush=True)
    if projection_id is None:
        return None
    try:
        store.mark_emitted(projection_id)
    except (KernelError, sqlite3.Error, OSError) as exc:
        raise DeliveryError("Memory trace update failed after output. Delivery confirmation is unavailable; retry after checking local storage.") from exc
    return None


def main(argv=None):
    args = parser().parse_args(argv)
    store = None
    try:
        if args.command == "adapter":
            result = configuration(args.client, args.workspace, args.db, args.scope, args.python, args.mode,
                                   args.proposals, args.strategy, args.fail_closed)
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
            if isinstance(exc, HookBlock) or args.fail_closed:
                print(canonical({"decision": "block", "reason": "Context Kernel: " + message}), flush=True)
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
