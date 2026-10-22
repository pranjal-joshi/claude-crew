"""Pipeline trigger: a finished Azure Pipelines run starts one task or workflow run, once. Read-only through `az`."""
from __future__ import annotations

import re
from datetime import datetime
from urllib.parse import quote

from . import azure
from .ado import ADO_SEEN_MAX
from .azure import AZ_UNSAFE, AzError
from .base import Trigger, now, register

# ---- Pipeline trigger --------------------------------------------------------------------------
# Azure Pipelines runs through the az CLI you are signed in with. A finished run of a named pipeline starts one task or
# workflow run, once. "Only mine" keeps runs requested for you, or that carry a work item assigned to you: a release
# build is requested for whoever merged, but its linked work items say whose work shipped.
PIPE_RESULTS = {"succeeded": ("succeeded",), "partial": ("partiallySucceeded",), "failed": ("failed",),
                "any": ("succeeded", "failed", "partiallySucceeded", "canceled")}
PIPE_MAX = 10  # runs started per check


def pipe_spec(sp: dict) -> tuple[str | None, dict]:
    raw = sp.get("pipelines") or []
    names = raw if isinstance(raw, list) else re.split(r"[,\n]", str(raw))
    names = list(dict.fromkeys(n for n in (" ".join(str(x).split()) for x in names) if n))[:20]
    if not names:
        return "add at least one pipeline name", {}
    if any(len(n) > 200 or AZ_UNSAFE.search(n) for n in names):
        return "a pipeline name has a character that can't be used", {}
    org = str(sp.get("org") or "").strip().rstrip("/")
    project = str(sp.get("project") or "").strip()
    if not org or not project:
        return "the organization URL and project are needed to read pipelines", {}
    if not org.startswith(("https://", "http://")) or AZ_UNSAFE.search(org) or AZ_UNSAFE.search(project):
        return "the organization URL looks like https://dev.azure.com/yourorg", {}
    try:
        every = max(2, int(sp.get("every") or 5))
    except (TypeError, ValueError):
        return "numbers only for intervals", {}
    return None, {"pipelines": names, "org": org, "project": project, "every": every,
                  "result": sp.get("result") if sp.get("result") in PIPE_RESULTS else "any", "mine": bool(sp.get("mine"))}


def pipe_ids(sp: dict) -> dict[str, int]:
    """Pipeline name -> id, case-insensitive. Raises AzError naming the ones that don't exist."""
    have = {str(p.get("name") or "").lower(): int(p["id"]) for p in azure.az("pipelines", "list", "--org", sp["org"], "--project", sp["project"],
                                                                         "--top", "2000", "--output", "json") or [] if p.get("id")}
    missing = [n for n in sp["pipelines"] if n.lower() not in have]
    if missing:
        raise AzError("no pipeline called " + ", ".join(missing) + " in " + sp["project"])
    return {n: have[n.lower()] for n in sp["pipelines"]}


def iso_ts(s) -> float:
    m = re.match(r"(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d)(\.\d+)?(Z|[+-]\d\d:?\d\d)?", str(s or ""))
    if not m:
        return 0.0
    tz = (m.group(3) or "Z").replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(m.group(1) + (m.group(2) or "")[:7] + tz).timestamp()
    except ValueError:
        return 0.0


def pipe_runs(sp: dict, ids: list[int], top: int = 40) -> list[dict]:
    """Finished runs of these pipelines, newest first."""
    out = []
    for r in azure.az("pipelines", "runs", "list", "--org", sp["org"], "--project", sp["project"], "--status", "completed",
                "--query-order", "FinishTimeDesc", "--top", str(top), "--pipeline-ids", *map(str, ids), "--output", "json") or []:
        req, ti = r.get("requestedFor") or {}, r.get("triggerInfo") or {}
        out.append({"id": int(r["id"]), "pipeline": str((r.get("definition") or {}).get("name") or ""), "result": str(r.get("result") or ""),
                    "finished": iso_ts(r.get("finishTime")), "branch": str(r.get("sourceBranch") or "").removeprefix("refs/heads/"),
                    "commit": str(r.get("sourceVersion") or "")[:12], "message": str(ti.get("ci.message") or ti.get("pr.title") or "")[:300],
                    "who": str(req.get("displayName") or ""), "who_id": str(req.get("uniqueName") or "").lower(), "reason": str(r.get("reason") or ""),
                    "url": f"{sp['org']}/{quote(sp['project'])}/_build/results?buildId={int(r['id'])}"})
    return out


def pipe_items(sp: dict, run_id: int) -> list[int]:
    """Work items linked to a run (the PRs and commits it built)."""
    ids = azure.az("devops", "invoke", "--org", sp["org"], "--area", "build", "--resource", "workitems", "--route-parameters",
             f"project={sp['project']}", f"buildId={run_id}", "--api-version", "7.1", "--query", "value[].id", "--output", "json") or []
    return [int(x) for x in ids if str(x).isdigit()][:200]


def pipe_my_items(sp: dict, ids: list[int]) -> list[dict]:
    """Of these work items, the ones assigned to you (the az sign-in), with title and state."""
    if not ids:
        return []
    wiql = ("SELECT [System.Id], [System.Title], [System.State] FROM WorkItems WHERE [System.Id] IN ("
            + ", ".join(map(str, ids[:200])) + ") AND [System.AssignedTo] = @Me")
    out = []
    for it in azure.az("boards", "query", "--wiql", wiql, "--org", sp["org"], "--project", sp["project"], "--output", "json") or []:
        f = it.get("fields") or {}
        out.append({"id": int(it.get("id") or f.get("System.Id")), "title": str(f.get("System.Title") or ""), "state": str(f.get("System.State") or "")})
    return out



@register
class Pipeline(Trigger):
    kind, order = "pipeline", 80
    ui_only = "pipeline triggers read Azure DevOps with your az sign-in: create them in Crew's Triggers panel"
    polled = own_task = check_on_run = keeps_seen = True

    def validate(self, sp, old=None):
        return pipe_spec(sp)

    def default_name(self, spec):
        return f"Pipeline: {spec['pipelines'][0]}"

    def task(self, spec):
        return {"title": "{pipeline} {result}", "details": "", "tag": "", "agent": spec["agent"], "workflow": spec["workflow"]}

    def form(self, crew, p):
        """POST /api/trigger/pipeline: {op: "pipelines", spec: {org, project}} lists names for the picker; {op: "test", spec} lists
        recent matching runs and starts nothing."""
        try:
            if p.get("op") == "pipelines":
                sp = p.get("spec") or {}
                org, project = str(sp.get("org") or "").strip().rstrip("/"), str(sp.get("project") or "").strip()
                if not org or not project or AZ_UNSAFE.search(org + project):
                    return {"error": "organization and project first"}
                names = [str(x.get("name") or "") for x in azure.az("pipelines", "list", "--org", org, "--project", project, "--top", "2000",
                                                                    "--query", "[].{name:name}", "--output", "json") or []]
                return {"ok": True, "names": sorted(n for n in names if n)}
            if p.get("op") == "test":
                err, sp = pipe_spec(p.get("spec") or {})
                if err:
                    return {"error": err}
                runs = [r for r in pipe_runs(sp, list(pipe_ids(sp).values()), 25) if r["result"] in PIPE_RESULTS[sp["result"]]]
                runs = [r for r in runs if r["finished"] > now() - 7 * 86400][:10]
                me = azure.az_me()
                for r in runs:
                    items = pipe_items(sp, r["id"])
                    mine = pipe_my_items(sp, items) if sp["mine"] else []
                    r.update(items=len(items), my_items=[f"AB#{x['id']}" for x in mine], mine=r["who_id"] == me or bool(mine))
                    r.pop("who_id")
                if sp["mine"]:
                    runs = [r for r in runs if r["mine"]]
                return {"ok": True, "total": len(runs), "items": runs}
        except AzError as e:
            return {"error": str(e)}
        return {"error": "unknown op"}

    @staticmethod
    def start(crew, tr: dict, run: dict, items: list[int], mine: list[dict]) -> str:
        """One task or workflow run for a finished pipeline run. Caller holds the lock."""
        sp = tr["spec"]
        base = f"{sp['org']}/{quote(sp['project'])}/_workitems/edit/"
        first = (mine[0]["id"] if mine else items[0]) if items else None
        ado = {"id": first, "url": base + str(first)} if first else None
        said = "partially succeeded" if run["result"] == "partiallySucceeded" else run["result"]
        title = f"{run['pipeline']} {said}"[:200]
        lines = [f"Pipeline {run['pipeline']} finished: {said}." + (" Some steps failed or had warnings; check them." if said != run["result"] else ""), f"Run: {run['url']}",
                 f"Branch {run['branch'] or '?'} · commit {run['commit'] or '?'}" + (f" · {run['message']}" if run["message"] else ""),
                 f"Requested for {run['who'] or 'unknown'} ({run['reason'] or 'manual'})."]
        if mine:
            lines += ["", "Your work items in this run (update or close these):"] + [f"- AB#{x['id']} {x['title']} ({x['state']})" for x in mine]
        if items:
            lines += ["", f"All {len(items)} linked work item{'s' if len(items) != 1 else ''}: " + ", ".join(f"AB#{i}" for i in items[:60])
                      + (" …" if len(items) > 60 else "")]
        details = "\n".join(lines)
        source = {"trigger": tr["id"], "name": tr["name"], "kind": "pipeline", "run": run["id"], "url": run["url"]} | ({"ado": ado} if ado else {})
        if (w := crew._workflow(sp.get("workflow"))) is not None:
            r = crew._start_run(w, title, details, source=source)
            crew.event("trigger", f"⚡ {tr['name']} → ⛓ {w['name']}: {title}")
            return f"{w['name']}, run #{r['id'].lstrip('r')}"
        who = sp.get("agent") if sp.get("agent") in crew.agents else None
        tag = "deploy" if "deploy" in crew._all_tags() else "ops" if "ops" in crew._all_tags() else "feature"
        t = {"id": crew.next_id, "title": title, "details": details[:8000], "tag": tag, "col": "backlog", "who": who, "pinned": who,
             "created": now(), "depth": 0, "source": source} | ({"ado": ado} if ado else {})
        crew.next_id += 1
        crew.tasks.append(t)
        crew.event("trigger", f"⚡ {tr['name']} → #{t['id']} {title}" + (f" → {who}" if who else ""), who)
        return f"task #{t['id']}"

    def check(self, crew, tr):
        """One check. Runs in a thread; az never runs while the lock is held."""
        sp, started, err, found, picked = tr["spec"], now(), None, 0, 0
        since = tr.get("since") or started - sp["every"] * 60
        try:
            if tr.get("_ids_for") != sp["pipelines"]:
                tr["_ids"], tr["_ids_for"] = list(pipe_ids(sp).values()), list(sp["pipelines"])
            with crew.lock:
                seen = set(tr.get("seen") or [])
            runs = [r for r in pipe_runs(sp, tr["_ids"]) if r["finished"] >= since - 120 and r["id"] not in seen
                    and r["result"] in PIPE_RESULTS[sp["result"]]]
            found = len(runs)
            me = azure.az_me() if sp["mine"] else ""
            for run in sorted(runs, key=lambda r: r["finished"])[:PIPE_MAX]:
                items = pipe_items(sp, run["id"])
                mine = pipe_my_items(sp, items)
                keep = not sp["mine"] or run["who_id"] == me or bool(mine)
                with crew.lock:
                    if tr not in crew.triggers:
                        return
                    tr["seen"] = ([i for i in tr.get("seen") or [] if i != run["id"]] + [run["id"]])[-ADO_SEEN_MAX:]
                    if keep:
                        label = self.start(crew, tr, run, items, mine)
                        tr["fired"], tr["last"], tr["note"] = tr.get("fired", 0) + 1, now(), f"{run['pipeline']} #{run['id']} → {label}"[:200]
                        tr["picked"] = tr.get("picked", 0) + 1
                        picked += 1
                crew.touch()
            with crew.lock:
                tr["since"] = started
        except AzError as e:
            err = str(e)
        except Exception as e:  # a bug here must not leave the trigger stuck as running
            err = f"unexpected: {e}"
        self.finish(crew, tr, err, {"found": found, "picked": picked})
