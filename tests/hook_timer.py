"""Runs a hook command unchanged and appends how long it took: hook_timer.py LOG EVENT COMMAND..."""

import json
import subprocess
import sys
import time


def main():
    log, event, command = sys.argv[1], sys.argv[2], sys.argv[3:]
    started = time.perf_counter()
    code = subprocess.call(command)  # stdin and stdout pass through to the host
    with open(log, "a", encoding="utf-8") as handle:
        handle.write(json.dumps({"event": event, "ms": round((time.perf_counter() - started) * 1000), "exit": code}) + "\n")
    return code


if __name__ == "__main__":
    raise SystemExit(main())
