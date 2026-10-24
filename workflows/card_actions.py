"""Card buttons you define, kept in crew.json under "card_actions". Each one shows on the board cards that match its
`when` and, pressed, does one of three things: asks the card's agent to do something in the card's own session (a skill
such as /rebase, or a plain instruction), starts a workflow on the card, or hands the card to another agent as a new
task. A button never runs a shell command itself, so the boss may add them too (crew tools set_card_button and
delete_card_button)."""
from __future__ import annotations

from .runs import find, now, start_run

KINDS = ("agent", "workflow", "handoff")
COLS = ("backlog", "doing", "review", "done")
HAS = ("", "pr", "no_pr", "pipeline")
MAX_BUTTONS = 30
PLACEHOLDERS = ("id", "title", "pr", "pipeline", "branch", "agent", "summary")


def _list(crew) -> list[dict]:
    return crew.config.setdefault("card_actions", [])


def get(crew, bid) -> dict | None:
    return next((b for b in _list(crew) if b["id"] == bid or b["label"] == bid), None)


def _workflow(crew, ref) -> dict | None:
    return find(crew, ref) or next((w for w in crew.config.get("workflows", []) if w["name"] == ref), None)


def clean(crew, p: dict) -> tuple[str | None, dict]:
    """Validate a button. Agents named in `when` or a hand-off may be missing (an agent renamed later): the button just
    never matches, or its run says so."""
    label = str(p.get("label") or "").strip()[:30]
    if not label:
        return "name the button (its label)", {}
    w = p.get("when") if isinstance(p.get("when"), dict) else {}
    tags = [str(x).strip() for x in (w.get("tags") or []) if str(x).strip()][:10] if isinstance(w.get("tags"), list) else []
    cols = [c for c in (w.get("cols") or []) if c in COLS] if isinstance(w.get("cols"), list) else []
    has = w.get("has") if w.get("has") in HAS else ""
    when = {"tags": tags, "cols": cols or ["review", "done"], "has": has, "agent": str(w.get("agent") or "").strip()[:40]}
    d = p.get("do") if isinstance(p.get("do"), dict) else {}
    kind = d.get("kind")
    if kind not in KINDS:
        return f"what should the button do? kind is one of {', '.join(KINDS)}", {}
    do = {"kind": kind}
    if kind == "agent":
        do["text"] = str(d.get("text") or "").strip()[:2000]
        if not do["text"]:
            return "write what the card's agent should do, e.g. /rebase {pr}", {}
    elif kind == "workflow":
        wf = _workflow(crew, str(d.get("workflow") or ""))
        if not wf:
            return "pick a workflow to start", {}
        do["workflow"] = wf["id"]
    else:
        to = str(d.get("to") or "").strip()
        if not to:
            return "hand off to whom? an agent's name, or auto", {}
        do["to"] = "auto" if to.lower() == "auto" else to
        do["tag"] = str(d.get("tag") or "").strip()[:40]
        do["text"] = str(d.get("text") or "").strip()[:2000]
    return None, {"label": label, "icon": str(p.get("icon") or "").strip()[:4], "hint": str(p.get("hint") or "").strip()[:200],
                  "when": when, "do": do}


def save(crew, p: dict) -> dict:
    err, b = clean(crew, p)
    if err:
        return {"error": err}
    items = _list(crew)
    cur = next((x for x in items if x["id"] == p.get("id")), None) if p.get("id") else None
    if not cur and any(x["label"].lower() == b["label"].lower() for x in items):
        cur = next(x for x in items if x["label"].lower() == b["label"].lower())  # the same label replaces it
    replaced = bool(cur)
    if cur:
        cur.update(b)
    else:
        if len(items) >= MAX_BUTTONS:
            return {"error": f"at most {MAX_BUTTONS} card buttons"}
        ids = [int(x["id"][1:]) for x in items if x["id"][1:].isdigit()]
        items.append(cur := {"id": f"b{max(ids, default=0) + 1}"} | b)
    crew._save_config()
    crew.event("config", f"card button {cur['label']} saved")
    crew.touch()
    return {"ok": True, "id": cur["id"], "replaced": replaced}


def delete(crew, p: dict) -> dict:
    b = get(crew, p.get("id"))
    if not b:
        return {"error": "no such card button"}
    crew.config["card_actions"] = [x for x in _list(crew) if x is not b]
    crew._save_config()
    crew.event("config", f"card button {b['label']} deleted")
    crew.touch()
    return {"ok": True}


def pr_of(t: dict) -> str:
    return t.get("pr") or (t.get("prs") or [""])[-1]


def matches(crew, b: dict, t: dict) -> bool:
    """Whether the button shows on this card. The page runs the same test (cardBtnOk in index.html)."""
    w = b["when"]
    if t["col"] not in w["cols"] or t.get("reviewing"):
        return False
    a = crew.agents.get(t.get("who") or "")
    if w["agent"] and (t.get("who") or "") != w["agent"]:
        return False
    if w["tags"] and not (a and set(a.get("tags") or []) & set(w["tags"])):
        return False
    pr = pr_of(t)
    return {"": True, "pr": bool(pr), "no_pr": not pr, "pipeline": bool(t.get("pipes"))}[w["has"]]


def fill(text: str, t: dict, crew) -> str:
    a = crew.agents.get(t.get("who") or "") or {}
    vals = {"id": f"#{t['id']}", "title": t["title"], "pr": pr_of(t), "pipeline": (t.get("pipes") or [""])[-1],
            "branch": a.get("branch") or "", "agent": t.get("who") or "", "summary": (t.get("summary") or "")[:4000]}
    for k in PLACEHOLDERS:
        text = text.replace("{" + k + "}", vals[k])
    return text


def run(crew, t: dict, p: dict) -> dict:
    """Press a button on task t (lock held)."""
    b = get(crew, p.get("id"))
    if not b:
        return {"error": "no such card button"}
    if not matches(crew, b, t):
        return {"error": f"{b['label']} doesn't apply to #{t['id']} now"}
    do = b["do"]
    if do["kind"] == "agent":
        w = crew.agents.get(t.get("who") or "")
        if not w:
            return {"error": f"#{t['id']} has no agent to ask"}
        if w["status"] == "running":
            return {"error": f"{w['name']} is working: try again when it's done"}
        if w["status"] == "killed":
            return {"error": f"{w['name']} is killed; respawn it first"}
        crew._task_session(w, t)
        if not crew._start(w):
            return {"error": w["error"]}
        text = fill(do["text"], t, crew)
        crew.log(w, "you", text)
        if not crew._send(w, text):
            w.update(status="error", activity="")
            return {"error": w["error"]}
        w.update(status="running", since=now(), activity=f"{b['label']}…", error=None, addressing=t["id"])
        t["reviewing"], t["reviewing_from"], t["col"], t["acting"] = now(), t["col"], "doing", b["id"]  # In progress until the turn ends
        crew.event("tell", f"→ {w['name']}: {b['label']} on #{t['id']}", w["name"])
        return {"ok": True, "agent": w["name"]}
    if do["kind"] == "workflow":
        wf = find(crew, do["workflow"])
        if not wf:
            return {"error": "its workflow was deleted: edit the button"}
        if wf.get("off"):
            return {"error": f"{wf['name']} is turned off: turn it on to run it"}
        links = "\n".join(x for x in (f"PR: {pr_of(t)}" if pr_of(t) else "", f"Pipeline run: {(t.get('pipes') or [''])[-1]}" if t.get("pipes") else "") if x)
        details = f"From card #{t['id']} ({t.get('who') or 'unassigned'}).\n{links}\n\n{t.get('details') or ''}\n\n{t.get('summary') or ''}".strip()
        r = start_run(crew, wf, t["title"], details, source={"kind": "card", "task": t["id"]})
        crew.event("run", f"{b['label']} on #{t['id']}: ⛓ {wf['name']} {r['id']}")
        return {"ok": True, "run": r["id"]}
    to = do["to"]
    if to != "auto" and to not in crew.agents:
        return {"error": f"no agent called {to}: edit the button"}
    if to != "auto" and crew.agents[to].get("off"):
        return {"error": f"{to} is turned off"}
    who = None if to == "auto" else to
    tag = do["tag"] if do["tag"] in crew._all_tags() else (crew.agents[who]["tags"] or ["feature"])[0] if who else "feature"
    src = crew.agents.get(t.get("who") or "") or {}
    details = fill(do["text"], t, crew) if do["text"] else ""
    details += f"\n\n---\nFrom #{t['id']} {t['title']}" + (f" ({t['who']})" if t.get("who") else "") + "."
    if pr_of(t):
        details += f"\nPR: {pr_of(t)}"
    if src.get("branch"):
        details += f"\nIts branch is {src['branch']}: `git merge {src['branch']}` if you need its changes."
    if t.get("summary"):
        details += f"\n\nIts summary:\n{t['summary'][:4000]}"
    nt = {"id": crew.next_id, "title": t["title"][:200], "details": details.strip()[:8000], "tag": tag, "col": "backlog",
          "who": who, "pinned": who, "created": now(), "depth": t.get("depth", 0) + 1,
          "handoff": {"from": "you", "task": t["id"]}}
    crew.next_id += 1
    crew.tasks.append(nt)
    crew.event("handoff", f"↪ {b['label']}: #{t['id']} → {who or 'auto (' + tag + ')'} as #{nt['id']}", who)
    return {"ok": True, "task": nt["id"]}
