import unittest
from unittest import mock

import server
import workflows
from tests.helpers import TempCrewCase


class PipelineFix(TempCrewCase):
    """A deploy watch whose pipeline failed hands it to a Pipeline fix run (fix, ship, keep green until merged), waits
    without checks, and is woken to follow the run that carries the fix."""

    def setUp(self):
        super().setUp()
        self.crew._save_config = lambda: None
        self.crew.config["workflows"] = [{"id": "w1", "name": "Deliver", "description": "", "output": {"kind": "board"}, "steps": [
            {"agent": "ops", "title": "Deployed: {input}", "details": "", "gate": "until", "every": 30, "checks": 96}]}]
        self.cd = self.start_deploy("Files into tmp", ado={"id": 77, "url": "https://dev.azure.com/acme/Proj/_workitems/edit/77"})
        self.card = self.crew._task(self.cd["tasks"][0])

    def start_deploy(self, title, ado=None):
        with self.crew.lock:
            run = self.crew._start_run(self.crew.config["workflows"][0], title, "", {"ado": ado} if ado else None)
        t = self.crew._task(run["tasks"][0])
        t.update(col="backlog", pipes=["https://dev.azure.com/acme/Proj/_build/results?buildId=4402"],
                 wait={"checks": 1, "last": "Run 4402 failed at Ruff lint (ISC004 adapter.py:1209).", "wake": "failed", "next": 0,
                       "probe": {"cmd": "az pipelines runs show --id 4402", "done": "succeeded", "fail": "failed",
                                 "since": 1.0, "seen": "x", "failed": "x", "out": "completed\nfailed"}})
        return run

    def fix(self, t=None, **p):
        return self.crew.act("pipeline_fix", {"task": (t or self.card)["id"], **p})

    def fix_run(self, rid):
        return workflows.runs.find_run(self.crew, rid)

    def tick(self):
        with self.crew.lock:
            workflows.fixes.tick(self.crew)

    def test_it_creates_the_workflow_from_the_crews_tags_once(self):
        r = self.fix(reason="lint red on master")
        self.assertTrue(r.get("ok"), r)
        flows = [w for w in self.crew.config["workflows"] if w.get("kind") == "pipeline_fix"]
        self.assertEqual(len(flows), 1)
        self.assertEqual([s["agent"] for s in flows[0]["steps"]], ["builder", "ops", "reviewer"])
        self.assertEqual([s["gate"] for s in flows[0]["steps"]], ["auto", "auto", "until"])
        other = self.start_deploy("Second change")
        self.assertTrue(self.fix(self.crew._task(other["tasks"][0])).get("ok"))
        self.assertEqual(len([w for w in self.crew.config["workflows"] if w.get("kind") == "pipeline_fix"]), 1)

    def test_every_step_checks_for_a_fix_someone_else_shipped(self):
        steps = workflows.fixes.steps_for("f", "s", "c")
        self.assertIn("already", steps[0]["details"])
        self.assertIn("right before you push", steps[1]["details"])
        self.assertIn("another PR", steps[2]["details"])

    def test_the_fixer_runs_only_the_tests_its_fix_touches(self):
        fixer, _, ci = workflows.fixes.steps_for("f", "s", "c")
        for want in ("failed in the run", "any you added", "files in your diff", "Don't run the whole suite"):
            self.assertIn(want, fixer["details"])
        self.assertNotIn("whole suite", ci["details"])

    def test_a_pr_blocked_by_master_waits_for_the_fix_then_brings_master_in(self):
        self.crew.config["workflows"].append({"id": "w2", "name": "Bug fix", "description": "", "output": {"kind": "board"},
                                              "steps": [{"agent": "reviewer", "title": "Green: {input}", "details": "",
                                                         "gate": "until", "every": 10, "checks": 144}]})
        with self.crew.lock:
            run = self.crew._start_run(self.crew.config["workflows"][1], "Cache warm-up", "", None)
        t = self.crew._task(run["tasks"][0])
        t.update(col="backlog", prs=["https://github.com/o/r/pull/812"],
                 wait={"checks": 7, "last": "Api suite fails the same 47 tests as master.",
                       "probe": {"cmd": "gh pr view 812", "fail": "FAILED", "seen": "x", "failed": "x", "out": "FAILED Api suite"}})
        self.assertTrue(workflows.fixes.is_pr_watch(self.crew, t))
        self.assertFalse(workflows.fixes.is_pr_watch(self.crew, self.card))  # ops follows deployments
        fx = self.fix_run(self.fix(t, reason="master red: ruff ISC004 and 47 tests")["run"])
        self.assertEqual((t["wait"]["fixing"], t["wait"]["fix_for"]), (fx["id"], "pr"))
        first = self.crew._task(fx["tasks"][0])
        for want in ("PR watch", "fix it on master, not in that PR", "pull/812", "47 tests"):
            self.assertIn(want, first["details"])
        self.assertNotIn("The change it deploys", first["details"])
        fx.update(status="done")
        self.tick()
        self.assertEqual(t["wait"]["wake"], "fix merged")
        body = workflows.runs.recheck_body(t, self.crew._until_step(t))
        self.assertIn("merge origin/master into its branch", body)
        self.assertNotIn("first run of the pipeline on master", body)

    def test_a_pr_check_failing_on_master_too_is_told_to_hand_it_off(self):
        step = {"checks": 144, "every": 10}
        body = workflows.runs.recheck_body({"id": 1, "title": "x", "wait": {"wake": "failed", "probe": {"cmd": "c", "out": "o"}}}, step)
        self.assertIn("fails on master too", body)
        self.assertIn("fix_pipeline", workflows.runs.until_brief(step))

    def test_the_card_waits_without_checks_while_the_fix_runs(self):
        r = self.fix(reason="lint red")
        fx = self.fix_run(r["run"])
        self.assertEqual(self.card["wait"]["fixing"], fx["id"])
        self.assertNotIn("wake", self.card["wait"])  # the failure it was woken for is the fix run's job
        self.assertEqual(fx["source"], {"kind": "pipeline_fix", "tasks": [self.card["id"]], "by": "you",
                                        "pipe": self.card["pipes"][-1], "ado": self.cd["ado"]})
        first = self.crew._task(fx["tasks"][0])
        self.assertEqual(first["who"], "builder")
        for want in ("buildId=4402", "ISC004", "lint red", "_workitems/edit/77", f"#{self.card['id']}"):
            self.assertIn(want, first["details"])
        self.assertEqual(first["ado"], self.cd["ado"])  # the shipper links the same work item
        begun = []
        self.crew._begin = lambda a, t: begun.append(t["id"])
        self.crew.dispatch()
        self.assertNotIn(self.card["id"], begun)
        self.card["wait"]["next"] = 0
        probed = []
        with mock.patch.object(workflows.probe.threading, "Thread",
                               lambda target, args, daemon: type("T", (), {"start": lambda s: probed.append(args[1]["id"])})()):
            workflows.probe.tick(self.crew)
        self.assertNotIn(self.card["id"], probed)
        self.card["wait"].pop("fixing")
        with mock.patch.object(workflows.probe.threading, "Thread",
                               lambda target, args, daemon: type("T", (), {"start": lambda s: probed.append(args[1]["id"])})()):
            workflows.probe.tick(self.crew)
        self.assertIn(self.card["id"], probed)  # the same card is checked once it no longer waits for a fix

    def test_a_merged_fix_wakes_the_card_to_follow_the_new_run(self):
        fx = self.fix_run(self.fix()["run"])
        ship = {"id": 9100, "title": "Ship", "col": "done", "prs": ["https://github.com/o/r/pull/12"]}
        self.crew.tasks.append(ship)
        fx["tasks"][1] = ship["id"]
        fx["results"][2] = {"who": "reviewer", "summary": "PR #12 merged as abc123.", "branch": "", "commit": ""}
        fx.update(status="done")
        self.tick()
        w = self.card["wait"]
        self.assertEqual((w["wake"], w["next"]), ("fix merged", 0))
        self.assertNotIn("fixing", w)
        self.assertEqual(w["fix"]["prs"], ["https://github.com/o/r/pull/12"])
        self.assertGreater(w["probe"]["since"], 1.0)  # the time spent fixing doesn't use up the watch
        body = workflows.runs.recheck_body(self.card, self.crew._until_step(self.card))
        self.assertIn("finished: the fix merged", body)
        self.assertIn("abc123", body)
        self.assertNotIn("Your watch", body)
        sent = []
        self.crew._start = lambda a: True
        self.crew._send = lambda a, text, images=None: sent.append(text) or True
        self.crew._begin(self.crew.agents["ops"], self.card)
        self.assertIn("finished: the fix merged", sent[-1])
        self.assertNotIn("wake", self.card["wait"])

    def test_a_stopped_or_deleted_fix_run_wakes_the_card_with_why(self):
        fx = self.fix_run(self.fix()["run"])
        fx.update(status="stopped", note="step 1: builder stopped it: already fixed on master in 9f1e2d")
        self.tick()
        self.assertEqual(self.card["wait"]["wake"], "fix stopped")
        self.assertIn("already fixed on master", workflows.runs.recheck_body(self.card, self.crew._until_step(self.card)))
        self.card["wait"].pop("wake")
        fx2 = self.fix_run(self.fix()["run"])
        self.crew.act("run_delete", {"id": fx2["id"], "tasks": True})
        self.tick()
        self.assertEqual(self.card["wait"]["wake"], "fix deleted")

    def test_a_second_watch_joins_the_running_fix(self):
        fx = self.fix_run(self.fix()["run"])
        other = self.crew._task(self.start_deploy("Second change")["tasks"][0])
        r = self.fix(other, join=fx["id"], reason="same lint error")
        self.assertTrue(r.get("joined"), r)
        self.assertEqual(fx["source"]["tasks"], [self.card["id"], other["id"]])
        self.assertIn("same lint error", fx["details"])
        n = len(self.crew.runs)
        self.assertIn("not a running", self.fix(self.crew._task(self.start_deploy("Third")["tasks"][0]), join=self.cd["id"])["error"])
        self.assertEqual(len(self.crew.runs), n + 1)  # the third deploy run, no fix run
        fx.update(status="done")
        self.tick()
        self.assertEqual([self.card["wait"]["wake"], other["wait"]["wake"]], ["fix merged", "fix merged"])

    def test_a_check_in_flight_when_the_fix_starts_wakes_nobody(self):
        self.card["wait"].pop("wake")
        pr = self.card["wait"]["probe"]
        self.fix()
        with mock.patch.object(workflows.probe, "run", lambda argv, cwd: ("completed\nfailed again", True)):
            workflows.probe._probe(self.crew, self.card)
        self.assertNotIn("wake", self.card["wait"])
        self.assertIs(self.card["wait"]["probe"], pr)

    def test_the_wait_survives_a_restart(self):
        fx = self.fix_run(self.fix()["run"])
        self.crew._save()
        crew = type(self.crew)(self.project, self.crew.config, ["claude-not-used"])
        t = crew._task(self.card["id"])
        self.assertEqual(t["wait"]["fixing"], fx["id"])
        self.assertEqual(workflows.runs.find_run(crew, fx["id"])["status"], "running")
        with crew.lock:
            workflows.fixes.tick(crew)
        self.assertEqual(t["wait"]["fixing"], fx["id"])  # still running: still waiting

    def test_what_cannot_wait_for_a_fix(self):
        self.assertTrue(self.fix().get("ok"))
        self.assertIn("already waits", self.fix()["error"])
        plain = {"id": 9200, "title": "Chat", "col": "backlog"}
        self.crew.tasks.append(plain)
        self.assertIn("not a ⟳", self.crew.act("pipeline_fix", {"task": 9200})["error"])
        other = self.crew._task(self.start_deploy("Other")["tasks"][0])
        other["col"] = "done"
        self.assertIn("is done", self.fix(other)["error"])

    def test_a_stopped_deploy_run_is_not_woken(self):
        fx = self.fix_run(self.fix()["run"])
        self.crew.act("run_stop", {"id": self.cd["id"]})
        fx.update(status="done")
        self.tick()
        self.assertNotIn("fixing", self.card["wait"])
        self.assertNotIn("wake", self.card["wait"])

    def test_missing_agents_are_named(self):
        for a in self.crew.agents.values():
            a["tags"] = [x for x in a["tags"] if x not in ("review", "ci")]
        self.assertIn("a CI agent", self.fix()["error"])
        self.assertNotIn("fixing", self.card["wait"])

    def test_the_deploy_agent_asks_for_a_fix_itself(self):
        self.assertIn("pipeline_fix", server.MCP_ACTIONS)
        ops = self.crew.agents["ops"]
        self.card["col"] = "doing"
        ops.update(status="running", task=self.card["id"])
        self.assertIn("reason", self.crew.act("pipeline_fix", {"from": "ops"}, via="mcp")["error"])
        r = self.crew.act("pipeline_fix", {"from": "ops", "reason": "ISC004 on master blocks the deploy"}, via="mcp")
        self.assertTrue(r.get("ok"), r)
        self.assertEqual(self.fix_run(r["run"])["source"]["by"], "ops")
        ops.update(status="idle", task=None)
        self.assertIn("step you are running", self.crew.act("pipeline_fix", {"from": "ops", "reason": "x"}, via="mcp")["error"])

    def test_blocked_parks_the_step_for_a_person_and_the_watch_can_still_continue_it(self):
        ops = self.crew.agents["ops"]
        self.card["col"] = "doing"
        ops.update(status="running", task=self.card["id"])
        r = self.crew.act("decide", {"from": "ops", "verdict": "blocked", "reason": "run cancelled; a person must run the stage"}, via="mcp")
        self.assertTrue(r.get("ok"), r)
        self.crew._on_event(ops, {"type": "result", "subtype": "success", "result": "parked", "total_cost_usd": 0})
        w = self.card["wait"]
        self.assertEqual((self.card["col"], w["blocked"]["reason"]), ("review", "run cancelled; a person must run the stage"))
        self.assertNotIn("decision", self.card)
        self.assertEqual((ops["status"], ops["task"]), ("idle", None))  # free for other work meanwhile
        self.assertEqual(self.fix_run(self.cd["id"])["status"], "running")  # nothing ended, nothing advanced
        for out, col in (("completed	failed", "review"), ("completed	succeeded", "backlog")):
            w["next"] = 0
            with mock.patch.object(workflows.probe, "run", return_value=(out, True)),                  mock.patch.object(workflows.probe.threading, "Thread",
                                   lambda target, args, daemon: type("T", (), {"start": lambda s: target(*args)})()):
                workflows.probe.tick(self.crew)
            self.assertEqual(self.card["col"], col, out)
        self.assertEqual(w["wake"], "done matched")
        self.assertNotIn("blocked", w)

    def test_blocked_is_only_for_watching_steps(self):
        b = self.crew.agents["builder"]
        t = {"id": 900, "title": "x", "col": "doing", "who": "builder", "created": 1}
        self.crew.tasks.append(t)
        b.update(status="running", task=900)
        self.assertIn("⟳", self.crew.act("decide", {"from": "builder", "verdict": "blocked", "reason": "x"}, via="mcp")["error"])


if __name__ == "__main__":
    unittest.main()
