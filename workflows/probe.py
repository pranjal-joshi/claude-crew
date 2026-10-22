"""A ⟳ step's watch: a cheap read-only command the step's agent sets (crew tool `watch`) and Crew runs every
WATCH_EVERY minutes instead of waking the agent. The agent wakes only when the output matches `done` (the goal) or
`fail` (something to fix, such as a failed CI run), or, with no `fail` set, when the output changes. A PR that takes two
days to merge costs a few turns, not one every half hour. Watch runs don't count as the step's checks; the watch lasts
as long as those checks would (checks × every minutes), then the agent gets a last look."""
from __future__ import annotations

import hashlib
import os
import re
import shlex
import shutil
import subprocess
import threading
import time

# Crew runs these unattended, outside the agent's permission rules: read-only subcommands only.
READS = {
    "gh": (("pr", "view"), ("pr", "checks"), ("pr", "list"), ("pr", "status"), ("run", "view"), ("run", "list"), ("api",)),
    "az": (("pipelines", "runs", "list"), ("pipelines", "runs", "show"), ("pipelines", "build", "list"),
           ("pipelines", "build", "show"), ("repos", "pr", "show"), ("repos", "pr", "list"), ("boards", "work-item", "show")),
    "git": (("log",), ("ls-remote",), ("rev-parse",), ("merge-base",)),
}
WRITE_FLAGS = {"-X", "--method", "-f", "-F", "--field", "--raw-field", "--input"}  # gh api writes with these
AZ_UNSAFE = re.compile(r'[%^&|<>"!`\x00-\x1f]')  # az is a .cmd file on Windows: cmd.exe would read these
SHELL = re.compile(r"[;&|<>`\n\r]|\$\(")
OUT_MAX, TIMEOUT = 1500, 60
WATCH_EVERY = 2  # minutes between watch runs: cheap, no model, so CI results arrive quickly


def parse(cmd: str) -> tuple[str | None, list[str]]:
    """(error, argv) for a watch command; no shell, so no pipes: use gh's --jq or az's --query to trim the output."""
    cmd = " ".join(str(cmd or "").split())
    if not cmd:
        return "give a command", []
    if SHELL.search(cmd):
        return "one command, no pipes or redirects: trim with gh --jq or az --query", []
    try:
        argv = shlex.split(cmd)
    except ValueError as e:
        return f"cannot read the command: {e}", []
    tool, rest = argv[0], argv[1:]
    words = [w for w in rest if not w.startswith("-")]
    if tool not in READS or not any(tuple(words[:len(p)]) == p for p in READS[tool]):
        return "read-only commands only: " + "; ".join(f"{t} {' '.join(p)}" for t, ps in READS.items() for p in ps), []
    if WRITE_FLAGS & {w.split("=")[0] for w in rest}:
        return "gh api is read-only here: no -X/--method/-f/-F/--input", []
    if tool == "az" and any(AZ_UNSAFE.search(a) for a in rest):
        return "az runs through cmd.exe on Windows: no quotes or % ^ & | < > ! in its arguments", []
    return None, argv


def run(argv: list[str], cwd) -> tuple[str, bool]:
    """(output, ok). A failing command's error text is its output: it still wakes the agent when it changes."""
    exe = shutil.which(argv[0])
    if not exe:
        return f"{argv[0]} not found", False
    try:
        r = subprocess.run([exe, *argv[1:]], cwd=cwd, capture_output=True, text=True, encoding="utf-8", errors="replace",
                           timeout=TIMEOUT, stdin=subprocess.DEVNULL, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0)
    except subprocess.TimeoutExpired:
        return f"timed out after {TIMEOUT} s", False
    except OSError as e:
        return f"could not start: {e}", False
    out = (r.stdout if not r.returncode else (r.stderr or r.stdout)).strip()
    return out[:OUT_MAX * 4], not r.returncode


def digest(out: str) -> str:
    return hashlib.sha1(out.encode("utf-8", "replace")).hexdigest()


def set_watch(crew, p: dict) -> dict:
    """Crew tool `watch`: the running agent puts a command on its ⟳ step; an empty command removes it."""
    src = crew.agents.get(p.get("from") or "")
    t = crew._task(src["task"]) if src and src.get("task") else None
    if not t or t["col"] != "doing" or not crew._until_step(t):
        return {"error": "watch works on a ⟳ (check until done) workflow step you are running now"}
    w = t.setdefault("wait", {"checks": 0})
    if not str(p.get("command") or "").strip():
        w.pop("probe", None)
        return {"ok": True, "note": "watch removed: Crew wakes you on the step's schedule again"}
    err, argv = parse(p["command"])
    done, fail = (str(p.get(k) or "").strip()[:200] for k in ("done", "fail"))
    for k, rx in (("done", done), ("fail", fail)):
        if not err and rx:
            try:
                re.compile(rx)
            except re.error as e:
                err = f"{k}: not a regular expression ({e})"
    if err:
        return {"error": err}
    old = w.get("probe") or {}
    w["probe"] = {"cmd": shlex.join(argv), "done": done, "fail": fail, "since": old.get("since") or time.time()}
    if old.get("failed"):
        w["probe"]["failed"] = old["failed"]  # the failure the agent already saw does not wake it again
    crew.event("wait", f"👁 {src['name']} watches #{t['id']}: {w['probe']['cmd'][:120]}", src["name"])
    crew.touch()
    wakes = [x for x in ("matches done" if done else "", "matches fail with a new failure" if fail else "",
                         "" if fail else "changes") if x]
    return {"ok": True, "note": f"Crew runs it every {WATCH_EVERY} min and wakes you only when its output "
                                + " or ".join(wakes) + ". End your turn with a short status."}


def tick(crew) -> None:
    """Start due watches (dispatcher thread, every second). Each runs in its own thread, outside the lock."""
    with crew.lock:
        due = [t for t in crew.tasks if t["col"] == "backlog" and (w := t.get("wait") or {}).get("probe")
               and not w.get("wake") and time.time() - w.get("probing", 0) > TIMEOUT * 2
               and w.get("next", 0) <= time.time() and not t.get("hold")]
        for t in due:
            t["wait"]["probing"] = time.time()  # a stamp, so a restart mid-run cannot leave it stuck
    for t in due:
        threading.Thread(target=_probe, args=(crew, t), daemon=True).start()


def _probe(crew, t: dict) -> None:
    pr = t["wait"]["probe"]
    out, _ok = run(shlex.split(pr["cmd"]), crew.project)
    with crew.lock:
        w = t.get("wait") or {}
        w.pop("probing", None)
        if w.get("probe") is not pr or t["col"] != "backlog":
            return  # replaced or moved while it ran
        step = crew._until_step(t)
        if not step:
            return
        h, first = digest(out), "seen" not in pr
        hit = bool(pr["done"] and re.search(pr["done"], out))
        failed = bool(pr.get("fail") and re.search(pr["fail"], out)) and h != pr.get("failed")
        changed = not first and h != pr["seen"] and not pr.get("fail")  # with a fail pattern, progress alone wakes nobody
        over = time.time() - pr.setdefault("since", time.time()) >= step["checks"] * step["every"] * 60
        pr.update(seen=h, ran=time.time(), out=out[:OUT_MAX])
        w["watched"] = w.get("watched", 0) + 1
        if failed:
            pr["failed"] = h
        if hit or failed or changed or over:
            w.update(wake=("done matched" if hit else "failed" if failed else "output changed" if changed else "last check"), next=0)
            if over and not hit:
                w["checks"] = max(w.get("checks", 0), step["checks"] - 1)  # this look is the last one, then a person takes over
            crew.event("wait", f"👁 #{t['id']}: {w['wake']} · waking {t.get('who') or 'its agent'}", t.get("who"))
        else:
            w["next"] = time.time() + WATCH_EVERY * 60
    crew.touch()
