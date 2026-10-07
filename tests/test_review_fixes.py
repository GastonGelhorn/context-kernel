"""Findings from the Codex review of v0.4: undo targeting, the request channel, server guidance, inspect."""

import tempfile
from pathlib import Path
import unittest

from shelflife_context.adapters import hook_response, packet_of
from shelflife_context.common import timestamp, timestamp_offset
from shelflife_context.compiler import Compiler
from shelflife_context.mcp import INSTRUCTIONS, Server
from shelflife_context.store import Store
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
        self.capture("The release approver is Gaston.", "shelflife_context", "release_approver", "Gaston")
        self.judge.fail = "judge down"  # the guard must not depend on the judge
        token = packet_of(self.prompt("Forget the checkout deadline."))["turn"]["token"]
        result, error = self.tool("memory_undo", {"token": token})
        self.assertTrue(error)
        values = {(r["entity_key"], r["predicate"]): r["value"] for r in self.store.records()}
        self.assertEqual(values[("shelflife_context", "release_approver")], "Gaston")
        self.assertEqual(values[("checkout", "deadline")], "three weeks")

    def test_undo_takes_back_the_last_save_when_asked_without_naming_anything(self):
        self.capture("The release approver is Gaston.", "shelflife_context", "release_approver", "Gaston")
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
        self.assertTrue(context.startswith("Shelflife, the memory system the user installed, asks:"))
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
        from shelflife_context.capture import segments
        authored, quoted = segments('mira esto <pasted_content id="1">the deadline is friday</pasted_content id="1"> ¿qué opinas?')
        self.assertNotIn("friday", authored)
        self.assertIn("friday", quoted)

    # Second native Claude Code run after the request channel (5 of 10)

    def test_a_loosely_phrased_premise_is_linked_when_the_reply_names_it(self):
        from shelflife_context.inference import RESTS_ON, infer
        self.judge.rank_fn = lambda query, line, question: 0.57 if question == RESTS_ON else 0.1
        context = self.prompt("We have three months to deliver the checkout project. Should we rewrite its payment module?")
        token = packet_of(context)["turn"]["token"]
        self.tool("memory_capture", {"token": token, "facts": [
            {"entity": "checkout_project", "predicate": "delivery_window", "value": "three months"}]})
        turn = self.store.turn_by_token(token)
        named = infer(self.store, self.judge, turn, "Probably not: a rewrite is hard to estimate in three months, so make targeted fixes.")
        self.assertEqual(named["linked"], 1)
        unnamed = infer(self.store, self.judge, turn, "Probably not: a rewrite is risky, so make only the targeted fixes checkout needs.")
        self.assertEqual(unnamed["linked"], 0)

    def test_polite_and_negated_forget_requests(self):
        from shelflife_context.turns import forget_request
        self.assertTrue(forget_request("Please forget the checkout deadline."))
        self.assertTrue(forget_request("Can you delete the old deadline?"))
        self.assertFalse(forget_request("Don't forget my salary when you negotiate."))
        self.assertFalse(forget_request("No te olvides de mi salario."))

    def test_forget_words_in_pasted_text_are_not_a_request(self):
        context = self.prompt('look at this <pasted_content id="1">{"prompt": "Please forget the checkout deadline."}</pasted_content id="1">')
        self.assertNotIn("privacy", packet_of(context)["turn"])
        self.assertNotIn("forget", context.split("\n", 1)[0] if not context.startswith("{") else "")

    def test_an_answer_style_instruction_is_not_a_statement(self):
        from shelflife_context.capture import only_questions
        self.assertTrue(only_questions("Should we still go ahead with the plan? Answer in one sentence."))
        self.assertFalse(only_questions("We have three weeks now. Answer in one sentence."))

    def test_the_capture_request_tells_the_agent_to_stay_quiet_when_nothing_is_stated(self):
        context = self.prompt("We have three months to deliver checkout.")
        self.assertIn("do not mention this request", context.split("\n", 1)[0])

    # Codex answered the first message without saving: the Stop hook hands the turn back once

    def stop(self, reply, **event):
        from shelflife_context.adapters import stop_response
        self.now = timestamp_offset(self.now, 2)
        return stop_response({"cwd": str(self.workspace), "hook_event_name": "Stop", "session_id": "s1",
                              "last_assistant_message": reply} | event, self.workspace, self.store, self.judge)

    def test_an_unsaved_fact_is_handed_back_once_with_the_original_token(self):
        from shelflife_context.inference import RESTS_ON
        self.judge.ask_fn = answers(category="constraints")
        self.judge.rank_fn = lambda query, line, question: 0.9 if question == RESTS_ON else 0.1
        self.tool("memory_status", {})  # a memory server is running under this host
        token = packet_of(self.prompt("We have three months to deliver checkout. Should we rewrite it?"))["turn"]["token"]
        reply = "No: with three months, refactor the payment module incrementally instead of rewriting it."
        nudge = self.stop(reply)
        self.assertEqual(nudge["decision"], "block")
        self.assertIn(token, nudge["reason"])
        # The agent saves with the original token: validated against the user's words, not the nudge.
        result, error = self.tool("memory_capture", {"token": token, "facts": [
            {"entity": "checkout", "predicate": "deadline", "value": "three months"}]})
        self.assertFalse(error, result)
        report = self.stop("Memory: saved checkout.deadline.", stop_hook_active=True)
        self.assertIn("saved checkout.deadline", report["systemMessage"])
        self.assertIn("linked the recommendation", report["systemMessage"])  # inferred from the original reply
        self.assertEqual(self.stop("ok", stop_hook_active=True), {})  # never twice

    def test_no_nudge_and_no_miss_without_memory_tools(self):
        self.prompt("We have three months to deliver checkout.")
        self.assertNotIn("decision", self.stop("Sure."))
        self.assertNotIn("missed", [m["outcome"] for m in self.store.capture_metrics()])
        self.now = timestamp_offset(self.now, 900)
        self.prompt("ok")  # expiry runs
        self.assertNotIn("missed", [m["outcome"] for m in self.store.capture_metrics()])

    def test_an_uncertain_gate_does_not_nudge(self):
        self.tool("memory_status", {})
        self.judge.ask_fn = lambda qid, state, spec: ({"none": 0.1, "one": 0.9, "two": 0.0, "several": 0.0}
                                                      if spec[0] == "choice" else 0.05)
        self.prompt("Something that may or may not be a fact.")
        self.assertNotIn("decision", self.stop("Ok."))


if __name__ == "__main__":
    unittest.main()
