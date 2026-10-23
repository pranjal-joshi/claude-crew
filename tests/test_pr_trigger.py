import time
import unittest
from unittest import mock

import triggers
from tests.helpers import TempCrewCase
from triggers import pr


def gh_pr(n, head="feature/x", draft=False, title="Fix the login timeout", created=None):
    return {"created": time.time() + 1 if created is None else created, "key": f"https://github.com/o/r/pull/{n}", "number": n, "title": title, "url": f"https://github.com/o/r/pull/{n}",
            "head": head, "base": "main", "draft": draft, "body": "Why: users time out.", "repo": ""}


class PrTrigger(TempCrewCase):
    """A PR you open outside Crew after the trigger exists starts one workflow run (a CI monitor), once. PRs opened
    before it, and Crew's own, never do."""

    def setUp(self):
        super().setUp()
        self.open = []
        p = mock.patch.object(pr, "list_prs", lambda crew, sp: list(self.open))
        p.start()
        self.addCleanup(p.stop)
        self.w = self.crew.config["workflows"][0]["id"]

    def add(self, **spec):
        r = self.crew.act("trigger_add", {"kind": "pr", "spec": {"workflow": self.w} | spec})
        self.assertTrue(r.get("ok"), r)
        return self.crew.triggers[-1]

    def check(self, tr):
        triggers.get("pr").check(self.crew, tr)

    def runs(self):
        return [r for r in self.crew.runs if (r.get("source") or {}).get("pr")]

    def test_only_prs_opened_after_the_trigger_start_once_each(self):
        tr = self.add()
        self.open = [gh_pr(1, created=tr["created"] - 60)]  # opened a minute before the trigger
        self.check(tr)
        self.assertEqual((self.runs(), tr["error"]), ([], None))
        self.open.append(gh_pr(2))
        self.check(tr)
        self.assertEqual([r["source"]["pr"] for r in self.runs()], ["https://github.com/o/r/pull/2"])
        t = self.crew.tasks[-1]
        self.assertTrue(t["title"].endswith("PR #2 Fix the login timeout"), t["title"])  # the step title wraps the goal
        self.assertIn("feature/x → main", t["details"])
        self.assertEqual(t["prs"], ["https://github.com/o/r/pull/2"])
        for _ in range(3):  # the same PR on later checks: nothing new
            self.check(tr)
        self.assertEqual(len(self.runs()), 1)

    def test_crews_own_prs_are_skipped(self):
        self.crew.tasks.append({"id": 900, "title": "x", "col": "done", "pr": "https://github.com/o/r/pull/3"})
        branch = next(a for a in self.crew.agents.values() if a.get("branch"))["branch"] if any(a.get("branch") for a in self.crew.agents.values()) else None
        self.open = [gh_pr(3), gh_pr(4)] + ([gh_pr(5, head=branch)] if branch else [])
        tr = self.add()
        self.check(tr)
        self.assertEqual([r["source"]["pr"] for r in self.runs()], ["https://github.com/o/r/pull/4"])

    def test_drafts_wait_until_ready(self):
        tr = self.add()
        self.open = [gh_pr(6, draft=True)]
        self.check(tr)
        self.assertEqual(self.runs(), [])
        self.open = [gh_pr(6)]
        self.check(tr)
        self.assertEqual(len(self.runs()), 1)

    def test_merged_starts_each_merge_after_the_trigger_once_crews_included(self):
        self.crew.tasks.append({"id": 901, "title": "x", "col": "done", "pr": "https://github.com/o/r/pull/8"})
        tr = self.add(state="merged")
        self.assertEqual((tr["spec"]["state"], tr["name"]), ("merged", "My merged PRs"))
        before = tr["created"] - 60
        self.open = [gh_pr(7, created=before) | {"merged": before},     # merged before the trigger: never
                     gh_pr(8, created=before) | {"merged": time.time() + 1},  # Crew's own, opened long ago, merged now: yes
                     gh_pr(9, draft=True) | {"merged": 0}]               # no merge time: never
        for _ in range(3):
            self.check(tr)
        runs = self.runs()
        self.assertEqual([r["source"]["pr"] for r in runs], ["https://github.com/o/r/pull/8"])
        self.assertTrue(runs[0]["source"]["merged"])
        t = self.crew.tasks[-1]
        self.assertIn("PR #8 merged: Fix the login timeout", t["title"])
        self.assertIn("Merged into main.", t["details"])

    def test_raised_and_merged_specs(self):
        self.assertEqual(pr.pr_spec({})[1]["state"], "raised")
        err, sp = pr.pr_spec({"state": "merged", "drafts": True})
        self.assertEqual((err, sp["state"], sp["drafts"]), (None, "merged", False))  # drafts mean nothing once merged
        self.assertEqual(pr.pr_spec({"state": "closed"})[1]["state"], "raised")

    def test_errors_raise_an_alert(self):
        tr = self.add()
        with mock.patch.object(pr, "list_prs", side_effect=pr.PrError("gh is not signed in: run `gh auth login`")):
            self.check(tr)
        self.assertIn("gh auth login", tr["error"])
        self.assertTrue(any(x["key"] == f"trigger:{tr['id']}" for x in self.crew.alerts))

    def test_spec_and_ui_only(self):
        self.assertEqual(pr.pr_spec({"repo": "nope"})[0][:30], "the repository looks like owne")
        self.assertIn("organization URL", pr.pr_spec({"host": "ado"})[0])
        err, sp = pr.pr_spec({"repo": "o/r", "every": "1"})
        self.assertEqual((err, sp["every"], sp["drafts"]), (None, 2, False))
        self.assertIn("error", self.crew.act("trigger_add", {"kind": "pr", "spec": {"workflow": self.w}}, via="mcp"))


if __name__ == "__main__":
    unittest.main()
