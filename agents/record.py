"""Agent records: defaults, validation, and the roster actions (configure, add, rename, remove, reorder, import).
Functions take the Crew first and run with its lock held."""
from __future__ import annotations

import re
from pathlib import Path

from .util import git

CONTEXTS = ["fresh", "keep"]
MODELS = ["fable", "opus", "sonnet", "haiku"]          # aliases the CLI resolves to the latest of each family
EFFORTS = ["low", "medium", "high", "xhigh", "max"]
NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9-]{0,23}")
RESERVED = {"boss", "you", "anyone", "auto"}
ICON_RE = re.compile(r"[a-z][a-z0-9-]{0,23}")  # a key from the UI's icon set
COLOR_RE = re.compile(r"#[0-9a-fA-F]{6}")
CONFIG_KEYS = ("model", "effort", "context", "role", "tags", "icon", "color", "off")
ROLE_MAX = 4000  # an agent role goes into its system prompt: room for a real brief, not a novel


def clean_tags(tags) -> list[str]:
    if isinstance(tags, str):
        tags = re.split(r"[,\s]+", tags)
    out = []
    for t in tags or []:
        t = re.sub(r"[^a-z0-9-]", "", str(t).lower())[:20]
        if t and t not in out:
            out.append(t)
    return out[:8]


def config_change(key: str, value) -> str:
    """How a configure event names one changed setting."""
    if key == "off":
        return "turned off: takes no new work" if value else "turned on"
    if key in ("role", "tags", "icon", "color"):
        return f"{key} updated"
    if key == "effort":
        return f"{value or 'default'} effort"
    return f"{key} {value or 'default'}" if key == "context" else (value or "default model")


def new_agent(crew, spec: dict) -> dict:
    return {
        "name": spec["name"], "role": spec.get("role", ""), "tags": list(spec.get("tags", [])),
        "model": spec.get("model") or None, "effort": spec.get("effort") or None,
        "icon": spec.get("icon") if ICON_RE.fullmatch(str(spec.get("icon") or "")) else None,
        "color": spec.get("color") if COLOR_RE.fullmatch(str(spec.get("color") or "")) else None,
        "off": bool(spec.get("off")),  # off: takes no new work; a task it is running finishes
        "status": "idle", "task": None, "session": None,
        "cost": 0.0, "proc_cost": 0.0, "log": [], "activity": "", "since": None,
        "cwd": str(crew.project), "branch": None, "error": None, "launched": None,
        "tokens": {"in": 0, "out": 0, "cache": 0}, "last_msg": None, "mcp": [], "skills": 0,
        "subs": {},  # live subagents: tool_use id -> {label, type, since}
        "bg": {},    # background shells and monitors in its Claude process: tool_use id -> {id, cmd, kind, since, task, out}
        # fresh: a new Claude session per task (send-back keeps it). keep: one session across tasks.
        "context": spec.get("context") if spec.get("context") in CONTEXTS else crew.config.get("context", "fresh"),
        "last_task": None,
        "denials": [],       # tool calls the permission mode refused, waiting in the inbox
        "budget_paused": False,
        "ctx": 0,           # tokens in the context window at the last model call
        "compacting": None,  # status to return to after /compact
    }

def attach_worktree(crew, a: dict) -> None:
    wt = crew.work_root / "wt" / crew.key / a["name"]
    if crew.git and (wt / ".git").exists():
        a["cwd"], a["branch"] = str(wt), f"crew/{a['name']}"

def roster(crew) -> list[dict]:
    """The agents as crew.json keeps them."""
    return [{"name": a["name"], "role": a["role"], "tags": a["tags"], "model": a["model"],
             "effort": a["effort"], "context": a["context"],
             **{k: a[k] for k in ("icon", "color", "off") if a.get(k)}} for a in crew.agents.values()]


def configure(crew, a: dict, p: dict) -> dict | None:
    before = {k: a.get(k) for k in CONFIG_KEYS}
    if "model" in p:
        if p["model"] and p["model"] not in MODELS:
            return {"error": f"unknown model {p['model']}"}
        a["model"] = p["model"] or None
    if "effort" in p:
        if p["effort"] and p["effort"] not in EFFORTS:
            return {"error": f"unknown effort {p['effort']}"}
        a["effort"] = p["effort"] or None
    if "role" in p:
        a["role"] = str(p["role"]).strip()[:ROLE_MAX]
    if "tags" in p:
        a["tags"] = clean_tags(p["tags"])
    if p.get("context") in CONTEXTS:
        a["context"] = p["context"]
    if "off" in p:
        a["off"] = bool(p["off"])
    if "icon" in p:
        if p["icon"] and not ICON_RE.fullmatch(str(p["icon"])):
            return {"error": "unknown icon"}
        a["icon"] = p["icon"] or None
    if "color" in p:
        if p["color"] and not COLOR_RE.fullmatch(str(p["color"])):
            return {"error": "color must be #rrggbb"}
        a["color"] = p["color"] or None
    crew._save_config()
    if a["status"] != "running" and crew._alive(a["name"]) and tuple(a.get("launched") or ()) != crew._launch_key(a):
        crew._kill_proc(a["name"])  # restarts with the new settings on its next task (session kept)
    changed = [k for k in CONFIG_KEYS if a.get(k) != before[k]]
    if changed:  # one event per real change, naming what changed (no repeat of the model line on every edit)
        crew.event("config", f"{a['name']} · " + ", ".join(config_change(k, a.get(k)) for k in changed), a["name"])


def add(crew, p: dict) -> dict | None:
    name = (p.get("name") or "").strip()
    if not NAME_RE.fullmatch(name):
        return {"error": "name: 1-24 letters, digits or dashes"}
    if name.lower() in RESERVED:
        return {"error": f"{name} is reserved"}
    if name.lower() in {n.lower() for n in crew.agents}:  # worktree folders are case-insensitive on Windows
        return {"error": f"{name} already exists"}
    if p.get("model") and p["model"] not in MODELS or p.get("effort") and p["effort"] not in EFFORTS:
        return {"error": "unknown model or effort"}
    crew.agents[name] = new_agent(crew, {"name": name, "role": (p.get("role") or "").strip()[:ROLE_MAX],
                                         "tags": clean_tags(p.get("tags") or []),
                                         "model": p.get("model"), "effort": p.get("effort"),
                                         "icon": p.get("icon"), "color": p.get("color")})
    attach_worktree(crew, crew.agents[name])
    crew._save_config()
    crew.event("agent", f"+ {name} joined the crew", name)


def rename(crew, a: dict, p: dict) -> dict | None:
    old, new = a["name"], (p.get("name") or "").strip()
    if new == old:
        return {"ok": True}
    if not NAME_RE.fullmatch(new):
        return {"error": "name: 1-24 letters, digits or dashes"}
    if new.lower() in RESERVED:
        return {"error": f"{new} is reserved"}
    if new.lower() != old.lower() and new.lower() in {n.lower() for n in crew.agents}:
        return {"error": f"{new} already exists"}
    if a["status"] == "running":
        return {"error": f"{old} is working: stop it first"}
    if crew._git_busy(old):
        return {"error": f"{old} has a commit or merge in progress: one moment"}
    crew._kill_proc(old)
    if crew.git and a.get("branch"):  # keep the worktree and branch in step with the name
        path, branch = crew.work_root / "wt" / crew.key / new, f"crew/{new}"
        case_only = new.lower() == old.lower()  # Windows paths and git refs ignore case: go via a temp name
        if Path(a["cwd"]).exists() and Path(a["cwd"]).name != new:
            for hop in ([path.with_name(new + "-renaming")] if case_only else []) + [path]:
                r = git(crew.project, "worktree", "move", a["cwd"], str(hop))
                if r.returncode != 0:
                    return {"error": "could not move the worktree: " + r.stderr.strip()[:200]}
                a["cwd"] = str(hop)
        if branch != a["branch"]:
            for hop in ([f"crew/{new}-renaming"] if case_only else []) + [branch]:
                r = git(crew.project, "branch", "-m", a["branch"], hop)
                if r.returncode != 0:
                    crew.log(a, "sys", f"kept branch {a['branch']}: {r.stderr.strip()[:120]}")
                    break
                a["branch"] = hop
    a["name"] = new
    a["session"] = None  # sessions are filed by folder; the next message starts fresh
    crew._forget_sessions(old)
    renamed = [(new if n == old else n, ag) for n, ag in crew.agents.items()]
    crew.agents.clear()
    crew.agents.update(renamed)
    for tk in crew.tasks:
        for k in ("who", "pinned"):
            if tk.get(k) == old:
                tk[k] = new
        for h in tk.get("history") or []:
            if h.get("who") == old:
                h["who"] = new
        if (tk.get("handoff") or {}).get("from") == old:
            tk["handoff"]["from"] = new
    for tr in crew.triggers:
        for part in (tr.get("task") or {}, tr.get("spec") or {}):
            if part.get("agent") == old:
                part["agent"] = new
    for w in crew.config.get("workflows", []):
        for st in w.get("steps", []):
            if st.get("agent") == old:
                st["agent"] = new
    crew._save_config()
    crew.log(a, "sys", f"renamed from {old}")
    crew.event("agent", f"{old} is now {new}", new)


def remove(crew, a: dict) -> dict | None:
    if len(crew.agents) <= 1:
        return {"error": "keep at least one agent"}
    if crew._git_busy(a["name"]):
        return {"error": f"{a['name']} has a commit or merge in progress: one moment"}
    crew._kill_proc(a["name"])
    for tk in crew.tasks:
        if tk.get("who") == a["name"] or tk.get("pinned") == a["name"]:
            if tk["col"] == "doing":
                tk["col"] = "backlog"
            if tk["col"] == "backlog":
                tk["who"] = tk["pinned"] = None
    del crew.agents[a["name"]]
    crew._save_config()
    crew.event("agent", f"− {a['name']} removed" + (f"; its work stays on {a['branch']}" if a.get("branch") else ""))


def reorder(crew, p: dict) -> dict | None:
    names = [n for n in p.get("names") or [] if n in crew.agents]
    order = names + [n for n in crew.agents if n not in names]
    ordered = [(n, crew.agents[n]) for n in order]
    crew.agents.clear()
    crew.agents.update(ordered)
    crew._save_config()


def import_agents(crew, specs, update: bool) -> dict:
    """Add agents from an exported file (lock held). A name that already exists is updated or skipped."""
    if not isinstance(specs, list) or not specs:
        return {"error": "no agents in the file"}
    if len(specs) > 100:
        return {"error": "at most 100 agents per import"}
    added, updated, skipped = [], [], []
    have = {n.lower(): n for n in crew.agents}
    for spec in specs:
        name = str(spec.get("name") or "").strip() if isinstance(spec, dict) else ""
        if not isinstance(spec, dict) or not NAME_RE.fullmatch(name):
            skipped.append({"name": name or "?", "why": "name: 1-24 letters, digits or dashes"})
            continue
        if name.lower() in RESERVED:
            skipped.append({"name": name, "why": "reserved name"})
            continue
        if spec.get("model") and spec["model"] not in MODELS or spec.get("effort") and spec["effort"] not in EFFORTS:
            skipped.append({"name": name, "why": "unknown model or effort"})
            continue
        fields = {"role": str(spec.get("role") or "").strip()[:ROLE_MAX], "tags": clean_tags(spec.get("tags") or []),
                  "model": spec.get("model") or None, "effort": spec.get("effort") or None}
        for k, rx in (("icon", ICON_RE), ("color", COLOR_RE)):
            fields[k] = spec.get(k) if rx.fullmatch(str(spec.get(k) or "")) else None
        if spec.get("context") in CONTEXTS:
            fields["context"] = spec["context"]
        if name.lower() in have:
            if not update:
                skipped.append({"name": have[name.lower()], "why": "already exists"})
                continue
            a = crew.agents[have[name.lower()]]
            a.update(fields)
            if a["status"] != "running" and crew._alive(a["name"]) and tuple(a.get("launched") or ()) != crew._launch_key(a):
                crew._kill_proc(a["name"])  # restarts with the new settings on its next task (session kept)
            updated.append(a["name"])
        else:
            crew.agents[name] = new_agent(crew, {"name": name, **fields})
            attach_worktree(crew, crew.agents[name])
            have[name.lower()] = name
            added.append(name)
    if added or updated:
        crew._save_config()
        crew.event("agent", "imported " + ", ".join(
            x for x in (f"{len(added)} new" if added else "", f"{len(updated)} updated" if updated else "") if x)
            + f" agent{'s' if len(added) + len(updated) > 1 else ''}")
    return {"ok": True, "added": added, "updated": updated, "skipped": skipped}
