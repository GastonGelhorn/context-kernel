"""Opt-in native Claude Code check of the v0.9 flow: a fact that ends with the period it names, and a step that is
closed, not forgotten, when the user says it is done. No memory commands, only conversation.

Same setup as tests/native_claude_check.py: the installed `claude` CLI headless in an empty pilot directory with
the generated hooks and MCP server, each phase a fresh session. It uses the owner's sign-in and quota with
fictional prompts. The kernel database is inspected afterwards; the model's own claims are not evidence.

    python3 -m tests.native_close_check --workspace "$(mktemp -d)" --jev-command "$(which jev)"
"""

import argparse
import json
from pathlib import Path
import shutil
import subprocess

from shelflife_context.adapters import configuration
from shelflife_context.common import KernelError, canonical
from shelflife_context.store import Store
from tests.native_claude_check import MEMORY_TOOLS, invoke

TOOLS = MEMORY_TOOLS + ",mcp__shelflife-context__memory_close"
PHASES = [
    ("plan_stated", "Our next step for the billing service is migrating it to Stripe."),
    ("period_stated", "Decidimos publicar la v0.3 del portal esta semana."),
    ("done_said", "We shipped the billing migration to Stripe yesterday."),
    ("asked_after", "What is the next step for the billing service? Answer in one sentence."),
]


def snapshot(database):
    store = Store(database, scope="pilot")
    try:
        history = [r for r in store.records(history=True) if r["assertion_kind"] != "inference"]

        def about(*words):
            return [{"id": r["id"], "value": r["value"], "state": r["effective_state"], "until": r["valid_until"]}
                    for r in history if any(w in json.dumps(r["value"], ensure_ascii=False).lower() for w in words)]
        return {"stripe": about("stripe"), "portal": about("0.3"), "closes": store.close_metrics(),
                "removals": store.db.execute("SELECT count(*) FROM operations WHERE operation IN ('forget','revoke','undo')").fetchone()[0],
                "taken_back": store.regret_metrics()["taken_back"], "captures": store.capture_metrics(),
                "last_trace": (store.traces(1) or [{}])[0]}
    finally:
        store.close()


def run(claude, workspace, model=None, jev_command="jev"):
    workspace = Path(workspace).resolve()
    if not workspace.is_dir() or any(workspace.iterdir()):
        raise KernelError("Supply an empty disposable workspace for the native pilot.")
    database = workspace / "memory.sqlite"
    Store(database, scope="pilot", create=True).close()
    hooks = configuration("claude", workspace, database, "pilot", strategy="jev", jev_command=jev_command)
    (workspace / ".claude").mkdir()
    (workspace / ".claude" / "settings.local.json").write_text(json.dumps(hooks["config"], indent=2))
    server = configuration("claude", workspace, database, "pilot", mode="mcp", strategy="jev", jev_command=jev_command)
    mcp_config = workspace / "mcp.json"
    mcp_config.write_text(json.dumps(server["config"], indent=2))
    report = {"client": "Claude Code", "version": subprocess.run([claude, "--version"], capture_output=True, text=True).stdout.strip(),
              "model": model or "client default", "phases": [],
              "evaluation_scope": "one fictional scenario through the real hooks and MCP server; not a quality benchmark"}
    for name, prompt in PHASES:
        result = invoke(claude, workspace, prompt, mcp_config, model, tools=TOOLS)
        if result["exit_code"] != 0 and "authenticate" in (result["answer"] or "").casefold():
            report["aborted"] = "Claude Code sign-in expired; run `claude auth login`, then start a new pilot directory."
            report["phases"].append(dict(result, name=name))
            break
        report["phases"].append(dict(result, name=name, state=snapshot(database)))
    checks = evaluate(report["phases"])
    report["checks"] = checks
    report["passed"] = bool(checks) and all(checks.values()) and "aborted" not in report
    report["wall_seconds"] = round(sum(p["duration_seconds"] for p in report["phases"]), 3)
    report["cost_usd_reported"] = [p.get("cost_usd") for p in report["phases"]]
    return report


def evaluate(phases):
    """Fixed before any run; each check needs positive evidence in the kernel database."""
    states = {p["name"]: p.get("state") for p in phases}
    if len(states) != len(PHASES) or not all(states.values()):
        return {}
    plan, period, done, after = (states[n] for n, _ in PHASES)
    stripe = {r["id"] for r in done["stripe"]}
    return {
        "plan_captured": any(r["state"] == "active" for r in plan["stripe"]),
        "period_fact_ends_with_its_week": any(r["state"] == "active" and r["until"] for r in period["portal"]),
        "done_step_closed_not_removed": bool(plan["stripe"]) and any(r["state"] == "expired" for r in done["stripe"])
                                        and done["closes"]["done"] >= 1 and done["removals"] == 0,
        "close_not_counted_as_regret": not any(done["taken_back"].values()),
        "closed_step_not_delivered_later": bool(stripe) and not set(after["last_trace"].get("selected") or []) & {
            r["id"] for r in done["stripe"] if r["state"] == "expired"},
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--workspace", required=True, help="Empty disposable directory")
    parser.add_argument("--claude", default=shutil.which("claude"))
    parser.add_argument("--model", help="Optional model override; default is the client's configured model")
    parser.add_argument("--jev-command", default="jev")
    args = parser.parse_args()
    if not args.claude:
        print(canonical({"error": "claude CLI not found"}))
        return 1
    try:
        report = run(args.claude, args.workspace, args.model, args.jev_command)
    except KernelError as exc:
        print(canonical({"error": str(exc)}))
        return 1
    print(canonical(report))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
