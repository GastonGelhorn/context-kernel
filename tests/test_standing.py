"""Standing facts: what the user keeps repeating, or states as a rule, reaches every session unasked."""

from contextlib import closing
from pathlib import Path
import sqlite3
import tempfile
import unittest

from shelflife_context.adapters import hook_response, packet_of, session_start_response
from shelflife_context.capture import STANDING_SESSIONS, standing_cue
from shelflife_context.common import timestamp, timestamp_offset
from shelflife_context.compiler import Compiler
from shelflife_context.mcp import Server
from shelflife_context.store import SCHEMA_VERSION, Store
from tests.fakes import FakeJudge, answers


HOST = [4242, "Mon Oct  5 12:00:00 2026"]


class StandingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.workspace = Path(self.temp.name)
        self.now = "2026-10-05T12:00:00+00:00"
        self.store = Store(self.workspace / "memory.sqlite", clock=lambda: timestamp(self.now), create=True)
        self.judge = FakeJudge(ask=answers(category="preferences"))
        self.compiler = Compiler(self.store)
        self.turns = 0

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def tick(self, seconds=5):
        self.now = timestamp_offset(self.now, seconds)

    def prompt(self, text, session="s1"):
        self.tick()
        self.turns += 1
        event = {"cwd": str(self.workspace), "prompt": text, "session_id": session, "prompt_id": f"p{self.turns}"}
        response, _ = hook_response(event, self.workspace, self.store, self.compiler, judge=self.judge)
        self.store.register_session(session, "claude", [HOST])
        return packet_of(response["hookSpecificOutput"]["additionalContext"])

    def tool(self, name, arguments):
        server = Server(self.store, self.compiler, judge=self.judge, parent=HOST)
        server.dispatch({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
            "protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {}}})
        server.dispatch({"jsonrpc": "2.0", "method": "notifications/initialized"})
        response = server.dispatch({"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": name, "arguments": arguments}})
        return response["result"]["structuredContent"], response["result"]["isError"]

    def say(self, text, session, fact=("user", "package_manager", "pnpm")):
        packet = self.prompt(text, session)
        result, error = self.tool("memory_capture", {"token": packet["turn"]["token"],
                                                     "facts": [{"entity": fact[0], "predicate": fact[1], "value": fact[2]}]})
        self.assertFalse(error, result)
        return result

    def standing_claims(self, packet):
        return [c for c in packet["claims"] if c.get("standing")]

    def test_a_fact_stated_in_three_sessions_becomes_standing(self):
        first = self.say("Usa pnpm en este repo.", "s1")
        self.assertNotIn("standing", first["results"][0])
        self.say("Usa pnpm, por favor.", "s2")
        self.assertEqual(self.store.standing(), {})
        third = self.say("Otra vez: usa pnpm.", "s3")
        self.assertEqual(third["results"][0]["standing"], "restated")
        self.assertIn("will keep in mind in every session user.package_manager", third["receipt"])
        self.assertEqual(STANDING_SESSIONS, 3)
        # The same session saying it again does not count twice.
        self.store.set_standing("user", "package_manager", "on", "restated")

    def test_repeating_within_one_session_is_not_enough(self):
        for _ in range(4):
            self.say("Usa pnpm.", "s1")
        self.assertEqual(self.store.standing(), {})

    def test_a_rule_stated_as_never_or_from_now_on_is_standing_at_once(self):
        result = self.say("Nunca agregues Co-Authored-By de Claude en los commits.", "s1",
                          ("user", "commit_attribution", "never add Co-Authored-By Claude"))
        self.assertEqual(result["results"][0]["standing"], "stated_as_rule")
        self.assertTrue(standing_cue("From now on use pnpm.", "pnpm"))
        self.assertFalse(standing_cue("¿Siempre usamos pnpm?", "pnpm"))
        self.assertFalse(standing_cue("Usa pnpm.", "pnpm"))

    def test_a_fact_of_the_moment_is_not_a_rule_even_with_never(self):
        self.judge.ask_fn = answers(category="project_state")
        result = self.say("Nunca hemos tenido un incidente en producción.", "s1", ("acme", "incidents", "none in production"))
        self.assertNotIn("standing", result["results"][0])

    def test_standing_facts_reach_each_session_once_and_again_after_it_restarts(self):
        self.store.remember("user", "package_manager", "pnpm", "Usa pnpm.")
        self.store.set_standing("user", "package_manager", "on", "owner")
        first = self.prompt("Explain what a SQLite primary key is.", "s9")
        self.assertEqual([c["value"] for c in self.standing_claims(first)], ["pnpm"])
        self.assertIn("standing", " ".join(first["reader_rules"]))
        self.assertEqual(self.standing_claims(self.prompt("Explain WAL mode.", "s9")), [])
        # Compaction or a resume runs SessionStart: what the session was told may be gone.
        session_start_response({"cwd": str(self.workspace), "hook_event_name": "SessionStart", "session_id": "s9"},
                               self.workspace, self.store)
        self.assertEqual(len(self.standing_claims(self.prompt("ok", "s9"))), 1)
        # Another session gets them on its own first prompt.
        self.assertEqual(len(self.standing_claims(self.prompt("Explain indexes.", "s10"))), 1)

    def test_a_changed_standing_value_is_delivered_again(self):
        self.store.remember("user", "package_manager", "pnpm", "Usa pnpm.")
        self.store.set_standing("user", "package_manager", "on", "owner")
        self.prompt("Explain indexes.", "s1")
        old = self.store.records()[0]["id"]
        self.store.correct(old, "bun", "Ahora usamos bun.")
        self.assertEqual([c["value"] for c in self.standing_claims(self.prompt("Explain views.", "s1"))], ["bun"])

    def test_standing_facts_are_exempt_from_decay(self):
        packet = self.prompt("Usa pnpm.", "s1")
        self.tool("memory_capture", {"token": packet["turn"]["token"],
                                     "facts": [{"entity": "user", "predicate": "package_manager", "value": "pnpm"}]})
        self.now = timestamp_offset(self.now, 200 * 86400)
        self.assertEqual(self.compiler.project("Which package manager?", "fts").trace["selected"], [])
        self.store.set_standing("user", "package_manager", "on", "owner")
        self.assertEqual(len(self.compiler.project("Which package manager?", "fts").trace["selected"]), 1)
        message = session_start_response({"cwd": str(self.workspace), "hook_event_name": "SessionStart", "session_id": "s2"},
                                         self.workspace, self.store)
        self.assertEqual(message, {})

    def test_the_user_can_take_a_fact_out_of_every_session_and_counting_does_not_put_it_back(self):
        for session in ("s1", "s2", "s3"):
            self.say("Usa pnpm.", session)
        fact = self.store.records()[0]
        packet = self.prompt("Ya no hace falta que recuerdes siempre lo de pnpm.", "s4")
        result, error = self.tool("memory_standing", {"token": packet["turn"]["token"], "id": fact["id"], "standing": False})
        self.assertFalse(error, result)
        self.assertEqual(self.store.standing(), {})
        self.say("Usa pnpm.", "s5")
        self.assertEqual(self.store.standing(), {})
        self.assertEqual(self.store.standing_state("user", "package_manager")["state"], "off")

    def test_standing_tool_needs_the_users_words_and_a_boolean(self):
        self.store.remember("user", "package_manager", "pnpm", "Usa pnpm.")
        fact = self.store.records()[0]
        packet = self.prompt("Explain indexes.", "s1")
        self.judge.ask_fn = answers(asked=0.1)
        result, error = self.tool("memory_standing", {"token": packet["turn"]["token"], "id": fact["id"], "standing": True})
        self.assertTrue(error)
        result, error = self.tool("memory_standing", {"token": packet["turn"]["token"], "id": fact["id"], "standing": "yes"})
        self.assertTrue(error)
        self.assertEqual(self.store.standing(), {})

    def test_forgetting_clears_standing_and_restarts_the_count(self):
        for session in ("s1", "s2", "s3"):
            self.say("Usa pnpm.", session)
        self.store.forget(self.store.records()[0]["id"])
        self.assertIsNone(self.store.standing_state("user", "package_manager"))
        self.tick(60)
        self.say("Usa pnpm.", "s4")
        self.assertEqual(self.store.stated_sessions("user", "package_manager"), 1)

    def test_inventory_marks_standing_facts(self):
        self.store.remember("user", "package_manager", "pnpm", "Usa pnpm.")
        self.store.set_standing("user", "package_manager", "on", "owner")
        inventory, _ = self.tool("memory_inventory", {})
        self.assertTrue(inventory["entities"]["user"][0]["standing"])

    def test_picking_among_listed_options_is_not_judged_as_a_statement(self):
        from shelflife_context.turns import choice_reply
        for message in ("haz 1 y 2", "la 3", "ambas", "sí, el 2", "go with option 2", "do both", "1, 2 y 3", "las dos"):
            self.assertTrue(choice_reply(message), message)
        for message in ("si", "dos semanas para el checkout", "usa la opción 2: Redis", "haz 1 y luego push", "Mi manager es Dani"):
            self.assertFalse(choice_reply(message), message)
        self.store.remember("user", "package_manager", "pnpm", "Usa pnpm.")
        self.judge.calls.clear()
        packet = self.prompt("haz 1 y 2", "s1")
        self.assertNotIn("capture", packet["turn"])
        self.assertEqual(self.judge.calls, [])  # neither the gate nor relevance was asked

    def test_v5_database_gains_the_new_tables_and_keeps_its_sessions(self):
        path = self.workspace / "v5.sqlite"
        legacy = Store(path, create=True)
        legacy.db.execute("DROP TABLE standing")
        legacy.db.execute("DROP TABLE repositories")
        legacy.db.execute("DROP TABLE repo_sources")
        legacy.db.execute("DROP TABLE sessions")
        legacy.db.execute("""CREATE TABLE sessions(scope TEXT NOT NULL, session_id TEXT NOT NULL, host TEXT NOT NULL,
                             chain TEXT NOT NULL, seen_at TEXT NOT NULL, PRIMARY KEY(scope,session_id))""")
        legacy.db.execute("INSERT INTO sessions VALUES('personal','old','claude','[]','2026-10-01T00:00:00.000000+00:00')")
        legacy.db.execute("UPDATE metadata SET value='5' WHERE key='schema_version'")
        legacy.db.commit()
        legacy.close()
        migrated = Store(path)
        try:
            self.assertEqual(migrated.db.execute("SELECT value FROM metadata WHERE key='schema_version'").fetchone()[0], SCHEMA_VERSION)
            migrated.register_session("old", "claude", [HOST])
            migrated.set_session_field("old", "standing_sent", "x")
            migrated.register_session("old", "claude", [HOST])
            self.assertEqual(migrated.session_field("old", "standing_sent"), "x")
            self.assertEqual(migrated.standing(), {})
        finally:
            migrated.close()
        with closing(sqlite3.connect(path)) as db:
            self.assertEqual(db.execute("SELECT count(*) FROM repo_sources").fetchone()[0], 0)


if __name__ == "__main__":
    unittest.main()
