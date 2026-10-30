"""Insights: a day-by-day history of what the crew did, for the Insights tab (the last 30 days shown, KEEP_DAYS kept).
Counters are bumped where the thing happens (tokens as Claude Code streams them, a PR as it joins a card, a watch as an
agent sets it, a task as it reaches Review or Done) and saved in the project's state file, so a restart or Clear board
keeps them. PRs are kept by URL, so one PR counts once however often agents cite it; Crew asks GitHub, read-only
through `gh`, whether the open ones merged. A state file from before Insights is backfilled once from its tasks.
Functions take the Crew and run with its lock held, except _check, which runs on its own thread."""
from __future__ import annotations

import threading
import time

from . import merge_watch

KEEP_DAYS = 90
SHOW_DAYS = 30
MAX_PRS = 500
CHECK_EVERY = 600  # seconds between merge checks of the open PRs Crew tracks
CHECK_MAX = 20     # PRs per check
# What a day counts. Tokens and cost are summed, the rest are events.
KEYS = ("tok_in", "tok_cache", "tok_out", "cost", "tasks_started", "tasks_review", "tasks_done", "tasks_failed",
        "approved", "sent_back", "prs_opened", "prs_merged", "merges", "address_reviews", "watches", "watch_checks",
        "watch_wakes", "runs", "fired", "handoffs", "alerts")
_busy = threading.Lock()
_last = {"t": 0.0}


def day_of(ts: float | None = None) -> str:
    return time.strftime("%Y-%m-%d", time.localtime(ts) if ts else time.localtime())


def empty() -> dict:
    return {"since": time.time(), "days": {}, "prs": {}}


def _day(crew, ts: float | None = None) -> dict:
    return crew.hist["days"].setdefault(day_of(ts), {})


def bump(crew, key: str, n: float = 1) -> None:
    if getattr(crew, "hist_quiet", False) or not n:
        return
    d = _day(crew)
    d[key] = d.get(key, 0) + n


def tokens(crew, agent: str, n: dict, model: str = "default") -> None:
    """Tokens one Claude Code (or OpenCode) event reported: n has in, cache and out; model is the one that answered."""
    if getattr(crew, "hist_quiet", False):
        return
    d = _day(crew)
    for k in ("in", "cache", "out"):
        if n.get(k):
            d["tok_" + k] = d.get("tok_" + k, 0) + n[k]
    if total := sum(n.values()):
        by = d.setdefault("by", {})
        by[agent] = by.get(agent, 0) + total
        m = d.setdefault("models", {}).setdefault(str(model)[:80], {"in": 0, "cache": 0, "out": 0, "agents": {}})
        for k in ("in", "cache", "out"):
            m[k] += n.get(k, 0)
        m["agents"][agent] = m["agents"].get(agent, 0) + total


def pr(crew, url: str, raised: bool) -> None:
    """A PR joined a card. raised: the crew opened it (Open PR, or an agent's own gh), not a PR its brief handed it."""
    if getattr(crew, "hist_quiet", False) or not url:
        return
    url = url.rstrip("/")
    e = crew.hist["prs"].get(url)
    if e is None:
        e = crew.hist["prs"][url] = {"seen": day_of(), "raised": False, "state": "OPEN"}
    if raised and not e["raised"]:
        e["raised"] = True
        bump(crew, "prs_opened")


def pr_state(crew, url: str, state: str | None) -> None:
    """What GitHub (or Azure Repos) says a tracked PR is now. A merge counts once, on the day Crew learns of it."""
    if not url or not (e := crew.hist["prs"].get(url.rstrip("/"))) or not state:
        return
    state = state.upper()
    if state == "MERGED" and not e.get("merged"):
        e["merged"] = day_of()
        bump(crew, "prs_merged")
        if e["raised"]:
            bump(crew, "prs_merged_raised")
    e["state"] = state


def prune(crew) -> None:
    cut = day_of(time.time() - KEEP_DAYS * 86400)
    h = crew.hist
    h["days"] = {k: v for k, v in h["days"].items() if k >= cut}
    keep = sorted(((k, v) for k, v in h["prs"].items() if v.get("seen", "") >= cut or v.get("state") == "OPEN"),
                  key=lambda kv: kv[1].get("seen", ""))[-MAX_PRS:]
    h["prs"] = dict(keep)


def load(crew, saved: dict | None) -> dict:
    """The saved history, or one backfilled from the board's tasks for a state file from before Insights."""
    if isinstance(saved, dict) and isinstance(saved.get("days"), dict):
        cut = day_of(time.time() - KEEP_DAYS * 86400)
        saved["days"] = {k: v for k, v in saved["days"].items() if k >= cut}
        return {"since": saved.get("since") or time.time(), "days": saved["days"], "prs": saved.get("prs") or {},
                **({"backfilled": saved["backfilled"]} if saved.get("backfilled") else {})}
    h = empty()
    first = time.time()
    for t in crew.tasks:
        when = t.get("finished") or t.get("started") or t.get("created")
        if not when:
            continue
        first = min(first, t.get("created") or when)
        d = h["days"].setdefault(day_of(when), {})
        tok = int(t.get("tokens") or 0)
        if tok:  # a task keeps only its total: counted as input, its agent gets it
            d["tok_in"] = d.get("tok_in", 0) + tok
            by = d.setdefault("by", {})
            by[t.get("who") or "?"] = by.get(t.get("who") or "?", 0) + tok
        if t.get("cost"):
            d["cost"] = d.get("cost", 0) + float(t["cost"])
        if t.get("started"):
            ds = h["days"].setdefault(day_of(t["started"]), {})
            ds["tasks_started"] = ds.get("tasks_started", 0) + 1
        if t["col"] == "done" and t.get("finished"):
            d["tasks_done"] = d.get("tasks_done", 0) + 1
        if (t.get("merged") or {}).get("commit"):
            d["merges"] = d.get("merges", 0) + 1
        brief = str(t.get("details") or "")
        for u in dict.fromkeys(x.rstrip("/") for x in [t.get("pr"), *(t.get("prs") or [])] if x):
            if u in h["prs"]:
                continue
            raised = u == (t.get("pr") or "").rstrip("/") or u not in brief
            h["prs"][u] = {"seen": day_of(when), "raised": raised, "state": "OPEN"}
            if raised:
                d["prs_opened"] = d.get("prs_opened", 0) + 1
    if crew.tasks:
        h["since"], h["backfilled"] = first, time.time()  # days before this are estimates from the tasks
    return h


def tick(crew) -> None:
    """Start one background merge check of the open GitHub PRs Crew tracks (lock held)."""
    if time.time() - _last["t"] < CHECK_EVERY:
        return
    _last["t"] = time.time()
    prune(crew)
    urls = [u for u, e in sorted(crew.hist["prs"].items(), key=lambda kv: kv[1].get("seen", ""), reverse=True)
            if e.get("state") == "OPEN" and merge_watch.GH_PR.match(u)][:CHECK_MAX]
    if urls and _busy.acquire(blocking=False):
        threading.Thread(target=_check, args=(crew, urls), daemon=True).start()


def _check(crew, urls: list[str]) -> None:
    try:
        got = []
        for u in urls:
            try:
                got.append((u, merge_watch.read(u, crew.project).get("state")))
            except Exception:  # gh missing, signed out, offline: asked again next time
                continue
        with crew.lock:
            for u, st in got:
                pr_state(crew, u, st)
        if got:
            crew.touch()
    finally:
        _busy.release()


def public(crew, days: int = SHOW_DAYS) -> dict:
    """The last `days` days, oldest first, every key filled in, with window totals and tokens by agent."""
    days = max(1, min(int(days or SHOW_DAYS), KEEP_DAYS))
    h = crew.hist
    t0 = time.time()
    out, tot, by, models = [], {k: 0 for k in (*KEYS, "prs_merged_raised")}, {}, {}
    for i in range(days - 1, -1, -1):
        k = day_of(t0 - i * 86400)
        d = h["days"].get(k) or {}
        row = {"day": k, **{x: d.get(x, 0) for x in tot}}
        row["cost"] = round(row["cost"], 4)
        row["tokens"] = row["tok_in"] + row["tok_cache"] + row["tok_out"]
        row["by"] = dict(d.get("by") or {})
        out.append(row)
        for x in tot:
            tot[x] += d.get(x, 0)
        for a, n in row["by"].items():
            by[a] = by.get(a, 0) + n
        row["models"] = {}
        for name, m in (d.get("models") or {}).items():
            row["models"][name] = m["in"] + m["cache"] + m["out"]
            w = models.setdefault(name, {"model": name, "in": 0, "cache": 0, "out": 0, "agents": {}})
            for k in ("in", "cache", "out"):
                w[k] += m[k]
            for a, n in m["agents"].items():
                w["agents"][a] = w["agents"].get(a, 0) + n
    tot["cost"] = round(tot["cost"], 4)
    tot["tokens"] = tot["tok_in"] + tot["tok_cache"] + tot["tok_out"]
    prs = h["prs"]
    by_model = [m | {"tokens": m["in"] + m["cache"] + m["out"], "agents": sorted(m["agents"].items(), key=lambda kv: -kv[1])}
                for m in models.values()]
    return {"days": out, "totals": tot, "by_agent": sorted(by.items(), key=lambda kv: -kv[1]),
            "by_model": sorted(by_model, key=lambda m: -m["tokens"]),
            "prs_open": sum(1 for e in prs.values() if e.get("state") == "OPEN" and e.get("raised")),
            "since": h.get("since"), "backfilled": h.get("backfilled")}
