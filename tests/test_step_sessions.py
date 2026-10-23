import unittest

from tests.helpers import TempCrewCase


class StepSessions(TempCrewCase):
    """Every workflow step starts on a fresh session, even for a "keep" agent, which gets its own session back after."""

    def setUp(self):
        super().setUp()
        self.crew._kill_proc = lambda name: None
        self.a = next(iter(self.crew.agents.values()))
        self.a.update(context="keep", session="own-session", last_task=None)

    def test_keep_agent_gets_a_fresh_session_for_a_step_and_its_own_back_after(self):
        step = {"id": 50, "title": "Pick", "run": {"id": "r1", "step": 0}}
        self.crew._task_session(self.a, step)
        self.assertIsNone(self.a["session"])
        self.a["session"] = "step-session"
        self.crew._task_session(self.a, {"id": 51, "title": "Chat"})
        self.assertEqual(self.a["session"], "own-session")
        self.assertNotIn("kept", self.a)

    def test_send_back_of_a_step_resumes_that_steps_session(self):
        step = {"id": 50, "title": "Pick", "run": {"id": "r1", "step": 0}, "sessions": {self.a["name"]: "step-session"}}
        self.crew._task_session(self.a, step)
        self.assertEqual(self.a["session"], "step-session")

    def test_fresh_agent_gets_a_new_session_for_every_step(self):
        self.a.update(context="fresh", session="chat-session")
        self.crew._task_session(self.a, {"id": 52, "title": "Pick", "run": {"id": "r2", "step": 0}})
        self.assertIsNone(self.a["session"])


    def test_after_a_restart_a_new_task_does_not_resume_the_last_steps_session(self):
        self.a.update(context="fresh", session="step-session", last_task=None)  # last_task is not saved; the session is
        self.crew._task_session(self.a, {"id": 53, "title": "Bump the SDK"})
        self.assertIsNone(self.a["session"])

    def test_load_gives_a_shared_session_back_to_the_task_that_made_it(self):
        n = self.a["name"]
        self.crew.tasks[:] = [{"id": 1, "title": "Step", "col": "done", "created": 10, "started": 99, "run": {"id": "r1", "step": 0},
                               "sessions": {n: "s1"}},
                              {"id": 2, "title": "Later", "col": "review", "created": 20, "started": 21, "sessions": {n: "s1"}},
                              {"id": 3, "title": "Own", "col": "review", "created": 30, "sessions": {n: "s3"}}]
        self.crew._save()
        crew = type(self.crew)(self.project, self.crew.config, ["claude-not-used"])
        self.assertEqual([t.get("sessions") for t in crew.tasks], [{n: "s1"}, {}, {n: "s3"}])


class LongSummaries(TempCrewCase):
    """A step's whole summary reaches the next step: ADO comment blocks past 3000 characters were being cut off."""

    def test_next_step_gets_the_full_previous_summary(self):
        names = list(self.crew.agents)
        w = {"id": "wl", "name": "L", "steps": [{"agent": names[0], "title": "{input}", "gate": "auto"},
                                              {"agent": names[1], "title": "{input}", "gate": "auto"},
                                              {"agent": names[2], "title": "{input}", "gate": "auto"}]}
        with self.crew.lock:
            self.crew._start_run(w, "Triage")
            first = self.crew.tasks[-1]
            first.update(summary="TAKE 1, SKIP 2. " + "s" * 1000, col="done")
            self.crew._wf_event(first, "done", None)
            second = self.crew.tasks[-1]
            second.update(summary="x" * 8000 + " ADO comment for AB#123: the last block", col="done")
            self.crew._wf_event(second, "done", None)
        third = self.crew.tasks[-1]
        self.assertEqual(third["run"]["step"], 2)
        self.assertIn("ADO comment for AB#123: the last block", third["details"])
        self.assertIn("s" * 1000, third["details"])  # an earlier step's 1000 characters, no longer cut to 400


if __name__ == "__main__":
    unittest.main()
