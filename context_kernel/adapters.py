"""Read-only prompt hooks and explicit configuration generation."""

from pathlib import Path
import json
import re
import shlex
import sys

from .common import KernelError, digest, text
from .protocol import parse_json


COMMAND = re.compile(r'^(Remember|Correct): ([a-zA-Z0-9_-]+)\.([a-zA-Z0-9_-]+) = (.+)$')


def propose_command(store, prompt, event_id=None):
    """A whole-message grammar creates proposals, never accepted facts."""
    if prompt.strip().lower() in {"it finally arrived.", "it has not arrived.", "i returned it."}:
        event = {"it finally arrived.": "arrived", "it has not arrived.": "not_arrived", "i returned it.": "returned"}[prompt.strip().lower()]
        entity = store.resolve_pending_delivery(event)
        return store.propose("transition", {"entity": entity, "event": event, "evidence": prompt}, event_id)
    transitions = ((r"I ordered ([a-zA-Z0-9_-]+)\.", "ordered"),
                   (r"([a-zA-Z0-9_-]+) has not arrived\.", "not_arrived"),
                   (r"([a-zA-Z0-9_-]+) arrived\.", "arrived"),
                   (r"I returned ([a-zA-Z0-9_-]+)\.", "returned"))
    for grammar, event in transitions:
        match = re.fullmatch(grammar, prompt.strip(), re.IGNORECASE)
        if match:
            return store.propose("transition", {"entity": match[1].lower(), "event": event, "evidence": prompt}, event_id)
    match = COMMAND.fullmatch(prompt.strip())
    if not match:
        return None
    operation, entity, predicate, raw = match.groups()
    value = parse_json(raw)
    payload = {"value": value, "evidence": prompt}
    if operation == "Remember":
        payload.update(entity=entity.lower(), predicate=predicate.lower())
        return store.propose("remember", payload, event_id)
    matches = [r for r in store.records() if r["entity_key"] == entity.lower() and r["predicate"] == predicate.lower()]
    if len(matches) != 1:
        raise KernelError("Correction needs exactly one current target. Use the owner CLI to resolve it.")
    payload["target_id"] = matches[0]["id"]
    return store.propose("correct", payload, event_id)


def hook_response(event, workspace, store, compiler, strategy="rules", proposals=False):
    if event.get("hook_event_name", "UserPromptSubmit") != "UserPromptSubmit":
        raise KernelError("Unsupported hook event.")
    cwd = event.get("cwd")
    if not isinstance(cwd, str) or not Path(cwd).is_absolute():
        raise KernelError("Hook workspace is missing or invalid.")
    root = Path(workspace).resolve()
    if not Path(cwd).resolve().is_relative_to(root):
        raise KernelError("Hook workspace is outside the configured boundary.")
    prompt = text(event.get("prompt"), 16384)
    notice = None
    if re.fullmatch(r"(?:Forget|Revoke): .+", prompt, re.IGNORECASE):
        raise KernelError("Privacy changes require the owner CLI: memory forget or memory revoke. This hook did not apply the request.")
    if proposals:
        event_id = digest({"session": event.get("session_id"), "turn": event.get("turn_id"), "prompt": prompt})
        # No content-derived event hashes persist unless proposal capture is enabled.
        if not store.db.execute("SELECT 1 FROM processed_events WHERE scope=? AND event_id=?", (store.scope, event_id)).fetchone():
            notice = propose_command(store, prompt, event_id)
    projection = compiler.project(prompt, strategy=strategy)
    try:
        compiler.revalidate(projection)
    except KernelError:
        store.mark_failed(projection.id)
        raise
    result = {"hookSpecificOutput": {"hookEventName": "UserPromptSubmit",
              "additionalContext": projection.content}}
    messages = []
    if notice:
        messages.append(f"Memory proposal {notice['id']} awaits owner approval; it is not a remembered fact.")
    if projection.trace["warnings"]:
        messages.append("Memory context warnings: " + ", ".join(projection.trace["warnings"]) + ".")
    if messages:
        result["systemMessage"] = " ".join(messages)
    return result, projection.id


def configuration(client, workspace, db, scope, python=None, mode="hook", proposals=False):
    root = str(Path(workspace).resolve())
    checkout = str(Path(__file__).resolve().parent.parent)
    executable = python or sys.executable
    args = ["-m", "context_kernel", "--db", str(Path(db).resolve()), "--scope", scope]
    if mode == "mcp" or client == "antigravity":
        server = {"command": executable, "args": args + ["serve"], "env": {"PYTHONPATH": checkout}}
        if client == "codex":
            toml = "[mcp_servers.context-kernel]\ncommand = " + json.dumps(executable) + "\nargs = " + json.dumps(server["args"]) + "\n"
            toml += "\n[mcp_servers.context-kernel.env]\nPYTHONPATH = " + json.dumps(checkout) + "\n"
            return {"destination": ".codex/config.toml", "content": toml}
        return {"destination": ".mcp.json" if client == "claude" else ".agents/mcp_config.json",
                "config": {"mcpServers": {"context-kernel": server}}}
    command = shlex.join(["env", "PYTHONPATH=" + checkout, executable] + args +
                         ["hook", "--client", client, "--workspace", root] + (["--proposals"] if proposals else []))
    hook = {"type": "command", "command": command, "timeout": 15}
    if client == "codex":
        hook["additionalContextLimit"] = 2048
    return {"destination": ".codex/hooks.json" if client == "codex" else ".claude/settings.local.json",
            "config": {"hooks": {"UserPromptSubmit": [{"hooks": [hook]}]}}}
