"""One card per thing: a PR or a work item that an open card is already about has an owner, and news about it (the PR
merged, a pipeline run that carries it finished, an alert) goes to that card instead of starting new work. A ⟳ owner
waiting on its watch is woken with the news; any owner reads it at the start of its next turn. Functions take the Crew
and run with its lock held."""
from __future__ import annotations

import re
import time

AB = re.compile(r"\bAB#(\d+)\b")
OPEN = ("backlog", "doing", "review")
NEWS_MAX = 10


def pr_key(url: str) -> str:
    return "pr:" + url.strip().rstrip("/").lower()


def keys(crew, t: dict) -> set[str]:
    """What a card is about: its PRs and work items, its run's, and AB#n in its title."""
    run = next((r for r in crew.runs if r["id"] == (t.get("run") or {}).get("id")), None) or {}
    src = run.get("source") or {}
    out = {pr_key(u) for u in [t.get("pr"), *(t.get("prs") or []), src.get("pr")] if u}
    out |= {f"ab:{i}" for i in [(t.get("ado") or {}).get("id"), (run.get("ado") or {}).get("id"), (src.get("ado") or {}).get("id")] if i}
    out |= {f"ab:{n}" for n in AB.findall(t.get("title") or "")}
    return out


def owner(crew, want: set[str], skip: int | None = None) -> dict | None:
    """The open card about any of `want`: a step of a running run first, then the newest."""
    if not want:
        return None
    live = {r["id"] for r in crew.runs if r["status"] == "running"}
    found = [t for t in crew.tasks if t["col"] in OPEN and t["id"] != skip and keys(crew, t) & want]
    found.sort(key=lambda t: ((t.get("run") or {}).get("id") in live, t["id"]))
    return found[-1] if found else None


def attach(crew, t: dict, text: str, by: str) -> None:
    """Give a card news about what it owns, and wake it if it is waiting on a watch."""
    news = t.setdefault("news", [])
    news.append({"text": " ".join(text.split())[:600], "by": by, "t": time.time()})
    del news[:-NEWS_MAX]
    w = t.get("wait") or {}
    if t["col"] == "review" and w.get("blocked"):  # parked for a person: the news may unblock it, so its agent looks
        w.pop("blocked")
        t["col"] = "backlog"
    if t["col"] == "backlog" and w and not w.get("fixing"):
        w.update(wake="news", next=0)
    crew.event("trigger", f"📎 {by} → #{t['id']}: {text[:120]}", t.get("who"))
    crew.touch()


def block(t: dict) -> str:
    """The news section for a card's next message, once."""
    news = t.pop("news", None)
    if not news:
        return ""
    lines = [f"- {time.strftime('%H:%M', time.localtime(n['t']))} {n['by']}: {n['text']}" for n in news]
    return ("\n\n---\nNews about what this card owns, since you last looked (Crew attached it here instead of starting "
            "new work on it):\n" + "\n".join(lines))
