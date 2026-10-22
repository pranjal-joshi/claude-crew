import unittest

from tests.helpers import TempCrewCase


class UntilGate(TempCrewCase):
    """A ⟳ step keeps asking its agent to check again until it decides: for a PR to merge or a deployment to land."""

    def setUp(self):
        super().setUp()
        self.sent = []
        self.crew._start = lambda ag: True
        self.crew._send = lambda ag, text, images=None: self.sent.append(text) or True
        names = list(self.crew.agents)
        self.ci, self.after = self.crew.agents[names[0]], self.crew.agents[names[1]]
        r = self.crew.act("wf_save", {"name": "Ship", "steps": [
            {"agent": self.ci["name"], "title": "Merge: {input}", "gate": "until", "every": 10, "checks": 3},
            {"agent": self.after["name"], "title": "Close: {input}", "gate": "approve"}]})
        self.assertTrue(r.get("ok"), r)
        self.w = self.crew.config["workflows"][-1]
        with self.crew.lock:
            self.run = self.crew._start_run(self.w, "AB#1 ship it")
        self.t = self.crew.tasks[-1]
        self.crew._begin(self.ci, self.t)

    def _end_turn(self, text):
        self.crew._on_event(self.ci, {"type": "result", "subtype": "success", "result": text, "total_cost_usd": 0.01})

    def _due(self):
        self.t["wait"]["next"] = 0
        self.crew.dispatch()

    def test_the_step_is_saved_with_its_schedule_and_told_how_it_works(self):
        self.assertEqual({k: self.w["steps"][0][k] for k in ("gate", "every", "checks")}, {"gate": "until", "every": 10, "checks": 3})
        self.assertIn("decide", self.t["details"])
        self.assertIn("10 minutes", self.t["details"])

    def test_waits_then_rechecks_in_a_short_turn(self):
        self._end_turn("PR open, CI running.")
        self.assertEqual((self.t["col"], self.t["wait"]["checks"], self.ci["status"]), ("backlog", 1, "idle"))
        self.crew.dispatch()  # not due yet
        self.assertEqual(self.t["col"], "backlog")
        self._due()
        self.assertEqual(self.t["col"], "doing")
        self.assertIn("check again (2 of 3)", self.sent[-1])
        self.assertIn("PR open, CI running.", self.sent[-1])
        self.assertNotIn("Workflow 'Ship'", self.sent[-1])  # not the whole brief again

    def test_decide_continue_moves_the_run_on(self):
        self._end_turn("PR open.")
        self._due()
        self.crew.act("decide", {"from": self.ci["name"], "verdict": "continue", "reason": "merged"}, via="mcp")
        self._end_turn("Merged as abc123.")
        self.assertEqual(self.t["col"], "done")
        nxt = self.crew.tasks[-1]
        self.assertEqual((nxt["who"], nxt["run"]["step"]), (self.after["name"], 1))

    def test_out_of_checks_goes_to_review(self):
        for _ in range(2):
            self._end_turn("still open")
            self._due()
        self._end_turn("still open")
        self.assertEqual(self.t["col"], "review")
        self.assertTrue(self.t["wait"]["over"])
        self.assertEqual(len([x for x in self.crew.tasks if x.get("run")]), 1)

    def test_bad_numbers_are_refused(self):
        r = self.crew.act("wf_save", {"name": "Bad", "steps": [{"agent": self.ci["name"], "gate": "until", "every": "soon"}]})
        self.assertIn("error", r)


if __name__ == "__main__":
    unittest.main()
