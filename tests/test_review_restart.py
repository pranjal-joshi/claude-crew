import unittest
from unittest import mock

import agents
from tests.helpers import TempCrewCase, server


class ReviewAcrossRestart(TempCrewCase):
    """#98: after a restart Fixer came back idle with #96 in Review, took #98, and approving #96 cleared #98 from it.
    With lanes the card keeps the lane holding its changes across a restart, and #98 runs on another lane."""

    def setUp(self):
        super().setUp()
        patcher = mock.patch.object(agents.lanes, "prepare", lambda crew, lane, wait=False: None)  # no worktrees on disk
        patcher.start()
        self.addCleanup(patcher.stop)

    def restarted(self, git=True):
        """The saved state read by a new Crew, in a git project (cards hold lanes only for changes in a worktree)."""
        self.crew._save()
        with mock.patch.object(server, "is_git", lambda p: git):
            return type(self.crew)(self.project, self.crew.config, ["claude-not-used"])

    def test_a_card_with_changes_keeps_its_lane_after_a_restart_and_other_work_goes_on(self):
        n = next(iter(self.crew.agents))
        self.crew.tasks[:] = [{"id": 96, "title": "Fix", "col": "review", "who": n, "created": 1, "finished": 5},
                              {"id": 98, "title": "Lint", "col": "backlog", "who": n, "created": 2}]
        crew = self.restarted()
        self.assertEqual((crew.agents[n]["status"], crew.agents[n]["task"]), ("review", 96))
        begun = []
        crew._begin = lambda a, t: begun.append((a["key"], t["id"])) or a.update(status="running", task=t["id"]) or True
        crew.dispatch()
        self.assertEqual(begun, [(f"{n}.2", 98)])  # #96 waits for you on lane 1; #98 doesn't wait for it

    def test_a_card_without_changes_holds_nothing_after_a_restart(self):
        n = next(iter(self.crew.agents))
        self.crew.tasks[:] = [{"id": 96, "title": "Fix", "col": "review", "who": n, "created": 1, "finished": 5,
                               "diff": {"stat": "", "patch": "", "note": ""}}]
        crew = self.restarted()
        self.assertEqual((crew.agents[n]["status"], crew.agents[n]["task"]), ("idle", None))

    def test_a_card_on_lane_2_comes_back_on_lane_2(self):
        n = next(iter(self.crew.agents))
        self.crew.tasks[:] = [{"id": 96, "title": "Fix", "col": "review", "who": n, "created": 1, "finished": 5, "lane": f"{n}.2"}]
        crew = self.restarted()
        ln = crew._holder(crew._task(96))
        self.assertEqual((ln["key"], ln["status"]), (f"{n}.2", "review"))
        self.assertEqual(crew.agents[n]["status"], "idle")

    def test_outside_git_a_card_holds_nothing(self):
        n = next(iter(self.crew.agents))
        self.crew.tasks[:] = [{"id": 96, "title": "Fix", "col": "review", "who": n, "created": 1, "finished": 5}]
        self.assertEqual(self.restarted(git=False).agents[n]["status"], "idle")

    def test_approving_an_older_card_leaves_the_agents_current_task_alone(self):
        n = next(iter(self.crew.agents))
        a = self.crew.agents[n]
        self.crew.tasks[:] = [{"id": 96, "title": "Fix", "col": "review", "who": n, "created": 1},
                              {"id": 98, "title": "Lint", "col": "doing", "who": n, "created": 2}]
        a.update(status="running", task=98)
        self.assertFalse(self.crew._approve(self.crew._task(96)).get("error"))
        self.assertEqual(self.crew._task(96)["col"], "done")
        self.assertEqual((a["status"], a["task"]), ("running", 98))

    def test_send_back_goes_to_another_lane_while_the_agent_works_on_another_card(self):
        n = next(iter(self.crew.agents))
        self.crew._start = lambda ag: True
        self.crew._send = lambda ag, text, images=None: True
        self.crew.tasks[:] = [{"id": 96, "title": "Fix", "col": "review", "who": n, "created": 1, "sessions": {n: "s-96"}},
                              {"id": 98, "title": "Lint", "col": "doing", "who": n, "created": 2}]
        self.crew.agents[n].update(status="running", task=98)
        self.assertEqual(self.crew.act("reject", {"task": 96, "text": "again"}), {"ok": True})
        ln = self.crew._holder(self.crew._task(96))
        self.assertEqual((ln["key"], ln["session"], ln["status"], self.crew._task(96)["col"]), (f"{n}.2", "s-96", "running", "doing"))
        self.assertEqual(self.crew.agents[n]["task"], 98)  # lane 1 keeps its work


if __name__ == "__main__":
    unittest.main()
