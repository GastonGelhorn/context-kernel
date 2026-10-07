"""Opt-in native check of an agent without hooks: the AGENTS.md brief and the hookless MCP server.

Claude Code stands in for an agent that has no hooks: the pilot folder has no hook configuration at all, its
CLAUDE.md only imports AGENTS.md (what such agents read on their own), and memory is reachable only through
`serve --hookless`. The kernel's database is seeded with a project whose deadline changed, whose earlier advice
rested on the old deadline, and whose step was closed. Each phase is a fresh headless session; the checks read
the database and the answers. It uses the owner's sign-in and quota with fictional prompts.

    python3 -m tests.native_hookless_check --workspace "$(mktemp -d)" --jev-command "$(which jev)"
"""

import argparse
import json
from pathlib import Path
import shutil
import subprocess

from shelflife_context import brief
from shelflife_context.adapters import configuration
from shelflife_context.common import KernelError, canonical
from shelflife_context.repository import entity_for
from shelflife_context.store import Store
from tests.native_claude_check import invoke

TOOLS = ",".join("mcp__shelflife-context__" + name for name in (
    "memory_context", "memory_capture", "memory_inventory", "memory_inspect", "memory_history", "memory_dependents"))
PHASES = [
    ("reads_the_brief", "Who signs off releases here, and how long do we have for checkout? Answer in one sentence."),
    ("saves_for_review", "Keep this in mind for later: our staging database is Postgres 16."),
]


def seed(root, database):
    store = Store(database, scope="pilot", create=True)
    try:
        project = entity_for(store, str(root))
        deadline = store.remember(project, "checkout_deadline", "three months", "We have three months for checkout.", kind="project")["id"]
        advice = store.remember(project, "recommendation", "Rewrite the payment module", "Rewrite the payment module",
                                assertion_kind="inference")["id"]
        store.depend(advice, deadline, provenance="inferred")
        store.remember(project, "release_approver", "Irene", "Irene signs off releases.", kind="project")
        step = store.remember(project, "next_step", "Migrate billing to Stripe", "Next we migrate billing to Stripe.", kind="project")["id"]
        store.correct(deadline, "three weeks", "The checkout deadline changed: we now have three weeks.")
        store.conclude(step, "done")
        brief.set_enabled(store, str(root), True)
        return brief.write(store, str(root))
    finally:
        store.close()


def snapshot(database):
    store = Store(database, scope="pilot")
    try:
        held = [r for r in store.records(quarantined=True) if r["trust"] == "quarantined"]
        return {"held": [{"value": r["value"], "predicate": r["predicate"]} for r in held],
                "evidence_values": [r["value"] for r in store.records()],
                "hookless_reasons": [r["reason"] for r in store.capture_metrics() if r["outcome"] == "quarantined"],
                "hook_turns": store.db.execute("SELECT count(*) FROM turns WHERE session_id NOT LIKE 'hookless:%'").fetchone()[0]}
    finally:
        store.close()


def run(claude, workspace, model=None, jev_command="jev"):
    root = Path(workspace).resolve()
    if not root.is_dir() or any(root.iterdir()):
        raise KernelError("Supply an empty disposable workspace for the native pilot.")
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    database = root / ".pilot-memory.sqlite"
    written = seed(root, database)
    (root / "CLAUDE.md").write_text("@AGENTS.md\n", encoding="utf-8")
    server = configuration("generic", root, database, "pilot", strategy="jev", jev_command=jev_command)
    mcp_config = root / "mcp.json"
    mcp_config.write_text(json.dumps(server["config"], indent=2))
    report = {"client": "Claude Code (no hooks, standing in for any agent that reads AGENTS.md)",
              "version": subprocess.run([claude, "--version"], capture_output=True, text=True).stdout.strip(),
              "brief": written, "agents_md": (root / "AGENTS.md").read_text(encoding="utf-8"), "phases": [],
              "evaluation_scope": "one fictional scenario through AGENTS.md and the hookless MCP server; not a quality benchmark"}
    for name, prompt in PHASES:
        result = invoke(claude, root, prompt, mcp_config, model, tools=TOOLS)
        if result["exit_code"] != 0 and "authenticate" in (result["answer"] or "").casefold():
            report["aborted"] = "Claude Code sign-in expired; run `claude auth login`, then start a new pilot directory."
            report["phases"].append(dict(result, name=name))
            break
        report["phases"].append(dict(result, name=name, state=snapshot(database)))
    report["checks"] = evaluate(report)
    report["passed"] = bool(report["checks"]) and all(report["checks"].values()) and "aborted" not in report
    report["wall_seconds"] = round(sum(p["duration_seconds"] for p in report["phases"]), 3)
    report["cost_usd_reported"] = [p.get("cost_usd") for p in report["phases"]]
    return report


def evaluate(report):
    """Fixed before any run."""
    phases = {p["name"]: p for p in report["phases"]}
    if len(phases) != len(PHASES) or not all(p.get("state") for p in phases.values()):
        return {}
    answer = (phases["reads_the_brief"].get("answer") or "").casefold()
    after = phases["saves_for_review"]["state"]
    brief_text = report["agents_md"]
    return {
        "brief_shows_change_review_and_close": "was three months" in brief_text and "Advice from" in brief_text
                                               and "Migrate billing to Stripe (done" in brief_text,
        "answer_uses_the_current_facts": "irene" in answer and ("three weeks" in answer or "3 weeks" in answer),
        "statement_held_for_review": any("postgres" in json.dumps(h["value"]).casefold() for h in after["held"])
                                     and "hookless" in after["hookless_reasons"],
        "nothing_saved_as_evidence": not any("postgres" in json.dumps(v).casefold() for v in after["evidence_values"]),
        "no_hook_ever_ran": after["hook_turns"] == 0,
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
