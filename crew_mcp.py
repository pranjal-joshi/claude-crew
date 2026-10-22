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
STEP = {"type": "object", "properties": {"agent": S, "title": S, "details": S, "gate": {"type": "string", "enum": ["approve", "auto"]}},
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
         "there is one. Raising the same title again updates the open alert (count, details) instead of adding a copy. "
         "Not for your own task's normal progress or test failures you are fixing.",
         {"title": S, "severity": {"type": "string", "enum": ["critical", "warning", "info"]}, "details": S, "link": S},
         ["title", "severity"]),
    tool("resolve_alert", "Resolve an alert you raised, by the same title, once the problem has recovered. Say how in `note`.",
         {"title": S, "note": S}, ["title"]),
    tool("decide", "Gate the task you are running: should this work go ahead? verdict 'continue': your task closes "
         "without review and, in a workflow, the next step starts with your summary (outside a workflow, hand the work on "
         "with the handoff tool first). verdict 'stop': your task closes marked stopped and the workflow run ends here; "
         "nothing else starts. `reason` is one line shown on the card. Call it once, last, then end with your summary.",
         {"verdict": {"type": "string", "enum": ["continue", "stop"]}, "reason": S}, ["verdict", "reason"]),
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
         "the user, 'auto' continues when the step is done. Step titles may use {input}. `output` says what happens when a "
         "run finishes: {kind: board} (default), {kind: report, path: folder in the project} (markdown report), "
         "{kind: webhook, url}, or {kind: workflow, workflow: id} (start another workflow with the same goal).",
         {"id": S, "name": S, "description": S, "steps": {"type": "array", "items": STEP}, "output": {"type": "object"}},
         ["name", "steps"], boss=True),
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
        lines.append(f"- {a['name']} [{a['status']}] tags={','.join(a['tags'])} model={a['model'] or 'default'}"
                     f"{' task #' + str(a['task']) if a.get('task') else ''}: {a['role'][:160]}")
    lines.append("Tasks:")
    for t in d["tasks"][-60:]:
        lines.append(f"- #{t['id']} [{t['col']}{', on hold' if t.get('hold') else ''}] {t['title']} (tag {t['tag']}, {t.get('who') or 'unassigned'})")
    lines.append("Workflows:")
    for w in d.get("workflows", []):
        lines.append(f"- {w['id']} {w['name']}: " + " -> ".join(f"{s['agent']}({s['gate']})" for s in w["steps"]))
    for r in d.get("runs", [])[-10:]:
        lines.append(f"- run {r['id']} {r['name']} [{r['status']}] step {r['step'] + 1}/{len(r['steps'])}: {r['title']}")
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
    elif name == "decide":
        r = call("/api/act/decide", {"from": ME, "verdict": a.get("verdict"), "reason": a.get("reason")})
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
