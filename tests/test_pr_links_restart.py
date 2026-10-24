from unittest import mock

import server
import triggers
from tests.helpers import TempCrewCase

PR = "https://github.com/o/r/pull/812"


class PrLinksAcrossRestart(TempCrewCase):
    """#107: after a restart a card's PR links are read again in the background; meanwhile the PR trigger saw the PR as
    not Crew's and started a second CI monitor on it."""

    def restarted(self, owned):
        self.crew.tasks[:] = [{"id": 101, "title": "Ship", "col": "done", "prs": [PR], "created": 1}]
        self.crew._save()
        jobs = []
        with mock.patch.object(server.Crew, "_bg", staticmethod(lambda fn, *a: jobs.append((fn, a)))):
            crew = server.Crew(self.project, self.crew.config, ["claude-not-used"])
        self.assertIn(PR.lower(), triggers.pr.crew_prs(crew)[0])  # still Crew's while its owner is read
        with mock.patch.object(triggers.pr, "owned", owned):
            for fn, a in jobs:
                fn(*a)
        return crew

    def test_the_link_counts_as_crews_while_it_is_read_again(self):
        crew = self.restarted(lambda url, b, cwd: True)
        self.assertEqual(crew._task(101)["prs"], [PR])
        self.assertIn(PR.lower(), triggers.pr.crew_prs(crew)[0])

    def test_a_link_whose_owner_cannot_be_read_stays_on_the_card(self):
        def offline(url, b, cwd):
            raise triggers.pr.PrError("gh offline")
        crew = self.restarted(offline)
        self.assertEqual(crew._task(101)["prs"], [PR])
        self.assertIn(PR.lower(), triggers.pr.crew_prs(crew)[0])
