"""Which host session a process belongs to, established outside the model.

Claude Code and Codex start both their hook commands and their stdio MCP servers as descendants of
the per-session host process. A hook records its ancestor chain (pid and start time, so a reused
pid does not match) together with the session id the host gave it. An MCP server later looks for
the session whose recorded chain contains its own parent. Nothing the model says is involved.
When no unique session matches, the server is unbound and refuses anything that writes or deletes.
"""

import os
import subprocess


def process_table():
    """{pid: (ppid, start)} from one `ps` call; empty where `ps` is unavailable."""
    try:
        output = subprocess.run(["ps", "-axo", "pid=,ppid=,lstart="], capture_output=True, text=True, timeout=5).stdout
    except (OSError, subprocess.SubprocessError):
        return {}
    table = {}
    for line in output.splitlines():
        parts = line.split(None, 2)
        if len(parts) == 3 and parts[0].isdigit() and parts[1].isdigit():
            table[int(parts[0])] = (int(parts[1]), parts[2].strip())
    return table


def ancestors(pid=None, table=None, depth=8):
    """[(pid, start)] from the parent of `pid` upwards, stopping before launchd/init."""
    table = process_table() if table is None else table
    pid = os.getpid() if pid is None else pid
    chain = []
    current = table.get(pid, (None, None))[0]
    while current and current > 1 and len(chain) < depth:
        entry = table.get(current)
        if not entry:
            break
        chain.append([current, entry[1]])
        current = entry[0]
    return chain


def parent_key(table=None):
    """The (pid, start) of this process's parent: what an MCP server matches against."""
    chain = ancestors(table=table, depth=1)
    return chain[0] if chain else None
