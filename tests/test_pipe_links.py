import unittest

from tests.helpers import TempCrewCase, server


class PipelineLinks(TempCrewCase):
    """Deploy agents follow a pipeline run: the card keeps its link, so the board can open the run instead of the PR."""

    def test_links_in_the_output_are_kept(self):
        t = {"id": 1, "title": "Deployed?"}
        with self.crew.lock:
            self.crew._note_pipes(t, "Run https://dev.azure.com/acme/Proj/_build/results?buildId=41&view=results is queued; "
                                     "the image build was https://github.com/o/r/actions/runs/77")
        self.assertEqual(t["pipes"], ["https://dev.azure.com/acme/Proj/_build/results?buildId=41",
                                      "https://github.com/o/r/actions/runs/77"])

    def test_a_watch_command_names_its_run(self):
        t = {"id": 2, "title": "Deployed?"}
        with self.crew.lock:
            self.crew._note_pipes(t, "az pipelines build show --organization https://dev.azure.com/acme --project 'My Proj' "
                                     "--id 306 --query status -o tsv")
        self.assertEqual(t["pipes"], ["https://dev.azure.com/acme/My%20Proj/_build/results?buildId=306"])

    def test_a_bare_run_id_uses_the_ado_project(self):
        t = {"id": 3, "title": "Deployed?"}
        self.crew.tasks.append({"id": 9, "title": "item", "col": "done", "ado": {"url": "https://dev.azure.com/acme/Proj/_workitems/edit/5"}})
        with self.crew.lock:
            self.crew._note_pipes(t, "az pipelines runs show --id 12")
        self.assertEqual(t["pipes"], ["https://dev.azure.com/acme/Proj/_build/results?buildId=12"])

    def test_a_run_named_in_the_summary(self):
        t = {"id": 5, "title": "Deployed?"}
        self.crew.tasks.append({"id": 9, "title": "item", "col": "done", "ado": {"url": "https://dev.azure.com/acme/Proj/_workitems/edit/5"}})
        with self.crew.lock:
            self.crew._note_pipes(t, "Its deploy run (build 20261023.90, run id 4403) is queued. Build 4410 failed at lint.")
        self.assertEqual(t["pipes"], ["https://dev.azure.com/acme/Proj/_build/results?buildId=4403",
                                      "https://dev.azure.com/acme/Proj/_build/results?buildId=4410"])

    def test_runs_set_aside_in_the_text_are_not_the_cards(self):
        t = {"id": 6, "title": "Deployed?"}
        self.crew.tasks.append({"id": 9, "title": "item", "col": "done", "ado": {"url": "https://dev.azure.com/acme/Proj/_workitems/edit/5"}})
        with self.crew.lock:
            self.crew._note_pipes(t, "- Build 4407 (the merge) is in progress.\n"
                                     "- Build 4411 is a PR build, not master. Ignored.\n"
                                     "- Latest master run is build 4333, which is before the merge, so it does not count.\n"
                                     "Baseline: build 4300 succeeded.")
        self.assertEqual(t["pipes"], ["https://dev.azure.com/acme/Proj/_build/results?buildId=4407"])

    def test_a_card_watching_a_pipeline_keeps_the_watched_run(self):
        # #234: the deploy agent watched build 4407 and its report also named a later master run (412); the card's link
        # (the last one) is the run it watches, and stays so after the watch ends
        self.crew.tasks.append({"id": 9, "title": "item", "col": "done", "ado": {"url": "https://dev.azure.com/acme/Proj/_workitems/edit/5"}})
        t = {"id": 7, "title": "Deployed?", "wait": {"probe": {"cmd": "az pipelines build show --id 4407 --query status -o tsv"}}}
        with self.crew.lock:
            self.crew._note_pipes(t, t["wait"]["probe"]["cmd"])
            self.crew._note_pipes(t, "Build 4407 is running. Build 4412 (a later master commit) is queued.")
            self.assertEqual(t["pipes"], ["https://dev.azure.com/acme/Proj/_build/results?buildId=4407"])
            t.pop("wait")  # the agent decided: the watch is over and its report names both runs
            self.crew._note_pipes(t, "Deployed: build 4407 succeeded. Build 4412 is still queued.")
        self.assertEqual(t["pipes"][-1], "https://dev.azure.com/acme/Proj/_build/results?buildId=4407")

    def test_a_watch_on_a_run_list_takes_no_runs_from_the_text(self):
        # #235: the watch looks for the merge's run by commit and names no run; the report's other runs are not the card's
        self.crew.tasks.append({"id": 9, "title": "item", "col": "done", "ado": {"url": "https://dev.azure.com/acme/Proj/_workitems/edit/5"}})
        t = {"id": 8, "title": "Deployed?", "wait": {"probe": {"cmd": "az pipelines runs list --pipeline-ids 1433 --query x -o tsv"}}}
        with self.crew.lock:
            self.crew._note_pipes(t, "No run for the merge yet. The last master run that finished is build 4333.")
        self.assertNotIn("pipes", t)

    def test_a_gh_watch_still_takes_runs_from_the_text(self):
        # CI-Review watches its PR with gh: the PR's builds named in its report are its runs (Pipeline fix reads them)
        self.crew.tasks.append({"id": 9, "title": "item", "col": "done", "ado": {"url": "https://dev.azure.com/acme/Proj/_workitems/edit/5"}})
        t = {"id": 10, "title": "CI", "wait": {"probe": {"cmd": "gh pr view 3 --json state"}}}
        with self.crew.lock:
            self.crew._note_pipes(t, "Build 4501 failed at lint.")
        self.assertEqual(t["pipes"], ["https://dev.azure.com/acme/Proj/_build/results?buildId=4501"])

    def test_nothing_without_a_project(self):
        old = server.Crew._ado_dflt
        self.addCleanup(setattr, server.Crew, "_ado_dflt", old)
        server.Crew._ado_dflt = {}  # not this computer's `az devops configure` defaults, read once in the background
        t = {"id": 4, "title": "x"}
        with self.crew.lock:
            self.crew._note_pipes(t, "az pipelines runs show --id 12")
        self.assertNotIn("pipes", t)


if __name__ == "__main__":
    unittest.main()
