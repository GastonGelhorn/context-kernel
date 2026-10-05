from pathlib import Path
import tempfile
import unittest

from context_kernel.common import KernelError, canonical, timestamp
from context_kernel.compiler import Compiler
from context_kernel.demo import run
from context_kernel.planner import Need, NeedPlan
from context_kernel.store import Store


class RegressionTests(unittest.TestCase):
    def test_disposable_end_to_end_demo(self):
        self.assertTrue(run()["passed"])

    def test_temporal_and_discretion_variants(self):
        # 120 deterministic scenarios: 15 variations of 8 checks, not a model benchmark.
        for variant in range(15):
            with self.subTest(variant=variant), tempfile.TemporaryDirectory() as directory:
                store = Store(Path(directory) / "memory.sqlite", create=True, clock=lambda: timestamp("2026-10-05T12:00:00Z"))
                try:
                    compiler = Compiler(store)
                    first = store.remember(f"project_{variant}", "project_status", "Waiting", "Not started", valid_from="2026-10-01")
                    second = store.correct(first["id"], "Complete", "Completed today")
                    self.assertEqual(store.records()[0]["id"], second["id"])
                    self.assertEqual(store.records("2026-10-02")[0]["id"], first["id"])
                    self.assertEqual(compiler.project("Explain SQLite.").content, "")
                    projection = compiler.project("My project status")
                    self.assertIn(second["id"], projection.trace["selected"])
                    self.assertNotIn(first["id"], projection.trace["selected"])
                    store.remember("draft", "project_status", "Guessed", "Maybe complete", assertion_kind="hypothesis")
                    self.assertNotIn("Guessed", compiler.project("My project status").content)
                    missing = compiler.project("My schedule", plan=NeedPlan((Need(("availability",)),), "oracle"))
                    self.assertIn("missing_critical_evidence", missing.trace["warnings"])
                    store.forget(first["id"])
                    self.assertNotIn("Complete", canonical(store.records(history=True)))
                finally:
                    store.close()

    def test_relation_cycles_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory) / "memory.sqlite", create=True)
            try:
                for entity in ("task", "project", "company"):
                    store.remember(entity, "label", entity, entity)
                store.relate("task", "project")
                store.relate("project", "company")
                with self.assertRaisesRegex(KernelError, "cycle"):
                    store.relate("company", "task")
            finally:
                store.close()

    def test_historical_projection_can_be_revalidated(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory) / "memory.sqlite", create=True, clock=lambda: timestamp("2026-10-05"))
            try:
                row = store.remember("user", "manager", "Alex", "Alex", valid_from="2026-10-01")
                store.correct(row["id"], "Blair", "Blair")
                compiler = Compiler(store)
                projection = compiler.project("My manager", strategy="fts", as_of="2026-10-02")
                compiler.revalidate(projection)
                self.assertIn("Alex", projection.content)
            finally:
                store.close()

    def test_new_conflict_invalidates_prepared_projection(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory) / "memory.sqlite", create=True)
            try:
                store.remember("user", "salary", 42000, "Annual salary")
                compiler = Compiler(store)
                projection = compiler.project("My salary")
                store.remember("user", "salary", 52000, "Conflicting salary")
                with self.assertRaises(KernelError):
                    compiler.revalidate(projection)
            finally:
                store.close()


if __name__ == "__main__":
    unittest.main()
