#!/usr/bin/env bash
# Installs Shelflife from this checkout for the terminal and Codex: the package to
# ~/.local/share/shelflife-context, the launcher to ~/.local/bin/shelflife-context, then the setup wizard
# (judge, scope, clients). Claude Code users can instead install the plugin:
#   /plugin marketplace add GastonGelhorn/jevmate   then   /plugin install shelflife-context@gastongelhorn
# Usage: ./install.sh [--no-setup] [setup flags...]
set -euo pipefail
SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

usable() { "$1" -c 'import sqlite3, sys; assert sys.version_info >= (3, 9); sqlite3.connect(":memory:").execute("create virtual table t using fts5(x)")' 2>/dev/null; }

PY="$(command -v python3 || true)"
if [ -z "$PY" ] || ! usable "$PY"; then
  if command -v uv >/dev/null 2>&1; then
    echo "python3 is missing, older than 3.9, or its SQLite lacks FTS5: using a Python managed by uv"
    PY="$(uv python find 3.12 2>/dev/null || { uv python install 3.12 >/dev/null && uv python find 3.12; })"
  fi
fi
if [ -z "$PY" ] || ! usable "$PY"; then
  echo "Shelflife needs Python 3.9+ with SQLite FTS5. Install one (python.org, Homebrew, or uv:" >&2
  echo "  curl -LsSf https://astral.sh/uv/install.sh | sh ) and run this again." >&2
  exit 1
fi

BIN="${HOME}/.local/bin"; LIB="${HOME}/.local/share/shelflife-context"
mkdir -p "$BIN" "$LIB"
rm -rf "$LIB/shelflife_context"
cp -R "$SRC/shelflife_context" "$LIB/shelflife_context"
find "$LIB/shelflife_context" -name '__pycache__' -type d -prune -exec rm -rf {} + 2>/dev/null || true
# The launcher runs with the Python checked above, whatever python3 a host finds on its PATH.
{ echo "#!${PY}"; tail -n +2 "$SRC/bin/shelflife-context"; } > "$BIN/shelflife-context"
chmod 755 "$BIN/shelflife-context"
"$PY" -m compileall -q "$LIB/shelflife_context" || true
echo "installed  $BIN/shelflife-context  (Python $("$PY" -c 'import platform; print(platform.python_version())'))"
echo "package    $LIB/shelflife_context"
case ":$PATH:" in *":$BIN:"*) ;; *) echo "add $BIN to your PATH";; esac

if [ "${1:-}" = "--no-setup" ]; then
  echo "next       shelflife-context setup"
  exit 0
fi
if [ -t 0 ]; then
  exec "$BIN/shelflife-context" setup "$@"
fi
echo "next       shelflife-context setup   (no terminal to ask in)"
