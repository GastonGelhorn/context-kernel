"""v0.10.1: an Ollama on another machine of the owner's judges as local once setup names it."""

import io
import json
import os
import stat
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

from shelflife_context import cli, plugin
from shelflife_context.capture import policy
from shelflife_context.common import KernelError
from shelflife_context.judge import JevCommand, JudgeRemote, is_local, origin
from shelflife_context.setup import doctor, wizard
from shelflife_context.store import Store

# A jev that answers what setup and doctor ask: `setup` points it somewhere (kept in a state file),
# the dry run reports where, and old versions have no `setup` at all.
FAKE = '''#!/usr/bin/env python3
import json, os, sys
state, args = os.environ["FAKE_JEV_STATE"], sys.argv[1:]
with open(state + ".log", "a") as log:
    log.write(json.dumps(args) + "\\n")
def url():
    try:
        return open(state).read().strip()
    except OSError:
        return "https://api.typesafe.ai/v1/systemone"
if "--dry-run" in args:
    print(json.dumps({"url": url(), "body": {"model": "tev1-32k"}}))
    sys.exit(0)
if args[:1] == ["setup"]:
    if os.environ.get("FAKE_JEV_OLD"):
        print("jev: invalid choice: 'setup'", file=sys.stderr)
        sys.exit(2)
    if "--help" in args:
        sys.exit(0)
    target = "http://" + args[args.index("--url") + 1] + ":11434" if "remote" in args else "http://localhost:11434"
    if os.environ.get("FAKE_JEV_NO_TEV1") and "--pull" not in args:
        print("FAIL  no tev1 at " + target + ": `ollama pull tev1` on that machine, or `jev setup --pull`")
        sys.exit(4)
    open(state, "w").write(target + "/v1/systemone")
    print("ok    Ollama 0.35.1 at " + target)
    print("ready: ollama · " + target + " · model tev1-32k")
    sys.exit(0)
print("healthy")
'''
SERVER = "http://192.0.2.1:11434"  # TEST-NET-1: never a real machine


class Locality(unittest.TestCase):
    def test_origin_spells_out_the_server(self):
        self.assertEqual(origin(SERVER + "/v1/systemone"), SERVER)
        self.assertEqual(origin("http://Box"), "http://box:80")
        self.assertEqual(origin("https://ollama.tail.ts.net/"), "https://ollama.tail.ts.net:443")
        self.assertEqual(origin("http://[fd7a::1]:11434"), "http://[fd7a::1]:11434")
        for other in ("", "ftp://box", "box", "http://:11434", "http://box:port"):
            self.assertEqual(origin(other), "", other)

    def test_only_this_machine_or_a_named_server_is_local(self):
        url = SERVER + "/v1/systemone"
        self.assertTrue(is_local("http://127.0.0.1:11434/v1/systemone"))
        self.assertFalse(is_local(url))
        self.assertTrue(is_local(url, [SERVER]))
        self.assertTrue(is_local(url, ["http://192.0.2.1:11434/"]))
        self.assertFalse(is_local(url, ["http://192.0.2.1:11435"]), "another port is another server")
        self.assertFalse(is_local(url, ["https://192.0.2.1:11434"]), "another scheme is another server")
        self.assertFalse(is_local("https://api.typesafe.ai/v1/systemone", [SERVER]))

    def test_a_private_judge_is_a_url(self):
        for bad in (["box"], [""], "http://box:1", [3]):
            with self.assertRaises(KernelError, msg=bad):
                JevCommand("jev", private=bad)
        self.assertEqual(JevCommand("jev", private=[SERVER + "/v1"]).private, (SERVER,))


class Wiring(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.config = Path(self.tmp.name) / "config.json"
        patcher = mock.patch.object(plugin, "CONFIG", self.config)
        patcher.start()
        self.addCleanup(patcher.stop)
        env = {k: v for k, v in os.environ.items() if not k.startswith("CLAUDE_PLUGIN_OPTION_")}
        environ = mock.patch.dict(os.environ, env, clear=True)
        environ.start()
        self.addCleanup(environ.stop)

    def test_only_the_file_names_private_judges_and_every_kernel_call_gets_them(self):
        self.config.write_text(json.dumps({"private_judges": [SERVER, "", 7]}))
        os.environ["CLAUDE_PLUGIN_OPTION_PRIVATE_JUDGES"] = "http://evil.example:11434"
        config, origin_of = plugin.settings()
        self.assertEqual(config["private_judges"], [SERVER])
        self.assertEqual(origin_of["private_judges"], str(self.config))
        base, _ = plugin.kernel_args(config, "/x/jev")
        self.assertEqual(base[-2:], ["--private-judge", SERVER])
        args = cli.parser().parse_args(base + ["hook", "--client", "claude", "--workspace", ".", "--strategy", "jev"])
        self.assertEqual(cli.make_judge(args).private, (SERVER,))
        args = cli.parser().parse_args(base + ["calibrate", "--check"])
        self.assertEqual(args.private_judge, [SERVER], "owner commands see the same judge as the hooks")


@mock.patch("shelflife_context.judge.weights", return_value=None)
@mock.patch("shelflife_context.setup.loaded", return_value=None)
class Setup(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.config, self.db, self.state = root / "config.json", root / "memory.sqlite", root / "jev-state"
        self.jev = root / "jev"
        self.jev.write_text(FAKE)
        self.jev.chmod(self.jev.stat().st_mode | stat.S_IXUSR)
        env = {k: v for k, v in os.environ.items() if not k.startswith(("CLAUDE_PLUGIN_OPTION_", "FAKE_JEV_"))}
        env["FAKE_JEV_STATE"] = str(self.state)
        for patcher in (mock.patch.object(plugin, "CONFIG", self.config), mock.patch("shelflife_context.setup.CONFIG", self.config),
                        mock.patch("shelflife_context.setup.find_jev", return_value=str(self.jev)),
                        mock.patch.dict(os.environ, env, clear=True)):
            patcher.start()
            self.addCleanup(patcher.stop)

    def setup(self, *argv):
        with redirect_stdout(io.StringIO()) as out:
            code = wizard(["--yes", "--claude", "no", "--scope", "work", "--db", str(self.db), *argv])
        return code, out.getvalue()

    def stored(self):
        return json.loads(self.config.read_text()).get("private_judges", [])

    def allowed(self):
        store = Store(str(self.db), "work")
        try:
            return policy(store)["allow_remote_judge"]
        finally:
            store.close()

    def test_remote_names_the_machine_and_drops_the_hosted_allowance(self, *_):
        plugin.ensure_database({"db": str(self.db), "scope": "work"})
        with redirect_stdout(io.StringIO()):
            cli.main(["--db", str(self.db), "--scope", "work", "policy", "--allow-remote-judge", "yes"])
        self.assertTrue(self.allowed())
        code, out = self.setup("--judge", "remote", "--judge-url", "192.0.2.1")
        self.assertIn("$ " + str(self.jev) + " setup --judge remote --url 192.0.2.1 --yes", out)
        self.assertEqual(self.stored(), [SERVER])
        self.assertFalse(self.allowed(), "a named machine needs no hosted allowance")
        self.assertIn(f"your machine at {SERVER}/v1/systemone", out)
        self.assertEqual(code, 0, out)

    def test_old_jev_or_no_address_changes_nothing(self, *_):
        os.environ["FAKE_JEV_OLD"] = "1"
        code, out = self.setup("--judge", "remote", "--judge-url", "192.0.2.1")
        self.assertIn("jevmate 1.10 or newer", out)
        self.assertEqual(self.stored(), [])
        del os.environ["FAKE_JEV_OLD"]
        code, out = self.setup("--judge", "remote")
        self.assertIn("--judge-url HOST", out)
        self.assertFalse(self.state.exists(), "jev was not pointed anywhere")

    def test_a_download_takes_pull(self, *_):
        os.environ["FAKE_JEV_NO_TEV1"] = "1"
        code, out = self.setup("--judge", "remote", "--judge-url", "192.0.2.1")
        self.assertIn("jev setup did not finish", out)
        self.assertEqual(self.stored(), [])
        code, out = self.setup("--judge", "remote", "--judge-url", "192.0.2.1", "--pull")
        self.assertEqual(self.stored(), [SERVER])
        calls = [json.loads(line) for line in (self.state.parent / "jev-state.log").read_text().splitlines()]
        setups = [c for c in calls if c[:1] == ["setup"] and "--help" not in c]
        self.assertEqual(["--pull" in c for c in setups], [False, True])

    def test_keep_names_nothing_unasked_and_local_forgets_the_name(self, *_):
        self.state.write_text(SERVER + "/v1/systemone")
        code, out = self.setup("--judge", "keep")
        self.assertEqual(self.stored(), [])
        self.assertIn("--judge remote --judge-url 192.0.2.1", out, "doctor says how to name it")
        self.setup("--judge", "remote", "--judge-url", "192.0.2.1")
        self.assertEqual(self.stored(), [SERVER])
        code, out = self.setup("--judge", "local")
        self.assertEqual(self.stored(), [])
        self.assertIn("local · http://localhost:11434/v1/systemone", out)

    def test_the_judge_refuses_an_unnamed_remote_and_takes_a_named_one(self, *_):
        self.state.write_text(SERVER + "/v1/systemone")
        with self.assertRaises(JudgeRemote):
            JevCommand(str(self.jev)).require_local()
        named = JevCommand(str(self.jev), private=[SERVER])
        named.require_local()
        self.assertEqual((named.describe()["local"], named.describe()["private"]), (True, True))


if __name__ == "__main__":
    unittest.main()
