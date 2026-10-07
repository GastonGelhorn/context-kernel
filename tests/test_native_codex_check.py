"""Native evaluation configuration checks without launching a model."""

import os
from pathlib import Path
import subprocess
import unittest
from unittest.mock import patch

from shelflife_context.common import KernelError
from tests.native_codex_check import child_environment, invoke, preflight, summary


class NativeConfigurationTests(unittest.TestCase):
    def test_child_environment_removes_api_credentials(self):
        names = ("OPENAI_API_KEY", "CODEX_API_KEY", "CODEX_ACCESS_TOKEN", "OPENAI_BASE_URL")
        with patch.dict(os.environ, {name: "fixture" for name in names}):
            environment = child_environment()
            self.assertTrue(all(name not in environment for name in names))
            self.assertEqual(os.environ["OPENAI_API_KEY"], "fixture")

    @patch("tests.native_codex_check.subprocess.run")
    def test_subscription_preflight_rejects_api_auth(self, run):
        run.return_value = subprocess.CompletedProcess([], 0, "Logged in using an API key", "")
        with self.assertRaises(KernelError):
            preflight("codex", "chatgpt", "chosen-model")

    @patch("tests.native_codex_check.subprocess.run")
    def test_subscription_command_is_explicit_and_scoped(self, run):
        run.return_value = subprocess.CompletedProcess([], 0, "", "")
        invoke("codex", Path("/fictional/pilot"), Path("/fictional/pilot/memory.sqlite"), "fixture",
               provider="chatgpt", model="chosen-model", reasoning="high")
        command = run.call_args.args[0]
        self.assertIn('forced_login_method="chatgpt"', command)
        self.assertIn('model_provider="openai"', command)
        self.assertIn('model_reasoning_effort="high"', command)
        self.assertNotIn("--oss", command)
        self.assertNotIn("--dangerously-bypass-hook-trust", command)
        self.assertIn("--ignore-user-config", command)
        self.assertIn("shell_tool", command)
        self.assertIn("read-only", command)

    @patch("tests.native_codex_check.subprocess.run")
    def test_control_command_has_no_memory_server(self, run):
        run.return_value = subprocess.CompletedProcess([], 0, "", "")
        invoke("codex", Path("/fictional/pilot"), Path("/fictional/pilot/memory.sqlite"), "fixture", mcp=False)
        command = run.call_args.args[0]
        self.assertIn("--oss", command)
        self.assertFalse(any("mcp_servers" in argument for argument in command))

    def test_exact_answer_without_context_call_is_not_a_memory_pass(self):
        result = {"exit_code": 0, "answer": "Nyra Vale", "duration_seconds": 1,
                  "tool_calls": [], "events": []}
        self.assertFalse(summary(result, "Nyra Vale")["passed"])

    def test_failed_tool_call_is_not_successful_retrieval(self):
        result = {"exit_code": 0, "answer": "UNKNOWN", "duration_seconds": 1,
                  "tool_calls": [{"server": "shelflife-context", "tool": "memory_context", "status": "failed",
                                  "result": {"structured_content": None}}], "events": []}
        measured = summary(result, "UNKNOWN")
        self.assertFalse(measured["passed"])
        self.assertEqual(measured["failed_context_tool_calls"], 1)


if __name__ == "__main__":
    unittest.main()
