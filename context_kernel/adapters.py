"""Host hooks (prompt, stop, session start) and explicit configuration generation."""

from pathlib import Path
import json
import re
import shlex
import shutil
import sys

from .binding import ancestors
from .capture import describe_results, gate, policy as capture_policy
from .common import KernelError, canonical, digest, text, timestamp_offset
from .inference import infer
from .language import fold
from .planner import NeedPlan
from .protocol import parse_json
from .turns import close_turn, do_not_remember, forget_request, open_turn, trivial_continuation


class HookBlock(KernelError):
    """The prompt must not proceed as if the kernel had acted on it."""


COMMAND = re.compile(r'^(Remember|Correct|Recuerda|Recordar|Corrige|Corregir): ([a-zA-Z0-9_-]+)\.([a-zA-Z0-9_-]+) = (.+)$', re.IGNORECASE)


def propose_command(store, prompt, event_id=None):
    """A whole-message grammar creates proposals, never accepted facts."""
    references = {"it finally arrived.": "arrived", "it has not arrived.": "not_arrived", "i returned it.": "returned",
                  "al final llego.": "arrived", "todavia no llego.": "not_arrived", "aun no llego.": "not_arrived",
                  "lo devolvi.": "returned"}
    if fold(prompt.strip()) in references:
        event = references[fold(prompt.strip())]
        entity = store.resolve_pending_delivery(event)
        return store.propose("transition", {"entity": entity, "event": event, "evidence": prompt}, event_id)
    transitions = ((r"I ordered ([a-zA-Z0-9_-]+)\.", "ordered"),
                   (r"([a-zA-Z0-9_-]+) has not arrived\.", "not_arrived"),
                   (r"([a-zA-Z0-9_-]+) arrived\.", "arrived"),
                   (r"I returned ([a-zA-Z0-9_-]+)\.", "returned"),
                   (r"Pedi ([a-zA-Z0-9_-]+)\.", "ordered"),
                   (r"([a-zA-Z0-9_-]+) no llego\.", "not_arrived"),
                   (r"([a-zA-Z0-9_-]+) llego\.", "arrived"),
                   (r"Devolvi ([a-zA-Z0-9_-]+)\.", "returned"))
    for grammar, event in transitions:
        match = re.fullmatch(grammar, fold(prompt.strip()), re.IGNORECASE)
        if match:
            return store.propose("transition", {"entity": match[1].lower(), "event": event, "evidence": prompt}, event_id)
    match = COMMAND.fullmatch(prompt.strip())
    if not match:
        return None
    operation, entity, predicate, raw = match.groups()
    value = parse_json(raw)
    payload = {"value": value, "evidence": prompt}
    if operation.casefold() in {"remember", "recuerda", "recordar"}:
        payload.update(entity=entity.lower(), predicate=predicate.lower())
        return store.propose("remember", payload, event_id)
    matches = [r for r in store.records() if r["entity_key"] == entity.lower() and r["predicate"] == predicate.lower()]
    if len(matches) != 1:
        raise KernelError("Correction needs exactly one current target. Use the owner CLI to resolve it.")
    payload["target_id"] = matches[0]["id"]
    return store.propose("correct", payload, event_id)


def _check_event(event, workspace, expected):
    name = event.get("hook_event_name", expected)
    if name != expected:
        raise KernelError("Unsupported hook event.")
    cwd = event.get("cwd")
    if not isinstance(cwd, str) or not Path(cwd).is_absolute():
        raise KernelError("Hook workspace is missing or invalid.")
    if not Path(cwd).resolve().is_relative_to(Path(workspace).resolve()):
        raise KernelError("Hook workspace is outside the configured boundary.")


def _pending_captures(store, turn):
    """Earlier turns of this session whose stated facts were not all captured and whose excerpt
    can still validate a late capture."""
    pending = []
    for earlier in store.recent_turns(turn["session_id"], 3)[1:]:
        missing = earlier["gate_count"] - len(earlier["captured_ids"])
        if earlier["origin"] == "interactive" and earlier["prompt_excerpt"] and missing > 0 \
                and earlier["expires_at"] > store.clock():
            pending.append({"token": earlier["token"], "facts_not_captured": missing})
    return pending


def hook_response(event, workspace, store, compiler, strategy="rules", proposals=False, fail_closed=False,
                  client="claude", judge=None, deadline=None):
    """UserPromptSubmit: open the turn, judge whether the message states facts, deliver context.

    The delivered packet always carries the turn token: write and delete tools accept only tokens
    the hook issued, in sessions bound to the same host process."""
    _check_event(event, workspace, "UserPromptSubmit")
    prompt = text(event.get("prompt") or event.get("prompt_text"), 16384)
    turn = open_turn(store, event, client, prompt)
    marker = {"token": turn["token"]}
    flags, messages, notice = [], [], None
    if do_not_remember(prompt):
        flags.append("do_not_remember")
        marker["capture"] = "off: the user asked not to keep this message"
    if forget_request(prompt):
        flags.append("forget_requested")
        marker["privacy"] = "The user asked to forget or undo something: use memory_undo or memory_forget with this token; never say it is done unless the tool confirms it."
    pending = _pending_captures(store, turn)
    if pending:
        marker["pending"] = pending
    if proposals:
        event_id = digest({"session": event.get("session_id"), "turn": event.get("turn_id"), "prompt": prompt})
        # No content-derived event hashes persist unless proposal capture is enabled.
        if not store.db.execute("SELECT 1 FROM processed_events WHERE scope=? AND event_id=?", (store.scope, event_id)).fetchone():
            notice = propose_command(store, prompt, event_id)
    gate_count = 0
    if judge and turn["origin"] == "interactive" and "do_not_remember" not in flags and not trivial_continuation(prompt) \
            and capture_policy(store)["auto_capture"] and (deadline is None or deadline.allows(4)):
        try:
            judge.require_local(capture_policy(store)["allow_remote_judge"])
            result = gate(judge, prompt, deadline)
            if result["facts"] and result["instruction"] < 0.8:
                gate_count = result["facts"]
                marker["capture"] = {"facts_stated": gate_count, "call": "memory_capture",
                                     "how": "Extract each as entity.predicate = value, reusing keys already in memory."}
        except KernelError:
            flags.append("gate_unavailable")
    quiet = trivial_continuation(prompt) and not pending and not flags and not store.pending_proposal_count()
    if quiet:
        projection = compiler.project(prompt, plan=NeedPlan(strategy=strategy), extra={"turn": marker})
    else:
        projection = compiler.prepare(prompt, strategy=strategy, extra={"turn": marker})
    store.update_turn(turn["session_id"], turn["turn_key"], projection_id=projection.id,
                      delivered_ids=projection.trace["selected"], gate_count=gate_count, flags=flags)
    if notice:
        messages.append(f"Memory proposal {notice['id']} awaits owner approval; it is not a remembered fact.")
    if projection.trace["status"] in {"unavailable", "insufficient_context"}:
        store.mark_failed(projection.id)
        message = ("Memory context is unavailable (" + projection.trace["status"] +
                   "). This is not evidence that a fact does not exist; inspect the trace before retrying.")
        if fail_closed:
            raise KernelError(message)
        # Fail open: the prompt proceeds with the token only, and the host is told why.
        return envelope(canonical({"type": "context_data", "turn": marker}), messages + [message]), None
    warnings = [w for w in projection.trace["warnings"] if w not in {"jev_inventory_capped"}]
    if warnings:
        messages.append("Memory context warnings: " + ", ".join(warnings) + ".")
    return envelope(projection.content, messages), projection.id


def stop_response(event, workspace, store, judge=None, deadline=None):
    """Stop: close the turn, tell the user in one line what memory did, infer what a recommendation
    rested on. Writes nothing the user said; capture happened through the tool during the turn."""
    _check_event(event, workspace, "Stop")
    if event.get("stop_hook_active"):
        return {}
    turn = close_turn(store, event)
    if not turn:
        return {}
    notes = []
    results = store.captures_for_turn(turn["session_id"], turn["turn_key"])
    rows = {r["id"]: r for r in store.records(history=True)}
    described = [dict(status=r["outcome"], entity=rows[r["statement_id"]]["entity_key"], predicate=rows[r["statement_id"]]["predicate"])
                 for r in results if r["statement_id"] in rows]
    described += [dict(status="needs_confirmation", entity=p["payload"].get("entity") or "", predicate=p["payload"].get("predicate") or "a confirmed fact")
                  for p in store.proposals() if p["status"] == "pending" and p["recorded_at"] >= turn["opened_at"]]
    line = describe_results(described)
    if line:
        notes.append(line)
    missing = turn["gate_count"] - len(turn["captured_ids"])
    if missing > 0 and turn["origin"] == "interactive":
        notes.append(f"Memory: {missing} fact(s) from your message are not saved yet.")
    elif turn["prompt_excerpt"]:
        store.update_turn(turn["session_id"], turn["turn_key"], prompt_excerpt=None)
    if "forget_requested" in turn["flags"] and not store.operations_since(turn["opened_at"], ("forget", "revoke", "undo")):
        notes.append("Memory: nothing was forgotten in that turn.")
    if judge:
        policy = capture_policy(store)
        inferred = infer(store, judge, turn, event.get("last_assistant_message") or "", policy["allow_remote_judge"], deadline)
        if inferred.get("linked"):
            notes.append(f"Memory: linked the recommendation to {inferred['linked']} fact(s) it relied on.")
    return {"systemMessage": " ".join(notes)} if notes else {}


def session_start_response(event, workspace, store, client="claude"):
    """SessionStart: bind this session to its host process; surface captures nobody confirmed."""
    _check_event(event, workspace, "SessionStart")
    session = str(event.get("session_id") or "unknown-session")
    store.register_session(session, client, ancestors(depth=2))
    old = [r for r in store.records(quarantined=True) if r["trust"] != "confirmed"
           and (r.get("last_confirmed_at") or r["recorded_at"]) <= timestamp_offset(store.clock(), -90 * 86400)]
    if old:
        return {"systemMessage": f"Memory: {len(old)} remembered fact(s) were captured over 90 days ago and never confirmed. "
                                 "Ask \"what do you remember about me?\" to review them."}
    return {}


def envelope(context, messages=(), event="UserPromptSubmit"):
    """The same hookSpecificOutput shape is accepted by Codex and Claude Code."""
    result = {"hookSpecificOutput": {"hookEventName": event, "additionalContext": context}}
    if messages:
        result["systemMessage"] = " ".join(messages)
    return result


def block(reason):
    """Claude Code reads `decision`; Codex reads `continue`. Both are sent."""
    return {"decision": "block", "reason": reason, "continue": False, "stopReason": reason}


def _jev_options(jev_command):
    # Hosts run hooks and servers with a minimal PATH; pin the executable the owner has now.
    resolved = shutil.which(jev_command) if not Path(jev_command).is_absolute() else jev_command
    if not resolved or not Path(resolved).is_file():
        raise KernelError("jev was not found on this PATH; install jevmate or pass --jev-command with an absolute path.")
    return ["--jev-command", str(Path(resolved).resolve())]


def configuration(client, workspace, db, scope, python=None, mode="hook", proposals=False, strategy="rules",
                  fail_closed=False, jev_command="jev"):
    root = str(Path(workspace).resolve())
    checkout = str(Path(__file__).resolve().parent.parent)
    executable = python or sys.executable
    args = ["-m", "context_kernel", "--db", str(Path(db).resolve()), "--scope", scope]
    judge_options = []
    if strategy == "jev" or mode == "hook":
        try:
            judge_options = _jev_options(jev_command)
        except KernelError:
            if strategy == "jev":
                raise
    strategy_options = (["--strategy", strategy] if strategy != "rules" else []) + judge_options
    if mode == "mcp" or client == "antigravity":
        server = {"command": executable, "args": args + ["serve"] + strategy_options, "env": {"PYTHONPATH": checkout}}
        if client == "codex":
            toml = "[mcp_servers.context-kernel]\ncommand = " + json.dumps(executable) + "\nargs = " + json.dumps(server["args"]) + "\n"
            toml += "\n[mcp_servers.context-kernel.env]\nPYTHONPATH = " + json.dumps(checkout) + "\n"
            return {"destination": ".codex/config.toml", "content": toml}
        return {"destination": ".mcp.json" if client == "claude" else ".agents/mcp_config.json",
                "config": {"mcpServers": {"context-kernel": server}}}

    def command(event, extra=()):
        return shlex.join(["env", "PYTHONPATH=" + checkout, executable] + args +
                          ["hook", "--client", client, "--workspace", root, "--event", event] + list(extra))

    prompt_options = (["--proposals"] if proposals else []) + (["--fail-closed"] if fail_closed else []) + strategy_options
    prompt = {"type": "command", "command": command("prompt", prompt_options), "timeout": 15}
    stop = {"type": "command", "command": command("stop", judge_options), "timeout": 20}
    start = {"type": "command", "command": command("session-start"), "timeout": 10}
    if client == "codex":
        prompt["additionalContextLimit"] = 2048
    hooks = {"UserPromptSubmit": [{"hooks": [prompt]}], "Stop": [{"hooks": [stop]}], "SessionStart": [{"hooks": [start]}]}
    return {"destination": ".codex/hooks.json" if client == "codex" else ".claude/settings.local.json",
            "config": {"hooks": hooks}}
