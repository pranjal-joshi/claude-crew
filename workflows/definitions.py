"""Workflow definitions: validation and the save, delete and run-now actions. They live in crew.json under "workflows"."""
from __future__ import annotations

import re
from pathlib import Path

from .runs import find, groups, start_run


# A step's gate: "approve" waits for you, "auto" goes on by itself, "until" keeps asking its agent to check again (every
# `every` minutes, at most `checks` times) until it calls decide: for a PR to merge, a deployment to land, CI to finish.
GATES = ("approve", "auto", "until")
UNTIL_EVERY, UNTIL_CHECKS = 30, 96
MAX_PAR = 6  # steps in one parallel group


def clean(crew, p: dict, allow_missing_agents: bool = False) -> tuple[str | None, dict]:
    """Validate a workflow. An import may name agents this crew lacks (allow_missing_agents): flagged, not refused."""
    name = str(p.get("name") or "").strip()[:60]
    if not name:
        return "name the workflow", {}
    steps = []
    for k, st in enumerate(p.get("steps") or []):
        agent = st.get("agent") if isinstance(st, dict) else None
        if not isinstance(agent, str) or not agent.strip() or (agent not in crew.agents and not allow_missing_agents):
            return f"step {k + 1}: pick an agent", {}
        gate = st.get("gate") if st.get("gate") in GATES else "approve"
        step = {"agent": agent, "title": str(st.get("title") or "").strip()[:200], "details": str(st.get("details") or "")[:3000], "gate": gate}
        if k and st.get("par"):  # runs at the same time as the step before it
            step["par"] = True
        if gate == "until":  # ask again every `every` minutes until the agent decides, at most `checks` times
            try:
                step["every"] = min(1440, max(5, int(st.get("every") or UNTIL_EVERY)))
                step["checks"] = min(500, max(1, int(st.get("checks") or UNTIL_CHECKS)))
            except (TypeError, ValueError):
                return f"step {k + 1}: numbers only for how often and how many times to check", {}
        steps.append(step)
    if not steps:
        return "add at least one step", {}
    if any(len(g) > MAX_PAR for g in groups(steps)):
        return f"at most {MAX_PAR} steps run at the same time", {}
    o = p.get("output") or {}
    kind = o.get("kind") if o.get("kind") in ("board", "report", "webhook", "workflow") else "board"
    out = {"kind": kind}
    if kind == "report":
        out["path"] = str(o.get("path") or "crew-reports").strip().strip("/\\")[:120] or "crew-reports"
        if ".." in Path(out["path"]).parts or Path(out["path"]).is_absolute():
            return "the report folder must be inside the project", {}
    elif kind == "webhook":
        out["url"] = str(o.get("url") or "").strip()[:500]
        if not re.match(r"https?://", out["url"]):
            return "the webhook needs an http(s) URL", {}
    elif kind == "workflow":
        out["workflow"] = str(o.get("workflow") or "")
        if not find(crew, out["workflow"]) or out["workflow"] == p.get("id"):
            return "pick another workflow to start", {}
    return None, {"name": name, "description": str(p.get("description") or "").strip()[:300], "steps": steps[:12], "output": out,
                  "subagents": bool(p.get("subagents"))}


def save(crew, p: dict) -> dict | None:
    err, clean = crew._clean_workflow(p)
    if err:
        return {"error": err}
    flows = crew.config.setdefault("workflows", [])
    if w := find(crew, p.get("id")):
        w.update(clean)
    else:
        ids = [int(x["id"][1:]) for x in flows if x["id"][1:].isdigit()]
        flows.append(w := clean | {"id": f"w{max(ids, default=0) + 1}"})
    crew._save_config()
    crew.touch()
    return {"ok": True, "id": w["id"]}


def delete(crew, p: dict) -> dict | None:
    crew.config["workflows"] = [w for w in crew.config["workflows"] if w["id"] != p["id"]]
    crew._save_config()


def run(crew, p: dict) -> dict | None:
    w = find(crew, p.get("id"))
    if w.get("off"):
        return {"error": f"{w['name']} is turned off: turn it on to run it"}
    title = (p.get("title") or "").strip()
    if not title:
        return {"error": "what should this run work on?"}
    start_run(crew, w, title, p.get("details") or "")


# ---- export / import: a file holds definitions only (no runs, no ids that mean something elsewhere) ----------------------
FILE_VERSION = 1
MAX_ITEMS = 100


def export_file(crew, ids) -> dict:
    """The file for the chosen workflows (all when ids is empty). A chain output names its target by name, not id."""
    flows = [w for w in crew.config.get("workflows", []) if not ids or w["id"] in ids]
    if not flows:
        return {"error": "no workflows to export"}
    items = []
    for w in flows:
        out = dict(w.get("output") or {"kind": "board"})
        if out.get("kind") == "workflow":
            out["workflow"] = (find(crew, out.get("workflow")) or {}).get("name") or ""
        items.append({"name": w["name"], "description": w.get("description", ""), "off": bool(w.get("off")), "subagents": bool(w.get("subagents")),
                      "steps": [{k: st[k] for k in ("agent", "title", "details", "gate", "every", "checks", "par") if k in st} for st in w["steps"]],
                      "output": out})
    return {"ok": True, "file": {"crew": "workflows", "version": FILE_VERSION, "items": items}}


def import_workflows(crew, items, update: bool) -> dict:
    """Add workflows from an exported file (lock held). A name that exists is updated or skipped. Steps naming an agent
    that is not in the crew are accepted and reported under "missing": the run would fail at that step."""
    if not isinstance(items, list) or not items:
        return {"error": "no workflows in the file"}
    if len(items) > MAX_ITEMS:
        return {"error": f"at most {MAX_ITEMS} workflows per import"}
    flows = crew.config.setdefault("workflows", [])
    added, updated, skipped, missing = [], [], [], []

    def chains(it) -> bool:
        return isinstance(it, dict) and isinstance(it.get("output"), dict) and it["output"].get("kind") == "workflow"

    for it in sorted(items, key=chains):  # chain targets first, so "start another workflow" finds them
        name = str(it.get("name") or "").strip()[:60] if isinstance(it, dict) else ""
        if not name:
            skipped.append({"name": "?", "why": "no name"})
            continue
        old = next((w for w in flows if w["name"].lower() == name.lower()), None)
        if old and not update:
            skipped.append({"name": old["name"], "why": "already exists"})
            continue
        p = dict(it, name=name, id=old["id"] if old else None)
        if chains(it):  # the file names the target workflow; this crew knows it by id
            ref = str(it["output"].get("workflow") or "")
            tgt = next((w for w in flows if w["name"].lower() == ref.lower() or w["id"] == ref), None)
            p["output"] = dict(it["output"], workflow=tgt["id"] if tgt else "")
        err, cl = clean(crew, p, allow_missing_agents=True)
        if err:
            skipped.append({"name": name, "why": err})
            continue
        if old:
            old.update(cl)
            w = old
            updated.append(w["name"])
        else:
            ids = [int(x["id"][1:]) for x in flows if x["id"][1:].isdigit()]
            flows.append(w := cl | {"id": f"w{max(ids, default=0) + 1}"})
            if it.get("off"):
                w["off"] = True
            added.append(name)
        gone = sorted({st["agent"] for st in w["steps"] if st["agent"] not in crew.agents})
        if gone:
            missing.append({"name": w["name"], "agents": gone})
    if added or updated:
        crew._save_config()
        crew.event("config", "imported workflows: " + ", ".join(
            x for x in (f"{len(added)} new" if added else "", f"{len(updated)} updated" if updated else "") if x))
    return {"ok": True, "added": added, "updated": updated, "skipped": skipped, "missing": missing}
