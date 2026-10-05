"""An in-memory judge with the JevCommand interface, for tests that should not spawn processes."""

from context_kernel.judge import JevCommand, JudgeError, JudgeRemote


def answers(affirmed=0.9, category="roles_and_relations", count="one", instruction=0.05, asked=0.9, quoted=None):
    """An `ask` function: fixed probabilities per question id. `quoted` overrides `affirmed` when the
    judged text is the pasted part of the message (the second validation call)."""
    def respond(qid, state, spec):
        if spec[0] == "choice":
            chosen = {"category": category, "count": count}.get(qid, next(iter(spec[2])))
            return {option: (1.0 if option == chosen else 0.0) for option in spec[2]}
        if qid == "affirmed":
            return quoted if quoted is not None and state.get("_quoted") else affirmed
        return {"instruction": instruction, "asked": asked}.get(qid, 0.5)
    return respond


class FakeJudge:
    question = JevCommand.RELEVANCE
    timeout, critical, supporting, band, max_pairs = 10, 0.6, 0.5, 0.35, 48

    def __init__(self, rank=None, ask=None, local=True, fail=None):
        self.rank_fn = rank or (lambda query, line, question: 0.1)
        self.ask_fn = ask or answers()
        self.local, self.fail, self.calls = local, fail, []

    def describe(self):
        return {"url": "http://localhost:11434/v1/systemone" if self.local else "https://api.example.com/v1",
                "model": "fake", "local": self.local}

    def require_local(self, allow_remote=False):
        if not allow_remote and not self.local:
            raise JudgeRemote("hosted backend")

    def rank(self, query, candidates, no_cache=False, timeout=None, question=None):
        self.calls.append(("rank", query, list(candidates), question))
        if self.fail:
            raise JudgeError(self.fail)
        return {i: self.rank_fn(query, c, question or self.question) for i, c in enumerate(candidates)}, {"latency_ms": 1}

    def ask(self, state, questions, no_cache=True, timeout=None):
        self.calls.append(("ask", dict(state), dict(questions)))
        if self.fail:
            raise JudgeError(self.fail)
        return {qid: self.ask_fn(qid, state, spec) for qid, spec in questions.items()}, {"latency_ms": 1}
