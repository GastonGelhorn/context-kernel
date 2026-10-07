import json
from pathlib import Path
import tempfile
import unittest

from shelflife_context.common import KernelError, timestamp
from shelflife_context.store import Store


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.now = "2026-10-05T12:00:00+00:00"
        self.store = Store(Path(self.temp.name) / "memory.sqlite", clock=lambda: timestamp(self.now), create=True)

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def remember(self, value="Alex", **kwargs):
        return self.store.remember("user", "manager", value, f"My manager is {value}.", **kwargs)

    def test_registration_and_attribution(self):
        row = self.remember()
        self.assertEqual(row["value"], "Alex")
        self.assertEqual(row["source_kind"], "user_statement")
        self.assertEqual(row["effective_state"], "active")

    def test_hypotheses_are_not_current_evidence(self):
        self.remember(assertion_kind="hypothesis")
        self.assertEqual(self.store.records(), [])

    def test_correction_preserves_history(self):
        old = self.remember()
        self.now = "2026-10-06T12:00:00+00:00"
        new = self.store.correct(old["id"], "Sam", "My manager is now Sam.")
        self.assertEqual([r["value"] for r in self.store.records()], ["Sam"])
        self.assertEqual(self.store.inspect(old["id"])["effective_state"], "superseded")
        self.assertEqual([r["value"] for r in self.store.records(as_of="2026-10-05T15:00:00Z")], ["Alex"])
        self.assertNotEqual(old["id"], new["id"])

    def test_expiry_and_future_dates(self):
        row = self.remember(valid_until="2026-10-06T00:00:00Z")
        self.now = "2026-10-07T12:00:00+00:00"
        self.assertEqual(self.store.inspect(row["id"])["effective_state"], "expired")
        self.assertEqual(self.store.records(), [])
        future = self.remember("Sam", valid_from="2026-10-08T00:00:00Z")
        self.assertEqual(self.store.inspect(future["id"])["effective_state"], "scheduled")

    def test_immediate_correction_does_not_leave_two_current_versions(self):
        old = self.remember()
        self.store.correct(old["id"], "Sam", "My manager is now Sam.")
        self.assertEqual([r["value"] for r in self.store.records()], ["Sam"])

    def test_future_correction_changes_state_only_when_due(self):
        old = self.remember()
        self.store.correct(old["id"], "Sam", "Sam will be my manager.", valid_from="2026-11-01")
        self.assertEqual([r["value"] for r in self.store.records()], ["Alex"])
        self.now = "2026-11-02T12:00:00Z"
        self.assertEqual([r["value"] for r in self.store.records()], ["Sam"])

    def test_stale_correction_proposal_cannot_be_approved(self):
        old = self.remember()
        proposal = self.store.propose("correct", {"target_id": old["id"], "value": "Sam"})
        self.store.revoke(old["id"])
        with self.assertRaises(KernelError):
            self.store.approve(proposal["id"])
        self.assertEqual(self.store.proposals()[0]["status"], "pending")

    def test_malformed_proposals_are_rejected(self):
        with self.assertRaises(KernelError):
            self.store.propose("remember", {"value": "Alex"})

    def test_scope_cannot_inspect_correct_or_forget_other_records(self):
        row = self.remember()
        other = Store(self.store.path, "private", clock=lambda: timestamp(self.now))
        try:
            for action in [lambda: other.inspect(row["id"]), lambda: other.correct(row["id"], "Sam", "Correction"), lambda: other.forget(row["id"])]:
                with self.assertRaises(KernelError):
                    action()
            self.assertEqual(other.records(), [])
        finally:
            other.close()

    def test_conflicts_remain_explicit(self):
        self.remember("Alex")
        self.remember("Sam")
        self.assertEqual(len(self.store.records()), 2)

    def test_revocation_does_not_delete_evidence(self):
        row = self.remember()
        self.store.revoke(row["id"])
        self.assertEqual(self.store.records(), [])
        self.assertEqual(self.store.inspect(row["id"])["source_text"], "My manager is Alex.")

    def test_forget_removes_all_versions_and_derived_records(self):
        old = self.remember("Sensitive Canary")
        self.now = "2026-10-06T12:00:00+00:00"
        new = self.store.correct(old["id"], "Another Canary", "Another Canary is current.")
        self.store.save_plan({"needs": ["Sensitive Canary"]})
        self.store.save_trace({"id": "trace", "delivery": "prepared", "selected": [old["id"]]})
        self.store.propose("remember", {"entity": "user", "predicate": "note", "value": "Sensitive Canary"})
        self.store.forget(new["id"])
        dump = "\n".join(self.store.db.iterdump())
        self.assertNotIn("Sensitive Canary", dump)
        self.assertNotIn("Another Canary", dump)
        for table in ["statements", "evidence", "plans", "projections", "proposals"]:
            self.assertEqual(self.store.db.execute(f"SELECT count(*) FROM {table}").fetchone()[0], 0)

    def test_proposals_require_local_approval(self):
        proposal = self.store.propose("remember", {"entity": "user", "predicate": "manager", "value": "Sam"})
        self.assertEqual(self.store.records(), [])
        self.store.approve(proposal["id"])
        self.assertEqual(self.store.records()[0]["source_kind"], "user_confirmation")
        with self.assertRaises(KernelError):
            self.store.approve(proposal["id"])

    def test_invalid_dates_and_values_do_not_write_partial_records(self):
        for kwargs in [{"valid_until": "2020-01-01"}, {"valid_from": "next Tuesday"}]:
            with self.assertRaises(KernelError):
                self.remember(**kwargs)
        with self.assertRaises(KernelError):
            self.remember(float("nan"))
        self.assertEqual(self.store.status()["stored_statements"], 0)

    def test_ambiguous_alias_does_not_resolve(self):
        self.store.remember("alex_one", "city", "Paris", "Alex lives in Paris.")
        self.store.remember("alex_two", "city", "London", "Alex lives in London.")
        self.store.add_alias("alex_one", "Alex")
        self.store.add_alias("alex_two", "Alex")
        with self.assertRaises(KernelError):
            self.store.resolve_entity("Alex")

    def test_secret_patterns_are_rejected(self):
        with self.assertRaises(KernelError):
            self.store.remember("user", "credential", "password=bad", "password=bad")

    def test_schema_reopen_preserves_state(self):
        row = self.remember()
        reopened = Store(self.store.path)
        try:
            self.assertEqual(reopened.inspect(row["id"])["value"], "Alex")
        finally:
            reopened.close()


if __name__ == "__main__":
    unittest.main()
