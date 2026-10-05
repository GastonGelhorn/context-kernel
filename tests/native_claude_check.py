"""Opt-in native Claude Code prompt-hook check in a disposable workspace.

Runs the installed `claude` CLI headless (`-p`) in a pilot directory whose
`.claude/settings.local.json` carries the kernel hook. This uses the owner's
existing Claude Code sign-in and quota, sends fictional prompts to Anthropic, and
never edits global settings, bypasses permissions, or downloads anything. The
workspace must be an empty directory the owner supplies.
"""

import argparse
import json
from pathlib import Path
import shutil
import subprocess
import sys
import time

from context_kernel.adapters import configuration
from context_kernel.common import KernelError, canonical
from context_kernel.demo import stale_reader_check
from context_kernel.store import Store


ROOT = Path(__file__).resolve().parent.parent
QUESTION = "Who is the current release approver? Reply with just the name, or UNKNOWN if the available context does not say. Do not use tools."
REWRITE = ("Should we go ahead with the payment module rewrite for the checkout project? "
           "Answer in one sentence using only the provided context. Do not use tools.")
TOOLS_OFF = "Bash,Read,Glob,Grep,Edit,Write,MultiEdit,NotebookEdit,WebFetch,WebSearch,Agent,Task"


def invoke(claude, workspace, prompt, model=None):
    command = [claude, "-p", prompt, "--output-format", "json", "--disallowedTools", TOOLS_OFF]
    if model:
        command.extend(["--model", model])
    started = time.perf_counter()
    try:
        result = subprocess.run(command, cwd=workspace, input="", text=True, capture_output=True, timeout=180)
    except subprocess.TimeoutExpired as exc:
        return {"exit_code": 124, "answer": None, "stderr": str(exc), "duration_seconds": 180.0, "raw": None}
    answer, raw = None, None
    try:
        raw = json.loads(result.stdout)
        answer = raw.get("result") if isinstance(raw, dict) else None
    except ValueError:
        answer = result.stdout.strip() or None
    return {"exit_code": result.returncode, "answer": answer, "stderr": result.stderr[-2000:],
            "duration_seconds": round(time.perf_counter() - started, 3),
            "usage": raw.get("usage") if isinstance(raw, dict) else None,
            "cost_usd": raw.get("total_cost_usd") if isinstance(raw, dict) else None}


def run(claude, workspace, model=None, strategy="rules"):
    workspace = Path(workspace).resolve()
    if not workspace.is_dir() or any(workspace.iterdir()):
        raise KernelError("Supply an empty disposable workspace for the native pilot.")
    database = workspace / "memory.sqlite"
    store = Store(database, scope="pilot", create=True)
    try:
        config = configuration("claude", workspace, database, "pilot", strategy=strategy)
        destination = workspace / config["destination"]
        destination.parent.mkdir(parents=True)
        destination.write_text(json.dumps(config["config"], indent=2))
        report = {"client": "Claude Code", "version": subprocess.run([claude, "--version"], capture_output=True, text=True).stdout.strip(),
                  "model": model or "client default", "strategy": strategy, "hook_file": str(destination),
                  "paid_api_calls": 0, "global_settings_edited": False, "permissions_bypassed": False, "phases": [],
                  "evaluation_scope": "fictional lifecycle, dependency, and evidence-use checks through the real hook; not a quality benchmark"}

        def phase(name, prompt, expected=None, check=None):
            before = len(store.traces(100))
            result = invoke(claude, workspace, prompt, model)
            traces = store.traces(100)
            emitted = [t for t in traces[:len(traces) - before] if t["delivery"] == "emitted"]
            result.update(name=name, expected=expected, new_traces=len(traces) - before, emitted_projections=len(emitted),
                          projection_status=[t["status"] for t in emitted])
            answer = result["answer"] or ""
            result["passed"] = bool(emitted) and result["exit_code"] == 0 and (
                check(answer) if check else expected.casefold() in answer.casefold())
            report["phases"].append(result)
            return result

        phase("empty_memory", QUESTION, "UNKNOWN")
        approver = store.remember("user", "release_approver", "Nyra Vale", "Fictional pilot: the release approver is Nyra Vale.")
        phase("registered_approver_fresh_session", QUESTION, "Nyra Vale")
        corrected = store.correct(approver["id"], "Orin Keel", "Fictional pilot: the approver is now Orin Keel.")
        phase("corrected_approver_fresh_session", QUESTION, "Orin Keel",
              check=lambda a: "orin keel" in a.casefold() and "nyra" not in a.casefold())
        deadline = store.remember("checkout", "deadline", "three months", "Fictional pilot: three months.", kind="project")
        rewrite = store.remember("checkout", "decision", "Rewrite the payment module before launch",
                                 "Fictional pilot: rewrite agreed.", kind="project")
        store.depend(rewrite["id"], deadline["id"])
        store.correct(deadline["id"], "three weeks", "Fictional pilot: the deadline moved to three weeks.")
        phase("stale_recommendation_after_deadline_change", REWRITE, check=stale_reader_check)
        store.forget(corrected["id"])
        phase("forgotten_approver_fresh_session", QUESTION, "UNKNOWN")
        phase("generic_question_no_personal_context", "Explain what a SQLite primary key is in one sentence. Do not use tools.",
              check=lambda a: "nyra" not in a.casefold() and "orin" not in a.casefold() and "three weeks" not in a.casefold())
        report["passed"] = all(p["passed"] for p in report["phases"])
        report["wall_seconds"] = round(sum(p["duration_seconds"] for p in report["phases"]), 3)
        report["cost_usd_reported"] = [p.get("cost_usd") for p in report["phases"]]
        return report
    finally:
        store.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", required=True, help="Empty disposable directory")
    parser.add_argument("--claude", default=shutil.which("claude"))
    parser.add_argument("--model", help="Optional model override; default is the client's configured model")
    parser.add_argument("--strategy", choices=("rules", "fts", "jev"), default="rules")
    args = parser.parse_args()
    if not args.claude:
        print(canonical({"error": "claude CLI not found"}))
        return 1
    try:
        report = run(args.claude, args.workspace, args.model, args.strategy)
    except KernelError as exc:
        print(canonical({"error": str(exc)}))
        return 1
    print(canonical(report))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
