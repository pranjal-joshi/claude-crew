import unittest

from tests.helpers import TempCrewCase
from workflows.runs import groups


class ParallelSteps(TempCrewCase):
    """Steps marked "par" start together with the step before them; the next step waits until every one of them is done."""

    def setUp(self):
        super().setUp()
        self.n = list(self.crew.agents)
        self.w = {"id": "wp", "name": "Incident", "steps": [
            {"agent": self.n[0], "title": "Triage: {input}", "gate": "auto"},
            {"agent": self.n[1], "title": "Telemetry: {input}", "gate": "auto", "par": True},
            {"agent": self.n[2], "title": "Plan: {input}", "gate": "approve"}]}

    def finish(self, t, summary):
        t.update(summary=summary, col="done")
        self.crew._wf_event(t, "done", None)

    def test_groups(self):
        self.assertEqual(groups(self.w["steps"]), [[0, 1], [2]])
        self.assertEqual(groups([{"par": True}, {}, {"par": True}, {"par": True}]), [[0], [1, 2, 3]])

    def test_parallel_steps_start_together_and_the_next_waits_for_both(self):
        with self.crew.lock:
            run = self.crew._start_run(self.w, "Login is slow")
            triage, telemetry = self.crew.tasks[-2:]
            self.assertEqual([triage["who"], telemetry["who"]], self.n[:2])
            self.assertEqual(run["tasks"][:2], [triage["id"], telemetry["id"]])
            self.assertIn("at the same time", triage["details"])
            self.finish(telemetry, "TELEMETRY: p95 went from 200 ms to 4 s at 09:10")
            self.assertIsNone(run["tasks"][2])  # triage is still going
            self.assertIn("waiting for " + self.n[0], run["note"])
            self.finish(triage, "TRIAGE: started after the 09:00 deploy")
        plan = self.crew.tasks[-1]
        self.assertEqual(plan["who"], self.n[2])
        self.assertEqual(run["step"], 2)
        self.assertIn("TELEMETRY: p95", plan["details"])
        self.assertIn("TRIAGE: started", plan["details"])

    def test_a_finished_run_needs_every_parallel_step(self):
        w = dict(self.w, steps=self.w["steps"][:2])
        with self.crew.lock:
            run = self.crew._start_run(w, "x")
            a, b = self.crew.tasks[-2:]
            self.finish(a, "one")
            self.assertEqual(run["status"], "running")
            self.finish(b, "two")
        self.assertEqual(run["status"], "done")

    def test_prev_summary_joins_every_parallel_result(self):
        w = dict(self.w, steps=self.w["steps"][:2] + [dict(self.w["steps"][2], title="Plan from {prev_who}", details="{prev_summary}")])
        with self.crew.lock:
            self.crew._start_run(w, "x")
            a, b = self.crew.tasks[-2:]
            self.finish(a, "AAA")
            self.finish(b, "BBB")
        plan = self.crew.tasks[-1]
        self.assertEqual(plan["title"], f"Plan from {self.n[0]}, {self.n[1]}")
        self.assertTrue(plan["details"].startswith(f"{self.n[0]}:\nAAA\n\n{self.n[1]}:\nBBB"))

    def test_clean_keeps_par_but_not_on_the_first_step(self):
        p = {"name": "P", "steps": [dict(s) for s in self.w["steps"]]}
        p["steps"][0]["par"] = True
        err, cl = self.crew._clean_workflow(p)
        self.assertIsNone(err)
        self.assertEqual([s.get("par", False) for s in cl["steps"]], [False, True, False])

    def test_sequential_workflows_still_run_one_step_at_a_time(self):
        w = dict(self.w, steps=[dict(s, par=False) for s in self.w["steps"]])
        with self.crew.lock:
            run = self.crew._start_run(w, "x")
            self.assertEqual(sum(1 for t in self.crew.tasks if t.get("run")), 1)
            self.finish(self.crew.tasks[-1], "one")
        self.assertEqual(self.crew.tasks[-1]["run"]["step"], 1)
        self.assertEqual(run["step"], 1)


if __name__ == "__main__":
    unittest.main()
