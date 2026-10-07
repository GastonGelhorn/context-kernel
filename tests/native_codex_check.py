"""Opt-in native Codex CLI checks with Ollama or ChatGPT subscription access.

No API keys, trust bypass, global settings edits, or automatic model downloads.
ChatGPT mode consumes subscription quota and sends fictional prompts to OpenAI.
The workspace must be a disposable directory explicitly supplied by the owner.
"""

import argparse
import json
import os
import re
from pathlib import Path
import shutil
import subprocess
import sys
import time

from shelflife_context.common import KernelError, canonical
from shelflife_context.store import Store
from shelflife_context.grounding import currency_review


ROOT = Path(__file__).resolve().parent.parent
MODEL = "qwen3.5:9b"
QUESTION = "Who is the current release approver?"
CARE_PROMPT = (
    "Use memory_context if it is available for: Should I accept a job offer that pays more? "
    "Answer in one sentence using only material constraints from authorized evidence. "
    "If the evidence does not state the offer's hours, do not assume them. "
    "Do not run shell commands or read files."
)


def child_environment():
    environment = os.environ.copy()
    for name in ("OPENAI_API_KEY", "CODEX_API_KEY", "CODEX_ACCESS_TOKEN", "OPENAI_BASE_URL"):
        environment.pop(name, None)
    return environment


def preflight(codex, provider, model):
    if provider == "chatgpt":
        status = subprocess.run([codex, "login", "status"], env=child_environment(),
                                capture_output=True, text=True, timeout=15)
        if status.returncode or "Logged in using ChatGPT" not in status.stdout + status.stderr:
            raise KernelError("ChatGPT subscription sign-in is required; API-key fallback is disabled.")
    else:
        ollama = shutil.which("ollama")
        if not ollama:
            raise KernelError("Ollama is not installed.")
        status = subprocess.run([ollama, "show", model], capture_output=True, text=True, timeout=15)
        if status.returncode:
            raise KernelError("The requested local model is not installed; this check will not download it.")


def invoke(codex, workspace, database, prompt, mcp=True, hook=None, provider="ollama", model=MODEL,
           reasoning="none"):
    command = [codex, "exec", "--model", model,
               "--ignore-user-config", "--skip-git-repo-check", "--ephemeral", "--sandbox", "read-only",
               "--disable", "apps", "--disable", "multi_agent", "--disable", "shell_tool",
               "--config", 'web_search="disabled"', "--config", "model_reasoning_effort=" + json.dumps(reasoning),
               "--json", "--cd", str(workspace)]
    if provider == "ollama":
        command.extend(["--oss", "--local-provider", "ollama"])
    elif provider == "chatgpt":
        command.extend(["--config", 'model_provider="openai"', "--config", 'forced_login_method="chatgpt"'])
    else:
        raise KernelError("Unsupported native provider.")
    if mcp:
        settings = {
            "mcp_servers.shelflife-context.command": sys.executable,
            "mcp_servers.shelflife-context.args": ["-m", "shelflife_context", "--db", str(database), "--scope", "pilot", "serve"],
            "mcp_servers.shelflife-context.env": {"PYTHONPATH": str(ROOT)},
        }
        for name, value in settings.items():
            # JSON scalar/array literals are also valid TOML; inline tables use TOML syntax.
            encoded = '{PYTHONPATH = ' + json.dumps(str(ROOT)) + '}' if isinstance(value, dict) else json.dumps(value)
            command.extend(["--config", name + "=" + encoded])
    if hook:
        handler = '{type="command", command=' + json.dumps(hook) + ', timeout=15, additionalContextLimit=2048}'
        command.extend(["--config", "hooks.UserPromptSubmit=[{hooks=[" + handler + "]}]"])
    command.append(prompt)
    started = time.perf_counter()
    try:
        result = subprocess.run(command, input="", text=True, capture_output=True, timeout=120,
                                env=child_environment())
    except subprocess.TimeoutExpired as exc:
        def decoded(value):
            return value.decode(errors="replace") if isinstance(value, bytes) else value or ""
        result = subprocess.CompletedProcess(command, 124, decoded(exc.stdout), decoded(exc.stderr))
    events = []
    for line in result.stdout.splitlines():
        try:
            event = json.loads(line)
            if isinstance(event, dict):
                events.append(event)
        except ValueError:
            pass
    items = [e["item"] for e in events if e.get("type") == "item.completed" and isinstance(e.get("item"), dict)]
    answers = [i["text"] for i in items if i.get("type") == "agent_message"]
    calls = [i for i in items if i.get("type") == "mcp_tool_call"]
    return {"exit_code": result.returncode, "answer": answers[-1] if answers else None,
            "tool_calls": calls, "events": events, "stderr": result.stderr,
            "duration_seconds": round(time.perf_counter() - started, 3)}


def summary(result, expected=None):
    allowed = [c for c in result["tool_calls"] if c.get("server") == "shelflife-context" and c.get("tool") == "memory_context"]
    successful = [c for c in allowed if c.get("status") == "completed"
                  and isinstance(c.get("result", {}).get("structured_content"), dict)]
    return {"exit_code": result["exit_code"], "answer": result["answer"], "duration_seconds": result["duration_seconds"],
            "context_tool_calls": len(allowed), "tool_calls": result["tool_calls"],
            "successful_context_tool_calls": len(successful),
            "failed_context_tool_calls": len(allowed) - len(successful),
            "usage": [e.get("usage") for e in result["events"] if e.get("type") == "turn.completed"],
            "passed": (result["exit_code"] == 0 and bool(successful) and result["answer"] == expected)
                      if expected is not None else result["exit_code"] == 0,
            "warnings": [i["item"].get("message") for i in result["events"] if i.get("type") == "item.completed" and i.get("item", {}).get("type") == "error"]}


def run(codex, workspace, provider="ollama", model=MODEL, reasoning="none", controls=False):
    preflight(codex, provider, model)
    workspace = Path(workspace).resolve()
    if not workspace.is_dir():
        raise KernelError("Create a disposable workspace before running this check.")
    database = workspace / "memory.sqlite"
    store = Store(database, scope="pilot", create=True)
    private = Store(database, scope="private")
    try:
        if store.records(history=True) or private.records(history=True):
            raise KernelError("The native pilot requires an empty database.")
        report = {"client": "Codex CLI", "model": model, "provider": provider, "reasoning": reasoning,
                  "authentication": "chatgpt_subscription" if provider == "chatgpt" else "local_oss",
                  "paid_api_calls": 0, "hook_trust_bypassed": False, "phases": [], "controls": [],
                  "quality_review_required": True,
                  "evaluation_scope": "small fictional lifecycle and evidence-use checks, not a quality benchmark",
                  "test_protocol": "v0.2 recovery instructions, Spanish correction query, unforced generic turn"}
        session_count = 0
        def call(prompt, mcp=True):
            nonlocal session_count
            session_count += 1
            result = invoke(codex, workspace, database, prompt, mcp=mcp, provider=provider,
                            model=model, reasoning=reasoning)
            print(f"Session {session_count} completed in {result['duration_seconds']}s (exit {result['exit_code']}).",
                  file=sys.stderr, flush=True)
            return result
        def question_prompt(question):
            return ("Use memory_context if available, with query set to the original question: " + question +
                    " If invalid arguments fail, retry once with that query. A tool error is not a missing fact. "
                    "Reply with only the exact name, UNKNOWN after successful empty retrieval or if no tool is available, "
                    "or UNAVAILABLE if retrieval fails. Do not run shell commands or read files.")
        prompt = question_prompt(QUESTION)
        empty = call(prompt)
        report["phases"].append({"condition": "empty_memory", **summary(empty, "UNKNOWN")})
        original = store.remember("project", "release_approver", "Nyra Vale", "The release approver is Nyra Vale.", kind="project")
        private.remember("project", "release_approver", "PRIVATE_ORCHID_CANARY", "PRIVATE_ORCHID_CANARY", kind="project")
        initial = call(prompt)
        report["phases"].append({"condition": "registered", **summary(initial, "Nyra Vale")})
        if controls:
            baseline = summary(call(prompt, mcp=False))
            baseline["passed"] = baseline["passed"] and baseline["answer"] == "UNKNOWN" and not baseline["tool_calls"]
            report["controls"].append({"condition": "registered_without_memory", **baseline})
        replacement = store.correct(original["id"], "Orin Keel", "The release approver is now Orin Keel.")
        corrected = call(question_prompt("¿Quién es el responsable actual del lanzamiento?"))
        report["phases"].append({"condition": "corrected_new_session", **summary(corrected, "Orin Keel")})
        store.forget(replacement["id"])
        forgotten = call(prompt)
        report["phases"].append({"condition": "forgotten_new_session", **summary(forgotten, "UNKNOWN")})
        store.remember("user", "salary", 42000, "My salary is 42000.")
        care = store.remember("user", "availability", "Must have flexible hours to care for a relative for four months.", "I need flexible hours while caring for a relative.")
        if controls:
            baseline = summary(call(CARE_PROMPT, mcp=False))
            baseline["passed"] = baseline["passed"] and not baseline["tool_calls"]
            report["controls"].append({"condition": "job_offer_without_memory", **baseline})
        causal = call(CARE_PROMPT)
        packets = [c.get("result", {}).get("structured_content") for c in causal["tool_calls"]
                   if c.get("tool") == "memory_context" and c.get("status") == "completed"]
        packets = [p for p in packets if isinstance(p, dict) and "trace" in p]
        selected = [p["trace"]["selected"] for p in packets]
        causal_summary = summary(causal)
        review = currency_review(causal["answer"], packets[-1].get("context") if packets else None)
        causal_summary["currency_review"] = review
        causal_summary["passed"] = (causal_summary["passed"] and causal_summary["successful_context_tool_calls"] == 1
                                    and any(care["id"] in ids for ids in selected)
                                    and any(w in (causal["answer"] or "").lower() for w in ("flexib", "care"))
                                    and not review["review_required"])
        report["phases"].append({"condition": "cross_domain_constraint", **causal_summary})
        technical = call("Explica qué es una clave primaria en SQLite, en una frase y sin información personal ajena a la pregunta. No ejecutes comandos ni leas archivos.")
        technical_summary = summary(technical)
        technical_packets = [c.get("result", {}).get("structured_content") for c in technical["tool_calls"]
                             if c.get("tool") == "memory_context" and c.get("status") == "completed"]
        irrelevant = re.search(r"\b(42000|42,000|relative|familiar|flexible|Nyra|Orin)\b",
                               technical["answer"] or "", re.IGNORECASE)
        technical_summary["empty_projection_observed"] = bool(technical_packets) and all(
            isinstance(p, dict) and p.get("trace", {}).get("selected") == [] for p in technical_packets)
        technical_summary["passed"] = (technical_summary["passed"] and not irrelevant and
                                       all(isinstance(p, dict) and p.get("trace", {}).get("selected") == [] for p in technical_packets))
        report["phases"].append({"condition": "generic_question_discretion", **technical_summary})
        report["private_canary_exposed"] = any("PRIVATE_ORCHID_CANARY" in canonical(p) for p in report["phases"] + report["controls"])
        report["passed"] = all(p["passed"] for p in report["phases"] + report["controls"]) and not report["private_canary_exposed"]
        report["sessions"] = session_count
        return report
    finally:
        private.close()
        store.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", required=True)
    parser.add_argument("--codex", default=shutil.which("codex"))
    parser.add_argument("--provider", choices=("ollama", "chatgpt"), default="ollama")
    parser.add_argument("--model", help="Explicit model; required for ChatGPT mode.")
    parser.add_argument("--reasoning", choices=("none", "low", "medium", "high", "xhigh"))
    parser.add_argument("--controls", action="store_true", help="Add two same-prompt runs without the memory server.")
    args = parser.parse_args()
    if not args.codex:
        raise SystemExit("Codex CLI was not found; pass --codex with its absolute path.")
    if args.provider == "chatgpt" and not args.model:
        raise SystemExit("Pass the account's chosen model explicitly for subscription checks.")
    result = run(args.codex, args.workspace, provider=args.provider, model=args.model or MODEL,
                 reasoning=args.reasoning or ("high" if args.provider == "chatgpt" else "none"), controls=args.controls)
    print(canonical(result))
    raise SystemExit(0 if result["passed"] else 1)
