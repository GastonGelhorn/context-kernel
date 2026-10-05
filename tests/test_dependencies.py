"""A recommendation rests on assumptions; when one changes, the conclusion is flagged, not replaced."""

from contextlib import closing
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

from context_kernel.common import KernelError, canonical, timestamp
from context_kernel.compiler import Compiler
from context_kernel.planner import Need, NeedPlan
from context_kernel.store import SCHEMA_VERSION, Store


class DependencyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.now = "2026-10-05T12:00:00+00:00"
        self.store = Store(Path(self.temp.name) / "memory.sqlite", create=True, clock=lambda: timestamp(self.now))
        self.compiler = Compiler(self.store)

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def add(self, entity, predicate, value, **kwargs):
        return self.store.remember(entity, predicate, value, canonical(value), kind="project", **kwargs)

    def deadline_and_rewrite(self):
        deadline = self.add("checkout", "deadline", "three months")
        rewrite = self.add("checkout", "decision", "Rewrite the payment module before launch")
        self.store.depend(rewrite["id"], deadline["id"])
        return deadline, rewrite

    def test_dependency_is_explicit_and_inspectable(self):
        deadline, rewrite = self.deadline_and_rewrite()
        row = self.store.inspect(rewrite["id"])
        self.assertEqual([a["id"] for a in row["assumptions"]], [deadline["id"]])
        self.assertFalse(row["stale"])
        self.assertEqual([r["id"] for r in self.store.dependents(deadline["id"])], [rewrite["id"]])
        self.assertEqual(self.store.depend(rewrite["id"], deadline["id"])["status"], "exists")

    def test_correcting_an_assumption_marks_the_dependent_stale_without_changing_it(self):
        deadline, rewrite = self.deadline_and_rewrite()
        self.now = "2026-10-06T12:00:00+00:00"
        new_deadline = self.store.correct(deadline["id"], "three weeks", "The deadline moved to three weeks.")
        row = self.store.inspect(rewrite["id"])
        self.assertTrue(row["stale"])
        self.assertEqual(row["value"], "Rewrite the payment module before launch")
        self.assertEqual(row["effective_state"], "active")
        self.assertEqual(row["assumptions"][0]["effective_state"], "superseded")
        self.assertEqual(row["assumptions"][0]["superseded_by"], new_deadline["id"])
        self.assertEqual([r["id"] for r in self.store.stale()], [rewrite["id"]])

    def test_projection_flags_stale_recommendation_and_names_the_changed_assumption(self):
        deadline, rewrite = self.deadline_and_rewrite()
        self.now = "2026-10-06T12:00:00+00:00"
        new_deadline = self.store.correct(deadline["id"], "three weeks", "The deadline moved to three weeks.")
        projection = self.compiler.project("Should we go ahead with the rewrite for the checkout project?")
        self.assertEqual(projection.trace["status"], "review_required")
        self.assertIn("stale_dependents", projection.trace["warnings"])
        claims = {c["id"]: c for c in json.loads(projection.content)["claims"]}
        self.assertEqual(claims[rewrite["id"]]["stale_assumptions"][0]["id"], deadline["id"])
        self.assertEqual(claims[rewrite["id"]]["stale_assumptions"][0]["superseded_by"], new_deadline["id"])
        self.assertNotIn("three months", projection.content)
        self.assertIn("three weeks", projection.content)
        self.assertEqual(projection.trace["reasons"][new_deadline["id"]], "changed_assumption")

    def test_selecting_the_new_assumption_pulls_in_its_stale_dependents(self):
        deadline, rewrite = self.deadline_and_rewrite()
        self.now = "2026-10-06T12:00:00+00:00"
        self.store.correct(deadline["id"], "three weeks", "Moved.")
        # A plan about the deadline alone would not select the decision; the stale link does.
        projection = self.compiler.project("When is the deadline?", plan=NeedPlan((Need(("deadline",), ("checkout",)),), "oracle"))
        self.assertEqual(projection.trace["reasons"][rewrite["id"]], "stale_dependent")

    def test_fresh_dependents_are_not_flagged(self):
        deadline, rewrite = self.deadline_and_rewrite()
        projection = self.compiler.project("Should we go ahead with the rewrite for the checkout project?")
        self.assertEqual(projection.trace["status"], "ok")
        self.assertTrue(all("stale_assumptions" not in c for c in json.loads(projection.content)["claims"]))

    def test_reaffirm_moves_the_link_to_the_current_version(self):
        deadline, rewrite = self.deadline_and_rewrite()
        new_deadline = self.store.correct(deadline["id"], "three weeks", "Moved.")
        result = self.store.reaffirm(rewrite["id"])
        self.assertEqual(result["moved"], [{"from": deadline["id"], "to": new_deadline["id"]}])
        self.assertFalse(self.store.inspect(rewrite["id"])["stale"])
        with self.assertRaisesRegex(KernelError, "no stale"):
            self.store.reaffirm(rewrite["id"])

    def test_revoked_assumption_cannot_be_reaffirmed(self):
        deadline, rewrite = self.deadline_and_rewrite()
        self.store.revoke(deadline["id"])
        self.assertTrue(self.store.inspect(rewrite["id"])["stale"])
        with self.assertRaisesRegex(KernelError, "no current successor"):
            self.store.reaffirm(rewrite["id"])

    def test_correcting_the_dependent_starts_a_version_without_inherited_links(self):
        deadline, rewrite = self.deadline_and_rewrite()
        self.store.correct(deadline["id"], "three weeks", "Moved.")
        revised = self.store.correct(rewrite["id"], "Patch the payment module; no rewrite", "Reviewed after the deadline change.")
        self.assertEqual(revised["assumptions"], [])
        self.assertFalse(revised["stale"])
        self.assertEqual(self.store.stale(), [])

    def test_cycles_self_links_and_inactive_sources_are_rejected(self):
        deadline, rewrite = self.deadline_and_rewrite()
        with self.assertRaisesRegex(KernelError, "cycle"):
            self.store.depend(deadline["id"], rewrite["id"])
        with self.assertRaisesRegex(KernelError, "itself"):
            self.store.depend(deadline["id"], deadline["id"])
        self.store.revoke(rewrite["id"])
        with self.assertRaisesRegex(KernelError, "active"):
            self.store.depend(rewrite["id"], deadline["id"])

    def test_dependencies_stay_within_scope(self):
        deadline, rewrite = self.deadline_and_rewrite()
        other = Store(self.store.path, "private", clock=lambda: timestamp(self.now))
        try:
            with self.assertRaises(KernelError):
                other.depend(rewrite["id"], deadline["id"])
            self.assertEqual(other.stale(), [])
        finally:
            other.close()

    def test_forgetting_an_assumption_removes_its_links(self):
        deadline, rewrite = self.deadline_and_rewrite()
        self.store.forget(deadline["id"])
        self.assertEqual(self.store.inspect(rewrite["id"])["assumptions"], [])

    def test_stale_state_changes_the_delivery_snapshot(self):
        deadline, rewrite = self.deadline_and_rewrite()
        projection = self.compiler.project("Should we go ahead with the rewrite for the checkout project?")
        self.store.correct(deadline["id"], "three weeks", "Moved.")
        with self.assertRaises(KernelError):
            self.compiler.revalidate(projection)

    def test_unrelated_facts_do_not_invalidate_delivery(self):
        self.add("checkout", "deadline", "three months")
        projection = self.compiler.project("checkout deadline", strategy="fts")
        self.store.remember("user", "favorite_color", "Blue", "Unrelated")
        self.compiler.revalidate(projection)

    def test_version_one_database_is_migrated_without_losing_rows(self):
        path = Path(self.temp.name) / "legacy.sqlite"
        legacy = Store(path, create=True)
        try:
            for entity in ("task", "project"):
                legacy.remember(entity, "label", entity, entity)
            legacy.relate("task", "project")
            with legacy.db:
                legacy.db.execute("UPDATE metadata SET value='1' WHERE key='schema_version'")
        finally:
            legacy.close()
        reopened = Store(path)
        try:
            self.assertEqual(reopened.db.execute("SELECT value FROM metadata WHERE key='schema_version'").fetchone()[0], SCHEMA_VERSION)
            self.assertEqual(reopened.db.execute("SELECT count(*) FROM relations WHERE kind='part_of'").fetchone()[0], 1)
            self.assertEqual(reopened.db.execute("SELECT count(*) FROM sqlite_master WHERE name='relations_v1'").fetchone()[0], 0)
        finally:
            reopened.close()

    def test_unknown_schema_versions_are_refused(self):
        path = Path(self.temp.name) / "future.sqlite"
        with closing(sqlite3.connect(path)) as db, db:
            db.execute("CREATE TABLE metadata(key TEXT PRIMARY KEY, value TEXT NOT NULL)")
            db.execute("INSERT INTO metadata VALUES('schema_version','99')")
        with self.assertRaisesRegex(KernelError, "Unsupported"):
            Store(path)


if __name__ == "__main__":
    unittest.main()
