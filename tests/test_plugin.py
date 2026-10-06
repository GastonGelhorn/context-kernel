"""The installed entry point: options become kernel arguments, jev is found by absolute path, and
the Codex files the wizard writes point at the launcher."""

import io
import json
import os
import stat
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

from context_kernel import plugin
from context_kernel.setup import codex_files


class PluginTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.config = self.root / "config.json"
        patcher = mock.patch.object(plugin, "CONFIG", self.config)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self.tmp.cleanup)
        env = {k: v for k, v in os.environ.items() if not k.startswith("CLAUDE_PLUGIN_OPTION_")}
        environ = mock.patch.dict(os.environ, env, clear=True)
        environ.start()
        self.addCleanup(environ.stop)

    def test_a_plugin_option_wins_over_the_file_and_the_file_over_defaults(self):
        self.config.write_text(json.dumps({"scope": "personal", "db": str(self.root / "file.sqlite")}))
        os.environ["CLAUDE_PLUGIN_OPTION_SCOPE"] = "client"
        os.environ["CLAUDE_PLUGIN_OPTION_DB"] = ""  # an empty option is no choice
        config, origin = plugin.settings()
        self.assertEqual((config["scope"], origin["scope"]), ("client", "plugin option"))
        self.assertEqual(config["db"], str(self.root / "file.sqlite"))
        self.assertEqual(origin["strategy"], "default")

    def test_without_jev_selection_uses_rules_and_says_where_jev_is_when_found(self):
        config, _ = plugin.settings()
        self.assertEqual(plugin.kernel_args(config, None)[1], ["--strategy", "rules"])
        self.assertEqual(plugin.kernel_args(config, "/x/jev")[1], ["--strategy", "jev", "--jev-command", "/x/jev"])

    def test_an_explicit_jev_must_be_an_executable_file(self):
        jev = self.root / "jev"
        jev.write_text("#!/bin/sh\n")
        self.assertNotEqual(plugin.find_jev(str(jev)), str(jev.resolve()))
        jev.chmod(jev.stat().st_mode | stat.S_IXUSR)
        self.assertEqual(plugin.find_jev(str(jev)), str(jev.resolve()))

    def test_hooks_create_the_database_on_first_use(self):
        os.environ["CLAUDE_PLUGIN_OPTION_DB"] = str(self.root / "deep" / "memory.sqlite")
        config, _ = plugin.settings()
        plugin.ensure_database(config)
        self.assertTrue((self.root / "deep" / "memory.sqlite").exists())
        with redirect_stdout(io.StringIO()) as out:
            plugin.main(["activity", "--session", "nobody"])
        self.assertEqual(json.loads(out.getvalue())["undo"], [])

    def test_codex_files_run_the_launcher_for_every_event(self):
        hooks_path, hooks, toml_path, toml = codex_files(self.root, "/usr/bin/python3", "/home/me/.local/bin/context-kernel")
        self.assertEqual(hooks_path, self.root / ".codex" / "hooks.json")
        self.assertEqual(set(hooks), {"UserPromptSubmit", "Stop", "SessionStart"})
        self.assertIn('"/home/me/.local/bin/context-kernel" hook prompt --client codex', hooks["UserPromptSubmit"][0]["hooks"][0]["command"])
        self.assertIn('args = ["/home/me/.local/bin/context-kernel", "serve"]', toml)


if __name__ == "__main__":
    unittest.main()
