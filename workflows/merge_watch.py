"""Ready to merge: Crew asks GitHub, read-only through `gh`, whether the PR an open card works on can be merged now
(open, not a draft, required checks passing, reviews done, no conflicts: mergeStateStatus CLEAN). The card then shows a
green "Ready to merge" button so a person merges it on GitHub; Crew never merges. One `gh` call per PR every CHECK_EVERY
seconds, on a thread, outside the lock. Deploy cards (agents tagged deploy or cd) follow pipelines instead, and are left out."""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import threading
import time

CHECK_EVERY = 120
GH_PR = re.compile(r"^https://github\.com/[^/]+/[^/]+/pull/\d+$", re.I)
READY = ("CLEAN", "HAS_HOOKS")  # HAS_HOOKS: clean, with pre-receive hooks on the base branch
_busy = threading.Lock()


def pr_of(crew, t: dict) -> str | None:
    """The GitHub PR an open, non-deploy card works on."""
    if t["col"] not in ("backlog", "doing", "review"):
        return None
    tags = (crew.agents.get(t.get("who") or "") or {}).get("tags") or []
    if {"deploy", "cd"} & set(tags):
        return None
    url = next((u for u in reversed([t.get("pr"), *(t.get("prs") or [])]) if u and GH_PR.match(u.rstrip("/"))), None)
    return url.rstrip("/") if url else None


def read(url: str, cwd) -> dict:
    exe = shutil.which("gh")
    if not exe:
        raise RuntimeError("gh not found")
    r = subprocess.run([exe, "pr", "view", url, "--json", "number,state,isDraft,mergeStateStatus,reviewDecision"], cwd=cwd,
                       capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60, stdin=subprocess.DEVNULL)
    if r.returncode:
        raise RuntimeError((r.stderr or r.stdout).strip()[-200:])
    x = json.loads(r.stdout or "{}")
    ready = x.get("state") == "OPEN" and not x.get("isDraft") and x.get("mergeStateStatus") in READY
    return {"url": url, "number": x.get("number"), "state": x.get("state"), "merge": x.get("mergeStateStatus"),
            "review": x.get("reviewDecision") or "", "draft": bool(x.get("isDraft")), "ready": ready, "t": time.time()}


def tick(crew) -> None:
    """Start one background check of the PRs that are due (lock held)."""
    t0 = time.time()
    due = {}
    for t in crew.tasks:
        if (url := pr_of(crew, t)) and t0 - ((t.get("gh") or {}).get("t") or 0) >= CHECK_EVERY and (t.get("gh") or {}).get("url", url) == url:
            due.setdefault(url, []).append(t["id"])
        elif url and (t.get("gh") or {}).get("url") not in (None, url):
            t.pop("gh")  # the card moved on to another PR: check that one
    if due and _busy.acquire(blocking=False):
        for ids in due.values():
            for i in ids:
                crew._task(i).setdefault("gh", {})["t"] = t0  # not again before CHECK_EVERY, even if gh fails
        threading.Thread(target=_check, args=(crew, due), daemon=True).start()


def _check(crew, due: dict) -> None:
    from . import insights
    try:
        for url, ids in due.items():
            try:
                got = read(url, crew.project)
            except Exception as e:  # gh missing, signed out, offline: keep what we knew, try again later
                got = {"error": str(e)[:200], "t": time.time()}
            with crew.lock:
                insights.pr_state(crew, url, got.get("state"))  # the Insights tab counts merges
                for i in ids:
                    if not (t := crew._task(i)):
                        continue
                    was = (t.get("gh") or {}).get("ready")
                    t["gh"] = ({**(t.get("gh") or {}), **got} if "error" in got else got)
                    if got.get("ready") and not was:
                        crew.event("review", f"✓ PR #{got['number']} is ready to merge on GitHub (#{i})", t.get("who"))
                crew.touch()
    finally:
        _busy.release()
