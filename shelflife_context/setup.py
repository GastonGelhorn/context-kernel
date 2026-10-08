"""`shelflife-context doctor` and `shelflife-context setup`: what is installed, and an interactive way to choose.

The wizard asks, in order: which scope and database, which judge (jev on Ollama on this machine,
on Ollama on another machine of yours, or on jev's paid hosted service), and which clients to wire (the Claude Code plugin, Codex hooks for a
project). Every change it makes is shown first and needs a yes; `--yes` with flags runs it unattended,
which is how the plugin's /shelflife-context:setup command drives it.
"""

import argparse
import contextlib
import getpass
import io
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlsplit

from . import cli
from .common import KernelError
from .judge import JevCommand, JudgeError, loaded, origin
from .plugin import CONFIG, DEFAULTS, ensure_database, find_jev, fts5_available, settings, workspace

MARKETPLACE = "GastonGelhorn/jevmate"
PLUGIN = "shelflife-context@gastongelhorn"


def _line(state, label, detail):
    print(f"{state:<5} {label}: {detail}")


def _backend(jev, private=()):
    try:
        return JevCommand(jev, timeout=15, private=private).describe()
    except (JudgeError, KernelError, OSError) as exc:
        return {"error": str(exc)}


def _policy(config):
    from .capture import policy
    from .store import Store
    store = Store(config["db"], config["scope"])
    try:
        return policy(store), store.status()
    finally:
        store.close()


def _wired_by_hand(root):
    """Project files that already run the kernel: with the plugin on too, every hook runs twice."""
    found = []
    for name in (".claude/settings.local.json", ".claude/settings.json", ".mcp.json"):
        path = Path(root) / name
        try:
            if "shelflife_context" in path.read_text(encoding="utf-8") or '"shelflife-context"' in path.read_text(encoding="utf-8"):
                found.append(name)
        except OSError:
            pass
    return found


def _plugin_installed(home=None):
    path = Path(home or Path.home()) / ".claude" / "plugins" / "installed_plugins.json"
    try:
        return PLUGIN in path.read_text(encoding="utf-8")
    except OSError:
        return False


def _plugin_disabled_here(root):
    """The project turned the plugin off (`enabledPlugins` false), so its own hook entries are the only ones."""
    for name in (".claude/settings.local.json", ".claude/settings.json"):
        try:
            if json.loads((Path(root) / name).read_text(encoding="utf-8")).get("enabledPlugins", {}).get(PLUGIN) is False:
                return True
        except (OSError, ValueError, AttributeError):
            pass
    return False


def _calibration_line(config, backend):
    from .judge import model_key
    from .store import Store
    store = Store(config["db"], config["scope"])
    try:
        record = store.judge_record(model_key(backend))
        others = [r for r in store.judge_records() if r["status"] == "passed" and r["model_key"] != model_key(backend)]
    finally:
        store.close()
    if record and record["status"] == "passed":
        return "ok", "calibration", f"this judge passed the canary on {record['checked_at'][:10]}"
    if record and record["status"] == "failed":
        wrong = len(record["detail"].get("wrong", []))
        return "warn", "calibration", (f"this judge got {wrong} canary row(s) wrong: captures are held for review. Tune the "
                                       "thresholds (`shelflife-context memory calibrate affirmed --score`), then "
                                       "`shelflife-context memory calibrate --check`")
    if others:
        return "warn", "calibration", "the judge changed since the last check: captures are held for review until the next " \
                                      "session start checks it (or run `shelflife-context memory calibrate --check`)"
    return "ok", "calibration", "not checked yet; the next session start runs the canary in the background"


def _regret(config):
    from .store import Store
    try:
        store = Store(config["db"], config["scope"])
    except (KernelError, OSError):
        return None
    try:
        return store.regret_metrics(30)
    finally:
        store.close()


SF_DATALESS = 0x40000000  # macOS: the file's content was evicted to iCloud and is downloaded on first read


def icloud(paths, home=None, stat=os.stat):
    """Folders iCloud syncs, and files in them it has evicted. Reading an evicted file waits for the
    download: a hook that imports one can outlast its timeout (measured: a prompt hook cancelled at 15 s
    while 14 of the kernel's modules were evicted; `git log` waited 44 s the same way)."""
    home = Path(home or Path.home())
    cloud = home / "Library" / "Mobile Documents"
    synced = [cloud] + [home / name for name in ("Documents", "Desktop") if (cloud / "com~apple~CloudDocs" / name).is_dir()]
    found = []
    for path in map(Path, paths):
        inside = any(path == root or root in path.parents for root in synced)
        evicted = 0
        for item in [path, *(list(path.rglob("*"))[:2000] if path.is_dir() else [])]:
            try:
                evicted += bool(getattr(stat(item, follow_symlinks=False), "st_flags", 0) & SF_DATALESS)
            except OSError:
                pass
        if inside or evicted:
            found.append({"path": str(path), "synced": inside, "evicted": evicted})
    return found


def doctor(config, jev, json_output=False):
    report = []

    def add(state, label, detail):
        report.append({"state": state, "label": label, "detail": detail})

    _, origin = settings()
    version = ".".join(map(str, sys.version_info[:3]))
    add("ok" if sys.version_info >= (3, 9) else "fail", "python", f"{version} at {sys.executable}")
    add("ok" if fts5_available() else "fail", "sqlite", "FTS5 available" if fts5_available() else "this Python's SQLite has no FTS5")
    add("ok", "scope", f"{config['scope']} ({origin['scope']})")
    if Path(config["db"]).exists():
        try:
            rules, status = _policy(config)
            add("ok", "database", f"{config['db']} · {status['current_statements']} facts, "
                                  f"{status['quarantined']} held for review, {status['pending_proposals']} waiting for a yes")
        except (KernelError, OSError) as exc:
            rules = None
            add("fail", "database", f"{config['db']}: {exc}")
    else:
        rules = None
        add("ok", "database", f"{config['db']} (created on first use)")
    if not jev:
        add("fail", "jev", "not found. Install jevmate (claude plugin install jevmate@gastongelhorn, or its install.sh): "
                           "without jev nothing is saved from the conversation")
    else:
        backend = _backend(jev, config.get("private_judges", ()))
        if "error" in backend:
            add("fail", "jev", f"{jev}: {backend['error']}")
        elif backend["local"]:
            state = {True: "loaded", False: "not loaded (the next session start loads it)", None: "load state unknown"}[loaded(backend)]
            where = "your machine at" if backend.get("private") else "local ·"
            add("ok", "jev", f"{jev} · {where} {backend['url']} · {backend['model']} · {state}")
        else:
            allowed = bool(rules and rules.get("allow_remote_judge"))
            hint = (" · if that machine is yours, `shelflife-context setup --judge remote --judge-url "
                    f"{urlsplit(backend['url']).hostname}` names it; otherwise" if backend["url"].startswith("http://") else " ·")
            add("ok" if allowed else "warn", "jev",
                f"{jev} · hosted · {backend['url']}" + ("" if allowed else
                    f"{hint} this scope does not allow a hosted judge, so nothing is saved: run `shelflife-context setup`"))
        if "error" not in backend and Path(config["db"]).exists():
            add(*_calibration_line(config, backend))
    if rules is not None:
        add("ok", "repository", "decision records" + (" and decision commits" if rules.get("repository_commits") else
                                                      " (commit messages off; `shelflife-context memory policy --repository-commits on`)")
            if rules.get("repository", True) else "off for this scope")
        regret = _regret(config)
        if regret and regret["captured"]:
            estimate = regret["precision_estimate"]
            add("ok" if estimate is None or estimate >= 0.9 else "warn", "precision",
                f"last 30 days: {regret['captured']} saved, {sum(regret['taken_back'].values())} taken back soon after "
                f"(estimate {estimate}); {regret['held_for_review']} held for review")
    for place in icloud([Path(__file__).resolve().parent, Path(config["db"]).expanduser().parent]):
        where = place["path"]
        if place["evicted"]:
            add("warn", "icloud", f"{place['evicted']} file(s) under {where} are evicted to iCloud. A hook that reads one waits for "
                                  "the download and can be cancelled. In Finder choose Keep Downloaded for that folder, or keep "
                                  "the kernel outside Documents and Desktop (install.sh copies it to ~/.local/share/shelflife-context).")
        else:
            add("warn", "icloud", f"{where} is in a folder iCloud syncs: macOS can evict its files later, and hooks would then wait "
                                  "for downloads. Choose Keep Downloaded for it in Finder, or keep it outside Documents and Desktop.")
    if os.environ.get("CLAUDE_PLUGIN_ROOT") or _plugin_installed():
        twice = [name for name in _wired_by_hand(workspace()) if not _plugin_disabled_here(workspace())]
        if twice:
            add("warn", "hooks", "the plugin is on and " + ", ".join(twice) + " in this project also runs the kernel: "
                "each prompt reaches the kernel twice (the second is skipped, but both wait). Remove the kernel's entries "
                "from those files, or turn the plugin off for this project.")
    if json_output:
        print(json.dumps(report, ensure_ascii=False))
    else:
        for row in report:
            _line(row["state"], row["label"], row["detail"])
    return 1 if any(r["state"] == "fail" for r in report) else 0


# The wizard

def _ask(question, default, choices=None, assume=False):
    if assume:
        return default
    hint = f" [{'/'.join(choices)}]" if choices else ""
    while True:
        answer = input(f"? {question}{hint} ({default}): ").strip() or default
        if not choices or answer in choices:
            return answer
        print(f"  choose one of {', '.join(choices)}")


def _yes(question, default=True, assume=False):
    if assume:
        return default
    answer = input(f"? {question} [{'Y/n' if default else 'y/N'}]: ").strip().lower()
    return default if not answer else answer.startswith(("y", "s"))


def _run(argv, show=True):
    if show:
        print("  $ " + " ".join(argv))
    return subprocess.run(argv, text=True, capture_output=True)


def _jev_has_setup(jev):
    """jevmate 1.10 and later prepare and check an Ollama themselves (`jev setup`)."""
    return _run([jev, "setup", "--help"], show=False).returncode == 0


def _jev_setup(jev, argv, args):
    """Run `jev setup` and show what it said. A missing tev1 is downloaded only after a yes or --pull."""
    result = _run([jev, "setup", *argv, "--yes"] + (["--pull"] if args.pull else []))
    output = (result.stdout or result.stderr).strip()
    if result.returncode and "--pull" in output and not args.pull and _yes("Download tev1 there (4.5 GB)?", False, args.yes):
        result = _run([jev, "setup", *argv, "--yes", "--pull"])
        output = (result.stdout or result.stderr).strip()
    print("\n".join("    " + line for line in output.splitlines()[-6:]))
    if result.returncode:
        print("  jev setup did not finish: nothing changed here.")
    return result.returncode == 0


def _choose_judge(args, jev, config):
    backend = _backend(jev, config["private_judges"])
    if "url" not in backend:
        current = "unknown"
    else:
        current = "remote" if backend.get("private") else ("local" if backend.get("local") else "hosted")
    print(f"\nJudge. jev decides what is worth remembering and what is relevant. Now: {current}"
          + (f" ({backend.get('model')} at {backend.get('url')})" if "url" in backend else f" ({backend.get('error')})"))
    print("  local   jev on Ollama on this machine: free, private, about 0.3 s per prompt once warm, 4 s cold")
    print("  remote  jev on Ollama on another machine of yours (Tailscale, your LAN): free, memory stays on your machines")
    print("  hosted  jev's paid service: about 0.25 s, your memory text is sent to it")
    print("  keep    leave jev as it is")
    choice = args.judge or _ask("Which judge", "keep" if current != "unknown" else "local", ["local", "remote", "hosted", "keep"], args.yes)
    if choice != "keep":
        print("  (jev's backend is shared with jevmate: changing it here changes it there too)")
    remote, named = None, config["private_judges"]
    if choice == "local":
        if _jev_has_setup(jev):
            if not _jev_setup(jev, ["--judge", "local"], args):
                return
        else:
            if not shutil.which("ollama"):
                print("  Ollama is not installed: get it from https://ollama.com, then run setup again.")
            _run([jev, "config", "set", "backend", "ollama"])
        remote, named = False, []
    elif choice == "remote":
        address = args.judge_url or _ask("Address of the machine running Ollama (host, host:port or URL)", "", assume=args.yes)
        if not address:
            print("  remote needs that machine's address: --judge-url HOST")
            return
        if not _jev_has_setup(jev):
            print("  jevmate 1.10 or newer sets up an Ollama on another machine. Update it (in Claude Code: "
                  "/plugin update jevmate@gastongelhorn; in a terminal: pull jevmate and run its install.sh), then setup again.")
            return
        if not _jev_setup(jev, ["--judge", "remote", "--url", address], args):
            return
        server = origin(_backend(jev).get("url", ""))
        print(f"  Memory text will go to {server}. Ollama has no password: name it only if the machine is yours "
              "and on a network you trust (Tailscale, your LAN).")
        if not _yes(f"Is {server} yours?", True, args.yes):
            print("  Not named: jev uses it, but memory saves nothing new there unless the scope allows a hosted judge.")
            return
        remote, named = False, [server]
    elif choice == "hosted":
        if args.yes and not os.environ.get("TYPESAFE_API_KEY"):
            print("  hosted needs a key: run `jev auth set <key>` (or set TYPESAFE_API_KEY), then setup again.")
        elif not args.yes:
            key = getpass.getpass("? jev API key (typesafe.ai or sk-or-…; empty keeps the saved one): ").strip()
            if key:
                result = subprocess.run([jev, "auth", "set", key], text=True, capture_output=True)
                print("  key saved" if result.returncode == 0 else "  jev auth failed: " + (result.stderr or result.stdout).strip()[:200])
        print("  With a hosted judge the text of your memory and of your messages leaves this machine.")
        remote = _yes(f"Allow that for the scope '{config['scope']}'?", False, args.yes and args.allow_hosted)
        if not remote:
            print("  Not allowed: memory keeps working for reading, but nothing new is saved until you allow it.")
        named = []
    elif current == "hosted" and backend["url"].startswith("http://"):
        server = origin(backend["url"])
        if _yes(f"jev points at {server}, off this machine. Is it a machine of yours, on a network you trust?", False, args.yes):
            named = [server]
    if named != config["private_judges"]:
        config["private_judges"] = named
        _write_config(config)
        print(f"  {'named ' + ', '.join(named) + ' as yours' if named else 'no machine named as yours'} in {CONFIG}")
    if remote is not None:
        with contextlib.redirect_stdout(io.StringIO()):
            cli.main(["--db", config["db"], "--scope", config["scope"], "policy", "--allow-remote-judge", "yes" if remote else "no"])
    check = _run([jev, "doctor"], show=False)
    print("  jev doctor: " + ("healthy" if check.returncode == 0 else "problems:\n" + (check.stdout or check.stderr)[-800:]))


def _write_config(config):
    CONFIG.parent.mkdir(parents=True, exist_ok=True)
    stored = {k: config[k] for k in DEFAULTS}
    if config.get("private_judges"):
        stored["private_judges"] = config["private_judges"]
    CONFIG.write_text(json.dumps(stored, indent=2) + "\n", encoding="utf-8")
    os.chmod(CONFIG, 0o600)


def _launcher():
    here = Path(sys.argv[0]).resolve()
    if here.name == "shelflife-context":
        return str(here)
    installed = Path.home() / ".local" / "bin" / "shelflife-context"
    return str(installed) if installed.exists() else None


def codex_files(root, python, launcher):
    """The Codex hooks and MCP server for one project, pointing at the installed launcher."""
    def command(event):
        return f"{json.dumps(python)} {json.dumps(launcher)} hook {event} --client codex"
    hooks = {"UserPromptSubmit": [{"hooks": [{"type": "command", "command": command("prompt"), "timeout": 15,
                                              "additionalContextLimit": 3072}]}],
             "Stop": [{"hooks": [{"type": "command", "command": command("stop"), "timeout": 20}]}],
             "SessionStart": [{"hooks": [{"type": "command", "command": command("session-start"), "timeout": 10}]}]}
    toml = (f"[mcp_servers.shelflife-context]\ncommand = {json.dumps(python)}\nargs = {json.dumps([launcher, 'serve'])}\n")
    return Path(root) / ".codex" / "hooks.json", hooks, Path(root) / ".codex" / "config.toml", toml


def _wire_codex(args):
    launcher = _launcher()
    if not launcher:
        print("  Codex needs the terminal install (install.sh) so its hooks have a stable path; skipped.")
        return
    root = Path(os.path.expanduser(args.codex or _ask("Codex project folder", os.getcwd(), assume=args.yes))).resolve()
    hooks_path, hooks, toml_path, toml = codex_files(root, sys.executable, launcher)
    try:
        existing = json.loads(hooks_path.read_text(encoding="utf-8")) if hooks_path.exists() else {}
    except ValueError:
        print(f"  {hooks_path} is not valid JSON; skipped.")
        return
    merged = dict(existing)
    merged.setdefault("hooks", {})
    for event, entries in hooks.items():
        kept = [e for e in merged["hooks"].get(event, []) if "shelflife-context" not in json.dumps(e) and "shelflife_context" not in json.dumps(e)]
        merged["hooks"][event] = kept + entries
    toml_text = toml_path.read_text(encoding="utf-8") if toml_path.exists() else ""
    print(f"\n  {hooks_path}:\n" + "\n".join("    " + l for l in json.dumps(merged, indent=2).splitlines()))
    if "[mcp_servers.shelflife-context]" in toml_text:
        print(f"  {toml_path} already has [mcp_servers.shelflife-context]; left as it is.")
        toml = None
    else:
        print(f"  {toml_path} (appended):\n" + "\n".join("    " + l for l in toml.splitlines()))
    if not _yes("Write these files?", True, args.yes):
        return
    hooks_path.parent.mkdir(parents=True, exist_ok=True)
    hooks_path.write_text(json.dumps(merged, indent=2) + "\n", encoding="utf-8")
    if toml:
        toml_path.write_text(toml_text + ("\n" if toml_text and not toml_text.endswith("\n") else "") + toml, encoding="utf-8")
    print("  Written. Codex asks you to review the hooks the next time it opens this folder: that is expected.")


def _wire_claude(args):
    claude = shutil.which("claude")
    if not claude:
        print(f"  Claude Code's CLI is not on PATH. In Claude Code run:\n    /plugin marketplace add {MARKETPLACE}\n    /plugin install {PLUGIN}")
        return
    if not _yes(f"Install the Claude Code plugin ({PLUGIN})?", True, args.yes):
        return
    for argv in ([claude, "plugin", "marketplace", "add", MARKETPLACE], [claude, "plugin", "install", PLUGIN]):
        result = _run(argv)
        if result.returncode != 0:
            print("  failed: " + (result.stderr or result.stdout).strip()[:300])
            return
    print("  Installed. Start a new Claude Code session to load it.")


def wizard(argv):
    parser = argparse.ArgumentParser(prog="shelflife-context setup")
    parser.add_argument("--judge", choices=("local", "remote", "hosted", "keep"))
    parser.add_argument("--judge-url", metavar="HOST", help="with --judge remote: the machine running Ollama (host, host:port or URL)")
    parser.add_argument("--pull", action="store_true", help="with --judge local|remote: download tev1 there when it is missing (4.5 GB)")
    parser.add_argument("--allow-hosted", action="store_true", help="with --yes: allow a hosted judge for the scope")
    parser.add_argument("--scope")
    parser.add_argument("--db")
    parser.add_argument("--claude", choices=("yes", "no"))
    parser.add_argument("--codex", metavar="FOLDER", help="wire Codex hooks for this project")
    parser.add_argument("--yes", action="store_true", help="accept the defaults and the flags without asking")
    args = parser.parse_args(argv)
    config, _ = settings()
    print("Shelflife setup: memory for coding agents, maintained by talking.\n")
    if sys.version_info < (3, 9) or not fts5_available():
        print("This Python is too old or its SQLite lacks FTS5. Install Python 3.9+ (python.org, Homebrew or uv) and run again.")
        return 1
    jev = find_jev(config["jev_command"])
    if not jev:
        print("jev is not installed. The kernel needs it to judge what to keep. Install jevmate first:\n"
              f"  Claude Code:  /plugin marketplace add {MARKETPLACE}  then  /plugin install jevmate@gastongelhorn\n"
              "  Terminal:     git clone https://github.com/GastonGelhorn/jevmate && jevmate/install.sh")
        if not _yes("Continue without jev (nothing will be saved until it is installed)?", False, args.yes):
            return 1
    config["scope"] = args.scope or _ask("Scope (a separate memory per area: work, personal, a client…)", config["scope"], assume=args.yes)
    config["db"] = str(Path(os.path.expanduser(args.db or _ask("Database", config["db"], assume=args.yes))))
    _write_config(config)
    ensure_database(config)
    print(f"  saved {CONFIG}")
    if jev:
        _choose_judge(args, jev, config)
    print("\nClients.")
    if (args.claude or ("yes" if _yes("Use it in Claude Code?", True, args.yes) else "no")) == "yes":
        _wire_claude(args)
    if args.codex or (not args.yes and _yes("Use it in Codex for a project?", False)):
        _wire_codex(args)
    print("\nCheck:")
    return doctor(config, find_jev(config["jev_command"]))
