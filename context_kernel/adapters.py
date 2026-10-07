"""Host hooks (prompt, stop, session start) and explicit configuration generation."""

from pathlib import Path
import json
import re
import shlex
import shutil
import sys

from .binding import ancestors
from .capture import (CONFIDENT_NONE, STANDING_LIMIT, describe_results, gate, only_questions, policy as capture_policy,
                      related_facts, segments)
from .turns import mask_secrets
from .common import KernelError, canonical, digest, text, timestamp_offset
from .inference import infer
from .language import fold
from .planner import NeedPlan, generic_question
from .protocol import parse_json
from . import repository
from .turns import choice_reply, close_turn, do_not_remember, forget_request, open_turn, trivial_continuation, undo_request


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


def standing_due(store, session):
    """Standing facts this session has not received yet: all of them on its first prompt, and again
    after the session restarts, is cleared or compacted (SessionStart resets the mark) or one changes."""
    pairs = store.standing()
    rows = sorted((r for r in store.records() if (r["entity_key"], r["predicate"]) in pairs),
                  key=lambda r: pairs[(r["entity_key"], r["predicate"])]["since"], reverse=True)[:STANDING_LIMIT]
    if not rows:
        return [], None
    mark = digest(sorted(r["id"] for r in rows))
    return ([], mark) if store.session_field(session, "standing_sent") == mark else ([r["id"] for r in rows], mark)


GATE_TASK_BAR = 0.85
KEEP_ALIVE_DEFAULT = "30m"


def judge_cold(judge):
    """A local model that is not in memory yet (JevCommand.loaded); a judge that cannot tell counts as warm."""
    probe = getattr(judge, "loaded", None)
    try:
        return callable(probe) and probe() is False
    except KernelError:
        return False


def keep_alive_duration(store):
    duration = str(capture_policy(store).get("keep_alive") or KEEP_ALIVE_DEFAULT)
    return None if duration.lower() in {"off", "0", "no", "false"} else duration


def refresh_keep_alive(judge, store):
    """Keep the local model loaded between turns (`keep_alive` in the scope's policy; "off" disables it).
    Ollama keeps a model five minutes by default, and a reload costs the next prompt seconds."""
    duration, extend = keep_alive_duration(store), getattr(judge, "keep_alive", None)
    if not duration or not callable(extend):
        return False
    try:
        return extend(duration, timeout=0.3)
    except KernelError:
        return False


def hook_response(event, workspace, store, compiler, strategy="rules", proposals=False, fail_closed=False,
                  client="claude", judge=None, deadline=None, warm=None):
    """UserPromptSubmit: open the turn, judge whether the message states facts, deliver context.

    The delivered packet always carries the turn token: write and delete tools accept only tokens
    the hook issued, in sessions bound to the same host process. `warm` starts the background warm-up
    when the local model is not loaded."""
    _check_event(event, workspace, "UserPromptSubmit")
    prompt = text(event.get("prompt") or event.get("prompt_text"), 16384)
    turn = open_turn(store, event, client, prompt)
    if turn.get("duplicate"):
        # Another install of the hooks is answering this prompt with the same memory: one packet is enough.
        return {}, None
    marker = {"token": turn["token"]}
    flags, messages, notice = [], [], None
    # Requests are read from what the user typed in this turn: pasted text or tool output that
    # mentions "forget" is not a request, and only a typed turn can ask for a deletion at all.
    authored = segments(prompt)[0]
    if do_not_remember(prompt):
        flags.append("do_not_remember")
        marker["capture"] = "off: the user asked not to keep this message"
    if turn["origin"] == "interactive" and (forget_request(authored) or undo_request(authored)):
        flags.append("forget_requested")
        if undo_request(authored):
            flags.append("undo_requested")
        marker["privacy"] = ("The user asked to forget something. If they named a fact, call memory_forget with that fact's id "
                             "(from the claims or memory_inventory); memory_undo only takes back the last thing saved. "
                             "Never say it is done unless the tool confirms it.")
    pending = _pending_captures(store, turn)
    if pending:
        marker["pending"] = pending
    if proposals:
        event_id = digest({"session": event.get("session_id"), "turn": event.get("turn_id"), "prompt": prompt})
        # No content-derived event hashes persist unless proposal capture is enabled.
        if not store.db.execute("SELECT 1 FROM processed_events WHERE scope=? AND event_id=?", (store.scope, event_id)).fetchone():
            notice = propose_command(store, prompt, event_id)
    gate_count = 0
    cold = judge is not None and judge_cold(judge)
    if cold:
        # Loading the model takes seconds the prompt should not wait for: this prompt is answered from keyword
        # matches, and a background warm-up loads the model for the next one.
        flags.append("judge_cold")
        if warm is not None:
            warm()
    if judge and not cold and turn["origin"] == "interactive" and not flags and not trivial_continuation(prompt) \
            and not choice_reply(prompt) and not generic_question(prompt, store.records()) and not only_questions(prompt) \
            and capture_policy(store)["auto_capture"] \
            and (deadline is None or deadline.allows(4)):
        try:
            judge.require_local(capture_policy(store)["allow_remote_judge"])
            result = gate(judge, prompt, deadline, capture_policy(store)["thresholds"].get("none_bar"))
            # A message often states a fact and asks something; the instruction score is not a veto.
            if not result["facts"] and result["calls"]:
                flags.append("gate_none")  # a capture the agent proposes anyway needs a near-certain reading
            if result["facts"]:
                gate_count = result["facts"]
                # "Implementa todos los puntos" read as one fact at P(none) 0.04; as a request for work (task
                # 0.91) it gets the capture hint but never hands the turn back.
                if result["p_none"] <= CONFIDENT_NONE and result.get("task", 0.0) < GATE_TASK_BAR:
                    flags.append("gate_confident")
                marker["capture"] = {"facts_stated": gate_count, "call": "memory_capture",
                                     "how": "Extract each as entity.predicate = value. If one changes a fact in related, pass its id as replaces."}
                related = related_facts(store, prompt)
                if related:
                    marker["capture"]["related"] = related
        except KernelError:
            flags.append("gate_unavailable")
    # Pending captures ride along in the marker; they do not justify judging an "ok" for relevance.
    quiet = (trivial_continuation(prompt) or choice_reply(prompt)) and not [f for f in flags if f != "judge_cold"] \
        and not store.pending_proposal_count()
    pinned, mark = standing_due(store, turn["session_id"])
    selection = "rules" if cold and strategy == "jev" else strategy
    if quiet:
        projection = compiler.project(prompt, plan=NeedPlan(strategy=selection), extra={"turn": marker}, pinned=pinned)
    else:
        projection = compiler.prepare(prompt, strategy=selection, extra={"turn": marker}, pinned=pinned)
    store.update_turn(turn["session_id"], turn["turn_key"], projection_id=projection.id,
                      delivered_ids=projection.trace["selected"], gate_count=gate_count, flags=flags)
    if judge is not None and not cold:
        refresh_keep_alive(judge, store)
    if pinned and set(pinned) <= set(projection.trace["selected"]):
        store.set_session_field(turn["session_id"], "standing_sent", mark)
    if notice:
        messages.append(f"Memory proposal {notice['id']} awaits owner approval; it is not a remembered fact.")
    # A packet that holds the best claims that fit is delivered even when critical ones were left out
    # (status insufficient_context, warning critical_budget_overflow); only an empty one is a failure.
    if projection.trace["status"] == "unavailable" or (projection.trace["status"] == "insufficient_context"
                                                        and not projection.trace["selected"]):
        store.mark_failed(projection.id)
        message = ("Memory context is unavailable (" + projection.trace["status"] +
                   "). This is not evidence that a fact does not exist; inspect the trace before retrying.")
        if fail_closed:
            raise KernelError(message)
        # Fail open: the prompt proceeds with the token only, and the host is told why.
        return envelope(with_requests(canonical({"type": "context_data", "turn": marker}), marker), messages + [message]), None
    warnings = [w for w in projection.trace["warnings"] if w not in {"jev_inventory_capped"}]
    if warnings:
        messages.append("Memory context warnings: " + ", ".join(warnings) + ".")
    return envelope(with_requests(projection.content, marker), messages), projection.id


def kernel_requests(marker):
    """What the memory system itself asks of the agent this turn, as plain sentences.

    They sit outside the JSON packet on purpose: the packet's claims are data the agent must not
    obey, and an agent that honours that rule (as Codex's model did) would otherwise also ignore a
    request to save what the user just said."""
    token = marker["token"]
    requests = []
    capture = marker.get("capture")
    if isinstance(capture, dict):
        related = " If one of them changes a fact listed in turn.capture.related, pass that fact's id as replaces." \
            if capture.get("related") else ""
        requests.append(f"The user's message appears to state {capture['facts_stated']} fact(s) worth remembering. If it does, "
                        f"call the memory_capture tool with token \"{token}\" before answering, one item per fact (entity, "
                        f"predicate, value, quote: the user's exact words that state it, cues: a few words a later "
                        f"question would use when it matters), using only what the user said." + related +
                        " If it states nothing worth keeping, do nothing and do not mention this request. When something "
                        "is saved, add the tool's receipt in one line.")
    if marker.get("pending"):
        requests.append("Facts from an earlier message were not saved yet; memory_capture accepts that message's token "
                        "from turn.pending.")
    if marker.get("privacy"):
        requests.append(marker["privacy"] + f" Use token \"{token}\".")
    if isinstance(marker.get("capture"), str):
        requests.append("The user asked not to keep this message: do not call memory_capture for it.")
    return requests


def with_requests(content, marker):
    requests = kernel_requests(marker)
    if not requests:
        return content
    return "Context Kernel, the memory system the user installed, asks: " + " ".join(requests) + "\n" + content


def packet_of(context):
    """The JSON packet inside a hook's additionalContext, after any plain-text requests."""
    return json.loads(context[context.index("{"):]) if context else None


def nudge_reason(turn):
    return (f"Before finishing: the user's last message stated facts worth remembering and nothing was saved. Call "
            f"memory_capture with token \"{turn['token']}\" for what the user stated there (only what they said), then "
            "reply with the tool's receipt in one line and nothing else.")


def stop_response(event, workspace, store, judge=None, deadline=None):
    """Stop: tell the user in one line what memory did, and infer what a recommendation rested on.

    When the user's message confidently stated facts, the agent has the memory tools, and nothing
    was saved, the turn is handed back to the agent once (`decision: block`; both hosts continue,
    Codex with the reason as a new prompt). The capture must use the original turn's token, so it is
    validated against the user's words, and the original reply is kept for the inference that runs
    after the capture. A continuation never nudges again."""
    _check_event(event, workspace, "Stop")
    session = str(event.get("session_id") or "unknown-session")
    reply = event.get("last_assistant_message") or ""
    if event.get("stop_hook_active"):
        # Codex opened a new turn for the continuation; Claude continues the same one. Either way the
        # work left is the nudged turn's receipt and inference.
        turn = next((t for t in store.recent_turns(session, 4)
                     if "capture_nudged" in t["flags"] and "nudge_done" not in t["flags"]), None)
        close_turn(store, event)
        if not turn:
            return {}
        store.update_turn(session, turn["turn_key"], flags=turn["flags"] + ["nudge_done"])
        turn = store.turn(session, turn["turn_key"])
        return _report(store, judge, deadline, turn, turn["reply_excerpt"] or reply, nudged=True)
    turn = close_turn(store, event)
    if not turn or turn.get("already_closed"):
        return {}  # nothing open, or another install of the hooks already reported this turn
    tools = store.tools_available(session)
    missing = turn["gate_count"] - len(turn["captured_ids"])
    if not tools and turn["gate_count"]:
        # Nobody could act on the capture request: not a miss, and the excerpt goes now.
        store.update_turn(session, turn["turn_key"], gate_count=0, prompt_excerpt=None, flags=turn["flags"] + ["no_tools"])
        turn = store.turn(session, turn["turn_key"])
        missing = 0
    # A capture the kernel refused is an answer, not a miss: asking again would get the same refusal.
    attempted = bool(store.captures_for_turn(session, turn["turn_key"]))
    if tools and missing > 0 and not turn["captured_ids"] and not attempted and turn["origin"] == "interactive" \
            and "gate_confident" in turn["flags"] and "capture_nudged" not in turn["flags"]:
        store.update_turn(session, turn["turn_key"], flags=turn["flags"] + ["capture_nudged"],
                          reply_excerpt=mask_secrets(reply)[:4000])
        return {"decision": "block", "reason": nudge_reason(turn)}
    return _report(store, judge, deadline, turn, reply)


def _report(store, judge, deadline, turn, reply, nudged=False):
    # What a background pass over the repository learned when this session started, said once.
    notice = store.pop_session_notice(turn["session_id"])
    notes = [notice] if notice else []
    results = store.captures_for_turn(turn["session_id"], turn["turn_key"])
    rows = {r["id"]: r for r in store.records(history=True)}
    described = [dict(status=r["outcome"], entity=rows[r["statement_id"]]["entity_key"], predicate=rows[r["statement_id"]]["predicate"])
                 for r in results if r["statement_id"] in rows]
    # What was not saved, by the label it was proposed under (no statement exists for it).
    described += [dict(status="rejected", reason=r["reason"], entity=r["label"].split(".", 1)[0], predicate=r["label"].split(".", 1)[1])
                  for r in results if r["outcome"] == "rejected" and r.get("label") and "." in r["label"]]
    described += [dict(status="needs_confirmation", entity=p["payload"].get("entity") or "", predicate=p["payload"].get("predicate") or "a confirmed fact")
                  for p in store.proposals() if p["status"] == "pending" and p["recorded_at"] >= turn["opened_at"]]
    line = describe_results(described)
    if line:
        notes.append(line)
    missing = turn["gate_count"] - len(turn["captured_ids"])
    if missing > 0 and not turn["captured_ids"] and not results and turn["origin"] == "interactive":
        # The gate's count is an estimate; the user hears about it only when nothing was even tried
        # (a refusal is already listed above, with its reason).
        notes.append("Memory: something in your message looked worth remembering but was not saved.")
    if missing <= 0 and turn["prompt_excerpt"]:
        # Nothing left to validate against this message: its excerpt goes now, not at expiry.
        store.update_turn(turn["session_id"], turn["turn_key"], prompt_excerpt=None)
    if turn["reply_excerpt"] and nudged:
        store.update_turn(turn["session_id"], turn["turn_key"], reply_excerpt=None)
    if "forget_requested" in turn["flags"] and not store.operations_since(turn["opened_at"], ("forget", "revoke", "undo")):
        notes.append("Memory: nothing was forgotten in that turn.")
    if judge:
        policy = capture_policy(store)
        inferred = infer(store, judge, store.turn(turn["session_id"], turn["turn_key"]) or turn, reply,
                         policy["allow_remote_judge"], deadline)
        if inferred.get("linked"):
            notes.append(f"Memory: linked the recommendation to {inferred['linked']} fact(s) it relied on.")
    if notes:
        # Kept on the turn so a surface the hook's message does not reach (the desktop app) can show it.
        store.update_turn(turn["session_id"], turn["turn_key"], notes=" ".join(notes))
    return {"systemMessage": " ".join(notes)} if notes else {}


def activity(store, session_id):
    """What memory did in this session's latest closed turn, for a status band: the line the Stop
    hook said, what that turn saved (so it can be undone), and what waits for the user."""
    turn = next((t for t in store.recent_turns(session_id, 4) if t["closed_at"]), None)
    saved = [r["statement_id"] for r in store.captures_for_turn(session_id, turn["turn_key"])
             if r["outcome"] in {"captured", "quarantined"} and r["statement_id"]] if turn else []
    current = {r["id"] for r in store.records(quarantined=True)}
    return {"turn": turn["turn_key"] if turn else None, "closed_at": turn["closed_at"] if turn else None,
            "line": (turn.get("notes") or "") if turn else "",
            "undo": [i for i in saved if i in current],
            "pending": sum(1 for p in store.proposals() if p["status"] == "pending"),
            "held": sum(1 for r in store.records(quarantined=True) if r["trust"] == "quarantined")}


def session_start_response(event, workspace, store, client="claude", learn=None, warm=None):
    """SessionStart: bind this session to its host process, make the next prompt carry the standing
    facts, surface captures nobody confirmed, and start one background pass (`warm(cwd, session, repo)`)
    that loads the local model, checks the judge's calibration when it changed, and in a git repository
    reads its decisions. The pass runs git and returns at once when nothing changed; this hook neither
    runs git nor waits. `learn(cwd, session)` is the older repository-only callback."""
    _check_event(event, workspace, "SessionStart")
    session = str(event.get("session_id") or "unknown-session")
    store.register_session(session, client, ancestors(depth=2))
    # Started, resumed, cleared or compacted: whatever the session was told before may be gone.
    store.set_session_field(session, "standing_sent", None)
    standing = store.standing()
    old = [r for r in store.records(quarantined=True) if r["trust"] != "confirmed" and r["source_kind"] != "repository"
           and (r["entity_key"], r["predicate"]) not in standing
           and (r.get("last_confirmed_at") or r["recorded_at"]) <= timestamp_offset(store.clock(), -90 * 86400)]
    in_repository = capture_policy(store).get("repository", True) and repository.inside_repository(event["cwd"])
    if warm is not None:
        warm(event["cwd"], session, in_repository)
    elif learn is not None and in_repository:
        learn(event["cwd"], session)
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
    start = {"type": "command", "command": command("session-start", judge_options), "timeout": 10}
    if client == "codex":
        # The packet stays within the kernel's 2 KiB budget; the plain-text requests before it add up to ~700 bytes.
        prompt["additionalContextLimit"] = 3072
    hooks = {"UserPromptSubmit": [{"hooks": [prompt]}], "Stop": [{"hooks": [stop]}], "SessionStart": [{"hooks": [start]}]}
    return {"destination": ".codex/hooks.json" if client == "codex" else ".claude/settings.local.json",
            "config": {"hooks": hooks}}
