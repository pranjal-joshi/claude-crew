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


class Alerts(TempCrewCase):
    def test_raise_dedupes_and_escalates(self):
        a = next(iter(self.crew.agents))
        r = self.crew.act("alert", {"from": a, "title": "Deploy failed on nonprod", "severity": "warning", "details": "pod crashloop"}, via="mcp")
        self.assertTrue(r["ok"])
        r2 = self.crew.act("alert", {"from": a, "title": "deploy  failed on NONPROD", "severity": "critical"}, via="mcp")
        self.assertEqual((r2["id"], r2["count"]), (r["id"], 2))  # same problem: one alert, counted, now critical
        live = [x for x in self.crew.snapshot()["alerts"] if x["status"] != "resolved"]
        self.assertEqual(len(live), 1)
        self.assertEqual(live[0]["severity"], "critical")
        self.assertIn("alert", [e["kind"] for e in self.crew.feed])

    def test_validation(self):
        self.assertIn("error", self.crew.act("alert", {"title": " "}))
        self.assertIn("error", self.crew.act("alert", {"title": "x", "severity": "panic"}))

    def test_ack_task_resolve(self):
        al = self.crew.act("alert", {"title": "Pipeline red on master", "severity": "warning", "link": "https://ci.example/1"})
        self.crew.act("alert_ack", {"id": al["id"]})
        self.assertEqual(self.crew.alerts[-1]["status"], "ack")
        r = self.crew.act("alert_task", {"id": al["id"]})
        t = next(t for t in self.crew.tasks if t["id"] == r["task"])
        self.assertEqual((t["col"], t["alert"]), ("backlog", al["id"]))
        self.assertIn("https://ci.example/1", t["details"])
        self.assertIn("error", self.crew.act("alert_task", {"id": al["id"]}))  # only once
        self.crew.act("alert_resolve", {"id": al["id"]})
        self.assertEqual(self.crew.alerts[-1]["status"], "resolved")

    def test_agents_resolve_only_their_own(self):
        a, b = list(self.crew.agents)[:2]
        al = self.crew.act("alert", {"from": a, "title": "Rollout stuck", "severity": "warning"}, via="mcp")
        self.assertIn("error", self.crew.act("alert_resolve", {"from": b, "id": al["id"]}, via="mcp"))
        self.assertTrue(self.crew.act("alert_resolve", {"from": a, "title": "rollout stuck"}, via="mcp")["ok"])

    def test_failing_trigger_alerts_and_recovers(self):
        self.crew.raise_alert("Trigger x check failed", "warning", "az: login expired", key="trigger:t1")
        self.assertTrue(self.crew.resolve_alert("trigger:t1"))
        self.assertFalse(self.crew.resolve_alert("trigger:t1"))

    def test_alert_actions_allowed_for_agents(self):
        self.assertTrue({"alert", "alert_resolve"} <= server.MCP_ACTIONS)
        self.assertFalse({"alert_task", "alert_ack", "release"} & server.MCP_ACTIONS)

    def test_release_refuses_running_agent(self):
        a = next(iter(self.crew.agents.values()))
        a["status"] = "running"
        self.assertIn("error", self.crew.act("release", {"agent": a["name"]}))
        a["status"] = "idle"
        self.assertTrue(self.crew.act("release", {"agent": a["name"]})["ok"])


class TellReopens(TempCrewCase):
    def _review_task(self):
        a = next(iter(self.crew.agents.values()))
        self.crew.act("add", {"title": "Fix the thing", "tag": "bug"})
        t = self.crew.tasks[-1]
        t.update(col="review", who=a["name"])
        a.update(status="review", task=t["id"])
        self.crew._start = lambda ag: True
        self.crew._send = lambda ag, text: True
        return a, t

    def test_message_moves_review_task_back_to_in_progress(self):
        a, t = self._review_task()
        self.assertNotIn("error", self.crew.act("tell", {"agent": a["name"], "text": "also handle empty input"}))
        self.assertEqual((t["col"], a["status"]), ("doing", "running"))

    def test_approved_task_stays_put(self):
        a, t = self._review_task()
        t["commit"] = "abc1234"
        self.crew.act("tell", {"agent": a["name"], "text": "thanks"})
        self.assertEqual(t["col"], "review")


class TaskSessions(TempCrewCase):
    def setUp(self):
        super().setUp()
        self.a = next(iter(self.crew.agents.values()))
        self.a["context"] = "fresh"
        self.crew._start = lambda ag: True
        self.crew._send = lambda ag, text: True
        self.crew._kill_proc = lambda name: None

    def _run(self, title):
        self.crew.act("add", {"title": title, "tag": "bug", "agent": self.a["name"]})
        t = self.crew.tasks[-1]
        self.crew._begin(self.a, t)
        self.crew._on_event(self.a, {"type": "system", "subtype": "init", "session_id": f"s-{t['id']}"})
        t.update(col="review")
        self.a.update(status="review")
        return t

    def test_new_task_fresh_old_task_resumes_its_session(self):
        t1 = self._run("first")
        t2 = self._run("second")
        self.assertEqual(t2["sessions"][self.a["name"]], f"s-{t2['id']}")  # a new task did not inherit t1's session
        self.crew.act("reject", {"task": t1["id"], "text": "one more thing"})
        self.assertEqual((self.a["session"], t1["col"]), (f"s-{t1['id']}", "doing"))  # back in t1's conversation

    def test_fresh_respawn_forgets_task_sessions(self):
        t1 = self._run("first")
        self.a["status"] = "idle"
        self.crew.act("respawn", {"agent": self.a["name"], "fresh": True})
        self.assertNotIn(self.a["name"], t1.get("sessions", {}))


class AddressReview(TempCrewCase):
    def test_runs_skill_in_task_session_and_clears_when_done(self):
        a = next(iter(self.crew.agents.values()))
        sent = []
        self.crew._start = lambda ag: True
        self.crew._send = lambda ag, text: sent.append(text) or True
        self.crew.act("add", {"title": "Ship it", "tag": "bug"})
        t = self.crew.tasks[-1]
        self.assertIn("error", self.crew.act("address_review", {"task": t["id"]}))  # no PR yet
        t.update(col="done", who=a["name"], pr="https://github.com/o/r/pull/7", sessions={a["name"]: "s-7"})
        self.assertNotIn("error", self.crew.act("address_review", {"task": t["id"]}))
        self.assertEqual((sent[-1], a["session"], a["status"], t["col"]), ("/address-review https://github.com/o/r/pull/7", "s-7", "running", "done"))
        self.assertIn("error", self.crew.act("address_review", {"task": t["id"]}))  # busy
        self.crew._on_event(a, {"type": "result", "subtype": "success", "result": "fixed 2 comments", "total_cost_usd": 0.1})
        self.assertNotIn("reviewing", t)
        self.assertEqual(a["status"], "idle")

    def test_ui_only(self):
        self.assertNotIn("address_review", server.MCP_ACTIONS)


class Decide(TempCrewCase):
    def setUp(self):
        super().setUp()
        self.crew._start = lambda ag: True
        self.crew._send = lambda ag, text: True
        names = list(self.crew.agents)
        self.gate, self.next = self.crew.agents[names[0]], self.crew.agents[names[1]]
        w = {"id": "gate", "name": "Gate", "steps": [{"agent": self.gate["name"], "title": "Pick: {input}", "gate": "approve"},
                                                    {"agent": self.next["name"], "title": "Do: {input}", "gate": "approve"}]}
        self.crew.config.setdefault("workflows", []).append(w)
        with self.crew.lock:
            self.run = self.crew._start_run(w, "AB#1 something broke")
        self.t = self.crew.tasks[-1]
        self.crew._begin(self.gate, self.t)

    def _end_turn(self, text="done"):
        self.crew._on_event(self.gate, {"type": "result", "subtype": "success", "result": text, "total_cost_usd": 0.01})

    def test_stop_ends_the_run(self):
        r = self.crew.act("decide", {"from": self.gate["name"], "verdict": "stop", "reason": "not our area"}, via="mcp")
        self.assertTrue(r["ok"])
        self._end_turn("Billing owns this.")
        self.assertEqual((self.t["col"], self.t["decision"]["verdict"], self.run["status"]), ("done", "stop", "stopped"))
        self.assertEqual(len([x for x in self.crew.tasks if x.get("run")]), 1)  # step 2 never created
        self.assertEqual(self.gate["status"], "idle")

    def test_continue_starts_next_step_without_review(self):
        self.crew.act("decide", {"from": self.gate["name"], "verdict": "continue", "reason": "ours: guardrail-svc"}, via="mcp")
        self._end_turn("Ours. Brief: ...")
        self.assertEqual(self.t["col"], "done")
        nxt = self.crew.tasks[-1]
        self.assertEqual((nxt["who"], nxt["run"]["step"], self.run["status"]), (self.next["name"], 1, "running"))
        self.assertIn("Ours. Brief", nxt["details"])

    def test_validation(self):
        self.assertIn("error", self.crew.act("decide", {"from": self.gate["name"], "verdict": "maybe", "reason": "x"}, via="mcp"))
        self.assertIn("error", self.crew.act("decide", {"from": self.gate["name"], "verdict": "stop", "reason": " "}, via="mcp"))
        self.assertIn("error", self.crew.act("decide", {"from": self.next["name"], "verdict": "stop", "reason": "x"}, via="mcp"))
        self.assertIn("decide", server.MCP_ACTIONS)


class PrLinks(TempCrewCase):
    def test_prs_read_from_summary_and_brief(self):
        a = next(iter(self.crew.agents.values()))
        self.crew._start = lambda ag: True
        self.crew._send = lambda ag, text: True
        self.crew.act("add", {"title": "Ship it", "tag": "bug", "details": "CI on https://github.com/o/r/pull/41 is red"})
        t = self.crew.tasks[-1]
        self.crew._begin(a, t)
        self.assertEqual(t["prs"], ["https://github.com/o/r/pull/41"])
        self.crew._on_event(a, {"type": "result", "subtype": "success", "total_cost_usd": 0.01,
                                "result": "Opened https://github.com/o/r/pull/42. Also see https://github.com/o/r/pull/41"})
        self.assertEqual(t["prs"], ["https://github.com/o/r/pull/41", "https://github.com/o/r/pull/42"])
        sent = []
        self.crew._send = lambda ag, text: sent.append(text) or True
        a["status"] = "idle"
        self.assertNotIn("error", self.crew.act("address_review", {"task": t["id"]}))
        self.assertEqual(sent[-1], "/address-review https://github.com/o/r/pull/42")  # the latest PR

    def test_no_false_matches(self):
        t = {}
        server.Crew._note_prs(t, "see github.com/o/r/pull/1 and https://github.com/o/r/issues/2")
        self.assertNotIn("prs", t)


class AdoBase(TempCrewCase):
    def test_board_url_from_a_fetched_item(self):
        server.Crew._ado_dflt = {}
        self.crew.tasks.append({"id": 99, "title": "x", "col": "done", "ado": {"id": 5, "url": "https://dev.azure.com/acme/My%20Proj/_workitems/edit/5"}})
        self.assertEqual(self.crew.snapshot()["ado_base"], "https://dev.azure.com/acme/My%20Proj/_workitems/edit/")

    def test_empty_when_unknown(self):
        server.Crew._ado_dflt = {}
        self.assertEqual(self.crew.snapshot()["ado_base"], "")


class Titles(TempCrewCase):
    def test_short_title(self):
        self.assertEqual(server.short_title("Model Editor: 'Harness Tool Suite completely offline' mid-session after a week"),
                         "Model Editor: 'Harness Tool Suite completely…")
        self.assertEqual(server.short_title("see https://x.y/z fix login"), "see fix login")

    def test_agents_must_use_short_titles(self):
        a = next(iter(self.crew.agents))
        long = "Investigate why the guardrail service returns a 500 error when the prompt is empty in preprod"
        self.assertIn("error", self.crew.act("handoff", {"from": a, "to": "auto", "title": long}, via="mcp"))
        self.assertIn("error", self.crew.act("add", {"title": "Check https://dev.azure.com/x/1"}, via="mcp"))
        self.assertNotIn("error", self.crew.act("add", {"title": "AB#4321 Fix empty prompt 500"}, via="mcp"))
        self.assertNotIn("error", self.crew.act("add", {"title": long}))  # yours, typed in the UI: untouched


class TeamsTrigger(TempCrewCase):
    def _fake_claude(self, result, cost=0.004):
        import subprocess
        out = json.dumps({"type": "result", "is_error": False, "result": result, "total_cost_usd": cost})
        self.calls = []
        def run(cmd, **kw):
            self.calls.append(cmd)
            return subprocess.CompletedProcess(cmd, 0, out, "")
        old = server.subprocess.run
        server.subprocess.run = run
        self.addCleanup(setattr, server.subprocess, "run", old)

    def _trigger(self):
        r = self.crew.act("trigger_add", {"name": "Incidents", "kind": "teams", "spec": {"where": "Platform > Incidents", "mode": "mention"},
                                          "task": {"title": "{title}", "details": "{from}: {text}\n{link}"}})
        self.assertTrue(r.get("ok"), r)
        return self.crew.triggers[-1]

    def test_new_messages_become_tasks_once(self):
        tr = self._trigger()
        msgs = {"messages": [{"id": "m1", "from": "Asha", "time": "2026-10-22T09:00:00Z", "text": "@Pranjal prod deploy failed",
                              "link": "https://teams.microsoft.com/l/message/1", "title": "Check failed prod deploy"}]}
        self._fake_claude("Here you go:\n" + json.dumps(msgs))
        self.crew._run_teams(tr)
        t = self.crew.tasks[-1]
        self.assertEqual((t["title"], tr["picked"], tr["error"]), ("Check failed prod deploy", 1, None))
        self.assertIn("https://teams.microsoft.com/l/message/1", t["details"])
        self.assertIn(server.TEAMS_TOOLS, self.calls[0])  # only the Microsoft 365 tools are allowed
        n = len(self.crew.tasks)
        self.crew._run_teams(tr)  # the same message again: nothing new
        self.assertEqual(len(self.crew.tasks), n)

    def test_errors_raise_an_alert(self):
        tr = self._trigger()
        self._fake_claude('{"error": "not signed in to Microsoft 365"}')
        self.crew._run_teams(tr)
        self.assertIn("not signed in", tr["error"])
        self.assertTrue(any(x["key"] == f"trigger:{tr['id']}" for x in self.crew.alerts))

    def test_ui_only_and_validated(self):
        self.assertIn("error", self.crew.act("trigger_add", {"name": "x", "kind": "teams", "spec": {"where": "a"}, "task": {"title": "t"}}, via="mcp"))
        self.assertIn("error", self.crew.act("trigger_add", {"name": "x", "kind": "teams", "spec": {"where": " "}, "task": {"title": "t"}}))


class PipelineTrigger(TempCrewCase):
    def setUp(self):
        super().setUp()
        self.runs = [
            {"id": 501, "result": "succeeded", "finishTime": "2099-01-01T10:00:00.1234567Z", "sourceBranch": "refs/heads/release",
             "sourceVersion": "abc123def4567890abcd", "definition": {"name": "Api.Release"},
             "requestedFor": {"displayName": "Someone Else", "uniqueName": "someone@x.com"}, "triggerInfo": {"ci.message": "Merge PR #1"}},
            {"id": 502, "result": "failed", "finishTime": "2099-01-01T11:00:00Z", "sourceBranch": "refs/heads/release",
             "definition": {"name": "Api.Release"}, "requestedFor": {"displayName": "Other", "uniqueName": "other@x.com"}},
        ]
        def fake_az(*args, raw=False, timeout=90):
            a = " ".join(args)
            if a.startswith("pipelines list"):
                return [{"id": 42, "name": "Api.Release"}]
            if a.startswith("pipelines runs list"):
                return self.runs
            if a.startswith("devops invoke"):
                return [7, 8] if "buildId=501" in a else [9]
            if a.startswith("boards query"):
                return [{"id": 8, "fields": {"System.Title": "Fix upload timeout", "System.State": "Resolved"}}] if "IN (7, 8)" in a else []
            if a.startswith("account show"):
                return "me@x.com"
            raise AssertionError(a)
        old = server.az
        server.az = fake_az
        self.addCleanup(setattr, server, "az", old)
        server._me = None
        self.qa = list(self.crew.agents)[0]

    def _add(self, **spec):
        r = self.crew.act("trigger_add", {"kind": "pipeline", "spec": {"pipelines": ["api.release"], "org": "https://dev.azure.com/acme",
                                                                         "project": "Proj", "agent": self.qa, **spec}})
        self.assertTrue(r.get("ok"), r)
        tr = self.crew.triggers[-1]
        tr["since"] = 1
        return tr

    def test_only_mine_keeps_runs_with_my_work_items(self):
        tr = self._add(mine=True, result="any")
        self.crew._run_pipeline(tr)
        made = [t for t in self.crew.tasks if (t.get("source") or {}).get("kind") == "pipeline"]
        self.assertEqual(len(made), 1)  # 501 carries AB#8, assigned to me; 502 is someone else's with nothing of mine
        t = made[0]
        self.assertEqual((t["title"], t["who"], t["ado"]["id"]), ("Api.Release succeeded", self.qa, 8))
        self.assertIn("AB#8 Fix upload timeout (Resolved)", t["details"])
        self.assertIn("buildId=501", t["details"])
        self.assertEqual(sorted(tr["seen"]), [501, 502])
        self.crew._run_pipeline(tr)  # seen: nothing fires twice
        self.assertEqual(len([t for t in self.crew.tasks if (t.get("source") or {}).get("kind") == "pipeline"]), 1)

    def test_result_filter(self):
        tr = self._add(mine=False, result="failed")
        self.crew._run_pipeline(tr)
        made = [t for t in self.crew.tasks if (t.get("source") or {}).get("kind") == "pipeline"]
        self.assertEqual([t["title"] for t in made], ["Api.Release failed"])

    def test_unknown_pipeline_is_an_error(self):
        tr = self._add()
        tr["spec"]["pipelines"] = ["Nope.Release"]
        self.crew._run_pipeline(tr)
        self.assertIn("no pipeline called Nope.Release", tr["error"])

    def test_iso_ts(self):
        self.assertEqual(server.iso_ts("1970-01-01T00:01:00.1234567Z"), 60.123456)
        self.assertEqual(server.iso_ts(""), 0.0)
