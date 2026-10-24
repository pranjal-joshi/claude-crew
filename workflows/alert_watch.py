"""Alerts that clear themselves: an open alert whose link is a failed pipeline run (Azure Pipelines or GitHub Actions) is
resolved once a later run of the same pipeline on the same branch succeeds. Other open alerts that name that pipeline go
with it, so one red master raised by three agents clears once. Read-only through `az` and `gh`; checks run on a thread,
one at a time, every CHECK_EVERY seconds per alert."""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import threading
import time
from urllib.parse import unquote

CHECK_EVERY = 300
ADO_RUN = re.compile(r"https://dev\.azure\.com/([^/?#]+)/([^/?#]+)/_build/results\?(?:[^#]*&)?buildId=(\d+)", re.I)
GH_RUN = re.compile(r"https://github\.com/([^/]+/[^/]+)/actions/runs/(\d+)", re.I)
GREEN = ("succeeded", "partiallySucceeded")  # partly: a continue-on-error step warned; the failing step passes again
_busy = threading.Lock()


def run_of(link: str) -> dict | None:
    """The pipeline run an alert's link points at, or None."""
    if m := ADO_RUN.search(link or ""):
        return {"kind": "ado", "org": f"https://dev.azure.com/{unquote(m[1])}", "project": unquote(m[2]), "id": int(m[3])}
    if m := GH_RUN.search(link or ""):
        return {"kind": "gh", "repo": m[1], "id": int(m[2])}
    return None


def _gh(*args: str):
    exe = shutil.which("gh")
    if not exe:
        raise RuntimeError("gh not found")
    r = subprocess.run([exe, *args], capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60,
                       stdin=subprocess.DEVNULL)
    if r.returncode:
        raise RuntimeError((r.stderr or r.stdout).strip()[-300:])
    return json.loads(r.stdout or "null")


def _az(*args: str):
    from triggers import azure
    return azure.az(*args, timeout=60)


def pipeline(run: dict) -> dict:
    """The failed run's pipeline and branch: {name, ref, branch} (read once, then kept on the alert)."""
    if run["kind"] == "ado":
        r = _az("pipelines", "runs", "show", "--id", str(run["id"]), "--org", run["org"], "--project", run["project"], "-o", "json")
        return {"name": r["definition"]["name"], "ref": str(r["definition"]["id"]), "branch": r["sourceBranch"]}
    r = _gh("run", "view", str(run["id"]), "-R", run["repo"], "--json", "workflowName,workflowDatabaseId,headBranch")
    return {"name": r["workflowName"], "ref": str(r["workflowDatabaseId"]), "branch": r["headBranch"]}


def latest(run: dict, pipe: dict) -> dict | None:
    """The newest finished run of that pipeline on that branch: {id, ok}."""
    if run["kind"] == "ado":
        rows = _az("pipelines", "runs", "list", "--pipeline-ids", pipe["ref"], "--branch", pipe["branch"], "--status",
                   "completed", "--query-order", "FinishTimeDesc", "--top", "1", "--org", run["org"], "--project",
                   run["project"], "-o", "json") or []
        return {"id": int(rows[0]["id"]), "ok": rows[0]["result"] in GREEN} if rows else None
    rows = _gh("run", "list", "-R", run["repo"], "--workflow", pipe["ref"], "--branch", pipe["branch"], "--status",
               "completed", "-L", "1", "--json", "databaseId,conclusion") or []
    return {"id": int(rows[0]["databaseId"]), "ok": rows[0]["conclusion"] == "success"} if rows else None


def due(crew) -> list[dict]:
    t = time.time()
    return [a for a in crew.alerts if a["status"] != "resolved" and run_of(a.get("link") or "")
            and t - (a.get("watch") or {}).get("t", 0) >= CHECK_EVERY]


def tick(crew) -> None:
    """Start one background check of the alerts that are due (lock held)."""
    if (todo := due(crew)) and _busy.acquire(blocking=False):
        for a in todo:
            a.setdefault("watch", {})["t"] = time.time()
        threading.Thread(target=_check, args=(crew, [dict(a) for a in todo]), daemon=True).start()


def _check(crew, todo: list[dict]) -> None:
    try:
        for a in todo:
            run = run_of(a["link"])
            w = dict(a.get("watch") or {})
            try:
                if not w.get("pipe"):
                    w["pipe"] = pipeline(run)
                last = latest(run, w["pipe"])
                w.pop("error", None)
            except Exception as e:  # az or gh missing, signed out, a deleted run: try again next time
                w["error"], last = str(e)[:200], None
            with crew.lock:
                al = next((x for x in crew.alerts if x["id"] == a["id"]), None)
                if not al or al["status"] == "resolved":
                    continue
                al["watch"] = {**w, "t": time.time()}
                if last and last["ok"] and last["id"] > run["id"]:
                    green(crew, w["pipe"], last["id"])
    finally:
        _busy.release()


def green(crew, pipe: dict, run_id: int) -> list[int]:
    """The pipeline is green again on its branch: resolve every open alert about it (lock held). Returns their ids."""
    branch = pipe["branch"].removeprefix("refs/heads/")
    note = f"auto: {pipe['name']} is green again on {branch} (run {run_id})"
    name = pipe["name"].lower()
    done = []
    for al in crew.alerts:
        if al["status"] == "resolved":
            continue
        w = al.get("watch") or {}
        same = (w.get("pipe") or {}).get("ref") == pipe["ref"] and (w.get("pipe") or {}).get("branch") == pipe["branch"]
        named = name in f"{al['title']} {al.get('details') or ''}".lower()
        if same or (named and not w.get("pipe")):
            al.update(status="resolved", resolved=time.time(), note=note)
            done.append(al["id"])
            crew.event("alert", f"✓ resolved: {al['title']} ({note})", al.get("agent"))
    if done:
        crew.touch()
    return done
