"""Findings from the Codex review of v0.4: undo targeting, the request channel, server guidance, inspect."""

import tempfile
from pathlib import Path
import unittest

from context_kernel.adapters import hook_response, packet_of
from context_kernel.common import timestamp, timestamp_offset
from context_kernel.compiler import Compiler
from context_kernel.mcp import INSTRUCTIONS, Server
from context_kernel.store import Store
from tests.fakes import FakeJudge, answers


HOST = [4243, "Tue Oct  6 01:00:00 2026"]


class ReviewFixTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.workspace = Path(self.temp.name)
        self.now = "2026-10-06T01:00:00+00:00"
        self.store = Store(self.workspace / "memory.sqlite", clock=lambda: timestamp(self.now), create=True)
        self.judge = FakeJudge()
        self.turns = 0

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def prompt(self, text):
        self.now = timestamp_offset(self.now, 5)
        self.turns += 1
        event = {"cwd": str(self.workspace), "prompt": text, "session_id": "s1", "prompt_id": f"p{self.turns}"}
        response, _ = hook_response(event, self.workspace, self.store, Compiler(self.store), judge=self.judge)
        self.store.register_session("s1", "codex", [HOST])
        return response["hookSpecificOutput"]["additionalContext"]

    def tool(self, name, arguments):
        server = Server(self.store, Compiler(self.store), judge=self.judge, parent=HOST)
        server.dispatch({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
            "protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {}}})
        server.dispatch({"jsonrpc": "2.0", "method": "notifications/initialized"})
        result = server.dispatch({"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                                  "params": {"name": name, "arguments": arguments}})["result"]
        return result["structuredContent"], result["isError"]

    def capture(self, text, entity, predicate, value):
        token = packet_of(self.prompt(text))["turn"]["token"]
        result, error = self.tool("memory_capture", {"token": token, "facts": [
            {"entity": entity, "predicate": predicate, "value": value}]})
        self.assertFalse(error, result)

    def test_undo_never_deletes_a_different_fact_than_the_one_named(self):
        self.capture("The checkout deadline is three weeks.", "checkout", "deadline", "three weeks")
        self.capture("The release approver is Gaston.", "context_kernel", "release_approver", "Gaston")
        self.judge.fail = "judge down"  # the guard must not depend on the judge
        token = packet_of(self.prompt("Forget the checkout deadline."))["turn"]["token"]
        result, error = self.tool("memory_undo", {"token": token})
        self.assertTrue(error)
        values = {(r["entity_key"], r["predicate"]): r["value"] for r in self.store.records()}
        self.assertEqual(values[("context_kernel", "release_approver")], "Gaston")
        self.assertEqual(values[("checkout", "deadline")], "three weeks")

    def test_undo_takes_back_the_last_save_when_asked_without_naming_anything(self):
        self.capture("The release approver is Gaston.", "context_kernel", "release_approver", "Gaston")
        token = packet_of(self.prompt("No, undo that."))["turn"]["token"]
        result, error = self.tool("memory_undo", {"token": token})
        self.assertFalse(error, result)
        self.assertEqual(self.store.records(), [])

    def test_a_forget_that_names_a_fact_is_not_an_undo(self):
        self.capture("The checkout deadline is three weeks.", "checkout", "deadline", "three weeks")
        token = packet_of(self.prompt("Please forget the checkout deadline."))["turn"]["token"]
        self.assertTrue(self.tool("memory_undo", {"token": token})[1])

    def test_the_kernels_requests_travel_outside_the_data_packet(self):
        context = self.prompt("We have three months to deliver checkout.")
        self.assertTrue(context.startswith("Context Kernel, the memory system the user installed, asks:"))
        packet = packet_of(context)
        self.assertIn(packet["turn"]["token"], context.split("\n", 1)[0])
        self.assertIn("memory_capture", context.split("\n", 1)[0])
        self.assertEqual(packet["type"], "context_data")

    def test_a_plain_turn_carries_only_the_packet(self):
        self.judge.ask_fn = answers(count="none")
        context = self.prompt("Thanks")
        self.assertTrue(context.startswith("{"))

    def test_server_guidance_describes_the_current_tools(self):
        self.assertNotIn("cannot approve, revoke, or forget", INSTRUCTIONS)
        for word in ("memory_capture", "turn.token", "memory_forget", "memory_undo", "captured_prompt"):
            self.assertIn(word, INSTRUCTIONS)

    def test_inspect_does_not_hand_out_held_records(self):
        held = self.store.remember("friend", "allergy", "nuts", "pasted", trust="quarantined")
        result, error = self.tool("memory_inspect", {"id": held["id"]})
        self.assertTrue(error)
        self.assertIn("held for review", result["error"]["message"])

    def test_pasted_content_blocks_are_not_authored_text(self):
        from context_kernel.capture import segments
        authored, quoted = segments('mira esto <pasted_content id="1">the deadline is friday</pasted_content id="1"> ¿qué opinas?')
        self.assertNotIn("friday", authored)
        self.assertIn("friday", quoted)


if __name__ == "__main__":
    unittest.main()
