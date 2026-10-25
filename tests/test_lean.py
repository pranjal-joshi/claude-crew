"""Toolkits (which tools an agent loads), subagent workflow steps, and the token budget since Crew started."""
import unittest

import agents
from agents import toolkit
from tests.helpers import TempCrewCase

INIT = {"type": "system", "subtype": "init", "session_id": "s1",
        "tools": ["Read", "Edit", "Write", "Glob", "Grep", "Bash", "Task", "WebSearch", "WebFetch", "Artifact", "DesignSync", "Skill",
                  "mcp__azure-devops__wit_get", "mcp__azure-devops__pr_get", "mcp__claude_ai_Microsoft_365__mail_search", "mcp__crew__decide"],
        "mcp_servers": [{"name": "azure-devops", "status": "connected"}, {"name": "claude.ai Microsoft 365", "status": "connected"},
                        {"name": "crew", "status": "connected"}]}


def usage(i, o=10, cache=0, mid="m1"):
    return {"type": "assistant", "message": {"id": mid, "content": [],
                                             "usage": {"input_tokens": i, "output_tokens": o, "cache_read_input_tokens": cache}}}


class Toolkits(TempCrewCase):
    def setUp(self):
        super().setUp()
        self.a = next(iter(self.crew.agents.values()))
        self.crew._on_event(self.a, INIT)

    def test_init_fills_the_catalog(self):
        self.assertIn("DesignSync", toolkit.builtins(self.crew))
        names = {s["prefix"]: s["tools"] for s in toolkit.servers(self.crew)}
        self.assertEqual(names, {"mcp__azure-devops": 2, "mcp__claude_ai_Microsoft_365": 1})

    def test_everything_adds_no_flags(self):
        self.assertIsNone(self.a["toolkit"])
        self.assertEqual(toolkit.flags(self.crew, self.a), [])

    def test_picked_tools_become_flags(self):
        self.crew.act("configure", {"agent": self.a["name"],
                                    "toolkit": {"groups": ["web"], "mcp": ["mcp__azure-devops"], "skills": False}})
        f = toolkit.flags(self.crew, self.a)
        self.assertEqual(f[0], "--tools=Read,Edit,Write,Glob,Grep,Bash,WebSearch,WebFetch")  # core always; unknown names dropped
        self.assertEqual(f[1:], ["--disallowedTools", "mcp__claude_ai_Microsoft_365", "--disable-slash-commands"])
        self.assertIn("toolkit", self.crew.config["agents"][0])  # saved to crew.json

    def test_other_group_takes_unmapped_builtins(self):
        self.a["toolkit"] = toolkit.clean({"groups": ["other"], "mcp": [], "skills": True})
        names = toolkit.flags(self.crew, self.a)[0].split("=")[1].split(",")
        self.assertIn("DesignSync", names)
        self.assertIn("Skill", names)
        self.assertNotIn("Artifact", names)  # in its own group, which isn't picked

    def test_recommended_follows_role_words(self):
        self.a.update(role="Triages new work items and files tickets", tags=["intake"])
        rec = toolkit.recommend(self.crew, self.a)
        self.assertIn("mcp__azure-devops", rec["mcp"])
        self.crew.act("configure", {"agent": self.a["name"], "toolkit": "recommended"})
        self.assertEqual(self.a["toolkit"], toolkit.clean(rec))
        self.assertLess(toolkit.estimate(self.crew, self.a["toolkit"]), toolkit.estimate(self.crew, None))

    def test_a_toolkit_change_is_part_of_the_launch_key(self):
        k = agents.launch.launch_key(self.crew, self.a)
        self.a["toolkit"] = toolkit.clean({"groups": [], "mcp": [], "skills": True})
        self.assertNotEqual(k, agents.launch.launch_key(self.crew, self.a))

    def test_bad_input_is_cleaned(self):
        self.assertEqual(toolkit.clean({"groups": ["nope", "web"], "mcp": ["rm -rf", "mcp__crew", "mcp__ok"]}),
                         {"groups": ["core", "web"], "mcp": ["mcp__ok"], "skills": True})
        self.assertIsNone(toolkit.clean("x"))


class SubagentSteps(TempCrewCase):
    def setUp(self):
        super().setUp()
        self.n = list(self.crew.agents)
        self.w = {"id": "ws", "name": "Ship", "subagents": True, "steps": [
            {"agent": self.n[0], "title": "Build {input}", "gate": "auto"},
            {"agent": self.n[1], "title": "Test {input}", "gate": "auto"},
            {"agent": self.n[0], "title": "Fix {input}", "gate": "auto"}]}

    def finish(self, t, summary, sid):
        t.setdefault("sessions", {})[t["who"]] = sid
        t.update(summary=summary, col="done")
        self.crew._wf_event(t, "done", None)

    def test_same_agent_reuses_the_run_session_and_steps_say_subagent(self):
        with self.crew.lock:
            run = self.crew._start_run(self.w, "login")
            first = self.crew.tasks[-1]
            self.assertNotIn("sessions", first)  # its first step: a new session
            self.assertIn(f"Ship · {run['id']} · step 1", first["details"])
            self.finish(first, "built", "sess-A")
            second = self.crew.tasks[-1]
            self.assertNotIn("sessions", second)  # another agent
            self.finish(second, "tested", "sess-B")
        third = self.crew.tasks[-1]
        self.assertEqual(third["sessions"], {self.n[0]: "sess-A"})
        self.assertTrue(third["run_session"])
        self.assertIn("step 3", third["details"])

    def test_off_by_default(self):
        with self.crew.lock:
            self.crew._start_run(dict(self.w, subagents=False), "x")
        self.assertNotIn("subagent", self.crew.tasks[-1]["details"])

    def test_saved_with_the_workflow(self):
        r = self.crew.act("wf_save", {"name": "S", "steps": self.w["steps"], "subagents": True})
        w = next(x for x in self.crew.config["workflows"] if x["id"] == r["id"])
        self.assertTrue(w["subagents"])


class TokenBudget(TempCrewCase):
    def setUp(self):
        super().setUp()
        self.a = next(iter(self.crew.agents.values()))

    def test_counts_since_start_and_by_agent(self):
        self.crew._on_event(self.a, usage(1000, 100, cache=500))
        self.crew._on_event(self.a, usage(1000, 100, cache=500))  # the same message again: counted once
        T = self.crew.snapshot()["budget"]["tokens"]
        self.assertEqual((T["in"], T["cache"], T["out"], T["total"]), (1000, 500, 100, 1600))
        self.assertEqual(T["by"], {self.a["name"]: 1600})

    def test_warns_at_80_then_blocks_at_100(self):
        self.assertEqual(self.crew.act("budgets", {"tokens": 1000}), {"ok": True})
        self.crew._on_event(self.a, usage(800, 10, mid="m1"))
        T = self.crew._tok_public()
        self.assertEqual((T["level"], T["ack"], T["blocked"]), (80, 0, False))
        self.crew.act("budget_ack", {"level": 80})
        self.assertEqual(self.crew._tok_public()["ack"], 80)
        self.crew._on_event(self.a, usage(300, 10, mid="m2"))
        T = self.crew._tok_public()
        self.assertEqual((T["level"], T["blocked"]), (100, True))
        self.assertTrue(self.crew._tok_blocked())
        self.assertTrue(any(al.get("key") == "budget:tokens" for al in self.crew.alerts))
        self.crew.act("budgets", {"tokens": "10000"})  # raised: unblocked, and the warning asks again next time
        self.assertFalse(self.crew._tok_blocked())
        self.assertEqual(self.crew._tok_public()["ack"], 0)

    def test_bad_token_budget(self):
        self.assertIn("error", self.crew.act("budgets", {"tokens": "lots"}))
        self.assertIn("error", self.crew.act("budgets", {"tokens": -5}))

    def test_plan_limits_come_from_the_stream(self):
        info = {"status": "allowed", "unifiedWindows": {"five_hour": {"utilization": 0.4, "resetsAt": 1},
                                                        "seven_day": {"utilization": 0.7, "resetsAt": 2}}}
        self.crew._on_event(self.a, {"type": "rate_limit_event", "rate_limit_info": info})
        self.assertEqual(self.crew.snapshot()["plan"]["unifiedWindows"]["seven_day"]["utilization"], 0.7)


if __name__ == "__main__":
    unittest.main()
