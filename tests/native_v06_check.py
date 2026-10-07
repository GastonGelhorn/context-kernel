"""Opt-in native check of v0.6: decisions read from the repository, and standing facts.

Claude Code: builds a disposable git repository with one decision record, wires it with the generated
hooks and the memory MCP server (as tests/native_claude_check.py does), and runs fresh headless
`claude -p` sessions in it. File, shell and web tools are disallowed; only the memory tools are allowed.

Codex: runs `codex exec` sessions in a pilot whose folder and hooks the owner already trusted in Codex
(prepared with tests/native_codex_v04_check.py --prepare). The runner resets the pilot's memory and
recreates the git repository it owns there (`.git` and `docs/adr`), which leaves the trusted hook
definitions unchanged.

Nobody types a memory command. The kernel database is inspected after each phase; the model's own
claims are not taken as evidence. Both use the owner's sign-in and quota with fictional prompts.
"""

import argparse
import json
from pathlib import Path
import shutil
import subprocess
import time

from shelflife_context.adapters import configuration
from shelflife_context.common import KernelError, canonical
from shelflife_context.store import Store
from tests.native_claude_check import invoke


RECORD = """# {number}. {title}

## Status

{status}

## Decision

{decision}
"""
PHASES = {
    "first_session": "Say ok.",
    "decision_question": "Which queue should the new email worker use in this project? Recommend one in one sentence.",
    "standing_in_use": "Give the one-line shell command that adds the lodash package to this project. Only the command.",
    "after_change": "Say ok.",
    "fresh_session_flags_review": "Should the email worker still use the queue you recommended earlier? Answer in one sentence.",
}


def git(root, *args):
    env = {"GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1", "PATH": "/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin"}
    subprocess.run(["git", "-c", "user.name=Ana", "-c", "user.email=ana@example.com", "-C", str(root), *args],
                   check=True, capture_output=True, text=True, env=env)


def record(root, number, title, status, decision):
    path = root / "docs" / "adr" / f"{number:04d}-{title.lower().replace(' ', '-')}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(RECORD.format(number=number, title=title, status=status, decision=decision), encoding="utf-8")


def wait_for_pass(database, root, previous=None, seconds=90):
    """The SessionStart hook only starts the pass; wait until it stored a new signature."""
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        store = Store(database, scope="pilot")
        try:
            row = store.repository(str(Path(root).resolve()))
        finally:
            store.close()
        if row and row["signature"] and row["signature"] != previous:
            return row["signature"]
        time.sleep(1)
    return None


def snapshot(database):
    store = Store(database, scope="pilot")
    try:
        history = store.records(history=True)
        turn = store.db.execute("SELECT * FROM turns WHERE scope=? ORDER BY opened_at DESC LIMIT 1", (store.scope,)).fetchone()
        trace = store.trace(turn["projection_id"]) if turn and turn["projection_id"] else {}
        return {
            "records": {r["predicate"]: {"id": r["id"], "state": r["effective_state"], "source": r["source_kind"],
                                         "superseded_by": r["superseded_by"]} for r in history if r["predicate"].startswith("adr_")},
            "standing": [f"{e}.{p}" for e, p in store.standing()],
            "selected": trace.get("selected", []), "reasons": trace.get("reasons", {}), "warnings": trace.get("warnings", []),
            "inferred_to": [row[0] for row in store.db.execute(
                "SELECT to_statement FROM relations WHERE scope=? AND kind='depends_on' AND provenance='inferred'", (store.scope,))],
            "notice": [row[0] for row in store.db.execute("SELECT notice FROM sessions WHERE notice IS NOT NULL")],
        }
    finally:
        store.close()


def seed(root, database):
    """A repository with one accepted decision record, and memory holding one standing fact."""
    git(root, "init", "-q", "-b", "main")
    record(root, 1, "Use SQLite for the job queue", "Accepted", "Jobs stay in one SQLite file next to the app.")
    git(root, "add", "docs")
    git(root, "commit", "-q", "-m", "Record the queue decision")
    store = Store(database, scope="pilot", create=True)
    try:
        standing = store.remember("user", "package_manager", "pnpm", "Usa pnpm, siempre.")
        store.set_standing("user", "package_manager", "on", "owner")
        return standing
    finally:
        store.close()


def run(claude, workspace, model=None, jev_command="jev"):
    root = Path(workspace).resolve()
    if not root.is_dir() or any(root.iterdir()):
        raise KernelError("Supply an empty disposable workspace for the native pilot.")
    database = root.parent / (root.name + "-memory.sqlite")
    standing = seed(root, database)
    hooks = configuration("claude", root, database, "pilot", strategy="jev", jev_command=jev_command)
    (root / ".claude").mkdir()
    (root / ".claude" / "settings.local.json").write_text(json.dumps(hooks["config"], indent=2))
    server = configuration("claude", root, database, "pilot", mode="mcp", strategy="jev", jev_command=jev_command)
    mcp_config = root.parent / (root.name + "-mcp.json")
    mcp_config.write_text(json.dumps(server["config"], indent=2))
    report = {"client": "Claude Code", "version": subprocess.run([claude, "--version"], capture_output=True, text=True).stdout.strip(),
              "model": model or "client default"}
    return scenario(report, root, database, standing, lambda prompt: invoke(claude, root, prompt, mcp_config, model))


def run_codex(codex, workspace, model=None, disable=()):
    from tests.native_codex_v04_check import environment, invoke as codex_invoke, trust_state
    root = Path(workspace).resolve()
    trust = trust_state(root)
    if not trust["folder_trusted"] or not all(trust["hooks_reviewed"].values()):
        raise KernelError(f"Codex has not recorded trust for this pilot ({canonical(trust)}); prepare and trust it first.")
    status = subprocess.run([codex, "login", "status"], capture_output=True, text=True, timeout=15, env=environment())
    if status.returncode or "ChatGPT" not in status.stdout + status.stderr:
        raise KernelError("ChatGPT sign-in is required; this runner never falls back to an API key.")
    database = root / "memory.sqlite"
    for owned in (root / ".git", root / "docs" / "adr"):
        if owned.exists():
            shutil.rmtree(owned)  # only what this runner created in the pilot
    for suffix in ("", "-journal"):
        Path(str(database) + suffix).unlink(missing_ok=True)
    standing = seed(root, database)
    report = {"client": "Codex CLI", "version": subprocess.run([codex, "--version"], capture_output=True, text=True).stdout.strip(),
              "model": model or "Codex CLI configured model", "provider": "chatgpt subscription"}
    return scenario(report, root, database, standing, lambda prompt: codex_invoke(codex, root, prompt, model, disable))


def scenario(report, root, database, standing, ask):
    report.update(memory_commands_typed=0, phases=[],
                  evaluation_scope="one fictional repository through the real hooks and MCP server; not a quality benchmark")

    def phase(name):
        result = ask(PHASES[name])
        report["phases"].append(dict(result, name=name, state=snapshot(database)))
        return report["phases"][-1]

    first = phase("first_session")
    signature = wait_for_pass(database, root)
    report["first_pass_seconds_waited"] = signature is not None
    learned = snapshot(database)
    question = phase("decision_question")
    in_use = phase("standing_in_use")
    record(root, 1, "Use SQLite for the job queue", "Superseded by [2. Use Redis for the job queue](0002-use-redis-for-the-job-queue.md)",
           "Jobs stay in one SQLite file next to the app.")
    record(root, 2, "Use Redis for the job queue", "Accepted", "Jobs move to Redis so several workers can share them.")
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "Move the job queue to Redis")
    phase("after_change")
    changed = wait_for_pass(database, root, previous=signature)
    flagged = phase("fresh_session_flags_review")
    adr = learned["records"].get("adr_0001", {})
    after = flagged["state"]["records"]
    report["checks"] = {
        "record_learned_in_background": adr.get("source") == "repository" and adr.get("state") == "active",
        "standing_delivered_on_first_prompt": standing["id"] in first["state"]["selected"]
                                              and first["state"]["reasons"].get(standing["id"]) == "standing",
        "record_delivered_to_the_question": adr.get("id") in question["state"]["selected"],
        "answer_uses_the_record": "sqlite" in (question.get("answer") or "").casefold(),
        "recommendation_linked_to_the_record": adr.get("id") in question["state"]["inferred_to"],
        "standing_fact_used": "pnpm" in (in_use.get("answer") or "").casefold(),
        "supersession_learned_in_background": changed is not None
                                              and after.get("adr_0001", {}).get("superseded_by") == after.get("adr_0002", {}).get("id"),
        "review_flagged_in_a_fresh_session": "review_recommended" in flagged["state"]["warnings"],
        "answer_names_the_change": "redis" in (flagged.get("answer") or "").casefold(),
    }
    report["passed"] = all(report["checks"].values())
    report["wall_seconds"] = round(sum(p["duration_seconds"] for p in report["phases"]), 3)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", required=True, help="Claude Code: an empty disposable directory. Codex: a trusted pilot")
    parser.add_argument("--client", choices=("claude", "codex"), default="claude")
    parser.add_argument("--claude", default=shutil.which("claude"))
    parser.add_argument("--codex", default=shutil.which("codex"))
    parser.add_argument("--model")
    parser.add_argument("--jev-command", default="jev")
    parser.add_argument("--disable-mcp", nargs="*", default=[], help="Codex: other configured MCP servers to switch off")
    args = parser.parse_args()
    if not (args.claude if args.client == "claude" else args.codex):
        print(canonical({"error": f"{args.client} CLI not found"}))
        return 1
    try:
        report = run(args.claude, args.workspace, args.model, args.jev_command) if args.client == "claude" \
            else run_codex(args.codex, args.workspace, args.model, args.disable_mcp)
    except KernelError as exc:
        print(canonical({"error": str(exc)}))
        return 1
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
