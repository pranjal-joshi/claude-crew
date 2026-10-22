"""Workflow definitions: validation and the save, delete and run-now actions. They live in crew.json under "workflows"."""
from __future__ import annotations

import re
from pathlib import Path

from .runs import find, start_run


def clean(crew, p: dict) -> tuple[str | None, dict]:
    name = str(p.get("name") or "").strip()[:60]
    if not name:
        return "name the workflow", {}
    steps = []
    for k, st in enumerate(p.get("steps") or []):
        if st.get("agent") not in crew.agents:
            return f"step {k + 1}: pick an agent", {}
        steps.append({"agent": st["agent"], "title": str(st.get("title") or "").strip()[:200],
                      "details": str(st.get("details") or "")[:3000], "gate": "auto" if st.get("gate") == "auto" else "approve"})
    if not steps:
        return "add at least one step", {}
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
    return None, {"name": name, "description": str(p.get("description") or "").strip()[:300], "steps": steps[:12], "output": out}


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
