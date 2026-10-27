"""Insights: the day-by-day history behind the Insights tab (workflows/insights.py)."""
import json
import unittest

import server
import workflows.insights as ins
from tests.helpers import TempCrewCase


class InsightsTest(TempCrewCase):
    def today(self):
        return self.crew.hist["days"][ins.day_of()]

    def test_tokens_and_cost_count_per_day_and_agent(self):
        c = self.crew
        a = c.agents["builder"]
        with c.lock:
            c._count_tokens(a, {"input_tokens": 100, "cache_read_input_tokens": 50, "output_tokens": 10})
            c._charge(None, 0.25)
        d = self.today()
        self.assertEqual((d["tok_in"], d["tok_cache"], d["tok_out"]), (100, 50, 10))
        self.assertEqual(d["by"], {"builder": 160})
        pub = ins.public(c, 7)
        self.assertEqual(len(pub["days"]), 7)
        self.assertEqual(pub["days"][-1]["tokens"], 160)
        self.assertEqual(pub["totals"]["cost"], 0.25)
        self.assertEqual(pub["by_agent"], [("builder", 160)])

    def test_a_pr_counts_once_and_its_merge_once(self):
        c = self.crew
        url = "https://github.com/o/r/pull/7"
        with c.lock:
            ins.pr(c, url, raised=True)
            ins.pr(c, url + "/", raised=True)  # cited again: same PR
            ins.pr_state(c, url, "OPEN")
            ins.pr_state(c, url, "MERGED")
            ins.pr_state(c, url, "MERGED")
        d = self.today()
        self.assertEqual(d["prs_opened"], 1)
        self.assertEqual(d["prs_merged"], 1)
        self.assertEqual(d["prs_merged_raised"], 1)

    def test_a_pr_the_brief_handed_over_is_not_raised(self):
        c = self.crew
        url = "https://github.com/o/r/pull/9"
        with c.lock:
            ins.pr(c, url, raised=False)
            ins.pr_state(c, url, "MERGED")
        d = self.today()
        self.assertNotIn("prs_opened", d)
        self.assertEqual(d["prs_merged"], 1)

    def test_history_survives_a_restart(self):
        c = self.crew
        with c.lock:
            ins.bump(c, "watches", 2)
            c._save()
        again = server.Crew(self.project, c.config, ["claude-not-used"])
        self.assertEqual(again.hist["days"][ins.day_of()]["watches"], 2)

    def test_an_old_state_file_is_backfilled_from_tasks_once(self):
        c = self.crew
        t0 = server.now() - 3 * 86400
        with c.lock:
            c.tasks.append({"id": 1, "title": "x", "col": "done", "who": "builder", "created": t0, "started": t0,
                            "finished": t0 + 60, "tokens": 5000, "cost": 0.5, "pr": "https://github.com/o/r/pull/1",
                            "details": ""})
            c._save()
        data = json.loads(c.state_file.read_text(encoding="utf-8"))
        data.pop("insights")
        c.state_file.write_text(json.dumps(data), encoding="utf-8")
        again = server.Crew(self.project, c.config, ["claude-not-used"])
        d = again.hist["days"][ins.day_of(t0 + 60)]
        self.assertEqual((d["tok_in"], d["tasks_done"], d["prs_opened"]), (5000, 1, 1))
        self.assertTrue(again.hist.get("backfilled"))
        self.assertNotIn("prs_opened", again.hist["days"].get(ins.day_of(), {}))  # reading links back is not a new PR

    def test_old_days_are_pruned(self):
        c = self.crew
        with c.lock:
            c.hist["days"]["2000-01-01"] = {"tok_in": 1}
            ins.prune(c)
        self.assertNotIn("2000-01-01", c.hist["days"])


if __name__ == "__main__":
    unittest.main()
