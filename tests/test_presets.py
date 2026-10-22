"""Built-in agent presets and workflow templates are drop-in modules; the defaults are what they load."""
import unittest

import agents
import workflows
from tests.helpers import server


class Presets(unittest.TestCase):
    def test_presets_load_in_order_and_are_copies(self):
        a = agents.presets.load()
        self.assertEqual([x["name"] for x in a][:3], ["planner", "researcher", "designer"])
        self.assertEqual(len({x["name"] for x in a}), len(a))
        a[0]["tags"].append("x")
        self.assertNotIn("x", agents.presets.load()[0]["tags"])
        self.assertEqual(server.DEFAULT_CONFIG["agents"], agents.presets.load())

    def test_every_preset_is_valid(self):
        for x in agents.presets.load():
            self.assertRegex(x["name"], agents.record.NAME_RE)
            self.assertIn(x["model"], agents.MODELS)
            self.assertIn(x["effort"], agents.EFFORTS)
            self.assertLessEqual(len(x["role"]), agents.record.ROLE_MAX)

    def test_templates_use_preset_agents_and_unique_ids(self):
        names = {x["name"] for x in agents.presets.load()}
        ts = workflows.templates.load()
        self.assertEqual([w["id"] for w in ts], ["w1", "w2", "w3", "w4", "w5", "w6"])
        for w in ts:
            self.assertTrue({s["agent"] for s in w["steps"]} <= names, w["name"])
        self.assertEqual(server.DEFAULT_WORKFLOWS, ts)


if __name__ == "__main__":
    unittest.main()
