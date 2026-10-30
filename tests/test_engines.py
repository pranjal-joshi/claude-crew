import json
import subprocess
import sys
import textwrap
import unittest
from pathlib import Path
from unittest import mock

from tests.helpers import ROOT, TempCrewCase

from agents import engines, launch
from agents import opencode_bridge as bridge

# `opencode run --format json` as the real CLI prints it: a tool call, a step's tokens, the answer, the last step
FAKE = textwrap.dedent('''
    import json, sys
    args = sys.argv[1:]
    msg = sys.stdin.read()
    sid = args[args.index("-s") + 1] if "-s" in args else "ses_fake1"
    out = lambda d: print(json.dumps(d), flush=True)
    if "FAIL" in msg:
        out({"type": "error", "sessionID": sid, "error": {"name": "APIError", "data": {"message": "rate limited"}}})
        sys.exit(1)
    out({"type": "step_start", "sessionID": sid, "part": {"type": "step-start", "messageID": "m1"}})
    out({"type": "tool_use", "sessionID": sid, "part": {"type": "tool", "tool": "read", "callID": "c1", "messageID": "m1",
         "state": {"status": "completed", "input": {"filePath": "a.txt"}, "output": "plum"}}})
    out({"type": "tool_use", "sessionID": sid, "part": {"type": "tool", "tool": "crew_decide", "callID": "c2", "messageID": "m1",
         "state": {"status": "error", "input": {}, "error": "denied"}}})
    out({"type": "step_finish", "sessionID": sid, "part": {"id": "p1", "type": "step-finish", "cost": 0.25,
         "tokens": {"input": 100, "output": 5, "reasoning": 2, "cache": {"read": 40, "write": 3}}}})
    out({"type": "text", "sessionID": sid, "part": {"type": "text", "messageID": "m2", "text": "got: " + msg.strip()[:20]}})
''')


def fake_opencode(folder: Path) -> list[str]:
    """Bridge arguments that make FAKE its `opencode`."""
    (folder / "fake_oc.py").write_text(FAKE, encoding="utf-8")
    return ["--opencode", sys.executable, "--opencode-arg", str(folder / "fake_oc.py")]


class Bridge(TempCrewCase):
    """opencode_bridge.py turns `opencode run` JSON into the stream-json Crew reads from Claude Code."""

    def run_bridge(self, *messages, extra=()):
        oc = fake_opencode(Path(self._tmp.name))
        stdin = "".join(json.dumps({"type": "user", "message": {"role": "user", "content": m}}) + "\n" for m in messages)
        r = subprocess.run([sys.executable, str(ROOT / "agents" / "opencode_bridge.py"), *oc, "--model", "opencode/x",
                            "--mcp", "crew", *extra], input=stdin, capture_output=True, text=True, encoding="utf-8", timeout=60)
        return [json.loads(x) for x in r.stdout.splitlines() if x.strip()]

    def test_a_turn_comes_out_as_claude_stream_json(self):
        ev = self.run_bridge("hello there", "again")
        self.assertEqual((ev[0]["type"], ev[0]["subtype"], ev[0]["mcp_servers"]), ("system", "init", [{"name": "crew", "status": "connected"}]))
        uses = [b for e in ev if e["type"] == "assistant" for b in e["message"]["content"] if b["type"] == "tool_use"]
        self.assertEqual([(u["name"], u["input"]) for u in uses][:2], [("Read", {"file_path": "a.txt"}), ("mcp__crew__decide", {})])
        errs = [b for e in ev if e["type"] == "user" for b in e["message"]["content"] if b["is_error"]]
        self.assertEqual(errs[0]["content"], "denied")
        usage = next(e["message"]["usage"] for e in ev if e["type"] == "assistant" and e["message"].get("usage"))
        self.assertEqual(usage, {"input_tokens": 100, "cache_read_input_tokens": 40, "cache_creation_input_tokens": 3, "output_tokens": 7})
        results = [e for e in ev if e["type"] == "result"]
        self.assertEqual([(r["subtype"], r["result"], r["session_id"]) for r in results],
                         [("success", "got: hello there", "ses_fake1"), ("success", "got: again", "ses_fake1")])
        self.assertEqual([r["total_cost_usd"] for r in results], [0.25, 0.5])  # cumulative, like Claude Code's

    def test_a_failed_run_is_an_error_result(self):
        ev = self.run_bridge("FAIL now")
        r = ev[-1]
        self.assertEqual((r["type"], r["is_error"], r["result"]), ("result", True, "rate limited"))

    def test_compact_ends_at_once(self):
        ev = self.run_bridge("/compact")
        self.assertEqual((ev[-1]["subtype"], ev[-1]["total_cost_usd"]), ("success", 0))

    def test_images_are_named_not_dropped(self):
        txt = bridge.message_text({"message": {"content": [{"type": "text", "text": "see"}, {"type": "image", "source": {}}]}})
        self.assertIn("1 image attached", txt)


class Settings(TempCrewCase):
    def setUp(self):
        super().setUp()
        self.a = next(iter(self.crew.agents.values()))

    def configure(self, **p):
        return self.crew.act("configure", {"agent": self.a["name"], **p})

    def test_engine_and_model_are_checked_together(self):
        self.configure(model="opus")
        self.assertIn("error", self.configure(engine="opencode", model="opus"))
        self.assertIn("error", self.configure(engine="opencode", model="github-copilot/gpt-x"))  # Copilot's, not OpenCode's
        self.assertIn("error", self.configure(engine="cursor"))
        self.assertNotIn("error", self.configure(engine="opencode"))
        self.assertEqual((self.a["engine"], self.a["model"]), ("opencode", None))  # a new engine starts on its default model
        self.assertNotIn("error", self.configure(model="opencode/big-pickle", fallback="claude"))
        self.assertEqual((self.a["model"], self.a["fallback"]), ("opencode/big-pickle", "claude"))
        self.assertIn("error", self.configure(fallback="gpt"))
        saved = next(x for x in json.loads(self.cfg_path.read_text(encoding="utf-8"))["agents"] if x["name"] == self.a["name"])
        self.assertEqual((saved["engine"], saved["model"], saved["fallback"]), ("opencode", "opencode/big-pickle", "claude"))
        self.assertNotIn("error", self.configure(engine=""))
        self.assertEqual((self.a["engine"], self.a["model"]), (None, None))

    def test_changing_engine_changes_the_launch_key(self):
        before = launch.launch_key(self.crew, self.a)
        self.configure(engine="copilot", model="github-copilot/claude-haiku-5.5")
        self.assertNotEqual(before, launch.launch_key(self.crew, self.a))

    def test_sessions_do_not_cross_engines(self):
        self.assertTrue(engines.session_ok("claude", "6f1c-uuid"))
        self.assertFalse(engines.session_ok("claude", "ses_abc"))
        self.assertTrue(engines.session_ok("opencode", "ses_abc"))
        self.assertFalse(engines.session_ok("copilot", "6f1c-uuid"))

    def test_snapshot_lists_engines(self):
        with mock.patch.dict(engines._found, {"path": "oc", "version": "1.0", "error": None, "checked": 1,
                                              "models": ["opencode/a-free", "github-copilot/gpt-x", "other/y"]}):
            self.crew._engines_probed = True
            e = self.crew.snapshot()["engines"]
        self.assertEqual((e["opencode"]["models"], e["copilot"]["models"], e["opencode"]["ok"]), (["opencode/a-free"], ["github-copilot/gpt-x"], True))


class Permissions(unittest.TestCase):
    def test_allow_rules_become_bash_patterns_and_the_rest_is_denied(self):
        p = engines.permissions("acceptEdits", ["Bash(git log:*)", "Bash(npm test)", "Read", "WebFetch", "mcp__crew"])
        self.assertEqual(p, {"edit": "allow", "bash": {"*": "deny", "git log*": "allow", "npm test": "allow"}, "webfetch": "allow"})
        self.assertEqual(engines.permissions("plan", [])["edit"], "deny")
        self.assertEqual(engines.permissions("bypassPermissions", []), {"edit": "allow", "bash": "allow", "webfetch": "allow"})

    def test_mcp_servers_in_opencode_shape(self):
        out = engines._mcp({"ado": {"type": "stdio", "command": "npx", "args": ["-y", "ado"], "env": {"K": "v"}},
                            "web": {"type": "http", "url": "https://x/mcp"}})
        self.assertEqual(out, {"ado": {"type": "local", "command": ["npx", "-y", "ado"], "enabled": True, "environment": {"K": "v"}},
                               "web": {"type": "remote", "url": "https://x/mcp", "enabled": True}})


class Launch(TempCrewCase):
    def setUp(self):
        super().setUp()
        self.a = next(iter(self.crew.agents.values()))
        self.crew.act("configure", {"agent": self.a["name"], "engine": "opencode", "model": "opencode/big-pickle"})
        self.cmds = []

        class P:
            pid, stdout, stdin = 4242, iter(()), None

            def poll(self):
                return None

        def popen(cmd, **kw):
            self.cmds.append(cmd)
            return P()
        for target, value in ((launch.subprocess, "Popen"),):
            pt = mock.patch.object(target, value, popen)
            pt.start()
            self.addCleanup(pt.stop)
        for name in ("_read", "_pid_add"):
            pt = mock.patch.object(self.crew, name, lambda *x: None)
            pt.start()
            self.addCleanup(pt.stop)
        pt = mock.patch.object(launch, "StdinWriter", lambda p: None)
        pt.start()
        self.addCleanup(pt.stop)

    def test_an_opencode_agent_starts_the_bridge_with_its_config(self):
        self.a["session"] = "uuid-from-claude"  # a Claude session can't be resumed by OpenCode
        with mock.patch.dict(engines._found, {"path": "C:/oc/opencode.cmd", "models": [], "error": None, "checked": 1}):
            self.assertTrue(launch.start(self.crew, self.a))
        cmd = self.cmds[-1]
        self.assertTrue(cmd[1].endswith("opencode_bridge.py"), cmd)
        self.assertEqual(cmd[cmd.index("--model") + 1], "opencode/big-pickle")
        self.assertNotIn("--session", cmd)
        self.assertIsNone(self.a["session"])
        cfg = json.loads(Path(cmd[cmd.index("--config") + 1]).read_text(encoding="utf-8"))
        self.assertIn("crew", cfg["mcp"])
        self.assertEqual(cfg["mcp"]["crew"]["environment"]["CREW_AGENT"], self.a["name"])
        self.assertIn(self.a["role"] or "", Path(cfg["instructions"][0]).read_text(encoding="utf-8"))

    def test_without_opencode_the_agent_shows_why(self):
        with mock.patch.dict(engines._found, {"path": None, "error": "opencode is not installed", "checked": 1}):
            self.assertFalse(launch.start(self.crew, self.a))
        self.assertIn("opencode is not installed", self.a["error"])

    def test_on_fallback_it_runs_claude_with_a_claude_model(self):
        self.a["on_fallback"] = True
        self.assertTrue(launch.start(self.crew, self.a))
        cmd = self.cmds[-1]
        self.assertEqual((cmd[0], cmd[cmd.index("--model") + 1]), ("claude-not-used", "sonnet"))


class Fallback(TempCrewCase):
    def setUp(self):
        super().setUp()
        self.a = next(iter(self.crew.agents.values()))
        self.crew._start = lambda ag: True
        self.crew._send = lambda ag, text, images=None: True
        self.crew.act("configure", {"agent": self.a["name"], "engine": "opencode", "fallback": "claude"})
        self.crew.act("add", {"title": "Fix it", "tag": "bug"})
        self.t = self.crew.tasks[-1]
        self.t.update(col="doing", who=self.a["name"])
        self.a.update(status="running", task=self.t["id"])

    def test_a_failed_turn_goes_again_on_claude_then_back(self):
        self.crew._on_event(self.a, {"type": "result", "subtype": "error_during_execution", "is_error": True, "result": "rate limited",
                                     "total_cost_usd": 0, "session_id": "ses_1"})
        self.assertEqual((self.t["col"], self.t["who"], self.t.get("on_fallback"), self.a["status"]), ("backlog", self.a["name"], True, "idle"))
        self.assertNotIn(self.a["name"], self.t.get("sessions") or {})
        with self.crew.lock:
            self.assertTrue(self.crew._begin(self.a, self.t))
        self.assertTrue(self.a.get("on_fallback"))
        self.assertEqual(engines.effective(self.a), "claude")
        self.crew._on_event(self.a, {"type": "result", "subtype": "success", "result": "fixed", "total_cost_usd": 0.1})
        self.assertNotIn("on_fallback", self.t)
        self.crew.act("add", {"title": "Next", "tag": "bug"})
        nxt = self.crew.tasks[-1]
        with self.crew.lock:
            self.crew._begin(self.a, nxt)
        self.assertEqual(engines.effective(self.a), "opencode")

    def test_no_fallback_set_is_the_usual_error(self):
        self.crew.act("configure", {"agent": self.a["name"], "fallback": ""})
        self.crew._on_event(self.a, {"type": "result", "subtype": "error_during_execution", "is_error": True, "result": "bad model",
                                     "total_cost_usd": 0})
        self.assertEqual((self.a["status"], self.t.get("on_fallback")), ("error", None))


if __name__ == "__main__":
    unittest.main()
