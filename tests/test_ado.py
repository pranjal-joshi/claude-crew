"""ADO tag trigger: WIQL building, one run per item, the types filter, tag write-back, and the UI-only rule.
No real `az` runs: triggers.azure.az is replaced by a fake that records its calls."""
import unittest
from unittest import mock

from tests.helpers import TempCrewCase, server
import triggers
from triggers import ado, azure


class FakeAz:
    """Stands in for triggers.azure.az. `items` are the query results; `fail` lists substrings of commands that should fail."""

    def __init__(self, items=(), fail=()):
        self.items = {i["id"]: i for i in items}
        self.fail = list(fail)
        self.calls = []

    def __call__(self, *args, raw=False, timeout=90):
        self.calls.append(args)
        line = " ".join(args)
        if any(f in line for f in self.fail):
            raise azure.AzError("boom")
        if args[:2] == ("boards", "query"):
            return [{"id": i["id"], "fields": {"System.Title": i["title"], "System.WorkItemType": i.get("type", "Bug"),
                                                "System.State": "New", "System.Tags": i.get("tags", "crew")}}
                    for i in self.items.values()] or None
        if args[:3] == ("boards", "work-item", "show"):
            i = self.items[int(args[args.index("--id") + 1])]
            return {"fields": {"System.Title": i["title"], "System.WorkItemType": i.get("type", "Bug"), "System.State": "New",
                               "System.Tags": i.get("tags", "crew"), "System.Description": i.get("desc", "<div>Hello&nbsp;<b>there</b><br>line2</div>")}}
        return {}

    def updates(self):
        return [c for c in self.calls if c[:3] == ("boards", "work-item", "update")]


def item(i, **kw):
    return {"id": i, "title": f"Item {i}", **kw}


class AdoCase(TempCrewCase):
    SPEC = {"tag": "crew", "org": "https://dev.azure.com/acme", "project": "Proj", "workflow": "w2", "types": [], "every": 5,
            "after": "tag_picked", "comment": True}

    def add(self, **spec):
        body = {"name": "board", "kind": "ado", "spec": self.SPEC | spec, "task": {}}
        res = self.crew.act("trigger_add", body, via="ui")
        self.assertNotIn("error", res, res)
        return self.crew.triggers[-1]

    def check(self, tr, fake):
        with mock.patch.object(azure, "az", fake):
            tr["_running"] = True
            triggers.get("ado").check(self.crew, tr)
        return fake


class WiqlCase(unittest.TestCase):
    def test_builds_the_query(self):
        q = ado.ado_wiql({"tag": "crew", "project": "Proj", "types": ["Bug", "Task"], "after": "tag_picked"})
        self.assertIn("[System.Tags] CONTAINS 'crew'", q)
        self.assertIn("[System.Tags] NOT CONTAINS 'crew-picked'", q)
        self.assertIn("[System.State] NOT IN ('Closed', 'Done', 'Removed')", q)
        self.assertIn("[System.WorkItemType] IN ('Bug', 'Task')", q)
        self.assertIn("[System.TeamProject] = 'Proj'", q)

    def test_escapes_single_quotes(self):
        q = ado.ado_wiql({"tag": "o'brien", "project": "it's", "types": ["a'b"], "after": "tag_picked"})
        self.assertIn("CONTAINS 'o''brien'", q)
        self.assertIn("'o''brien-picked'", q)
        self.assertIn("= 'it''s'", q)
        self.assertIn("IN ('a''b')", q)

    def test_no_exclusion_unless_tag_picked_and_no_empty_filters(self):
        q = ado.ado_wiql({"tag": "crew", "project": "", "types": [], "after": "remove_tag"})
        self.assertNotIn("NOT CONTAINS", q)
        self.assertNotIn("WorkItemType] IN", q)
        self.assertNotIn("TeamProject", q)

    def test_spec_rejects_what_cmd_would_read_as_syntax(self):
        for bad in ("a&b", "a|b", "100%", 'a"b', "a;b"):
            self.assertIsNotNone(ado.ado_spec({"tag": bad})[0], bad)
        self.assertIn("tag", ado.ado_spec({"tag": "  "})[0])

    def test_html_to_text(self):
        self.assertEqual(ado.html_text("<div>Hi&nbsp;<b>you</b><br>next &amp; last</div><p>x</p>"), "Hi you\nnext & last\nx")

    def test_tag_write_back_strings(self):
        f = ado.ado_tags_after
        self.assertEqual(f("bug; crew", "crew", "tag_picked"), "bug; crew; crew-picked")
        self.assertEqual(f("crew; crew-picked", "crew", "tag_picked"), "crew; crew-picked")
        self.assertEqual(f("bug; crew; ui", "crew", "remove_tag"), "bug; ui")
        self.assertEqual(f("Crew", "crew", "remove_tag"), "")
        self.assertEqual(f("crew;ui", "crew", "tag_picked"), "crew; ui; crew-picked")
        self.assertIsNone(f("crew", "crew", "none"))


class AdoTriggerCase(AdoCase):
    def test_validation(self):
        bad = lambda **kw: self.crew.act("trigger_add", {"name": "b", "kind": "ado", "spec": self.SPEC | kw, "task": {}}, via="ui")
        self.assertIn("tag", bad(tag="")["error"])
        self.assertIn("workflow or an agent", bad(workflow="")["error"])
        self.assertIn("no workflow", bad(workflow="nope")["error"])
        self.assertIn("organization", bad(org="acme")["error"])
        tr = self.add(every=-3, types=["Bug"], after="weird")
        self.assertEqual((tr["spec"]["every"], tr["spec"]["after"], tr["spec"]["types"]), (1, "tag_picked", ["Bug"]))
        agent = self.add(workflow="", agent=next(iter(self.crew.agents)))
        self.assertEqual(agent["spec"]["workflow"], "")

    def test_public_next_check(self):
        tr = self.add(every=7)
        pub = lambda: next(t for t in self.crew._public_triggers() if t["id"] == tr["id"])
        self.assertEqual((pub()["next"], pub()["checking"]), (7 * 60, False))  # never checked: due now
        tr["_checked"], tr["_running"] = 1000.0, True
        self.assertEqual((pub()["next"], pub()["checking"]), (1000.0 + 7 * 60, True))
        tr["enabled"] = False
        self.assertIsNone(pub()["next"])
        self.assertNotIn("_checked", pub())

    def test_mcp_callers_are_refused(self):
        body = {"name": "b", "kind": "ado", "spec": self.SPEC, "task": {}}
        for via in ("mcp", "boss"):
            self.assertIn("error", self.crew.act("trigger_add", body, via=via))
        self.assertEqual(self.crew.triggers, [])
        tr = self.add()
        res = self.crew.act("trigger_update", {**body, "id": tr["id"]}, via="mcp")
        self.assertIn("ado", res["error"])

    def test_one_run_per_new_item_and_no_duplicates(self):
        tr = self.add()
        fake = self.check(tr, FakeAz([item(1), item(2)]))
        self.assertEqual([r["title"] for r in self.crew.runs], ["AB#1 Item 1", "AB#2 Item 2"])
        run = self.crew.runs[0]
        self.assertEqual(run["ado"], {"id": 1, "url": "https://dev.azure.com/acme/Proj/_workitems/edit/1"})
        self.assertEqual(run["source"]["name"], "board")
        self.assertIn("Hello there\nline2", run["details"])
        self.assertEqual(self.crew._task(run["tasks"][0])["ado"]["id"], 1)
        self.assertEqual(len(fake.updates()), 2)
        self.assertEqual((tr["picked"], tr["fired"], tr["seen"], tr["error"]), (2, 2, [1, 2], None))
        # the same items again (tag write-back lagging): nothing starts
        self.check(tr, FakeAz([item(1), item(2)]))
        self.assertEqual(len(self.crew.runs), 2)
        self.assertEqual(tr["checked"]["picked"], 0)
        # a new one is the only one picked
        self.check(tr, FakeAz([item(1), item(3)]))
        self.assertEqual([r["ado"]["id"] for r in self.crew.runs], [1, 2, 3])

    def test_live_task_blocks_even_when_unseen(self):
        tr = self.add()
        self.check(tr, FakeAz([item(7)]))
        tr["seen"] = []
        self.check(tr, FakeAz([item(7)]))
        self.assertEqual(len(self.crew.runs), 1)

    def test_a_running_run_does_not_block_new_items(self):
        tr = self.add()
        self.check(tr, FakeAz([item(1)]))
        run = self.crew.runs[0]
        self.crew._task(run["tasks"][0])["col"] = "doing"  # its run is mid-way, like a Feature delivery at step 3
        self.assertEqual(run["status"], "running")
        self.check(tr, FakeAz([item(1), item(2)]))
        self.assertEqual([r["ado"]["id"] for r in self.crew.runs], [1, 2])
        self.assertEqual(tr["checked"]["picked"], 1)

    def test_agent_mode_creates_a_task_with_a_link(self):
        who = next(iter(self.crew.agents))
        tr = self.add(workflow="", agent=who)
        self.check(tr, FakeAz([item(5, type="Bug")]))
        t = self.crew.tasks[-1]
        self.assertEqual((t["title"], t["who"], t["ado"]["id"], t["source"]["ado"]["id"]), ("AB#5 Item 5", who, 5, 5))
        self.assertIn("Bug · New", t["details"])

    def test_query_filters_by_types(self):
        tr = self.add(types=["Bug", "Product Backlog Item"])
        fake = self.check(tr, FakeAz([]))
        wiql = fake.calls[0][fake.calls[0].index("--wiql") + 1]
        self.assertIn("[System.WorkItemType] IN ('Bug', 'Product Backlog Item')", wiql)
        self.assertEqual(fake.calls[0][-4:], ("--organization", "https://dev.azure.com/acme", "--project", "Proj"))

    def test_write_back_modes(self):
        for after, want in (("tag_picked", "System.Tags=ui; crew; crew-picked"), ("remove_tag", "System.Tags=ui"), ("none", None)):
            with self.subTest(after):
                tr = self.add(after=after, comment=False)
                fake = self.check(tr, FakeAz([item(9, tags="ui; crew")]))
                ups = fake.updates()
                if want is None:
                    self.assertEqual(ups, [])
                else:
                    self.assertEqual(len(ups), 1)
                    self.assertEqual(ups[0][ups[0].index("--fields") + 1], want)
                    self.assertNotIn("--discussion", ups[0])
                self.crew.runs.clear()
                self.crew.tasks.clear()
                self.crew.triggers.clear()

    def test_comment_names_the_workflow_and_run(self):
        tr = self.add(after="none")
        fake = self.check(tr, FakeAz([item(4)]))
        up = fake.updates()[0]
        self.assertEqual(up[up.index("--discussion") + 1], "Picked up by Crew: Bug fix, run #1")
        self.assertNotIn("--fields", up)

    def test_write_back_failure_keeps_the_run_and_marks_it_seen(self):
        tr = self.add()
        self.check(tr, FakeAz([item(3)], fail=["work-item update"]))
        self.assertEqual(len(self.crew.runs), 1)
        self.assertEqual(tr["seen"], [3])
        self.assertIn("write-back failed", tr["error"])
        self.assertTrue(any("marking it in Azure DevOps failed" in e["text"] for e in self.crew.feed))
        self.check(tr, FakeAz([item(3)]))
        self.assertEqual(len(self.crew.runs), 1)

    def test_query_error_is_recorded_and_clears_running(self):
        tr = self.add()
        self.check(tr, FakeAz([item(1)], fail=["boards query"]))
        self.assertEqual((tr["error"], tr["_running"], self.crew.runs), ("boom", False, []))

    def test_at_most_ten_per_check(self):
        tr = self.add()
        items = [item(i) for i in range(1, 26)]
        self.check(tr, FakeAz(items))
        self.assertEqual(len(self.crew.runs), 10)
        self.assertEqual(tr["checked"], {"t": tr["checked"]["t"], "found": 25, "picked": 10})
        self.check(tr, FakeAz(items))
        self.assertEqual(len(self.crew.runs), 20)

    def test_seen_is_capped(self):
        tr = self.add()
        tr["seen"] = list(range(1000, 1000 + ado.ADO_SEEN_MAX))
        self.check(tr, FakeAz([item(1)]))
        self.assertEqual(len(tr["seen"]), ado.ADO_SEEN_MAX)
        self.assertEqual(tr["seen"][-1], 1)
        self.assertNotIn(1000, tr["seen"])

    def test_tick_checks_on_the_interval_in_a_thread(self):
        self.add(every=5)
        fake = FakeAz([item(1)])
        done = []
        trig = triggers.get("ado")
        real = trig.check
        with mock.patch.object(azure, "az", fake), mock.patch.object(trig, "check", lambda c, t: (real(c, t), done.append(1))):
            self.crew.tick_triggers()
            for _ in range(200):
                if done:
                    break
                import time
                time.sleep(0.02)
            self.assertTrue(done)
            self.crew.tick_triggers()  # not due again for 5 minutes
        self.assertEqual(len(self.crew.runs), 1)
        self.assertEqual(len([c for c in fake.calls if c[:2] == ("boards", "query")]), 1)


class AdoHelpersCase(AdoCase):
    def test_ado_tasks_tell_the_agent_which_project_to_pass(self):
        base = "https://dev.azure.com/org/My%20Proj/_workitems/edit/"
        with mock.patch.object(server.Crew, "_ado_base", lambda self: base):
            hint = self.crew._ado_hint({"ado": {"id": 5}}, "Task #1: x")
            self.assertIn('organization https://dev.azure.com/org, project "My Proj"', hint)
            self.assertIn("Pass `project`", hint)
            self.assertIn("project", self.crew._ado_hint({}, "Update ADO: AB#123 x"))
            self.assertEqual(self.crew._ado_hint({}, "Fix the login page"), "")
        with mock.patch.object(server.Crew, "_ado_base", lambda self: ""):
            self.assertEqual(self.crew._ado_hint({"ado": {"id": 5}}, "x"), "")

    def test_default_name_when_blank(self):
        self.crew.act("trigger_add", {"name": "", "kind": "ado", "spec": self.SPEC, "task": {}}, via="ui")
        self.assertEqual(self.crew.triggers[-1]["name"], "ADO: crew")

    def test_defaults_are_read_from_az_configure(self):
        text = "Use git alias = No\nOrganization = https://dev.azure.com/acme\nProject = My Proj\n"
        with mock.patch.object(azure, "az", lambda *a, raw=False, timeout=90: text):
            self.assertEqual(azure.ado_defaults(), {"org": "https://dev.azure.com/acme", "project": "My Proj"})
        with mock.patch.object(azure, "az", lambda *a, raw=False, timeout=90: "Use git alias = No\n"):
            self.assertEqual(azure.ado_defaults(), {})

    def test_test_button_is_read_only_and_shows_errors(self):
        fake = FakeAz([item(1), item(2)])
        with mock.patch.object(azure, "az", fake):
            res = triggers.get("ado").form(self.crew, {"op": "test", "spec": self.SPEC})
        self.assertEqual((res["total"], [i["id"] for i in res["items"]]), (2, [1, 2]))
        self.assertEqual(fake.updates(), [])
        self.assertEqual([c[:2] for c in fake.calls], [("boards", "query")])
        self.assertEqual(self.crew.runs, [])
        with mock.patch.object(azure, "az", FakeAz(fail=["boards query"])):
            self.assertEqual(triggers.get("ado").form(self.crew, {"op": "test", "spec": self.SPEC}), {"error": "boom"})
        self.assertIn("tag", triggers.get("ado").form(self.crew, {"op": "test", "spec": {"tag": ""}})["error"])

    def test_az_error_hints(self):
        self.assertIn("az login", azure.az_hint("ERROR: Please run 'az login' to setup account."))
        self.assertIn("az extension add --name azure-devops", azure.az_hint("ERROR: 'boards' is misspelled or not recognized"))
        self.assertEqual(azure.az_hint("a\nlast line"), "last line")

    def test_missing_az_and_unsafe_args(self):
        with mock.patch.object(server.shutil, "which", return_value=None):
            with self.assertRaises(azure.AzError) as cm:
                azure.az("boards", "query")
        self.assertIn("az not found", str(cm.exception))
        with mock.patch.object(server.shutil, "which", return_value="az"), mock.patch.object(server.subprocess, "run") as run:
            with self.assertRaises(azure.AzError):
                azure.az("boards", "x & calc")
            run.assert_not_called()


if __name__ == "__main__":
    unittest.main()


class AssignedCase(AdoCase):
    """match "assigned": items assigned to the az user; no tag, no tag write-back; existing ones are a baseline by default."""

    def test_query_and_spec(self):
        err, sp = ado.ado_spec({"match": "assigned", "tag": "ignored", "after": "tag_picked"})
        self.assertIsNone(err)
        self.assertEqual((sp["tag"], sp["after"], sp["existing"]), ("", "none", "skip"))
        q = ado.ado_wiql(sp | {"project": "Proj", "types": ["Bug"]})
        self.assertIn("[System.AssignedTo] = @Me", q)
        self.assertNotIn("System.Tags] CONTAINS", q)
        self.assertIn("[System.WorkItemType] IN ('Bug')", q)

    def test_existing_items_are_the_baseline_then_new_ones_start(self):
        tr = self.add(match="assigned", tag="")
        self.assertEqual(tr["name"], "board")
        self.check(tr, FakeAz([item(1), item(2)]))
        self.assertEqual((self.crew.runs, sorted(tr["seen"]), tr["baselined"]), ([], [1, 2], True))
        fake = self.check(tr, FakeAz([item(1), item(2), item(3)]))
        self.assertEqual([r["ado"]["id"] for r in self.crew.runs], [3])
        ups = fake.updates()  # a comment only: tags are left alone
        self.assertEqual(len(ups), 1)
        self.assertNotIn("--fields", ups[0])

    def test_start_existing(self):
        tr = self.add(match="assigned", tag="", existing="start")
        self.check(tr, FakeAz([item(1), item(2)]))
        self.assertEqual(sorted(r["ado"]["id"] for r in self.crew.runs), [1, 2])

    def test_default_name_and_test_button(self):
        self.crew.act("trigger_add", {"name": "", "kind": "ado", "spec": self.SPEC | {"match": "assigned", "tag": ""}, "task": {}}, via="ui")
        self.assertEqual(self.crew.triggers[-1]["name"], "ADO: assigned to me")
        with mock.patch.object(azure, "az", FakeAz([item(1)])):
            res = triggers.get("ado").form(self.crew, {"op": "test", "spec": {"match": "assigned"}})
        self.assertEqual((res["total"], res["baseline"]), (1, True))

    def test_widening_the_filter_takes_a_new_baseline(self):
        tr = self.add(match="assigned", tag="", types=["Bug"])
        self.check(tr, FakeAz([item(1)]))
        res = self.crew.act("trigger_update", {"id": tr["id"], "name": "board", "kind": "ado", "task": {},
                                               "spec": self.SPEC | {"match": "assigned", "tag": "", "types": ["Bug", "Product Backlog Item"]}}, via="ui")
        self.assertNotIn("error", res, res)
        self.assertFalse(tr["baselined"])
        self.check(tr, FakeAz([item(1), item(2, type="Product Backlog Item")]))  # the PBI was already assigned: baseline
        self.assertEqual(self.crew.runs, [])
        self.check(tr, FakeAz([item(1), item(2), item(3)]))
        self.assertEqual([r["ado"]["id"] for r in self.crew.runs], [3])

    def test_turning_it_back_on_takes_a_new_baseline(self):
        tr = self.add(match="assigned", tag="")
        self.check(tr, FakeAz([item(1)]))
        self.crew.act("trigger_update", {"id": tr["id"], "enabled": False}, via="ui")
        self.assertTrue(tr["baselined"])
        self.crew.act("trigger_update", {"id": tr["id"], "enabled": True}, via="ui")
        self.assertFalse(tr["baselined"])
        self.check(tr, FakeAz([item(1), item(2)]))  # assigned while it was off: not new work
        self.assertEqual(self.crew.runs, [])
        self.check(tr, FakeAz([item(1), item(2), item(3)]))
        self.assertEqual([r["ado"]["id"] for r in self.crew.runs], [3])

    def test_editing_only_the_interval_keeps_the_baseline(self):
        tr = self.add(match="assigned", tag="")
        self.check(tr, FakeAz([item(1)]))
        self.crew.act("trigger_update", {"id": tr["id"], "name": "board", "kind": "ado", "task": {},
                                         "spec": self.SPEC | {"match": "assigned", "tag": "", "every": 15}}, via="ui")
        self.assertTrue(tr["baselined"])
