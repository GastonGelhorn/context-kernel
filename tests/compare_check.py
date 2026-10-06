"""Opt-in comparison for plan phase 5: jevmate alone, the kernel alone, both, and neither.

Every variant runs the same continuity script in its own disposable git repository: facts are
taught, corrected and changed across fresh headless `claude -p` sessions, the repository's decision
record is superseded, and later sessions are asked to use what came before. Nobody types a memory
command. Variants differ only in what is wired into the pilot:

  none     no kernel, jevmate plugin off
  jevmate  no kernel, jevmate plugin on
  kernel   kernel hooks and MCP server (selection by rules; jev still judges captures), jevmate off
  both     kernel with jev selection, jevmate plugin on

Read, Glob and Grep are allowed in every variant, so a variant without memory can still read the
repository the way a real session would. Other synced plugins, claude.ai connectors and user MCP
servers are switched off in all of them so the variants carry the same baseline prompt.

Measured per variant: recall checks a user would otherwise have to re-explain, stale
recommendations flagged, forgotten facts kept out, wall time per session, kernel hook time per
event, tokens and cost, tool calls, and what is left in the kernel waiting for the owner.
It uses the owner's Claude Code sign-in and quota with fictional prompts.
"""

import argparse
import json
import os
import random
import shlex
import shutil
import statistics
import subprocess
import sys
import time
from pathlib import Path

from context_kernel.adapters import configuration
from context_kernel.common import KernelError, canonical
from context_kernel.store import Store
from tests.native_claude_check import MEMORY_TOOLS
from tests.native_v06_check import git, record, wait_for_pass


ROOT = Path(__file__).resolve().parent.parent
TIMER = Path(__file__).resolve().parent / "hook_timer.py"
TOOLS_OFF = "Bash,Edit,Write,MultiEdit,NotebookEdit,WebFetch,WebSearch,Agent,Task"
READ_TOOLS = "Read,Glob,Grep"
JEV_TOOLS = ",".join("mcp__plugin_jevmate_jev__" + name for name in
                     ("ask", "cluster", "decide", "diff", "rank", "session", "sift", "tests"))
OTHER_PLUGINS = ("cowork-plugin-management@synced", "design@synced", "data@synced", "engineering@synced",
                 "render@synced", "productivity@synced", "claude-subconscious@claude-subconscious")
VARIANTS = {
    "none": {"kernel": None, "jevmate": False},
    "jevmate": {"kernel": None, "jevmate": True},
    "kernel": {"kernel": "rules", "jevmate": False},
    "both": {"kernel": "jev", "jevmate": True},
}

# (task, kind, prompt, check). A check holds `any` (each inner list: at least one must appear) and
# `none` (none may appear), matched case-insensitively against the answer.
SCRIPT = [
    ("deploy", "teach", "Contexto del proyecto: desplegamos con Fly.io y la app de staging se llama checkout-stg. "
                        "Responde solo ok.", None),
    ("package_manager", "teach", "En este proyecto usa pnpm, nunca npm. Responde solo ok.", None),
    ("meeting", "teach", "La reunión de release es los jueves a las 10. Responde solo ok.", None),
    ("queue", "recommend", "¿Qué cola debería usar el nuevo worker de email de este proyecto? Recomienda una en una frase.", None),
    ("deadline", "recommend", "Tenemos tres meses para entregar el proyecto checkout. ¿Conviene reescribir su módulo de "
                              "pagos? Responde en una frase.", None),
    ("meeting", "correct", "Corrección: la reunión de release pasó a los martes. Responde solo ok.", None),
    ("deadline", "change", "Actualización: el plazo de checkout cambió, ahora tenemos tres semanas. Responde solo ok.", None),
    ("legacy", "teach", "La base de datos antigua se llama orders-legacy y está en Hetzner. Responde solo ok.", None),
    ("legacy", "forget", "Olvida lo de la base de datos antigua, ya no existe. Responde solo ok.", None),
    ("queue", "repo_change", None, None),
    # Checks name what a right answer must contain; v0.7's first run passed "I can't give you the command"
    # because it mentioned checkout-stg in passing.
    ("deploy", "recall", "Dame el comando para desplegar staging. Solo el comando.",
     {"any": [["fly deploy"], ["checkout-stg"]], "none": ["no puedo", "no sé", "no encuentro"]}),
    ("package_manager", "recall", "Dame el comando de una línea para añadir zod a este proyecto. Solo el comando.",
     {"any": [["pnpm"]], "none": ["npm install", "npm i "]}),
    ("meeting", "recall", "¿Qué día es la reunión de release? Responde con una palabra.", {"any": [["martes"]], "none": ["jueves"]}),
    ("deadline", "stale", "¿Seguimos adelante con lo que decidimos sobre el módulo de pagos de checkout? Una frase.",
     {"any": [["tres semanas", "3 semanas"]], "none": ["ninguna decisión", "no encuentro ninguna"]}),
    ("queue", "stale", "¿El worker de email debería seguir usando la cola que recomendaste antes? Una frase.",
     {"any": [["redis"]]}),
    # Names both halves of what was forgotten; "base de datos antigua" alone gets read as the superseded SQLite queue.
    ("legacy", "forgotten", "¿Cómo se llama la base de datos de pedidos antigua y en qué proveedor está alojada? Si no lo sabes, dilo.",
     {"none": ["orders-legacy", "hetzner"]}),
    ("noise", "generic", "Explica en una frase qué es una clave primaria en SQLite.", {"any": [["clave primaria", "primary key"]]}),
]
RECALL = {"recall"}
STALE = {"stale"}


def passes(check, answer):
    text = (answer or "").casefold()
    return all(any(term.casefold() in text for term in group) for group in check.get("any", [])) \
        and not any(term.casefold() in text for term in check.get("none", []))


def timed(command, log, event):
    return shlex.join([sys.executable, str(TIMER), str(log), event]) + " " + command


def prepare(root, variant, jev_command):
    """A git repository with one accepted decision record, wired for the variant."""
    git(root, "init", "-q", "-b", "main")
    record(root, 1, "Use SQLite for the job queue", "Accepted", "Jobs stay in one SQLite file next to the app.")
    git(root, "add", "docs")
    git(root, "commit", "-q", "-m", "Record the queue decision")
    spec = VARIANTS[variant]
    settings = {"enabledPlugins": {name: False for name in OTHER_PLUGINS}}
    settings["enabledPlugins"]["jevmate@gastongelhorn"] = spec["jevmate"]
    database, mcp = root.parent / (root.name + "-memory.sqlite"), {"mcpServers": {}}
    hook_log = root.parent / (root.name + "-hooks.jsonl")
    if spec["kernel"]:
        Store(database, scope="pilot", create=True).close()
        hooks = configuration("claude", root, database, "pilot", strategy=spec["kernel"], jev_command=jev_command)["config"]["hooks"]
        for event, groups in hooks.items():
            for group in groups:
                for hook in group["hooks"]:
                    hook["command"] = timed(hook["command"], hook_log, event)
        settings["hooks"] = hooks
        mcp = configuration("claude", root, database, "pilot", mode="mcp", strategy=spec["kernel"], jev_command=jev_command)["config"]
    (root / ".claude").mkdir()
    (root / ".claude" / "settings.local.json").write_text(json.dumps(settings, indent=2))
    mcp_path = root.parent / (root.name + "-mcp.json")
    mcp_path.write_text(json.dumps(mcp, indent=2))
    allowed = [READ_TOOLS] + ([MEMORY_TOOLS] if spec["kernel"] else []) + ([JEV_TOOLS] if spec["jevmate"] else [])
    return database if spec["kernel"] else None, mcp_path, hook_log, ",".join(allowed)


def invoke(claude, root, prompt, mcp_path, allowed, model):
    command = [claude, "-p", prompt, "--output-format", "stream-json", "--verbose",
               "--mcp-config", str(mcp_path), "--disallowedTools", TOOLS_OFF, "--allowedTools", allowed]
    if model:
        command += ["--model", model]
    # The pilot wires its own kernel; an installed plugin must not write fictional prompts into real memory.
    env = dict(os.environ, CONTEXT_KERNEL_OFF="1", ENABLE_CLAUDEAI_MCP_SERVERS="false")
    started = time.perf_counter()
    try:
        done = subprocess.run(command, cwd=root, stdin=subprocess.DEVNULL, text=True, capture_output=True, timeout=300, env=env)
    except subprocess.TimeoutExpired:
        return {"exit_code": 124, "answer": None, "seconds": 300.0}
    out = {"exit_code": done.returncode, "seconds": round(time.perf_counter() - started, 3), "tools": [], "hooks": []}
    for line in done.stdout.splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        kind, sub = event.get("type"), event.get("subtype")
        if kind == "system" and sub == "init":
            out["plugins"] = sorted(p["name"] for p in event.get("plugins", []) if p.get("path") != "builtin")
            out["mcp"] = sorted(m["name"] + ":" + m["status"] for m in event.get("mcp_servers", []))
        elif kind == "system" and sub == "hook_response":
            out["hooks"].append(event.get("hook_name") or event.get("hook_event"))
        elif kind == "assistant":
            out["tools"] += [c["name"] for c in event["message"].get("content", []) if c.get("type") == "tool_use"]
        elif kind == "result":
            usage = event.get("usage") or {}
            out.update(answer=event.get("result"), api_ms=event.get("duration_api_ms"), cost_usd=event.get("total_cost_usd"),
                       num_turns=event.get("num_turns"), output_tokens=usage.get("output_tokens"),
                       input_tokens=sum(usage.get(k) or 0 for k in ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens")))
    if out["exit_code"]:
        out["stderr"] = done.stderr[-800:]
    return out


def kernel_state(database):
    store = Store(database, scope="pilot")
    try:
        turn = store.db.execute("SELECT projection_id FROM turns WHERE scope=? ORDER BY opened_at DESC LIMIT 1", (store.scope,)).fetchone()
        try:
            trace = store.trace(turn["projection_id"]) if turn and turn["projection_id"] else {}
        except KernelError:
            trace = {}  # a forget in this turn removed its projection
        return {"selected": len(trace.get("selected", [])), "warnings": trace.get("warnings", [])}
    finally:
        store.close()


def kernel_summary(database):
    store = Store(database, scope="pilot")
    try:
        history = store.records(history=True)
        return {"facts": sorted(f"{r.get('entity_key') or r.get('entity')}.{r['predicate']}={r['value']} [{r['effective_state']}, {r['trust']}]"
                                for r in history),
                "held_for_review": len(store.records(quarantined=True)) - len(store.records()),
                "waiting_for_a_yes": store.pending_proposal_count(),
                "captures": dict(store.db.execute("SELECT outcome, count(*) FROM captures_log GROUP BY outcome").fetchall())}
    finally:
        store.close()


def run_variant(claude, base, variant, model, jev_command):
    root = base / variant
    root.mkdir(parents=True)
    database, mcp_path, hook_log, allowed = prepare(root, variant, jev_command)
    steps, signature = [], None
    for index, (task, kind, prompt, check) in enumerate(SCRIPT):
        if kind == "repo_change":
            record(root, 1, "Use SQLite for the job queue",
                   "Superseded by [2. Use Redis for the job queue](0002-use-redis-for-the-job-queue.md)",
                   "Jobs stay in one SQLite file next to the app.")
            record(root, 2, "Use Redis for the job queue", "Accepted", "Jobs move to Redis so several workers can share them.")
            git(root, "add", "-A")
            git(root, "commit", "-q", "-m", "Move the job queue to Redis")
            continue
        result = invoke(claude, root, prompt, mcp_path, allowed, model)
        result.update(task=task, kind=kind)
        if check:
            result["passed"] = passes(check, result.get("answer"))
        if database:
            result["kernel"] = kernel_state(database)
            if index == 0 or SCRIPT[index - 1][1] == "repo_change":
                # The SessionStart hook only starts the repository pass; later sessions should see its result.
                signature = wait_for_pass(database, root, previous=signature, seconds=60) or signature
        steps.append(result)
    hooks = [json.loads(line) for line in hook_log.read_text().splitlines()] if hook_log.exists() else []
    return {"variant": variant, "steps": steps, "hook_ms": hooks, "kernel": kernel_summary(database) if database else None}


def summarize(runs):
    table = {}
    for variant in VARIANTS:
        mine = [r for r in runs if r["variant"] == variant]
        steps = [s for r in mine for s in r["steps"]]
        checked = [s for s in steps if "passed" in s]
        hook = {}
        for r in mine:
            for h in r["hook_ms"]:
                hook.setdefault(h["event"], []).append(h["ms"])
        table[variant] = {
            "runs": len(mine),
            "repeated_explanations": sum(not s["passed"] for s in checked if s["kind"] in RECALL),
            "recall_checks": sum(s["kind"] in RECALL for s in checked),
            "stale_missed": sum(not s["passed"] for s in checked if s["kind"] in STALE),
            "stale_checks": sum(s["kind"] in STALE for s in checked),
            "forgotten_leaked": sum(not s["passed"] for s in checked if s["kind"] == "forgotten"),
            "generic_ok": sum(s["passed"] for s in checked if s["kind"] == "generic"),
            "median_session_s": round(statistics.median(s["seconds"] for s in steps), 2) if steps else None,
            "mean_session_s": round(statistics.mean(s["seconds"] for s in steps), 2) if steps else None,
            "kernel_hook_median_ms": {e: round(statistics.median(v)) for e, v in hook.items()},
            "kernel_hook_max_ms": {e: max(v) for e, v in hook.items()},
            "cost_usd": round(sum(s.get("cost_usd") or 0 for s in steps), 4),
            "input_tokens": sum(s.get("input_tokens") or 0 for s in steps),
            "tool_calls": sum(len(s.get("tools", [])) for s in steps),
            "memory_tool_calls": sum(sum(t.startswith("mcp__context-kernel") for t in s.get("tools", [])) for s in steps),
            "jev_tool_calls": sum(sum(t.startswith("mcp__plugin_jevmate") for t in s.get("tools", [])) for s in steps),
            "errors": sum(bool(s["exit_code"]) for s in steps),
            "left_for_owner": [{k: r["kernel"][k] for k in ("held_for_review", "waiting_for_a_yes")} for r in mine if r["kernel"]],
        }
    return table


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--workspace", required=True, help="an empty disposable directory outside iCloud")
    parser.add_argument("--variants", nargs="*", default=list(VARIANTS), choices=list(VARIANTS))
    parser.add_argument("--repetitions", type=int, default=1)
    parser.add_argument("--claude", default=shutil.which("claude"))
    parser.add_argument("--model")
    parser.add_argument("--jev-command", default=shutil.which("jev") or "jev")
    parser.add_argument("--out", help="write the full report here as JSON")
    args = parser.parse_args()
    base = Path(args.workspace).resolve()
    if base.exists() and any(base.iterdir()):
        print(canonical({"error": "Supply an empty disposable workspace."}))
        return 1
    base.mkdir(parents=True, exist_ok=True)
    report = {"client": subprocess.run([args.claude, "--version"], capture_output=True, text=True).stdout.strip(),
              "model": args.model or "client default", "script": [s[:3] for s in SCRIPT], "runs": []}
    rng = random.Random(7)
    for repetition in range(args.repetitions):
        order = list(args.variants)
        rng.shuffle(order)  # interleave so drift in API or local-model latency does not favour one variant
        for variant in order:
            print(f"[{time.strftime('%H:%M:%S')}] rep {repetition + 1} · {variant}", file=sys.stderr, flush=True)
            run = run_variant(args.claude, base / f"rep{repetition + 1}", variant, args.model, args.jev_command)
            run["repetition"] = repetition + 1
            report["runs"].append(run)
            if args.out:
                Path(args.out).write_text(json.dumps(report, ensure_ascii=False, indent=2))
    report["summary"] = summarize(report["runs"])
    if args.out:
        Path(args.out).write_text(json.dumps(report, ensure_ascii=False, indent=2))
    print(json.dumps(report["summary"], ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
