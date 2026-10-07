from pathlib import Path
import tempfile
import unittest

from shelflife_context.adapters import propose_command
from shelflife_context.common import KernelError, canonical
from shelflife_context.compiler import Compiler
from shelflife_context.demo import histories
from shelflife_context.store import Store


class TransitionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.temp.name) / "memory.sqlite", create=True)

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def values(self, entity="laptop"):
        return {r["predicate"]: r["value"] for r in self.store.records() if r["entity_key"] == entity}

    def test_order_arrival_return_are_distinct_atomic_states(self):
        self.store.transition("laptop", "ordered", "I ordered laptop.")
        self.assertEqual(self.values(), {"delivery_status": "pending", "ownership_status": "not_received", "open_loop": "awaiting_delivery"})
        self.store.transition("laptop", "arrived", "laptop arrived.")
        self.assertEqual(self.values()["ownership_status"], "owned")
        self.assertEqual(self.values()["open_loop"], "resolved")
        self.store.transition("laptop", "returned", "I returned laptop.")
        self.assertEqual(self.values()["delivery_status"], "delivered")
        self.assertEqual(self.values()["ownership_status"], "returned")
        self.assertEqual(len(self.store.records()), 3)
        self.assertGreater(len(self.store.records(history=True)), 3)

    def test_negative_statement_does_not_close_delivery_loop(self):
        self.store.transition("laptop", "ordered", "Ordered")
        proposal = propose_command(self.store, "It has not arrived.")
        self.store.approve(proposal["id"])
        self.assertEqual(self.values()["open_loop"], "awaiting_delivery")
        self.assertEqual(self.values()["ownership_status"], "not_received")

    def test_unique_reference_creates_pending_proposal(self):
        self.store.transition("laptop", "ordered", "Ordered")
        proposal = propose_command(self.store, "It finally arrived.")
        self.assertEqual(self.values()["delivery_status"], "pending")
        self.store.approve(proposal["id"])
        self.assertEqual(self.values()["delivery_status"], "delivered")

    def test_ambiguous_reference_never_guesses(self):
        for entity in ("laptop", "camera"):
            self.store.transition(entity, "ordered", "Ordered")
        with self.assertRaisesRegex(KernelError, "ambiguous"):
            propose_command(self.store, "It finally arrived.")
        projection = Compiler(self.store).project("Did it arrive?")
        self.assertIn("clarification_required", projection.trace["warnings"])
        self.assertEqual(projection.trace["selected"], [])
        self.assertEqual(self.store.proposals(), [])

    def test_return_reference_uses_owned_object_not_pending_object(self):
        self.store.transition("laptop", "ordered", "Ordered")
        self.store.transition("laptop", "arrived", "Arrived")
        self.store.transition("camera", "ordered", "Ordered")
        proposal = propose_command(self.store, "I returned it.")
        self.store.approve(proposal["id"])
        self.assertEqual(self.values()["ownership_status"], "returned")
        self.assertEqual(self.values("camera")["open_loop"], "awaiting_delivery")

    def test_stale_transition_proposal_is_not_applied(self):
        self.store.transition("laptop", "ordered", "Ordered")
        proposal = propose_command(self.store, "It finally arrived.")
        self.store.transition("laptop", "returned", "Returned through owner CLI")
        with self.assertRaisesRegex(KernelError, "stale"):
            self.store.approve(proposal["id"])
        self.assertEqual(self.values()["ownership_status"], "returned")
        self.assertEqual(self.store.proposals()[0]["status"], "pending")

    def test_repeated_transition_does_not_create_duplicate_versions(self):
        self.store.transition("laptop", "ordered", "Ordered")
        before = self.store.records(history=True)
        self.store.transition("laptop", "ordered", "Ordered again")
        self.assertEqual(before, self.store.records(history=True))

    def test_conflicting_targets_roll_back_entire_transition(self):
        self.store.transition("laptop", "ordered", "Ordered")
        self.store.remember("laptop", "ownership_status", "owned", "Conflicting statement")
        before = canonical(self.store.records(history=True))
        with self.assertRaises(KernelError):
            self.store.transition("laptop", "returned", "Returned")
        self.assertEqual(before, canonical(self.store.records(history=True)))

    def test_projection_reconstruction_keeps_defined_recent_messages(self):
        stale = [{"role": "user", "content": "First"}, {"role": "assistant", "content": "Old answer"},
                 {"role": "user", "content": "Second"}, {"role": "assistant", "content": "Last answer"},
                 {"role": "user", "content": "Third"}]
        result = histories(stale, "Current context")
        contents = [m["content"] for m in result["reconstructed"]]
        self.assertNotIn("First", contents)
        self.assertIn("Second", contents)
        self.assertIn("Third", contents)
        self.assertIn("Last answer", contents)
        self.assertNotIn("Old answer", contents)


if __name__ == "__main__":
    unittest.main()
