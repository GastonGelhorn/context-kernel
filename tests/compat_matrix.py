"""Opt-in compatibility matrix: run the native checks against the clients installed now and record the versions.

Hooks, MCP, process ancestry, continuations and hook timeouts are host behaviour the kernel depends on, and
each client release can change one of them. This runs the end-to-end checks in fresh disposable folders:

  claude-v04   tests/native_claude_check.py: capture, change, review flag, forget, generic question
  claude-v06   tests/native_v06_check.py --client claude: decision records, standing facts, supersession
  claude-v09   tests/native_close_check.py: a fact that ends with its week, a step closed when it is done
  claude-hookless  tests/native_hookless_check.py: no hooks at all; AGENTS.md brief and the hookless MCP server
  codex-v06  tests/native_v06_check.py --client codex, only with --codex-pilot (a folder whose hooks the
               owner already trusted in Codex; see tests/native_codex_v04_check.py --prepare)

Each run appends one entry to docs/compatibility.json and rewrites the table in docs/compatibility.md.
It uses the owner's sign-ins and quota with fictional prompts; the installed plugin is switched off for the
runs (SHELFLIFE_CONTEXT_OFF), so nothing reaches the owner's real memory. Exit status 1 when a check failed.

    python3 -m tests.compat_matrix
    python3 -m tests.compat_matrix --codex-pilot ~/pilots/codex-ck
"""

import argparse
import contextlib
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import platform
import shutil
import subprocess
import sys
import tempfile

from shelflife_context import __version__

ROOT = Path(__file__).resolve().parent.parent
HISTORY = ROOT / "docs" / "compatibility.json"
TABLE = ROOT / "docs" / "compatibility.md"
CHECKS = {"claude-v04": ["tests.native_claude_check"], "claude-v06": ["tests.native_v06_check", "--client", "claude"],
          "claude-v09": ["tests.native_close_check"], "claude-hookless": ["tests.native_hookless_check"],
          "codex-v06": ["tests.native_v06_check", "--client", "codex"]}


def version(command):
    if not command:
        return None
    try:
        done = subprocess.run([command, "--version"], capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return None
    return (done.stdout or done.stderr).strip().splitlines()[0] if (done.stdout or done.stderr).strip() else None


REPORTS = Path.home() / ".shelflife-context" / "compat-runs"   # full reports, for diagnosis; not part of the repository


def attempt(name, workspace, jev):
    argv = [sys.executable, "-m", *CHECKS[name], "--workspace", str(workspace)] + (["--jev-command", jev] if jev else [])
    env = dict(os.environ, SHELFLIFE_CONTEXT_OFF="1", PYTHONPATH=str(ROOT))
    started = datetime.now(timezone.utc)
    try:
        done = subprocess.run(argv, cwd=ROOT, capture_output=True, text=True, timeout=1800, env=env)
    except subprocess.TimeoutExpired:
        return {"passed": False, "error": "timed out after 30 minutes"}
    try:
        report = json.loads(done.stdout)
    except ValueError:
        return {"passed": False, "error": (done.stderr or done.stdout).strip()[-300:]}
    REPORTS.mkdir(parents=True, exist_ok=True)
    saved = REPORTS / f"{started.strftime('%Y%m%dT%H%M%S')}-{name}.json"
    saved.write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
    checks = report.get("checks") or {}
    return {"passed": bool(report.get("passed")), "checks": f"{sum(map(bool, checks.values()))}/{len(checks)}",
            "failed": [k for k, v in checks.items() if not v], "client": report.get("version"),
            "minutes": round((datetime.now(timezone.utc) - started).total_seconds() / 60, 1),
            "error": report.get("error") or report.get("aborted"), "report": "~/.shelflife-context/compat-runs/" + saved.name}


def run_check(name, workspace_factory, jev):
    """One attempt, and a second when the first fails: the agent writes a new answer each time, so a check
    that passes on the retry is recorded as flaky, with what failed the first time, rather than as a pass."""
    with workspace_factory() as workspace:
        first = attempt(name, Path(workspace), jev)
    if first["passed"] or first.get("error"):
        return first
    with workspace_factory() as workspace:
        second = attempt(name, Path(workspace), jev)
    if second["passed"]:
        return second | {"flaky": True, "failed_first": first["failed"], "report_first": first.get("report")}
    return second | {"failed_first": first["failed"]}


def render(history):
    lines = ["# Compatibility", "",
             "End-to-end checks run against the real clients (`python3 -m tests.compat_matrix`). Each row is one run; "
             "the newest first. A check is the whole flow through the client's own hooks and MCP server, judged from "
             "the kernel's database, not from the model's answer alone.", "",
             "| When (UTC) | Kernel | jev | Check | Client | Result | Failed |", "| --- | --- | --- | --- | --- | --- | --- |"]
    for entry in reversed(history[-40:]):
        for name, result in entry["results"].items():
            outcome = ("flaky " if result.get("flaky") else "pass " if result["passed"] else "FAIL ") + str(result.get("checks") or "")
            failed = ", ".join(result.get("failed") or []) or (result.get("error") or "")
            if result.get("failed_first"):
                failed = (failed + "; " if failed else "") + "first attempt failed: " + ", ".join(result["failed_first"])
            lines.append(f"| {entry['at'][:16].replace('T', ' ')} | {entry['kernel']} | {entry['jev'] or '–'} | {name} | "
                         f"{result.get('client') or entry['clients'].get(name.split('-')[0]) or '–'} | {outcome.strip()} | {failed} |")
    lines += ["", "A failed check is run once more, since the agent writes a new answer each time: `flaky` means it passed on the "
              "second attempt, and the first attempt's failures are listed. Full reports are kept in ~/.shelflife-context/compat-runs. "
              "Codex rows need a pilot folder whose hooks were trusted in Codex once by hand; without one the run skips Codex.", ""]
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--jev-command", default=shutil.which("jev"))
    parser.add_argument("--codex-pilot", help="A Codex pilot folder the owner trusted (hooks reviewed once)")
    parser.add_argument("--only", nargs="*", choices=sorted(CHECKS), help="Run only these checks")
    args = parser.parse_args()
    claude, codex = shutil.which("claude"), shutil.which("codex")
    entry = {"at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "kernel": __version__,
             "jev": version(args.jev_command), "python": platform.python_version(), "os": f"{platform.system()} {platform.release()}",
             "clients": {"claude": version(claude), "codex": version(codex)}, "results": {}}
    wanted = args.only or [n for n in CHECKS if not n.startswith("codex") or args.codex_pilot]
    for name in wanted:
        if name.startswith("claude") and not claude:
            entry["results"][name] = {"passed": False, "error": "claude CLI not found"}
            continue
        if name.startswith("codex") and not (codex and args.codex_pilot):
            entry["results"][name] = {"passed": False, "error": "codex CLI or trusted pilot missing"}
            continue
        if name.startswith("codex"):
            pilot = Path(args.codex_pilot).expanduser()
            entry["results"][name] = run_check(name, lambda: contextlib.nullcontext(pilot), args.jev_command)
        else:
            entry["results"][name] = run_check(name, lambda: tempfile.TemporaryDirectory(prefix=f"ck-{name}-"), args.jev_command)
        print(f"{name}: {entry['results'][name]}", flush=True)
    history = json.loads(HISTORY.read_text(encoding="utf-8")) if HISTORY.exists() else []
    history.append(entry)
    HISTORY.write_text(json.dumps(history, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    TABLE.write_text(render(history), encoding="utf-8")
    return 0 if all(r["passed"] for r in entry["results"].values()) else 1


if __name__ == "__main__":
    raise SystemExit(main())
