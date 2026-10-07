"""v0.7: fixes from the four-variant comparison (tests/compare_check.py) and the external review."""

from http.server import BaseHTTPRequestHandler, HTTPServer
import json
from pathlib import Path
import tempfile
import threading
import unittest

from shelflife_context.adapters import hook_response, packet_of, stop_response
from shelflife_context.common import timestamp, timestamp_offset
from shelflife_context.compiler import Compiler
from shelflife_context.inference import RESTS_ON
from shelflife_context.judge import weights
from shelflife_context.mcp import Server
from shelflife_context.store import Store
from tests.fakes import FakeJudge, answers


HOST = [4242, "Mon Oct  5 12:00:00 2026"]


class ComparisonFixes(unittest.TestCase):
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

    def prompt(self, text, session="s1"):
        self.now = timestamp_offset(self.now, 5)
        self.turns += 1
        event = {"cwd": str(self.workspace), "prompt": text, "session_id": session, "prompt_id": f"p{self.turns}"}
        response, _ = hook_response(event, self.workspace, self.store, self.compiler, strategy="rules", judge=self.judge)
        self.store.register_session(session, "claude", [HOST])
        return packet_of(response["hookSpecificOutput"]["additionalContext"]), event

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
        result, error = self.tool("memory_capture", {"token": packet["turn"]["token"],
                                                     "facts": [{"entity": e, "predicate": p, "value": v} for e, p, v in facts]})
        self.assertFalse(error, result)
        return result["results"]

    def test_one_message_can_forget_every_property_of_a_thing(self):
        """In the comparison, "forget the old database" removed its name and left its host: the first
        forget deleted every turn, so the second call's token no longer existed."""
        name = self.store.remember("legacy_database", "name", "orders-legacy", "Owner CLI")
        host = self.store.remember("legacy_database", "hosting_provider", "Hetzner", "Owner CLI")
        packet, _ = self.prompt("Olvida lo de la base de datos antigua, ya no existe.")
        token = packet["turn"]["token"]
        first, error = self.tool("memory_forget", {"token": token, "id": name["id"]})
        self.assertFalse(error, first)
        self.assertEqual(first["still_remembered_about_entity"], [{"id": host["id"], "predicate": "hosting_provider"}])
        second, error = self.tool("memory_forget", {"token": token, "id": host["id"]})
        self.assertFalse(error, second)
        self.assertEqual(second["still_remembered_about_entity"], [])
        self.assertEqual(self.store.records(history=True), [])

    def test_a_forget_still_drops_other_turns_excerpts(self):
        self.store.remember("legacy_database", "name", "orders-legacy", "Owner CLI")
        earlier, _ = self.prompt("La base antigua se llama orders-legacy.")
        packet, _ = self.prompt("Olvida la base de datos antigua.")
        row = self.store.records()[0]
        self.assertFalse(self.tool("memory_forget", {"token": packet["turn"]["token"], "id": row["id"]})[1])
        self.assertIsNone(self.store.turn_by_token(earlier["turn"]["token"]))
        kept = self.store.turn_by_token(packet["turn"]["token"])
        self.assertIsNotNone(kept)
        self.assertIsNone(kept["projection_id"])

    def test_a_requested_command_is_not_stored_as_a_recommendation(self):
        self.judge.ask_fn = answers(recommends=0.95)
        self.judge.rank_fn = lambda query, line, question: 0.95
        self.store.remember("project", "staging_app_name", "checkout-stg", "Owner CLI")
        packet, event = self.prompt("Dame el comando para desplegar staging. Solo el comando.")
        self.store.db.execute("UPDATE turns SET delivered_ids=? WHERE token=?",
                              (json.dumps([self.store.records()[0]["id"]]), packet["turn"]["token"]))
        self.stop(event, "```\nfly deploy -a checkout-stg\n```\n\nNo fly.toml here.")
        self.assertEqual([r for r in self.store.records(history=True) if r["predicate"] == "recommendation"], [])

    def test_a_question_is_never_captured_even_if_the_agent_proposes_it(self):
        self.judge.ask_fn = answers(affirmed=0.95)
        packet, _ = self.prompt("Does Ops own the on-call rotation now?")
        self.assertEqual(self.capture(packet, ("on_call", "owner", "Ops"))[0]["reason"], "question_only")
        packet, _ = self.prompt("Ops owns the on-call rotation now. Can you note that?")
        self.assertEqual(self.capture(packet, ("on_call", "owner", "Ops"))[0]["status"], "captured")

    def stale_queue_recommendation(self):
        """project.recommendation rests on project.queue; project also holds unrelated facts."""
        queue = self.store.remember("project", "queue", "SQLite", "Owner CLI")
        self.store.remember("project", "package_manager", "pnpm", "Owner CLI")
        with self.store.db:
            recommendation = self.store._insert("project", "recommendation", "Use the SQLite queue.", "Use the SQLite queue.",
                                                assertion_kind="inference", source_kind="agent_reply", trust="captured")
        self.store.depend(recommendation, queue["id"], provenance="inferred")
        self.now = timestamp_offset(self.now, 60)
        current = self.store.correct(queue["id"], "Redis", "The queue moved to Redis.")
        return recommendation, current

    def test_a_bare_yes_is_not_stored_as_the_recommendation(self):
        from shelflife_context.inference import first_sentence
        self.assertEqual(first_sentence("Sí. Mejor refactorizar por partes con tests. Luego reescribir."),
                         "Sí. Mejor refactorizar por partes con tests.")

    def test_one_translated_word_does_not_name_a_recommendation(self):
        """"proyecto" also reads as "project": one word of the question used to count as two."""
        self.stale_queue_recommendation()
        with self.store.db:
            self.store._insert("project", "note", "x", "x")
        self.store.db.execute("UPDATE statements SET value=? WHERE predicate='recommendation'",
                              (json.dumps("El worker debería usar la cola SQLite que ya tiene el proyecto."),))
        projection = self.compiler.project("Dame el comando para añadir zod a este proyecto.", strategy="fts")
        self.assertNotIn("review_recommended", projection.trace["warnings"])

    def test_a_review_needs_the_changed_premise_not_just_the_same_entity(self):
        self.stale_queue_recommendation()
        projection = self.compiler.project("Give me the command to add zod to this project with pnpm.", strategy="fts")
        self.assertNotIn("review_recommended", projection.trace["warnings"])

    def test_a_review_brings_the_premise_current_value(self):
        recommendation, current = self.stale_queue_recommendation()
        projection = self.compiler.project("Should the worker still use the sqlite queue you recommended?", strategy="fts")
        self.assertIn("review_recommended", projection.trace["warnings"])
        content = json.loads(projection.content)
        self.assertEqual(content["review"][0]["id"], recommendation)
        self.assertIn(current["id"], [c["id"] for c in content["claims"]])
        self.assertNotIn("Use the SQLite queue.", projection.content)

    def test_reader_rules_travel_only_with_what_they_explain(self):
        """All eight rules took 1.1 KB of the 2 KB packet; in the comparison the meeting day was cut for it."""
        for predicate, value in (("day", "martes"), ("time", "10"), ("room", "Sala 2"), ("host", "Ana")):
            self.store.remember("release_meeting", predicate, value, "Owner CLI")
        projection = self.compiler.project("¿Qué día es la reunión de release?", strategy="fts")
        content = json.loads(projection.content)
        self.assertEqual(len(content["reader_rules"]), 3)
        self.assertEqual(len(content["claims"]), 4)

    def test_a_review_premise_comes_before_other_claims(self):
        recommendation, current = self.stale_queue_recommendation()
        for n in range(12):
            self.store.remember("queue_worker", f"setting_{n}", "x" * 120, "Owner CLI")
        projection = self.compiler.project("Should the worker still use the sqlite queue you recommended? queue worker settings",
                                           strategy="fts")
        self.assertIn("review_recommended", projection.trace["warnings"])
        self.assertEqual(projection.trace["selected"][0], current["id"])
        self.assertIn("Items under review", " ".join(json.loads(projection.content)["reader_rules"]))

    def test_rules_deliver_a_keyword_match_that_names_one_thing(self):
        """Several project entities make "deploy" ambiguous; "staging" still names one app."""
        for entity in ("checkout", "kernel"):
            self.store.remember(entity, "project_status", "active", "Owner CLI", kind="project")
        staging = self.store.remember("project", "staging_app_name", "checkout-stg", "Owner CLI", kind="project")
        projection = self.compiler.project("Dame el comando para desplegar staging.")
        self.assertIn("clarification_required", projection.trace["warnings"])
        self.assertIn(staging["id"], projection.trace["selected"])


class GrowthFixes(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.now = "2026-09-01T09:00:00+00:00"
        self.store = Store(Path(self.temp.name) / "memory.sqlite", clock=lambda: timestamp(self.now), create=True)

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def test_critical_pairs_over_budget_deliver_the_best_that_fit(self):
        """Judged-critical pairs that overflow the packet used to empty it."""
        rows = [self.store.remember(f"service{n}", "owner", f"Person {n} " + "x" * 300, "Owner CLI") for n in range(8)]
        judge = FakeJudge(rank=lambda query, line, question: 0.9 if "service0 " in line else 0.8)
        projection = Compiler(self.store, jev=judge).project("Who should review the change?", strategy="jev")
        self.assertEqual(projection.trace["status"], "insufficient_context")
        self.assertIn("critical_budget_overflow", projection.trace["warnings"])
        self.assertEqual(projection.trace["selected"][0], rows[0]["id"])
        self.assertLessEqual(len(projection.content.encode()), 2048)
        self.assertIn("critical_budget_overflow", json.loads(projection.content)["warnings"])

    def test_an_old_constraint_is_judged_before_newer_chatter(self):
        deadline = self.store.remember("checkout", "delivery_deadline", "three weeks", "Owner CLI")
        for n in range(60):
            self.now = timestamp_offset(self.now, 60)
            self.store.remember(f"service{n}", "slack_channel", f"#team-{n}", "Owner CLI")
        judge = FakeJudge(rank=lambda query, line, question: 0.9 if "deadline" in line else 0.1)
        projection = Compiler(self.store, jev=judge).project("Is there room for the refactor before we ship?", strategy="jev")
        self.assertIn("jev_inventory_capped", projection.trace["warnings"])
        self.assertIn(deadline["id"], projection.trace["selected"])


class WeightsFingerprint(unittest.TestCase):
    def serve(self, models):
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                body = json.dumps({"models": models}).encode()
                self.send_response(200 if self.path == "/api/tags" else 404)
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass
        server = HTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        return f"http://127.0.0.1:{server.server_port}/v1/systemone"

    def test_a_re_pulled_alias_changes_the_fingerprint(self):
        url = self.serve([{"name": "tev1-32k:latest", "digest": "527084f384df0682aaaa"}])
        self.assertEqual(weights(url, "tev1-32k"), "527084f384df0682")
        url = self.serve([{"name": "tev1-32k:latest", "digest": "9b5bb969e46c4b77bbbb"}])
        self.assertEqual(weights(url, "tev1-32k"), "9b5bb969e46c4b77")

    def test_hosted_or_unreachable_backends_are_not_asked(self):
        self.assertIsNone(weights("https://api.example.com/v1", "tev1-32k"))
        self.assertIsNone(weights("http://127.0.0.1:9/v1", "tev1-32k"))


if __name__ == "__main__":
    unittest.main()
