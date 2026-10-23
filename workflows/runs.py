"""Workflow runs: starting one, advancing it as each step's task finishes, delivering the result. A run is a dict in crew.runs.
Functions take the Crew first and run with its lock held; the Crew keeps one-line methods (crew._start_run, ...) that call them."""
from __future__ import annotations

import json
import re
import threading
import time

from . import probe


def now() -> float:
    return time.time()


def fill(template: str, ctx: dict) -> str:
    """{name} placeholders from ctx; unknown ones are left as they are."""
    return re.sub(r"\{(\w+)\}", lambda m: str(ctx[m.group(1)]) if m.group(1) in ctx else m.group(0), template)


def find(crew, wid) -> dict | None:
    return next((w for w in crew.config.get("workflows", []) if w["id"] == wid), None)


def find_run(crew, rid) -> dict | None:
    return next((r for r in crew.runs if r["id"] == rid), None)


# What a step passes on. The previous step's summary goes to the next one in full: it often carries blocks the next
# agent must copy verbatim (ADO comments, tables), so a cut there loses work. Earlier steps get a shorter excerpt.
SUMMARY_MAX = 20000
EARLIER_MAX = 4000


def groups(steps: list) -> list[list[int]]:
    """Step indexes in the order they run. A step with "par" runs at the same time as the step before it, so a group is
    one step and the parallel steps right after it; the next group starts once every step in this one is done."""
    out = []
    for i, st in enumerate(steps):
        if i and st.get("par"):
            out[-1].append(i)
        else:
            out.append([i])
    return out


def group_of(steps: list, i: int) -> list[int]:
    return next(g for g in groups(steps) if i in g)


def start_run(crew, w: dict, title: str, details: str = "", source: dict | None = None) -> dict:
    n = len(w["steps"])
    run = {"id": f"r{crew.next_run}", "wf": w["id"], "name": w["name"], "title": title[:200],
           "details": (details or "").strip()[:SUMMARY_MAX], "steps": json.loads(json.dumps(w["steps"])),
           "status": "running", "step": 0, "tasks": [None] * n, "results": [None] * n, "started": now(), "note": "",
           "source": source, "ado": (source or {}).get("ado")}
    crew.next_run += 1
    crew.runs.append(run)
    start_group(crew, run, 0)
    return run


def start_group(crew, run: dict, i: int) -> None:
    """Start step i and the steps that run alongside it. run["step"] is the group's first step."""
    for k in group_of(run["steps"], i):
        start_step(crew, run, k)
        if run["status"] != "running":
            return
    run["step"] = i


def start_step(crew, run: dict, i: int) -> None:
    step, n = run["steps"][i], len(run["steps"])
    a = crew.agents.get(step["agent"])
    if not a:
        run.update(status="failed", note=f"step {i + 1}: no agent called {step['agent']}", ended=now())
        crew.event("error", f"workflow {run['name']} stopped: {step['agent']} is not in the crew")
        return
    gs = groups(run["steps"])
    g = next(k for k, x in enumerate(gs) if i in x)
    before = [k for x in gs[:g - 1] for k in x] if g else []  # older steps: an excerpt
    last = [k for k in gs[g - 1] if run["results"][k]] if g else []  # the group just finished: in full
    prevs = [run["results"][k] for k in last]
    prev = prevs[0] if len(prevs) == 1 else None
    joined = "\n\n".join(f"{r['who']}:\n{r['summary']}" for r in prevs) if len(prevs) > 1 else ""
    ctx = {"input": run["title"], "details": run["details"], "step": i + 1, "steps": n, "workflow": run["name"],
           "prev_summary": (prev or {}).get("summary", joined), "prev_who": ", ".join(r["who"] for r in prevs),
           "prev_branch": (prev or {}).get("branch", ""), "prev_commit": (prev or {}).get("commit", "")}
    title = fill(step.get("title") or "{input}", ctx).strip()[:200] or run["title"]
    body = fill(step.get("details") or "", ctx).strip()
    side = [run["steps"][k]["agent"] for k in gs[g] if k != i]
    brief = [f"Workflow '{run['name']}', step {i + 1} of {n}. Goal: {run['title']}"
             + (f" {', '.join(side)} {'works' if len(side) == 1 else 'work'} on {'its' if len(side) == 1 else 'their'} "
                f"own step{'s' if len(side) > 1 else ''} at the same time: stick to yours." if side else "")]
    if run["details"]:
        brief.append(run["details"])
    for k in before + last:  # every earlier step, the group just finished in full
        r = run["results"][k]
        if not r:
            continue
        where = f" on {r['branch']} (commit {r['commit']})" if r.get("commit") else ""
        text = r["summary"] if k in last else r["summary"][:EARLIER_MAX]
        brief.append(f"Step {k + 1} by {r['who']}{where}:\n{text}")
    merge = list(dict.fromkeys(r["branch"] for r in prevs if r.get("commit") and r.get("branch")))
    if merge:
        brief.append(f"Run `git merge {' '.join(merge)}` in your worktree first so you build on that work.")
    if step.get("gate") == "until":
        brief.append(until_brief(step))
    t = {"id": crew.next_id, "title": title, "details": ((body + "\n\n---\n") if body else "") + "\n\n".join(brief),
         "tag": (a["tags"] or ["feature"])[0], "col": "backlog", "who": a["name"], "pinned": a["name"], "created": now(),
         "run": {"id": run["id"], "step": i, "name": run["name"], "n": n}}
    if run.get("ado"):
        t["ado"] = run["ado"]
    crew.next_id += 1
    crew.tasks.append(t)
    run["tasks"][i] = t["id"]
    crew.event("workflow", f"⛓ {run['name']} {i + 1}/{n}{' ∥' if len(gs[g]) > 1 else ''} → {a['name']}: {title}", a["name"])


WATCH_EXAMPLE = 'gh pr view 123 --json state,reviewDecision,statusCheckRollup --jq \'[.state,.reviewDecision,[.statusCheckRollup[]|select(.conclusion=="FAILURE")|"FAILED \\(.name) \\(.detailsUrl)"]]\''


def until_brief(step: dict) -> str:
    return (f"This step lasts until its goal is reached, which can take hours or days (a PR merging, a deployment landing). "
            f"Do what you can now. When the goal is reached, call the crew tool `decide` with verdict continue; if it can "
            f"never be reached, call it with verdict stop. Otherwise, before you end your turn with a short status, call the "
            f"crew tool `watch` with one read-only gh, az or git command that prints the state and the link of every failed "
            f"check (e.g. `{WATCH_EXAMPLE}`; no timestamps or counters that change every run), `done` as a regex for the "
            f"goal (e.g. MERGED) and `fail` as a regex for what you must act on (e.g. FAILED|CHANGES_REQUESTED). Crew runs it "
            f"every {probe.WATCH_EVERY} minutes at no cost and wakes you, in this same session, when it matches done, or "
            f"matches fail with a new failure: then fix that failure yourself. Without a watch Crew wakes you every "
            f"{step.get('every', 30)} minutes. Up to {step.get('checks', 96)} checks, then a person takes over.")


def recheck_body(t: dict, step: dict) -> str:
    """What an "until" step's agent gets when Crew asks it to look again."""
    w = t.get("wait") or {}
    last = " ".join((w.get("last") or "").split())[:400]
    pr = w.get("probe") or {}
    seen = (f"Your watch `{pr['cmd']}` woke you ({w.get('wake')}). It now prints:\n{pr.get('out') or '(nothing)'}\n\n"
            if pr and w.get("wake") else "")
    if pr and w.get("wake") in ("failed", "output changed"):
        seen += ("If a CI check or pipeline failed: open its run (the link above, or `gh pr checks <number>` for the links), "
                 "find the cause in its log, fix it on this same branch and push, then re-run what failed if the push does "
                 "not re-run it (`gh run rerun <id> --failed`, or queue the Azure pipeline again). If review asked for "
                 "changes, make them. Then end your turn with a short status: your watch keeps running.\n\n")
    return (f"Task #{t['id']}: check again ({w.get('checks', 0) + 1} of {step.get('checks', 96)}): {t['title']}\n\n"
            + seen + (f"Last time you reported: {last}\n\n" if last else "")
            + "Look at the current state. Goal reached: call `decide` with continue and summarize. Can never be reached: "
              f"`decide` stop. Still waiting: end with one or two lines of status; your watch stays set (call `watch` "
              f"again to change it), or without one Crew asks again in {step.get('every', 30)} min.")


def on_task_event(crew, t: dict, col: str, summary: str | None) -> None:
    ref = t.get("run") or {}
    run = find_run(crew, ref.get("id"))
    if not run or run["status"] != "running" or run["tasks"][ref["step"]] != t["id"]:
        return
    i, step = ref["step"], run["steps"][ref["step"]]
    if col == "error":
        run["note"] = f"step {i + 1} hit an error; it is back in the backlog and the run continues when it finishes"
    elif col == "review" and step.get("gate") == "auto":
        crew._jobs.append(("approve", t["id"], "workflow"))  # committed outside the lock; "done" then advances the run
    elif col == "done":
        w = crew.agents.get(t.get("who") or "")
        run["results"][i] = {"who": t.get("who") or "", "summary": (t.get("summary") or "")[:SUMMARY_MAX],
                             "branch": w["branch"] if w and w.get("branch") else "", "commit": t.get("commit") or ""}
        run["note"] = ""
        g = group_of(run["steps"], i)
        if waiting := [k for k in g if not run["results"][k]]:  # a parallel step is still going: the next group waits
            run["note"] = "waiting for " + ", ".join(run["steps"][k]["agent"] for k in waiting)
        elif g[-1] + 1 < len(run["steps"]):
            start_group(crew, run, g[-1] + 1)
        else:
            run.update(status="done", ended=now())
            crew.event("workflow", f"⛓ {run['name']} finished: {run['title']}")
            output(crew, run)


def report(crew, run: dict) -> str:
    out = [f"# {run['name']}: {run['title']}", "", f"Finished {time.strftime('%Y-%m-%d %H:%M', time.localtime(run['ended']))}"
           f" · {len(run['steps'])} steps", ""]
    if run["details"]:
        out += [run["details"], ""]
    for k, r in enumerate(run["results"]):
        if r:
            where = f" · `{r['branch']}` @ `{r['commit']}`" if r.get("commit") else ""
            out += [f"## {k + 1}. {r['who']}{where}", "", r["summary"] or "(no summary)", ""]
    return "\n".join(out)


def output(crew, run: dict) -> None:
    """Deliver a finished run: a markdown report, a webhook, or the next workflow."""
    w = find(crew, run["wf"]) or {}
    o = w.get("output") or {}
    kind = o.get("kind") or "board"
    try:
        if kind == "report":
            d = crew.project / (o.get("path") or "crew-reports")
            d.mkdir(parents=True, exist_ok=True)
            slug = re.sub(r"[^a-z0-9]+", "-", run["title"].lower()).strip("-")[:40] or "run"
            f = d / f"{time.strftime('%Y%m%d-%H%M')}-{run['id']}-{slug}.md"
            f.write_text(report(crew, run), encoding="utf-8")
            run["output"] = str(f.relative_to(crew.project)) if f.is_relative_to(crew.project) else str(f)
            crew.event("workflow", f"⛓ {run['name']} report saved: {run['output']}")
        elif kind == "webhook" and o.get("url"):
            body = json.dumps({"workflow": run["name"], "run": run["id"], "title": run["title"], "status": run["status"],
                               "report": report(crew, run), "steps": run["results"]}).encode()

            def post(url=o["url"], name=run["name"]):
                import urllib.request
                try:
                    urllib.request.urlopen(urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"}), timeout=20).close()
                except OSError as e:
                    with crew.lock:
                        crew.event("error", f"⛓ {name} webhook failed: {e}")
            threading.Thread(target=post, daemon=True).start()
            run["output"] = "webhook sent"
        elif kind == "workflow" and (nxt := find(crew, o.get("workflow"))):
            if nxt.get("off"):
                crew.event("workflow", f"⛓ {run['name']} did not start {nxt['name']}: it is turned off")
                run["output"] = f"{nxt['name']} is off"
                return
            if run.get("depth", 0) >= crew.MAX_CHAIN:
                crew.event("error", f"⛓ {run['name']} did not start {nxt['name']}: too many chained workflows")
                return
            last = next((r for r in reversed(run["results"]) if r), None)
            nr = start_run(crew, nxt, run["title"], ((run["details"] + "\n\n") if run["details"] else "")
                                 + (f"From {run['name']}: {last['summary'][:SUMMARY_MAX]}" if last else ""), {"workflow": run["id"]})
            nr["depth"] = run.get("depth", 0) + 1
            run["output"] = f"started {nxt['name']}"
    except OSError as e:
        crew.event("error", f"⛓ {run['name']} output failed: {e}")


def decide(crew, p: dict) -> dict | None:
    src = crew.agents.get(p.get("from") or "")
    verdict, reason = p.get("verdict"), " ".join(str(p.get("reason") or "").split())[:300]
    if verdict not in ("continue", "stop"):
        return {"error": "verdict: continue or stop"}
    if not reason:
        return {"error": "say why in `reason`"}
    tk = crew._task(src["task"]) if src and src.get("task") else None
    if not tk or tk["col"] != "doing":
        return {"error": "decide works on the task you are running now"}
    tk["decision"] = {"verdict": verdict, "reason": reason, "by": src["name"], "t": now()}
    ref = tk.get("run") or {}
    run = find_run(crew, ref.get("id"))
    if verdict == "stop" and run and run["status"] == "running" and run["tasks"][ref["step"]] == tk["id"]:
        run.update(status="stopped", ended=now(), note=f"step {ref['step'] + 1}: {src['name']} stopped it: {reason}")
    crew.event("decide", f"{'⏹' if verdict == 'stop' else '→'} {src['name']} {'stopped' if verdict == 'stop' else 'passed'} "
                         f"#{tk['id']}: {reason}", src["name"])
    crew.touch()
    return {"ok": True, "note": "applies when your turn ends: finish with a short summary"}


def stop(crew, run: dict) -> dict | None:
    run.update(status="stopped", ended=now())
    crew.event("workflow", f"⛓ {run['name']} stopped")


def delete(crew, run: dict) -> dict | None:
    crew.runs.remove(run)
