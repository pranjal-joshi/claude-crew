import time
import unittest

import agents
import workflows
from tests.helpers import server
from triggers import schedule


class PureHelpers(unittest.TestCase):
    def test_version(self):
        self.assertRegex(server.__version__, r"^\d+\.\d+\.\d+")

    def test_clean_tags(self):
        self.assertEqual(agents.record.clean_tags("Plan, triage  PLAN"), ["plan", "triage"])
        self.assertEqual(agents.record.clean_tags(["A_b!", "", "x" * 40]), ["ab", "x" * 20])
        self.assertEqual(agents.record.clean_tags(None), [])
        self.assertEqual(len(agents.record.clean_tags([f"t{i}" for i in range(20)])), 8)

    def test_fill(self):
        self.assertEqual(workflows.fill("Build: {input} {nope}", {"input": "x"}), "Build: x {nope}")

    def test_cron_validation(self):
        self.assertIsNone(schedule.cron_error("*/15 9-17 * * 1-5"))
        self.assertIn("5 fields", schedule.cron_error("* * *"))
        self.assertIsNotNone(schedule.cron_error("99 * * * *"))

    def test_cron_match_and_next(self):
        lt = time.struct_time((2026, 1, 5, 9, 30, 0, 0, 5, -1))  # Monday 09:30
        self.assertTrue(schedule.cron_match("30 9 * * 1", lt))
        self.assertFalse(schedule.cron_match("31 9 * * 1", lt))
        after = time.mktime((2026, 1, 5, 9, 0, 0, 0, 0, -1))
        nxt = schedule.cron_next("30 9 * * *", after)
        self.assertIsNotNone(nxt)
        self.assertGreater(nxt, after)
        self.assertLess(nxt - after, 3600)


if __name__ == "__main__":
    unittest.main()
