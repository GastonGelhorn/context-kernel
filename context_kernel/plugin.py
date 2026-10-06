"""One executable for everything an installed kernel runs: hooks, the MCP server, the band, the owner.

Claude Code runs it as `python3 <plugin>/bin/context-kernel <command>` and hands the plugin's options
to it as CLAUDE_PLUGIN_OPTION_<KEY>; a terminal install reads ~/.context-kernel/config.json, which
`context-kernel setup` writes. Either way it becomes the kernel CLI's own arguments, so nothing has to
be copied into a project's settings. The plugin's options win over the file, the file over defaults.
"""

import glob
import json
import os
import shutil
import sqlite3
import sys
from pathlib import Path

from . import cli

HOME = Path(os.path.expanduser("~"))
CONFIG = HOME / ".context-kernel" / "config.json"
DEFAULTS = {"scope": "work", "db": str(HOME / ".context-kernel" / "memory.sqlite"), "strategy": "jev", "jev_command": ""}
USAGE = """usage: context-kernel <command>

  hook prompt|stop|session-start [--client claude|codex]   what the host's hooks run
  serve                                                    the MCP server
  activity --session ID                                    what memory did in the session's last turn (JSON)
  undo ID                                                  take back a fact memory saved
  doctor                                                   check Python, the database, jev and the hooks
  setup                                                    choose the judge, the scope and the clients
  memory ARGS...                                           the kernel's owner CLI with this install's db and scope
"""


def settings():
    """The effective configuration and where each value came from."""
    stored = {}
    try:
        stored = json.loads(CONFIG.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        pass
    merged, origin = {}, {}
    for name, default in DEFAULTS.items():
        option = (os.environ.get(f"CLAUDE_PLUGIN_OPTION_{name.upper()}") or "").strip()
        if option:
            merged[name], origin[name] = option, "plugin option"
        elif str(stored.get(name) or "").strip():
            merged[name], origin[name] = str(stored[name]).strip(), str(CONFIG)
        else:
            merged[name], origin[name] = default, "default"
    merged["db"] = str(Path(os.path.expanduser(merged["db"])))
    return merged, origin


def find_jev(explicit=""):
    """jev by absolute path: hosts run hooks with a minimal PATH, so a bare name is not enough.
    The configured one, the terminal install's, the jevmate plugin's newest copy, then PATH."""
    cached = sorted(glob.glob(str(HOME / ".claude" / "plugins" / "cache" / "*" / "jevmate" / "*" / "bin" / "jev")),
                    key=lambda p: os.path.getmtime(p), reverse=True)
    for candidate in [os.path.expanduser(explicit) if explicit else "", str(HOME / ".local" / "bin" / "jev"), *cached,
                      shutil.which("jev") or ""]:
        if candidate and Path(candidate).is_file() and os.access(candidate, os.X_OK):
            return str(Path(candidate).resolve())
    return None


def kernel_args(config, jev):
    base = ["--db", config["db"], "--scope", config["scope"]]
    strategy = config["strategy"] if config["strategy"] in {"rules", "fts", "jev"} else "jev"
    if strategy == "jev" and not jev:
        strategy = "rules"  # selection fails open; captures still need a judge and are refused without one
    judge = ["--strategy", strategy] + (["--jev-command", jev] if jev else [])
    return base, judge


def ensure_database(config):
    """Installing the kernel is consent to keep its database; the first hook creates it."""
    if not Path(config["db"]).exists():
        Path(config["db"]).parent.mkdir(parents=True, exist_ok=True)
        with open(os.devnull, "w") as quiet:
            stdout, sys.stdout = sys.stdout, quiet
            try:
                cli.main(["--db", config["db"], "--scope", config["scope"], "init"])
            finally:
                sys.stdout = stdout


def workspace():
    return os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd()


def activity(config, session):
    from .adapters import activity as summary
    from .store import Store
    store = Store(config["db"], config["scope"])
    try:
        return summary(store, session)
    finally:
        store.close()


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0] in {"-h", "--help", "help"}:
        print(USAGE)
        return 0
    command, rest = argv[0], argv[1:]
    config, _ = settings()
    jev = find_jev(config["jev_command"])
    base, judge = kernel_args(config, jev)
    if command == "hook":
        event = rest[0] if rest and not rest[0].startswith("-") else "prompt"
        client = rest[rest.index("--client") + 1] if "--client" in rest else "claude"
        ensure_database(config)
        return cli.main(base + ["hook", "--client", client, "--workspace", workspace(), "--event", event] + judge)
    if command == "serve":
        ensure_database(config)
        return cli.main(base + ["serve"] + judge)
    if command == "activity":
        session = rest[rest.index("--session") + 1] if "--session" in rest else ""
        if not Path(config["db"]).exists() or not session:
            print(json.dumps({"line": "", "undo": [], "pending": 0, "held": 0}))
            return 0
        print(json.dumps(activity(config, session), ensure_ascii=False))
        return 0
    if command == "undo" and rest:
        return cli.main(base + ["undo", rest[0]])
    if command == "memory":
        return cli.main(base + rest)
    if command == "doctor":
        from .setup import doctor
        return doctor(config, jev, json_output="--json" in rest)
    if command == "setup":
        from .setup import wizard
        return wizard(rest)
    print(USAGE, file=sys.stderr)
    return 2


def fts5_available():
    try:
        db = sqlite3.connect(":memory:")
        db.execute("CREATE VIRTUAL TABLE t USING fts5(x)")
        db.close()
        return True
    except sqlite3.Error:
        return False


if __name__ == "__main__":
    raise SystemExit(main())
