"""Workflow runs: starting one, advancing it as each step's task finishes, delivering the result. A run is a dict in crew.runs.
Functions take the Crew first and run with its lock held; the Crew keeps one-line methods (crew._start_run, ...) that call them."""
from __future__ import annotations

import json
import re
import threading
import time


def now() -> float:
    return time.time()


def fill(template: str, ctx: dict) -> str:
    """{name} placeholders from ctx; unknown ones are left as they are."""
    return re.sub(r"\{(\w+)\}", lambda m: str(ctx[m.group(1)]) if m.group(1) in ctx else m.group(0), template)


def find(crew, wid) -> dict | None:
    return next((w for w in crew.config.get("workflows", []) if w["id"] == wid), None)


def find_run(crew, rid) -> dict | None:
    return next((r for r in crew.runs if r["id"] == rid), None)


def start_run(crew, w: dict, title: str, details: str = "", source: dict | None = None) -> dict:
    n = len(w["steps"])
    run = {"id": f"r{crew.next_run}", "wf": w["id"], "name": w["name"], "title": title[:200],
           "details": (details or "").strip()[:4000], "steps": json.loads(json.dumps(w["steps"])),
           "status": "running", "step": 0, "tasks": [None] * n, "results": [None] * n, "started": now(), "note": "",
           "source": source, "ado": (source or {}).get("ado")}
    crew.next_run += 1
    crew.runs.append(run)
    start_step(crew, run, 0)
    return run


def start_step(crew, run: dict, i: int) -> None:
    step, n = run["steps"][i], len(run["steps"])
    a = crew.agents.get(step["agent"])
    if not a:
        run.update(status="failed", note=f"step {i + 1}: no agent called {step['agent']}", ended=now())
        crew.event("error", f"workflow {run['name']} stopped: {step['agent']} is not in the crew")
        return
    prev = run["results"][i - 1] if i else None
    ctx = {"input": run["title"], "details": run["details"], "step": i + 1, "steps": n, "workflow": run["name"],
           "prev_summary": (prev or {}).get("summary", ""), "prev_who": (prev or {}).get("who", ""),
           "prev_branch": (prev or {}).get("branch", ""), "prev_commit": (prev or {}).get("commit", "")}
    title = fill(step.get("title") or "{input}", ctx).strip()[:200] or run["title"]
    body = fill(step.get("details") or "", ctx).strip()
    brief = [f"Workflow '{run['name']}', step {i + 1} of {n}. Goal: {run['title']}"]
    if run["details"]:
        brief.append(run["details"])
    for k, r in enumerate(run["results"][:i]):  # every earlier step, newest in full
        if not r:
            continue
        where = f" on {r['branch']} (commit {r['commit']})" if r.get("commit") else ""
        text = r["summary"] if k == i - 1 else r["summary"][:400]
        brief.append(f"Step {k + 1} by {r['who']}{where}:\n{text}")
    if prev and prev.get("commit") and prev.get("branch"):
        brief.append(f"Run `git merge {prev['branch']}` in your worktree first so you build on that work.")
    t = {"id": crew.next_id, "title": title, "details": ((body + "\n\n---\n") if body else "") + "\n\n".join(brief),
         "tag": (a["tags"] or ["feature"])[0], "col": "backlog", "who": a["name"], "pinned": a["name"], "created": now(),
         "run": {"id": run["id"], "step": i, "name": run["name"], "n": n}}
    if run.get("ado"):
        t["ado"] = run["ado"]
    crew.next_id += 1
    crew.tasks.append(t)
    run["step"], run["tasks"][i] = i, t["id"]
    crew.event("workflow", f"⛓ {run['name']} {i + 1}/{n} → {a['name']}: {title}", a["name"])


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
        run["results"][i] = {"who": t.get("who") or "", "summary": (t.get("summary") or "")[:3000],
                             "branch": w["branch"] if w and w.get("branch") else "", "commit": t.get("commit") or ""}
        run["note"] = ""
        if i + 1 < len(run["steps"]):
            start_step(crew, run, i + 1)
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
                                 + (f"From {run['name']}: {last['summary'][:2000]}" if last else ""), {"workflow": run["id"]})
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
