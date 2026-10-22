"""Export and import of workflows and triggers."""
import json

import server
from tests.helpers import TempCrewCase


class Workflows(TempCrewCase):
    def _file(self, ids=()):
        r = self.crew.act("export_workflows", {"ids": list(ids)})
        self.assertTrue(r["ok"])
        return r["file"]

    def test_round_trip_into_a_fresh_name(self):
        f = self._file()
        self.assertEqual((f["crew"], f["version"]), ("workflows", 1))
        for w in self.crew.config["workflows"]:
            w["name"] += " copy"
        r = self.crew.act("import_workflows", {"items": f["items"]})
        self.assertEqual(len(r["added"]), len(f["items"]))
        self.assertEqual(r["skipped"], [])

    def test_skip_vs_update(self):
        item = self._file()["items"][0]
        item["description"] = "changed by import"
        r = self.crew.act("import_workflows", {"items": [item]})
        self.assertEqual((r["added"], r["skipped"][0]["why"]), ([], "already exists"))
        r = self.crew.act("import_workflows", {"items": [item], "existing": "update"})
        self.assertEqual(r["updated"], [item["name"]])
        self.assertTrue(any(w["description"] == "changed by import" for w in self.crew.config["workflows"]))
        self.assertEqual(len({w["id"] for w in self.crew.config["workflows"]}), len(self.crew.config["workflows"]))

    def test_missing_agent_is_allowed_but_reported(self):
        item = {"name": "Needs Ghost", "steps": [{"agent": "Ghost", "title": "t", "gate": "auto"}], "off": True}
        r = self.crew.act("import_workflows", {"items": [item]})
        self.assertEqual(r["added"], ["Needs Ghost"])
        self.assertEqual(r["missing"], [{"name": "Needs Ghost", "agents": ["Ghost"]}])
        w = next(w for w in self.crew.config["workflows"] if w["name"] == "Needs Ghost")
        self.assertTrue(w["off"])

    def test_bad_entries_are_reported_not_fatal(self):
        good = {"name": "Fine", "steps": [{"agent": next(iter(self.crew.agents)), "title": "x"}]}
        bad = [{"name": ""}, {"name": "No steps", "steps": []}, "nope",
               {"name": "Bad out", "steps": good["steps"], "output": {"kind": "webhook", "url": "ftp://x"}}]
        r = self.crew.act("import_workflows", {"items": bad + [good]})
        self.assertEqual(r["added"], ["Fine"])
        self.assertEqual(len(r["skipped"]), 4)
        self.assertIn("error", self.crew.act("import_workflows", {"items": []}))
        self.assertIn("error", self.crew.act("import_workflows", {"items": [good] * 101}))

    def test_chain_output_travels_by_name(self):
        flows = self.crew.config["workflows"]
        steps = [{"agent": next(iter(self.crew.agents)), "title": "x"}]
        self.crew.act("import_workflows", {"items": [
            {"name": "First", "steps": steps, "output": {"kind": "workflow", "workflow": "second"}},
            {"name": "Second", "steps": steps}]})
        first = next(w for w in flows if w["name"] == "First")
        second = next(w for w in flows if w["name"] == "Second")
        self.assertEqual(first["output"]["workflow"], second["id"])
        out = self._file([first["id"]])["items"][0]["output"]
        self.assertEqual(out, {"kind": "workflow", "workflow": "Second"})

    def test_ui_only(self):
        names = {"import_workflows", "import_triggers", "export_workflows", "export_triggers"}
        self.assertTrue(names.isdisjoint(server.MCP_ACTIONS))


class Triggers(TempCrewCase):
    def _add(self, **kw):
        agent = next(iter(self.crew.agents))
        p = {"name": "Nightly", "kind": "schedule", "enabled": True, "spec": {"cron": "0 2 * * *"},
             "task": {"title": "Tidy", "agent": agent}} | kw
        r = self.crew.act("trigger_add", p)
        self.assertTrue(r.get("ok"), r)
        return self.crew.triggers[-1]

    def test_export_has_no_secrets_or_state(self):
        hook = self._add(name="Deploy hook", kind="webhook", spec={})
        token = hook["spec"]["token"]
        hook.update(fired=3, last=1.0, note="n", seen=["a"], since=5, checked={"t": 1}, error="boom")
        hook["_ids"] = [1]
        f = self.crew.act("export_triggers", {"ids": [hook["id"]]})["file"]
        text = json.dumps(f)
        self.assertNotIn(token, text)
        self.assertEqual(set(f["items"][0]), {"name", "kind", "enabled", "spec", "task"})
        for k in ("fired", "last", "note", "seen", "since", "checked", "error", "_ids", "token"):
            self.assertNotIn(f'"{k}"', text)

    def test_round_trip_arrives_off_with_new_token(self):
        hook = self._add(name="Deploy hook", kind="webhook", spec={})
        f = self.crew.act("export_triggers", {})["file"]
        self.crew.act("trigger_delete", {"id": hook["id"]})
        r = self.crew.act("import_triggers", {"items": f["items"]})
        self.assertEqual(r["added"], ["Deploy hook"])
        tr = self.crew.triggers[-1]
        self.assertFalse(tr["enabled"])
        self.assertTrue(tr["spec"]["token"])
        self.assertNotEqual(tr["spec"]["token"], hook["spec"]["token"])
        self.assertEqual((tr["fired"], tr["last"]), (0, None))

    def test_a_token_in_the_file_is_ignored(self):
        item = {"name": "Hook", "kind": "webhook", "spec": {"token": "from-file"}, "task": {"title": "t"}}
        self.crew.act("import_triggers", {"items": [item]})
        self.assertNotEqual(self.crew.triggers[-1]["spec"]["token"], "from-file")

    def test_skip_vs_update_turns_off(self):
        tr = self._add()
        item = {"name": "nightly", "kind": "schedule", "enabled": True, "spec": {"cron": "30 3 * * *"},
                "task": {"title": "New title"}}
        r = self.crew.act("import_triggers", {"items": [item]})
        self.assertEqual(r["skipped"][0]["why"], "already exists")
        self.assertEqual(tr["spec"]["cron"], "0 2 * * *")
        r = self.crew.act("import_triggers", {"items": [item], "existing": "update"})
        self.assertEqual(r["updated"], ["Nightly"])
        self.assertEqual((tr["spec"]["cron"], tr["task"]["title"], tr["enabled"]), ("30 3 * * *", "New title", False))
        self.assertEqual(len(self.crew.triggers), 1)

    def test_workflow_is_resolved_by_name_and_missing_ones_are_skipped(self):
        w = self.crew.config["workflows"][0]
        spec = {"cron": "0 9 * * *"}
        ok = {"name": "Starts flow", "kind": "schedule", "spec": spec, "task": {"title": "Go", "workflow": w["name"]}}
        ghost = {"name": "Starts ghost", "kind": "schedule", "spec": spec, "task": {"title": "Go", "workflow": "No Such Flow"}}
        nobody = {"name": "Needs nobody", "kind": "schedule", "spec": spec, "task": {"title": "Go", "agent": "Nobody"}}
        r = self.crew.act("import_triggers", {"items": [ok, ghost, nobody]})
        self.assertEqual(r["added"], ["Starts flow"])
        self.assertEqual({s["name"] for s in r["skipped"]}, {"Starts ghost", "Needs nobody"})
        self.assertEqual(self.crew.triggers[-1]["task"]["workflow"], w["id"])
        back = self.crew.act("export_triggers", {"ids": [self.crew.triggers[-1]["id"]]})["file"]
        self.assertEqual(back["items"][0]["task"]["workflow"], w["name"])

    def test_bad_entries_are_reported(self):
        agent = next(iter(self.crew.agents))
        items = ["nope", {"name": "x", "kind": "carrier-pigeon"}, {"name": "", "kind": "schedule", "task": {"title": "t"}},
                 {"name": "Bad cron", "kind": "schedule", "spec": {"cron": "nonsense"}, "task": {"title": "t", "agent": agent}}]
        r = self.crew.act("import_triggers", {"items": items})
        self.assertEqual(r["added"], [])
        self.assertEqual(len(r["skipped"]), 4)
        self.assertIn("error", self.crew.act("import_triggers", {"items": []}))
        self.assertIn("error", self.crew.act("import_triggers", {"items": items * 30}))

    def test_ui_only_kinds_are_allowed_from_the_ui(self):
        item = {"name": "Watch cmd", "kind": "poll", "spec": {"command": "echo hi", "every": 5}, "task": {"title": "t"}}
        r = self.crew.act("import_triggers", {"items": [item]})
        self.assertEqual(r["added"], ["Watch cmd"])
        self.assertFalse(self.crew.triggers[-1]["enabled"])
