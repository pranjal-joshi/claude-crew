"""Event trigger: a task reached review or done, or its agent hit an error."""
from __future__ import annotations

from .base import MAX_CHAIN, Trigger, register

EVENT_ON = ["review", "done", "error"]


@register
class Event(Trigger):
    kind, order = "event", 20

    def validate(self, sp, old=None):
        if sp.get("on") not in EVENT_ON:
            return "pick when: review, done or error", {}
        return None, {"on": sp["on"], "tag": sp.get("tag") or "", "agent": sp.get("agent") or ""}

    def on_task_event(self, crew, tr, t, col, summary):
        sp = tr["spec"]
        if sp.get("on") != col:
            return
        if (sp.get("tag") and sp["tag"] != t["tag"]) or (sp.get("agent") and sp["agent"] != t.get("who")):
            return
        if (t.get("source") or {}).get("trigger") == tr["id"] or t.get("depth", 0) >= MAX_CHAIN:
            return  # no self-loops, no runaway chains
        w = crew.agents.get(t.get("who") or "")
        branch = w["branch"] if w and w.get("branch") else ""
        crew._fire(tr, {"id": t["id"], "title": t["title"], "tag": t["tag"], "who": t.get("who") or "",
                        "summary": (summary if summary is not None else t.get("summary") or "")[:3000],
                        "branch": branch, "commit": t.get("commit") or "",
                        "merge": f"git merge {branch}" if branch and t.get("commit") else "",
                        "depth": t.get("depth", 0) + 1}, f"#{t['id']} reached {col}")
