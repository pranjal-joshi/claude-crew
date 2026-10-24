from unittest import mock

import workflows
from tests.helpers import TempCrewCase

aw = workflows.alert_watch
RED = "https://dev.azure.com/acme/My%20Proj/_build/results?buildId=470"
PIPE = {"name": "App.Int", "ref": "14", "branch": "refs/heads/master"}


class AlertWatch(TempCrewCase):
    """An alert about a red pipeline run clears itself once a later run on the same branch succeeds."""

    def raise_(self, title, agent="ops", link="", details=""):
        with self.crew.lock:
            return self.crew.raise_alert(title, "warning", details, agent, link)

    def check(self, last):
        with mock.patch.object(aw, "pipeline", return_value=PIPE), mock.patch.object(aw, "latest", return_value=last):
            with self.crew.lock:
                todo = aw.due(self.crew)
            aw._busy.acquire()
            aw._check(self.crew, todo)

    def test_it_reads_ado_and_github_run_links(self):
        self.assertEqual(aw.run_of(RED), {"kind": "ado", "org": "https://dev.azure.com/acme", "project": "My Proj", "id": 470})
        self.assertEqual(aw.run_of("https://github.com/o/r/actions/runs/99/job/1"), {"kind": "gh", "repo": "o/r", "id": 99})
        self.assertIsNone(aw.run_of("https://github.com/o/r/pull/12"))

    def test_two_agents_reporting_the_same_failed_run_make_one_alert(self):
        a = self.raise_("master red: lint", "ops", RED)
        b = self.raise_("App.Int failing on master", "reviewer", RED)
        self.assertIs(a, b)
        self.assertEqual(a["count"], 2)

    def test_a_green_run_after_the_red_one_resolves_it_and_the_alerts_that_name_it(self):
        a = self.raise_("master red: lint", "ops", RED)
        named = self.raise_("App.Int red on master blocks deploy", "ops")
        other = self.raise_("Expired access token", "ops")
        self.check({"id": 469, "ok": True})  # older than the red run: not a recovery
        self.assertEqual(a["status"], "open")
        a["watch"]["t"] = 0
        self.check({"id": 480, "ok": False})
        self.assertEqual(a["status"], "open")
        a["watch"]["t"] = 0
        self.check({"id": 481, "ok": True})
        self.assertEqual((a["status"], named["status"], other["status"]), ("resolved", "resolved", "open"))
        self.assertIn("green again on master (run 481)", a["note"])

    def test_a_failed_check_is_kept_and_retried(self):
        a = self.raise_("master red", "ops", RED)
        with mock.patch.object(aw, "pipeline", side_effect=RuntimeError("az not signed in")):
            with self.crew.lock:
                todo = aw.due(self.crew)
            aw._busy.acquire()
            aw._check(self.crew, todo)
        self.assertEqual((a["status"], a["watch"]["error"]), ("open", "az not signed in"))
        with self.crew.lock:
            self.assertEqual(aw.due(self.crew), [])  # not again before CHECK_EVERY
