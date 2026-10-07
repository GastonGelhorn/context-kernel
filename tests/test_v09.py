"""v0.9: facts that were right and are over (close, reopen) and facts that end with the period they name."""

from datetime import timedelta, timezone
import json
from pathlib import Path
import sqlite3

from context_kernel.calibration import QUESTIONS
from context_kernel.capture import describe_results, last_day
from context_kernel.common import KernelError, timestamp, timestamp_offset
from context_kernel.language import period_end, periods
from context_kernel.mcp import ASKS, INSTRUCTIONS, TOOLS
from context_kernel.store import SCHEMA_VERSION
from context_kernel.turns import done_statement
from tests.fakes import answers
from tests.test_v08 import Base, judged

import unittest

WEDNESDAY = "2026-10-07T10:00:00+00:00"
MADRID = timezone(timedelta(hours=2))


class Periods(unittest.TestCase):
    def test_relative_periods_in_both_languages(self):
        self.assertEqual(periods("Publicar v0.3 esta semana"), {("week", 0)})
        self.assertEqual(periods("publish v0.3 this week"), {("week", 0)})
        self.assertEqual(periods("Mañana trabajo desde casa"), {("day", 1)})
        self.assertEqual(periods("pasado mañana"), {("day", 2)})
        self.assertEqual(periods("esta mañana tengo médico"), {("day", 0)})
        self.assertEqual(periods("la semana que viene"), {("week", 1)})
        self.assertEqual(periods("el mes que viene"), {("month", 1)})
        self.assertEqual(periods("this weekend"), {("week", 0)})
        self.assertEqual(periods("este año"), {("year", 0)})
        self.assertEqual(periods("el viernes"), {("weekday", 4, "coming")})
        self.assertEqual(periods("hasta el viernes"), {("weekday", 4, "this")})
        self.assertEqual(periods("next Friday"), {("weekday", 4, "next")})
        self.assertEqual(periods("el próximo lunes"), {("weekday", 0, "next")})

    def test_starts_habits_and_idioms_are_not_bounds(self):
        for value in ("desde hoy", "a partir de mañana", "from next week on", "starting tomorrow", "hoy en día usamos Postgres",
                      "hoy por hoy", "por la mañana", "cada mañana", "los viernes hacemos deploy", "todos los viernes",
                      "el viernes pasado", "every week", "deadline 2026-10-15", "Postgres"):
            self.assertEqual(periods(value), set(), value)

    def test_the_end_of_each_period_in_local_time(self):
        utc = timezone.utc
        self.assertEqual(period_end("hoy", "hoy", WEDNESDAY, utc), "2026-10-08T00:00:00.000000+00:00")
        self.assertEqual(period_end("mañana", "mañana", WEDNESDAY, utc), "2026-10-09T00:00:00.000000+00:00")
        self.assertEqual(period_end("esta semana", "esta semana", WEDNESDAY, utc), "2026-10-12T00:00:00.000000+00:00")
        self.assertEqual(period_end("next week", "next week", WEDNESDAY, utc), "2026-10-19T00:00:00.000000+00:00")
        self.assertEqual(period_end("este mes", "este mes", WEDNESDAY, utc), "2026-11-01T00:00:00.000000+00:00")
        self.assertEqual(period_end("next month", "next month", "2026-12-20T10:00:00+00:00", utc),
                         "2027-02-01T00:00:00.000000+00:00")
        self.assertEqual(period_end("este año", "este año", WEDNESDAY, utc), "2027-01-01T00:00:00.000000+00:00")
        # Local midnight, stored in UTC: the week ends at 00:00 on Monday in Madrid, 22:00 UTC on Sunday.
        self.assertEqual(period_end("esta semana", "esta semana", WEDNESDAY, MADRID), "2026-10-11T22:00:00.000000+00:00")

    def test_weekdays_end_after_the_named_day(self):
        utc = timezone.utc
        self.assertEqual(period_end("el viernes", "el viernes", WEDNESDAY, utc), "2026-10-10T00:00:00.000000+00:00")
        self.assertEqual(period_end("este miércoles", "este miércoles", WEDNESDAY, utc), "2026-10-08T00:00:00.000000+00:00")
        self.assertEqual(period_end("el miércoles", "el miércoles", WEDNESDAY, utc), "2026-10-15T00:00:00.000000+00:00")
        # "next Friday" can mean either Friday; the later one, so the fact never ends early.
        self.assertEqual(period_end("next Friday", "next Friday", WEDNESDAY, utc), "2026-10-17T00:00:00.000000+00:00")

    def test_both_the_fact_and_the_users_words_must_name_it(self):
        utc = timezone.utc
        self.assertEqual(period_end("Publish v0.3 this week", "Decidimos publicar v0.3 esta semana.", WEDNESDAY, utc),
                         "2026-10-12T00:00:00.000000+00:00")
        self.assertIsNone(period_end("Publish v0.3 this week", "Decidimos publicar v0.3.", WEDNESDAY, utc))
        self.assertIsNone(period_end("Use Postgres", "Esta semana decidimos usar Postgres.", WEDNESDAY, utc))
        # Several shared periods: the latest end.
        self.assertEqual(period_end("hoy o mañana", "lo hago hoy o mañana", WEDNESDAY, utc), "2026-10-09T00:00:00.000000+00:00")


class PeriodCapture(Base):
    def test_a_fact_said_for_this_week_ends_with_the_week(self):
        packet, _, _ = self.prompt("Decidimos publicar la v0.3 esta semana.")
        result = self.capture(packet, {"entity": "context_kernel", "predicate": "decision", "value": "Publicar v0.3 esta semana",
                                       "quote": "Decidimos publicar la v0.3 esta semana"})[0]
        self.assertEqual(result["status"], "captured")
        statement = self.store.inspect(result["id"])
        self.assertEqual(statement["valid_until"], result["until"])
        self.assertEqual(statement["valid_until"], period_end("esta semana", "esta semana", statement["valid_from"]))
        self.assertIn(f"until {last_day(result['until'])}", describe_results([result]))
        self.assertEqual([r["id"] for r in self.store.records()], [result["id"]])
        self.now = timestamp_offset(self.now, 8 * 86400)
        self.assertEqual(self.store.records(), [])
        self.assertEqual(self.store.inspect(result["id"])["effective_state"], "expired")

    def test_a_period_only_in_the_users_words_is_when_it_was_said(self):
        packet, _, _ = self.prompt("Esta semana decidimos usar Postgres para el backend.")
        result = self.capture(packet, {"entity": "backend", "predicate": "database", "value": "Postgres"})[0]
        self.assertEqual(result["status"], "captured")
        self.assertNotIn("until", result)
        self.assertIsNone(self.store.inspect(result["id"])["valid_until"])

    def test_a_start_is_not_an_end(self):
        packet, _, _ = self.prompt("Desde mañana el approver de releases es Irene.")
        result = self.capture(packet, {"entity": "context_kernel", "predicate": "release_approver", "value": "Irene desde mañana"})[0]
        self.assertEqual(result["status"], "captured")
        self.assertIsNone(self.store.inspect(result["id"])["valid_until"])


class Close(Base):
    def remember(self, entity, predicate, value):
        return self.store.remember(entity, predicate, value, f"{entity} {predicate} {value}")["id"]

    def test_a_closed_fact_stays_in_history_and_is_not_delivered(self):
        fact = self.remember("billing", "next_step", "Migrate billing to Stripe")
        self.now = timestamp_offset(self.now, 60)
        closed = self.store.conclude(fact)
        self.assertEqual((closed["status"], closed["outcome"], closed["closed_at"]), ("closed", "done", timestamp(self.now)))
        self.assertEqual(self.store.records(), [])
        row = self.store.inspect(fact)
        self.assertEqual((row["lifecycle"], row["effective_state"], row["valid_until"]), ("active", "expired", timestamp(self.now)))
        self.assertEqual([r["id"] for r in self.store.records(history=True)], [fact])
        self.assertEqual(self.store.close_metrics(), {"done": 1, "cancelled": 0, "ended": 0})

    def test_a_close_is_not_regret(self):
        packet, _, _ = self.prompt("Our next step is migrating billing to Stripe.")
        fact = self.capture(packet, {"entity": "billing", "predicate": "next_step", "value": "Migrate billing to Stripe"})[0]["id"]
        before = self.store.regret_metrics()
        self.now = timestamp_offset(self.now, 3600)
        self.store.conclude(fact, "done")
        after = self.store.regret_metrics()
        self.assertEqual(after["taken_back"], before["taken_back"])
        self.assertEqual(after["precision_estimate"], 1.0)

    def test_what_rested_on_it_is_flagged_for_review(self):
        freeze = self.remember("team", "constraint", "Hiring freeze until further notice")
        plan = self.remember("team", "plan", "Cover the launch with the current team")
        self.store.depend(plan, freeze)
        self.now = timestamp_offset(self.now, 60)
        closed = self.store.conclude(freeze, "ended")
        self.assertEqual(closed["review_needed"], [plan])
        self.assertTrue(self.store.inspect(plan)["stale"])

    def test_only_a_current_fact_closes_and_never_in_the_future(self):
        fact = self.remember("billing", "next_step", "Migrate billing to Stripe")
        self.now = timestamp_offset(self.now, 60)
        with self.assertRaises(KernelError):
            self.store.conclude(fact, "finished")
        with self.assertRaises(KernelError):
            self.store.conclude(fact, at=timestamp_offset(self.now, 3600))
        self.store.conclude(fact)
        with self.assertRaises(KernelError):
            self.store.conclude(fact)

    def test_reopen_restores_the_end_it_had(self):
        packet, _, _ = self.prompt("Decidimos publicar la v0.3 esta semana.")
        fact = self.capture(packet, {"entity": "context_kernel", "predicate": "decision", "value": "Publicar v0.3 esta semana"})[0]["id"]
        until = self.store.inspect(fact)["valid_until"]
        self.now = timestamp_offset(self.now, 60)
        self.store.conclude(fact, "cancelled")
        self.assertEqual(self.store.records(), [])
        reopened = self.store.reopen(fact)
        self.assertEqual((reopened["effective_state"], reopened["valid_until"]), ("active", until))
        with self.assertRaises(KernelError):
            self.store.reopen(fact)

    def test_reopen_refuses_when_a_newer_value_is_current(self):
        fact = self.remember("billing", "next_step", "Migrate billing to Stripe")
        self.now = timestamp_offset(self.now, 60)
        self.store.conclude(fact)
        self.remember("billing", "next_step", "Move invoices to PDF")
        with self.assertRaises(KernelError):
            self.store.reopen(fact)

    def test_the_database_stays_readable_by_older_kernels(self):
        fact = self.remember("billing", "next_step", "Migrate billing to Stripe")
        self.now = timestamp_offset(self.now, 60)
        self.store.conclude(fact)
        db = sqlite3.connect(self.store.path)
        try:
            lifecycle, until = db.execute("SELECT lifecycle, valid_until FROM statements WHERE id=?", (fact,)).fetchone()
            version = db.execute("SELECT value FROM metadata WHERE key='schema_version'").fetchone()[0]
        finally:
            db.close()
        self.assertEqual((lifecycle, until, version, SCHEMA_VERSION), ("active", timestamp(self.now), "6", "6"))


class CloseTool(Base):
    def setUp(self):
        super().setUp()
        self.fact = self.store.remember("billing", "next_step", "Migrate billing to Stripe",
                                        "Our next step is migrating billing to Stripe.")["id"]

    def test_the_user_saying_it_is_done_closes_it(self):
        packet, event, _ = self.prompt("We shipped the billing migration to Stripe yesterday.")
        result, error = self.tool("memory_close", {"token": packet["turn"]["token"], "id": self.fact})
        self.assertFalse(error, result)
        self.assertEqual((result["status"], result["outcome"]), ("closed", "done"))
        self.assertEqual(result["receipt"], "Memory: closed billing.next_step (done).")
        self.assertEqual(self.store.records(), [])
        asked = [c for c in self.judge.calls if c[0] == "ask"][-1]
        self.assertEqual(set(asked[2]), {"asked", "asked2"})
        # The Stop hook's line says it too, and the turn is not nudged to capture instead.
        line = self.stop(event, "Congrats on shipping it.")
        self.assertIn("closed billing.next_step (done)", line["systemMessage"])
        self.assertNotIn("decision", line)
        self.assertEqual(self.store.regret_metrics()["taken_back"], {"undone": 0, "forgotten": 0, "revoked": 0, "corrected": 0})

    def test_words_that_do_not_say_it_change_nothing(self):
        self.judge.ask_fn = answers(asked=0.2)
        packet, _, _ = self.prompt("We're halfway through the billing migration.")
        result, error = self.tool("memory_close", {"token": packet["turn"]["token"], "id": self.fact, "outcome": "done"})
        self.assertTrue(error)
        self.assertEqual([r["id"] for r in self.store.records()], [self.fact])

    def test_either_question_can_say_it(self):
        self.judge.ask_fn = judged(asked=0.1, asked2=0.93)
        packet, _, _ = self.prompt("Al final no migramos el billing a Stripe.")
        result, error = self.tool("memory_close", {"token": packet["turn"]["token"], "id": self.fact, "outcome": "cancelled"})
        self.assertFalse(error, result)
        self.assertEqual(result["outcome"], "cancelled")

    def test_an_unknown_outcome_is_refused(self):
        packet, _, _ = self.prompt("We shipped the billing migration to Stripe yesterday.")
        result, error = self.tool("memory_close", {"token": packet["turn"]["token"], "id": self.fact, "outcome": "finished"})
        self.assertTrue(error)
        self.assertEqual([r["id"] for r in self.store.records()], [self.fact])

    def test_the_tool_and_the_instructions_name_it(self):
        tool = next(t for t in TOOLS if t["name"] == "memory_close")
        self.assertEqual(tool["inputSchema"]["required"], ["token", "id"])
        self.assertFalse(tool["annotations"]["destructiveHint"])
        self.assertIn("memory_close", INSTRUCTIONS)
        self.assertEqual(len(ASKS["memory_close"]), 2)
        self.assertEqual(len(QUESTIONS["close_asked"]), 2)


class CloseRequest(Base):
    def test_completion_words_with_claims_ask_the_agent_to_look(self):
        self.store.remember("billing", "next_step", "Migrate billing to Stripe", "Our next step is migrating billing to Stripe.")
        _, _, response = self.prompt("We shipped the billing migration to Stripe yesterday.")
        context = response["hookSpecificOutput"]["additionalContext"]
        self.assertIn("call memory_close with token", context)

    def test_no_request_without_completion_words_or_without_claims(self):
        _, _, response = self.prompt("We shipped the billing migration to Stripe yesterday.")
        self.assertNotIn("memory_close", response["hookSpecificOutput"]["additionalContext"])
        self.store.remember("billing", "next_step", "Migrate billing to Stripe", "Our next step is migrating billing to Stripe.")
        _, _, response = self.prompt("How is the billing migration to Stripe going?")
        self.assertNotIn("memory_close", response["hookSpecificOutput"]["additionalContext"])

    def test_completion_words(self):
        for said in ("Ya publicamos la v0.3", "listo, ya está", "We shipped it", "Cancelamos el viaje", "the freeze is over",
                     "Ya no estoy de guardia", "terminé el informe"):
            self.assertTrue(done_statement(said), said)
        for said in ("De hecho, mi manager es Dani", "¿Cómo va la migración?", "Publicaremos el viernes"):
            self.assertFalse(done_statement(said), said)


class Measured(unittest.TestCase):
    def test_close_rows_are_labelled_both_ways(self):
        rows = [json.loads(line) for line in (Path(__file__).resolve().parent.parent / "fixtures" / "calibration.jsonl")
                .read_text(encoding="utf-8").splitlines() if line.strip()]
        close = [r for r in rows if r["kind"] == "close_asked"]
        self.assertEqual((len(close), sum(r["label"] for r in close)), (35, 19))


if __name__ == "__main__":
    unittest.main()
