"""Crew tools for Claude Code agents: a tiny MCP server (stdio, JSON-RPC, standard library only).

Crew starts one per agent session. Every agent can read the crew and hand work to a teammate;
the boss can also run the whole crew: tasks, workflows, agents and triggers.

    CREW_URL=http://127.0.0.1:8770  CREW_AGENT=builder  CREW_ROLE=agent|boss
"""
import json
import os
import sys
import urllib.error
import urllib.request

URL = os.environ.get("CREW_URL", "http://127.0.0.1:8770")
ME = os.environ.get("CREW_AGENT", "")
BOSS = os.environ.get("CREW_ROLE") == "boss"
TOKEN = os.environ.get("CREW_TOKEN", "")

S = {"type": "string"}
I = {"type": "integer"}
STEP = {"type": "object", "properties": {"agent": S, "title": S, "details": S, "gate": {"type": "string", "enum": ["approve", "auto", "until"]},
                                          "every": I, "checks": I, "par": {"type": "boolean"}},
        "required": ["agent", "title"]}


def tool(name, desc, props=None, required=(), boss=False):
    return {"name": name, "description": desc, "boss": boss,
            "inputSchema": {"type": "object", "properties": props or {}, "required": list(required)}}


TOOLS = [
    tool("crew_status", "The crew right now: agents (role, tags, model, status, current task), tasks by column, workflows, runs and triggers."),
    tool("handoff", "Hand follow-up work to a teammate as a new task. `to` is an agent name, or 'auto' to let the agent "
         "whose tags match `tag` pick it up. Use once, when your finished work clearly needs another specialist next "
         "(tests, review, docs, deploy check). Include what you did and where (branch/commit) in `details`. `title` is what people read on the card: about 6 plain words saying what to do (\"Fix login timeout on upload\"), no links or long IDs (AB#123 is fine); put everything else in `details`.",
         {"to": S, "title": S, "details": S, "tag": S}, ["to", "title"]),
    tool("raise_alert", "Raise an alert on the Crew board for something failing that a person must see now: a failed "
         "deployment or rollout, a red pipeline on the main branch, an outage or error spike, a security problem, a blocked "
         "dependency or expired access. severity: critical (users affected or prod down), warning (needs attention soon) or "
         "info. Put the evidence in `details` (what failed, where, since when, the query or log line) and a URL in `link` if "
         "there is one; for a red pipeline, the failed run's link: Crew then resolves the alert itself once a later run of "
         "that pipeline on the same branch succeeds. Raising the same title again updates the open alert (count, details) instead of adding a copy. "
         "Not for your own task's normal progress or test failures you are fixing.",
         {"title": S, "severity": {"type": "string", "enum": ["critical", "warning", "info"]}, "details": S, "link": S},
         ["title", "severity"]),
    tool("resolve_alert", "Resolve an alert you raised, by the same title, once the problem has recovered. Say how in `note`.",
         {"title": S, "note": S}, ["title"]),
    tool("watch", "On a ⟳ (check until done) workflow step: instead of being woken on a timer, give Crew one read-only "
         "command (gh pr view/checks/list/status, gh run view/list, gh api GET, az pipelines runs/build list|show, az repos pr "
         "show|list, az boards work-item show, git log/ls-remote/rev-parse/merge-base; no pipes: trim with --jq or --query). "
         "Crew runs it every 2 minutes at no cost and wakes you when it matches the `done` regex (the goal) or the `fail` "
         "regex with a new failure (e.g. FAILED|CHANGES_REQUESTED: print each failed check's link so you can fix it); with "
         "no `fail`, any change wakes you. Print only what matters (no timestamps). Empty command removes the watch.",
         {"command": S, "done": S, "fail": S}, ["command"]),
    tool("fix_pipeline", "On a ⟳ step blocked by a pipeline failure only a code change fixes (lint, a build or test error): "
         "a deploy step whose run failed, or a PR step whose check fails on master too (not this PR's doing). Hands it to "
         "Crew's Pipeline fix workflow, which finds the cause, checks that nobody else has fixed it already, ships a PR and "
         "keeps it green until it merges. Your step waits meanwhile (no checks) and you are woken when the fix run ends: a "
         "deploy step follows the first run that carries the fix, a PR step brings master into its branch. Not for flaky "
         "or infrastructure failures. `reason`: what failed, where, and why code must change. `join`: the id of a running "
         "Pipeline fix run (see crew_status) already fixing the same failure, instead of starting another.",
         {"reason": S, "join": S}, ["reason"]),
    tool("decide", "Gate the task you are running: should this work go ahead? verdict 'continue': your task closes "
         "without review and, in a workflow, the next step starts with your summary (outside a workflow, hand the work on "
         "with the handoff tool first). verdict 'stop': your task closes marked stopped and the workflow run ends here; "
         "nothing else starts. verdict 'blocked' (⟳ steps only): the goal is stuck on something outside your change "
         "that you can't fix (a cancelled or superseded run, another change's failure, a stage a person must run): the "
         "card goes to Review for a person, with no more checks, and your watch still continues the step if it reaches "
         "done. `reason` is one line shown on the card: for blocked, what a person must do. Call it once, last, then end "
         "with your summary.",
         {"verdict": {"type": "string", "enum": ["continue", "stop", "blocked"]}, "reason": S}, ["verdict", "reason"]),
    tool("remember", "Save a fact about this project that teammates need in later tasks, in Crew's shared memory: every "
         "agent reads it when it starts a task. For durable truths you had to work out: how a deploy is confirmed, which "
         "pipeline steps are non-blocking or skipped by design, where config lives, who owns what, a recurring gotcha. "
         "Not task progress or anything one task needs. `text`: one fact, the rule and why, under 400 characters. `key`: "
         "a short slug (e.g. deploy-confirmed-by); saving the same key replaces that fact, so correct an outdated one "
         "instead of adding another. Empty `text` with a `key` forgets it.",
         {"text": S, "key": S}, []),
    tool("create_task", "Add a task to the board. Leave `agent` empty to let tags route it. `title` is what people read on the card: about 6 plain words saying what to do (\"Fix login timeout on upload\"), no links or long IDs (AB#123 is fine); put everything else in `details`.",
         {"title": S, "details": S, "tag": S, "agent": S}, ["title"], boss=True),
    tool("assign_task", "Give an existing task (any column) to an agent as a to-do.", {"task": I, "agent": S}, ["task", "agent"], boss=True),
    tool("message_agent", "Send a message to an agent; it lands mid-run if the agent is busy.", {"agent": S, "text": S}, ["agent", "text"], boss=True),
    tool("stop_agent", "Stop an agent's current run. Its task goes back to the backlog on hold; the session is kept.", {"agent": S}, ["agent"], boss=True),
    tool("run_workflow", "Start a workflow run on a goal. `title` is what people read on the run: about 6 plain words saying what to do (\"Fix login timeout on upload\"), no links or long IDs (AB#123 is fine); put everything else in `details`.", {"workflow": S, "title": S, "details": S}, ["workflow", "title"], boss=True),
    tool("add_agent", "Add an agent to the crew. model: fable|opus|sonnet|haiku or empty; effort: low..max or empty.",
         {"name": S, "role": S, "tags": {"type": "array", "items": S}, "model": S, "effort": S}, ["name", "role"], boss=True),
    tool("update_agent", "Change an agent's role, tags, model or effort.",
         {"name": S, "role": S, "tags": {"type": "array", "items": S}, "model": S, "effort": S}, ["name"], boss=True),
    tool("save_workflow", "Create a workflow, or replace one when `id` is given. Steps run in order; gate 'approve' waits for "
         "the user, 'auto' continues when the step is done, 'until' asks the step's agent to check again every `every` minutes "
         "(at most `checks` times) until it calls decide: for a PR to merge or a deployment to land. A step with par: true starts "
         "at the same time as the step before it; the step after them waits until all of them are done and gets every "
         "summary. Step titles may use {input}. `output` says what happens when a "
         "run finishes: {kind: board} (default), {kind: report, path: folder in the project} (markdown report), "
         "{kind: webhook, url}, or {kind: workflow, workflow: id} (start another workflow with the same goal). `subagents`: true "
         "keeps one session per agent for a run, and each step runs in a subagent named after the workflow, run and step "
         "(for workflows where one agent works several steps).",
         {"id": S, "name": S, "description": S, "steps": {"type": "array", "items": STEP}, "output": {"type": "object"},
          "subagents": {"type": "boolean"}},
         ["name", "steps"], boss=True),
    tool("set_card_button", "Add a button to the board's task cards, or replace one with the same `label` (or `id`). It shows on "
         "cards that match `when` and, pressed by the user, does `do`. when: {tags: [agent tags; the card's agent must have one], "
         "cols: [backlog|doing|review|done, default review and done], has: ''|pr|no_pr|pipeline, agent: one agent's name}. "
         "do: {kind: agent, text} asks the card's own agent, in the card's session, e.g. '/rebase {pr}' to run a skill it has "
         "or a plain instruction; {kind: workflow, workflow: id or name} starts that workflow on the card; {kind: handoff, "
         "to: agent name or auto, tag, text} gives the card to another agent as a new task. `text` may use {id}, {title}, "
         "{pr}, {pipeline}, {branch}, {agent} and {summary}. `icon`: one emoji; `hint`: the tooltip. Buttons run no shell "
         "commands themselves. Prefer an existing skill or workflow over a long instruction.",
         {"id": S, "label": S, "icon": S, "hint": S,
          "when": {"type": "object", "properties": {"tags": {"type": "array", "items": S}, "cols": {"type": "array", "items": S},
                                                    "has": S, "agent": S}},
          "do": {"type": "object", "properties": {"kind": {"type": "string", "enum": ["agent", "workflow", "handoff"]},
                                                  "text": S, "workflow": S, "to": S, "tag": S}, "required": ["kind"]}},
         ["label", "do"], boss=True),
    tool("delete_card_button", "Remove a card button by its id or label.", {"id": S}, ["id"], boss=True),
    tool("add_trigger", "Create a trigger. kind: schedule (spec.cron, 5-field local time), event (spec.on: review|done|error, "
         "optional spec.tag/spec.agent), watch (spec.glob, spec.every seconds) or webhook. kind poll (runs a shell command) and kind ado (writes to Azure DevOps) "
         "are UI-only: you cannot create them, so ask the user to add it in Crew's Triggers panel. It creates `task` (title, details, tag, agent) or, with task.workflow "
         "set to a workflow id, starts that workflow with the task title as its goal.",
         {"name": S, "kind": S, "spec": {"type": "object"}, "task": {"type": "object"}}, ["name", "kind", "task"], boss=True),
]


def call(path, body=None):
    req = urllib.request.Request(URL + path, data=json.dumps(body).encode() if body is not None else None,
                                 headers={"Content-Type": "application/json", "X-Crew": "1", "X-Crew-Auth": TOKEN}, method="POST" if body is not None else "GET")
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        return json.loads(e.read() or b"{}") or {"error": f"HTTP {e.code}"}
    except OSError as e:
        return {"error": f"Crew is not reachable: {e}"}


def status() -> str:
    d = call("/api/state")
    if "error" in d:
        return d["error"]
    lines = ["Agents:"]
    for a in d["agents"]:
        lines.append(f"- {a['name']} [{'OFF, takes no new work' if a.get('off') else a['status']}] tags={','.join(a['tags'])} model={a['model'] or 'default'}"
                     f"{' task #' + str(a['task']) if a.get('task') else ''}: {a['role'][:160]}")
    lines.append("Tasks:")
    for t in d["tasks"][-60:]:
        lines.append(f"- #{t['id']} [{t['col']}{', on hold' if t.get('hold') else ''}] {t['title']} (tag {t['tag']}, {t.get('who') or 'unassigned'})")
    lines.append("Workflows:")
    for w in d.get("workflows", []):
        lines.append(f"- {w['id']} {w['name']}{' [OFF, cannot run]' if w.get('off') else ''}: " + " -> ".join(f"{s['agent']}({s['gate']})" for s in w["steps"]))
    for r in d.get("runs", [])[-10:]:
        lines.append(f"- run {r['id']} {r['name']} [{r['status']}] step {r['step'] + 1}/{len(r['steps'])}: {r['title']}")
    if d.get("card_actions"):
        lines.append("Card buttons:")
        for b in d["card_actions"]:
            w, do = b["when"], b["do"]
            lines.append(f"- {b['id']} {b['icon']} {b['label']}: on {'/'.join(w['cols'])} cards" + (f" of tags {','.join(w['tags'])}" if w['tags'] else "")
                         + (f" of {w['agent']}" if w['agent'] else "") + (f" with {w['has']}" if w['has'] else "") + f" -> {do['kind']} "
                         + (do.get("text") or do.get("workflow") or "")[:80] + (f" to {do['to']}" if do.get("to") else ""))
    lines.append("Triggers:")
    for tr in d.get("triggers", []):
        lines.append(f"- {tr['id']} {tr['name']} ({tr['kind']}, {'on' if tr['enabled'] else 'off'})")
    live = [x for x in d.get("alerts", []) if x["status"] != "resolved"]
    if live:
        lines.append("Open alerts:")
        lines += [f"- [{x['severity']}{', acknowledged' if x['status'] == 'ack' else ''}] {x['title']} (by {x['agent'] or 'Crew'}, x{x['count']})" for x in live]
    return "\n".join(lines)


def run_tool(name, a):
    if name == "crew_status":
        return status()
    if name == "handoff":
        r = call("/api/act/handoff", {"from": ME, "to": a.get("to"), "title": a.get("title"), "details": a.get("details", ""), "tag": a.get("tag", "")})
    elif name == "watch":
        r = call("/api/act/watch", {"from": ME, "command": a.get("command"), "done": a.get("done"), "fail": a.get("fail")})
    elif name == "remember":
        r = call("/api/act/remember", {"from": ME, "text": a.get("text") or "", "key": a.get("key") or ""})
    elif name == "decide":
        r = call("/api/act/decide", {"from": ME, "verdict": a.get("verdict"), "reason": a.get("reason")})
    elif name == "fix_pipeline":
        r = call("/api/act/pipeline_fix", {"from": ME, "reason": a.get("reason"), "join": a.get("join") or ""})
    elif name == "raise_alert":
        r = call("/api/act/alert", {"from": ME, **{k: a.get(k) for k in ("title", "severity", "details", "link")}})
    elif name == "resolve_alert":
        r = call("/api/act/alert_resolve", {"from": ME, "title": a.get("title"), "note": a.get("note", "")})
    elif not BOSS:
        return {"error": f"{name} is for the boss only"}
    elif name == "create_task":
        r = call("/api/act/add", {"title": a.get("title"), "details": a.get("details", ""), "tag": a.get("tag", ""), "agent": a.get("agent", "")})
    elif name == "assign_task":
        r = call("/api/act/assign", {"task": a.get("task"), "agent": a.get("agent")})
    elif name == "message_agent":
        r = call("/api/act/tell", {"agent": a.get("agent"), "text": a.get("text"), "from": ME})
    elif name == "stop_agent":
        r = call("/api/act/stop", {"agent": a.get("agent")})
    elif name == "run_workflow":
        wf = a.get("workflow", "")
        d = call("/api/state")
        match = next((w for w in d.get("workflows", []) if wf in (w["id"], w["name"])), None)
        r = call("/api/act/wf_run", {"id": match["id"] if match else wf, "title": a.get("title"), "details": a.get("details", "")})
    elif name == "add_agent":
        r = call("/api/act/add_agent", {k: a.get(k) for k in ("name", "role", "tags", "model", "effort") if a.get(k) is not None})
    elif name == "update_agent":
        r = call("/api/act/configure", {"agent": a.get("name"), **{k: a[k] for k in ("role", "tags", "model", "effort") if k in a}})
    elif name == "save_workflow":
        r = call("/api/act/wf_save", a)
    elif name == "set_card_button":
        r = call("/api/act/card_button_save", a)
    elif name == "delete_card_button":
        r = call("/api/act/card_button_delete", {"id": a.get("id")})
    elif name == "add_trigger":
        r = call("/api/act/trigger_add", a)
    else:
        return {"error": f"unknown tool {name}"}
    return r


def reply(rid, result=None, error=None):
    msg = {"jsonrpc": "2.0", "id": rid}
    msg["error" if error else "result"] = error or result
    sys.stdout.write(json.dumps(msg) + "\n")
    sys.stdout.flush()


def main():
    for line in sys.stdin:
        try:
            m = json.loads(line)
        except ValueError:
            continue
        rid, method = m.get("id"), m.get("method")
        if rid is None:
            continue  # notifications need no answer
        if method == "initialize":
            reply(rid, {"protocolVersion": (m.get("params") or {}).get("protocolVersion", "2025-06-18"),
                        "capabilities": {"tools": {}}, "serverInfo": {"name": "crew", "version": "1.0"}})
        elif method == "tools/list":
            reply(rid, {"tools": [{k: v for k, v in t.items() if k != "boss"} for t in TOOLS if BOSS or not t["boss"]]})
        elif method == "tools/call":
            p = m.get("params") or {}
            out = run_tool(p.get("name"), p.get("arguments") or {})
            err = isinstance(out, dict) and "error" in out
            text = out if isinstance(out, str) else json.dumps(out)
            reply(rid, {"content": [{"type": "text", "text": text}], "isError": err})
        elif method == "ping":
            reply(rid, {})
        else:
            reply(rid, error={"code": -32601, "message": f"method not found: {method}"})


if __name__ == "__main__":
    main()
