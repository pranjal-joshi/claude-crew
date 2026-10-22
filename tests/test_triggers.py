"""The trigger registry: kinds come from triggers/, and a new kind needs nothing in server.py."""
import unittest
from unittest import mock

import triggers
from tests.helpers import server
from tests.test_auth import AuthCase


class Demo(triggers.Trigger):
    """A kind that exists only in this test: polled, UI-only, with a form."""
    kind, polled, ui_only = "demo", True, "demo triggers: create them in Crew's Triggers panel"

    def __init__(self):
        self.checked = []

    def validate(self, sp, old=None):
        return (None, {"every": int(sp["every"])}) if sp.get("every") else ("every is missing", {})

    def check(self, crew, tr):
        self.checked.append(tr["id"])
        self.finish(crew, tr, None, {"found": 0})

    def form(self, crew, p):
        return {"ok": True, "echo": p.get("op")}


class Registry(AuthCase):
    def setUp(self):
        super().setUp()
        self.demo = Demo()
        triggers.REGISTRY["demo"] = self.demo
        self.addCleanup(triggers.REGISTRY.pop, "demo", None)

    def test_the_builtin_kinds_in_ui_order(self):
        self.assertEqual(server.TRIGGER_KINDS, ["schedule", "event", "watch", "poll", "webhook", "ado", "teams", "pipeline"])
        self.assertEqual(triggers.kinds()[-1], "demo")  # unordered kinds go last

    def test_a_new_kind_is_validated_and_ui_only(self):
        body = {"name": "d", "kind": "demo", "spec": {"every": "3"}, "task": {"title": "t"}}
        self.assertEqual(self.crew.act("trigger_add", body, via="mcp"), {"error": Demo.ui_only})
        self.assertIn("every", self.crew.act("trigger_add", body | {"spec": {}}, via="ui")["error"])
        self.assertTrue(self.crew.act("trigger_add", body, via="ui")["ok"])
        self.assertEqual(self.crew.triggers[-1]["spec"], {"every": 3})
        self.assertEqual(self.crew.act("trigger_add", body | {"kind": "nope"}, via="ui"), {"error": "pick a trigger type"})
        self.assertEqual(self.crew.act("trigger_add", body | {"kind": ["demo"]}, via="ui"), {"error": "pick a trigger type"})

    def test_tick_runs_the_check_in_a_thread_when_due(self):
        self.crew.act("trigger_add", {"name": "d", "kind": "demo", "spec": {"every": 1}, "task": {"title": "t"}}, via="ui")
        tr = self.crew.triggers[-1]
        self.crew.tick_triggers()
        for _ in range(200):
            if self.demo.checked:
                break
            import time
            time.sleep(0.02)
        self.assertEqual(self.demo.checked, [tr["id"]])
        self.crew.tick_triggers()  # not due for another minute
        self.assertEqual(self.demo.checked, [tr["id"]])
        self.assertIsNotNone(self.crew._public_triggers()[-1]["next"])

    def test_form_route_is_generic_and_ui_only(self):
        self.assertEqual(self.post("/api/trigger/demo", {"op": "x"}, token=server.UI_TOKEN), (200, {"ok": True, "echo": "x"}))
        self.assertEqual(self.post("/api/trigger/demo", {"op": "x"}, token=server.MCP_TOKEN)[0], 403)
        self.assertEqual(self.post("/api/trigger/demo", {"op": "x"})[0], 403)
        self.assertEqual(self.post("/api/trigger/nope", {}, token=server.UI_TOKEN)[0], 404)
        self.assertEqual(self.post("/api/trigger/webhook", {}, token=server.UI_TOKEN)[0], 404)  # a kind without a form

    def test_old_routes_are_aliases(self):
        for path, kind in (("/api/ado", "ado"), ("/api/teams", "teams")):
            with mock.patch.object(triggers.get(kind), "form", return_value={"ok": True}) as form:
                self.assertEqual(self.post(path, {"op": "test"}, token=server.UI_TOKEN), (200, {"ok": True}))
                self.assertEqual(self.post(path, {"op": "test"}, token=server.MCP_TOKEN)[0], 403)
            form.assert_called_once()

    def test_ado_route_still_serves_the_pipeline_form(self):
        with mock.patch.object(triggers.get("pipeline"), "form", return_value={"ok": True}) as form:
            self.post("/api/ado", {"op": "pipeline_test", "spec": {}}, token=server.UI_TOKEN)
            self.post("/api/ado", {"op": "pipelines", "spec": {}}, token=server.UI_TOKEN)
        self.assertEqual([c.args[1]["op"] for c in form.call_args_list], ["test", "pipelines"])


if __name__ == "__main__":
    unittest.main()
