from unittest import mock

import agents
from tests.helpers import TempCrewCase


class WatchWhileInReview(TempCrewCase):
    """#117: Watchtower had a no-change card in Review (#122), so its woken ⟳ deploy watches never ran. With lanes a
    card in Review never holds other work up: without changes it holds no lane, with changes it keeps its own."""

    def setUp(self):
        super().setUp()
        self.crew._save_config = lambda: None
        self.crew.git = True  # a card's changes live in a worktree (no worktrees are made here)
        patcher = mock.patch.object(agents.lanes, "prepare", lambda crew, lane, wait=False: None)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.crew.config["workflows"] = [{"id": "w1", "name": "Deliver", "description": "", "output": {"kind": "board"}, "steps": [
            {"agent": "reviewer", "title": "Ship: {input}", "details": "", "gate": "auto"},
            {"agent": "ops", "title": "Deployed: {input}", "details": "", "gate": "until", "every": 30, "checks": 96}]}]
        with self.crew.lock:
            self.run = self.crew._start_run(self.crew.config["workflows"][0], "Cache warm-up", "", None)
        self.ops = self.crew.agents["ops"]
        self.rev = {"id": 122, "title": "PR merged", "col": "review", "who": "ops", "created": 1, "lane": "ops",
                    "diff": {"stat": "", "patch": ""}}
        self.watch = {"id": 117, "title": "Deployed", "col": "backlog", "who": "ops", "created": 2,
                      "run": {"id": self.run["id"], "step": 1}, "wait": {"checks": 1, "next": 0, "wake": "output changed",
                                                                        "probe": {"cmd": "az x", "done": "ok"}}}
        self.crew.tasks += [self.rev, self.watch]
        self.ops.update(status="review", task=122)
        self.begun = []
        self.crew._begin = lambda a, t: (a["name"] == "ops" and self.begun.append((a["key"], t["id"]))) or a.update(status="running", task=t["id"]) or True

    def test_a_review_card_without_changes_frees_its_lane(self):
        with self.crew.lock:
            self.crew._lane_after_diff(self.rev)
        self.assertEqual((self.ops["status"], self.ops["task"]), ("idle", None))
        self.crew.dispatch()
        self.assertEqual(self.begun, [("ops", 117)])

    def test_a_review_card_with_changes_keeps_its_lane_and_the_work_takes_another(self):
        self.rev["diff"] = {"stat": "1 file", "patch": "+x"}
        with self.crew.lock:
            self.crew._lane_after_diff(self.rev)
        self.assertEqual((self.ops["status"], self.ops["task"]), ("review", 122))
        self.crew.dispatch()
        self.assertEqual(self.begun, [("ops.2", 117)])
        self.assertEqual((self.ops["status"], self.ops["task"]), ("review", 122))

    def test_any_work_runs_meanwhile_not_only_watch_checks(self):
        self.rev["diff"] = {"stat": "1 file", "patch": "+x"}
        self.watch["run"] = None
        self.watch.pop("wait")
        self.crew.dispatch()
        self.assertEqual(self.begun, [("ops.2", 117)])

    def test_a_lane_whose_worktree_is_being_made_starts_on_a_later_tick(self):
        self.rev["diff"] = {"stat": "1 file", "patch": "+x"}
        with mock.patch.object(agents.lanes, "prepare", lambda crew, lane, wait=False: lane.update(preparing=True)):
            self.crew.dispatch()
        self.assertEqual(self.begun, [])
        self.crew.lanes["ops"][0].pop("preparing")  # the worktree is ready
        self.crew.dispatch()
        self.assertEqual(self.begun, [("ops.2", 117)])
