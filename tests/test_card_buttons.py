"""Card buttons: defined in crew.json (card_actions), added by you or the boss, pressed on a board card."""
import json
import unittest
from unittest import mock

from tests.helpers import TempCrewCase, server

import crew_mcp


class CardButtons(TempCrewCase):
    def setUp(self):
        super().setUp()
        self.sent = []
        self.crew._start = lambda ag: True
        self.crew._send = lambda ag, text, images=None: self.sent.append(text) or True
        self.a, self.b = list(self.crew.agents.values())[:2]
        self.crew.act("add", {"title": "Ship login fix", "tag": "bug"})
        self.t = self.crew.tasks[-1]
        self.t.update(col="review", who=self.a["name"], pr="https://github.com/o/r/pull/7", sessions={self.a["name"]: "s-7"})

    def save(self, **kw):
        return self.crew.act("card_button_save", {"label": "Rebase", "do": {"kind": "agent", "text": "/rebase {pr} for {id}"}} | kw)

    def test_save_validates_and_persists(self):
        self.assertIn("error", self.crew.act("card_button_save", {"label": "", "do": {"kind": "agent", "text": "x"}}))
        self.assertIn("error", self.crew.act("card_button_save", {"label": "X", "do": {"kind": "shell", "text": "rm -rf /"}}))
        self.assertIn("error", self.crew.act("card_button_save", {"label": "X", "do": {"kind": "agent", "text": ""}}))
        self.assertIn("error", self.crew.act("card_button_save", {"label": "X", "do": {"kind": "workflow", "workflow": "nope"}}))
        r = self.save(when={"has": "pr", "cols": ["review", "bogus"]})
        self.assertTrue(r["ok"])
        b = self.crew.config["card_actions"][0]
        self.assertEqual((b["id"], b["when"]["cols"], b["when"]["has"]), ("b1", ["review"], "pr"))
        self.assertEqual(json.loads(self.cfg_path.read_text(encoding="utf-8"))["card_actions"][0]["label"], "Rebase")
        r2 = self.save(do={"kind": "agent", "text": "/rebase"})  # the same label replaces it
        self.assertEqual((r2["id"], r2["replaced"], len(self.crew.config["card_actions"])), ("b1", True, 1))
        self.assertIn("card_actions", self.crew.snapshot())

    def test_agent_button_runs_in_the_cards_session_and_comes_back(self):
        bid = self.save(when={"has": "pr"})["id"]
        r = self.crew.act("card_button_run", {"task": self.t["id"], "id": bid})
        self.assertTrue(r["ok"], r)
        self.assertEqual(self.sent[-1], f"/rebase https://github.com/o/r/pull/7 for #{self.t['id']}")
        self.assertEqual((self.a["session"], self.a["status"], self.t["col"], self.t["acting"]), ("s-7", "running", "doing", bid))
        self.assertIn("error", self.crew.act("card_button_run", {"task": self.t["id"], "id": bid}))  # busy, and not on In progress
        self.crew._on_event(self.a, {"type": "result", "subtype": "success", "result": "rebased", "total_cost_usd": 0.01})
        self.assertEqual((self.a["status"], self.t["col"], "acting" in self.t, "reviewing" in self.t), ("idle", "review", False, False))

    def test_conditions(self):
        bid = self.save(when={"has": "no_pr"})["id"]
        self.assertIn("error", self.crew.act("card_button_run", {"task": self.t["id"], "id": bid}))
        self.save(when={"tags": ["nobody-has-this"]})
        self.assertIn("error", self.crew.act("card_button_run", {"task": self.t["id"], "id": bid}))
        self.save(when={"agent": self.a["name"], "cols": ["review"]})
        self.assertTrue(self.crew.act("card_button_run", {"task": self.t["id"], "id": bid})["ok"])

    def test_workflow_button_starts_a_run_on_the_card(self):
        w = self.crew.config["workflows"][0]
        bid = self.crew.act("card_button_save", {"label": "Ship", "do": {"kind": "workflow", "workflow": w["name"]}})["id"]
        self.assertEqual(self.crew.config["card_actions"][0]["do"]["workflow"], w["id"])  # a name is stored as the id
        r = self.crew.act("card_button_run", {"task": self.t["id"], "id": bid})
        run = self.crew.runs[-1]
        self.assertEqual((r["run"], run["title"], run["source"]), (run["id"], "Ship login fix", {"kind": "card", "task": self.t["id"]}))
        self.assertIn("https://github.com/o/r/pull/7", run["details"])

    def test_handoff_button_makes_a_task_for_another_agent(self):
        self.assertIn("error", self.crew.act("card_button_save", {"label": "H", "do": {"kind": "handoff"}}))
        bid = self.crew.act("card_button_save", {"label": "Security check", "do": {"kind": "handoff", "to": self.b["name"], "text": "Check {pr}"}})["id"]
        r = self.crew.act("card_button_run", {"task": self.t["id"], "id": bid})
        nt = self.crew._task(r["task"])
        self.assertEqual((nt["who"], nt["handoff"]["task"], nt["col"]), (self.b["name"], self.t["id"], "backlog"))
        self.assertTrue(nt["details"].startswith("Check https://github.com/o/r/pull/7"))

    def test_delete(self):
        bid = self.save()["id"]
        self.assertIn("error", self.crew.act("card_button_delete", {"id": "b99"}))
        self.assertTrue(self.crew.act("card_button_delete", {"id": "Rebase"})["ok"])  # by label too
        self.assertEqual(self.crew.config["card_actions"], [])
        self.assertIn("error", self.crew.act("card_button_run", {"task": self.t["id"], "id": bid}))

    def test_boss_can_define_but_not_press(self):
        self.assertTrue({"card_button_save", "card_button_delete"} <= server.MCP_ACTIONS)
        self.assertNotIn("card_button_run", server.MCP_ACTIONS)


class BossTools(unittest.TestCase):
    def test_tools_are_boss_only_and_call_the_actions(self):
        names = {t["name"]: t for t in crew_mcp.TOOLS}
        self.assertTrue(names["set_card_button"]["boss"] and names["delete_card_button"]["boss"])
        calls = []
        with mock.patch.object(crew_mcp, "BOSS", True), mock.patch.object(crew_mcp, "call", lambda path, body=None: calls.append((path, body)) or {"ok": True}):
            crew_mcp.run_tool("set_card_button", {"label": "Rebase", "do": {"kind": "agent", "text": "/rebase {pr}"}})
            crew_mcp.run_tool("delete_card_button", {"id": "b1"})
        self.assertEqual([c[0] for c in calls], ["/api/act/card_button_save", "/api/act/card_button_delete"])
        with mock.patch.object(crew_mcp, "BOSS", False):
            self.assertIn("error", crew_mcp.run_tool("set_card_button", {"label": "x"}))


if __name__ == "__main__":
    unittest.main()
