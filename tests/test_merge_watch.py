from unittest import mock

import workflows
from tests.helpers import TempCrewCase

mw = workflows.merge_watch
PR = "https://github.com/o/r/pull/812"


class ReadyToMerge(TempCrewCase):
    """A PR card shows Ready to merge once GitHub says it can be merged: Crew checks, a person merges."""

    def setUp(self):
        super().setUp()
        self.t = {"id": 102, "title": "Green and reviewed", "col": "backlog", "who": "reviewer", "created": 1, "prs": [PR]}
        self.crew.tasks.append(self.t)

    def check(self, gh):
        out = {"number": 812, "state": "OPEN", "isDraft": False, "reviewDecision": "APPROVED"} | gh
        run = mock.Mock(returncode=0, stdout=__import__("json").dumps(out), stderr="")
        with mock.patch.object(mw.shutil, "which", return_value="gh"), mock.patch.object(mw.subprocess, "run", return_value=run), \
             mock.patch.object(mw.threading, "Thread", lambda target, args, daemon: type("T", (), {"start": lambda s: target(*args)})()):
            with self.crew.lock:
                mw.tick(self.crew)

    def test_clean_is_ready_and_announced_once(self):
        self.check({"mergeStateStatus": "CLEAN"})
        self.assertTrue(self.t["gh"]["ready"])
        self.assertEqual(self.t["gh"]["url"], PR)
        self.check({"mergeStateStatus": "CLEAN"})  # not due yet: no second call, no second event
        self.t["gh"]["t"] = 0
        events = []
        self.crew.event = lambda kind, text, who=None: events.append(text)
        self.check({"mergeStateStatus": "CLEAN"})
        self.assertEqual(events, [])  # already ready: announced only when it becomes ready

    def test_blocked_draft_or_merged_is_not_ready(self):
        for gh in ({"mergeStateStatus": "BLOCKED", "reviewDecision": "REVIEW_REQUIRED"}, {"mergeStateStatus": "DIRTY"},
                   {"mergeStateStatus": "CLEAN", "isDraft": True}, {"mergeStateStatus": "UNKNOWN", "state": "MERGED"}):
            self.t.pop("gh", None)
            self.check(gh)
            self.assertFalse(self.t["gh"]["ready"], gh)

    def test_deploy_cards_and_finished_cards_are_left_out(self):
        self.t["who"] = "ops"  # tagged deploy: follows the pipeline, not the PR
        self.assertIsNone(mw.pr_of(self.crew, self.t))
        self.t.update(who="reviewer", col="done")
        self.assertIsNone(mw.pr_of(self.crew, self.t))
        self.t.update(col="review", prs=["https://dev.azure.com/a/b/_git/r/pullrequest/9"])
        self.assertIsNone(mw.pr_of(self.crew, self.t))  # Azure Repos: not GitHub

    def test_gh_failing_keeps_what_was_known(self):
        self.check({"mergeStateStatus": "CLEAN"})
        self.t["gh"]["t"] = 0
        bad = mock.Mock(returncode=1, stdout="", stderr="gh auth login required")
        with mock.patch.object(mw.shutil, "which", return_value="gh"), mock.patch.object(mw.subprocess, "run", return_value=bad), \
             mock.patch.object(mw.threading, "Thread", lambda target, args, daemon: type("T", (), {"start": lambda s: target(*args)})()):
            with self.crew.lock:
                mw.tick(self.crew)
        self.assertTrue(self.t["gh"]["ready"])
        self.assertIn("auth", self.t["gh"]["error"])
