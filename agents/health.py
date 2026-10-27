"""Self-checks and bounded recovery. Functions take the Crew first and run with its lock held.

check() looks for states no code path should leave behind (an agent "running" with no process, a task "doing" that
nobody is working on, a status or column Crew doesn't know) and repairs them. A state must look wrong on two checks
REPAIR_AFTER seconds apart before it is touched, so a change that is half-way through on another thread is left alone.

A turn that failed for a passing reason (the API overloaded, a timeout, the process dying) is retried on its own a
few times with a growing wait; after that it waits for you, with an alert."""
from __future__ import annotations

import re
import time

AGENT_STATUSES = {"idle", "running", "review", "error", "paused", "killed"}
COLUMNS = {"backlog", "doing", "review", "done"}
REPAIR_AFTER = 20  # seconds a state must stay wrong before it is repaired
RETRY_WAITS = (30, 120)  # seconds before each automatic retry; len() is how many a task gets
TRANSIENT_RE = re.compile(r"overloaded|rate.?limit|too many requests|\b(?:429|500|502|503|504|529)\b|timed? ?out|timeout|"
                          r"econnreset|econnrefused|socket hang up|connection (?:error|reset|refused|closed)|network error|"
                          r"exited unexpectedly|internal server error|service unavailable", re.I)


def now() -> float:
    return time.time()


def _suspect(crew, key: str, wrong: bool) -> bool:
    """True when the state named key has been wrong for REPAIR_AFTER seconds."""
    seen = crew._suspects
    if not wrong:
        seen.pop(key, None)
        return False
    first = seen.setdefault(key, now())
    if now() - first < REPAIR_AFTER:
        return False
    seen.pop(key, None)
    return True


def check(crew) -> int:
    """Repair impossible states. Returns how many were repaired."""
    fixed: list[tuple[str | None, str]] = []
    owners = {}  # task id -> the agent working on it
    for a in [*crew._all_lanes(), crew.boss]:  # every lane of every agent: each has its own process and task
        n, key = a["name"], a["key"]
        if a["status"] not in AGENT_STATUSES:
            fixed.append((n, f"{n} had an unknown status {a['status']!r}: set to idle"))
            a.update(status="idle", activity="")
        if a.get("task") is not None and not crew._task(a["task"]) and _suspect(crew, f"gone:{key}", True):
            fixed.append((n, f"{n} pointed at #{a['task']}, which no longer exists"))
            if a["status"] in ("running", "review"):
                crew._kill_proc(key)
                a.update(status="idle", activity="", since=None)
            a["task"] = None
        elif a.get("task") is None or crew._task(a["task"]):
            _suspect(crew, f"gone:{key}", False)
        running = a["status"] == "running"
        if _suspect(crew, f"noproc:{key}", running and not crew._alive(key) and not crew._lane_busy(a)):
            fixed.append((n, f"{n} was marked running but has no Claude process"))
            a.update(status="error", activity="", error="its Claude process was gone")
            if (t := crew._task(a.get("task"))) and t["col"] == "doing":
                t.update(col="backlog", who=t.get("pinned") or n)
            a["task"] = None
        if running:
            for tid in (a.get("task"), a.get("addressing")):
                if tid is not None:
                    owners[tid] = n
    for t in crew.tasks:
        if t.get("col") not in COLUMNS:
            fixed.append((None, f"#{t['id']} was in an unknown column {t.get('col')!r}: moved to the backlog"))
            t["col"] = "backlog"
        orphan = t["col"] == "doing" and t["id"] not in owners and t["id"] not in crew._busy
        if _suspect(crew, f"doing:{t['id']}", orphan):
            fixed.append((t.get("who"), f"#{t['id']} was in progress with nobody working on it: back to the backlog"))
            t["col"] = t.pop("reviewing_from", None) or "backlog"
            t.pop("reviewing", None)
            t.pop("acting", None)
    for who, msg in fixed:
        crew.event("repair", msg, who if who in crew.agents else None)
    return len(fixed)


def transient(msg: str | None) -> bool:
    return bool(TRANSIENT_RE.search(str(msg or "")))


def schedule_retry(crew, a: dict, t: dict | None, msg: str) -> bool:
    """A turn on task t failed with msg. If that looks passing and t has retries left, put t back for the same agent
    after a wait and return True; the agent shows the error until then. Otherwise False (out of retries: an alert)."""
    if not t or not transient(msg) or t.get("hold"):
        return False
    n = int(t.get("retries") or 0)
    if n >= len(RETRY_WAITS):
        crew.raise_alert(f"#{t['id']} failed {n + 1} times", "warning",
                         f"{a['name']}: {msg[:300]}\nAutomatic retries are used up; send it back or respawn {a['name']} to try again.",
                         agent=a["name"], key=f"retry:{t['id']}")
        return False
    wait = RETRY_WAITS[n]
    t["retries"] = n + 1
    t.update(col="backlog", who=a["name"], retry_at=now() + wait)  # the dispatcher leaves it until then
    a["retry"] = {"task": t["id"], "at": now() + wait}
    crew.event("retry", f"#{t['id']} failed ({msg[:80]}): {a['name']} retries in {wait} s ({n + 1} of {len(RETRY_WAITS)})", a["name"])
    return True


def tick_retries(crew) -> bool:
    """Agents waiting to retry: once the wait is over, back to idle so the dispatcher hands them the task again."""
    moved = False
    for a in crew._all_lanes():
        r = a.get("retry")
        if not r or now() < r["at"]:
            continue
        a.pop("retry")
        if a["status"] == "error":
            a.update(status="idle", error=None)
            crew.log(a, "sys", f"retrying #{r['task']}")
            moved = True
    return moved


def succeeded(crew, t: dict | None) -> None:
    """A turn on t finished: its retry count starts again and a retry alert closes."""
    if t:
        t.pop("retry_at", None)
        if t.pop("retries", None):
            crew.resolve_alert(f"retry:{t['id']}", "a later try worked")
