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
