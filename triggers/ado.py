"""ADO trigger: Azure DevOps through the `az` CLI. A work item carrying your tag, or newly assigned to you, starts one task or
workflow run, once."""
from __future__ import annotations

import html
import re
from urllib.parse import quote

from . import azure, get
from .azure import AZ_UNSAFE, AzError
from .base import Trigger, now, register, short_title

# ---- ADO tag trigger: Azure DevOps through the `az` CLI ----------------------------------
ADO_AFTER = ["tag_picked", "remove_tag", "none"]
ADO_MATCH = ["tag", "assigned"]   # items carrying a tag, or items assigned to the signed-in az user
ADO_EXISTING = ["skip", "start"]  # assigned: what to do with items already assigned when the trigger starts
ADO_MAX = 10      # items started per check; the rest wait for the next one
ADO_SEEN_MAX = 500
ADO_CLOSED = ("Closed", "Done", "Removed")

def ado_spec(sp: dict) -> tuple[str | None, dict]:
    """The parts of an ADO spec the query needs: match, tag, org, project, types, every, after, comment, existing."""
    match = sp.get("match") if sp.get("match") in ADO_MATCH else "tag"
    tag = " ".join(str(sp.get("tag") or "").split()) if match == "tag" else ""
    if match == "tag" and not tag:
        return "which tag will you put on items? Enter it", {}
    if len(tag) > 50 or AZ_UNSAFE.search(tag) or ";" in tag or "," in tag:
        return "the tag is too long or has a character Azure DevOps tags can't use", {}
    org = str(sp.get("org") or "").strip().rstrip("/")
    project = str(sp.get("project") or "").strip()
    if org and not org.startswith(("https://", "http://")):
        return "the organization URL looks like https://dev.azure.com/yourorg", {}
    if AZ_UNSAFE.search(org) or AZ_UNSAFE.search(project) or len(org) > 200 or len(project) > 100:
        return "the organization or project has a character that can't be used", {}
    types = [str(x).strip() for x in (sp.get("types") or []) if str(x).strip()][:10]
    if any(AZ_UNSAFE.search(x) or len(x) > 40 for x in types):
        return "a work item type has a character that can't be used", {}
    try:
        every = max(1, int(sp.get("every") or 5))
    except (TypeError, ValueError):
        return "numbers only for intervals", {}
    after = sp.get("after") if sp.get("after") in ADO_AFTER else "tag_picked"
    return None, {"match": match, "tag": tag, "org": org, "project": project, "types": types, "every": every,
                  "after": after if match == "tag" else "none",  # no tag to mark: `seen` alone keeps an item from starting twice
                  "comment": bool(sp.get("comment", True)),
                  "existing": sp.get("existing") if sp.get("existing") in ADO_EXISTING else "skip"}


def wiql_quote(s: str) -> str:
    return "'" + s.replace("'", "''") + "'"


def ado_wiql(sp: dict) -> str:
    if sp.get("match") == "assigned":
        cond = ["[System.AssignedTo] = @Me"]
    else:
        cond = [f"[System.Tags] CONTAINS {wiql_quote(sp['tag'])}"]
    if sp.get("match") != "assigned" and sp.get("after", "tag_picked") == "tag_picked":
        cond.append(f"[System.Tags] NOT CONTAINS {wiql_quote(sp['tag'] + '-picked')}")
    cond.append("[System.State] NOT IN (" + ", ".join(wiql_quote(x) for x in ADO_CLOSED) + ")")
    if sp.get("types"):
        cond.append("[System.WorkItemType] IN (" + ", ".join(wiql_quote(x) for x in sp["types"]) + ")")
    if sp.get("project"):
        cond.append(f"[System.TeamProject] = {wiql_quote(sp['project'])}")
    return ("SELECT [System.Id], [System.Title], [System.WorkItemType], [System.State], [System.Tags] FROM WorkItems WHERE "
            + " AND ".join(cond) + " ORDER BY [System.ChangedDate] DESC")


def ado_org_args(sp: dict) -> list[str]:
    return ["--organization", sp["org"]] if sp.get("org") else []


def ado_query(sp: dict) -> list[dict]:
    """Read-only: the work items that match the tag, as [{id, title, type, state, tags}]."""
    args = ["boards", "query", "--wiql", ado_wiql(sp), "--output", "json", *ado_org_args(sp)]
    if sp.get("project"):
        args += ["--project", sp["project"]]
    out = []
    for it in azure.az(*args) or []:
        f = it.get("fields") or {}
        try:
            wid = int(it.get("id") or f.get("System.Id"))
        except (TypeError, ValueError):
            continue
        out.append({"id": wid, "title": str(f.get("System.Title") or ""), "type": str(f.get("System.WorkItemType") or ""),
                    "state": str(f.get("System.State") or ""), "tags": str(f.get("System.Tags") or "")})
    return out


def html_text(s: str) -> str:
    s = re.sub(r"(?i)<br\s*/?>|</(p|div|li|h\d|tr)>", "\n", s or "")
    s = html.unescape(re.sub(r"<[^>]*>", "", s)).replace("\xa0", " ")
    return re.sub(r"\n\s*\n+", "\n\n", "\n".join(x.strip() for x in s.splitlines())).strip()


def ado_item(sp: dict, wid: int) -> dict:
    """Read-only: one item's title, type, state, tags, description (as text, ~3000 chars) and URL."""
    d = azure.az("boards", "work-item", "show", "--id", str(wid), "--output", "json", *ado_org_args(sp)) or {}
    f = d.get("fields") or {}
    if sp.get("org") and sp.get("project"):
        url = f"{sp['org']}/{quote(sp['project'])}/_workitems/edit/{wid}"
    else:
        url = str(((d.get("_links") or {}).get("html") or {}).get("href") or "")
    return {"id": wid, "title": str(f.get("System.Title") or f"work item {wid}"), "type": str(f.get("System.WorkItemType") or ""),
            "state": str(f.get("System.State") or ""), "tags": str(f.get("System.Tags") or ""), "url": url,
            "description": html_text(str(f.get("System.Description") or ""))[:3000]}


def ado_tags_after(old: str, tag: str, after: str) -> str | None:
    """The new System.Tags value ('; '-separated), or None when the tags stay as they are."""
    tags = [x.strip() for x in (old or "").split(";") if x.strip()]
    if after == "tag_picked":
        if not any(x.lower() == (tag + "-picked").lower() for x in tags):
            tags.append(tag + "-picked")
    elif after == "remove_tag":
        tags = [x for x in tags if x.lower() != tag.lower()]
    else:
        return None
    return "; ".join(tags)


def ado_write_back(sp: dict, item: dict, label: str) -> None:
    """Mark the item as picked up: tags per `after`, plus a discussion comment. Raises AzError."""
    new = ado_tags_after(item["tags"], sp["tag"], sp["after"])
    if new is None and not sp.get("comment"):
        return
    args = ["boards", "work-item", "update", "--id", str(item["id"]), "--output", "json", *ado_org_args(sp)]
    if new is not None:
        args += ["--fields", f"System.Tags={new}"]
    if sp.get("comment"):
        args += ["--discussion", AZ_UNSAFE.sub(" ", f"Picked up by Crew: {label}")]
    azure.az(*args)



@register
class Ado(Trigger):
    kind, order = "ado", 60
    ui_only = "ado triggers write to Azure DevOps: create them in Crew's Triggers panel, not through the boss or an agent"
    polled = own_task = check_on_run = keeps_seen = True

    def validate(self, sp, old=None):
        return ado_spec(sp)

    def clean(self, crew, p, old):
        """Assigned to me: a new trigger, or an edit that changes what the query matches, takes a new baseline on its next
        check, so items that were already assigned (say, PBIs when only Bugs were watched) never count as new."""
        err, rec = super().clean(crew, p, old)
        if not err and rec["spec"]["match"] == "assigned":
            keys = ("match", "org", "project", "types", "existing")
            if old is None or any((old.get("spec") or {}).get(k) != rec["spec"][k] for k in keys):
                rec["baselined"] = False
        return err, rec

    def default_name(self, spec):
        return "ADO: assigned to me" if spec.get("match") == "assigned" else f"ADO: {spec['tag']}"

    def task(self, spec):
        return {"title": "AB#{id} {title}", "details": "", "tag": "", "agent": spec["agent"], "workflow": spec["workflow"]}

    def form(self, crew, p):
        """POST /api/trigger/ado: {op: "defaults"} reads `az devops configure --list`; {op: "test", spec} runs the query and starts nothing.
        The Pipeline form still posts "pipelines" and "pipeline_test" here (the /api/ado alias): they go to its own kind."""
        if p.get("op") in ("pipelines", "pipeline_test"):
            return get("pipeline").form(crew, p | {"op": "test" if p["op"] == "pipeline_test" else "pipelines"})
        try:
            if p.get("op") == "defaults":
                return azure.ado_defaults()
            if p.get("op") == "test":
                err, sp = ado_spec(p.get("spec") or {})
                if err:
                    return {"error": err}
                items = ado_query(sp)
                res = {"ok": True, "total": len(items), "items": items[:25], "wiql": ado_wiql(sp)}
                return res | ({"baseline": True} if sp["match"] == "assigned" and sp["existing"] == "skip" else {})
        except AzError as e:
            return {"error": str(e)}
        return {"error": "unknown op"}

    @staticmethod
    def live(crew) -> set[int]:
        """Work item ids that a running run or an open task already carries."""
        ids = {(r.get("ado") or {}).get("id") for r in crew.runs if r["status"] == "running"}
        ids |= {(t.get("ado") or {}).get("id") for t in crew.tasks if t["col"] != "done"}
        return {i for i in ids if i is not None}

    @staticmethod
    def start(crew, tr: dict, item: dict) -> str:
        """Start the workflow run (or create the task) for one work item. Caller holds the lock. Returns what to call it."""
        sp = tr["spec"]
        ado = {"id": item["id"], "url": item["url"]}
        title = f"AB#{item['id']} {short_title(item['title'])}"  # the full title leads the details
        details = f"{item['title']}\n{item['type']} · {item['state']}\n{item['url']}" + (f"\n\n{item['description']}" if item["description"] else "")
        source = {"trigger": tr["id"], "name": tr["name"], "ado": ado}
        if (w := crew._workflow(sp.get("workflow"))) is not None:
            run = crew._start_run(w, title, details, source=source)
            crew.event("trigger", f"⚡ {tr['name']} → ⛓ {w['name']}: {title}")
            return f"{w['name']}, run #{run['id'].lstrip('r')}"
        if sp.get("workflow"):
            raise AzError(f"workflow {sp['workflow']} no longer exists")
        who = sp.get("agent") if sp.get("agent") in crew.agents else None
        t = {"id": crew.next_id, "title": title, "details": details[:8000], "tag": "bug" if item["type"] == "Bug" and "bug" in crew._all_tags() else "feature",
             "col": "backlog", "who": who, "pinned": who, "created": now(), "depth": 0, "ado": ado,
             "source": source | {"kind": "ado"}}
        crew.next_id += 1
        crew.tasks.append(t)
        crew.event("trigger", f"⚡ {tr['name']} → #{t['id']} {title}" + (f" → {who}" if who else ""), who)
        return f"task #{t['id']}"

    def check(self, crew, tr):
        """One check. Runs in a thread; az never runs while the lock is held."""
        sp = tr["spec"]
        found = picked = 0
        err = None
        try:
            items = ado_query(sp)
            found = len(items)
            if sp.get("match") == "assigned" and sp.get("existing") == "skip" and not tr.get("baselined"):
                with crew.lock:  # first check: what is assigned already is the baseline, only items assigned after it start
                    if tr in crew.triggers:
                        ids = [i for i in tr.get("seen") or [] if i not in {x["id"] for x in items}] + [x["id"] for x in items]
                        tr["seen"], tr["baselined"], tr["note"] = ids[-ADO_SEEN_MAX:], True, f"baseline: {found} already assigned"
                self.finish(crew, tr, None, {"found": found, "picked": 0})
                return
            with crew.lock:
                skip = set(tr.get("seen") or []) | self.live(crew)
            for it in [i for i in items if i["id"] not in skip][:ADO_MAX]:
                item = ado_item(sp, it["id"])
                with crew.lock:
                    if tr not in crew.triggers:
                        return
                    label = self.start(crew, tr, item)
                    seen = [i for i in tr.get("seen") or [] if i != item["id"]] + [item["id"]]
                    tr["seen"] = seen[-ADO_SEEN_MAX:]  # seen first: a failed write-back must not start it twice
                    tr["fired"], tr["last"], tr["note"] = tr.get("fired", 0) + 1, now(), f"AB#{item['id']}"
                    tr["picked"] = tr.get("picked", 0) + 1
                    picked += 1
                try:
                    ado_write_back(sp, item, label)
                except AzError as e:
                    with crew.lock:
                        crew.event("error", f"⚠ {tr['name']}: AB#{item['id']} started, but marking it in Azure DevOps failed: {e}")
                    err = f"AB#{item['id']} started; write-back failed: {e}"
                crew.touch()
        except AzError as e:
            err = str(e)
        except Exception as e:  # a bug here must not leave the trigger stuck as running
            err = f"unexpected: {e}"
        self.finish(crew, tr, err, {"found": found, "picked": picked})
