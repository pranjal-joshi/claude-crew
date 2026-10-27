"""Every reply is the whole deliverable: the system prompt says so, chat follow-ups repeat it, and changing it restarts the agent."""
from unittest import mock

import agents
from tests.helpers import TempCrewCase


class Deliverable(TempCrewCase):
    def setUp(self):
        super().setUp()
        self.sent = []
        self.crew._start = lambda a: True
        self.crew._send = lambda a, text, images=None: self.sent.append(text) or True

    def launched_prompt(self, a: dict) -> str:
        """The --append-system-prompt text start() would pass to claude (the process itself is never started)."""
        cmds = []
        with mock.patch.object(agents.launch.subprocess, "Popen", side_effect=lambda cmd, **kw: cmds.append(cmd) or (_ for _ in ()).throw(OSError("test"))), \
                mock.patch.object(agents.launch, "ensure_worktree"):
            agents.launch.start(self.crew, a)
        cmd = cmds[0]
        return cmd[cmd.index("--append-system-prompt") + 1]

    def test_every_preset_has_a_deliverable(self):
        for x in agents.presets.load():
            self.assertTrue(x.get("deliverable"), x["name"])
            self.assertLessEqual(len(x["deliverable"]), agents.record.DELIVERABLE_MAX)

    def test_deliverable_falls_back_to_the_preset_then_generic(self):
        planner = self.crew.agents["planner"]
        self.assertIn("full plan", agents.launch.deliverable(planner))
        self.assertIn("full plan", agents.launch.deliverable(planner | {"name": "Planner", "deliverable": ""}))  # older crew.json
        self.assertEqual(agents.launch.deliverable({"name": "db-migrator"}), agents.launch.GENERIC_DELIVERABLE)
        self.assertEqual(agents.launch.deliverable(planner | {"deliverable": "a haiku"}), "a haiku")

    def test_system_prompt_asks_for_the_whole_deliverable_every_time(self):
        prompt = self.launched_prompt(self.crew.agents["planner"])
        self.assertIn("Your deliverable is the full plan", prompt)
        self.assertIn("never a partial diff", prompt)

    def test_chat_follow_up_carries_the_reminder_but_the_log_keeps_your_words(self):
        a = self.crew.agents["planner"]
        self.crew.act("tell", {"agent": "planner", "text": "first"})
        self.assertEqual(self.sent[-1], "first")  # no session yet: the task brief is the first message
        a["session"] = "s1"
        self.crew.act("tell", {"agent": "planner", "text": "make step 3 smaller"})
        self.assertTrue(self.sent[-1].startswith("make step 3 smaller\n\n(Reply with the full plan"))
        self.assertEqual(a["log"][-1]["text"], "make step 3 smaller")

    def test_no_reminder_for_the_boss_or_slash_commands(self):
        self.crew.boss["session"] = "b1"
        self.crew.act("tell", {"agent": "boss", "text": "status?"})
        self.assertEqual(self.sent[-1], "status?")
        self.assertEqual(agents.launch.follow_up(self.crew.agents["planner"] | {"session": "s1"}, "/compact"), "/compact")

    def test_role_or_deliverable_change_is_part_of_the_launch_key(self):
        a = self.crew.agents["planner"]
        k = agents.launch.launch_key(self.crew, a)
        self.crew.act("configure", {"agent": "planner", "role": "Plans only database work."})
        k2 = agents.launch.launch_key(self.crew, a)
        self.assertNotEqual(k, k2)
        self.crew.act("configure", {"agent": "planner", "deliverable": "the full migration plan"})
        self.assertNotEqual(k2, agents.launch.launch_key(self.crew, a))
        self.assertEqual(a["deliverable"], "the full migration plan")

    def test_deliverable_is_saved_to_crew_json_and_clipped(self):
        r = self.crew.act("add_agent", {"name": "db-migrator", "role": "Writes migrations.", "deliverable": "x" * 900})
        self.assertFalse(r and r.get("error"), r)
        self.assertEqual(len(self.crew.agents["db-migrator"]["deliverable"]), agents.record.DELIVERABLE_MAX)
        row = next(x for x in agents.record.roster(self.crew) if x["name"] == "db-migrator")
        self.assertEqual(row["deliverable"], "x" * agents.record.DELIVERABLE_MAX)
