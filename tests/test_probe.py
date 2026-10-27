import unittest
import unittest.mock

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

    def test_pipes_inside_a_quoted_filter_are_fine(self):
        self.assertIsNone(probe.parse(server.workflows.runs.WATCH_EXAMPLE)[0])  # the example Crew gives agents
        self.assertIsNone(probe.parse("gh pr view 1 --jq '[.a[]|select(.b>1)|.c]'")[0])
        self.assertIsNotNone(probe.parse("gh pr view 1 --jq '.a' | head")[0])


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
        self.addCleanup(setattr, server.Crew, "_ado_dflt", server.Crew._ado_dflt)
        server.Crew._ado_dflt = {}  # Now runs threads inline: never read this machine's az defaults
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

    def test_a_command_that_keeps_failing_wakes_it_once(self):
        probe.run = lambda argv, cwd: (self.out[0], self.out[0] != "boom")
        self._watch(done="completed", fail="failed")
        self._end_turn()
        self.out[0] = "boom"  # e.g. cmd.exe could not read the az --query: it never matches done or fail
        for _ in range(probe.ERRS - 1):
            self._poll()
        self.assertEqual(self.t["col"], "backlog")
        self._poll()
        self.assertEqual(self.t["col"], "doing")
        self.assertIn("the watch command fails", self.sent[-1])
        self.assertIn("boom", self.sent[-1])
        self._end_turn("set a simpler watch")
        for _ in range(probe.ERRS + 2):  # the same error does not wake it again
            self._poll()
        self.assertEqual(self.t["col"], "backlog")

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

    def test_a_turn_you_start_on_the_waiting_step_can_set_its_watch(self):
        self._end_turn()
        self.ci.update(status="running")  # you sent it a message, or pressed Address review
        self.assertTrue(self._watch(fail="FAILED").get("ok"))
        self.assertIn("probe", self.t["wait"])

    def test_what_fails_when_the_watch_is_set_is_the_baseline(self):
        self.out[0] = "OPEN REVIEW copilot 08:50"  # the review the agent just answered
        self._watch(done="MERGED", fail="FAILED|REVIEW")
        self._end_turn()
        self._poll()
        self.assertEqual(self.t["col"], "backlog")
        self.out[0] = "OPEN REVIEW copilot 08:50 REVIEW copilot 09:40"  # a new review
        self._poll()
        self.assertEqual(self.t["col"], "doing")

    def test_a_seen_failure_does_not_wake_it_again_when_a_field_beside_it_changes(self):
        fail = '["OPEN","MERGEABLE",["FAILED build https://x/run/7"],["REVIEW copilot COMMENTED 08:50"]]'
        self.out[0] = fail
        self._watch(done="MERGED", fail="FAILED|REVIEW|CONFLICTING")
        self._end_turn()
        self._poll()
        self.out[0] = fail.replace("MERGEABLE", "UNKNOWN")  # GitHub recomputing mergeability: nothing new to act on
        self._poll()
        self.assertEqual((self.t["col"], len(self.sent)), ("backlog", 1))
        self.out[0] = fail.replace('08:50"]', '08:50","REVIEW copilot COMMENTED 09:40"]')  # a new review: new work
        self._poll()
        self.assertEqual(self.t["col"], "doing")
        self.assertIn("09:40", self.sent[-1])

    def test_output_items(self):
        self.assertEqual(sorted(probe.items('["OPEN",null,["FAILED a"],{"k":"REVIEW b"}]')), ["FAILED a", "OPEN", "REVIEW b"])
        self.assertEqual(probe.items("one\n\n two "), ["one", "two"])

    def test_the_pr_watch_skips_copilot_reviews_with_nothing_new(self):
        jq = server.workflows.runs.WATCH_EXAMPLE
        self.assertIn('test("No open issues") and (test("previously missed")|not)', jq)
        self.assertIn("generated no new comments", jq)

    def test_empty_command_removes_the_watch(self):
        self._watch()
        self.assertTrue(self.crew.act("watch", {"from": self.ci["name"], "command": ""}, via="mcp").get("ok"))
        self.assertNotIn("probe", self.t["wait"])



class AzOnWindows(unittest.TestCase):
    """az is az.cmd on Windows: cmd.exe broke on a --query with ( ) , { } and no space, so every argument is quoted."""

    def test_cmd_files_get_every_argument_quoted(self):
        argv = server.triggers.azure.argv
        with unittest.mock.patch.object(server.triggers.azure.os, "name", "nt"):
            exe, q = r"C:\az\az.CMD", "[?starts_with(v,'3a')].{n:id}"
            self.assertEqual(argv(exe, ["build", "--query", q, "C:\\x\\"]),
                             f'"{exe}" "build" "--query" "{q}" "C:\\x\\\\"')  # a trailing \ is doubled
            self.assertEqual(argv("/usr/bin/az", ["a"]), ["/usr/bin/az", "a"])
        with unittest.mock.patch.object(server.triggers.azure.os, "name", "posix"):
            self.assertEqual(argv("az.cmd", ["a b"]), ["az.cmd", "a b"])


if __name__ == "__main__":
    unittest.main()
