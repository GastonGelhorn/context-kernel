"""Opt-in native Codex check of the v0.4 flow with your ChatGPT subscription: conversation only.

Codex requires a person to review project hooks once in its own interactive `/hooks` prompt, and
keeps that trust for the same definitions. So this runner works on a prepared pilot directory:

    python3 -m tests.native_codex_v04_check --prepare /abs/pilot
    (open Codex once in /abs/pilot, trust the folder, review and trust the three hooks, quit)
    python3 -m tests.native_codex_v04_check --workspace /abs/pilot

Each run resets only that pilot's memory database (the hook definitions, and therefore their trust,
stay the same) and runs the five conversation phases shared with the Claude Code runner, each in a
fresh `codex exec` session using the model configured in your Codex CLI. Only the memory tools are
pre-approved, for these invocations only. API-key variables are removed from the environment, so a
run can only use your ChatGPT sign-in. Fictional prompts go to OpenAI and use your quota.
"""

import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import time

from context_kernel.adapters import configuration
from context_kernel.common import KernelError, canonical
from context_kernel.store import Store
from tests.native_claude_check import PHASES, evaluate, snapshot


CODEX = "/Applications/ChatGPT.app/Contents/Resources/codex-cli/CodexCLI.app/Contents/MacOS/codex"
APPROVED = ("memory_context", "memory_capture", "memory_undo", "memory_inventory", "memory_history",
            "memory_dependents", "memory_forget", "memory_revoke", "memory_confirm", "memory_reaffirm", "memory_policy")


def environment():
    env = os.environ.copy()
    for name in ("OPENAI_API_KEY", "CODEX_API_KEY", "CODEX_ACCESS_TOKEN", "OPENAI_BASE_URL"):
        env.pop(name, None)
    return env


def prepare(workspace, jev_command):
    workspace = Path(workspace).resolve()
    workspace.mkdir(parents=True, exist_ok=True)
    database = workspace / "memory.sqlite"
    hooks = configuration("codex", workspace, database, "pilot", strategy="jev", jev_command=jev_command)
    server = configuration("codex", workspace, database, "pilot", mode="mcp", strategy="jev", jev_command=jev_command)
    (workspace / ".codex").mkdir(exist_ok=True)
    (workspace / ".codex" / "hooks.json").write_text(json.dumps(hooks["config"], indent=2))
    (workspace / ".codex" / "config.toml").write_text(server["content"])
    Store(database, scope="pilot", create=True).close()
    return {"prepared": str(workspace), "next": f"Open Codex in {workspace}, trust the folder, review and trust the three "
                                                "context-kernel hooks in its prompt or /hooks, then quit and run --workspace."}


def trust_state(workspace, home=None):
    """Whether Codex recorded trust for this folder and its three hooks (read-only; nothing is changed).

    Without folder trust Codex does not load the project's .codex/config.toml, so the memory server is
    missing and per-invocation tool approvals describe a server with no command ("invalid transport")."""
    config = Path(home or Path.home()) / ".codex" / "config.toml"
    text = config.read_text(encoding="utf-8") if config.is_file() else ""
    hooks = str(Path(workspace) / ".codex" / "hooks.json")
    folder = f'[projects."{workspace}"]'
    folder_trusted = folder in text and 'trust_level = "trusted"' in text.split(folder, 1)[1].split("[", 1)[0]
    events = {event: f'[hooks.state."{hooks}:{event}:' in text for event in ("user_prompt_submit", "stop", "session_start")}
    return {"folder_trusted": folder_trusted, "hooks_reviewed": events}


def invoke(codex, workspace, prompt, model=None, disable=()):
    command = [codex, "exec", "--json", "--skip-git-repo-check", "--ephemeral", "--sandbox", "read-only",
               "--disable", "apps", "--disable", "multi_agent", "--disable", "shell_tool", "-c", 'web_search="disabled"',
               "-c", 'model_provider="openai"', "-c", 'forced_login_method="chatgpt"', "--cd", str(workspace)]
    if model:
        command.extend(["--model", model])
    for name in APPROVED:
        command.extend(["-c", f'mcp_servers.context-kernel.tools.{name}.approval_mode="approve"'])
    for name in disable:
        command.extend(["-c", f"mcp_servers.{name}.enabled=false"])
    command.append(prompt)
    started = time.perf_counter()
    try:
        result = subprocess.run(command, input="", text=True, capture_output=True, timeout=240, env=environment(), cwd=workspace)
    except subprocess.TimeoutExpired as exc:
        return {"exit_code": 124, "answer": None, "stderr": str(exc)[-500:], "duration_seconds": 240.0, "tool_calls": []}
    events = []
    for line in result.stdout.splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if isinstance(event, dict):
            events.append(event)
    items = [e["item"] for e in events if e.get("type") == "item.completed" and isinstance(e.get("item"), dict)]
    answers = [i.get("text") for i in items if i.get("type") == "agent_message"]
    calls = [{"tool": i.get("tool"), "status": i.get("status")} for i in items if i.get("type") == "mcp_tool_call"]
    return {"exit_code": result.returncode, "answer": answers[-1] if answers else None, "tool_calls": calls,
            "stderr": result.stderr.strip()[-800:] if result.returncode else "",
            "errors": [i.get("message") for i in items if i.get("type") == "error"][:3],
            "duration_seconds": round(time.perf_counter() - started, 3),
            "usage": [e.get("usage") for e in events if e.get("type") == "turn.completed"]}


def run(codex, workspace, model=None, disable=()):
    workspace = Path(workspace).resolve()
    if not (workspace / ".codex" / "hooks.json").is_file():
        raise KernelError("Prepare the pilot first with --prepare, then trust its hooks in Codex once.")
    trust = trust_state(workspace)
    if not trust["folder_trusted"] or not all(trust["hooks_reviewed"].values()):
        raise KernelError(f"Codex has not recorded trust for this pilot yet ({canonical(trust)}). Open Codex once in "
                          f"{workspace}, choose to trust the folder, review and trust the three context-kernel hooks, "
                          "quit, and run again.")
    status = subprocess.run([codex, "login", "status"], capture_output=True, text=True, timeout=15, env=environment())
    if status.returncode or "ChatGPT" not in status.stdout + status.stderr:
        raise KernelError("ChatGPT sign-in is required; this runner never falls back to an API key.")
    database = workspace / "memory.sqlite"
    for suffix in ("", "-journal"):
        Path(str(database) + suffix).unlink(missing_ok=True)
    Store(database, scope="pilot", create=True).close()
    report = {"client": "Codex CLI", "version": subprocess.run([codex, "--version"], capture_output=True, text=True).stdout.strip(),
              "model": model or "Codex CLI configured model", "provider": "chatgpt subscription", "api_keys_removed": True,
              "hook_trust": "owner-reviewed once in Codex", "memory_commands_typed": 0, "phases": [],
              "evaluation_scope": "one fictional scenario through the real hooks and MCP server; not a quality benchmark"}
    for name, prompt in PHASES:
        result = invoke(codex, workspace, prompt, model, disable)
        report["phases"].append(dict(result, name=name, state=snapshot(database)))
        if result["exit_code"]:
            report["aborted"] = name
            break
    checks = evaluate(report["phases"])
    report["checks"] = checks
    report["hooks_ran"] = all(p["state"]["sessions"] >= n + 1 for n, p in enumerate(report["phases"]))
    report["passed"] = bool(checks) and all(checks.values()) and report["hooks_ran"] and "aborted" not in report
    report["wall_seconds"] = round(sum(p["duration_seconds"] for p in report["phases"]), 3)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--prepare", help="Write the v0.4 hooks and MCP server into this pilot directory")
    parser.add_argument("--workspace", help="A prepared pilot whose hooks you already trusted in Codex")
    parser.add_argument("--codex", default=CODEX if Path(CODEX).is_file() else shutil.which("codex"))
    parser.add_argument("--model", help="Override the model configured in your Codex CLI")
    parser.add_argument("--disable-mcp", nargs="*", default=[], help="Other configured MCP servers to switch off for these runs")
    parser.add_argument("--jev-command", default="jev")
    args = parser.parse_args()
    try:
        if args.prepare:
            print(canonical(prepare(args.prepare, args.jev_command)))
            return 0
        if not args.workspace or not args.codex:
            raise KernelError("Pass --prepare DIR first, or --workspace DIR to run; Codex must be installed.")
        report = run(args.codex, args.workspace, args.model, args.disable_mcp)
    except KernelError as exc:
        print(canonical({"error": str(exc)}))
        return 1
    print(canonical(report))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
