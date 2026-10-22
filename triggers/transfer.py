"""Export and import of triggers. A file holds the definition only: never the webhook token (a new one is made on
import) and none of the runtime state (what fired, what was seen, when it last ran, errors). Imported triggers arrive
turned off, so nothing starts paid work before you have looked at it."""
from __future__ import annotations

from .base import get, now

FILE_VERSION = 1
MAX_ITEMS = 100
SECRET_SPEC_KEYS = ("token",)


def _workflow(crew, ref):
    return next((w for w in crew.config.get("workflows", []) if w["id"] == ref or w["name"].lower() == str(ref).lower()), None)


def export_file(crew, ids) -> dict:
    """The file for the chosen triggers (all when ids is empty). Workflows are named, not numbered: ids mean nothing elsewhere."""
    chosen = [t for t in crew.triggers if not ids or t["id"] in ids]
    if not chosen:
        return {"error": "no triggers to export"}
    items = []
    for tr in chosen:
        spec = {k: v for k, v in (tr.get("spec") or {}).items() if k not in SECRET_SPEC_KEYS and not k.startswith("_")}
        task = dict(tr.get("task") or {})
        for d in (spec, task):
            if d.get("workflow"):
                d["workflow"] = (crew._workflow(d["workflow"]) or {}).get("name") or d["workflow"]
        items.append({"name": tr["name"], "kind": tr["kind"], "enabled": bool(tr.get("enabled")), "spec": spec, "task": task})
    return {"ok": True, "file": {"crew": "triggers", "version": FILE_VERSION, "items": items}}


def import_triggers(crew, items, update: bool) -> dict:
    """Add triggers from an exported file (lock held), through the same validation as trigger_add. A name that exists is
    updated or skipped. Everything arrives off. A trigger aimed at a workflow or agent this crew lacks is skipped."""
    if not isinstance(items, list) or not items:
        return {"error": "no triggers in the file"}
    if len(items) > MAX_ITEMS:
        return {"error": f"at most {MAX_ITEMS} triggers per import"}
    added, updated, skipped = [], [], []
    for it in items:
        name = str(it.get("name") or "").strip()[:60] if isinstance(it, dict) else ""
        label = name or (it.get("kind") if isinstance(it, dict) else None) or "?"
        trig = get(it.get("kind")) if isinstance(it, dict) else None
        if not trig:
            skipped.append({"name": label, "why": "unknown trigger type"})
            continue
        spec = {k: v for k, v in (it.get("spec") if isinstance(it.get("spec"), dict) else {}).items() if k not in SECRET_SPEC_KEYS}
        task = dict(it.get("task")) if isinstance(it.get("task"), dict) else {}
        for d in (spec, task):
            if d.get("workflow") and (w := _workflow(crew, d["workflow"])):
                d["workflow"] = w["id"]
        old = next((t for t in crew.triggers if name and t["name"].lower() == name.lower()), None)
        if old and not update:
            skipped.append({"name": old["name"], "why": "already exists"})
            continue
        if old and old["kind"] != it["kind"]:
            skipped.append({"name": old["name"], "why": f"exists with another type ({old['kind']})"})
            continue
        err, cl = crew._clean_trigger({"name": name, "kind": it["kind"], "enabled": False, "spec": spec, "task": task}, old)
        if err:
            skipped.append({"name": label, "why": err})
            continue
        if old:
            keep = trig.keeps_seen
            for k in [k for k in old if k.startswith("_")] + ["minute", "checked", "error"] + ([] if keep else ["seen"]):
                old.pop(k, None)  # new settings, new baseline
            old.update(cl | {"name": old["name"]})
            tr = old
            updated.append(tr["name"])
        else:
            tr = cl | {"id": f"t{crew.next_trig}", "fired": 0, "last": None, "created": now()}
            crew.next_trig += 1
            crew.triggers.append(tr)
            added.append(tr["name"])
        tr["next"] = None  # off
    if added or updated:
        crew.event("trigger", "imported triggers (all off): " + ", ".join(
            x for x in (f"{len(added)} new" if added else "", f"{len(updated)} updated" if updated else "") if x))
    return {"ok": True, "added": added, "updated": updated, "skipped": skipped}
