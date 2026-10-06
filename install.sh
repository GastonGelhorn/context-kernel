#!/usr/bin/env bash
# Installs Context Kernel from this checkout for the terminal and Codex: the package to
# ~/.local/share/context-kernel, the launcher to ~/.local/bin/context-kernel, then the setup wizard
# (judge, scope, clients). Claude Code users can instead install the plugin:
#   /plugin marketplace add GastonGelhorn/jevmate   then   /plugin install context-kernel@gastongelhorn
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
  echo "Context Kernel needs Python 3.9+ with SQLite FTS5. Install one (python.org, Homebrew, or uv:" >&2
  echo "  curl -LsSf https://astral.sh/uv/install.sh | sh ) and run this again." >&2
  exit 1
fi

BIN="${HOME}/.local/bin"; LIB="${HOME}/.local/share/context-kernel"
mkdir -p "$BIN" "$LIB"
rm -rf "$LIB/context_kernel"
cp -R "$SRC/context_kernel" "$LIB/context_kernel"
find "$LIB/context_kernel" -name '__pycache__' -type d -prune -exec rm -rf {} + 2>/dev/null || true
# The launcher runs with the Python checked above, whatever python3 a host finds on its PATH.
{ echo "#!${PY}"; tail -n +2 "$SRC/bin/context-kernel"; } > "$BIN/context-kernel"
chmod 755 "$BIN/context-kernel"
"$PY" -m compileall -q "$LIB/context_kernel" || true
echo "installed  $BIN/context-kernel  (Python $("$PY" -c 'import platform; print(platform.python_version())'))"
echo "package    $LIB/context_kernel"
case ":$PATH:" in *":$BIN:"*) ;; *) echo "add $BIN to your PATH";; esac

if [ "${1:-}" = "--no-setup" ]; then
  echo "next       context-kernel setup"
  exit 0
fi
if [ -t 0 ]; then
  exec "$BIN/context-kernel" setup "$@"
fi
echo "next       context-kernel setup   (no terminal to ask in)"
