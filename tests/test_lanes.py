"""Lanes: an agent runs up to max_parallel tasks at once, one Claude process per lane, and a card waiting in Review
never stops it from taking other work (agents/lanes.py)."""
import json
import os
import sys
import time
import unittest
from pathlib import Path
from unittest import mock

import agents
from tests.helpers import ROOT, TempCrewCase, server
from tests.test_git import G, GitCrewCase, run

RESULT = {"type": "result", "subtype": "success", "is_error": False, "result": "done", "total_cost_usd": 0.1}


def usage(n):
    return {"type": "assistant", "message": {"id": f"m{n}-{time.time_ns()}", "content": [], "usage": {"input_tokens": n, "output_tokens": 0}}}


class Lanes(TempCrewCase):
    def setUp(self):
        super().setUp()
        self.crew._save_config = lambda: None
        self.a = self.crew.agents["builder"]
        self.begun = []
        self.crew._begin = lambda ln, t: self.begun.append((ln["key"], t["id"])) or ln.update(status="running", task=t["id"])             or t.update(col="doing", who=ln["name"], lane=ln["key"]) or True

    def add(self, title, **kw):
        self.crew.act("add", {"title": title, "agent": "builder", **kw})
        return self.crew.tasks[-1]

    def test_one_at_a_time_by_default(self):
        self.add("one")
        self.add("two")
        self.crew.dispatch()
        self.assertEqual(self.begun, [("builder", 1)])

    def test_max_parallel_runs_that_many_at_once(self):
        self.crew.act("configure", {"agent": "builder", "max_parallel": 2})
        for x in ("one", "two", "three"):
            self.add(x)
        self.crew.dispatch()
        self.assertEqual(self.begun, [("builder", 1), ("builder.2", 2)])
        self.a.update(status="idle", task=None)  # lane 1 finished
        self.crew.dispatch()
        self.assertEqual(self.begun[-1], ("builder", 3))

    def test_a_card_in_review_with_changes_does_not_count_against_the_limit(self):
        self.crew.git = True
        with mock.patch.object(agents.lanes, "prepare", lambda crew, lane, wait=False: None):
            self.crew.tasks.append({"id": 50, "title": "Waiting", "col": "review", "who": "builder", "created": 0, "lane": "builder",
                                    "diff": {"stat": "1 file", "patch": "+x"}})
            self.a.update(status="review", task=50)
            nxt = self.add("next")
            self.add("after")
            self.crew.dispatch()
        self.assertEqual(self.begun, [("builder.2", nxt["id"])])  # one running, as max_parallel says; #50 waits for you on lane 1

    def test_a_session_one_lane_runs_is_not_started_in_another(self):
        self.crew.act("configure", {"agent": "builder", "max_parallel": 2})
        t1, t2 = self.add("one"), self.add("two")
        t2["sessions"] = {"builder": "s-shared"}
        self.a.update(status="running", task=t1["id"], session="s-shared")  # (a run's subagent steps share one session)
        t1["col"] = "doing"
        self.crew.dispatch()
        self.assertEqual(self.begun, [])

    def test_off_paused_or_killed_agents_take_no_work_on_any_lane(self):
        self.crew.act("configure", {"agent": "builder", "max_parallel": 3})
        self.add("one")
        for st in ("paused", "killed", "error"):
            self.a["status"] = st
            self.crew.dispatch()
        self.a.update(status="idle", off=True)
        self.crew.dispatch()
        self.assertEqual(self.begun, [])

    def test_max_parallel_is_checked_saved_and_exported(self):
        self.assertIn("error", self.crew.act("configure", {"agent": "builder", "max_parallel": 0}))
        self.assertIn("error", self.crew.act("configure", {"agent": "builder", "max_parallel": "lots"}))
        self.crew.act("configure", {"agent": "builder", "max_parallel": 3})
        self.assertEqual(next(x for x in agents.record.roster(self.crew) if x["name"] == "builder")["max_parallel"], 3)
        self.assertNotIn("max_parallel", next(x for x in agents.record.roster(self.crew) if x["name"] == "tester"))
        self.assertEqual(agents.lanes.cap({"max_parallel": 99}), agents.lanes.PARALLEL_MAX)

    def test_board_shows_every_lane_and_sums_cost_and_tokens(self):
        self.crew.act("configure", {"agent": "builder", "max_parallel": 2})
        self.add("one")
        self.add("two")
        self.crew.dispatch()
        ln2 = self.crew.lanes["builder"][0]
        self.a["tokens"]["in"], ln2["tokens"]["in"], self.a["cost"], ln2["cost"] = 100, 50, 1.0, 0.5
        snap = next(x for x in self.crew.snapshot()["agents"] if x["name"] == "builder")
        self.assertEqual((snap["status"], snap["running"], [ln["task"] for ln in snap["lanes"]]), ("running", 2, [1, 2]))
        self.assertEqual((snap["tokens"]["in"], snap["cost"]), (150, 1.5))
        self.crew._save()
        saved = json.loads(self.crew.state_file.read_text(encoding="utf-8"))["agents"]["builder"]
        self.assertEqual((saved["tokens"]["in"], saved["cost"]), (150, 1.5))

    def test_assign_starts_on_a_free_lane(self):
        self.crew.act("configure", {"agent": "builder", "max_parallel": 2})
        t1 = self.add("one")
        self.crew.dispatch()
        t2 = {"id": 9, "title": "Hand over", "col": "backlog", "who": None, "created": 0, "tag": "feature"}
        self.crew.tasks.append(t2)
        self.crew.act("assign", {"task": 9, "agent": "builder"})
        self.assertEqual(self.begun, [("builder", t1["id"]), ("builder.2", 9)])


class LaneTurns(TempCrewCase):
    """Turns on two lanes at once, through the real event handlers (no claude process)."""

    def setUp(self):
        super().setUp()
        self.crew._save_config = lambda: None
        self.crew._start = lambda ln: True
        self.crew._send = lambda ln, text, images=None: True
        self.crew._kill_proc = lambda key, wait=False: None
        self.crew.act("configure", {"agent": "builder", "max_parallel": 2})
        self.a = self.crew.agents["builder"]
        for title in ("one", "two"):
            self.crew.act("add", {"title": title, "agent": "builder"})
        self.crew.dispatch()
        self.ln2 = self.crew.lanes["builder"][0]
        self.t1, self.t2 = self.crew.tasks

    def test_each_lane_works_its_own_task(self):
        self.assertEqual((self.a["task"], self.ln2["task"], self.t1["lane"], self.t2["lane"]), (1, 2, "builder", "builder.2"))
        self.crew._on_event(self.ln2, {"type": "system", "subtype": "init", "session_id": "s-two"})
        self.assertEqual(self.t2["sessions"], {"builder": "s-two"})
        self.assertNotIn("sessions", self.t1)

    def test_tokens_are_counted_per_task(self):
        self.crew._on_event(self.a, usage(300))
        self.crew._on_event(self.ln2, usage(20))
        self.crew._on_event(self.ln2, RESULT)
        self.crew._on_event(self.a, RESULT)
        self.assertEqual((self.t1["tokens"], self.t2["tokens"], self.t1["col"], self.t2["col"]), (300, 20, "review", "review"))

    def test_crew_tools_act_on_the_calling_lane(self):
        r = self.crew.act("decide", {"from": "builder", "lane": "builder.2", "verdict": "stop", "reason": "nothing to do"})
        self.assertNotIn("error", r or {})
        self.assertEqual((self.t2.get("decision") or {}).get("verdict"), "stop")
        self.assertNotIn("decision", self.t1)
        self.crew.act("remember", {"from": "builder", "lane": "builder.2", "text": "the API is rate limited", "key": "api"}, via="mcp")
        self.assertEqual(self.crew.facts[-1]["task"], 2)

    def test_a_lane_from_another_agent_is_not_trusted(self):
        self.assertIsNot(self.crew._src({"from": "tester", "lane": "builder.2"}), self.ln2)

    def test_stop_stops_every_lane_and_pauses_the_agent(self):
        self.crew.act("stop", {"agent": "builder"})
        self.assertEqual((self.a["status"], self.ln2["status"]), ("paused", "idle"))
        self.assertEqual((self.t1["col"], self.t2["col"], self.t1.get("hold"), self.t2.get("hold")), ("backlog", "backlog", True, True))

    def test_stop_keeps_a_card_in_review_on_its_lane_and_still_pauses(self):
        self.crew.git = True
        self.t1.update(col="review", diff={"stat": "1", "patch": "+x"})
        self.a.update(status="review")  # lane 1 holds #1's changes; lane 2 works on #2
        self.crew.act("stop", {"agent": "builder"})
        self.assertEqual((self.a["status"], self.a["task"], self.t1["col"]), ("review", 1, "review"))
        snap = next(x for x in self.crew.snapshot()["agents"] if x["name"] == "builder")
        self.assertEqual(snap["status"], "paused")
        self.crew.act("add", {"title": "three", "agent": "builder"})
        self.crew.dispatch()
        self.assertEqual(self.crew._task(3)["col"], "backlog")  # paused: nothing new starts
        self.crew.act("resume", {"agent": "builder"})
        self.assertNotIn("paused", self.a)
        self.assertEqual(self.a["status"], "review")

    def test_health_repairs_only_the_lane_that_lost_its_process(self):
        self.crew._alive = lambda key: key != "builder.2"
        with mock.patch.object(agents.health, "REPAIR_AFTER", 0):
            agents.health.check(self.crew)
        self.assertEqual((self.a["status"], self.ln2["status"], self.t1["col"], self.t2["col"]), ("running", "error", "doing", "backlog"))

    def test_a_reply_on_a_card_whose_lane_moved_on_resumes_it_on_a_free_lane(self):
        self.crew._on_event(self.ln2, {"type": "system", "subtype": "init", "session_id": "s-two"})
        self.crew._on_event(self.ln2, RESULT)  # outside git the card frees its lane at once
        self.assertEqual((self.t2["col"], self.ln2["status"]), ("review", "idle"))
        self.crew.act("add", {"title": "three", "agent": "builder"})
        self.crew.dispatch()
        self.assertEqual(self.ln2["task"], 3)  # lane 2 took new work; both lanes busy now
        self.assertEqual(self.crew.act("reject", {"task": 2, "text": "again"}), {"ok": True})
        ln3 = self.crew._holder(self.t2)
        self.assertEqual((ln3["key"], ln3["session"], self.t2["col"]), ("builder.3", "s-two", "doing"))  # your send-back may pass the limit

    def test_approving_a_card_frees_its_lane_and_leaves_the_others(self):
        self.crew.git = True
        self.ln2.update(status="review")
        self.t2.update(col="review", diff={"stat": "1", "patch": "+x"})
        with mock.patch.object(self.crew, "_approve_git", return_value={"commit": None, "full": None}):
            self.assertNotIn("error", self.crew._approve(self.t2))
        self.assertEqual((self.ln2["status"], self.ln2["task"], self.a["status"], self.a["task"]), ("idle", None, "running", 1))


class LaneWorktrees(GitCrewCase):
    """Real git: lane 2 gets its own worktree and branch, and the card's git actions use them."""

    def wait_ready(self, ln, timeout=30):
        end = time.time() + timeout
        while time.time() < end:
            with self.crew.lock:
                if not ln.get("preparing"):
                    return
            time.sleep(0.05)
        self.fail("lane worktree not made")

    def test_lane_2_worktree_branch_approve_and_merge(self):
        self.crew._save_config = lambda: None
        with self.crew.lock:
            self.crew._ensure_worktree(self.agent)
            ln = agents.lanes.new(self.crew, self.agent)
        self.wait_ready(ln)
        wt2 = server.WORK_ROOT / "wt" / self.crew.key / "builder.2"
        self.assertEqual((ln["branch"], Path(ln["cwd"])), ("crew/builder.2", wt2))
        self.assertTrue(self.branch_exists("crew/builder.2"))
        t = {"id": 7, "title": "Lane two work", "col": "review", "who": "builder", "created": 0,
             "lane": ln["key"], "cwd": ln["cwd"], "branch": ln["branch"]}
        self.crew.tasks.append(t)
        (wt2 / "two.txt").write_text("from lane 2\n", encoding="utf-8")
        self.assertFalse(self.crew.act("diff", {"task": 7}).get("error"))
        self.assertIn("two.txt", t["diff"]["stat"])
        self.assertFalse(self.crew._approve(t).get("error"))
        self.assertTrue(t["commit"])
        self.assertIn("two.txt", run(self.project, "show", "--stat", "crew/builder.2"))
        self.assertNotIn("two.txt", run(self.project, "show", "--stat", "crew/builder"))  # lane 1's branch is untouched
        self.assertFalse(self.crew.act("merge", {"task": 7}).get("error"))
        self.assertTrue((self.project / "two.txt").exists())

    def test_rename_moves_every_lane(self):
        self.crew._save_config = lambda: None
        with self.crew.lock:
            self.crew._ensure_worktree(self.agent)
            ln = agents.lanes.new(self.crew, self.agent, wait=True)
            self.crew.tasks.append({"id": 3, "title": "x", "col": "done", "who": "builder", "created": 0,
                                    "lane": "builder.2", "cwd": ln["cwd"], "branch": "crew/builder.2"})
            self.assertIsNone(self.crew.act("rename_agent", {"agent": "builder", "name": "maker"}).get("error"))
        self.assertTrue(self.branch_exists("crew/maker.2") and self.branch_exists("crew/maker"))
        self.assertFalse(self.branch_exists("crew/builder.2"))
        self.assertEqual((ln["key"], ln["branch"]), ("maker.2", "crew/maker.2"))
        self.assertTrue((server.WORK_ROOT / "wt" / self.crew.key / "maker.2" / ".git").exists())
        t = self.crew._task(3)
        self.assertEqual((t["lane"], t["branch"], Path(t["cwd"]).name), ("maker.2", "crew/maker.2", "maker.2"))


class TwoProcesses(TempCrewCase):
    """End to end on the demo's fake claude: one agent, two tasks, two processes at once."""

    def setUp(self):
        super().setUp()
        self.crew._save_config = lambda: None
        patcher = mock.patch.dict(os.environ, {"CREW_DEMO_PACE": "0.05", "CREW_DEMO_COST": "0.2"})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.crew.claude = [sys.executable, str(ROOT / "demo" / "fake_claude.py")]
        self.addCleanup(self.crew.shutdown)

    def test_both_run_at_once_and_reach_review(self):
        self.crew.act("configure", {"agent": "builder", "max_parallel": 2})
        self.crew.act("add", {"title": "Add rate limiting to the login endpoint", "agent": "builder"})
        self.crew.act("add", {"title": "Fix flaky date parsing on the invoices page", "agent": "builder"})
        self.crew.dispatch()
        with self.crew.lock:
            self.assertEqual(sorted(k for k in self.crew.procs), ["builder", "builder.2"])
            self.assertEqual([t["col"] for t in self.crew.tasks], ["doing", "doing"])
        end = time.time() + 60
        while time.time() < end and any(t["col"] != "review" for t in self.crew.tasks):
            time.sleep(0.1)
        self.assertEqual([t["col"] for t in self.crew.tasks], ["review", "review"])
        self.assertEqual({t["lane"] for t in self.crew.tasks}, {"builder", "builder.2"})
        self.assertTrue(all(t["cost"] > 0 for t in self.crew.tasks))
        pids = json.loads(self.crew.pid_file.read_text(encoding="utf-8"))["procs"]
        self.assertEqual(sorted(p["agent"] for p in pids), ["builder", "builder.2"])


if __name__ == "__main__":
    unittest.main()
