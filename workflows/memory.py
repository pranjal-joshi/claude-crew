"""Crew memory: short facts about the project that agents save with the crew tool `remember` (how a deploy is confirmed,
which pipeline steps are non-blocking, who owns what) and every agent reads when it starts a task, or when the facts
changed since it last looked. A fact has a key: saving the same key again replaces it, so facts stay current instead of
piling up. Kept in the project's state file, never in the repo. Functions take the Crew and run with its lock held."""
from __future__ import annotations

import re
import time

MAX_FACTS = 50
TEXT_MAX = 400
KEY = re.compile(r"^[a-z0-9][a-z0-9._-]{0,39}$")


def slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:40].strip("-") or "fact"


def save(crew, p: dict, by: str) -> dict:
    """remember: add a fact, replace the one with the same key, or forget it (empty text). {ok, key} or {error}."""
    text = " ".join(str(p.get("text") or "").split())
    key = str(p.get("key") or "").strip().lower() or (slug(text) if text else "")
    if not key:
        return {"error": "give `text` to save, or the `key` of a fact to forget"}
    if not KEY.match(key):
        return {"error": "key: 1-40 lowercase letters, digits, . _ or -, e.g. deploy-confirmed-by"}
    old = next((f for f in crew.facts if f["key"] == key), None)
    if not text:
        if not old:
            return {"error": f"no fact {key}"}
        crew.facts.remove(old)
        crew.facts_v += 1
        crew.event("memory", f"🧠 {by} forgot {key}", by if by in crew.agents else None)
        return {"ok": True, "key": key, "forgot": True}
    if len(text) > TEXT_MAX:
        return {"error": f"keep it under {TEXT_MAX} characters: one fact, the rule and why"}
    if not old and len(crew.facts) >= MAX_FACTS:
        return {"error": f"memory is full ({MAX_FACTS}): replace an outdated fact (same key) or forget one. Keys: "
                         + ", ".join(f["key"] for f in crew.facts)}
    task = (crew.agents.get(by) or {}).get("task")
    fact = {"key": key, "text": text, "by": by, "t": time.time(), **({"task": task} if task else {})}
    if old:
        crew.facts[crew.facts.index(old)] = fact
    else:
        crew.facts.append(fact)
    crew.facts_v += 1
    crew.event("memory", f"🧠 {by} {'updated' if old else 'saved'} {key}: {text[:100]}", by if by in crew.agents else None)
    return {"ok": True, "key": key, "replaced": bool(old)}


def block(crew, t: dict) -> str:
    """The memory section for a task's next message: every fact when the task hasn't seen this version yet, else ''."""
    if t.get("facts_v") == crew.facts_v:
        return ""
    t["facts_v"] = crew.facts_v
    if not crew.facts:
        return ""
    lines = [f"- [{f['key']}] {f['text']} ({f['by']}, {time.strftime('%d %b', time.localtime(f['t']))})" for f in crew.facts]
    return ("\n\n---\nCrew memory: facts teammates saved about this project. Use them before guessing; if one turns out "
            "wrong or outdated, fix it with the crew tool `remember` (same key).\n" + "\n".join(lines))
