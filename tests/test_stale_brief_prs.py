"""A task's brief cites your own earlier PRs as background ("follow-up from the review of PR #2"). Those joined the card,
so Open PR pointed at a merged PR the task never touched. A link only the brief names now joins only while it is open."""
from unittest import mock

import server
import triggers
from tests.helpers import TempCrewCase

OLD, NEW, LIVE = "https://github.com/o/r/pull/2", "https://github.com/o/r/pull/9", "https://github.com/o/r/pull/5"
STATE = {OLD: "MERGED", NEW: "OPEN", LIVE: "OPEN"}
REAL_OWNED = triggers.pr.owned  # before the tests patch it


class StaleBriefPrs(TempCrewCase):
    def setUp(self):
        super().setUp()
        p = mock.patch.object(triggers.pr, "owned", lambda url, branches, cwd: STATE.get(url, False))
        p.start()
        self.addCleanup(p.stop)
        self.crew._bg = lambda fn, *a: fn(*a)
        self.crew._start = lambda ag: True
        self.crew._send = lambda ag, text, images=None: True
        self.a = next(iter(self.crew.agents.values()))

    def begin(self, details):
        self.crew.act("add", {"title": "Move reads", "tag": "bug", "details": details})
        t = self.crew.tasks[-1]
        self.crew._begin(self.a, t)
        return t

    def test_a_merged_pr_in_the_brief_stays_off(self):
        t = self.begin(f"Follow-up flagged in the review of {OLD}. CI on {LIVE} is red.")
        self.assertEqual(t["prs"], [LIVE])  # the open one is what the task works on

    def test_the_agent_naming_it_still_counts(self):
        t = self.begin(f"Follow-up from {OLD}.")
        self.assertNotIn("prs", t)
        self.crew._on_event(self.a, {"type": "result", "subtype": "success", "total_cost_usd": 0.01,
                                     "result": f"Reverted part of {OLD} and opened {NEW}"})
        self.assertCountEqual(t["prs"], [OLD, NEW])

    def test_known_merged_link_is_dropped_on_the_next_read(self):
        self.crew._pr_mine[OLD] = True  # read before Crew knew its state
        t = self.begin(f"See {OLD}.")
        self.assertEqual(t["prs"], [OLD])
        self.crew._pr_closed.add(OLD)
        self.crew._note_prs(t, brief=(t["details"],))
        self.assertNotIn("prs", t)

    def test_restart_drops_brief_only_merged_links(self):
        self.crew.tasks[:] = [{"id": 145, "title": "Build", "col": "doing", "created": 1, "details": f"Review of {OLD}.",
                               "summary": "Working on it.", "prs": [OLD]},
                              {"id": 143, "title": "Ship", "col": "done", "created": 2, "details": f"Earlier fix {OLD}.",
                               "summary": f"Opened {NEW}.", "prs": [OLD, NEW]}]
        self.crew._save()
        with mock.patch.object(server.Crew, "_bg", staticmethod(lambda fn, *a: fn(*a))):
            crew = server.Crew(self.project, self.crew.config, ["claude-not-used"])
        self.assertNotIn("prs", crew._task(145))
        self.assertEqual(crew._task(143)["prs"], [NEW])

    def test_owned_reports_the_state(self):
        done = mock.Mock(returncode=0, stdout='{"author":{"login":"me"},"headRefName":"x","state":"MERGED"}')
        with mock.patch.object(triggers.pr, "gh_me", return_value="me"), mock.patch.object(triggers.pr.shutil, "which", return_value="gh"), \
                mock.patch.object(triggers.pr.subprocess, "run", return_value=done):
            self.assertEqual(REAL_OWNED(OLD, set(), "."), "MERGED")
            done.stdout = '{"author":{"login":"someone"},"headRefName":"x","state":"OPEN"}'
            self.assertFalse(REAL_OWNED(OLD, set(), "."))
