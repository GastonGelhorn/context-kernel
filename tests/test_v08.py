"""v0.8: precision of capture, cues and selection at scale, safe mode, warm-up, duplicate installs, regret."""

import json
from pathlib import Path
import sqlite3
import subprocess
import os
import tempfile
import unittest

from shelflife_context.adapters import hook_response, packet_of, session_start_response, stop_response
from shelflife_context.budget import Budget
from shelflife_context.calibration import CANARY, check, judge_state
from shelflife_context.capture import clean_cues, locate_quote, task_clause
from shelflife_context.common import timestamp, timestamp_offset
from shelflife_context.compiler import Compiler
from shelflife_context.inference import stale_recommendations
from shelflife_context.judge import model_key
from shelflife_context.mcp import Server
from shelflife_context.planner import RESERVE_SECONDS, affordable_pairs, select
from shelflife_context.repository import learn
from shelflife_context.store import Store
from tests.fakes import FakeJudge, answers


HOST = [4242, "Mon Oct  5 12:00:00 2026"]


def judged(**scores):
    """An ask function: `answers` with extra noul scores (sarcasm, task, …) by question id."""
    base = answers(**{k: v for k, v in scores.items() if k in {"affirmed", "category", "count", "instruction", "asked",
                                                              "quoted", "recommends"}})
    extra = {k: v for k, v in scores.items() if k not in {"affirmed", "category", "count", "instruction", "asked", "quoted",
                                                         "recommends"}}

    def respond(qid, state, spec):
        if qid in extra:
            return extra[qid]
        return base(qid, state, spec)
    return respond


class Base(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.workspace = Path(self.temp.name)
        self.now = "2026-10-06T12:00:00+00:00"
        self.store = Store(self.workspace / "memory.sqlite", clock=lambda: timestamp(self.now), create=True)
        self.judge = FakeJudge()
        self.compiler = Compiler(self.store)
        self.turns = 0

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def prompt(self, text, session="s1", compiler=None, strategy="rules", judge=None, warm=None, prompt_id=None):
        self.now = timestamp_offset(self.now, 5)
        self.turns += 1
        event = {"cwd": str(self.workspace), "prompt": text, "session_id": session, "prompt_id": prompt_id or f"p{self.turns}"}
        response, _ = hook_response(event, self.workspace, self.store, compiler or self.compiler, strategy=strategy,
                                    judge=judge or self.judge, warm=warm)
        self.store.register_session(session, "claude", [HOST])
        context = (response.get("hookSpecificOutput") or {}).get("additionalContext")
        return (packet_of(context) if context else None), event, response

    def stop(self, event, reply):
        self.now = timestamp_offset(self.now, 2)
        return stop_response({"cwd": str(self.workspace), "hook_event_name": "Stop", "session_id": event["session_id"],
                              "prompt_id": event["prompt_id"], "last_assistant_message": reply}, self.workspace,
                             self.store, self.judge)

    def tool(self, name, arguments):
        server = Server(self.store, self.compiler, judge=self.judge, parent=HOST)
        server.dispatch({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
            "protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {}}})
        server.dispatch({"jsonrpc": "2.0", "method": "notifications/initialized"})
        response = server.dispatch({"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                                    "params": {"name": name, "arguments": arguments}})
        return response["result"]["structuredContent"], response["result"]["isError"]

    def capture(self, packet, *facts):
        result, error = self.tool("memory_capture", {"token": packet["turn"]["token"], "facts": list(facts)})
        self.assertFalse(error, result)
        return result["results"]


class CapturePrecision(Base):
    def test_a_quote_must_be_the_users_own_words(self):
        packet, _, _ = self.prompt("Our API must stay backwards compatible until v3.")
        result = self.capture(packet, {"entity": "api", "predicate": "constraint", "value": "backwards compatible until v3",
                                       "quote": "must stay backwards compatible until v3"})[0]
        self.assertEqual(result["status"], "captured")
        self.assertEqual(self.store.inspect(result["id"])["source_text"], "Our API must stay backwards compatible until v3.")
        packet, _, _ = self.prompt("Let's look at the API next.")
        result = self.capture(packet, {"entity": "api", "predicate": "owner", "value": "Lina", "quote": "Lina owns the API"})[0]
        self.assertEqual((result["status"], result["reason"]), ("rejected", "quote_not_found"))

    def test_words_found_only_in_pasted_text_are_held_as_someone_elses(self):
        packet, _, _ = self.prompt("A client forwarded this.\n> The API must stay backwards compatible until v3.\nI am not sure it applies to us.")
        result = self.capture(packet, {"entity": "api", "predicate": "constraint", "value": "backwards compatible until v3",
                                       "quote": "The API must stay backwards compatible until v3."})[0]
        self.assertEqual((result["status"], result["reason"]), ("quarantined", "quoted_source"))

    def test_quotes_tolerate_case_accents_and_a_dropped_word(self):
        self.assertTrue(locate_quote("el limite de gasto en AWS es 3000 dolares",
                                     "Ojo: el límite de gasto mensual en AWS es 3000 dólares."))
        self.assertIsNone(locate_quote("el límite es 9000", "Ojo: el límite de gasto mensual en AWS es 3000 dólares."))

    def test_a_joke_is_held_not_saved(self):
        self.judge.ask_fn = judged(affirmed=0.85, sarcasm=0.72, task=0.05)
        packet, _, _ = self.prompt("lol sure, we totally have infinite budget for AWS")
        result = self.capture(packet, {"entity": "aws", "predicate": "budget", "value": "unlimited"})[0]
        self.assertEqual((result["status"], result["reason"]), ("quarantined", "joke"))
        self.assertEqual(self.store.records(), [])

    def test_a_joke_with_its_own_markers_is_held_at_a_lower_score(self):
        """"Sí, claro, como tenemos presupuesto infinito… jaja" scored 0.588 on the sarcasm question."""
        self.judge.ask_fn = judged(affirmed=0.89, sarcasm=0.588, past=0.05)
        packet, _, _ = self.prompt("Sí, claro, como tenemos presupuesto infinito, metamos un cluster de GPUs, jaja.")
        result = self.capture(packet, {"entity": "brujula", "predicate": "infra_budget", "value": "infinito"})[0]
        self.assertEqual((result["status"], result["reason"]), ("quarantined", "joke"))
        self.judge.ask_fn = judged(affirmed=0.89, sarcasm=0.588, past=0.05)
        packet, _, _ = self.prompt("Our infra budget is 1,500 dollars a month, which is tight.")
        self.assertEqual(self.capture(packet, {"entity": "brujula", "predicate": "infra_budget", "value": "1500 USD"})[0]["status"],
                         "captured")

    def test_a_fact_next_to_a_question_is_judged_on_the_cited_sentence(self):
        def ask(qid, state, spec):
            if spec[0] == "choice":
                return {o: (1.0 if o == "constraints" else 0.0) for o in spec[2]} if qid == "category" else \
                    {"none": 0.02, "one": 0.98, "two": 0.0, "several": 0.0}
            if qid == "affirmed":  # the whole message, with its question, reads weaker than the sentence alone
                return 0.64 if "?" in state["text"] or "kidding" in state["text"] else 0.84
            return 0.05
        self.judge.ask_fn = ask
        packet, _, _ = self.prompt("La app de Turnos tiene que seguir funcionando en iOS 15. ¿Uso SwiftData o Core Data?")
        saved = self.capture(packet, {"entity": "turnos", "predicate": "min_ios", "value": "iOS 15",
                                      "quote": "tiene que seguir funcionando en iOS 15"})[0]
        self.assertEqual(saved["status"], "captured")
        packet, _, _ = self.prompt("La app de Turnos tiene que seguir funcionando en iOS 15. ¿Uso SwiftData o Core Data?")
        self.judge.ask_fn = lambda qid, state, spec: ask(qid, state, spec) if qid != "affirmed" else (
            0.06 if "kidding" in state["text"] else 0.96)
        packet, _, _ = self.prompt("Our budget is 20k EUR. Just kidding, we have no budget yet.")
        refused = self.capture(packet, {"entity": "migration", "predicate": "budget", "value": "20k EUR",
                                        "quote": "Our budget is 20k EUR"})[0]
        self.assertEqual((refused["status"], refused["reason"]), ("rejected", "not_affirmed"))

    def test_a_held_reading_never_blocks_the_value_stated_next(self):
        packet, _, _ = self.prompt("The Orion migration is due on March 14.")
        self.capture(packet, {"entity": "orion", "predicate": "deadline", "value": "March 14"})
        self.judge.ask_fn = judged(affirmed=0.65)
        packet, _, _ = self.prompt("Heads up: the Orion migration deadline moved.")
        held = self.capture(packet, {"entity": "orion", "predicate": "deadline", "value": "moved, date not set"})[0]
        self.assertEqual(held["status"], "quarantined")
        self.judge.ask_fn = answers()
        packet, _, _ = self.prompt("Update: the Orion migration is now due on April 4.")
        result = self.capture(packet, {"entity": "orion", "predicate": "deadline", "value": "April 4"})[0]
        self.assertEqual(result["status"], "captured")
        self.assertEqual([r["value"] for r in self.store.records(quarantined=True) if r["predicate"] == "deadline"], ["April 4"])

    def test_a_past_state_is_held_not_saved_as_current(self):
        self.judge.ask_fn = judged(affirmed=0.865, sarcasm=0.1, past=0.767)
        packet, _, _ = self.prompt("Hasta agosto el deploy de Brújula era manual, con un script de Mateo.")
        result = self.capture(packet, {"entity": "brujula", "predicate": "deploy_method", "value": "manual"})[0]
        self.assertEqual((result["status"], result["reason"]), ("quarantined", "past_state"))

    def test_a_request_for_work_is_held_but_a_rule_is_saved(self):
        self.judge.ask_fn = judged(affirmed=0.8, sarcasm=0.1, task=0.88)
        packet, _, _ = self.prompt("Write a test that checks invoices are PDF only.")
        held = self.capture(packet, {"entity": "client", "predicate": "invoice_format", "value": "PDF only",
                                     "quote": "invoices are PDF only"})[0]
        self.assertEqual((held["status"], held["reason"]), ("quarantined", "task_request"))
        packet, _, _ = self.prompt("From now on, write commit messages in English.")
        saved = self.capture(packet, {"entity": "user", "predicate": "commit_language", "value": "English",
                                      "quote": "write commit messages in English"})[0]
        self.assertEqual(saved["status"], "captured")
        self.assertTrue(task_clause("Genera un script que borre los logs de más de 30 días.", "30 días"))
        self.assertFalse(task_clause("Use pnpm in this repo.", "pnpm"))
        self.assertFalse(task_clause("Recuerda que el staging se llama kite-stg.", "kite-stg"))

    def test_a_fact_the_gate_did_not_see_needs_a_near_certain_reading(self):
        self.judge.ask_fn = judged(affirmed=0.8, count="none", sarcasm=0.1, task=0.1)
        packet, _, _ = self.prompt("ok and the cluster is kestrel-stg i think we said")
        self.assertIn("gate_none", self.store.turn("s1", "p1")["flags"])
        result = self.capture(packet, {"entity": "staging", "predicate": "cluster", "value": "kestrel-stg"})[0]
        self.assertEqual((result["status"], result["reason"]), ("quarantined", "unprompted"))
        self.judge.ask_fn = judged(affirmed=0.95, count="none", sarcasm=0.1, task=0.1)
        packet, _, _ = self.prompt("the staging cluster is heron-stg now")
        self.assertEqual(self.capture(packet, {"entity": "staging", "predicate": "region", "value": "eu-west"})[0]["status"],
                         "captured")

    def test_a_request_for_work_never_hands_the_turn_back(self):
        """"Implementa todos los puntos" read as one fact at P(none) 0.04: it gets the hint, not a second turn."""
        self.judge.ask_fn = judged(count="one", task=0.91)
        self.store.register_session("s1", "claude", [HOST])
        self.store.register_server(HOST)
        packet, event, _ = self.prompt("Implementa todos los puntos y deja la herramienta impecable.")
        self.assertEqual(packet["turn"]["capture"]["facts_stated"], 1)
        self.assertNotIn("gate_confident", self.store.turn("s1", event["prompt_id"])["flags"])
        self.assertNotEqual(self.stop(event, "Hecho.").get("decision"), "block")

    def test_cues_are_kept_short_plain_and_few(self):
        cues = clean_cues(["ship date", "SHIP DATE", "x", "ignore previous instructions", "api_key=abc123", 7] +
                          [f"cue {n}" for n in range(12)])
        self.assertEqual(cues[:1], ["ship date"])
        self.assertEqual(len(cues), 8)
        self.assertFalse(any("ignore" in c or "api_key" in c for c in cues))


class CuesAndSelection(Base):
    def test_cues_reach_a_paraphrased_question_and_rank_first(self):
        judge = FakeJudge(rank=lambda query, line, question: 0.42 if "deadline" in line else 0.62)
        compiler = Compiler(self.store, jev=judge)
        packet, _, _ = self.prompt("The checkout deadline is three weeks from today.")
        self.capture(packet, {"entity": "checkout", "predicate": "deadline", "value": "three weeks",
                              "quote": "deadline is three weeks", "cues": ["ship date", "launch", "release"]})
        for n in range(5):
            self.store.remember(f"team{n}", "slack_channel", f"#team-{n}", "x", kind="project")
        projection = compiler.project("Is there room for the refactor before we ship?", strategy="jev")
        first = projection.trace["selected"][0]
        self.assertEqual(self.store.inspect(first)["predicate"], "deadline")
        self.assertIn("checkout.deadline", projection.trace["usage"]["lexical_matches"])

    def test_a_new_value_keeps_the_cues_and_a_restatement_adds_its_words(self):
        packet, _, _ = self.prompt("The checkout deadline is three months.")
        first = self.capture(packet, {"entity": "checkout", "predicate": "deadline", "value": "three months",
                                      "cues": ["ship date"]})[0]
        packet, _, _ = self.prompt("Update: the checkout deadline is three weeks now.")
        second = self.capture(packet, {"entity": "checkout", "predicate": "deadline", "value": "three weeks"})[0]
        self.assertEqual(self.store.inspect(second["id"])["cues"], ["ship date"])
        packet, _, _ = self.prompt("Again: checkout deadline, three weeks.")
        self.capture(packet, {"entity": "checkout", "predicate": "deadline", "value": "three weeks", "cues": ["go live"]})
        self.assertEqual(self.store.inspect(second["id"])["cues"], ["ship date", "go live"])
        self.assertNotEqual(first["id"], second["id"])

    def test_facts_without_cues_get_their_kinds_defaults(self):
        self.store.remember("user", "allergy", "cannot eat peanuts", "x")
        projection = Compiler(self.store).project("What snack should I bring to the offsite?", strategy="fts")
        self.assertEqual(len(projection.trace["selected"]), 1)

    def test_two_agreeing_signals_beat_one_strong_one(self):
        chosen = select([(0.63, "team", "channel"), (0.42, "checkout", "deadline"), (0.2, "x", "y")],
                        {("checkout", "deadline"): 1.0}, critical=0.6, supporting=0.5, band=0.35)
        self.assertEqual([(c[2], c[3]) for c in chosen], [("checkout", "deadline"), ("team", "channel")])

    def test_pairs_judged_fit_the_time_left(self):
        self.assertEqual(affordable_pairs(None, 0.2, 48), 48)
        self.assertEqual(affordable_pairs(Budget(3.5), 0.2, 48), int((3.5 - RESERVE_SECONDS) / 0.2 - 0.01))
        self.assertEqual(affordable_pairs(Budget(RESERVE_SECONDS), 0.2, 48), 0)
        self.assertEqual(affordable_pairs(Budget(60), 0.2, 48), 48)
        judge = FakeJudge(rank=lambda query, line, question: 0.9)
        for n in range(30):
            self.store.remember("user", f"note_{n}", f"value {n}", "x")
        projection = Compiler(self.store, jev=judge, deadline=Budget(3.5)).project("my notes", strategy="jev")
        self.assertLessEqual(projection.trace["usage"]["judged_pairs"], 10)
        self.assertIn("jev_inventory_capped", projection.trace["warnings"])

    def test_claims_are_compact(self):
        self.store.remember("user", "manager", "Dani", "My manager is Dani.")
        packet = json.loads(Compiler(self.store).project("who is my manager?", strategy="fts").content)
        claim = packet["claims"][0]
        self.assertNotIn("evidence_id", claim)
        self.assertNotIn("valid_until", claim)
        self.assertEqual(len(claim["valid_from"]), 10)

    def test_a_partial_packet_is_delivered_when_critical_claims_overflow(self):
        judge = FakeJudge(rank=lambda query, line, question: 0.9)
        for n in range(12):
            self.store.remember("project", f"constraint_{n}", "x" * 180 + str(n), "x", kind="project")
        packet, _, response = self.prompt("What constraints apply to the project?", compiler=Compiler(self.store, jev=judge),
                                          strategy="jev", judge=judge)
        self.assertGreater(len(packet["claims"]), 3)
        self.assertIn("critical_budget_overflow", packet["warnings"])
        self.assertNotIn("unavailable", response.get("systemMessage", ""))


class SafeMode(Base):
    class Other(FakeJudge):
        def describe(self):
            return {"url": "http://localhost:11434/v1/systemone", "model": "other-model", "local": True}

    def test_a_judge_that_changed_holds_facts_until_its_check_passes(self):
        self.store.set_judge_record(model_key(self.judge.describe()), "passed", {"model": "fake"})
        self.judge = self.Other()
        self.assertEqual(judge_state(self.store, self.judge)[0], "changed")
        packet, _, _ = self.prompt("My manager is Dani.")
        result = self.capture(packet, {"entity": "user", "predicate": "manager", "value": "Dani"})[0]
        self.assertEqual((result["status"], result["reason"]), ("quarantined", "judge_unverified"))
        # A well-calibrated judge: each canary row's own question answered as expected, the others quiet.
        canary = {(kind if kind != "facts" else "count", state["text"], state.get("fact")): expected
                  for kind, state, expected in CANARY}

        def ask(qid, state, spec):
            expected = canary.get((qid, state.get("text"), state.get("fact")))
            if spec[0] == "choice":
                if qid == "count":
                    return {"none": 0.02 if expected else 0.9, "one": 0.98 if expected else 0.1, "two": 0.0, "several": 0.0}
                return {o: (1.0 if n == 0 else 0.0) for n, o in enumerate(spec[2])}
            if expected is None:
                return 0.95 if qid == "affirmed" else 0.05
            return 0.95 if expected else 0.05
        self.judge.ask_fn = ask
        self.assertEqual(check(self.store, self.judge)["status"], "passed")
        self.judge.ask_fn = answers()
        packet, _, _ = self.prompt("My manager is Ana.")
        self.assertEqual(self.capture(packet, {"entity": "user", "predicate": "approver", "value": "Ana"})[0]["status"], "captured")

    def test_a_judge_that_fails_the_canary_saves_nothing(self):
        self.judge.ask_fn = lambda qid, state, spec: ({o: 0.25 for o in spec[2]} if spec[0] == "choice" else 0.7)
        result = check(self.store, self.judge)
        self.assertEqual(result["status"], "failed")
        self.assertTrue(result["wrong"])
        self.judge.ask_fn = answers()
        packet, _, _ = self.prompt("My manager is Dani.")
        self.assertEqual(self.capture(packet, {"entity": "user", "predicate": "manager", "value": "Dani"})[0]["reason"],
                         "judge_unverified")

    def test_first_use_trusts_the_shipped_thresholds(self):
        self.assertEqual(judge_state(self.store, self.judge)[0], "unverified")
        packet, _, _ = self.prompt("My manager is Dani.")
        self.assertEqual(self.capture(packet, {"entity": "user", "predicate": "manager", "value": "Dani"})[0]["status"], "captured")


class WarmAndDuplicates(Base):
    class Cold(FakeJudge):
        def loaded(self):
            return False

    def test_a_cold_model_answers_from_keywords_and_starts_a_warm_up(self):
        judge = self.Cold(rank=lambda query, line, question: 0.9)
        self.store.remember("user", "manager", "Dani", "x")
        started = []
        packet, event, _ = self.prompt("Who is my manager? Dani joined in May.", compiler=Compiler(self.store, jev=judge),
                                       strategy="jev", judge=judge, warm=lambda: started.append(1))
        self.assertEqual(started, [1])
        self.assertIn("judge_cold", self.store.turn("s1", event["prompt_id"])["flags"])
        self.assertEqual([c for c in judge.calls if c[0] in {"rank", "ask"}], [])
        self.assertEqual(packet["claims"][0]["value"], "Dani")

    def test_a_second_install_of_the_hooks_delivers_nothing_twice(self):
        self.store.remember("checkout", "deadline", "three months", "x", kind="project")
        self.judge.rank_fn = lambda query, line, question: 0.9
        first, event, _ = self.prompt("Should we rewrite the checkout payment module before the deadline?", prompt_id="same",
                                      strategy="fts")
        response, _ = hook_response(event, self.workspace, self.store, self.compiler, strategy="fts", judge=self.judge)
        self.assertEqual(response, {})
        self.assertEqual(self.store.turn("s1", "same")["token"], first["turn"]["token"])
        reply = "Yes: with three months you have room to rewrite the checkout payment module and test it."
        self.assertIn("linked the recommendation", self.stop(event, reply)["systemMessage"])
        self.assertEqual(self.stop(event, reply), {})
        self.assertEqual(len([r for r in self.store.records(history=True) if r["predicate"] == "recommendation"]), 1)

    def test_advice_followed_by_a_caveat_is_still_a_recommendation(self):
        """2 of 4 native runs lost the link: the caveat pulled the whole reply to 0.71, the opening read 0.93."""
        self.store.remember("acme", "adr_0001", "Use SQLite for the job queue", "x", kind="project")
        self.judge.ask_fn = judged(recommends=0.65, opening=0.93)
        self.judge.rank_fn = lambda query, line, question: 0.93
        _, event, _ = self.prompt("Which queue should the new email worker use?", strategy="fts")
        reply = ("The new email worker should use the SQLite job queue. I couldn't open the file in this session, "
                 "so I'm going on the project memory's summary of that decision record.")
        self.assertIn("linked the recommendation", self.stop(event, reply)["systemMessage"])

    def test_the_same_prompt_id_much_later_opens_a_new_turn(self):
        first, event, _ = self.prompt("My manager is Dani.", prompt_id="again")
        self.now = timestamp_offset(self.now, 120)
        second, _, _ = self.prompt("My manager is Dani.", prompt_id="again")
        self.assertNotEqual(first["turn"]["token"], second["turn"]["token"])

    def test_session_start_starts_one_background_pass(self):
        calls = []
        session_start_response({"cwd": str(self.workspace), "hook_event_name": "SessionStart", "session_id": "s9"},
                               self.workspace, self.store, warm=lambda cwd, session, repo: calls.append((session, repo)))
        self.assertEqual(calls, [("s9", False)])


class Regret(Base):
    def test_taking_back_a_capture_counts_against_precision(self):
        packet, _, _ = self.prompt("My manager is Dani.")
        saved = self.capture(packet, {"entity": "user", "predicate": "manager", "value": "Dani"})[0]
        packet, _, _ = self.prompt("The staging cluster is kite-stg.")
        kept = self.capture(packet, {"entity": "staging", "predicate": "cluster", "value": "kite-stg"})[0]
        self.assertEqual(self.store.regret_metrics()["precision_estimate"], 1.0)
        self.store.undo_capture(saved["id"])
        metrics = self.store.regret_metrics()
        self.assertEqual((metrics["captured"], metrics["taken_back"]["undone"], metrics["precision_estimate"]), (2, 1, 0.5))
        self.now = timestamp_offset(self.now, 10 * 86400)
        self.store.forget(kept["id"])
        self.assertEqual(self.store.regret_metrics()["taken_back"]["forgotten"], 0)  # ten days later: a change of mind
        labels = [r[0] for r in self.store.db.execute("SELECT label FROM captures_log")]
        self.assertNotIn("staging.cluster", labels)

    def test_replacing_a_capture_within_the_hour_is_a_misreading(self):
        packet, _, _ = self.prompt("The checkout deadline is three weeks.")
        self.capture(packet, {"entity": "checkout", "predicate": "deadline", "value": "three weeks"})
        packet, _, _ = self.prompt("No wait, the checkout deadline is three weeks from Monday.")
        self.capture(packet, {"entity": "checkout", "predicate": "deadline", "value": "three weeks from Monday"})
        self.assertEqual(self.store.regret_metrics()["taken_back"]["corrected"], 1)


class Compatibility(Base):
    def test_a_database_stamped_7_reads_as_6_and_older_ones_gain_the_new_columns(self):
        path = self.workspace / "memory.sqlite"
        self.store.close()
        raw = sqlite3.connect(path)
        raw.execute("UPDATE metadata SET value='7' WHERE key='schema_version'")
        raw.execute("DROP TABLE judges")
        raw.commit()
        raw.close()
        self.store = Store(path, clock=lambda: timestamp(self.now))
        self.assertEqual(self.store.db.execute("SELECT value FROM metadata WHERE key='schema_version'").fetchone()[0], "6")
        self.assertEqual(self.store.judge_records(), [])

    def test_a_review_follows_a_superseded_record_to_its_successor(self):
        old = self.store.remember("repo", "adr_0003", "Use SQLite for the job queue", "x", kind="project")
        new = self.store.remember("repo", "adr_0004", "Use Redis for the job queue", "x", kind="project")
        with self.store.db:
            advice = self.store._insert("repo", "recommendation", "Use the SQLite queue for digests.", "x",
                                        assertion_kind="inference", source_kind="agent_reply", trust="captured")
        self.store.depend(advice, old["id"], provenance="inferred")
        with self.store.db:
            self.store._supersede(old["id"], new["id"])
        pairs = stale_recommendations(self.store.records(history=True))[0]["pairs"]
        self.assertIn(["repo", "adr_0004"], [list(p) for p in pairs])


class OwnerCommands(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = str(Path(self.temp.name) / "memory.sqlite")
        self.run_cli("init")

    def tearDown(self):
        self.temp.cleanup()

    def run_cli(self, *argv):
        import contextlib
        import io
        from shelflife_context import cli
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main(["--db", self.db, "--scope", "work", *argv])
        return code, json.loads(out.getvalue()) if out.getvalue().strip() else None

    def test_policy_sets_commits_keep_alive_and_new_thresholds(self):
        code, policy = self.run_cli("policy", "--repository-commits", "on", "--keep-alive", "2h", "--threshold", "sarcasm=0.7")
        self.assertEqual(code, 0)
        self.assertEqual((policy["repository_commits"], policy["keep_alive"], policy["thresholds"]["sarcasm"]), (True, "2h", 0.7))
        self.assertEqual(self.run_cli("policy", "--keep-alive", "forever")[0], 1)

    def test_a_new_threshold_clears_the_judges_verdicts(self):
        store = Store(self.db, scope="work")
        store.set_judge_record("k", "passed", {"model": "m"})
        store.close()
        self.run_cli("policy", "--threshold", "affirmed=0.8")
        store = Store(self.db, scope="work")
        self.assertEqual(store.judge_records(), [])
        store.close()

    def test_metrics_report_precision_latency_and_judges(self):
        code, metrics = self.run_cli("metrics")
        self.assertEqual(code, 0)
        self.assertEqual(set(metrics) >= {"precision", "latency", "judges", "captures", "status"}, True)
        self.assertIsNone(metrics["precision"]["last_30_days"]["precision_estimate"])

    def test_warm_survives_a_missing_judge(self):
        code, result = self.run_cli("warm", "--jev-command", str(Path(self.temp.name) / "no-jev"))
        self.assertEqual(code, 0)
        self.assertIn("judge", result)


class Commits(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / "acme-api"
        self.root.mkdir()
        self.now = "2026-10-05T12:00:00+00:00"
        self.store = Store(Path(self.temp.name) / "memory.sqlite", scope="work", clock=lambda: timestamp(self.now), create=True)
        self.judge = FakeJudge(ask=lambda qid, state, spec: 0.9)
        env = dict(os.environ, GIT_AUTHOR_NAME="A", GIT_AUTHOR_EMAIL="a@example.com", GIT_COMMITTER_NAME="A",
                   GIT_COMMITTER_EMAIL="a@example.com", GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM="1",
                   GIT_AUTHOR_DATE="2026-10-01T10:00:00+00:00", GIT_COMMITTER_DATE="2026-10-01T10:00:00+00:00")
        for argv in (["init", "-q", "-b", "main"], ["commit", "-q", "--allow-empty", "-m", "Use Postgres for the analytics store"]):
            subprocess.run(["git", "-C", str(self.root), *argv], check=True, capture_output=True, env=env)

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def commit_facts(self):
        return [r for r in self.store.records() if (r.get("source_ref") or "").startswith("commit ")]

    def test_commit_messages_are_read_only_when_turned_on(self):
        learn(self.store, self.judge, str(self.root))
        self.assertEqual(self.commit_facts(), [])
        self.store.set_policy({"repository_commits": True})
        self.now = timestamp_offset(self.now, 60)
        learn(self.store, self.judge, str(self.root), if_changed=True)
        self.assertEqual(len(self.commit_facts()), 1)
        self.store.set_policy({"repository_commits": False})
        self.now = timestamp_offset(self.now, 60)
        summary = learn(self.store, self.judge, str(self.root), if_changed=True)
        self.assertEqual(self.commit_facts(), [])
        self.assertIn("stopped using 1 decision(s) read from commit messages", summary["note"])
        self.store.set_policy({"repository_commits": True})
        self.now = timestamp_offset(self.now, 60)
        learn(self.store, self.judge, str(self.root), if_changed=True)
        self.assertEqual(len(self.commit_facts()), 1)  # no tombstone: turning it back on reads it again


if __name__ == "__main__":
    unittest.main()
