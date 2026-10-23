import tempfile
import unittest
from pathlib import Path

from tests.helpers import TempCrewCase


def started(use_id, cmd, kind="Bash"):
    inp = {"command": cmd, "run_in_background": True} if kind == "Bash" else {"command": cmd, "description": "watch"}
    return {"type": "assistant", "message": {"id": use_id, "content": [{"type": "tool_use", "id": use_id, "name": kind, "input": inp}]}}


def result(use_id, text, err=False):
    return {"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": use_id, "content": text, "is_error": err}]}}


class BackgroundJobs(TempCrewCase):
    """#93: an agent's background shells outlive its turn. Crew lists them, shows their output and stops them."""

    def setUp(self):
        super().setUp()
        self.a = self.crew.agents[list(self.crew.agents)[0]]
        self.a.update(status="running", task=93)
        self.out = Path(tempfile.gettempdir()) / "claude" / "crew-test" / "bjob1.output"
        self.out.parent.mkdir(parents=True, exist_ok=True)
        self.out.write_text("waiting for checks\nall green\n", encoding="utf-8")
        self.addCleanup(self.out.unlink)
        self.sent = []
        self.crew._send = lambda ag, text, images=None: self.sent.append(text) or True
        self.crew._start = lambda ag: True

    def feed(self, *events):
        with self.crew.lock:
            for d in events:
                self.crew._on_event(self.a, d)

    def test_a_background_shell_is_listed_until_it_finishes(self):
        self.feed(started("tu1", "gh pr checks 123 --watch"),
                  result("tu1", f"Command running in background with ID: bjob1. Output is being written to: {self.out}. You will be notified"))
        j = next(iter(self.a["bg"].values()))
        self.assertEqual((j["id"], j["task"], j["kind"]), ("bjob1", 93, "Bash"))
        self.assertIn("all green", self.crew.act("bg_output", {"agent": self.a["name"], "id": "bjob1"})["text"])
        self.feed({"type": "user", "message": {"content": "<task-notification>\n<task-id>bjob1</task-id>\n<status>completed</status>\n</task-notification>"}})
        self.assertEqual(self.a["bg"], {})

    def test_a_monitor_counts_and_a_failed_start_does_not(self):
        self.feed(started("tu2", "until gh pr view 1; do sleep 30; done", "Monitor"),
                  result("tu2", "Monitor started (task bmon7, expires in 30m)"),
                  started("tu3", "npm run dev"), result("tu3", "spawn failed", err=True))
        self.assertEqual([j["id"] for j in self.a["bg"].values()], ["bmon7"])

    def test_taskstop_and_the_process_ending_clear_them(self):
        self.feed(started("tu1", "sleep 999"), result("tu1", "Command running in background with ID: b1."),
                  started("tu2", "sleep 998"), result("tu2", "Command running in background with ID: b2."),
                  {"type": "assistant", "message": {"id": "m9", "content": [{"type": "tool_use", "id": "tu9", "name": "TaskStop", "input": {"task_id": "b1"}}]}},
                  result("tu9", "stopped"))
        self.assertEqual([j["id"] for j in self.a["bg"].values()], ["b2"])
        self.crew._kill_proc(self.a["name"])
        self.assertEqual(self.a["bg"], {})

    def test_stop_one_asks_the_agent_and_stop_all_ends_the_idle_process(self):
        self.feed(started("tu1", "sleep 999"), result("tu1", "Command running in background with ID: b1."),
                  started("tu2", "sleep 998"), result("tu2", "Command running in background with ID: b2."))
        self.a.update(status="idle", task=None)
        self.assertTrue(self.crew.act("bg_stop", {"agent": self.a["name"], "id": "b1"}).get("ok"))
        self.assertIn("TaskStop", self.sent[-1])
        self.a["status"] = "idle"
        killed = []
        self.crew._kill_proc = lambda name, wait=False: killed.append(name)
        self.assertTrue(self.crew.act("bg_stop", {"agent": self.a["name"]}).get("ok"))
        self.assertEqual(killed, [self.a["name"]])

    def test_output_is_only_read_from_claude_temp_files(self):
        self.feed(started("tu1", "x"), result("tu1", "Command running in background with ID: b1. Output is being written to: C:/Windows/win.ini.output"))
        self.assertIn("no output file", self.crew.act("bg_output", {"agent": self.a["name"], "id": "b1"})["text"])


if __name__ == "__main__":
    unittest.main()
