import unittest

from tests.helpers import TempCrewCase


class RunRestart(TempCrewCase):
    """↻ on the board: a run starts again from the start or from a step. Restarted steps are new tasks (fresh sessions),
    earlier steps keep their results, and the open step's card ends."""

    def setUp(self):
        super().setUp()
        self.crew._kill_proc = lambda name, wait=False: None
        names = list(self.crew.agents)
        self.w = {"id": "wr", "name": "R", "steps": [{"agent": names[0], "title": "One: {input}"},
                                                    {"agent": names[1], "title": "Two: {input}"},
                                                    {"agent": names[2], "title": "Three: {input}", "par": True},
                                                    {"agent": names[0], "title": "Four: {input}"}]}
        self.crew.config["workflows"].append(self.w)
        with self.crew.lock:
            self.run = self.crew._start_run(self.w, "Goal")
        self.finish(0, "first summary")

    def finish(self, i, summary):
        t = self.crew._task(self.run["tasks"][i])
        with self.crew.lock:
            t.update(summary=summary, col="done")
            self.crew._wf_event(t, "done", None)
        return t

    def test_restart_from_a_step_keeps_earlier_results_and_starts_new_tasks(self):
        old = self.crew._task(self.run["tasks"][1])
        old["col"] = "doing"
        r = self.crew.act("run_restart", {"id": self.run["id"], "step": 2})  # 3 runs with 2: the group restarts from 2
        self.assertEqual((r.get("ok"), r.get("step")), (True, 1), r)
        self.assertEqual(old["col"], "done")
        self.assertIn("restarted from step 2", old["decision"]["reason"])
        self.assertNotIn("run", old)
        self.assertEqual(self.run["results"][0]["summary"], "first summary")
        new = self.crew._task(self.run["tasks"][1])
        self.assertNotEqual(new["id"], old["id"])
        self.assertIn("first summary", new["details"])
        self.assertEqual(self.run["status"], "running")
        self.assertIsNotNone(self.run["tasks"][2])  # its parallel mate started with it

    def test_restart_whole_run_after_it_stopped_uses_the_current_steps(self):
        self.crew.act("run_stop", {"id": self.run["id"], "cards": True})
        self.w["steps"][0]["title"] = "Uno: {input}"
        r = self.crew.act("run_restart", {"id": self.run["id"], "step": 0})
        self.assertTrue(r.get("ok"), r)
        self.assertEqual(self.run["results"], [None] * 4)
        self.assertTrue(self.crew._task(self.run["tasks"][0])["title"].startswith("Uno"))
        self.assertEqual(self.run.get("restarts"), 1)

    def test_cannot_skip_a_step_that_never_finished(self):
        self.assertIn("steps 2, 3 never finished: restart from step 2", self.crew.act("run_restart", {"id": self.run["id"], "step": 3})["error"])
        self.assertIn("step: 1 to 4", self.crew.act("run_restart", {"id": self.run["id"], "step": 9})["error"])


    def test_clearing_done_keeps_a_running_workflows_steps(self):
        self.crew.tasks.append({"id": 9100, "title": "Old", "col": "done"})
        step1 = self.run["tasks"][0]
        r = self.crew.act("clear", {"scope": "done"})
        self.assertNotIn("error", r)
        ids = {t["id"] for t in self.crew.tasks}
        self.assertIn(step1, ids)
        self.assertNotIn(9100, ids)
        self.crew.act("run_stop", {"id": self.run["id"], "cards": True})
        self.crew.act("clear", {"scope": "done"})  # once the run ends, its cards clear like any other
        self.assertNotIn(step1, {t["id"] for t in self.crew.tasks})

if __name__ == "__main__":
    unittest.main()
