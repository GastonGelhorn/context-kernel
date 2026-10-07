"""The judgment client: calibrated probabilities from the `jev` command line.

The kernel depends on this small interface, not on the jevmate product: no hooks, compaction,
or panels are assumed. `jev` (shipped by the `jevmate` package) is the one implementation; the
Jev model it queries is whatever backend jev is configured for. Failure policy and privacy are
decided by the kernel per use, never inherited from jev's own configuration.
"""

import ipaddress
import json
import os
import re
import subprocess
import tempfile
import time
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

from .common import KernelError, canonical, digest, text
from .protocol import parse_json


class JudgeError(KernelError):
    """The judge could not answer. Selection falls back; validated writes do not happen."""


class JudgeRemote(JudgeError):
    """The judge would send memory text to a hosted backend this scope has not authorized."""


def is_local(url):
    parts = urlsplit(url or "")
    host = parts.hostname or ""
    if host == "localhost" or host.endswith(".localhost"):
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def weights(url, model):
    """The digest of the weights behind a local Ollama model name, or None.

    A judgment cache keyed by URL and model name keeps answers from an older model after the same
    alias is pulled again. Only a loopback backend is asked, with a short timeout; any failure
    leaves the fingerprint at None, which still differs from every known digest."""
    if not model or not is_local(url):
        return None
    parts = urlsplit(url)
    try:
        with urlopen(f"{parts.scheme}://{parts.netloc}/api/tags", timeout=1) as response:
            listed = json.load(response).get("models", [])
    except (OSError, ValueError, AttributeError):
        return None
    names = {model, model + ":latest"} if ":" not in model else {model}
    for item in listed if isinstance(listed, list) else ():
        if isinstance(item, dict) and item.get("name") in names and isinstance(item.get("digest"), str):
            return item["digest"][:16]
    return None


def model_key(description):
    """Which judge answered: backend, model name and weights. Thresholds are measured per key."""
    return digest({k: description.get(k) for k in ("url", "model", "weights")})[:24]


def _ollama(url, path, body=None, timeout=1.0):
    parts = urlsplit(url)
    request = Request(f"{parts.scheme}://{parts.netloc}{path}", data=None if body is None else json.dumps(body).encode(),
                      headers={"Content-Type": "application/json"}, method="GET" if body is None else "POST")
    with urlopen(request, timeout=timeout) as response:
        return json.load(response)


def loaded(description, timeout=0.3):
    """Whether a local Ollama backend has the judge's model in memory: True, False, or None when that
    cannot be told (a hosted backend, another server, no answer in time). A cold model takes several
    seconds to load, longer than a prompt hook should wait."""
    url, model = description.get("url"), description.get("model")
    if not model or not is_local(url):
        return None
    try:
        running = _ollama(url, "/api/ps", timeout=timeout).get("models", [])
    except (OSError, ValueError, AttributeError):
        return None
    names = {model, model + ":latest"} if ":" not in model else {model}
    return any(isinstance(m, dict) and (m.get("name") in names or m.get("model") in names) for m in running or ())


def keep_alive(description, duration, timeout=1.0):
    """Ask a local Ollama backend to load the judge's model (if needed) and keep it for `duration`
    ("30m"). An empty generate request only loads the model; jev's own requests reset the timer to
    Ollama's default, so this is repeated after each turn. Returns whether the backend accepted it."""
    url, model = description.get("url"), description.get("model")
    if not model or not is_local(url) or not duration:
        return False
    try:
        _ollama(url, "/api/generate", {"model": model, "keep_alive": duration}, timeout=timeout)
        return True
    except (OSError, ValueError, AttributeError):
        return False


class JevCommand:
    """Judge backed by the `jev` CLI. Memory text and the user's words travel on stdin or in a
    private temporary file, never on argv (visible to every local process) and never as a bare
    `--query` value, which jev would read as a path when it starts with `@`."""

    RELEVANCE = "Is `candidate` a fact that someone answering `query` must take into account?"

    def __init__(self, command="jev", timeout=10, critical=0.6, supporting=0.5, question=None, band=0.35, max_pairs=48):
        if not isinstance(command, str) or not command.strip() or "\0" in command:
            raise KernelError("Invalid jev command.")
        if not 0 < timeout <= 60:
            raise KernelError("jev timeout must be 1-60 seconds.")
        if not (0 < band <= supporting <= critical <= 1):
            raise KernelError("jev thresholds must satisfy 0 < band <= supporting <= critical <= 1.")
        if type(max_pairs) is not int or not 1 <= max_pairs <= 500:
            raise KernelError("jev max pairs must be between 1 and 500.")
        self.command, self.timeout, self.max_pairs = command, timeout, max_pairs
        self.critical, self.supporting, self.band = critical, supporting, band
        self.question = text(question or self.RELEVANCE, 1024)
        self._description = None

    # Backward-compatible name used by traces and tests.
    QUESTION = RELEVANCE

    def _run(self, argv, stdin, timeout=None, query=None):
        limit = min(self.timeout, timeout) if timeout else self.timeout
        if limit <= 0:
            raise JudgeError("No time left for the judge in this turn.")
        started = time.perf_counter()
        folder = None
        try:
            if query is not None:
                # `--query @file`: the text never reaches argv, and a prompt such as "@src/app.py fix
                # this" is sent as written instead of making jev read that file.
                folder = tempfile.mkdtemp(prefix="context-kernel-")  # 0700, readable by this user only
                path = os.path.join(folder, "query.txt")
                with open(path, "w", encoding="utf-8") as handle:
                    handle.write(query)
                argv = [*argv, "--query", "@" + path]
            process = subprocess.run([self.command, *argv], input=stdin, capture_output=True, text=True, timeout=limit)
        except (OSError, subprocess.SubprocessError) as exc:
            raise JudgeError("jev is unavailable or timed out.") from exc
        finally:
            if folder:
                for name in os.listdir(folder):
                    os.unlink(os.path.join(folder, name))
                os.rmdir(folder)
        if process.returncode != 0:
            # jev's last stderr line is its own error record (backend, budget, key status); it never
            # echoes candidates or state. Bounded so the trace explains the fallback.
            detail = (process.stderr or "").strip().splitlines()
            reason = re.sub(r"\s+", " ", detail[-1])[:300] if detail else "no diagnostic output"
            raise JudgeError(f"jev exited with status {process.returncode}: {reason}")
        return parse_json(process.stdout), round((time.perf_counter() - started) * 1000, 3)

    def describe(self):
        """Where jev would send a request, read from a dry run: nothing is sent."""
        if self._description is None:
            result, _ = self._run(["rank", "--dry-run", "--json",
                                   "--instructions", "Is `candidate` relevant to `query`?"], "c\n", timeout=10, query="q")
            url = result.get("url") if isinstance(result, dict) else None
            model = (result.get("body") or {}).get("model") if isinstance(result, dict) else None
            if not isinstance(url, str):
                raise JudgeError("jev did not report its backend.")
            # `weights` is part of every cached judgment's model key: a re-pulled alias starts fresh.
            self._description = {"url": url, "model": model, "local": is_local(url), "weights": weights(url, model)}
        return self._description

    def require_local(self, allow_remote=False):
        if not allow_remote and not self.describe()["local"]:
            raise JudgeRemote("jev points to a hosted backend; memory text stays on this machine unless the scope allows it.")

    def loaded(self):
        """Whether the local model is in memory (see `loaded` above); None when that cannot be told."""
        return loaded(self.describe())

    def keep_alive(self, duration, timeout=1.0):
        """Load the local model if needed and keep it for `duration` (see `keep_alive` above)."""
        return keep_alive(self.describe(), duration, timeout=timeout)

    def rank(self, query, candidates, no_cache=False, timeout=None, question=None):
        """P(yes) per candidate line under `question` (default: relevance); the index is positional."""
        if not isinstance(candidates, list) or not 1 <= len(candidates) <= 500:
            raise KernelError("jev accepts 1-500 candidates per call.")
        if any("\n" in c for c in candidates):
            raise KernelError("jev candidates must be single lines.")
        argv = ["rank", "--json", "--instructions", question or self.question] + (["--no-cache"] if no_cache else [])
        result, latency = self._run(argv, "\n".join(candidates) + "\n", timeout, query=query)
        rows = result.get("results") if isinstance(result, dict) else None
        if not isinstance(rows, list):
            raise JudgeError("Invalid jev response.")
        scores = {}
        for row in rows:
            index, p = (row.get("i"), row.get("p")) if isinstance(row, dict) else (None, None)
            if type(index) is not int or not 0 <= index < len(candidates) or type(p) not in {int, float} or not 0 <= p <= 1:
                raise JudgeError("Invalid jev response.")
            scores[index] = float(p)
        usage = result.get("usage") if isinstance(result.get("usage"), dict) else {}
        return scores, {"input_tokens": usage.get("input_tokens"), "requests": result.get("requests"),
                        "jev_ms": result.get("ms"), "latency_ms": latency}

    def ask(self, state, questions, no_cache=True, timeout=None):
        """Several questions about one state in one request.

        `questions` maps an id to ("noul", instructions) or ("choice", instructions, {option: description}).
        Returns {id: p} for noul and {id: {option: p}} for choice. The request goes on stdin.
        """
        if not isinstance(state, dict) or not isinstance(questions, dict) or not 1 <= len(questions) <= 4:
            raise KernelError("jev ask takes an object state and 1-4 questions.")
        body = {}
        for qid, spec in questions.items():
            if spec[0] == "noul":
                body[qid] = {"type": "noul", "instructions": spec[1]}
            elif spec[0] == "choice":
                body[qid] = {"type": "choice", "instructions": spec[1], "criteria": dict(spec[2])}
            else:
                raise KernelError("Unsupported jev question type.")
        argv = ["ask", "--json", "-f", "-"] + (["--no-cache"] if no_cache else [])
        result, latency = self._run(argv, canonical({"state": state, "questions": body}), timeout)
        answers = result.get("answers") if isinstance(result, dict) else None
        if not isinstance(answers, dict):
            raise JudgeError("Invalid jev response.")
        out = {}
        for qid, spec in questions.items():
            answer = answers.get(qid)
            if not isinstance(answer, dict):
                raise JudgeError("Invalid jev response.")
            if spec[0] == "noul":
                p = answer.get("noul")
                if type(p) not in {int, float} or not 0 <= p <= 1:
                    raise JudgeError("Invalid jev response.")
                out[qid] = float(p)
            else:
                probabilities = answer.get("probabilities")
                if not isinstance(probabilities, dict) or set(probabilities) - set(spec[2]):
                    raise JudgeError("Invalid jev response.")
                out[qid] = {k: float(v) for k, v in probabilities.items()}
        usage = result.get("usage") if isinstance(result.get("usage"), dict) else {}
        return out, {"input_tokens": usage.get("input_tokens"), "latency_ms": latency}
