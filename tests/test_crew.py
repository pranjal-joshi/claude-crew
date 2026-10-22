import json
import unittest

from tests.helpers import TempCrewCase, server


class Workflows(TempCrewCase):
    def test_clean_workflow_ok(self):
        err, wf = self.crew._clean_workflow({"name": " Ship it ", "steps": [
            {"agent": "builder", "title": "Build", "gate": "auto"}, {"agent": "tester"}]})
        self.assertIsNone(err)
        self.assertEqual(wf["name"], "Ship it")
        self.assertEqual([s["gate"] for s in wf["steps"]], ["auto", "approve"])
        self.assertEqual(wf["output"], {"kind": "board"})

    def test_clean_workflow_rejects(self):
        c = self.crew._clean_workflow
        self.assertIn("name", c({"name": "", "steps": []})[0])
        self.assertIn("agent", c({"name": "x", "steps": [{"agent": "nobody"}]})[0])
        self.assertIn("at least one", c({"name": "x", "steps": []})[0])
        step = [{"agent": "builder"}]
        self.assertIn("inside the project", c({"name": "x", "steps": step, "output": {"kind": "report", "path": "../out"}})[0])
        self.assertIn("http", c({"name": "x", "steps": step, "output": {"kind": "webhook", "url": "ftp://a"}})[0])
        self.assertIsNone(c({"name": "x", "steps": step, "output": {"kind": "webhook", "url": "https://a.example/h"}})[0])


class Actions(TempCrewCase):
    def test_snapshot_has_version(self):
        snap = self.crew.snapshot()
        self.assertEqual(snap["app_version"], server.__version__)
        self.assertEqual(len(snap["agents"]), len(server.DEFAULT_CONFIG["agents"]))

    def test_add_task(self):
        self.assertIn("error", self.crew.act("add", {"title": "  "}))
        self.crew.act("add", {"title": "Fix login", "tag": "bug"})
        self.assertEqual(self.crew.tasks[0]["title"], "Fix login")
        self.assertEqual(self.crew.tasks[0]["col"], "backlog")

    def test_reorder_agents_saves_config(self):
        names = list(self.crew.agents)
        new = list(reversed(names))
        self.crew.act("reorder_agents", {"names": new})
        self.assertEqual(list(self.crew.agents), new)
        saved = json.loads(self.cfg_path.read_text(encoding="utf-8"))
        self.assertEqual([a["name"] for a in saved["agents"]], new)
        self.assertEqual(saved["permission_mode"], "acceptEdits")

    def test_reorder_ignores_unknown_and_keeps_rest(self):
        names = list(self.crew.agents)
        self.crew.act("reorder_agents", {"names": ["ghost", names[-1]]})
        self.assertEqual(list(self.crew.agents)[0], names[-1])
        self.assertEqual(sorted(self.crew.agents), sorted(names))

    def test_clear_chat_idle_agent(self):
        a = self.crew.agents[next(iter(self.crew.agents))]
        a["session"], a["log"], a["ctx"] = "sess-1", [{"k": "x"}], 123
        self.crew.act("clear_chat", {"agent": a["name"]})
        self.assertIsNone(a["session"])
        self.assertEqual(a["ctx"], 0)
        self.assertEqual(a["status"], "idle")
        self.assertTrue(any("chat cleared" in str(m) for m in a["log"]))

    def test_clear_chat_running_agent_refused(self):
        a = self.crew.agents[next(iter(self.crew.agents))]
        a["status"] = "running"
        self.assertIn("error", self.crew.act("clear_chat", {"agent": a["name"]}))

    def test_rename_agent_updates_references(self):
        names = list(self.crew.agents)
        old = names[0]
        self.crew.act("add", {"title": "x", "agent": old})
        self.crew.config["workflows"].append({"id": "w", "name": "w", "steps": [{"agent": old, "title": "s"}]})
        r = self.crew.act("rename_agent", {"agent": old, "name": "Renamed"})
        self.assertNotIn("error", r)
        self.assertEqual(list(self.crew.agents)[0], "Renamed")  # keeps its place in the rail
        self.assertNotIn(old, self.crew.agents)
        self.assertEqual(self.crew.agents["Renamed"]["name"], "Renamed")
        self.assertEqual(self.crew.tasks[-1]["who"], "Renamed")
        self.assertEqual(self.crew.config["workflows"][-1]["steps"][0]["agent"], "Renamed")
        saved = json.loads(self.cfg_path.read_text(encoding="utf-8"))
        self.assertEqual(saved["agents"][0]["name"], "Renamed")

    def test_rename_agent_rejects_bad_names(self):
        a, b = list(self.crew.agents)[:2]
        self.assertIn("error", self.crew.act("rename_agent", {"agent": a, "name": b.upper()}))
        self.assertIn("error", self.crew.act("rename_agent", {"agent": a, "name": "boss"}))
        self.assertIn("error", self.crew.act("rename_agent", {"agent": a, "name": "no spaces"}))
        self.crew.agents[a]["status"] = "running"
        self.assertIn("error", self.crew.act("rename_agent", {"agent": a, "name": "Fine"}))

    def test_agent_icon_and_color_saved(self):
        a = next(iter(self.crew.agents))
        self.assertNotIn("error", self.crew.act("configure", {"agent": a, "icon": "code", "color": "#3e63dd"}))
        saved = json.loads(self.cfg_path.read_text(encoding="utf-8"))["agents"][0]
        self.assertEqual((saved["icon"], saved["color"]), ("code", "#3e63dd"))
        self.assertIn("error", self.crew.act("configure", {"agent": a, "color": "blue"}))
        self.assertIn("error", self.crew.act("configure", {"agent": a, "icon": "<svg>"}))
        self.crew.act("configure", {"agent": a, "icon": "", "color": ""})
        self.assertNotIn("icon", json.loads(self.cfg_path.read_text(encoding="utf-8"))["agents"][0])

    def test_configure_event_names_the_change_and_skips_no_ops(self):
        a = next(iter(self.crew.agents))
        configs = lambda: [e["text"] for e in self.crew.feed if e["kind"] == "config"]
        self.crew.act("configure", {"agent": a, "role": "Reviews diffs."})
        self.assertEqual(configs()[-1], f"{a} · role updated")
        n = len(configs())
        self.crew.act("configure", {"agent": a, "role": "Reviews diffs."})  # nothing changed: no event, so no toast
        self.assertEqual(len(configs()), n)
        self.crew.act("configure", {"agent": a, "model": "haiku", "effort": "low"})
        self.assertEqual(configs()[-1], f"{a} · haiku, low effort")

    def test_busy_tracks_running_work(self):
        self.assertFalse(self.crew.busy())
        next(iter(self.crew.agents.values()))["status"] = "running"
        self.assertTrue(self.crew.busy())

    def test_keep_awake_off_never_holds(self):
        k = server.KeepAwake("off")
        k.set(True)
        self.assertFalse(k.on)

    def test_state_is_written_under_work_root_only(self):
        self.assertTrue(str(self.crew.state_file).startswith(str(server.WORK_ROOT)))


if __name__ == "__main__":
    unittest.main()


class ImportAgents(TempCrewCase):
    def test_import_adds_skips_and_updates(self):
        first = next(iter(self.crew.agents))
        specs = [{"name": "Docwriter", "role": "Reviews diffs.", "tags": ["review"], "model": "sonnet", "icon": "code",
                  "color": "#3e63dd", "context": "keep"},
                 {"name": first.upper(), "role": "Imported role."},
                 {"name": "bad name!"}, {"name": "x", "model": "gpt-9"}, {"name": "boss"}, "not an object"]
        r = self.crew.act("import_agents", {"agents": specs})
        self.assertEqual(r["added"], ["Docwriter"])
        self.assertEqual(r["updated"], [])
        self.assertEqual({s["why"] for s in r["skipped"]} >= {"already exists", "reserved name", "unknown model or effort"}, True)
        a = self.crew.agents["Docwriter"]
        self.assertEqual((a["model"], a["icon"], a["color"], a["context"], a["tags"]), ("sonnet", "code", "#3e63dd", "keep", ["review"]))
        saved = {x["name"] for x in json.loads(self.cfg_path.read_text(encoding="utf-8"))["agents"]}
        self.assertIn("Docwriter", saved)
        r = self.crew.act("import_agents", {"agents": specs[1:2], "existing": "update"})  # case-insensitive match
        self.assertEqual(r["updated"], [first])
        self.assertEqual(self.crew.agents[first]["role"], "Imported role.")

    def test_import_drops_unsafe_icon_and_color(self):
        self.crew.act("import_agents", {"agents": [{"name": "Odd", "icon": "<svg>", "color": "red;x:y"}]})
        self.assertEqual((self.crew.agents["Odd"]["icon"], self.crew.agents["Odd"]["color"]), (None, None))

    def test_import_rejects_empty_or_huge(self):
        self.assertIn("error", self.crew.act("import_agents", {"agents": []}))
        self.assertIn("error", self.crew.act("import_agents", {"agents": "nope"}))
        self.assertIn("error", self.crew.act("import_agents", {"agents": [{"name": f"a{i}"} for i in range(101)]}))

    def test_import_is_ui_only(self):
        self.assertNotIn("import_agents", server.MCP_ACTIONS)
