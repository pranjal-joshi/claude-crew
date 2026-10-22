import unittest

import agents

from tests.helpers import TempCrewCase


class OnOff(TempCrewCase):
    """Agents and workflows can be turned off: off agents take no new work, off workflows can't run."""

    def test_off_agent_takes_no_new_work(self):
        name = list(self.crew.agents)[0]
        self.assertFalse(self.crew.act("configure", {"agent": name, "off": True}).get("error"))
        self.assertTrue(self.crew.agents[name]["off"])
        saved = next(r for r in agents.record.roster(self.crew) if r["name"] == name)
        self.assertTrue(saved["off"])
        self.crew.act("add", {"title": "Do the thing", "agent": name})
        started = []
        self.crew._begin = lambda a, t: started.append(a["name"])
        self.crew.dispatch()
        self.assertEqual(started, [])
        self.crew.act("configure", {"agent": name, "off": False})
        self.crew.dispatch()
        self.assertEqual(started, [name])

    def test_handoff_to_an_off_agent_is_refused(self):
        a, b = list(self.crew.agents)[:2]
        self.crew.act("configure", {"agent": b, "off": True})
        r = self.crew.act("handoff", {"from": a, "to": b, "title": "Next step"})
        self.assertIn("turned off", r.get("error", ""))

    def test_off_workflow_cannot_run_and_pauses_its_triggers(self):
        agent = list(self.crew.agents)[0]
        self.crew.config["workflows"].append({"id": "wz", "name": "Z", "steps": [{"agent": agent, "title": "{input}"}]})
        self.crew.act("wf_toggle", {"id": "wz", "off": True})
        self.assertIn("turned off", self.crew.act("wf_run", {"id": "wz", "title": "Go"}).get("error", ""))
        tr = {"kind": "schedule", "enabled": True, "spec": {}, "task": {"workflow": "wz"}}
        self.assertTrue(self.crew._target_off(tr))
        self.crew.act("wf_toggle", {"id": "wz", "off": False})
        self.assertNotIn("off", self.crew._workflow("wz"))
        self.assertFalse(self.crew._target_off(tr))
        self.assertFalse(self.crew.act("wf_run", {"id": "wz", "title": "Go"}).get("error"))


class ContextWindow(TempCrewCase):
    def test_window_size_comes_from_claude_code(self):
        a = next(iter(self.crew.agents.values()))
        with self.crew.lock:
            self.crew._on_result(a, {"total_cost_usd": 0, "modelUsage": {"claude-opus-5-5": {"contextWindow": 1000000}}}, "success")
        self.assertEqual(a["ctx_max"], 1000000)


if __name__ == "__main__":
    unittest.main()
