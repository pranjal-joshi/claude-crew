"""One agent works many cards, and its log and chat mixed them all: a message typed in its drawer landed in whatever
session it last had. Log lines now carry their card, each card keeps its own transcript, and a message about a card
goes to that card's session. A workflow can also go back to a step with changes: that step and the ones after it
resume their earlier sessions and build on their own work, the shipper on the same PR."""
from tests.helpers import TempCrewCase

PR = "https://github.com/o/r/pull/7"


class CardChat(TempCrewCase):
    def setUp(self):
        super().setUp()
        self.crew._start = lambda ag: True
        self.crew._kill_proc = lambda name, wait=False: None
        self.sent = []
        self.crew._send = lambda ag, text, images=None: self.sent.append((ag["key"], ag.get("session"), text)) or True
        self.a = next(iter(self.crew.agents.values()))

    def card(self, tid, **kw):
        t = {"id": tid, "title": f"Card {tid}", "col": "done", "created": tid} | kw
        self.crew.tasks.append(t)
        return t

    def test_log_lines_carry_their_card_and_the_card_keeps_its_own(self):
        t = self.card(1, col="backlog")
        self.crew._begin(self.a, t)
        self.crew.log(self.a, "text", "working on it")
        self.assertEqual(self.a["log"][-1]["task"], 1)
        self.assertEqual([e["text"] for e in t["chat"]][-1], "working on it")
        self.assertEqual(t["chat"][-1]["who"], self.a["name"])
        self.assertNotIn("chat", next(x for x in self.crew.snapshot()["tasks"] if x["id"] == 1))
        self.assertEqual(self.crew.act("card_log", {"task": 1})["log"][-1]["text"], "working on it")

    def test_a_message_about_a_card_goes_to_its_session_and_leaves_the_card_where_it_is(self):
        self.card(1, sessions={self.a["name"]: "s-one"})
        self.card(2, sessions={self.a["name"]: "s-two"})
        self.a["session"] = "s-two"  # it last worked on #2
        r = self.crew.act("tell", {"agent": self.a["name"], "task": 1, "text": "why this approach?"})
        self.assertTrue(r.get("ok", True) and "error" not in r, r)
        self.assertEqual(self.sent[-1][1], "s-one")
        self.assertEqual(self.a["log"][-1]["task"], 1)
        self.assertEqual(self.crew._task(1)["col"], "done")
        with self.crew.lock:  # its answer, then its turn ends: nothing moves, the talk is over
            self.crew._on_event(self.a, {"type": "assistant", "message": {"content": [{"type": "text", "text": "Because…"}]}})
            self.crew._on_event(self.a, {"type": "result", "subtype": "success", "total_cost_usd": 0.01, "result": "Because…"})
        self.assertEqual(self.crew._task(1)["col"], "done")
        self.assertNotIn("talk", self.a)
        self.assertIn("Because…", [e["text"] for e in self.crew._task(1)["chat"]])

    def test_a_plain_message_is_unchanged(self):
        self.a["session"] = "s-two"
        self.crew.act("tell", {"agent": self.a["name"], "text": "hi"})
        self.assertEqual(self.sent[-1][1], "s-two")


class GoBackWithChanges(TempCrewCase):
    def setUp(self):
        super().setUp()
        self.crew._kill_proc = lambda name, wait=False: None
        names = list(self.crew.agents)
        self.fixer, self.shipper = names[0], names[1]
        self.w = {"id": "wb", "name": "Bug fix", "steps": [{"agent": self.fixer, "title": "Fix: {input}"},
                                                          {"agent": self.shipper, "title": "Ship: {input}"},
                                                          {"agent": names[2], "title": "CI: {input}"}]}
        self.crew.config["workflows"].append(self.w)
        with self.crew.lock:
            self.run = self.crew._start_run(self.w, "AB#1 Goal")
        self.finish(0, "fixed it", sessions={self.fixer: "fix-s"}, branch="crew/Fixer", commit="abc123")
        self.finish(1, f"opened {PR}", sessions={self.shipper: "ship-s"}, prs=[PR])

    def finish(self, i, summary, **kw):
        t = self.crew._task(self.run["tasks"][i])
        with self.crew.lock:
            t.update(summary=summary, col="done", **kw)
            self.crew._wf_event(t, "done", None)
        return t

    def test_back_to_the_fix_step_resumes_its_session_with_the_request(self):
        ci = self.crew._task(self.run["tasks"][2])
        r = self.crew.act("run_restart", {"id": self.run["id"], "step": 0, "text": "CI failed: also cover the empty case"})
        self.assertTrue(r.get("ok"), r)
        self.assertEqual(ci["col"], "done")
        self.assertIn("went back to step 1", ci["decision"]["reason"])
        fix = self.crew._task(self.run["tasks"][0])
        self.assertEqual(fix["sessions"], {self.fixer: "fix-s"})
        self.assertIn("also cover the empty case", fix["details"])
        self.assertIn("fixed it", fix["details"])
        self.assertIn("abc123", fix["details"])
        self.assertIn(PR, fix["details"])
        self.assertEqual(self.w["steps"][0]["title"], "Fix: {input}")
        self.finish(0, "added the empty case")
        ship = self.crew._task(self.run["tasks"][1])
        self.assertEqual(ship["sessions"], {self.shipper: "ship-s"})
        self.assertEqual(ship["prs"], [PR])
        self.assertIn("don't open another PR", ship["details"])
        self.assertIn("also cover the empty case", ship["details"])

    def test_the_boss_must_say_what_to_change(self):
        self.assertIn("error", self.crew.act("run_restart", {"id": self.run["id"], "step": 0}, via="mcp"))
        self.assertTrue(self.crew.act("run_restart", {"id": self.run["id"], "step": 0, "text": "add X"}, via="mcp").get("ok"))

    def test_a_plain_restart_still_starts_fresh(self):
        self.crew.act("run_restart", {"id": self.run["id"], "step": 0})
        fix = self.crew._task(self.run["tasks"][0])
        self.assertNotIn("sessions", fix)
        self.assertNotIn("went back", fix["details"])
