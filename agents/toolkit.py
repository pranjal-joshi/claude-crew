"""Toolkits: which tools an agent loads. Every tool's description is sent on every turn, so an agent that starts with
everything (all built-in tools, every MCP server, plugin and claude.ai connector) pays for it on each call. A toolkit
narrows that: a few groups of built-in tools (`--tools`), the MCP servers it keeps (the rest go in
`--disallowedTools mcp__<server>`, which drops their definitions), and skills on or off.
Measured with Claude Code 2.1: everything ≈ 85k tokens before the first word, core tools only ≈ 8k.
Deferred tool search (ENABLE_TOOL_SEARCH) changed nothing in -p mode, so a toolkit picks instead."""
from __future__ import annotations

import re

# key, label, tools, ≈ tokens each turn (measured on top of the core set), why you'd want it
GROUPS = [
    ("core", "Files & shell", ["Read", "Edit", "Write", "Glob", "Grep", "Bash", "NotebookEdit"], 8000,
     "Read, edit and search code; run commands. Includes Claude Code's own instructions."),
    ("subagents", "Subagents", ["Task", "Agent"], 2800, "Hand a piece of work to a subagent (needed for subagent workflow steps)."),
    ("web", "Web", ["WebSearch", "WebFetch"], 1000, "Search the web and read pages."),
    ("background", "Background jobs", ["Monitor", "TaskStop"], 3200, "Long builds and log watches that run while it works."),
    ("powershell", "PowerShell", ["PowerShell"], 3500, "A PowerShell tool next to Bash (Windows)."),
    ("schedule", "Scheduling", ["CronCreate", "CronDelete", "CronList", "ScheduleWakeup", "RemoteTrigger"], 4800,
     "Its own timers and routines (Crew triggers do this without it)."),
    ("orchestration", "Orchestration", ["Workflow", "SendMessage", "ListAgents"], 11400, "Claude Code's own multi-agent workflows and messaging."),
    ("artifacts", "Artifacts", ["Artifact", "ArtifactComments", "ArtifactData"], 21400, "Publish pages to claude.ai."),
    ("other", "Other built-ins", [], 7500, "Everything else Claude Code ships (design sync, notifications, worktrees, MCP resources…)."),
]
GROUP_KEYS = [g[0] for g in GROUPS]
SKILLS_TOKENS = 1000
MCP_TOOL_TOKENS = 450  # one MCP tool definition, on average


def prefix(server: str) -> str:
    """An MCP server's name as its tools carry it: "claude.ai Microsoft 365" -> mcp__claude_ai_Microsoft_365."""
    return "mcp__" + re.sub(r"[^A-Za-z0-9_-]", "_", server)


def clean(raw) -> dict | None:
    """None is everything (the default). Otherwise {groups, mcp (prefixes kept), skills}."""
    if not isinstance(raw, dict):
        return None
    groups = [g for g in GROUP_KEYS if g in (raw.get("groups") or [])]
    if "core" not in groups:
        groups.insert(0, "core")
    mcp = sorted({str(m) for m in raw.get("mcp") or [] if re.fullmatch(r"mcp__[A-Za-z0-9_-]{1,120}", str(m))} - {"mcp__crew"})
    return {"groups": groups, "mcp": mcp, "skills": bool(raw.get("skills", True))}


def builtins(crew) -> list[str]:
    """Built-in tool names Claude Code reported in init events (none until an agent has started). Only grows: an agent
    on a toolkit reports just its own tools."""
    return sorted(crew.tool_seen.get("builtin") or [])


def servers(crew) -> list[dict]:
    """Every MCP server any agent has seen: [{prefix, name, tools, status}], from init events (kept across restarts)."""
    out: dict[str, dict] = {}
    for a in crew.agents.values():
        for m in a.get("mcp") or []:
            if not m.get("name") or m["name"] == "crew":
                continue
            p = prefix(m["name"])
            e = out.setdefault(p, {"prefix": p, "name": m["name"], "tools": 0, "status": m.get("status")})
            if m.get("status") == "connected":
                e["status"] = "connected"
    for p, n in (crew.tool_seen.get("mcp") or {}).items():
        if p in out:
            out[p]["tools"] = n
    return sorted(out.values(), key=lambda e: (e["status"] != "connected", e["name"].lower()))


def tools_flag(crew, kit: dict) -> str:
    names = [t for g in GROUPS if g[0] in kit["groups"] for t in g[2]]
    known = builtins(crew)
    if "other" in kit["groups"]:
        mapped = {t for g in GROUPS for t in g[2]}
        names += [t for t in known if t not in mapped and t != "Skill"]
    if kit["skills"]:
        names.append("Skill")
    if known:  # names this Claude Code doesn't have (Task vs Agent, a tool from a newer version) are left out
        names = [t for t in names if t in known or t == "Skill"]
    return "--tools=" + ",".join(dict.fromkeys(names))


def note_init(crew, tools: list) -> None:
    """Remember the tools an init event listed: built-ins by name, MCP tools counted per server."""
    seen = crew.tool_seen
    b = set(seen.get("builtin") or [])
    counts: dict[str, int] = {}
    for t in tools:
        if t.startswith("mcp__"):
            p = "__".join(t.split("__")[:2])
            counts[p] = counts.get(p, 0) + 1
        else:
            b.add(t)
    seen["builtin"] = sorted(b)
    m = seen.setdefault("mcp", {})
    for p, n in counts.items():
        m[p] = max(m.get(p, 0), n)


def flags(crew, a: dict) -> list[str]:
    """CLI flags for an agent's toolkit; [] when it loads everything."""
    kit = clean(a.get("toolkit"))
    if not kit:
        return []
    out = [tools_flag(crew, kit)]
    drop = [s["prefix"] for s in servers(crew) if s["prefix"] not in kit["mcp"]]
    if drop:
        out += ["--disallowedTools", *drop]
    if not kit["skills"]:
        out.append("--disable-slash-commands")
    return out


# Words in an agent's role and tags that point at a kind of MCP server, matched against the server's name.
HINTS = {
    "devops|ado|jira|linear|github|gitlab": "work item ticket bug pbi triage intake pipeline deploy release ci cd pr review",
    "insight|monitor|log|datadog|sentry|grafana|azure": "incident telemetry alert outage ops monitor logs kql latency error",
    "365|teams|outlook|slack|mail|calendar": "teams outlook email slack chat intake standup report message share",
    "browser|chrome|playwright|puppeteer|figma": "ui design frontend browser screenshot css component page",
    "docs|notion|confluence|wiki": "docs documentation wiki report notes",
}


def recommend(crew, a: dict) -> dict:
    """A starting toolkit for an agent, from its role and tags."""
    text = (a.get("role") or "").lower() + " " + " ".join(a.get("tags") or [])
    words = set(re.findall(r"[a-z0-9]+", text))
    has = lambda *ws: bool(words & set(ws))
    groups = ["core", "subagents", "web"]
    if has("build", "builder", "test", "tester", "qa", "ops", "deploy", "ci", "feature", "bug", "eval", "incident", "pipeline"):
        groups.append("background")
    mcp = []
    for s in servers(crew):
        name = s["name"].lower()
        toks = {t for t in re.findall(r"[a-z0-9]+", name) if len(t) > 2} - {"claude", "mcp", "the", "for"}
        if toks & words:  # the role names the server ("uses the azure-devops MCP")
            mcp.append(s["prefix"])
            continue
        for pat, cue in HINTS.items():
            if re.search(pat, name) and words & set(cue.split()):
                mcp.append(s["prefix"])
                break
    return {"groups": groups, "mcp": mcp, "skills": True}


def estimate(crew, kit: dict | None) -> int:
    """Tokens a toolkit loads before any work, roughly."""
    if kit is None:
        return sum(g[3] for g in GROUPS) + SKILLS_TOKENS + sum(s["tools"] or 8 for s in servers(crew)) * MCP_TOOL_TOKENS
    n = sum(g[3] for g in GROUPS if g[0] in kit["groups"]) + (SKILLS_TOKENS if kit["skills"] else 0)
    return n + sum((s["tools"] or 8) * MCP_TOOL_TOKENS for s in servers(crew) if s["prefix"] in kit["mcp"])


def catalog(crew) -> dict:
    """What the agent editor offers."""
    return {"groups": [{"key": k, "label": l, "tools": t, "tokens": n, "hint": h} for k, l, t, n, h in GROUPS],
            "servers": servers(crew), "skills_tokens": SKILLS_TOKENS, "mcp_tool_tokens": MCP_TOOL_TOKENS}
