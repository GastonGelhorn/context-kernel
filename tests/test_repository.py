"""Decisions the repository already records: decision records and decision commits, without a command."""

import os
from pathlib import Path
import subprocess
import tempfile
import unittest

from context_kernel.adapters import session_start_response, stop_response
from context_kernel.common import timestamp, timestamp_offset
from context_kernel.compiler import Compiler
from context_kernel.inference import stale_recommendations
from context_kernel.repository import (COMMIT_BAR, DECISION_COMMIT, REPLACES, decision_language, inside_repository,
                                       learn, parse_record, record_files, root_of, routine, signature)
from context_kernel.store import Store
from tests.fakes import FakeJudge


RECORD = """# {number}. {title}

Date: 2026-09-01

## Status

{status}

## Context

The queue must survive restarts.

## Decision

{decision}

## Consequences

Backups are a copy of one file.
"""


class RepositoryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / "acme-api"
        self.root.mkdir()
        self.now = "2026-10-05T12:00:00+00:00"
        self.store = Store(Path(self.temp.name) / "memory.sqlite", scope="work", clock=lambda: timestamp(self.now), create=True)
        self.judge = FakeJudge(ask=lambda qid, state, spec: 0.9 if "Postgres" in state.get("commit", "") else 0.2)
        self.git("init", "-q", "-b", "main")

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def git(self, *args, date="2026-10-01T10:00:00+00:00", author="Ana", email="ana@example.com"):
        env = dict(os.environ, GIT_AUTHOR_NAME=author, GIT_AUTHOR_EMAIL=email, GIT_COMMITTER_NAME=author,
                   GIT_COMMITTER_EMAIL=email, GIT_AUTHOR_DATE=date, GIT_COMMITTER_DATE=date,
                   GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM="1")
        return subprocess.run(["git", "-C", str(self.root), *args], check=True, capture_output=True, text=True, env=env).stdout

    def write(self, path, content):
        target = self.root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")

    def commit(self, subject, body="", date="2026-10-01T10:00:00+00:00", **who):
        self.git("add", "-A")
        self.git("commit", "-q", "--allow-empty", "-m", subject, *(["-m", body] if body else []), date=date, **who)
        return self.git("rev-parse", "HEAD").strip()

    def record(self, number, title, status="Accepted", decision="We will do it."):
        slug = title.lower().replace(" ", "-")
        self.write(f"docs/adr/{number:04d}-{slug}.md", RECORD.format(number=number, title=title, status=status, decision=decision))

    def learn(self, judge=None, session=None):
        self.now = timestamp_offset(self.now, 60)
        return learn(self.store, judge if judge is not None else self.judge, str(self.root), session=session)

    def facts(self, history=False):
        rows = self.store.records(history=history)
        return {r["predicate"]: r for r in rows if r["entity_key"] == "acme_api"}

    # Decision records

    def test_parser_reads_nygard_madr_and_spanish_records(self):
        nygard = parse_record(RECORD.format(number=2, title="Use SQLite for the queue", status="Superseded by "
                              "[4. Use Postgres](0004-use-postgres.md)", decision="We will use SQLite."), "docs/adr/0002-use-sqlite.md")
        self.assertEqual((nygard["number"], nygard["title"], nygard["status"], nygard["successor"], nygard["decision"]),
                         (2, "Use SQLite for the queue", "superseded", 4, "We will use SQLite."))
        madr = parse_record("---\nstatus: accepted\ndate: 2026-01-01\n---\n# Use MADR\n\n## Decision Outcome\n\n"
                            "Chosen option: \"MADR 4\", because it is lean.\n", "docs/decisions/0005-use-madr.md")
        self.assertEqual((madr["number"], madr["status"], madr["decision"]), (5, "accepted", 'Chosen option: "MADR 4", because it is lean.'))
        bullets = parse_record("# Use Redis\n\n* Status: deprecated\n* Date: 2026-01-01\n", "adr/0007-use-redis.md")
        self.assertEqual(bullets["status"], "retired")
        spanish = parse_record("# 3. Usar pnpm\n\n## Estado\n\nPropuesto\n\n## Decisión\n\nUsaremos pnpm.\n", "docs/adr/0003-usar-pnpm.md")
        self.assertEqual((spanish["title"], spanish["status"], spanish["decision"]), ("Usar pnpm", "proposed", "Usaremos pnpm."))
        self.assertIsNone(parse_record("no heading here", "docs/adr/notes.md"))

    def test_only_records_in_decision_folders_are_read(self):
        self.record(1, "Use SQLite for the queue")
        self.write("docs/adr/template.md", "# Title\n")
        self.write("docs/adr/README.md", "# Decisions\n")
        self.write("docs/notes/0001-idea.md", "# Idea\n")
        self.write("node_modules/pkg/docs/adr/0001-x.md", "# X\n")
        self.commit("Initial")
        self.assertEqual([p for p, _ in record_files(self.root)], ["docs/adr/0001-use-sqlite-for-the-queue.md"])

    def test_accepted_records_are_learned_with_their_source_and_proposed_ones_are_not(self):
        self.record(1, "Use SQLite for the queue", decision="We will use SQLite as the job queue.")
        self.record(2, "Adopt GraphQL", status="Proposed")
        self.commit("Initial")
        summary = self.learn()
        facts = self.facts()
        self.assertEqual(list(facts), ["adr_0001"])
        fact = facts["adr_0001"]
        self.assertEqual(fact["value"], "Use SQLite for the queue: We will use SQLite as the job queue.")
        self.assertEqual((fact["source_kind"], fact["source_ref"], fact["assertion_kind"], fact["trust"], fact["category"]),
                         ("repository", "docs/adr/0001-use-sqlite-for-the-queue.md", "observed", "captured", "project_decisions"))
        self.assertEqual(summary["learned"], ["ADR 1"])
        self.assertIn("learned 1 decision(s) from the repository (ADR 1)", summary["note"])
        # Nothing changed: nothing is learned twice and nothing is judged.
        self.judge.calls.clear()
        again = self.learn()
        self.assertEqual((again["learned"], again["changed"], len(self.facts())), ([], [], 1))
        self.assertEqual(self.judge.calls, [])

    def test_a_superseded_record_flags_what_rested_on_it_and_points_at_its_successor(self):
        self.record(2, "Use SQLite for the queue")
        self.commit("Initial")
        self.learn()
        old = self.facts()["adr_0002"]
        with self.store.db:
            advice = self.store._insert("acme_api", "recommendation", "Keep the queue in one file.", "reply",
                                        assertion_kind="inference", source_kind="agent_reply", trust="captured")
        self.store.depend(advice, old["id"], provenance="inferred")
        self.record(2, "Use SQLite for the queue", status="Superseded by [4. Use Postgres](0004-use-postgres-for-the-queue.md)")
        self.record(4, "Use Postgres for the queue")
        self.commit("Move the queue to Postgres", date="2026-10-02T10:00:00+00:00")
        summary = self.learn()
        self.assertEqual((summary["learned"], summary["superseded"]), (["ADR 4"], ["ADR 2"]))
        history = {r["id"]: r for r in self.store.records(history=True)}
        new = self.facts()["adr_0004"]
        self.assertEqual((history[old["id"]]["effective_state"], history[old["id"]]["superseded_by"]), ("superseded", new["id"]))
        self.assertEqual([r["id"] for r in stale_recommendations(list(history.values()))], [advice])
        # The successor is current, so the recommendation can be reaffirmed against it.
        self.assertEqual(self.store.reaffirm(advice)["moved"], [{"from": old["id"], "to": new["id"]}])

    def test_deprecated_and_deleted_records_stop_being_evidence(self):
        self.record(1, "Use SQLite for the queue")
        self.record(2, "Use Redis for sessions")
        self.commit("Initial")
        self.learn()
        self.record(1, "Use SQLite for the queue", status="Deprecated")
        (self.root / "docs/adr/0002-use-redis-for-sessions.md").unlink()
        self.commit("Retire two decisions", date="2026-10-02T10:00:00+00:00")
        summary = self.learn()
        self.assertEqual(sorted(summary["retired"]), ["0002-use-redis-for-sessions", "ADR 1"])
        self.assertEqual(self.facts(), {})

    def test_a_changed_record_is_a_new_version(self):
        self.record(1, "Use SQLite for the queue", decision="We will use SQLite.")
        self.commit("Initial")
        self.learn()
        first = self.facts()["adr_0001"]
        self.record(1, "Use SQLite for the queue", decision="We will use SQLite with WAL mode.")
        self.commit("Clarify the queue decision", date="2026-10-02T10:00:00+00:00")
        self.assertEqual(self.learn()["changed"], ["ADR 1"])
        current = self.facts()["adr_0001"]
        self.assertNotEqual(current["id"], first["id"])
        self.assertIn("WAL", current["value"])

    def test_a_forgotten_record_stays_forgotten_until_the_record_changes(self):
        self.record(1, "Use SQLite for the queue")
        self.commit("Initial")
        self.learn()
        self.store.forget(self.facts()["adr_0001"]["id"])
        self.commit("Unrelated", date="2026-10-02T10:00:00+00:00")
        self.learn()
        self.assertEqual(self.facts(), {})
        self.record(1, "Use SQLite for the queue", decision="We will use SQLite, revisited.")
        self.commit("Revisit the queue", date=timestamp_offset(self.now, 3600))
        self.now = timestamp_offset(self.now, 7200)
        self.learn()
        self.assertIn("revisited", self.facts()["adr_0001"]["value"])

    # Decision commits

    def test_a_commit_that_writes_a_decision_record_is_not_a_second_decision(self):
        self.record(4, "Use Postgres for the queue")
        self.commit("Use Postgres for the queue")
        self.learn()
        self.assertEqual(list(self.facts()), ["adr_0004"])
        self.assertEqual(self.judge.calls, [])

    def test_decision_commits_are_judged_and_routine_ones_never_reach_the_judge(self):
        self.commit("Fix typo in README")
        self.commit("chore: bump dependencies")
        self.commit("Use Postgres for the analytics store", body="MySQL cannot do window functions we need.",
                    date="2026-10-02T10:00:00+00:00")
        self.commit("Add logging to the payment worker", date="2026-10-03T10:00:00+00:00")
        self.commit("Use the correct path in the upload test", date="2026-10-03T10:30:00+00:00")
        self.commit("Adopt Renovate", author="renovate[bot]", email="bot@example.com", date="2026-10-03T11:00:00+00:00")
        summary = self.learn()
        asked = [c for c in self.judge.calls if c[0] == "ask"]
        # Finished work without the language of a decision never reaches the judge; a routine change
        # worded like one does, one commit per request, and the judge turns it down.
        self.assertEqual(sorted(c[1]["commit"] for c in asked), ["Use Postgres for the analytics store",
                                                                 "Use the correct path in the upload test"])
        self.assertEqual({c[2]["decision"][1] for c in asked}, {DECISION_COMMIT})
        facts = self.facts()
        self.assertEqual([f["value"] for f in facts.values()], ["Use Postgres for the analytics store"])
        fact = next(iter(facts.values()))
        self.assertTrue(fact["source_ref"].startswith("commit ") and fact["source_ref"].endswith("2026-10-02"))
        self.assertIn("window functions", fact["source_text"])
        self.assertEqual(summary["judged"], 2)
        self.judge.calls.clear()
        self.learn()
        self.assertEqual(self.judge.calls, [])

    def test_a_revert_retires_the_decision_and_a_rewrite_does_not_duplicate_it(self):
        sha = self.commit("Use Postgres for the analytics store")
        self.learn()
        self.assertEqual(len(self.facts()), 1)
        self.git("commit", "-q", "--amend", "--allow-empty", "-m", "Use Postgres for the analytics store",
                 date="2026-10-01T10:00:01+00:00")
        self.learn()
        self.assertEqual(len(self.facts()), 1)
        current = self.git("rev-parse", "HEAD").strip()
        self.assertNotEqual(current, sha)
        self.commit('Revert "Use Postgres for the analytics store"', body=f"This reverts commit {current}.",
                    date="2026-10-02T10:00:00+00:00")
        summary = self.learn()
        self.assertEqual(self.facts(), {})
        self.assertTrue(summary["retired"])

    def replacing_judge(self):
        """Every queue subject is a decision; Redis replaces SQLite and nothing else replaces anything."""
        return FakeJudge(ask=lambda qid, state, spec: 0.9 if any(w in state.get("commit", "") for w in ("queue", "pnpm")) else 0.2,
                         rank=lambda query, line, question: 0.9 if question == REPLACES and "Redis" in query and "SQLite" in line
                         else 0.1)

    def test_a_later_decision_commit_ends_the_earlier_one_it_replaces(self):
        old = self.commit("Use SQLite for the job queue")
        self.learn(self.replacing_judge())
        sqlite = self.facts()
        with self.store.db:
            advice = self.store._insert("acme_api", "recommendation", "Keep jobs in one file.", "reply",
                                        assertion_kind="inference", source_kind="agent_reply", trust="captured")
        self.store.depend(advice, next(iter(sqlite.values()))["id"], provenance="inferred")
        self.commit("Use Redis for the job queue", date="2026-10-02T10:00:00+00:00")
        summary = self.learn(self.replacing_judge())
        self.assertEqual(summary["superseded"], [f"commit {old[:7]}"])
        self.assertEqual([f["value"] for f in self.facts().values()], ["Use Redis for the job queue"])
        history = {r["id"]: r for r in self.store.records(history=True)}
        self.assertTrue(history[advice]["stale"])

    def test_an_older_decision_learned_late_is_ended_by_the_later_one(self):
        from context_kernel import repository
        self.commit("Use SQLite for the job queue")
        self.commit("Use Redis for the job queue", date="2026-10-02T10:00:00+00:00")
        limit, repository.JUDGED_PER_PASS = repository.JUDGED_PER_PASS, 1
        try:
            self.assertEqual(self.learn(self.replacing_judge())["more"], 1)  # the newest first
            self.learn(self.replacing_judge())
        finally:
            repository.JUDGED_PER_PASS = limit
        self.assertEqual([f["value"] for f in self.facts().values()], ["Use Redis for the job queue"])

    def test_decisions_on_different_topics_are_never_compared(self):
        judge = self.replacing_judge()
        self.commit("Use SQLite for the job queue")
        self.commit("Adopt pnpm as the package manager", date="2026-10-02T10:00:00+00:00")
        self.learn(judge)
        self.assertEqual(len(self.facts()), 2)
        self.assertEqual([c for c in judge.calls if c[0] == "rank"], [])

    def test_a_decision_reverted_within_the_same_window_is_never_judged(self):
        sha = self.commit("Use Postgres for the analytics store")
        self.commit('Revert "Use Postgres for the analytics store"', body=f"This reverts commit {sha}.",
                    date="2026-10-02T10:00:00+00:00")
        self.learn()
        self.assertEqual(self.facts(), {})
        self.assertEqual(self.judge.calls, [])

    def test_without_a_local_judge_records_are_still_read_and_commits_wait(self):
        self.record(1, "Use SQLite for the queue")
        self.commit("Initial")
        self.commit("Use Postgres for the analytics store", date="2026-10-02T10:00:00+00:00")
        summary = self.learn(judge=FakeJudge(local=False))
        self.assertEqual((list(self.facts()), summary["skipped"]), (["adr_0001"], ["judge_remote"]))
        failing = self.learn(judge=FakeJudge(fail="down"))
        self.assertEqual(failing["skipped"], ["judge_unavailable"])
        # An unanswered pass leaves the signature unset, so the next session tries again.
        self.assertIsNone(self.store.repository(str(self.root.resolve()))["signature"])

    def test_the_scope_can_turn_repository_learning_off(self):
        self.record(1, "Use SQLite for the queue")
        self.commit("Initial")
        self.store.set_policy({"repository": False})
        self.assertEqual(self.learn(), {"skipped": ["policy"]})
        self.assertEqual(self.facts(), {})

    def test_chat_captures_do_not_drift_onto_a_repository_record(self):
        from context_kernel.capture import _resolve
        self.record(1, "Use SQLite for the queue")
        self.commit("Initial")
        self.learn()
        same = FakeJudge(rank=lambda query, line, question: 0.95)
        self.assertEqual(_resolve(self.store, same, "acme_api", "adr_queue", None, None), ("acme_api", "adr_queue", None))

    # Hooks

    def session_start(self, session="s1", calls=None):
        event = {"cwd": str(self.root), "hook_event_name": "SessionStart", "session_id": session}
        return session_start_response(event, self.root, self.store, learn=lambda root, s: calls.append((root, s)))

    def test_a_session_start_starts_a_pass_that_returns_at_once_when_nothing_changed(self):
        self.record(1, "Use SQLite for the queue")
        self.commit("Initial")
        (self.root / "docs").mkdir(exist_ok=True)
        calls = []
        session_start_response({"cwd": str(self.root / "docs"), "hook_event_name": "SessionStart", "session_id": "s1"},
                               self.root, self.store, learn=lambda cwd, s: calls.append((cwd, s)))
        self.assertEqual(calls, [(str(self.root / "docs"), "s1")])
        self.assertEqual(learn(self.store, self.judge, str(self.root), session="s1", if_changed=True)["learned"], ["ADR 1"])
        self.assertEqual(learn(self.store, self.judge, str(self.root), if_changed=True), {"skipped": ["unchanged"]})
        self.commit("Use Postgres for the analytics store", date="2026-10-02T10:00:00+00:00")
        self.assertEqual(learn(self.store, self.judge, str(self.root), if_changed=True)["learned"], ["commit " + self.git(
            "rev-parse", "--short=7", "HEAD").strip()])
        self.store.set_policy({"repository": False})
        self.session_start("s4", calls)
        self.assertEqual(len(calls), 1)

    def test_a_session_outside_any_repository_starts_nothing(self):
        plain = Path(self.temp.name) / "plain"
        plain.mkdir()
        calls = []
        session_start_response({"cwd": str(plain), "hook_event_name": "SessionStart", "session_id": "s1"}, plain, self.store,
                               learn=lambda root, s: calls.append(root))
        self.assertEqual((calls, root_of(plain), signature(plain), inside_repository(plain)), ([], None, None, False))

    def test_what_was_learned_is_said_once_at_the_next_stop(self):
        self.record(1, "Use SQLite for the queue")
        self.commit("Initial")
        self.session_start(calls=[])
        learn(self.store, self.judge, str(self.root), session="s1")
        stop = {"cwd": str(self.root), "hook_event_name": "Stop", "session_id": "s1", "last_assistant_message": ""}
        from context_kernel.adapters import hook_response
        hook_response({"cwd": str(self.root), "prompt": "Explain SQLite", "session_id": "s1", "prompt_id": "p1"},
                      self.root, self.store, Compiler(self.store))
        self.assertIn("learned 1 decision(s) from the repository", stop_response(stop, self.root, self.store)["systemMessage"])
        hook_response({"cwd": str(self.root), "prompt": "Explain WAL", "session_id": "s1", "prompt_id": "p2"},
                      self.root, self.store, Compiler(self.store))
        self.assertNotIn("repository", stop_response(stop | {"prompt_id": "p2"}, self.root, self.store).get("systemMessage", ""))

    def test_repository_claims_carry_their_source(self):
        self.record(1, "Use SQLite for the queue")
        self.commit("Initial")
        self.learn()
        judge = FakeJudge(rank=lambda query, line, question: 0.9 if "SQLite" in line else 0.1)
        projection = Compiler(self.store, jev=judge).project("Which queue do we use in acme-api?", "jev")
        claim = __import__("json").loads(projection.content)["claims"][0]
        self.assertEqual((claim["attribution"], claim["source"]), ("repository", "docs/adr/0001-use-sqlite-for-the-queue.md"))

    def test_routine_filter_and_bar(self):
        for subject in ("Fix typo in README", "Release v1.2.0", "Bump lodash", "docs: explain", "Add ADR 0007",
                        "Update dependencies", "Actualizar dependencias", "Merge branch 'main'", "1.2.0"):
            self.assertTrue(routine(subject), subject)
        for subject in ("Use pnpm", "feat: use pnpm", "Release under the MIT license", "Usar SQLite para la cola"):
            self.assertFalse(routine(subject), subject)
            self.assertTrue(decision_language(subject), subject)
        for subject in ("Add logging to the payment worker", "Harden retrieval and hook delivery", "Record the 10/10 run"):
            self.assertFalse(decision_language(subject), subject)
        self.assertEqual(COMMIT_BAR, 0.5)


if __name__ == "__main__":
    unittest.main()
