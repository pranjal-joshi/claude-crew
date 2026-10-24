"""Pipeline fix: a ⟳ watch blocked by a failed pipeline hands the failure to a fix run (fix, ship a PR, keep it green until
it merges) and waits; once that run ends, Crew wakes the watch. A deploy watch then follows the first run that carries the
fix; a PR watch (a PR blocked by a failure that is on master too) brings master into its branch and re-runs its checks.

The card keeps its ⟳ step: `wait.fixing` holds the fix run's id while it works (no checks, no wake-ups), and
`wait.fix` what it ended with, for the agent's next check. One fix run can serve several waiting cards (`join`), so a
master-wide failure is fixed once. Functions take the Crew first and run with its lock held."""
from __future__ import annotations

import re
import time

from . import runs

KIND = "pipeline_fix"
NAME = "Pipeline fix"
BRIEF_MAX = 4000
CHECK_OTHERS = ("Someone else may already be fixing this. Before changing anything, look for a fix that exists already: open "
                "and recently merged PRs that touch the failing file or name the error (`gh pr list --state all --search "
                "\"<file or error>\"`), remote branches (`git fetch -q origin && git branch -r --sort=-committerdate | head -30`) "
                "and the last master commits (`git log origin/master -15 --oneline -- <file>`).")


def steps_for(fixer: str, shipper: str, ci: str) -> list[dict]:
    """The Pipeline fix workflow's steps, for the crew's fixer, shipper and CI agents."""
    return [
        {"agent": fixer, "title": "Fix pipeline: {input}", "gate": "auto",
         "details": ("A pipeline run failed, and a deployment or a PR is blocked until it passes. The brief has the failed run, "
                     "what the waiting agent saw, and the change it was deploying or the PR that is blocked. When a PR is "
                     "blocked by a failure that is on master too, fix master, not that PR.\n"
                     "1. Open the failed run and find the failing step and its cause in the log (`az pipelines runs show "
                     "--id <n>`, the run's link, `az devops invoke --area build --resource timeline` for the failed step).\n"
                     f"2. {CHECK_OTHERS} If one already fixes it, don't redo it: name it (PR, branch or commit) and call "
                     "`decide` with continue when it is an open PR (the next steps get it green and merged), or with stop when "
                     "it is already on master (the waiting agent then carries on from it).\n"
                     "3. If no code change can fix it (an agent, network, quota or permission problem, a flaky test that passes "
                     "on a re-run), don't change code: call `decide` with stop and say what a person must do.\n"
                     "4. Otherwise make the smallest fix for this failure on your worktree branch, branched from fresh "
                     "origin/master, and commit it. Fix only what the run reports; nothing else.\n"
                     "5. Before you finish, run locally only these tests: the one that failed in the run (if a test failed), "
                     "any you added, and those covering the files in your diff. They must pass. Don't run the whole suite or "
                     "the pipeline; CI does that on the PR. Skip any that need secrets or infrastructure you don't have, and "
                     "list them in your summary with why.")},
        {"agent": shipper, "title": "Ship pipeline fix: {input}", "gate": "auto",
         "details": ("Open a PR for the pipeline fix from the step before, against master, ready for review, using the work "
                     "item in the brief for the AB# link. If the step before named an open PR that fixes it, reuse that PR "
                     f"instead (mark it ready) and don't open a second one.\nCheck again right before you push: {CHECK_OTHERS} "
                     "If a fix landed on master meanwhile, don't open the PR: call `decide` with stop and name it. "
                     "End with the PR's full URL.")},
        {"agent": ci, "title": "Merge pipeline fix: {input}", "gate": "until", "every": 10, "checks": 144,
         "details": ("Get the pipeline fix PR green and merged. Fix failures this PR caused and push; address review comments. "
                     "Never merge it yourself. Call `decide` with continue once it is merged, naming the merge commit; with "
                     "stop if it was closed. While you wait, check whether master got a fix for the same failure from another "
                     "PR: if so, call `decide` with stop, name that PR and its merge commit, and say this PR can be closed.")},
    ]


def _agent_by(crew, *tags: str, skip: str = "") -> str:
    for tag in tags:
        if a := next((a for a in crew.agents.values() if tag in a["tags"] and a["name"] != skip and not a.get("off")), None):
            return a["name"]
    return ""


def workflow(crew) -> tuple[str | None, dict | None]:
    """The crew's Pipeline fix workflow, created from its agents' tags the first time it is needed. (error, workflow)"""
    flows = crew.config.setdefault("workflows", [])
    if w := next((w for w in flows if w.get("kind") == KIND), None) or next((w for w in flows if w["name"] == NAME), None):
        if w.get("off"):
            return f"the {w['name']} workflow is turned off: turn it on to fix pipelines", None
        if missing := [s["agent"] for s in w["steps"] if s["agent"] not in crew.agents]:
            return f"the {w['name']} workflow names agents this crew lacks: {', '.join(missing)}", None
        return None, w
    fixer = _agent_by(crew, "bug", "feature", "incident")
    shipper = _agent_by(crew, "release", "chore", skip=fixer)
    ci = _agent_by(crew, "ci", "review", skip=fixer)
    if not (fixer and shipper and ci):
        need = [x for x, ok in (("a fixer (tag bug or feature)", fixer), ("a shipper (tag release or chore)", shipper),
                                ("a CI agent (tag ci or review)", ci)) if not ok]
        return f"a pipeline fix needs {', '.join(need)}: add one, or create a workflow called '{NAME}'", None
    ids = [int(x["id"][1:]) for x in flows if x["id"][1:].isdigit()]
    w = {"id": f"w{max(ids, default=0) + 1}", "name": NAME, "kind": KIND,
         "description": "Fix a failed pipeline run, ship the fix, keep it green until it merges; the blocked watch then carries on.",
         "steps": steps_for(fixer, shipper, ci), "output": {"kind": "board"}}
    flows.append(w)
    crew._save_config()
    crew.event("config", f"⛓ added the {NAME} workflow: {fixer} → {shipper} → {ci} (edit it in Workflows)")
    return None, w


def is_pr_watch(crew, t: dict) -> bool:
    """A PR's CI watch (it has a PR and its agent doesn't follow deployments), not a deploy watch."""
    tags = (crew.agents.get(t.get("who") or "") or {}).get("tags") or []
    return bool(t.get("prs")) and not {"deploy", "cd"} & set(tags)


def is_fix_run(run: dict | None) -> bool:
    return bool(run and (run.get("source") or {}).get("kind") == KIND)


def can_fix(crew, t: dict | None) -> str | None:
    """Why this card can't hand its pipeline to a fix run, or None."""
    if not t:
        return "no such task"
    if not crew._until_step(t):
        return f"#{t['id']} is not a ⟳ (check until done) workflow step: only a waiting watch hands off a pipeline fix"
    if t["col"] not in ("backlog", "doing"):
        return f"#{t['id']} is {t['col']}: only a step that is still watching can wait for a fix"
    if (rid := (t.get("wait") or {}).get("fixing")) and (r := runs.find_run(crew, rid)) and r["status"] == "running":
        return f"#{t['id']} already waits for {r['id']} ({r['title']})"
    run = runs.find_run(crew, (t.get("run") or {}).get("id"))
    if not run or run["status"] != "running":
        return f"#{t['id']}'s workflow is not running"
    return None


def _brief(crew, t: dict, reason: str) -> str:
    w = t.get("wait") or {}
    pr = w.get("probe") or {}
    run = runs.find_run(crew, (t.get("run") or {}).get("id")) or {}
    saw = (w.get("last") or t.get("summary") or "").strip()
    pr_watch = is_pr_watch(crew, t)
    parts = [f"PR watch: #{t['id']} {t['title']} ({t.get('who') or 'its agent'}). Its PR is blocked by a failure that is on "
             f"master too: fix it on master, not in that PR ({', '.join(t['prs'])})." if pr_watch else
             f"Deploy watch: #{t['id']} {t['title']} ({t.get('who') or 'its agent'})."]
    if reason:
        parts.append(f"Why a fix: {reason}")
    if t.get("pipes"):
        parts.append("Failed pipeline run: " + t["pipes"][-1] + ("" if len(t["pipes"]) < 2 else
                                                                 "\nEarlier runs it followed: " + ", ".join(t["pipes"][:-1])))
    if pr.get("cmd"):
        parts.append(f"Its watch: `{pr['cmd']}`\nLast output:\n{(pr.get('out') or '(none)')[:600]}")
    if saw:
        parts.append(f"What {t.get('who') or 'the waiting agent'} reported:\n{runs.cut(saw, BRIEF_MAX, t['id'])}")
    if run and not pr_watch:
        parts.append(f"The change it deploys: {run.get('title', '')} (workflow {run.get('name', '')} {run.get('id', '')})"
                     + (f"\nPRs: {', '.join(t['prs'])}" if t.get("prs") else ""))
    if ado := t.get("ado") or run.get("ado"):
        parts.append(f"Work item for the AB# link: {ado.get('url') or ado.get('id') or ado}")
    return "\n\n".join(parts)


def start(crew, t: dict, reason: str = "", join: str | None = None, by: str = "you") -> dict:
    """Hand the card's failed pipeline to a fix run (a new one, or `join` a running one) and park the card on it."""
    if err := can_fix(crew, t):
        return {"error": err}
    if join:
        fx = runs.find_run(crew, join)
        if not is_fix_run(fx) or fx["status"] != "running":
            return {"error": f"{join} is not a running {NAME} run"}
        fx["source"].setdefault("tasks", []).append(t["id"])
        fx["details"] = (fx["details"] + "\n\n---\nAlso waiting on this fix:\n" + _brief(crew, t, reason))[:runs.SUMMARY_MAX]
    else:
        err, w = workflow(crew)
        if err:
            return {"error": err}
        pipe = (t.get("pipes") or [""])[-1]
        n = (re.search(r"(?:buildId=|/runs/)(\d+)", pipe) or [None, ""])[1]
        src_run = runs.find_run(crew, (t.get("run") or {}).get("id")) or {}
        ado = t.get("ado") or src_run.get("ado")
        title = f"failed run {n} for #{t['id']}" if n else f"failed pipeline for #{t['id']}"
        fx = runs.start_run(crew, w, title, _brief(crew, t, reason),
                            {"kind": KIND, "tasks": [t["id"]], "pipe": pipe, "by": by, **({"ado": ado} if ado else {})})
        if fx["status"] != "running":
            return {"error": f"the {w['name']} run could not start: {fx.get('note') or 'see the feed'}"}
    w = t.setdefault("wait", {"checks": 0})
    w["fixing"] = fx["id"]
    w["fix_for"] = "pr" if is_pr_watch(crew, t) else "deploy"
    w.pop("wake", None)  # the failure it was woken for is the fix run's job now
    w.pop("fix", None)
    crew.event("workflow", f"🛠 #{t['id']} waits for {fx['name']} {fx['id']}" + (" (joined)" if join else ""), t.get("who"))
    crew.touch()
    return {"ok": True, "run": fx["id"], "joined": bool(join)}


def tick(crew) -> None:
    """A fix run that ended (merged, stopped, failed or deleted) wakes every card waiting on it. Lock held."""
    for t in crew.tasks:
        w = t.get("wait") or {}
        if not (rid := w.get("fixing")):
            continue
        fx = runs.find_run(crew, rid)
        if fx and fx["status"] == "running":
            continue
        w.pop("fixing")
        status = fx["status"] if fx else "deleted"
        prs = list(dict.fromkeys(u for k in (fx or {}).get("tasks") or [] if k and (x := crew._task(k)) for u in x.get("prs") or []))
        last = next((r for r in reversed((fx or {}).get("results") or []) if r), None)
        w["fix"] = {"run": rid, "status": status, "note": (fx or {}).get("note", ""), "prs": prs,
                    "said": runs.cut((last or {}).get("summary", ""), 2500, (fx or {}).get("tasks", [None])[-1]),
                    "who": (last or {}).get("who", ""), "t": time.time()}
        own = runs.find_run(crew, (t.get("run") or {}).get("id"))
        if t["col"] == "backlog" and crew._until_step(t) and own and own["status"] == "running":
            w.update(wake=f"fix {'merged' if status == 'done' else status}", next=0)
            if pr := w.get("probe"):
                pr["since"] = time.time()  # the time spent fixing doesn't count against the watch
        crew.event("workflow", f"🛠 {NAME} {rid} {'finished' if status == 'done' else status} · "
                               + (f"waking #{t['id']}" if w.get("wake", "").startswith("fix") else f"#{t['id']} is {t['col']}"), t.get("who"))
        crew.touch()


def recheck_note(w: dict) -> str:
    """What the waiting agent reads when the fix run woke it."""
    fx = w.get("fix") or {}
    if not fx or not str(w.get("wake") or "").startswith("fix"):
        return ""
    ok = fx["status"] == "done"
    out = [f"The {NAME} run {fx['run']} {'finished: the fix merged' if ok else 'ended without merging a fix (' + fx['status'] + ')'}."]
    if fx.get("prs"):
        out.append("Its PR: " + ", ".join(fx["prs"]))
    if fx.get("note"):
        out.append(f"Run note: {fx['note']}")
    if fx.get("said"):
        out.append(f"Its last step ({fx.get('who') or 'agent'}) said:\n{fx['said']}")
    if w.get("fix_for") == "pr":
        out.append("Now bring the fix into your PR: merge origin/master into its branch and push (that re-runs its checks; "
                   "if the branch already has the fix, re-run the failed check instead), set your watch again and carry on "
                   "as before. " + ("" if ok else "If the fix run said the fix is already on master or in another PR, take "
                                    "that one; if it said a person must act, say so and raise_alert."))
        return "\n".join(out) + "\n\n"
    out.append("Now find the first run of the pipeline on master whose source commit contains both the fix's merge commit "
               "and this change's (`git merge-base --is-ancestor <commit> <run commit>`), set your watch on that run and "
               "carry on as before. " + ("" if ok else "If the fix run said the fix is already on master or another PR, "
                                         "follow that one; if it said a person must act, say so and raise_alert."))
    return "\n".join(out) + "\n\n"

