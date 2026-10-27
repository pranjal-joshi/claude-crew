"""Self-checks, automatic retries, budget headroom, trigger coalescing, the state file's schema and backups, the event
log and crew.json history. The last class runs a real (misbehaving) agent process: tests/faulty_claude.py."""
import json
import os
import sys
import time
import unittest
from pathlib import Path
from unittest import mock

from tests.helpers import TempCrewCase, server

health = server.agents.health


class Base(TempCrewCase):
    def setUp(self):
        super().setUp()
        self.a = next(iter(self.crew.agents.values()))
        self.crew._start = lambda ag: True
        self.crew._send = lambda ag, text, images=None: True
        self.crew._kill_proc = lambda name, wait=False: None

    def task(self, title="Do it"):
        self.crew.act("add", {"title": title, "tag": "bug", "agent": self.a["name"]})
        return self.crew.tasks[-1]

    def begin(self, t):
        with self.crew.lock:
            self.assertTrue(self.crew._begin(self.a, t))

    def result(self, ok=True, text="done", cost=0.1):
        with self.crew.lock:
            self.crew._on_event(self.a, {"type": "result", "subtype": "success" if ok else "error_during_execution",
                                         "is_error": not ok, "result": text, "total_cost_usd": cost})

    def check_later(self):
        """Run the health check twice, REPAIR_AFTER apart."""
        with self.crew.lock:
            first = health.check(self.crew)
            for k in self.crew._suspects:
                self.crew._suspects[k] -= health.REPAIR_AFTER + 1
            return first, health.check(self.crew)


class HealthCheck(Base):
    def test_running_without_a_process_is_repaired_only_after_the_grace(self):
        t = self.task()
        self.begin(t)  # _start is stubbed: no process exists
        first, later = self.check_later()
        self.assertEqual(first, 0)  # one look is not enough
        self.assertEqual(later, 1)
        self.assertEqual((self.a["status"], self.a["task"], t["col"]), ("error", None, "backlog"))
        self.assertTrue(any(e["kind"] == "repair" for e in self.crew.feed))

    def test_a_running_agent_with_a_process_is_left_alone(self):
        t = self.task()
        self.begin(t)
        with mock.patch.object(self.crew, "_alive", return_value=True):
            self.assertEqual(self.check_later(), (0, 0))
        self.assertEqual((self.a["status"], t["col"]), ("running", "doing"))

    def test_a_state_that_comes_right_is_forgotten(self):
        t = self.task()
        self.begin(t)
        with self.crew.lock:
            health.check(self.crew)
            self.a.update(status="idle", task=None)
            t["col"] = "review"
            health.check(self.crew)
        self.assertEqual(self.crew._suspects, {})

    def test_doing_with_nobody_on_it_goes_back(self):
        t = self.task()
        t.update(col="doing", who=self.a["name"])
        self.check_later()
        self.assertEqual(t["col"], "backlog")

    def test_unknown_status_and_column(self):
        t = self.task()
        t["col"] = "limbo"
        self.a["status"] = "confused"
        with self.crew.lock:
            self.assertEqual(health.check(self.crew), 2)
        self.assertEqual((t["col"], self.a["status"]), ("backlog", "idle"))

    def test_agent_pointing_at_a_deleted_task(self):
        self.a.update(status="review", task=999)
        self.check_later()
        self.assertEqual((self.a["status"], self.a["task"]), ("idle", None))

    def test_dispatch_runs_it(self):
        with mock.patch.object(health, "check", return_value=0) as c:
            self.crew.dispatch()
        c.assert_called_once()


class Retries(Base):
    def test_transient(self):
        for msg in ("API Error: 529 Overloaded", "Request timed out", "claude exited unexpectedly", "ECONNRESET", "429 Too Many Requests"):
            self.assertTrue(health.transient(msg), msg)
        for msg in ("Invalid API key", "error_max_turns", "you are not logged in", ""):
            self.assertFalse(health.transient(msg), msg)

    def test_overloaded_turn_retries_with_backoff_then_alerts(self):
        t = self.task()
        for n, wait in enumerate(health.RETRY_WAITS, 1):
            self.begin(t)
            self.result(False, "API Error: 529 Overloaded")
            self.assertEqual((t["col"], t["who"], t["retries"], self.a["status"]), ("backlog", self.a["name"], n, "error"))
            self.assertAlmostEqual(t["retry_at"], time.time() + wait, delta=5)
            self.crew.dispatch()
            self.assertEqual(t["col"], "backlog")  # not before its wait is over
            t["retry_at"] = self.a["retry"]["at"] = 0
            with self.crew.lock:
                self.assertTrue(health.tick_retries(self.crew))
            self.assertEqual((self.a["status"], self.a["error"]), ("idle", None))
        self.begin(t)
        self.result(False, "API Error: 529 Overloaded")  # out of retries
        self.assertEqual((t["col"], self.a["status"]), ("backlog", "error"))
        self.assertTrue(any(x["key"] == f"retry:{t['id']}" for x in self.crew.alerts))

    def test_dispatch_picks_it_up_again(self):
        t = self.task()
        self.begin(t)
        self.result(False, "Request timed out")
        t["retry_at"] = self.a["retry"]["at"] = 0
        self.crew.dispatch()
        self.assertEqual((t["col"], self.a["status"], self.a["task"]), ("doing", "running", t["id"]))

    def test_success_resets_and_closes_the_alert(self):
        t = self.task()
        t["retries"] = len(health.RETRY_WAITS)
        self.crew.raise_alert("x", key=f"retry:{t['id']}")
        self.begin(t)
        self.result(True)
        self.assertNotIn("retries", t)
        self.assertEqual([x["status"] for x in self.crew.alerts if x["key"] == f"retry:{t['id']}"], ["resolved"])

    def test_a_lasting_error_does_not_retry(self):
        t = self.task()
        self.begin(t)
        self.result(False, "Invalid API key")
        self.assertNotIn("retries", t)
        self.assertNotIn("retry", self.a)
        self.assertEqual(t["col"], "backlog")


class BudgetHeadroom(Base):
    def test_running_agents_count_against_the_rest_of_the_day(self):
        self.crew.config["budget_day_usd"] = 1.0
        self.crew.day_spend = 0.5
        self.crew.turn_costs = [0.2]
        self.assertFalse(self.crew._day_no_room())  # nothing running: always room for one
        others = [a for a in self.crew.agents.values() if a is not self.a]
        others[0]["status"] = "running"
        self.assertFalse(self.crew._day_no_room())  # 0.5 + 2 x 0.2 = 0.9
        others[1]["status"] = "running"
        self.assertTrue(self.crew._day_no_room())   # 0.5 + 3 x 0.2 = 1.1
        with self.crew.lock:
            self.assertFalse(self.crew._begin(self.a, self.task()))

    def test_turns_are_measured(self):
        t = self.task()
        self.begin(t)
        self.result(True, cost=0.25)
        self.assertEqual(self.crew.turn_costs, [0.25])


class TriggerCoalescing(Base):
    def setUp(self):
        super().setUp()
        self.tr = {"id": 77, "name": "Standup", "kind": "schedule", "enabled": True,
                   "task": {"title": "Standup {weekday}", "details": "write it", "tag": "", "agent": "", "workflow": ""}}

    def fire(self, ctx=None):
        with self.crew.lock:
            return self.crew._fire(self.tr, ctx or {})

    def test_the_same_waiting_work_is_not_repeated(self):
        t1, t2 = self.fire(), self.fire()
        self.assertIs(t1, t2)
        self.assertEqual((t1["refired"], self.tr["fired"]), (1, 2))
        self.assertEqual(sum(1 for t in self.crew.tasks if (t.get("source") or {}).get("trigger") == 77), 1)

    def test_started_or_different_work_is_a_new_task(self):
        t1 = self.fire()
        t1["started"] = time.time()
        self.assertIsNot(self.fire(), t1)
        self.tr["task"]["details"] = "{n}"
        a, b = self.fire({"n": "1"}), self.fire({"n": "2"})
        self.assertIsNot(a, b)

    def test_workflow_runs_too(self):
        names = list(self.crew.agents)
        self.crew.config["workflows"].append({"id": "wq", "name": "Q", "steps": [{"agent": names[0], "title": "{input}"}]})
        self.tr["task"]["workflow"] = "wq"
        t1, t2 = self.fire(), self.fire()
        self.assertIs(t1, t2)
        self.assertEqual(sum(1 for r in self.crew.runs if r["wf"] == "wq"), 1)


class StateFile(Base):
    def reload(self):
        return server.Crew(self.project, self.crew.config, ["claude-not-used"])

    def test_schema_is_saved_and_old_files_migrate(self):
        self.task("keep me")
        self.crew._save()
        self.assertEqual(json.loads(self.crew.state_file.read_text(encoding="utf-8"))["schema"], server.STATE_SCHEMA)
        self.assertEqual(server.migrate_state({"tasks": []})["schema"], server.STATE_SCHEMA)

    def test_a_corrupt_file_falls_back_to_the_backup(self):
        self.task("keep me")
        self.crew._save()
        bak = self.crew.state_file.with_name(self.crew.state_file.name + ".bak")
        bak.write_text(self.crew.state_file.read_text(encoding="utf-8"), encoding="utf-8")
        self.crew.state_file.write_text("{not json", encoding="utf-8")
        c = self.reload()
        self.assertEqual([t["title"] for t in c.tasks], ["keep me"])
        self.assertTrue(list(self.crew.state_file.parent.glob("*.corrupt-*")))

    def test_corrupt_with_no_backup_starts_empty(self):
        self.crew.state_file.write_text("{not json", encoding="utf-8")
        self.assertEqual(self.reload().tasks, [])

    def test_a_newer_schema_is_copied_before_it_is_overwritten(self):
        self.task("from the future")
        self.crew._save()
        d = json.loads(self.crew.state_file.read_text(encoding="utf-8"))
        d["schema"] = server.STATE_SCHEMA + 5
        self.crew.state_file.write_text(json.dumps(d), encoding="utf-8")
        c = self.reload()
        self.assertEqual([t["title"] for t in c.tasks], ["from the future"])
        self.assertTrue(self.crew.state_file.with_name(f"{self.crew.state_file.name}.schema-{server.STATE_SCHEMA + 5}").exists())


class EventLogAndConfigHistory(Base):
    def test_events_are_appended_and_roll_over(self):
        self.crew.event("note", "hello")
        lines = self.crew.event_file.read_text(encoding="utf-8").splitlines()
        self.assertEqual(json.loads(lines[-1])["text"], "hello")
        with mock.patch.object(server, "EVENT_LOG_MAX", 10):
            self.crew.event("note", "next")
        self.assertTrue(self.crew.event_file.with_name(self.crew.event_file.name + ".1").exists())
        self.assertEqual(json.loads(self.crew.event_file.read_text(encoding="utf-8"))["text"], "next")

    def test_crew_json_versions_are_kept(self):
        hist = self.crew.work_root / "config-history" / self.crew.key
        self.crew._save_config()
        first = self.cfg_path.read_text(encoding="utf-8")
        self.crew._save_config()  # no change: nothing new kept
        n = len(list(hist.glob("crew-*.json")))
        self.crew.config["permission_mode"] = "auto"
        self.crew._save_config()
        kept = sorted(hist.glob("crew-*.json"))
        self.assertEqual(len(kept), n + 1)
        self.assertEqual(kept[-1].read_text(encoding="utf-8"), first)
        with mock.patch.object(server, "CONFIG_HISTORY", 2):
            for mode in ("acceptEdits", "auto", "acceptEdits"):
                self.crew.config["permission_mode"] = mode
                self.crew._save_config()
        self.assertEqual(len(list(hist.glob("crew-*.json"))), 2)


class FaultyProcess(TempCrewCase):
    """A real agent process that misbehaves: Crew must end up in a state it can recover from."""

    def setUp(self):
        super().setUp()
        self.crew.claude = [sys.executable, str(Path(__file__).with_name("faulty_claude.py"))]
        self.addCleanup(self.crew.shutdown)
        self.a = next(iter(self.crew.agents.values()))
        self.crew.act("add", {"title": "Do it", "tag": "bug", "agent": self.a["name"]})
        self.t = self.crew.tasks[-1]

    def run_fault(self, fault, msg=""):
        with mock.patch.dict(os.environ, {"CREW_FAULT": fault, "CREW_FAULT_MSG": msg}):
            with self.crew.lock:
                self.assertTrue(self.crew._begin(self.a, self.t))
            end = time.time() + 20
            while time.time() < end:
                with self.crew.lock:
                    if self.a["status"] != "running":
                        return
                time.sleep(0.05)
        self.fail(f"agent still running after {fault}")

    def test_exits_before_reading(self):
        self.run_fault("exit1")
        self.assertEqual((self.a["status"], self.a["task"], self.t["col"], self.t["retries"]), ("error", None, "backlog", 1))

    def test_crashes_mid_turn(self):
        self.run_fault("crash")
        self.assertEqual((self.a["status"], self.t["col"], self.t["retries"]), ("error", "backlog", 1))
        self.assertIn("Working on it", [x["text"] for x in self.a["log"]])

    def test_garbage_lines_are_logged_and_the_turn_still_lands(self):
        self.run_fault("garbage")
        # outside git a card in Review has no worktree to keep: the agent is free at once
        self.assertEqual((self.a["status"], self.t["col"], self.t["summary"]), ("idle", "review", "done despite the noise"))
        self.assertTrue(any(x["kind"] == "sys" and "not JSON" in x["text"] for x in self.a["log"]))

    def test_error_results(self):
        self.run_fault("error", "API Error: 529 Overloaded")
        self.assertEqual((self.a["status"], self.t["retries"]), ("error", 1))
        with self.crew.lock:
            self.crew._kill_proc(self.a["name"], wait=True)  # its replacement reads the new CREW_FAULT_MSG
            self.a.update(status="idle", error=None)
            self.t.update(retry_at=0)
        self.run_fault("error", "Invalid API key")  # not passing: no second retry is scheduled
        self.assertEqual((self.a["status"], self.t["retries"], self.t["col"]), ("error", 1, "backlog"))

    def test_a_good_turn(self):
        self.run_fault("ok")
        self.assertEqual((self.a["status"], self.t["col"]), ("idle", "review"))


if __name__ == "__main__":
    unittest.main()
