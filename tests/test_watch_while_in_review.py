from tests.helpers import TempCrewCase


class WatchWhileInReview(TempCrewCase):
    """#117: Watchtower had a no-change card in Review (#122), so its woken ⟳ deploy watches never ran."""

    def setUp(self):
        super().setUp()
        self.crew._save_config = lambda: None
        self.crew.config["workflows"] = [{"id": "w1", "name": "Deliver", "description": "", "output": {"kind": "board"}, "steps": [
            {"agent": "reviewer", "title": "Ship: {input}", "details": "", "gate": "auto"},
            {"agent": "ops", "title": "Deployed: {input}", "details": "", "gate": "until", "every": 30, "checks": 96}]}]
        with self.crew.lock:
            self.run = self.crew._start_run(self.crew.config["workflows"][0], "Cache warm-up", "", None)
        self.ops = self.crew.agents["ops"]
        self.rev = {"id": 122, "title": "PR merged", "col": "review", "who": "ops", "created": 1,
                    "diff": {"stat": "", "patch": ""}}
        self.watch = {"id": 117, "title": "Deployed", "col": "backlog", "who": "ops", "created": 2,
                      "run": {"id": self.run["id"], "step": 1}, "wait": {"checks": 1, "next": 0, "wake": "output changed",
                                                                        "probe": {"cmd": "az x", "done": "ok"}}}
        self.crew.tasks += [self.rev, self.watch]
        self.ops.update(status="review", task=122)
        self.begun = []
        self.crew._begin = lambda a, t: (a["name"] == "ops" and self.begun.append(t["id"])) or a.update(status="running", task=t["id"]) or True

    def test_a_review_card_without_changes_lets_the_watch_run_then_takes_the_agent_back(self):
        self.crew.dispatch()
        self.assertEqual(self.begun, [117])
        self.assertEqual(self.ops["review_hold"], 122)
        self.ops.update(status="idle", task=None)  # the check ends: the card waits on its watch again
        self.watch["wait"].pop("wake")
        self.crew.dispatch()
        self.assertEqual((self.ops["status"], self.ops["task"]), ("review", 122))
        self.assertNotIn("review_hold", self.ops)

    def test_a_review_card_with_changes_keeps_the_agent(self):
        self.rev["diff"] = {"stat": "1 file", "patch": "+x"}
        self.crew.dispatch()
        self.assertEqual(self.begun, [])

    def test_only_watch_checks_run_meanwhile(self):
        self.watch["run"] = None
        self.watch.pop("wait")
        self.crew.dispatch()
        self.assertEqual(self.begun, [])
