"""Opt-in native Claude Code check of the v0.4 flow: no memory commands, only conversation.

Runs the installed `claude` CLI headless (`-p`) in an empty pilot directory that carries the
generated bundle: prompt, stop, and session-start hooks in `.claude/settings.local.json` and the
memory MCP server through `--mcp-config`. Each phase is a fresh session. The kernel database is
inspected directly afterwards; the model's own claims are not taken as evidence.

It uses the owner's Claude Code sign-in and quota and sends fictional prompts to Anthropic. It never
edits global settings or bypasses permissions: only the memory tools are allowed, and file, shell,
and web tools are disallowed.
"""

import argparse
import json
import os
import shutil
import subprocess
import time
from pathlib import Path

from context_kernel.adapters import configuration
from context_kernel.common import KernelError, canonical
from context_kernel.store import Store


ROOT = Path(__file__).resolve().parent.parent
TOOLS_OFF = "Bash,Read,Glob,Grep,Edit,Write,MultiEdit,NotebookEdit,WebFetch,WebSearch,Agent,Task"
MEMORY_TOOLS = ",".join("mcp__context-kernel__" + name for name in (
    "memory_context", "memory_capture", "memory_undo", "memory_inventory", "memory_history", "memory_dependents",
    "memory_forget", "memory_revoke", "memory_confirm", "memory_reaffirm", "memory_depend", "memory_policy"))
PHASES = [
    ("constraint_and_recommendation",
     "We have three months to deliver the checkout project. Should we rewrite its payment module? Answer in one sentence."),
    ("constraint_changes",
     "Update: the checkout deadline changed, we now have three weeks."),
    ("fresh_session_recalls_and_flags",
     "Should we still go ahead with what we discussed for the checkout payment module? Answer in one sentence."),
    ("forget_by_asking",
     "Please forget the checkout deadline."),
    ("generic_question",
     "Explain what a SQLite primary key is in one sentence."),
]


def invoke(claude, workspace, prompt, mcp_config, model=None):
    command = [claude, "-p", prompt, "--output-format", "json", "--disallowedTools", TOOLS_OFF,
               "--allowedTools", MEMORY_TOOLS, "--mcp-config", str(mcp_config)]
    if model:
        command.extend(["--model", model])
    started = time.perf_counter()
    try:
        # An installed Context Kernel plugin would also run here, against the owner's real memory.
        env = dict(os.environ, CONTEXT_KERNEL_OFF="1")
        result = subprocess.run(command, cwd=workspace, input="", text=True, capture_output=True, timeout=240, env=env)
    except subprocess.TimeoutExpired as exc:
        return {"exit_code": 124, "answer": None, "stderr": str(exc), "duration_seconds": 240.0}
    raw = None
    try:
        raw = json.loads(result.stdout)
    except ValueError:
        pass
    return {"exit_code": result.returncode, "answer": raw.get("result") if isinstance(raw, dict) else result.stdout.strip() or None,
            "stderr": result.stderr[-1500:], "duration_seconds": round(time.perf_counter() - started, 3),
            "cost_usd": raw.get("total_cost_usd") if isinstance(raw, dict) else None}


def snapshot(database):
    store = Store(database, scope="pilot")
    try:
        history = store.records(history=True)
        # Keys are the agent's choice; the checks follow the values the user stated.
        timeline = [r for r in history if r["assertion_kind"] != "inference"
                    and any(w in json.dumps(r["value"]).lower() for w in ("month", "week", "mes", "semana"))]
        pairs = {(r["entity_key"], r["predicate"]) for r in timeline}
        return {
            "deadline": [(r["value"], r["trust"], r["effective_state"], f"{r['entity_key']}.{r['predicate']}") for r in timeline],
            "deadline_pairs": sorted(f"{e}.{p}" for e, p in pairs),
            "deadline_current": [r["value"] for r in timeline if r["effective_state"] == "active"],
            "recommendations": [{"id": r["id"], "stale": r["stale"], "links": len(r["assumptions"])}
                                for r in history if r["predicate"] == "recommendation"],
            "inferred_links": store.db.execute("SELECT count(*) FROM relations WHERE provenance='inferred'").fetchone()[0],
            "captures": store.capture_metrics(),
            "last_trace": (store.traces(1) or [{}])[0],
            "sessions": store.db.execute("SELECT count(*) FROM sessions").fetchone()[0],
            "forget_ops": store.db.execute("SELECT count(*) FROM operations WHERE operation='forget'").fetchone()[0],
        }
    finally:
        store.close()


def run(claude, workspace, model=None, strategy="jev", jev_command="jev"):
    workspace = Path(workspace).resolve()
    if not workspace.is_dir() or any(workspace.iterdir()):
        raise KernelError("Supply an empty disposable workspace for the native pilot.")
    database = workspace / "memory.sqlite"
    Store(database, scope="pilot", create=True).close()
    hooks = configuration("claude", workspace, database, "pilot", strategy=strategy, jev_command=jev_command)
    (workspace / ".claude").mkdir()
    (workspace / ".claude" / "settings.local.json").write_text(json.dumps(hooks["config"], indent=2))
    server = configuration("claude", workspace, database, "pilot", mode="mcp", strategy=strategy, jev_command=jev_command)
    mcp_config = workspace / "mcp.json"
    mcp_config.write_text(json.dumps(server["config"], indent=2))
    report = {"client": "Claude Code", "version": subprocess.run([claude, "--version"], capture_output=True, text=True).stdout.strip(),
              "model": model or "client default", "strategy": strategy, "paid_api_calls": 0, "global_settings_edited": False,
              "permissions_bypassed": False, "memory_commands_typed": 0, "phases": [],
              "evaluation_scope": "one fictional scenario through the real hooks and MCP server; not a quality benchmark"}
    for name, prompt in PHASES:
        result = invoke(claude, workspace, prompt, mcp_config, model)
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
    """Checks fixed before any run, shared by the Claude Code and Codex runners. Each one needs
    positive evidence in the kernel database: an empty store does not count as a successful forget."""
    states = {p["name"]: p.get("state") for p in phases}
    if len(states) != len(PHASES) or not all(states.values()):
        return {}
    first, second, third, fourth, fifth = (states[n] for n, _ in PHASES)
    answer = (phases[2].get("answer") or "").casefold()
    return {
        "constraint_captured_from_conversation": any("month" in str(v).lower() for v in first["deadline_current"]),
        "write_accepted_through_bound_session": any(c["outcome"] == "captured" for c in first["captures"]),
        "recommendation_linked_by_inference": first["inferred_links"] > 0,
        "change_captured_as_new_version": len(second["deadline_pairs"]) == 1 and len(second["deadline"]) >= 2
                                          and all("week" in str(v).lower() for v in second["deadline_current"]),
        "recommendation_flagged_after_change": any(r["stale"] for r in second["recommendations"]),
        "fresh_session_projection_requires_review": third["last_trace"].get("status") == "review_required"
                                                     or "review_recommended" in third["last_trace"].get("warnings", []),
        "fresh_session_answer_names_the_change": ("week" in answer or "semana" in answer)
                                                 and any(w in answer for w in ("review", "revis", "reconsider", "changed", "no longer",
                                                                               "second look", "assumed", "re-examin", "re-evaluat")),
        "forget_removed_an_existing_deadline": bool(third["deadline_current"]) and not fourth["deadline_current"]
                                                and fourth["forget_ops"] > third["forget_ops"],
        "forget_removed_derived_recommendations": bool(third["recommendations"]) and not fourth["recommendations"],
        "generic_question_carried_no_claims": not fifth["last_trace"].get("selected"),
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", required=True, help="Empty disposable directory")
    parser.add_argument("--claude", default=shutil.which("claude"))
    parser.add_argument("--model", help="Optional model override; default is the client's configured model")
    parser.add_argument("--strategy", choices=("rules", "fts", "jev"), default="jev")
    parser.add_argument("--jev-command", default="jev")
    args = parser.parse_args()
    if not args.claude:
        print(canonical({"error": "claude CLI not found"}))
        return 1
    try:
        report = run(args.claude, args.workspace, args.model, args.strategy, args.jev_command)
    except KernelError as exc:
        print(canonical({"error": str(exc)}))
        return 1
    print(canonical(report))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
