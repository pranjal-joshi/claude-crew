from unittest import mock

import server
import triggers
import workflows
from tests.helpers import TempCrewCase

PR = "https://github.com/o/r/pull/812"
mem, owners = workflows.memory, workflows.owners


class Memory(TempCrewCase):
    """#117 and #121 never learned what #122 found out (deploy = the ImageUri stage): agents now share project facts."""

    def remember(self, who="ops", **p):
        return self.crew.act("remember", {"from": who, **p}, via="mcp")

    def test_a_fact_reaches_every_new_task_and_a_changed_one_the_next_turn(self):
        r = self.remember(text="Deploy is confirmed by the ImageUri stage; partiallySucceeded is normal", key="deploy-confirmed-by")
        self.assertTrue(r.get("ok"), r)
        t = {"id": 50, "title": "Deployed", "col": "backlog"}
        first = mem.block(self.crew, t)
        self.assertIn("[deploy-confirmed-by] Deploy is confirmed by the ImageUri stage", first)
        self.assertIn("(ops,", first)
        self.assertEqual(mem.block(self.crew, t), "")  # seen: not repeated every turn
        self.remember(text="Deploy is confirmed by the ImageUri stage only", key="deploy-confirmed-by")
        again = mem.block(self.crew, t)
        self.assertIn("ImageUri stage only", again)
        self.assertEqual(len(self.crew.facts), 1)  # the same key replaced it

    def test_it_goes_into_the_task_message(self):
        self.remember(text="Use the py311 venv for pytest")
        sent = []
        a = self.crew.agents["ops"]
        self.crew._start = lambda ag: True
        self.crew._send = lambda ag, text, images=None: sent.append(text) or True
        t = {"id": 51, "title": "Check", "col": "backlog", "who": "ops", "created": 1}
        self.crew.tasks.append(t)
        self.crew._begin(a, t)
        self.assertIn("Crew memory", sent[-1])
        self.assertIn("[use-the-py311-venv-for-pytest]", sent[-1])

    def test_forget_limits_and_bad_keys(self):
        self.remember(text="x", key="a")
        self.assertTrue(self.remember(key="a").get("forgot"))
        self.assertIn("no fact", self.remember(key="a")["error"])
        self.assertIn("key:", self.remember(text="x", key="Bad Key!")["error"])
        self.assertIn("400", self.remember(text="y" * 401)["error"])
        for i in range(mem.MAX_FACTS):
            self.remember(text=f"fact {i}", key=f"k{i}")
        self.assertIn("memory is full", self.remember(text="one more", key="new")["error"])
        self.assertTrue(self.remember(text="replaced", key="k3").get("replaced"))  # replacing still works when full

    def test_facts_survive_a_restart(self):
        self.crew.act("remember", {"text": "from the board", "key": "mine"})
        self.assertEqual(self.crew.facts[0]["by"], "you")
        self.crew._save()
        crew = server.Crew(self.project, self.crew.config, ["claude-not-used"])
        self.assertEqual([f["key"] for f in crew.facts], ["mine"])


class Owners(TempCrewCase):
    """#107, #108 and #122 were second cards for PRs an open card already owned: news now goes to the owner."""

    def setUp(self):
        super().setUp()
        self.crew._save_config = lambda: None
        self.crew.config["workflows"] = [{"id": "w1", "name": "Bug fix", "description": "", "output": {"kind": "board"}, "steps": [
            {"agent": "ops", "title": "Deployed: {input}", "details": "", "gate": "until", "every": 30, "checks": 96}]}]
        with self.crew.lock:
            run = self.crew._start_run(self.crew.config["workflows"][0], "AB#123 Cache warm-up", "", None)
        self.card = self.crew._task(run["tasks"][0])
        self.card.update(col="backlog", prs=[PR], wait={"checks": 1, "next": 9e9, "probe": {"cmd": "az x", "done": "ok"}})
        self.tr = {"id": "t9", "name": "PR merged by me", "spec": {"state": "merged", "workflow": "w1"}}
        self.pr = {"url": PR, "number": 812, "title": "fix: size the cache", "head": "pj/x", "base": "master",
                   "draft": False, "body": "", "merged": 10, "created": 0}

    def test_a_merged_pr_with_an_owner_wakes_it_instead_of_starting_a_run(self):
        runs = len(self.crew.runs)
        with self.crew.lock:
            said = triggers.pr.Pr.start(self.crew, self.tr, self.pr)
        self.assertEqual(said, f"news for #{self.card['id']}")
        self.assertEqual(len(self.crew.runs), runs)
        self.assertEqual((self.card["wait"]["wake"], self.card["wait"]["next"]), ("news", 0))
        body = workflows.runs.recheck_body(self.card, self.crew._until_step(self.card)) + owners.block(self.card)
        self.assertIn("News about what this card owns woke you", body)
        self.assertIn("PR #812 merged into master", body)
        self.assertNotIn("news", self.card)  # delivered once

    def test_without_an_owner_it_starts_as_before(self):
        self.card["col"] = "done"
        with self.crew.lock:
            said = triggers.pr.Pr.start(self.crew, self.tr, self.pr)
        self.assertIn("Bug fix", said)

    def test_work_items_own_too_and_a_blocked_owner_is_woken(self):
        self.card.update(col="review", prs=[])
        self.card["wait"]["blocked"] = {"reason": "a person must run the stage"}
        with self.crew.lock:
            o = owners.owner(self.crew, {"ab:123"})  # from AB# in its title
            self.assertIs(o, self.card)
            owners.attach(self.crew, o, "Api.Int run 4401 partiallySucceeded, carrying AB#123", "Deploy pipeline")
        self.assertEqual((self.card["col"], self.card["wait"]["wake"]), ("backlog", "news"))
        self.assertNotIn("blocked", self.card["wait"])

    def test_an_alert_linking_an_owned_pr_reaches_its_card(self):
        with self.crew.lock:
            self.crew.raise_alert("PR checks red", "warning", "", "reviewer", PR)
        self.assertIn("alert (warning): PR checks red", self.card["news"][0]["text"])

    def test_the_pr_trigger_test_view_says_where_the_news_goes(self):
        with mock.patch.object(triggers.pr, "list_prs", return_value=[self.pr | {"key": PR, "merged": 9e9}]):
            r = triggers.pr.Pr().form(self.crew, {"op": "test", "spec": {"host": "github", "state": "merged"}})
        self.assertEqual(r["items"][0]["why"], f"news for #{self.card['id']}")
