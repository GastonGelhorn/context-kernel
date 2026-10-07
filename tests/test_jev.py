"""Calibrated relevance through the jev CLI: a judged inventory, metadata-only traces, fail-open."""

import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import unittest

from shelflife_context.adapters import configuration, hook_response
from shelflife_context.common import KernelError, canonical
from shelflife_context.compiler import Compiler
from shelflife_context.planner import Jev, jev_candidate, jev_plan
from shelflife_context.store import Store


ROOT = Path(__file__).resolve().parent.parent
FAKE = '''#!/usr/bin/env python3
import json, os, sys
if "--dry-run" in sys.argv:
    print(json.dumps({"url": os.environ.get("FAKE_JEV_URL", "http://localhost:11434/v1/systemone"), "body": {"model": "fake"}}))
    sys.exit(0)
raw = sys.stdin.read()
if sys.argv[1] == "ask":
    request = json.loads(raw)
    preset = json.loads(os.environ.get("FAKE_JEV_ANSWERS", "{}"))
    answers = {}
    for qid, question in request["questions"].items():
        if question["type"] == "noul":
            answers[qid] = {"type": "noul", "noul": preset.get(qid, 0.9)}
        else:
            chosen = preset.get(qid, next(iter(question["criteria"])))
            answers[qid] = {"type": "choice", "probabilities": {k: (1.0 if k == chosen else 0.0) for k in question["criteria"]}}
    print(json.dumps({"answers": answers, "usage": {"input_tokens": 40}}))
    sys.exit(0)
candidates = [line for line in raw.split("\\n") if line]
query = sys.argv[sys.argv.index("--query") + 1]
if query.startswith("@"):  # as jev does: the value names a file to read
    query = open(query[1:], encoding="utf-8").read()
log = os.environ.get("FAKE_JEV_LOG")
if log:
    with open(log, "a") as handle:
        handle.write(json.dumps({"argv": sys.argv[1:], "candidates": candidates, "query": query}) + "\\n")
mode = os.environ.get("FAKE_JEV_MODE", "score")
if mode == "crash":
    print(json.dumps({"error": "7 questions would take about 14 s on the local model, more than the 10 s this call has", "exit_code": 4}), file=sys.stderr)
    sys.exit(4)
if mode == "garbage":
    print("not json"); sys.exit(0)
scores = json.loads(os.environ.get("FAKE_JEV_SCORES", "{}"))
results = [{"i": i, "p": scores.get(c.split(":")[0], 0.1), "candidate": c} for i, c in enumerate(candidates)]
print(json.dumps({"results": results, "n": len(candidates), "requests": 1, "usage": {"input_tokens": 50, "output_tokens": 1}, "ms": 12}))
'''


class JevTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.workspace = Path(self.temp.name)
        self.fake = self.workspace / "fake-jev"
        self.fake.write_text(FAKE)
        self.fake.chmod(self.fake.stat().st_mode | stat.S_IXUSR)
        self.log = self.workspace / "calls.jsonl"
        os.environ["FAKE_JEV_LOG"] = str(self.log)
        os.environ.pop("FAKE_JEV_MODE", None)
        os.environ["FAKE_JEV_SCORES"] = json.dumps({"user allergy": 0.82, "user availability": 0.55, "user favorite color": 0.12})
        self.store = Store(self.workspace / "memory.sqlite", create=True)
        self.jev = Jev(str(self.fake), timeout=10)
        self.compiler = Compiler(self.store, jev=self.jev)

    def tearDown(self):
        self.store.close()
        for name in ("FAKE_JEV_LOG", "FAKE_JEV_MODE", "FAKE_JEV_SCORES"):
            os.environ.pop(name, None)
        self.temp.cleanup()

    def add(self, predicate, value, entity="user"):
        return self.store.remember(entity, predicate, value, canonical(value))

    def calls(self):
        return [json.loads(line) for line in self.log.read_text().splitlines()] if self.log.exists() else []

    def test_pairs_above_thresholds_become_needs_with_criticality(self):
        allergy, availability = self.add("allergy", "My friend cannot eat nuts"), self.add("availability", "Flexible hours needed")
        colour = self.add("favorite_color", "Blue")
        result = self.compiler.project("Would a snack hamper be a good gift for my friend?", strategy="jev")
        self.assertEqual(result.trace["reasons"][allergy["id"]], "critical_need")
        self.assertEqual(result.trace["reasons"][availability["id"]], "supporting_need")
        self.assertNotIn(colour["id"], result.trace["selected"])
        self.assertEqual(result.plan.strategy, "jev")
        self.assertEqual(result.trace["usage"]["scores"]["user.allergy"], 0.82)
        self.assertEqual(result.trace["usage"]["calls"], 1)

    def test_candidates_carry_the_pair_and_its_values_one_per_line(self):
        self.add("allergy", "My friend\ncannot eat nuts")
        self.add("allergy", "Also shellfish")
        self.compiler.project("A gift for my friend", strategy="jev")
        call = self.calls()[0]
        self.assertEqual(call["candidates"], ["user allergy: My friend cannot eat nuts | Also shellfish"])
        self.assertEqual(call["argv"][:2], ["rank", "--json"])
        self.assertIn(Jev.QUESTION, call["argv"])
        self.assertEqual(call["query"], "A gift for my friend")

    def test_user_text_never_reaches_argv(self):
        """The prompt goes in a private file named with `@`; candidate values go on stdin. A prompt that
        starts with `@` is judged as written, not read as a path."""
        self.add("allergy", "Secret Nut Canary")
        for prompt in ("A gift for my friend, canary-7781", "@/etc/hosts canary-7781 gift for my friend"):
            self.compiler.project(prompt, strategy="jev")
        reply = "With canary-9932 in mind, I recommend we drop the hamper and pick flowers instead."
        self.jev.rank(reply, ["user allergy: Secret Nut Canary"], no_cache=True)
        calls = self.calls()
        self.assertEqual(len(calls), 3)
        for call in calls:
            argv = " ".join(call["argv"])
            for secret in ("canary-7781", "canary-9932", "Nut Canary", "/etc/hosts"):
                self.assertNotIn(secret, argv)
        self.assertEqual(calls[1]["query"], "@/etc/hosts canary-7781 gift for my friend")
        self.assertEqual(calls[2]["query"], reply)
        leftovers = [p for p in Path(tempfile.gettempdir()).glob("shelflife-context-*")]
        self.assertEqual(leftovers, [])

    def test_trace_never_copies_values_only_pair_scores(self):
        self.add("allergy", "Nut Canary")
        result = self.compiler.project("A gift for my friend", strategy="jev")
        self.assertNotIn("Nut Canary", canonical(result.trace))
        self.assertNotIn("Nut Canary", canonical(result.plan.to_dict()))

    def test_generic_question_and_empty_memory_make_no_call(self):
        self.assertEqual(self.compiler.project("Explain a SQLite primary key.", strategy="jev").trace["usage"]["calls"], 0)
        self.assertEqual(self.compiler.project("My plans", strategy="jev").trace["usage"]["calls"], 0)
        self.assertEqual(self.calls(), [])

    def test_failure_falls_back_to_rules_with_a_visible_warning(self):
        allergy = self.add("allergy", "My friend cannot eat nuts")
        for mode in ("crash", "garbage"):
            os.environ["FAKE_JEV_MODE"] = mode
            result = self.compiler.project("A gift for my friend", strategy="jev")
            self.assertIn("jev_unavailable", result.trace["warnings"])
            self.assertNotEqual(result.trace["status"], "unavailable")
            self.assertIn(allergy["id"], result.trace["selected"])
            self.assertIn("failure", result.trace["usage"])
        self.assertNotIn("Traceback", canonical(result.trace))
        os.environ["FAKE_JEV_MODE"] = "crash"
        result = self.compiler.project("A gift for my friend", strategy="jev")
        self.assertIn("status 4: {\"error\": \"7 questions", result.trace["usage"]["failure"])

    def test_missing_executable_fails_open_in_the_hook(self):
        self.add("allergy", "My friend cannot eat nuts")
        compiler = Compiler(self.store, jev=Jev(str(self.workspace / "absent-jev")))
        response, projection_id = hook_response({"cwd": str(self.workspace), "prompt": "A gift for my friend"},
                                                self.workspace, self.store, compiler, strategy="jev")
        self.assertIsNotNone(projection_id)
        self.assertIn("jev_unavailable", response["systemMessage"])
        self.assertIn("nuts", response["hookSpecificOutput"]["additionalContext"])

    def test_timeout_is_bounded_and_does_not_hang(self):
        self.add("allergy", "x")
        slow = self.workspace / "slow-jev"
        slow.write_text("#!/bin/sh\nsleep 5\n")
        slow.chmod(slow.stat().st_mode | stat.S_IXUSR)
        with self.assertRaisesRegex(KernelError, "unavailable or timed out"):
            Jev(str(slow), timeout=0.2).rank("q", ["c"])

    def test_invalid_scores_are_rejected(self):
        self.add("allergy", "x")
        os.environ["FAKE_JEV_SCORES"] = json.dumps({"user allergy": 1.5})
        with self.assertRaisesRegex(KernelError, "Invalid jev response"):
            self.jev.rank("q", ["user allergy: x"])

    def test_at_most_sixteen_needs_are_kept(self):
        os.environ["FAKE_JEV_SCORES"] = json.dumps({f"user p{n}": 0.9 for n in range(20)})
        records = [self.add(f"p{n}", n) for n in range(20)]
        plan, usage = jev_plan("my numbers", self.store.records(), self.jev)
        self.assertEqual(len(plan.needs), 16)
        self.assertEqual(len(usage["scores"]), 20)

    def test_uncertain_band_is_decided_by_a_lexical_match_not_a_lower_bar(self):
        os.environ["FAKE_JEV_SCORES"] = json.dumps({"user manager": 0.467, "user favorite color": 0.467})
        manager, colour = self.add("manager", "Dani"), self.add("favorite_color", "Blue")
        result = self.compiler.project("quien es mi manager?", strategy="jev")
        # v0.8: the question's words and jev's band agree, which counts as much as a high score alone.
        self.assertEqual(result.trace["reasons"][manager["id"]], "critical_need")
        self.assertNotIn(colour["id"], result.trace["selected"])
        self.assertEqual(result.trace["usage"]["lexical_matches"], ["user.manager"])
        os.environ["FAKE_JEV_SCORES"] = json.dumps({"user manager": 0.2})
        # A new question is judged afresh (the first answer is cached for its own wording only).
        self.assertEqual(self.compiler.project("y mi manager actual?", strategy="jev").trace["selected"], [])

    def test_inventory_beyond_the_cap_judges_mentioned_then_recent_pairs(self):
        os.environ["FAKE_JEV_SCORES"] = json.dumps({"user manager": 0.9, "old note": 0.9})
        self.store.remember("old", "note", "earliest", "earliest")
        for n in range(5):
            self.add(f"p{n}", n)
        manager = self.add("manager", "Dani")
        jev = Jev(str(self.fake), max_pairs=3)
        result = Compiler(self.store, jev=jev).project("quien es mi manager?", strategy="jev")
        self.assertIn("jev_inventory_capped", result.trace["warnings"])
        self.assertEqual(result.trace["usage"]["judged_pairs"], 3)
        self.assertEqual(result.trace["usage"]["unjudged_pairs"], 4)
        self.assertIn(manager["id"], result.trace["selected"])
        self.assertNotIn("old.note", result.trace["usage"]["scores"])
        self.assertEqual(len(self.calls()[0]["candidates"]), 3)

    def test_a_hosted_backend_never_receives_the_inventory(self):
        self.add("allergy", "My friend cannot eat nuts")
        os.environ["FAKE_JEV_URL"] = "https://openrouter.ai/api/v1/systemone"
        try:
            result = Compiler(self.store, jev=Jev(str(self.fake))).project("A gift for my friend", strategy="jev")
        finally:
            os.environ.pop("FAKE_JEV_URL")
        self.assertIn("judge_remote", result.trace["warnings"])
        self.assertEqual(self.calls(), [])  # only the dry run ran, and it sends nothing

    def test_ask_sends_the_request_on_stdin_and_reads_both_question_types(self):
        os.environ["FAKE_JEV_ANSWERS"] = json.dumps({"a": 0.7, "c": "roles"})
        try:
            out, usage = Jev(str(self.fake)).ask({"text": "secret words"}, {"a": ("noul", "Is `text` x?"),
                                                                              "c": ("choice", "Which?", {"roles": "r", "limits": "l"})})
        finally:
            os.environ.pop("FAKE_JEV_ANSWERS")
        self.assertEqual(out["a"], 0.7)
        self.assertEqual(out["c"], {"roles": 1.0, "limits": 0.0})

    def test_thresholds_and_command_are_validated(self):
        for kwargs in ({"critical": 0.3, "supporting": 0.5}, {"supporting": 0}, {"timeout": 0}, {"command": ""}, {"band": 0.55}, {"max_pairs": 0}):
            with self.assertRaises(KernelError):
                Jev(**({"command": str(self.fake)} | kwargs))

    def test_candidate_rendering(self):
        self.assertEqual(jev_candidate("user", "favorite_color", ["Blue"]), "user favorite color: Blue")
        self.assertEqual(jev_candidate("user", "salary", [42000, {"type": "quantity", "amount": 1}]),
                         'user salary: 42000 | {"amount":1,"type":"quantity"}')

    def test_cli_project_and_adapter_use_the_strategy(self):
        self.add("allergy", "My friend cannot eat nuts")
        process = subprocess.run([sys.executable, "-m", "shelflife_context", "--db", str(self.store.path), "project",
                                  "A gift for my friend", "--strategy", "jev", "--jev-command", str(self.fake)],
                                 capture_output=True, text=True, cwd=ROOT, timeout=20)
        self.assertEqual(process.returncode, 0, process.stderr)
        self.assertEqual(json.loads(process.stdout)["plan"]["strategy"], "jev")
        for client in ("codex", "claude"):
            command = configuration(client, self.workspace, self.store.path, "personal", strategy="jev", jev_command=str(self.fake))["config"]["hooks"]["UserPromptSubmit"][0]["hooks"][0]["command"]
            self.assertIn("--strategy jev", command)
            self.assertIn("--jev-command " + str(self.fake.resolve()), command)
            self.assertNotIn("--fail-closed", command)
        server = configuration("claude", self.workspace, self.store.path, "personal", mode="mcp", strategy="jev", jev_command=str(self.fake))
        self.assertIn("--jev-command", server["config"]["mcpServers"]["shelflife-context"]["args"])
        with self.assertRaisesRegex(KernelError, "not found"):
            configuration("claude", self.workspace, self.store.path, "personal", strategy="jev", jev_command="definitely-missing-jev")

    def test_mcp_server_uses_the_configured_strategy(self):
        from shelflife_context.mcp import Server
        allergy = self.add("allergy", "My friend cannot eat nuts")
        server = Server(self.store, self.compiler, "jev")
        server.dispatch({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {}}})
        server.dispatch({"jsonrpc": "2.0", "method": "notifications/initialized"})
        response = server.dispatch({"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "memory_context", "arguments": {"query": "A gift for my friend"}}})
        self.assertEqual(response["result"]["structuredContent"]["plan"]["strategy"], "jev")
        self.assertIn(allergy["id"], response["result"]["structuredContent"]["trace"]["selected"])


if __name__ == "__main__":
    unittest.main()
