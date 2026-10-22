import unittest

import triggers
from tests.helpers import TempCrewCase
from triggers import azure


class ManyTriggersOneWorkflow(TempCrewCase):
    """Several triggers can start the same workflow; each fires its own runs."""

    def setUp(self):
        super().setUp()
        runs = {42: [{"id": 601, "result": "partiallySucceeded", "finishTime": "2099-01-01T10:00:00Z", "definition": {"name": "Api.Release"}}],
                43: [{"id": 701, "result": "partiallySucceeded", "finishTime": "2099-01-01T10:05:00Z", "definition": {"name": "Web.Release"}}]}

        def fake_az(*args, raw=False, timeout=90):
            a = " ".join(args)
            if a.startswith("pipelines list"):
                return [{"id": 42, "name": "Api.Release"}, {"id": 43, "name": "Web.Release"}]
            if a.startswith("pipelines runs list"):
                return [r for pid, rs in runs.items() if str(pid) in args for r in rs]
            if a.startswith("devops invoke"):
                return []
            raise AssertionError(a)
        old = azure.az
        azure.az = fake_az
        self.addCleanup(setattr, azure, "az", old)
        agent = list(self.crew.agents)[0]
        self.crew.config["workflows"].append({"id": "wq", "name": "Release QA", "steps": [{"agent": agent, "title": "{input}"}]})
        self.wf = "wq"

    def _add(self, name, pipe):
        r = self.crew.act("trigger_add", {"name": name, "kind": "pipeline", "spec": {
            "pipelines": [pipe], "org": "https://dev.azure.com/acme", "project": "Proj", "workflow": self.wf, "result": "partial"}})
        self.assertTrue(r.get("ok"), r)
        tr = self.crew.triggers[-1]
        tr["since"] = 1
        return tr

    def test_two_pipeline_triggers_start_the_same_workflow(self):
        a, b = self._add("Api partial", "Api.Release"), self._add("Web partial", "Web.Release")
        for tr in (a, b):
            triggers.get("pipeline").check(self.crew, tr)
        runs = [r for r in self.crew.runs if r["wf"] == self.wf]
        self.assertEqual(sorted(r["title"] for r in runs), ["Api.Release partially succeeded", "Web.Release partially succeeded"])
        self.assertEqual({r["source"]["trigger"] for r in runs}, {a["id"], b["id"]})


if __name__ == "__main__":
    unittest.main()
