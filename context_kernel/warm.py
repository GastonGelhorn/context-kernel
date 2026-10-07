"""The background pass a session starts with (`memory warm`): nobody waits for it.

It loads the local judge's model and asks Ollama to keep it (the first prompt then pays no load), runs
the calibration canary when the judge changed or was never checked (until it passes, captures are held
for review), and, in a git repository, reads the repository's decision records. A prompt that finds the
model cold starts it too, without the repository pass.
"""

import time

from .budget import Budget
from .calibration import check, judge_state
from .capture import policy
from .common import KernelError, timestamp_offset
from .judge import JudgeError

STARTED_KEY = "warm_started_at"
CHECK_STALE_SECONDS = 300   # a canary marked running longer than this died with its process


def recently_started(store, seconds=60):
    """Whether a warm-up started in the last `seconds`, so a run of cold prompts starts only one."""
    started = store.meta(STARTED_KEY)
    return bool(started and started > timestamp_offset(store.clock(), -seconds))


def mark_started(store):
    store.meta(STARTED_KEY, store.clock())


def warm(store, judge, workspace=None, session=None, learn_repository=False):
    result = {}
    mark_started(store)
    if judge is not None:
        try:
            duration = str(policy(store).get("keep_alive") or "30m")
            extend = getattr(judge, "keep_alive", None)
            if callable(extend):
                started = time.perf_counter()
                # "off" still loads the model once; Ollama's own default then decides how long it stays.
                result["loaded"] = extend("5m" if duration.lower() in {"off", "0", "no", "false"} else duration, timeout=120)
                result["load_seconds"] = round(time.perf_counter() - started, 2)
            state, key = judge_state(store, judge)
            record = store.judge_record(key) if key else None
            running = record and record["status"] == "running" and \
                record["checked_at"] > timestamp_offset(store.clock(), -CHECK_STALE_SECONDS)
            result["calibration"] = state
            if state in {"changed", "unverified"} and not running:
                result["calibration"] = check(store, judge)["status"]
        except (JudgeError, KernelError) as exc:
            result["judge"] = str(exc)[:200]
    if learn_repository and workspace:
        from .repository import learn, root_of
        root = root_of(workspace)
        if root:
            result["repository"] = learn(store, judge, root, Budget(90), session, if_changed=True)
    return result
