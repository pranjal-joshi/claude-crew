"""Transcript lines: kept whole, sent cut short on each board update, and fetched whole on a click."""
import unittest

from tests.helpers import TempCrewCase, server


class LogView(TempCrewCase):
    def test_a_long_command_is_kept_whole_sent_short_and_fetched_on_a_click(self):
        a = self.crew.agents["builder"]
        cmd = "python - <<'EOF'\n" + "print('x')\n" * 400 + "EOF"
        self.crew._on_event(a, {"type": "assistant", "message": {"id": "m1", "content": [
            {"type": "tool_use", "id": "u1", "name": "Bash", "input": {"command": cmd}}]}})
        e = a["log"][-1]
        self.assertEqual(e["detail"], cmd[:server.LOG_FULL])
        sent = next(x for x in self.crew.snapshot()["agents"] if x["name"] == "builder")["log"][-1]
        self.assertTrue(sent["more"])
        self.assertEqual(len(sent["detail"]), server.LOG_CUT["detail"])
        self.assertEqual(len(a["log"][-1]["detail"]), len(cmd[:server.LOG_FULL]))  # the snapshot didn't cut the kept line
        r = self.crew.act("log_entry", {"agent": "builder", "kind": "tool", "t": e["t"]})
        self.assertEqual(r["detail"], cmd[:server.LOG_FULL])
        self.assertIn("error", self.crew.act("log_entry", {"agent": "builder", "kind": "tool", "t": 1.0}))

    def test_long_briefs_are_cut_but_an_agents_answer_goes_whole(self):
        a = self.crew.agents["builder"]
        self.crew.log(a, "you", "brief " * 1000)
        self.crew.log(a, "text", "answer " * 1000)
        you, text = server.log_view(a["log"][-2:])
        self.assertTrue(you["more"] and len(you["text"]) == server.LOG_CUT["text"])
        self.assertNotIn("more", text)
        self.assertEqual(text["text"], "answer " * 1000)

    def test_the_boss_transcript_too(self):
        self.crew.log(self.crew.boss, "you", "x" * 5000)
        t = self.crew.boss["log"][-1]["t"]
        self.assertEqual(len(self.crew.act("log_entry", {"agent": "boss", "kind": "you", "t": t})["text"]), 5000)


if __name__ == "__main__":
    unittest.main()
