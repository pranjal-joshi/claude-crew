"""PR trigger: a pull request of yours starts one task or workflow run, once. On "raised", a PR you opened outside Crew
(by hand, in your IDE, from another tool): say a CI monitor that keeps it green until it merges. On "merged", a PR of
yours that merged, Crew's included: say a CD workflow that deploys and checks it. GitHub through `gh`, Azure Repos through `az`."""
from __future__ import annotations

import json
import re
import shutil
import subprocess
from urllib.parse import quote

from . import azure
from .azure import AZ_UNSAFE, AzError
from .base import Trigger, now, register, short_title
from .pipeline import iso_ts

# ---- PR trigger ----------------------------------------------------------------------------------
# Each check lists your open (or merged) PRs, read-only, and starts work for the ones that are new. On "raised", a PR Crew
# already knows is skipped: one it opened, one an agent opened or works on (Crew notes PR links from task text and
# summaries), and one opened from an agent's branch. Only PRs opened (or merged) after the trigger was count, and each
# one starts once.
PR_HOSTS = ("github", "ado")
PR_STATES = ("raised", "merged")
PR_MAX = 10  # PRs started per check
PR_SEEN_MAX = 500
GH_REPO = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")


class PrError(Exception):
    pass


def pr_spec(sp: dict) -> tuple[str | None, dict]:
    host = sp.get("host") if sp.get("host") in PR_HOSTS else "github"
    state = sp.get("state") if sp.get("state") in PR_STATES else "raised"
    repo = str(sp.get("repo") or "").strip()
    org = str(sp.get("org") or "").strip().rstrip("/")
    project = str(sp.get("project") or "").strip()
    if host == "github":
        if repo and not GH_REPO.match(repo):
            return "the repository looks like owner/name; leave it empty for this project's own", {}
        org = project = ""
    else:
        if not org or not project:
            return "the organization URL and project are needed to read Azure Repos PRs", {}
        if not org.startswith(("https://", "http://")):
            return "the organization URL looks like https://dev.azure.com/yourorg", {}
        if any(AZ_UNSAFE.search(x) or len(x) > 200 for x in (org, project, repo)):
            return "the organization, project or repository has a character that can't be used", {}
    try:
        every = max(2, int(sp.get("every") or 5))
    except (TypeError, ValueError):
        return "numbers only for intervals", {}
    return None, {"host": host, "state": state, "repo": repo[:200], "org": org, "project": project, "every": every,
                  "drafts": bool(sp.get("drafts")) and state == "raised"}


def merged(sp: dict) -> bool:
    return sp.get("state") == "merged"


def gh_prs(sp: dict, cwd) -> list[dict]:
    """Read-only: your open (or merged) PRs on GitHub, through gh as whichever account it is signed in with."""
    exe = shutil.which("gh")
    if not exe:
        raise PrError("gh not found: install the GitHub CLI and run `gh auth login`")
    cmd = [exe, "pr", "list", "--author", "@me", "--state", "merged" if merged(sp) else "open", "--limit", "100",
           "--json", "number,title,url,headRefName,baseRefName,isDraft,body,createdAt,mergedAt"] + (["--repo", sp["repo"]] if sp.get("repo") else [])
    try:
        r = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=90, stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired:
        raise PrError("gh timed out after 90 s") from None
    except OSError as e:
        raise PrError(f"gh could not start: {e}") from None
    if r.returncode:
        err = (r.stderr or r.stdout).strip()
        if re.search(r"auth login|not logged|authentication", err, re.I):
            raise PrError("gh is not signed in: run `gh auth login`")
        raise PrError((err.splitlines() or ["gh failed"])[-1][:300])
    try:
        rows = json.loads(r.stdout or "[]")
    except ValueError:
        raise PrError("gh printed something that is not JSON") from None
    return [{"key": str(x.get("url") or ""), "number": int(x["number"]), "title": str(x.get("title") or ""), "url": str(x.get("url") or ""),
             "head": str(x.get("headRefName") or ""), "base": str(x.get("baseRefName") or ""), "draft": bool(x.get("isDraft")),
             "body": str(x.get("body") or "")[:3000], "repo": sp.get("repo") or "", "created": iso_ts(x.get("createdAt")),
             "merged": iso_ts(x.get("mergedAt")) if x.get("mergedAt") else 0} for x in rows if x.get("number") and x.get("url")]


def ado_prs(sp: dict) -> list[dict]:
    """Read-only: your active (or completed, that is merged) PRs in Azure Repos, created by the az sign-in."""
    me = azure.az_me()
    if not me:
        raise AzError("no az sign-in: run `az login`")
    args = ["repos", "pr", "list", "--creator", me, "--status", "completed" if merged(sp) else "active", "--top", "100", "--output", "json",
            "--organization", sp["org"], "--project", sp["project"]] + (["--repository", sp["repo"]] if sp.get("repo") else [])
    out = []
    for x in azure.az(*args) or []:
        repo = str((x.get("repository") or {}).get("name") or sp.get("repo") or "")
        n = x.get("pullRequestId")
        if not n:
            continue
        url = f"{sp['org']}/{quote(sp['project'])}/_git/{quote(repo)}/pullrequest/{n}"
        out.append({"key": url, "number": int(n), "title": str(x.get("title") or ""), "url": url,
                    "head": str(x.get("sourceRefName") or "").removeprefix("refs/heads/"),
                    "base": str(x.get("targetRefName") or "").removeprefix("refs/heads/"),
                    "draft": bool(x.get("isDraft")), "body": str(x.get("description") or "")[:3000], "repo": repo,
                    "created": iso_ts(x.get("creationDate")), "merged": iso_ts(x.get("closedDate")) if merged(sp) and x.get("closedDate") else 0})
    return out


_gh_me: str | None = None
ADO_PR = re.compile(r"^(https://[^/]+/[^/]+)/[^/]+/_git/[^/]+/pullrequest/(\d+)$")


def gh_me(cwd) -> str:
    """gh's signed-in login, lower case, read once."""
    global _gh_me
    if _gh_me is None:
        r = subprocess.run([shutil.which("gh") or "gh", "api", "user", "--jq", ".login"], cwd=cwd, capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=30, stdin=subprocess.DEVNULL)
        if r.returncode:
            raise PrError("gh is not signed in")
        _gh_me = r.stdout.strip().lower()
    return _gh_me


ADO_STATES = {"active": "OPEN", "completed": "MERGED", "abandoned": "CLOSED"}


def owned(url: str, branches: set[str], cwd) -> str | bool:
    """Whether a PR link an agent mentioned is yours to work on: opened by your gh or az sign-in, or from an agent's
    branch. Yours: its state, OPEN, MERGED or CLOSED (truthy); not yours: False. Read-only. Raises PrError or AzError
    when it can't tell (no CLI, no sign-in, no network)."""
    if m := ADO_PR.match(url):
        x = azure.az("repos", "pr", "show", "--id", m.group(2), "--organization", m.group(1), "--output", "json") or {}
        who = str((x.get("createdBy") or {}).get("uniqueName") or "").lower()
        mine = bool(who) and who == azure.az_me() or str(x.get("sourceRefName") or "").removeprefix("refs/heads/") in branches
        return mine and ADO_STATES.get(str(x.get("status") or "").lower(), "OPEN")
    exe = shutil.which("gh")
    if not exe:
        raise PrError("gh not found")
    try:
        r = subprocess.run([exe, "pr", "view", url, "--json", "author,headRefName,state"], cwd=cwd, capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=60, stdin=subprocess.DEVNULL)
    except (subprocess.TimeoutExpired, OSError) as e:
        raise PrError(str(e)) from None
    if r.returncode:
        raise PrError((r.stderr or "gh failed").strip()[:200])
    x = json.loads(r.stdout or "{}")
    mine = str((x.get("author") or {}).get("login") or "").lower() == gh_me(cwd) or x.get("headRefName") in branches
    return mine and str(x.get("state") or "OPEN").upper()


def list_prs(crew, sp: dict) -> list[dict]:
    return gh_prs(sp, crew.project) if sp["host"] == "github" else ado_prs(sp)


def norm(url: str) -> str:
    return url.strip().rstrip("/").lower()


def crew_prs(crew) -> tuple[set[str], set[str]]:
    """(PR links Crew knows, agent branches). Lock held."""
    links = {norm(u) for t in crew.tasks for u in [t.get("pr"), *(t.get("prs") or [])] if u}
    links |= {norm((r.get("source") or {}).get("pr") or "") for r in crew.runs if r["status"] == "running"} - {""}
    links |= {norm(u) for u in [*crew._pr_wait, *crew._pr_had]}  # cards' links still being checked (after a restart)
    branches = {a["branch"] for a in crew.agents.values() if a.get("branch")}
    return links, branches


def owned_by(crew, pr: dict) -> dict | None:
    """The open card that already owns this PR, if any. Lock held."""
    from workflows import owners
    return owners.owner(crew, {owners.pr_key(pr["url"])})


def is_crews(pr: dict, links: set[str], branches: set[str]) -> bool:
    return norm(pr["url"]) in links or pr["head"] in branches


def why_not(pr: dict, sp: dict, links: set[str], branches: set[str], since: float = 0) -> str | None:
    """Why this PR does not start, or None when it does. A merged PR starts whoever opened it: the merge is the event."""
    if merged(sp):
        return None if pr.get("merged") and pr["merged"] >= since else "merged before the trigger"
    if is_crews(pr, links, branches):
        return "opened by Crew"
    if pr["draft"] and not sp["drafts"]:
        return "draft"  # not seen yet: it starts once marked ready for review
    return None if pr["created"] >= since else "opened before the trigger"


@register
class Pr(Trigger):
    kind, order = "pr", 65
    ui_only = "pr triggers read your PRs with your gh or az sign-in: create them in Crew's Triggers panel"
    polled = own_task = check_on_run = keeps_seen = True

    def validate(self, sp, old=None):
        return pr_spec(sp)

    def default_name(self, spec):
        return ("My merged PRs" if merged(spec) else "My PRs") + (f": {spec['repo']}" if spec.get("repo") else "")

    def task(self, spec):
        return {"title": "PR #{number} {title}", "details": "", "tag": "", "agent": spec["agent"], "workflow": spec["workflow"]}

    def form(self, crew, p):
        """POST /api/trigger/pr {op: "test", spec}: lists your open (or merged) PRs and which would start. Starts nothing."""
        if p.get("op") != "test":
            return {"error": "unknown op"}
        err, sp = pr_spec(p.get("spec") or {})
        if err:
            return {"error": err}
        try:
            prs = list_prs(crew, sp)
        except (PrError, AzError) as e:
            return {"error": str(e)}
        with crew.lock:
            links, branches = crew_prs(crew)
            since = now()  # a trigger made now starts nothing that already happened
            items = [{"number": x["number"], "title": x["title"], "url": x["url"], "draft": x["draft"],
                      "why": why_not(x, sp, links, branches, since) or (f"news for #{o['id']}" if (o := owned_by(crew, x)) else "would start")}
                     for x in prs]
        return {"ok": True, "total": len(items), "items": items[:25]}

    @staticmethod
    def start(crew, tr: dict, pr: dict) -> str:
        """Start the run (or task) for one PR. Caller holds the lock. Returns what to call it."""
        sp = tr["spec"]
        done = merged(sp)
        title = f"PR #{pr['number']} {'merged: ' if done else ''}{short_title(pr['title'])}"
        details = (f"{pr['title']}\n{pr['url']}\n{pr['head']} → {pr['base']}{' · draft' if pr['draft'] and not done else ''}"
                   + (f" · {pr['repo']}" if pr.get("repo") else "") + (f"\nMerged into {pr['base']}." if done else "\nOpened outside Crew.")
                   + (f"\n\n{pr['body']}" if pr["body"].strip() else ""))
        source = {"trigger": tr["id"], "name": tr["name"], "pr": pr["url"]} | ({"merged": True} if done else {})
        from workflows import owners
        if o := owned_by(crew, pr):  # an open card already owns this PR: it gets the news, nothing new starts
            owners.attach(crew, o, f"PR #{pr['number']} {'merged into ' + pr['base'] if done else 'was raised'} ({pr['url']})", tr["name"])
            return f"news for #{o['id']}"
        if (w := crew._workflow(sp.get("workflow"))) is not None:
            run = crew._start_run(w, title, details, source=source)
            crew.event("trigger", f"⚡ {tr['name']} → ⛓ {w['name']}: {title}")
            first = crew._task(run["tasks"][0]) if run.get("tasks") else None
            if first:
                first["prs"] = [pr["url"]]  # the card links its PR
            return f"{w['name']}, run #{run['id'].lstrip('r')}"
        if sp.get("workflow"):
            raise PrError(f"workflow {sp['workflow']} no longer exists")
        who = sp.get("agent") if sp.get("agent") in crew.agents else None
        t = {"id": crew.next_id, "title": title, "details": details[:8000], "tag": "review" if "review" in crew._all_tags() else "feature",
             "col": "backlog", "who": who, "pinned": who, "created": now(), "depth": 0, "prs": [pr["url"]],
             "source": source | {"kind": "pr"}}
        crew.next_id += 1
        crew.tasks.append(t)
        crew.event("trigger", f"⚡ {tr['name']} → #{t['id']} {title}" + (f" → {who}" if who else ""), who)
        return f"task #{t['id']}"

    def check(self, crew, tr):
        """One check. Runs in a thread; gh and az never run while the lock is held."""
        sp, err, found, picked = tr["spec"], None, 0, 0
        try:
            prs = list_prs(crew, sp)
            with crew.lock:
                if tr not in crew.triggers:
                    return
                links, branches = crew_prs(crew)
                seen = list(tr.get("seen") or [])
                since = tr.get("created") or 0  # PRs opened (or merged) before the trigger existed never start
                new = [x for x in prs if x["key"] not in seen and not why_not(x, sp, links, branches, since)]
                found = len(new)
                for x in new[:PR_MAX]:
                    seen.append(x["key"])  # seen first: a failure below must not start it twice
                    tr["seen"] = seen[-PR_SEEN_MAX:]
                    label = self.start(crew, tr, x)
                    tr["fired"], tr["last"], tr["note"] = tr.get("fired", 0) + 1, now(), f"PR #{x['number']} → {label}"
                    tr["picked"] = tr.get("picked", 0) + 1
                    picked += 1
        except (PrError, AzError) as e:
            err = str(e)
        except Exception as e:  # a bug here must not leave the trigger stuck as running
            err = f"unexpected: {e}"
        self.finish(crew, tr, err, {"found": found, "picked": picked})
