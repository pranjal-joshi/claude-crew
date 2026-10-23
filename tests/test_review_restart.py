import unittest

from tests.helpers import TempCrewCase


class ReviewAcrossRestart(TempCrewCase):
    """#98: after a restart Fixer came back idle with #96 in Review, took #98, and approving #96 cleared #98 from it."""

    def test_an_agent_with_work_in_review_stays_on_it_after_a_restart(self):
        n = next(iter(self.crew.agents))
        self.crew.tasks[:] = [{"id": 96, "title": "Fix", "col": "review", "who": n, "created": 1, "finished": 5},
                              {"id": 98, "title": "Lint", "col": "backlog", "who": n, "created": 2}]
        self.crew._save()
        crew = type(self.crew)(self.project, self.crew.config, ["claude-not-used"])
        self.assertEqual((crew.agents[n]["status"], crew.agents[n]["task"]), ("review", 96))
        begun = []
        crew._begin = lambda a, t: begun.append(t["id"])
        crew.dispatch()
        self.assertEqual(begun, [])  # #98 waits until #96 is approved or sent back

    def test_approving_an_older_card_leaves_the_agents_current_task_alone(self):
        n = next(iter(self.crew.agents))
        a = self.crew.agents[n]
        self.crew.tasks[:] = [{"id": 96, "title": "Fix", "col": "review", "who": n, "created": 1},
                              {"id": 98, "title": "Lint", "col": "doing", "who": n, "created": 2}]
        a.update(status="running", task=98)
        self.assertFalse(self.crew._approve(self.crew._task(96)).get("error"))
        self.assertEqual(self.crew._task(96)["col"], "done")
        self.assertEqual((a["status"], a["task"]), ("running", 98))

    def test_send_back_waits_while_the_agent_works_on_another_card(self):
        n = next(iter(self.crew.agents))
        self.crew.tasks[:] = [{"id": 96, "title": "Fix", "col": "review", "who": n, "created": 1},
                              {"id": 98, "title": "Lint", "col": "doing", "who": n, "created": 2}]
        self.crew.agents[n].update(status="running", task=98)
        r = self.crew.act("reject", {"task": 96, "text": "again"})
        self.assertIn("working on #98", r.get("error", ""))
        self.assertEqual(self.crew._task(96)["col"], "review")


if __name__ == "__main__":
    unittest.main()
