"""Opt-in measurement of the whole capture path on held-out rows (fixtures/holdout.jsonl).

fixtures/calibration.jsonl is where thresholds were chosen; holdout.jsonl was written afterwards and
must never be used to tune them. Each `affirmed` row goes through the real prompt hook and
`memory_capture` with the real jev, as if the agent proposed exactly the row's fact. The outcome per
row is what the kernel did with it: captured (delivered later), quarantined (held, never
delivered), or refused. A false row that ends up captured is an unsupported memory.

The judge's model fingerprint is printed with the results: re-run when it changes.
"""

import argparse
import json
from pathlib import Path
import shutil
import tempfile

from shelflife_context.adapters import hook_response, packet_of
from shelflife_context.common import timestamp, timestamp_offset
from shelflife_context.compiler import Compiler
from shelflife_context.judge import JevCommand
from shelflife_context.mcp import Server
from shelflife_context.store import Store


HOST = [4242, "holdout"]
ROOT = Path(__file__).resolve().parent.parent


def triple(fact):
    head, value = fact.split(":", 1)
    entity, *predicate = head.split()
    return {"entity": entity, "predicate": "_".join(predicate) or "fact", "value": value.strip()}


def run(jev_command, fixtures):
    rows = [json.loads(line) for line in Path(fixtures).read_text(encoding="utf-8").splitlines() if line.strip()]
    rows = [r for r in rows if r["kind"] == "affirmed"]
    judge = JevCommand(jev_command, timeout=60)
    out = []
    for n, row in enumerate(rows):
        folder = Path(tempfile.mkdtemp(prefix="ck-holdout-"))
        clock = {"now": "2026-10-06T12:00:00+00:00"}
        store = Store(folder / "memory.sqlite", clock=lambda: timestamp(clock["now"]), create=True)
        try:
            event = {"cwd": str(folder), "prompt": row["message"], "session_id": "h", "prompt_id": f"p{n}"}
            response, _ = hook_response(event, folder, store, Compiler(store), strategy="rules", judge=judge)
            packet = packet_of(response["hookSpecificOutput"]["additionalContext"])
            store.register_session("h", "claude", [HOST])
            clock["now"] = timestamp_offset(clock["now"], 3)
            server = Server(store, Compiler(store), judge=judge, parent=HOST)
            server.dispatch({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
                "protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {}}})
            server.dispatch({"jsonrpc": "2.0", "method": "notifications/initialized"})
            result = server.dispatch({"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {
                "name": "memory_capture", "arguments": {"token": packet["turn"]["token"], "facts": [triple(row["fact"])]}}})
            content = result["result"]["structuredContent"]
            first = (content.get("results") or [{}])[0]
            out.append({"label": row["label"], "message": row["message"], "outcome": first.get("status", "error"),
                        "reason": first.get("reason"), "asked": bool(packet.get("turn", {}).get("capture"))})
        finally:
            store.close()
            shutil.rmtree(folder)
    return {"judge": judge.describe(), "rows": out}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--jev-command", default=shutil.which("jev") or "jev")
    parser.add_argument("--fixtures", default=str(ROOT / "fixtures" / "holdout.jsonl"))
    args = parser.parse_args()
    report = run(args.jev_command, args.fixtures)
    rows = report["rows"]
    report["summary"] = {
        "true_rows": sum(r["label"] for r in rows),
        "true_captured": sum(r["label"] and r["outcome"] == "captured" for r in rows),
        "false_rows": sum(not r["label"] for r in rows),
        "false_captured": sum(not r["label"] and r["outcome"] == "captured" for r in rows),
        "false_quarantined": sum(not r["label"] and r["outcome"] == "quarantined" for r in rows),
    }
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    raise SystemExit(main())
