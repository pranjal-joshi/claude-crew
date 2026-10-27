"""Phase 2b: permission inbox, cost caps, git outside the lock, process hygiene.
Only processes these tests start themselves are ever signalled; foreign pids are never touched."""
import json
import os
import subprocess
import sys
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

import agents
from tests.helpers import ROOT, TempCrewCase, server
from tests.test_git import GitCrewCase

RESULT = {"type": "result", "subtype": "success", "is_error": False, "result": "done"}
KEY = "proj-abc123"
CLAUDE_CMD = f"python demo/fake_claude.py -p --input-format stream-json --output-format stream-json --mcp-config=C:/x/mcp/{KEY}-builder.json"


def result(total, **extra):
    return RESULT | {"total_cost_usd": total} | extra


def wait_for(fn, what, timeout=40):
    end = time.time() + timeout
    while time.time() < end:
        if fn():
            return
        time.sleep(0.1)
    raise AssertionError(f"timed out waiting for {what}")


class Denials(TempCrewCase):
    def test_parse_denials_reads_stream_json_fields(self):
        raw = [{"tool_name": "Bash", "tool_use_id": "toolu_1", "tool_input": {"command": "npm test --silent"}},
               {"tool_name": "WebFetch", "tool_use_id": "toolu_2", "tool_input": {"url": "https://example.com/docs?q=1"}},
               {"tool_name": "Edit", "tool_use_id": "toolu_3", "tool_input": {"file_path": "/etc/hosts"}},
               {"tool_input": {}}, "junk", {"tool_name": 5}]
        got = server.parse_denials(raw)
        self.assertEqual([(d["tool"], d["cmd"], d["rule"]) for d in got], [
            ("Bash", "npm test --silent", "Bash(npm test:*)"),
            ("WebFetch", "https://example.com/docs?q=1", "WebFetch(domain:example.com)"),
            ("Edit", "/etc/hosts", "Edit")])
        self.assertEqual(got[0]["use_id"], "toolu_1")
        self.assertEqual(server.parse_denials(None), [])
        self.assertEqual(server.parse_denials("x"), [])

    def test_allow_rule_builder(self):
        rule = lambda cmd: server.allow_rule("Bash", {"command": cmd})
        self.assertEqual(rule("npm test"), "Bash(npm test:*)")
        self.assertEqual(rule("git status -s"), "Bash(git status:*)")
        self.assertEqual(rule("pytest tests/test_x.py -q"), "Bash(pytest:*)")  # a path is not a subcommand
        self.assertEqual(rule("python -m pytest -q"), "Bash(python -m pytest:*)")
        self.assertEqual(rule("npm run build -- --prod"), "Bash(npm run build:*)")
        self.assertEqual(rule("FOO=1 ls -la"), "Bash(ls:*)")
        self.assertEqual(rule("cd sub && cargo build --release"), "Bash(cargo build:*)")  # skips the cd
        self.assertEqual(rule("rm foo.txt"), "Bash(rm:*)")
        self.assertEqual(rule(""), "Bash")
        self.assertEqual(server.allow_rule("mcp__ops__pod_health", {"deployment": "api"}), "mcp__ops__pod_health")
        self.assertEqual(server.allow_rule("WebSearch", {"query": "x"}), "WebSearch")

    def test_clean_rule(self):
        for ok in ("Bash(npm test:*)", "WebFetch(domain:example.com)", "Edit", "mcp__ops__pod_health"):
            self.assertIsNone(server.clean_rule(ok)[0], ok)
        for bad in ("", "  ", "Bash(a\nb)", "(x)", "Bash(npm test:*) extra", "x" * 400):
            self.assertIsNotNone(server.clean_rule(bad)[0], bad)
        self.assertEqual(server.clean_rule("  Bash(ls:*) ")[1], "Bash(ls:*)")

    def running(self, agent="builder", tid=1):
        a = self.crew.agents[agent]
        t = {"id": tid, "title": "T", "details": "", "tag": "feature", "col": "doing", "who": agent, "pinned": agent, "created": 0}
        self.crew.tasks.append(t)
        self.crew.next_id = tid + 1
        a.update(status="running", task=tid)
        return a, t

    def test_result_event_fills_the_inbox_deduplicated(self):
        a, t = self.running()
        den = lambda cmd, tool="Bash": {"tool_name": tool, "tool_use_id": "u", "tool_input": {"command": cmd}}
        self.crew._on_event(a, result(0.1, permission_denials=[den("npm test"), den("npm test -- -u"), den("ls -la"), den("echo hi", "Write")]))
        got = [(d["tool"], d["rule"], d["n"], d["task"]) for d in a["denials"]]
        self.assertEqual(got, [("Bash", "Bash(npm test:*)", 2, 1), ("Bash", "Bash(ls:*)", 1, 1), ("Write", "Write", 1, 1)])
        self.assertEqual(len({d["id"] for d in a["denials"]}), 3)
        self.assertTrue(any(e["kind"] == "blocked" for e in self.crew.feed))
        a.update(status="running", task=1)
        t["col"] = "doing"
        self.crew._on_event(a, result(0.2, permission_denials=[den("npm test")]))  # the same call again: counted, not duplicated
        self.assertEqual(len(a["denials"]), 3)
        self.assertEqual(a["denials"][0]["n"], 3)

    def test_boss_denials_are_logged_not_offered(self):
        self.crew._on_event(self.crew.boss, result(0.1, permission_denials=[{"tool_name": "Bash", "tool_input": {"command": "rm -rf x"}}]))
        self.assertEqual(self.crew.boss["denials"], [])
        self.assertIn("blocked", self.crew.boss["log"][-2]["text"])

    def test_denials_are_per_task_and_dropped_when_the_task_ends(self):
        a, t = self.running()
        self.crew._on_event(a, result(0.1, permission_denials=[{"tool_name": "Bash", "tool_input": {"command": "make"}}]))
        self.assertEqual(len(a["denials"]), 1)
        self.crew.act("approve", {"task": 1})
        self.assertEqual(a["denials"], [])

    def test_allow_persists_and_restarts(self):
        a, t = self.running()
        self.crew._on_event(a, result(0.1, permission_denials=[{"tool_name": "Bash", "tool_input": {"command": "npm run lint --silent"}}]))
        d = a["denials"][0]
        before = list(server.DEFAULT_CONFIG["allowed_tools"])
        with mock.patch.object(self.crew, "_retry", return_value={"ok": True}) as retry:
            self.assertEqual(self.crew.act("denial_allow", {"agent": "builder", "id": d["id"]}), {"ok": True})
        retry.assert_called_once()
        self.assertIn("Bash(npm run lint:*)", self.crew.config["allowed_tools"])
        self.assertEqual(server.DEFAULT_CONFIG["allowed_tools"], before)  # the shared defaults are untouched
        self.assertIn("Bash(npm run lint:*)", json.loads(self.cfg_path.read_text(encoding="utf-8"))["allowed_tools"])
        self.assertEqual(a["denials"], [])
        self.assertIn("Bash(npm run lint:*)", self.crew._launch_key(a)[3])  # a running agent restarts at its next turn end

    def test_allow_with_an_edited_rule_and_bad_rule(self):
        a, t = self.running()
        self.crew._on_event(a, result(0.1, permission_denials=[{"tool_name": "Bash", "tool_input": {"command": "npm run lint"}}]))
        d = a["denials"][0]
        self.assertIn("rule", self.crew.act("denial_allow", {"agent": "builder", "id": d["id"], "rule": "not a rule("})["error"])
        self.assertEqual(len(a["denials"]), 1)
        with mock.patch.object(self.crew, "_retry", return_value={"ok": True}):
            self.crew.act("denial_allow", {"agent": "builder", "id": d["id"], "rule": "Bash(npm run:*)"})
        self.assertIn("Bash(npm run:*)", self.crew.config["allowed_tools"])
        self.assertIn("no longer", self.crew.act("denial_allow", {"agent": "builder", "id": d["id"]})["error"])

    def test_allow_once_lives_on_the_task_and_ends_with_it(self):
        a, t = self.running()
        self.crew._on_event(a, result(0.1, permission_denials=[{"tool_name": "Bash", "tool_input": {"command": "npm run lint"}}]))
        d = a["denials"][0]
        with mock.patch.object(self.crew, "_retry", return_value={"ok": True}):
            self.crew.act("denial_allow", {"agent": "builder", "id": d["id"], "once": True})
        self.assertEqual(t["allow_once"], ["Bash(npm run lint:*)"])
        self.assertNotIn("Bash(npm run lint:*)", self.crew.config["allowed_tools"])
        a["task"] = 1  # the (mocked) retry puts a lane back on the card: it starts with the rule
        self.assertEqual(self.crew._once_rules(a), ["Bash(npm run lint:*)"])
        self.crew.act("approve", {"task": 1})  # the task ends: the rule goes with it
        self.assertNotIn("allow_once", t)
        self.assertEqual(self.crew._once_rules(a), [])

    def test_dismiss(self):
        a, t = self.running()
        self.crew._on_event(a, result(0.1, permission_denials=[{"tool_name": "Bash", "tool_input": {"command": "make"}}]))
        self.assertEqual(self.crew.act("denial_dismiss", {"agent": "builder", "id": a["denials"][0]["id"]}), {"ok": True})
        self.assertEqual(a["denials"], [])
        self.assertIn("error", self.crew.act("denial_dismiss", {"agent": "builder", "id": 99}))

    def test_inbox_and_budget_actions_are_ui_only(self):
        for act in ("denial_allow", "denial_dismiss", "budgets", "approve", "merge", "open_pr", "unapprove", "diff"):
            self.assertNotIn(act, server.MCP_ACTIONS)


class DemoFlow(TempCrewCase):
    """End to end on the demo's fake claude: a refused command lands in the inbox, Allow & retry restarts and retries."""

    def setUp(self):
        super().setUp()
        for k, v in (("CREW_DEMO_PACE", "0.02"), ("CREW_DEMO_COST", "0.7")):
            patcher = mock.patch.dict(os.environ, {k: v})
            patcher.start()
            self.addCleanup(patcher.stop)
        self.crew.claude = [sys.executable, str(ROOT / "demo" / "fake_claude.py")]
        self.addCleanup(self.crew.shutdown)
        self.a = self.crew.agents["builder"]

    def run_denied_task(self):
        self.crew.act("add", {"title": "Deny the lint run", "agent": "builder"})
        self.crew.dispatch()
        wait_for(lambda: (self.crew._task(1) or {}).get("col") == "review", "the first turn to end")
        return self.crew._task(1)

    def test_denial_cost_pidfile_allow_and_retry(self):
        t = self.run_denied_task()
        self.assertEqual([(d["tool"], d["rule"]) for d in self.a["denials"]], [("Bash", "Bash(npm run lint:*)")])
        self.assertEqual(self.a["denials"][0]["cmd"], "npm run lint --silent")
        self.assertAlmostEqual(t["cost"], 0.7)
        self.assertAlmostEqual(self.crew.day_spend, 0.7)
        pid = self.crew.procs["builder"].pid
        self.assertEqual(json.loads(self.crew.pid_file.read_text(encoding="utf-8"))["procs"], [{"pid": pid, "agent": "builder"}])
        r = self.crew.act("denial_allow", {"agent": "builder", "id": self.a["denials"][0]["id"]})
        self.assertEqual(r, {"ok": True})
        self.assertEqual(t["col"], "doing")  # asked to retry, like a send-back
        wait_for(lambda: t["col"] == "review" and self.a["status"] != "running", "the retry to end")
        self.assertEqual(self.a["denials"], [])
        self.assertIn("lint: no problems", [e["text"] for e in self.a["log"] if e["kind"] == "text"])
        self.assertNotEqual(self.crew.procs["builder"].pid, pid)  # restarted with the new flags, session resumed
        self.assertGreater(t["cost"], 0.7)  # the retry turn was charged to the same task
        self.assertIn("Bash(npm run lint:*)", json.loads(self.cfg_path.read_text(encoding="utf-8"))["allowed_tools"])

    def test_allow_once_retry_then_the_rule_ends_with_the_task(self):
        t = self.run_denied_task()
        self.crew.act("denial_allow", {"agent": "builder", "id": self.a["denials"][0]["id"], "once": True})
        wait_for(lambda: t["col"] == "review" and self.a["status"] != "running", "the retry to end")
        self.assertEqual(t["allow_once"], ["Bash(npm run lint:*)"])
        self.assertNotIn("Bash(npm run lint:*)", self.crew.config["allowed_tools"])
        self.crew.act("approve", {"task": 1})
        self.assertNotIn("allow_once", t)
        wait_for(lambda: not self.crew._alive("builder"), "the process started with the one-off rule to be ended")

    def test_pidfile_is_removed_when_the_process_exits(self):
        self.run_denied_task()
        pid = self.crew.procs["builder"].pid
        self.assertTrue(self.crew.pid_file.exists())
        self.crew.shutdown()
        wait_for(lambda: not self.crew.pid_file.exists(), "the pidfile to go")
        self.assertNotIn(pid, self.crew.pids)


class Budgets(TempCrewCase):
    def running(self, agent, tid, **task):
        a = self.crew.agents[agent]
        t = {"id": tid, "title": f"T{tid}", "details": "", "tag": "feature", "col": "doing", "who": agent, "pinned": agent, "created": 0} | task
        self.crew.tasks.append(t)
        self.crew.next_id = max(self.crew.next_id, tid + 1)
        a.update(status="running", task=tid)
        return a, t

    def test_cost_is_charged_to_the_task_and_the_day(self):
        a, t = self.running("builder", 1)
        self.crew._on_event(a, result(0.5))
        a.update(status="running", task=1)
        t["col"] = "doing"
        self.crew._on_event(a, result(0.8))  # total_cost_usd is cumulative per process
        self.assertAlmostEqual(t["cost"], 0.8)
        self.assertAlmostEqual(self.crew.day_spend, 0.8)
        self.assertAlmostEqual(a["cost"], 0.8)

    def test_task_budget_flags_the_task_and_blocks_send_back(self):
        self.crew.config["budget_task_usd"] = 1.0
        a, t = self.running("builder", 1)
        self.crew._on_event(a, result(0.9))
        self.assertNotIn("budget_hit", t)
        a.update(status="running", task=1)
        t["col"] = "doing"
        self.crew._on_event(a, result(1.5))
        self.assertTrue(t["budget_hit"])
        self.assertEqual(t["col"], "review")  # the finished work still reaches review
        self.assertTrue(any(e["kind"] == "budget" and "#1 budget reached ($1.50 of $1.00)" in e["text"] for e in self.crew.feed))
        self.assertTrue(any("budget reached ($1.50 of $1.00)" in e["text"] for e in a["log"]))
        self.assertIn("budget", self.crew.act("reject", {"task": 1, "text": "more"})["error"])
        self.assertEqual(self.crew.act("budgets", {"task_usd": 5}), {"ok": True})  # raising it clears the flag
        self.assertNotIn("budget_hit", t)
        self.assertEqual(self.crew.config["budget_task_usd"], 5.0)
        self.assertEqual(json.loads(self.cfg_path.read_text(encoding="utf-8"))["budget_task_usd"], 5.0)

    def test_over_budget_task_after_an_error_is_held_and_not_redispatched(self):
        self.crew.config["budget_task_usd"] = 1.0
        a, t = self.running("builder", 1)
        self.crew._on_event(a, result(2.0, is_error=True, subtype="error_during_execution", result="boom"))
        self.assertEqual(t["col"], "backlog")
        self.assertTrue(t["hold"])
        a["status"] = "idle"
        t.pop("hold")  # even without the hold, the dispatcher skips it
        with mock.patch.object(self.crew, "_start") as start:
            self.crew.dispatch()
        start.assert_not_called()
        self.assertEqual(a["status"], "idle")

    def test_budget_off_by_default_and_with_zero(self):
        a, t = self.running("builder", 1)
        self.crew._on_event(a, result(500))
        self.assertNotIn("budget_hit", t)
        self.assertFalse(self.crew._day_blocked())
        self.crew.config["budget_day_usd"] = 0
        self.assertFalse(self.crew._day_blocked())

    def test_budget_input_is_validated(self):
        for bad in ("abc", -1, 10 ** 9):
            self.assertIn("error", self.crew.act("budgets", {"task_usd": bad}))
        self.assertEqual(self.crew.act("budgets", {"task_usd": "", "day_usd": "2.5"}), {"ok": True})
        self.assertEqual((self.crew._budget("budget_task_usd"), self.crew._budget("budget_day_usd")), (0.0, 2.5))
        b = self.crew.snapshot()["budget"]
        self.assertEqual({k: b[k] for k in ("task_usd", "day_usd", "day_spent", "blocked")}, {"task_usd": 0.0, "day_usd": 2.5, "day_spent": 0.0, "blocked": False})
        self.assertEqual((b["tokens"]["total"], b["tokens"]["limit"]), (0, 0))

    def test_day_budget_stops_everyone_and_blocks_dispatch(self):
        self.crew.config["budget_day_usd"] = 1.0
        a, t1 = self.running("builder", 1)
        b, t2 = self.running("tester", 2)
        self.crew.act("add", {"title": "queued", "tag": "feature", "agent": "designer"})
        self.crew._on_event(a, result(1.2))  # builder's turn ends over the day's budget while the tester is mid-turn
        self.assertEqual(b["status"], "paused")  # stopped like ■ Stop: session kept, task on hold
        self.assertEqual((t2["col"], t2["hold"]), ("backlog", True))
        self.assertTrue(b["budget_paused"])
        self.assertEqual(t1["col"], "review")
        self.assertTrue(any(e["kind"] == "budget" and "budget reached ($1.20 of $1.00 today)" in e["text"] for e in self.crew.feed))
        self.assertTrue(any("budget reached" in e["text"] for e in b["log"]))
        self.assertTrue(self.crew.snapshot()["budget"]["blocked"])
        with mock.patch.object(self.crew, "_start") as start:
            self.crew.dispatch()
        start.assert_not_called()
        self.assertEqual(self.crew.agents["designer"]["status"], "idle")
        budget_events = lambda: [e for e in self.crew.feed if e["kind"] == "budget"]
        n_events = len(budget_events())
        self.crew._on_event(a, result(1.5))  # the stop is applied once, not on every later result
        self.assertEqual(len(budget_events()), n_events)

    def test_raising_the_day_budget_resumes_what_it_held(self):
        self.crew.config["budget_day_usd"] = 1.0
        a, t1 = self.running("builder", 1)
        b, t2 = self.running("tester", 2)
        self.crew._on_event(a, result(1.2))
        self.crew.act("budgets", {"day_usd": 0.5})  # still blocked: nothing resumes
        self.assertEqual(b["status"], "paused")
        self.crew.act("budgets", {"day_usd": 10})
        self.assertEqual(b["status"], "idle")
        self.assertFalse(b["budget_paused"])
        self.assertNotIn("hold", t2)
        self.assertFalse(self.crew._day_blocked())

    def test_a_new_local_day_resets_the_spend_and_resumes(self):
        self.crew.config["budget_day_usd"] = 1.0
        a, t1 = self.running("builder", 1)
        b, t2 = self.running("tester", 2)
        self.crew._on_event(a, result(1.2))
        self.assertTrue(self.crew._day_blocked())
        self.crew.touch()  # persisted with its day
        again = server.Crew(self.project, json.loads(json.dumps(self.crew.config)), ["claude-not-used"])
        self.assertAlmostEqual(again.day_spend, 1.2)  # survives a restart on the same day
        with mock.patch.object(server, "today", return_value="2099-01-01"):
            self.assertTrue(self.crew._roll_day())
            self.assertEqual((self.crew.day, self.crew.day_spend), ("2099-01-01", 0.0))
            self.assertFalse(self.crew._day_blocked())
            self.assertEqual(b["status"], "idle")
            self.assertNotIn("hold", t2)
            self.assertFalse(self.crew._roll_day())  # only once
            self.crew._on_event(a, result(0.3 + 1.2))
            self.assertAlmostEqual(self.crew.day_spend, 0.3)  # only the new turn's cost counts today
            lost = server.Crew(self.project, json.loads(json.dumps(self.crew.config)), ["claude-not-used"])
        self.assertEqual(lost.day_spend, 0.0)  # state saved on another day starts at zero

    def test_dispatch_rolls_the_day_over(self):
        self.crew.day, self.crew.day_spend = "2000-01-01", 99.0
        self.crew.dispatch()
        self.assertEqual((self.crew.day, self.crew.day_spend), (server.today(), 0.0))


class GitOutsideTheLock(GitCrewCase):
    def setUp(self):
        super().setUp()
        self.crew._ensure_worktree(self.agent)
        self.checks = []
        self.during = None
        real = server.git

        def probe(*args, **kw):
            got = []

            def try_lock():
                got.append(self.crew.lock.acquire(timeout=3))
                if got[0]:
                    self.crew.lock.release()
            th = threading.Thread(target=try_lock)
            th.start()
            th.join()
            self.checks.append(got[0])
            if self.during:
                self.during, fn = None, self.during
                fn()
            return real(*args, **kw)
        p = mock.patch.object(server, "git", probe)
        p.start()
        self.addCleanup(p.stop)

    def task(self, col="review", tid=1):
        t = {"id": tid, "title": "Task", "details": "", "tag": "feature", "col": col, "who": "builder", "summary": "done"}
        self.crew.tasks.append(t)
        self.crew.next_id = tid + 1
        return t

    def test_approve_diff_merge_unapprove_pr_never_hold_the_lock_during_git(self):
        (self.wt / "work.txt").write_text("x\n", encoding="utf-8")
        t = self.task()
        self.agent.update(status="review", task=1)
        self.assertEqual(self.crew.act("diff", {"task": 1}), {"ok": True})
        self.assertIn("work.txt", t["diff"]["patch"])
        self.assertTrue(self.crew.act("approve", {"task": 1})["ok"])
        self.assertTrue(t["commit"])
        self.assertTrue(self.crew.act("unapprove", {"task": 1})["ok"])
        self.assertEqual(t["col"], "review")
        self.assertTrue(self.crew.act("approve", {"task": 1})["ok"])
        self.assertTrue(self.crew.act("merge", {"task": 1})["ok"])
        t2 = self.task("done", 2)
        t2.update(commit="abc", commit_full="abc", who="builder")
        t2.pop("merged", None)
        with mock.patch.object(server.shutil, "which", return_value="gh"):
            self.assertIn("origin", self.crew.act("open_pr", {"task": 2})["error"])
        self.assertGreater(len(self.checks), 8)  # every git call was observed
        self.assertTrue(all(self.checks), "self.lock was held while git ran")
        self.assertEqual(self.crew._busy, {})

    def test_task_is_busy_during_git_and_a_second_click_is_refused(self):
        (self.wt / "work.txt").write_text("x\n", encoding="utf-8")
        t = self.task()
        self.agent.update(status="review", task=1)
        seen = {}

        def second_click():
            seen["again"] = self.crew.act("approve", {"task": 1})
            seen["delete"] = self.crew.act("delete", {"task": 1})
            seen["busy"] = [x.get("busy") for x in self.crew.snapshot()["tasks"]]
        self.during = second_click
        self.assertTrue(self.crew.act("approve", {"task": 1})["ok"])
        self.assertIn("busy", seen["again"]["error"])
        self.assertIn("busy", seen["delete"]["error"])
        self.assertEqual(seen["busy"], ["approve"])
        self.assertEqual(t["col"], "done")
        self.assertEqual(run_count(self.wt, "log", "--oneline"), 2)  # one approval commit, not two
        self.assertNotIn("busy", self.crew.snapshot()["tasks"][0])

    def test_result_event_collects_the_diff_after_the_handler_returns(self):
        (self.wt / "work.txt").write_text("x\n", encoding="utf-8")
        t = self.task("doing")
        self.agent.update(status="running", task=1)
        with self.crew.lock:
            self.crew._on_event(self.agent, result(0.1))
        self.assertEqual(self.checks, [])  # nothing ran git inside the handler
        self.assertTrue(t["diff"]["pending"])
        self.crew._run_jobs()
        self.assertFalse(t["diff"].get("pending"))
        self.assertIn("work.txt", t["diff"]["patch"])
        self.assertTrue(all(self.checks) and self.checks)

    def test_workflow_auto_gate_commits_outside_the_lock_and_advances(self):
        self.crew.config["workflows"] = [{"id": "w9", "name": "Two", "description": "", "steps": [
            {"agent": "builder", "title": "One", "details": "", "gate": "auto"}, {"agent": "tester", "title": "Two", "details": "", "gate": "approve"}],
            "output": {"kind": "board"}}]
        (self.wt / "work.txt").write_text("x\n", encoding="utf-8")
        run = self.crew._start_run(self.crew.config["workflows"][0], "go")
        t = self.crew._task(run["tasks"][0])
        t.update(col="doing")
        self.agent.update(status="running", task=t["id"])
        with self.crew.lock:
            self.crew._on_event(self.agent, result(0.1))
        self.assertEqual(t["col"], "review")  # the handler only queued the commit
        self.crew._run_jobs()
        self.assertEqual(t["col"], "done")
        self.assertTrue(t["commit"])
        self.assertEqual(len(run["tasks"]) and run["tasks"][1] is not None, True)  # the next step exists
        self.assertTrue(all(self.checks))

    def test_auto_gate_waits_for_the_cards_diff_on_another_thread(self):
        # #91/#92: another agent's reader thread took the approve job while this card's diff still held it busy
        self.crew.config["workflows"] = [{"id": "w9", "name": "Two", "description": "", "steps": [
            {"agent": "builder", "title": "One", "details": "", "gate": "auto"}, {"agent": "tester", "title": "Two", "details": "", "gate": "approve"}],
            "output": {"kind": "board"}}]
        run = self.crew._start_run(self.crew.config["workflows"][0], "go")
        t = self.crew._task(run["tasks"][0])
        t.update(col="doing")
        self.agent.update(status="running", task=t["id"])
        with self.crew.lock:
            self.crew._on_event(self.agent, result(0.1))
            self.crew._draining = True  # another reader thread is mid-queue
        self.crew._run_jobs()
        self.assertEqual(t["col"], "review")  # left for the thread that is draining
        self.crew._draining = False
        self.crew._busy[t["id"]] = "diff"  # you are looking at its diff
        threading.Timer(0.3, lambda: self.crew._busy.pop(t["id"], None)).start()
        self.crew._run_jobs()
        self.assertEqual(t["col"], "done")
        self.assertFalse(self.crew._draining)


def run_count(cwd, *args):
    return len(server.git.__wrapped__(Path(cwd), *args).stdout.split()) if hasattr(server.git, "__wrapped__") else len(
        subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True).stdout.splitlines())


class Pidfile(TempCrewCase):
    def test_write_and_remove(self):
        self.crew._pid_add(4242, "builder")
        self.crew._pid_add(4343, "tester")
        data = json.loads(self.crew.pid_file.read_text(encoding="utf-8"))
        self.assertEqual(data["owner"], os.getpid())
        self.assertEqual(data["procs"], [{"pid": 4242, "agent": "builder"}, {"pid": 4343, "agent": "tester"}])
        self.assertEqual(self.crew.pid_file.parent, server.WORK_ROOT / "pids")
        self.crew._pid_drop(4242)
        self.assertEqual(json.loads(self.crew.pid_file.read_text(encoding="utf-8"))["procs"], [{"pid": 4343, "agent": "tester"}])
        self.crew._pid_drop(4343)
        self.assertFalse(self.crew.pid_file.exists())
        self.crew._pid_drop(4343)  # idempotent

    def test_orphan_filter_is_pure_and_strict(self):
        entries = [{"pid": 11}, {"pid": 22}, {"pid": 33}, {"pid": 44}, {"pid": "x"}, "junk", {"pid": os.getpid()}]
        cmds = {11: CLAUDE_CMD,                                    # an agent of this project
                22: "C:\\Windows\\notepad.exe README.md",          # a reused pid: someone else's process
                33: CLAUDE_CMD.replace(KEY, "other-ffffff"),       # a claude of another project
                os.getpid(): CLAUDE_CMD}                           # never ourselves
        # 44 is not alive at all (absent from cmds)
        self.assertEqual(agents.procs.orphans_to_kill(entries, cmds, KEY), [11])
        self.assertEqual(agents.procs.orphans_to_kill(entries, cmds, ""), [11, 33])
        self.assertTrue(agents.procs.is_agent_cmdline('node C:\\x\\claude-code\\cli.js -p --output-format stream-json', ""))
        self.assertFalse(agents.procs.is_agent_cmdline("claude --version", ""))
        self.assertFalse(agents.procs.is_agent_cmdline("python -m http.server stream-json", ""))
        self.assertFalse(agents.procs.is_agent_cmdline("", KEY))

    def test_reap_only_signals_matching_pids(self):
        self.crew.pid_file.parent.mkdir(parents=True, exist_ok=True)
        self.crew.pid_file.write_text(json.dumps({"owner": 1, "procs": [
            {"pid": 11, "agent": "builder"}, {"pid": 22, "agent": "tester"}, {"pid": 33, "agent": "ops"}]}), encoding="utf-8")
        cmds = {11: CLAUDE_CMD.replace(KEY, self.crew.key), 22: "C:\\Windows\\notepad.exe"}
        killed = []
        with mock.patch.object(agents.procs, "proc_cmdlines", side_effect=lambda pids: {p: c for p, c in cmds.items() if p in pids}), \
                mock.patch.object(agents.procs, "terminate_tree", side_effect=lambda pid, *a, **k: killed.append(pid)):
            self.assertEqual(self.crew.reap_orphans(), 1)
        self.assertEqual(killed, [11])  # not the unrelated 22, not the dead 33
        self.assertFalse(self.crew.pid_file.exists())
        self.assertTrue(any(e["text"] == "reaped 1 orphaned agent process" for e in self.crew.feed))

    def test_reap_leaves_alone_when_another_crew_owns_the_file(self):
        self.crew.pid_file.parent.mkdir(parents=True, exist_ok=True)
        self.crew.pid_file.write_text(json.dumps({"owner": 7, "procs": [{"pid": 11, "agent": "builder"}]}), encoding="utf-8")
        cmds = {7: "python C:\\code\\claude-crew\\server.py --project x", 11: CLAUDE_CMD.replace(KEY, self.crew.key)}
        with mock.patch.object(agents.procs, "proc_cmdlines", side_effect=lambda pids: {p: c for p, c in cmds.items() if p in pids}), \
                mock.patch.object(agents.procs, "terminate_tree") as term:
            self.assertEqual(self.crew.reap_orphans(), 0)
        term.assert_not_called()

    def test_reap_survives_missing_or_garbled_files(self):
        self.assertEqual(self.crew.reap_orphans(), 0)
        self.crew.pid_file.parent.mkdir(parents=True, exist_ok=True)
        self.crew.pid_file.write_text("{nope", encoding="utf-8")
        self.assertEqual(self.crew.reap_orphans(), 0)
        self.crew.pid_file.write_text("[1, 2]", encoding="utf-8")
        self.assertEqual(self.crew.reap_orphans(), 0)


def reap(p):
    """Test cleanup: end a process this test started and collect it."""
    if p.poll() is None:
        p.kill()
    p.wait(timeout=10)
    for f in (p.stdin, p.stdout):
        try:
            if f:
                f.close()
        except (OSError, ValueError):
            pass


class OwnProcesses(unittest.TestCase):
    """Real processes, but only ones this test starts."""

    def spawn(self, stdin=False):
        p = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)  # crew-test-sleeper"],
                             stdin=subprocess.PIPE if stdin else subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                             text=True, **({"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP} if server.IS_WIN else {"start_new_session": True}))
        self.addCleanup(reap, p)
        return p

    def test_proc_cmdlines_reads_a_live_process_and_skips_dead_pids(self):
        p = self.spawn()
        for _ in range(40):  # the child may still be starting (exec) on a slow runner
            got = agents.procs.proc_cmdlines([p.pid, 999999])
            if "crew-test-sleeper" in got.get(p.pid, ""):
                break
            time.sleep(0.1)
        self.assertIn("crew-test-sleeper", got.get(p.pid, ""))
        self.assertNotIn(999999, got)

    def test_terminate_tree_ends_the_process_and_reports_success(self):
        p = self.spawn()
        t0 = time.time()
        self.assertIsNone(agents.procs.terminate_tree(p.pid, p))
        self.assertIsNotNone(p.poll())
        self.assertLess(time.time() - t0, 10)

    @unittest.skipIf(server.IS_WIN, "POSIX escalation: SIGTERM, then SIGKILL after the grace period")
    def test_sigterm_ignoring_process_gets_sigkill(self):
        p = subprocess.Popen([sys.executable, "-c", "import signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); print('up', flush=True); time.sleep(60)"],
                             stdout=subprocess.PIPE, text=True, start_new_session=True)
        self.addCleanup(reap, p)
        p.stdout.readline()
        self.assertIsNone(agents.procs.terminate_tree(p.pid, p, grace=0.5))
        self.assertIsNotNone(p.poll())

    @unittest.skipUnless(server.IS_WIN, "Windows: taskkill's exit code is checked")
    def test_failed_taskkill_is_reported(self):
        p = self.spawn()
        failed = subprocess.CompletedProcess(["taskkill"], 1, "", "ERROR: Access is denied.")
        with mock.patch.object(server.subprocess, "run", return_value=failed):
            err = agents.procs.terminate_tree(p.pid, p)
        self.assertIn("taskkill exited 1", err)
        self.assertIn("Access is denied", err)


class HungStdin(TempCrewCase):
    def test_send_never_blocks_and_a_hung_pipe_ends_the_agent(self):
        p = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"], stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                             text=True, **({"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP} if server.IS_WIN else {"start_new_session": True}))
        self.addCleanup(reap, p)
        a = self.crew.agents["builder"]
        self.crew.procs["builder"] = p  # the process never reads, so a big write fills the pipe and blocks
        self.crew.writers["builder"] = agents.procs.StdinWriter(p)
        t0 = time.time()
        self.assertTrue(self.crew._send(a, "x" * 4_000_000))
        self.assertTrue(self.crew._send(a, "second"))
        self.assertLess(time.time() - t0, 2)  # queued on the writer thread: the caller never waited on the pipe
        wait_for(lambda: self.crew.writers["builder"].stuck(0.3), "the writer to be stuck on the full pipe", 10)
        with mock.patch.object(agents.procs, "STDIN_LIMIT", 0.3):
            self.assertFalse(self.crew._send(a, "third"))
        self.assertIn("has not read its input", a["error"])
        self.assertNotIn("builder", self.crew.procs)
        wait_for(lambda: p.poll() is not None, "the hung process to be ended", 15)

    def test_dispatch_ends_a_hung_agent_and_returns_its_task(self):
        p = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"], stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                             text=True, **({"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP} if server.IS_WIN else {"start_new_session": True}))
        self.addCleanup(reap, p)
        a = self.crew.agents["builder"]
        t = {"id": 1, "title": "T", "details": "", "tag": "feature", "col": "doing", "who": "builder", "pinned": "builder", "created": 0}
        self.crew.tasks.append(t)
        a.update(status="running", task=1)
        self.crew.procs["builder"] = p
        self.crew.writers["builder"] = agents.procs.StdinWriter(p)
        self.crew._send(a, "y" * 4_000_000)
        wait_for(lambda: self.crew.writers["builder"].stuck(0.3), "the writer to be stuck", 10)
        with mock.patch.object(agents.procs, "STDIN_LIMIT", 0.3):
            self.crew.dispatch()
        self.assertEqual((a["status"], t["col"]), ("error", "backlog"))
        wait_for(lambda: p.poll() is not None, "the hung process to be ended", 15)


if __name__ == "__main__":
    unittest.main()
