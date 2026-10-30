"""Lanes: an agent works up to `max_parallel` tasks at once, one Claude process per lane.

Lane 1 is the agent's own record (its worktree, branch crew/<name>). Lane n is a record of the same shape keyed
"<name>.<n>" with its own worktree, branch crew/<name>.<n>, process, session and task. Agent names have no dots, so a
lane key never clashes with an agent. A lane takes its settings (role, model, tools…) from the agent and shares the
agent's log and blocked-call inbox.

Claude sessions resume from any folder, so a task can move between lanes. Only a card waiting in Review with
uncommitted changes keeps its lane, because those changes live in that worktree; a card without changes holds none.
So a card waiting for you never stops the agent from taking other work. Functions take the Crew first, lock held."""
from __future__ import annotations

import itertools
import threading

from . import record
from .util import git

SYNC = ("role", "deliverable", "tags", "engine", "fallback", "model", "effort", "toolkit", "off", "icon", "color", "max_parallel")  # read from the agent
SHARED = ("mcp", "skills", "skill_names", "plugins", "commands")  # what the agent's chip shows; any lane's start updates it
PARALLEL_MAX = 8


def cap(a: dict) -> int:
    """How many tasks the agent may run at once."""
    try:
        n = int(a.get("max_parallel") or 1)
    except (TypeError, ValueError):
        n = 1
    return max(1, min(PARALLEL_MAX, n))


def of(crew, a: dict) -> list[dict]:
    """The agent's lanes, lane 1 (the agent itself) first."""
    if a.get("kind") == "boss":
        return [a]
    return [a, *crew.lanes.get(a["name"], [])]


def every(crew) -> list[dict]:
    return [ln for a in crew.agents.values() for ln in of(crew, a)]


def find(crew, key) -> dict | None:
    """A lane (or the boss, or an agent's lane 1) by its process key."""
    if key == "boss":
        return crew.boss
    if key in crew.agents:
        return crew.agents[key]
    name = str(key or "").rsplit(".", 1)[0]
    return next((ln for ln in crew.lanes.get(name, []) if ln["key"] == key), None)


def ensure(crew, key) -> dict | None:
    """The lane with this key, opened (worktree made here) if it isn't known yet, e.g. after a restart."""
    if ln := find(crew, key):
        return ln
    name, _, n = str(key or "").rpartition(".")
    if n.isdigit() and int(n) > 1 and (a := crew.agents.get(name)):
        return new(crew, a, int(n), wait=True)
    return None


def holder(crew, t: dict) -> dict | None:
    """The lane working on task t, or holding it in Review."""
    a = crew.agents.get(t.get("who") or "")
    return next((ln for ln in (of(crew, a) if a else every(crew)) if ln.get("task") == t["id"]), None)


def of_task(crew, t: dict) -> dict | None:
    """The lane task t last ran on (its worktree has the task's changes), if that lane is still known."""
    return find(crew, t.get("lane") or t.get("who"))


def sync(crew, lane: dict) -> None:
    """A lane runs with the agent's current settings."""
    if "n" in lane and (a := crew.agents.get(lane["name"])):
        lane.update({k: a.get(k) for k in SYNC})
        lane["context"] = "fresh"  # one long session belongs to lane 1; other lanes start a session per task


def new(crew, a: dict, n: int | None = None, wait: bool = False) -> dict:
    """Open lane n (the lowest free number by default). Its worktree is made on a thread unless wait is set."""
    used = {ln["n"] for ln in crew.lanes.get(a["name"], [])}
    n = n or next(i for i in itertools.count(2) if i not in used)
    lane = record.new_agent(crew, {"name": a["name"], "context": "fresh", **{k: a.get(k) for k in SYNC}})
    lane.update(key=f"{a['name']}.{n}", n=n, log=a["log"], denials=a["denials"], **{k: a.get(k) for k in SHARED})
    crew.lanes.setdefault(a["name"], []).append(lane)
    crew.lanes[a["name"]].sort(key=lambda x: x["n"])
    prepare(crew, lane, wait)
    return lane


def prepare(crew, lane: dict, wait: bool = False) -> None:
    """Give a lane its worktree. A folder from an earlier run is reused at once; a new one is made on a thread (git
    worktree add takes a while on a big repo, and the lock must not be held that long) and the lane takes work once
    it is ready. wait=True makes it here instead, for a lane you asked for."""
    if not crew.git:
        return
    path = crew.work_root / "wt" / crew.key / lane["key"]
    if (path / ".git").exists():
        lane["cwd"], lane["branch"] = str(path), f"crew/{lane['key']}"
    elif wait:
        crew._ensure_worktree(lane)
    else:
        lane["preparing"] = True
        threading.Thread(target=_make, args=(crew, lane, path), daemon=True).start()


def _make(crew, lane: dict, path) -> None:
    branch = f"crew/{lane['key']}"
    with crew.git_mutex:  # one git change to the project's repo at a time (merges and pushes take it too)
        path.parent.mkdir(parents=True, exist_ok=True)
        git(crew.project, "worktree", "prune")
        have = git(crew.project, "rev-parse", "--verify", "-q", f"refs/heads/{branch}").returncode == 0
        r = git(crew.project, "worktree", "add", str(path), branch) if have else \
            git(crew.project, "worktree", "add", "-b", branch, str(path), "HEAD")
    with crew.lock:
        lane.pop("preparing", None)
        if r.returncode == 0:
            lane["cwd"], lane["branch"] = str(path), branch
            crew.log(lane, "sys", f"lane {lane['n']} ready: {branch}")
        else:
            crew.lanes[lane["name"]] = [x for x in crew.lanes.get(lane["name"], []) if x is not lane]
            crew.log(lane, "error", f"lane {lane['n']}: worktree failed: {r.stderr.strip()[:300]}")
            crew.event("error", f"{lane['name']}: could not make a worktree for lane {lane['n']}", lane["name"])
    crew.touch()


def free(crew, a: dict) -> list[dict]:
    """Lanes ready for a task: idle, holding nothing, worktree made, no git action on them."""
    return [ln for ln in of(crew, a) if ln["status"] == "idle" and not ln.get("task") and not ln.get("preparing")
            and not crew._lane_busy(ln)]


def running(crew, a: dict) -> int:
    return sum(1 for ln in of(crew, a) if ln["status"] == "running")


def room(crew, a: dict) -> bool:
    """May another lane open? Lanes holding a card in Review don't count: they wait on you, not on the work."""
    return sum(1 for ln in of(crew, a) if ln["status"] != "review") < cap(a)


def for_user(crew, a: dict, t: dict) -> tuple[dict | None, str]:
    """The lane for work you ask for on task t (send back, Address review, a card button): the lane holding t, else a
    lane holding nothing, else a new one. What you ask for may go past max_parallel. (None, why) when t is busy."""
    if h := holder(crew, t):
        return (None, f"{a['name']} is already working on #{t['id']}") if h["status"] == "running" else (h, "")
    if any(ln.get("addressing") == t["id"] and ln["status"] == "running" for ln in of(crew, a)):
        return None, f"{a['name']} is already working on #{t['id']}: try again when it's done"
    if a["status"] == "killed":
        return None, f"{a['name']} is killed; respawn it first"
    ok = [ln for ln in of(crew, a) if ln["status"] != "running" and not ln.get("task") and not ln.get("preparing")
          and not crew._lane_busy(ln)]
    return (ok[0] if ok else new(crew, a, wait=True)), ""


def totals(crew, a: dict) -> tuple[float, dict]:
    """Cost and tokens across the agent's lanes."""
    ls = of(crew, a)
    return sum(ln["cost"] for ln in ls), {k: sum(ln["tokens"][k] for ln in ls) for k in ("in", "cache", "out")}


def view(crew, a: dict) -> dict:
    """What the board shows for an agent with more than one lane: the lane worth looking at, and every lane."""
    ls = of(crew, a)
    if len(ls) == 1:
        return {}
    run = [ln for ln in ls if ln["status"] == "running"]
    show = a if a["status"] in ("paused", "killed") else (
        run[0] if run else next((ln for ln in ls if ln["status"] == "error"), None)
        or next((ln for ln in ls if ln["status"] == "review"), None) or a)
    cost, tokens = totals(crew, a)
    subs = {k: v for ln in ls for k, v in (ln.get("subs") or {}).items()}
    bg = {k: v for ln in ls for k, v in (ln.get("bg") or {}).items()}
    return {**{k: show.get(k) for k in ("status", "task", "activity", "since", "error", "ctx", "ctx_max")},
            "subs": subs, "bg": bg, "cost": cost, "tokens": tokens, "running": len(run),
            "lanes": [{"key": ln["key"], "n": ln.get("n", 1), "status": ln["status"], "task": ln.get("task"),
                       "activity": ln.get("activity", ""), "since": ln.get("since"), "branch": ln.get("branch"),
                       "preparing": bool(ln.get("preparing"))} for ln in ls]}
