import unittest

from tests.helpers import TempCrewCase, server

probe = server.workflows.probe


class Now:
    """Runs a thread's target at once, so a watch finishes inside tick()."""

    def __init__(self, target, args=(), daemon=None):
        self.target, self.args = target, args

    def start(self):
        self.target(*self.args)


class Parse(unittest.TestCase):
    def test_read_only_commands_pass(self):
        for cmd in ("gh pr view 123 --json state --jq .state", "gh pr checks 123", "gh api repos/o/r/pulls/1",
                    "az pipelines runs list --pipeline-ids 7 --top 3 --query [].result", "git ls-remote origin main"):
            self.assertIsNone(probe.parse(cmd)[0], cmd)

    def test_writes_shells_and_unknown_tools_are_refused(self):
        for cmd in ("gh pr merge 123", "gh api -X POST repos/o/r/issues", "gh api repos/o/r --method=PATCH",
                    "gh pr view 1 | grep x", "az pipelines run --id 7", "git push", "rm -rf x", "curl http://x",
                    "gh pr view 1; git push", 'az repos pr show --id 1 --query %PATH%', ""):
            self.assertIsNotNone(probe.parse(cmd)[0], cmd)


class Watch(TempCrewCase):
    """A ⟳ step's agent sets a watch; Crew polls it and wakes the agent only when the output changes."""

    def setUp(self):
        super().setUp()
        self.sent, self.out = [], ["OPEN"]
        self.crew._start = lambda ag: True
        self.crew._send = lambda ag, text, images=None: self.sent.append(text) or True
        self._run, self._thread = probe.run, probe.threading.Thread
        probe.run = lambda argv, cwd: (self.out[0], True)
        probe.threading.Thread = Now
        self.addCleanup(setattr, probe, "run", self._run)
        self.addCleanup(setattr, probe.threading, "Thread", self._thread)
        self.ci = next(iter(self.crew.agents.values()))
        r = self.crew.act("wf_save", {"name": "Ship", "steps": [{"agent": self.ci["name"], "title": "Merge: {input}",
                                                                  "gate": "until", "every": 10, "checks": 50}]})
        self.assertTrue(r.get("ok"), r)
        with self.crew.lock:
            self.crew._start_run(self.crew.config["workflows"][-1], "ship it")
        self.t = self.crew.tasks[-1]
        self.crew._begin(self.ci, self.t)

    def _end_turn(self, text="PR open."):
        self.crew._on_event(self.ci, {"type": "result", "subtype": "success", "result": text, "total_cost_usd": 0.01})

    def _poll(self):
        self.t["wait"]["next"] = 0 if not self.t["wait"].get("wake") else self.t["wait"]["next"]
        probe.tick(self.crew)
        self.crew.dispatch()

    def _watch(self, **kw):
        return self.crew.act("watch", {"from": self.ci["name"], "command": "gh pr view 1 --json state --jq .state", **kw}, via="mcp")

    def test_only_a_running_until_step_can_watch(self):
        self._end_turn()
        self.assertIn("error", self._watch())

    def test_unchanged_output_does_not_wake_the_agent(self):
        self.assertTrue(self._watch().get("ok"))
        self._end_turn()
        self.assertEqual(self.t["wait"]["next"] <= server.now(), True)  # baseline at once
        for _ in range(3):
            self._poll()
        self.assertEqual((self.t["col"], len(self.sent)), ("backlog", 1))
        self.assertEqual((self.t["wait"]["checks"], self.t["wait"]["watched"]), (1, 3))  # watch runs aren't checks
        self.assertLessEqual(self.t["wait"]["next"] - server.now(), probe.WATCH_EVERY * 60 + 1)  # every 2 min, not the step's 10

    def test_changed_output_wakes_it_with_what_changed(self):
        self._watch()
        self._end_turn()
        self._poll()
        self.out[0] = "MERGED"
        self._poll()
        self.assertEqual(self.t["col"], "doing")
        self.assertIn("output changed", self.sent[-1])
        self.assertIn("MERGED", self.sent[-1])
        self._end_turn("still waiting on deploy")  # back to watching, re-baselined on what it just saw
        self.assertEqual((self.t["col"], self.t["wait"].get("wake")), ("backlog", None))
        self._poll()
        self._poll()
        self.assertEqual(self.t["col"], "backlog")

    def test_done_regex_wakes_it_on_the_first_run(self):
        self.out[0] = "MERGED"
        self._watch(done="MERGED")
        self._end_turn()
        self._poll()
        self.assertEqual(self.t["col"], "doing")
        self.assertIn("done matched", self.sent[-1])

    def test_a_new_failure_wakes_it_to_fix_and_the_same_one_does_not_again(self):
        self._watch(done="MERGED", fail="FAILED")
        self._end_turn()
        self._poll()
        self.out[0] = "OPEN · in progress"
        self._poll()  # progress alone wakes nobody when fail is set
        self.assertEqual(self.t["col"], "backlog")
        self.out[0] = "OPEN FAILED build https://dev.azure.com/acme/Proj/_build/results?buildId=7"
        self._poll()
        self.assertEqual(self.t["col"], "doing")
        self.assertIn("failed", self.sent[-1])
        self.assertIn("buildId=7", self.sent[-1])
        self.assertIn("fix it on this same branch", self.sent[-1])
        self._end_turn("pushed a fix")  # CI hasn't re-run yet: the same failure must not wake it again
        self._poll()
        self._poll()
        self.assertEqual(self.t["col"], "backlog")
        self.out[0] = "OPEN FAILED build https://dev.azure.com/acme/Proj/_build/results?buildId=8"
        self._poll()
        self.assertEqual(self.t["col"], "doing")

    def test_the_watch_ends_after_the_steps_time_budget(self):
        self._watch(fail="FAILED")
        self._end_turn()
        self._poll()
        self.t["wait"]["probe"]["since"] -= 50 * 10 * 60  # checks × every minutes have passed
        self._poll()
        self.assertEqual(self.t["col"], "doing")
        self.assertIn("last check", self.sent[-1])
        self._end_turn("still open")
        self.assertEqual(self.t["col"], "review")

    def test_empty_command_removes_the_watch(self):
        self._watch()
        self.assertTrue(self.crew.act("watch", {"from": self.ci["name"], "command": ""}, via="mcp").get("ok"))
        self.assertNotIn("probe", self.t["wait"])


if __name__ == "__main__":
    unittest.main()
