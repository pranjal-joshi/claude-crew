"""Agent records: defaults, validation, and the roster actions (configure, add, rename, remove, reorder, import).
Functions take the Crew first and run with its lock held."""
from __future__ import annotations

import re
from pathlib import Path

from . import engines, toolkit
from .util import git

CONTEXTS = ["fresh", "keep"]
MODELS = ["fable", "opus", "sonnet", "haiku"]          # aliases the CLI resolves to the latest of each family
EFFORTS = ["low", "medium", "high", "xhigh", "max"]
NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9-]{0,23}")
RESERVED = {"boss", "you", "anyone", "auto"}
ICON_RE = re.compile(r"[a-z][a-z0-9-]{0,23}")  # a key from the UI's icon set
COLOR_RE = re.compile(r"#[0-9a-fA-F]{6}")
CONFIG_KEYS = ("engine", "fallback", "model", "effort", "context", "role", "deliverable", "tags", "icon", "color", "off", "toolkit", "max_parallel")
PARALLEL_MAX = 8  # tasks one agent may run at once (agents/lanes.py)
ROLE_MAX = 4000  # an agent role goes into its system prompt: room for a real brief, not a novel
DELIVERABLE_MAX = 500  # what every reply of the agent is: one line, also repeated on chat follow-ups


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
    if key == "toolkit":
        return "tools: " + ("everything" if not value else f"{len(value['groups'])} groups, {len(value['mcp'])} MCP, skills {'on' if value['skills'] else 'off'}")
    if key in ("role", "deliverable", "tags", "icon", "color"):
        return f"{key} updated"
    if key == "max_parallel":
        return f"up to {value} task{'s' if value != 1 else ''} at once"
    if key == "effort":
        return f"{value or 'default'} effort"
    if key == "engine":
        return {"opencode": "runs on OpenCode", "copilot": "runs on GitHub Copilot"}.get(value, "runs on Claude Code")
    if key == "fallback":
        return "falls back to Claude Code" if value else "no fallback"
    return f"{key} {value or 'default'}" if key == "context" else (value or "default model")


def clean_parallel(v) -> int | None:
    """max_parallel from the UI, the boss or a file: 1..PARALLEL_MAX, None when it isn't a number."""
    try:
        n = int(v)
    except (TypeError, ValueError):
        return None
    return n if 1 <= n <= PARALLEL_MAX else None


def new_agent(crew, spec: dict) -> dict:
    return {
        "name": spec["name"], "key": spec["name"],  # key: its process, worktree and pidfile name (a lane's is <name>.<n>)
        "max_parallel": clean_parallel(spec.get("max_parallel")) or 1, "role": spec.get("role", ""), "tags": list(spec.get("tags", [])),
        "deliverable": str(spec.get("deliverable") or "").strip()[:DELIVERABLE_MAX],  # "": the preset's, else a generic one
        "model": spec.get("model") or None, "effort": spec.get("effort") or None,
        "engine": spec.get("engine") if spec.get("engine") in engines.ENGINES[1:] else None,  # None: Claude Code
        "fallback": spec.get("fallback") if spec.get("fallback") in engines.FALLBACKS else None,
        "icon": spec.get("icon") if ICON_RE.fullmatch(str(spec.get("icon") or "")) else None,
        "color": spec.get("color") if COLOR_RE.fullmatch(str(spec.get("color") or "")) else None,
        "off": bool(spec.get("off")),  # off: takes no new work; a task it is running finishes
        "toolkit": toolkit.clean(spec.get("toolkit")),  # None: loads everything; else the tools it keeps (toolkit.py)
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
    wt = crew.work_root / "wt" / crew.key / a["key"]
    if crew.git and (wt / ".git").exists():
        a["cwd"], a["branch"] = str(wt), f"crew/{a['key']}"

def roster(crew) -> list[dict]:
    """The agents as crew.json keeps them."""
    return [{"name": a["name"], "role": a["role"], "tags": a["tags"], "model": a["model"],
             "effort": a["effort"], "context": a["context"],
             **{k: a[k] for k in ("engine", "fallback", "deliverable", "icon", "color", "off", "toolkit") if a.get(k)},
             **({"max_parallel": a["max_parallel"]} if a.get("max_parallel", 1) > 1 else {})} for a in crew.agents.values()]


def configure(crew, a: dict, p: dict) -> dict | None:
    before = {k: a.get(k) for k in CONFIG_KEYS}
    engine = p.get("engine", engines.of(a)) or "claude"
    if engine not in engines.ENGINES:
        return {"error": f"unknown engine {engine}"}
    if "fallback" in p and p["fallback"] and p["fallback"] not in engines.FALLBACKS:
        return {"error": f"unknown fallback {p['fallback']}"}
    model = p["model"] if "model" in p else (a["model"] if engine == engines.of(a) else None)  # a new engine: its default model
    if not engines.model_ok(engine, model, MODELS):
        return {"error": f"unknown model {model} for {engine}"}
    a["engine"] = None if engine == "claude" else engine
    a["model"] = model or None
    if "fallback" in p:
        a["fallback"] = p["fallback"] or None
    if a["engine"] is None:
        a.pop("on_fallback", None)
    if "effort" in p:
        if p["effort"] and p["effort"] not in EFFORTS:
            return {"error": f"unknown effort {p['effort']}"}
        a["effort"] = p["effort"] or None
    if "role" in p:
        a["role"] = str(p["role"]).strip()[:ROLE_MAX]
    if "deliverable" in p:
        a["deliverable"] = str(p["deliverable"] or "").strip()[:DELIVERABLE_MAX]
    if "tags" in p:
        a["tags"] = clean_tags(p["tags"])
    if p.get("context") in CONTEXTS:
        a["context"] = p["context"]
    if "off" in p:
        a["off"] = bool(p["off"])
    if "max_parallel" in p:
        if (n := clean_parallel(p["max_parallel"])) is None:
            return {"error": f"max_parallel: 1 to {PARALLEL_MAX}"}
        a["max_parallel"] = n
    if "toolkit" in p:  # null: everything; "recommended": picked from its role and tags; else {groups, mcp, skills}
        a["toolkit"] = toolkit.recommend(crew, a) if p["toolkit"] == "recommended" else toolkit.clean(p["toolkit"])
    if "icon" in p:
        if p["icon"] and not ICON_RE.fullmatch(str(p["icon"])):
            return {"error": "unknown icon"}
        a["icon"] = p["icon"] or None
    if "color" in p:
        if p["color"] and not COLOR_RE.fullmatch(str(p["color"])):
            return {"error": "color must be #rrggbb"}
        a["color"] = p["color"] or None
    crew._save_config()
    for ln in crew._lanes(a):
        if ln["status"] != "running" and crew._alive(ln["key"]) and tuple(ln.get("launched") or ()) != crew._launch_key(ln):
            crew._kill_proc(ln["key"])  # restarts with the new settings on its next task (session kept)
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
    engine = p.get("engine") or "claude"
    if engine not in engines.ENGINES or not engines.model_ok(engine, p.get("model"), MODELS) or p.get("effort") and p["effort"] not in EFFORTS:
        return {"error": "unknown model or effort"}
    crew.agents[name] = new_agent(crew, {"name": name, "role": (p.get("role") or "").strip()[:ROLE_MAX], "deliverable": p.get("deliverable"),
                                         "tags": clean_tags(p.get("tags") or []),
                                         "model": p.get("model"), "effort": p.get("effort"), "engine": p.get("engine"),
                                         "fallback": p.get("fallback"), "icon": p.get("icon"), "color": p.get("color"), "max_parallel": p.get("max_parallel")})
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
    lanes = crew._lanes(a)
    if any(ln["status"] == "running" or ln.get("preparing") for ln in lanes):
        return {"error": f"{old} is working: stop it first"}
    if crew._git_busy(old):
        return {"error": f"{old} has a commit or merge in progress: one moment"}
    for ln in lanes:
        crew._kill_proc(ln["key"])
    moves = {}  # old lane key -> new: worktrees, branches and the tasks that point at them follow the name
    wt = crew.work_root / "wt" / crew.key
    if crew.git:  # other lanes' folders from earlier runs move too
        known = {ln["key"]: ln for ln in lanes}
        extra = sorted(f.name for f in wt.glob(f"{old}.*") if f.name.rsplit(".", 1)[-1].isdigit()) if wt.is_dir() else []
        for key in [ln["key"] for ln in lanes if ln.get("branch")] + [k for k in extra if k not in known]:
            ln = known.get(key) or {"cwd": str(wt / key), "branch": f"crew/{key}"}
            nkey = new + key[len(old):]
            if err := _move_lane(crew, ln, wt / nkey, f"crew/{nkey}", nkey.lower() == key.lower()):
                return {"error": err}
            moves[key] = nkey
    for ln in lanes:
        nkey = moves.setdefault(ln["key"], new + ln["key"][len(old):])
        ln["key"], ln["name"] = nkey, new
        ln["session"] = None  # sessions follow their tasks; the next chat message starts fresh
    for tk in crew.tasks:
        if (k := tk.get("lane") or (tk.get("who") == old and old)) in moves:
            tk["lane"] = moves[k]
            for f, val in (("cwd", str(wt / moves[k])), ("branch", f"crew/{moves[k]}")):
                if tk.get(f):
                    tk[f] = val
    if old in crew.lanes:
        crew.lanes[new] = crew.lanes.pop(old)
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


def _move_lane(crew, ln: dict, path: Path, branch: str, case_only: bool) -> str | None:
    """Move a lane's worktree and rename its branch. Windows paths and git refs ignore case, so a case-only rename
    goes via a temp name. Returns why it failed, or None."""
    if Path(ln["cwd"]).exists() and Path(ln["cwd"]).name != path.name:
        for hop in ([path.with_name(path.name + "-renaming")] if case_only else []) + [path]:
            r = git(crew.project, "worktree", "move", ln["cwd"], str(hop))
            if r.returncode != 0:
                return "could not move the worktree: " + r.stderr.strip()[:200]
            ln["cwd"] = str(hop)
    if branch != ln["branch"]:
        for hop in ([branch + "-renaming"] if case_only else []) + [branch]:
            r = git(crew.project, "branch", "-m", ln["branch"], hop)
            if r.returncode != 0:
                if "log" in ln:
                    crew.log(ln, "sys", f"kept branch {ln['branch']}: {r.stderr.strip()[:120]}")
                break
            ln["branch"] = hop
    return None


def remove(crew, a: dict) -> dict | None:
    if len(crew.agents) <= 1:
        return {"error": "keep at least one agent"}
    if crew._git_busy(a["name"]):
        return {"error": f"{a['name']} has a commit or merge in progress: one moment"}
    for ln in crew._lanes(a):
        crew._kill_proc(ln["key"])
    crew.lanes.pop(a["name"], None)
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
        eng = spec.get("engine") or "claude"
        if eng not in engines.ENGINES or not engines.model_ok(eng, spec.get("model"), MODELS) or spec.get("effort") and spec["effort"] not in EFFORTS:
            skipped.append({"name": name, "why": "unknown model or effort"})
            continue
        fields = {"role": str(spec.get("role") or "").strip()[:ROLE_MAX], "tags": clean_tags(spec.get("tags") or []),
                  "model": spec.get("model") or None, "effort": spec.get("effort") or None,
                  "engine": None if eng == "claude" else eng,
                  "fallback": spec.get("fallback") if spec.get("fallback") in engines.FALLBACKS else None}
        for k, rx in (("icon", ICON_RE), ("color", COLOR_RE)):
            fields[k] = spec.get(k) if rx.fullmatch(str(spec.get(k) or "")) else None
        if n := clean_parallel(spec.get("max_parallel")):
            fields["max_parallel"] = n
        if spec.get("context") in CONTEXTS:
            fields["context"] = spec["context"]
        if spec.get("deliverable"):
            fields["deliverable"] = str(spec["deliverable"]).strip()[:DELIVERABLE_MAX]
        if name.lower() in have:
            if not update:
                skipped.append({"name": have[name.lower()], "why": "already exists"})
                continue
            a = crew.agents[have[name.lower()]]
            a.update(fields)
            for ln in crew._lanes(a):
                if ln["status"] != "running" and crew._alive(ln["key"]) and tuple(ln.get("launched") or ()) != crew._launch_key(ln):
                    crew._kill_proc(ln["key"])  # restarts with the new settings on its next task (session kept)
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
