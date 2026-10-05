"""The judgment client: calibrated probabilities from the `jev` command line.

The kernel depends on this small interface, not on the jevmate product: no hooks, compaction,
or panels are assumed. `jev` (shipped by the `jevmate` package) is the one implementation; the
Jev model it queries is whatever backend jev is configured for. Failure policy and privacy are
decided by the kernel per use, never inherited from jev's own configuration.
"""

import ipaddress
import re
import subprocess
import time
from urllib.parse import urlsplit

from .common import KernelError, canonical, text
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


class JevCommand:
    """Judge backed by the `jev` CLI, run as a subprocess with text on stdin, never on argv."""

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

    def _run(self, argv, stdin, timeout=None):
        limit = min(self.timeout, timeout) if timeout else self.timeout
        if limit <= 0:
            raise JudgeError("No time left for the judge in this turn.")
        started = time.perf_counter()
        try:
            process = subprocess.run([self.command, *argv], input=stdin, capture_output=True, text=True, timeout=limit)
        except (OSError, subprocess.SubprocessError) as exc:
            raise JudgeError("jev is unavailable or timed out.") from exc
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
            result, _ = self._run(["rank", "--dry-run", "--json", "--query", "q",
                                   "--instructions", "Is `candidate` relevant to `query`?"], "c\n", timeout=10)
            url = result.get("url") if isinstance(result, dict) else None
            model = (result.get("body") or {}).get("model") if isinstance(result, dict) else None
            if not isinstance(url, str):
                raise JudgeError("jev did not report its backend.")
            self._description = {"url": url, "model": model, "local": is_local(url)}
        return self._description

    def require_local(self, allow_remote=False):
        if not allow_remote and not self.describe()["local"]:
            raise JudgeRemote("jev points to a hosted backend; memory text stays on this machine unless the scope allows it.")

    def rank(self, query, candidates, no_cache=False, timeout=None, question=None):
        """P(yes) per candidate line under `question` (default: relevance); the index is positional."""
        if not isinstance(candidates, list) or not 1 <= len(candidates) <= 500:
            raise KernelError("jev accepts 1-500 candidates per call.")
        if any("\n" in c for c in candidates):
            raise KernelError("jev candidates must be single lines.")
        argv = ["rank", "--json", "--query", query, "--instructions", question or self.question] + (["--no-cache"] if no_cache else [])
        result, latency = self._run(argv, "\n".join(candidates) + "\n", timeout)
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
