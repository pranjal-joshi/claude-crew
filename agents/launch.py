"""Agent processes: worktrees, the claude command line, sending, killing, pidfile, sessions. Functions take the Crew first;
the Crew keeps one-line methods (crew._start, crew._send, ...) that call them."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

from . import procs
from .files import local_mcp_servers, sync_claude_files
from .procs import CREW_SERVER_RE, IS_WIN, StdinWriter, orphans_to_kill
from .util import git, write_atomic

APP_DIR = Path(__file__).resolve().parent.parent  # crew_mcp.py sits next to server.py (flat) or next to this package (installed)


def now() -> float:
    return time.time()


def ensure_worktree(crew, a: dict) -> None:
    if not crew.git or a["branch"]:
        return
    branch = f"crew/{a['name']}"
    path = crew.work_root / "wt" / crew.key / a["name"]
    if not (path / ".git").exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        git(crew.project, "worktree", "prune")  # a stale registration for a deleted folder would block the add
        # An existing branch keeps its approved commits: check it out, never reset it
        have = git(crew.project, "rev-parse", "--verify", "-q", f"refs/heads/{branch}").returncode == 0
        r = git(crew.project, "worktree", "add", str(path), branch) if have else git(crew.project, "worktree", "add", "-b", branch, str(path), "HEAD")
        if r.returncode != 0:
            crew.log(a, "error", "worktree failed: " + r.stderr.strip())
            return
    a["cwd"], a["branch"] = str(path), branch


def start(crew, a: dict) -> bool:
    if crew._alive(a["name"]):
        return True
    boss = a.get("kind") == "boss"
    if not boss:
        ensure_worktree(crew, a)
    team = ", ".join(f"{x['name']} ({', '.join(x['tags']) or 'no tags'})" for x in crew.agents.values())
    if boss:
        role = (f"You are the boss of Crew, a team of Claude Code agents working on the project {crew.project}. "
                f"The crew: {team}. You do not write code yourself: you plan, delegate and coordinate through the crew "
                "tools (crew_status, create_task, assign_task, message_agent, stop_agent, run_workflow, add_agent, "
                "update_agent, save_workflow, add_trigger). Call crew_status before acting. Prefer existing agents and "
                "workflows; add or change them only when the request needs it. Give tasks and runs short titles people "
                "can scan: about 6 plain words saying what to do, details in details. Keep replies short: what you did, "
                "task numbers, and what the user should approve next. Read files only to understand the project.")
    else:
        role = (f"You are '{a['name']}', one of {len(crew.agents)} Claude Code agents working in parallel on this project. "
                f"Your role: {a['role']} Teammates: {team}. "
                f"Your working folder is {a['cwd']}" + (f", a git worktree of the project {crew.project}" if a['branch'] else "") + ". "
                "Stay inside it: do not read, create or change files outside this folder unless the task explicitly names a path outside it. "
                "Never touch other agents' worktrees. "
                + ("When your finished work clearly needs another specialist next, call the crew handoff tool once "
                   "(to a teammate's name, or 'auto' with a tag); say what you did and where. Not in a workflow step: there the "
                   "workflow starts the next step, so put what comes next in your summary. " if crew.config.get("auto_handoff", True) else "")
                + "If you find something failing that a person must see now (a failed deployment, a red pipeline on the main branch, "
                "an outage, a security problem, a blocked dependency), call the crew raise_alert tool with a short title and the evidence; "
                "call resolve_alert with the same title once it recovers. Not for your own task's normal progress or failures you are fixing. "
                "When you finish a task, end with a short summary: what you changed, files touched, and anything left open.")
    mode = "acceptEdits" if boss else crew.config["permission_mode"]
    cmd = [*crew.claude, "-p", "--input-format", "stream-json", "--output-format", "stream-json", "--verbose",
           "--permission-mode", mode, "--append-system-prompt", role]
    if boss:  # coordinates, never edits: crew tools and read-only tools only
        cmd += ["--allowedTools", "mcp__crew", "Read", "Grep", "Glob", "WebSearch", "WebFetch", "Bash(git log:*)", "Bash(git status:*)",
                "--disallowedTools", "Edit", "Write", "MultiEdit", "NotebookEdit"]
    elif mode == "bypassPermissions":
        cmd += ["--dangerously-skip-permissions"]
    else:
        allowed = list(crew.config.get("allowed_tools") or []) + ["mcp__crew"] + crew._once_rules(a)
        if crew.config.get("allow_mcp", True):
            allowed += sorted(crew.mcp_allow)  # e.g. mcp__azure-devops: every tool on that server
        if allowed:
            cmd += ["--allowedTools", *allowed]
    if a["effort"]:
        cmd += ["--effort", a["effort"]]
    a["launched"] = crew._launch_key(a)
    if a["model"]:
        cmd += ["--model", a["model"]]
    if a["session"]:
        cmd += ["--resume", a["session"]]
    # Same setup as `claude` in a terminal: user-level skills, plugins, MCP servers and claude.ai
    # connectors load on their own; these add what is tied to the project folder.
    if a["branch"]:
        if copied := sync_claude_files(crew.project, Path(a["cwd"])):
            crew.log(a, "sys", f"synced {len(copied)} local Claude file{'s' if len(copied) != 1 else ''} into the worktree")
    servers = {} if boss else local_mcp_servers(crew.project)
    servers["crew"] = {"type": "stdio", "command": sys.executable, "args": [str(APP_DIR / "crew_mcp.py")],
                       "env": {"CREW_URL": f"http://127.0.0.1:{crew.port}", "CREW_AGENT": a["name"], "CREW_ROLE": "boss" if boss else "agent",
                               "CREW_TOKEN": crew.mcp_token}}
    f = crew.work_root / "mcp" / f"{crew.key}-{a['name']}.json"
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(json.dumps({"mcpServers": servers}, indent=1), encoding="utf-8")
    cmd += [f"--mcp-config={f}"]  # "=" form: the flag is variadic and would swallow what follows
    if boss:  # the boss only needs the crew tools and a few readers: a small context keeps every turn cheap
        cmd += ["--strict-mcp-config", "--disable-slash-commands", "--tools=Read,Grep,Glob,WebSearch,WebFetch,Bash"]
    flags = subprocess.CREATE_NEW_PROCESS_GROUP if IS_WIN else 0
    try:
        p = subprocess.Popen(cmd, cwd=a["cwd"], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                             text=True, encoding="utf-8", errors="replace", bufsize=1, creationflags=flags,
                             start_new_session=not IS_WIN)
    except OSError as e:
        a["status"], a["error"] = "error", f"could not start claude: {e}"
        crew.log(a, "error", a["error"])
        return False
    crew.procs[a["name"]] = p
    crew.writers[a["name"]] = StdinWriter(p)
    crew._pid_add(p.pid, a["name"])
    a["proc_cost"] = 0.0
    a["error"] = None
    threading.Thread(target=crew._read, args=(a, p), daemon=True).start()
    return True


IMAGE_TYPES = ("image/png", "image/jpeg", "image/gif", "image/webp")
IMAGES_MAX = 4
IMAGE_B64_MAX = 7_000_000  # about 5 MB of image: the API's limit per image


def clean_images(raw) -> tuple[str | None, list[dict]]:
    """Screenshots pasted into a chat: [{media_type, data (base64)}], checked before they reach claude."""
    out = []
    for im in (raw or [])[:IMAGES_MAX + 1]:
        if not isinstance(im, dict) or im.get("media_type") not in IMAGE_TYPES or not isinstance(im.get("data"), str):
            return "images must be PNG, JPEG, GIF or WebP", []
        if len(im["data"]) > IMAGE_B64_MAX:
            return "an image is over 5 MB: crop or shrink it", []
        out.append({"type": "image", "source": {"type": "base64", "media_type": im["media_type"], "data": im["data"]}})
    if len(out) > IMAGES_MAX:
        return f"at most {IMAGES_MAX} images per message", []
    return None, out


def send(crew, a: dict, text: str, images: list[dict] | None = None) -> bool:
    """Queue a user message for the agent's process. The write happens on the process's own writer thread, so
    this never blocks. False when the process is gone, its pipe broke, or it has not read for procs.STDIN_LIMIT seconds."""
    w = crew.writers.get(a["name"])
    if w and w.stuck():
        w.error = w.error or f"claude has not read its input for {int(procs.STDIN_LIMIT)} s"
    if not w or w.error or not crew._alive(a["name"]):
        why = w.error if w and w.error else "the process is not running"
        crew._kill_proc(a["name"])
        a["error"] = f"could not send to claude: {why}"
        crew.log(a, "error", a["error"])
        return False
    content = [{"type": "text", "text": text}, *images] if images else text
    w.put(json.dumps({"type": "user", "message": {"role": "user", "content": content}}) + "\n")
    return True


def check_writers(crew) -> bool:
    """A process that stopped reading its stdin is hung: end it (session kept) instead of waiting forever."""
    hung = False
    for name, w in list(crew.writers.items()):
        if w.stuck() and (a := crew._agent(name)):
            crew._fail(a, f"claude has not read its input for {int(procs.STDIN_LIMIT)} s and was ended; its session is kept")
            hung = True
    return hung


def fail(crew, a: dict, msg: str) -> None:
    """End an agent's process after a failure: error state, its task back in the backlog."""
    crew._kill_proc(a["name"])
    a["status"], a["error"], a["activity"] = "error", msg[:400], ""
    crew.log(a, "error", a["error"])
    if (t := crew._task(a["task"]) if a["task"] else None) and t["col"] == "doing":
        t["col"], t["who"] = "backlog", t.get("pinned")
    a["task"] = None
    crew.event("error", f"{a['name']}: {msg[:120]}", a["name"])


def pid_flush(crew) -> None:
    """Mirror the live agent pids to ~/.crew/pids/<project key>.json (the file goes away when none are left).
    Caller holds pid_lock."""
    try:
        if crew.pids:
            crew.pid_file.parent.mkdir(parents=True, exist_ok=True)
            write_atomic(crew.pid_file, json.dumps({"owner": os.getpid(), "project": str(crew.project), "t": now(),
                                                    "procs": [{"pid": k, "agent": v} for k, v in crew.pids.items()]}, indent=1))
        else:
            crew.pid_file.unlink(missing_ok=True)
    except OSError as e:
        print(f"crew: could not update {crew.pid_file}: {e}", file=sys.stderr)


def pid_add(crew, pid: int, name: str) -> None:
    with crew.pid_lock:
        crew.pids[pid] = name
        crew._pid_flush()


def pid_drop(crew, pid: int) -> None:
    with crew.pid_lock:
        if crew.pids.pop(pid, None) is not None:
            crew._pid_flush()


def reap_orphans(crew) -> int:
    """At startup: end agent processes an earlier run left running (crash, kill -9, closed console). Every
    recorded pid is matched against its live command line first, so a reused pid is never touched."""
    try:
        data = json.loads(crew.pid_file.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return 0
    if not isinstance(data, dict):
        return 0
    owner = data.get("owner")
    if isinstance(owner, int) and owner != os.getpid() and CREW_SERVER_RE.search(procs.proc_cmdlines([owner]).get(owner, "")):
        return 0  # another Crew is serving this project right now: those processes are not orphans
    entries = [e for e in data.get("procs") or [] if isinstance(e, dict) and e.get("pid") not in crew.pids]
    todo = orphans_to_kill(entries, procs.proc_cmdlines([e["pid"] for e in entries if isinstance(e.get("pid"), int)]), crew.key)
    failed = [(pid, err) for pid in todo if (err := procs.terminate_tree(pid))]
    with crew.pid_lock:
        crew._pid_flush()  # rewrites the file for this run's own processes, or removes it
    n = len(todo) - len(failed)
    if n:
        print(f"crew: reaped {n} orphaned agent process{'es' if n != 1 else ''}", file=sys.stderr)
        crew.event("kill", f"reaped {n} orphaned agent process{'es' if n != 1 else ''}")
    for pid, err in failed:
        crew.event("error", f"could not stop orphaned agent process {pid}: {err}")
    return n


def kill_proc(crew, name: str, wait: bool = False) -> None:
    """End an agent's process tree. Windows: taskkill /T /F (failures are logged). POSIX: SIGTERM to the group,
    then SIGKILL after 3 s, on a helper thread so the caller (usually holding crew.lock) doesn't wait; wait=True joins it."""
    a = crew._agent(name)
    if a:
        a["subs"] = {}  # the tree kill ends its subagents too
        a["bg"] = {}    # and its background jobs
        crew._addressed(a)  # a stopped /address-review turn: its task goes back to Review or Done
    if w := crew.writers.pop(name, None):
        w.close()
    p = crew.procs.pop(name, None)
    if not p or p.poll() is not None:
        return
    if IS_WIN:
        end_tree(crew, p, name)
    else:
        th = threading.Thread(target=end_tree, args=(crew, p, name), daemon=True)
        th.start()
        if wait:
            th.join(timeout=12)


def end_tree(crew, p: subprocess.Popen, name: str) -> None:
    err = procs.terminate_tree(p.pid, p)
    if err:
        try:
            p.kill()
        except OSError:
            pass
        msg = f"could not stop {name}'s process tree (pid {p.pid}): {err}"
        with crew.lock:
            if a := crew._agent(name):
                crew.log(a, "error", msg)
            crew.event("error", msg, name if name in crew.agents else None)
    else:
        try:
            p.wait(timeout=5)
        except subprocess.TimeoutExpired:
            pass
        if p.poll() is not None:
            crew._pid_drop(p.pid)
            try:
                p.stdin.close()
            except (OSError, ValueError):
                pass


def launch_key(crew, a: dict) -> tuple:
    # MCP servers found later are allowed from the agent's next start; no restart (that would drop the live context).
    # Rules added from the permission inbox do restart it (the session is kept).
    return (crew.config["permission_mode"], a["model"], a["effort"], tuple(crew.config.get("allowed_tools") or []), tuple(crew._once_rules(a)))


def once_rules(crew, a: dict) -> list[str]:
    """Allow-once rules: they live on the agent's current task and vanish when it ends."""
    t = crew._task(a["task"]) if a.get("task") else None
    return list((t or {}).get("allow_once") or [])


def task_session(crew, a: dict, t: dict) -> None:
    """Pick the Claude session for work on task t: the one this agent already had for t, so a reply, send-back or
    re-run continues that conversation; a new one for a task it hasn't worked on (a new task from you, the boss or a
    trigger). "keep" agents stay in their one running session, except for workflow steps: every step starts on a
    fresh session, so no run inherits another's context, and the agent goes back to its own session afterwards."""
    step = bool(t.get("run"))
    if a["context"] != "fresh" and not step:
        if "kept" in a:  # back from a workflow step: its own long-running session again
            kept = a.pop("kept")
            if kept != a["session"]:
                crew._kill_proc(a["name"])
                a["session"] = kept
                crew.log(a, "sys", "back in its own session")
        a["last_task"] = t["id"]
        return
    if a["context"] != "fresh" and "kept" not in a:
        a["kept"] = a["session"]
    sid = (t.get("sessions") or {}).get(a["name"])
    if sid:
        if sid != a["session"]:
            crew._kill_proc(a["name"])
            a["session"] = sid
            crew.log(a, "sys", f"back in #{t['id']}'s session")
    elif a["session"] or crew._alive(a["name"]):
        # a task with no session of its own starts a new one: never another task's, a step's or a chat's context (and
        # no paying to re-read old work). Not keyed on last_task, which a restart forgets while the session survives.
        crew._kill_proc(a["name"])
        a["session"] = None
        crew.log(a, "sys", f"fresh context for #{t['id']}")
    a["last_task"] = t["id"]


def forget_sessions(crew, name: str) -> None:
    """The agent's sessions are gone (cleared, fresh respawn, renamed): its tasks start fresh next time."""
    for tk in crew.tasks:
        (tk.get("sessions") or {}).pop(name, None)


def stop_agent(crew, a: dict, why: str = "stopped by you") -> None:
    """End the current run now. The session is kept; the task waits in the backlog until you move it."""
    crew._kill_proc(a["name"])
    if t := crew._task(a["task"]):
        if t["col"] == "doing":
            t.update(col="backlog", who=t.get("pinned") or a["name"], hold=True)
    a.update(status="paused" if a.get("kind") != "boss" else "idle", task=None, activity="", since=None, compacting=None)
    crew.log(a, "sys", why + (" · paused until you resume it or give it work" if a.get("kind") != "boss" else ""))


def kill(crew, a: dict, p: dict) -> dict | None:
    crew._kill_proc(a["name"])
    if t := crew._task(a["task"]):
        t["col"], t["who"] = "backlog", t.get("pinned")
    a.update(status="killed", task=None, activity="", since=None)
    crew.log(a, "error", "killed by you")
    crew.event("kill", f"{a['name']} killed", a["name"])


def release(crew, a: dict, p: dict) -> dict | None:
    # Close an idle agent's Claude process (session kept) so you can `claude --resume` it in your own terminal
    if a["status"] == "running":
        return {"error": f"{a['name']} is working: stop it first"}
    crew._kill_proc(a["name"])
    crew.log(a, "sys", "process closed so the session can continue in your terminal; the next task resumes it here")


def clear_chat(crew, a: dict, p: dict) -> dict | None:
    if a["status"] == "running":
        return {"error": f"{a['name']} is working: stop it first"}
    crew._kill_proc(a["name"])
    crew._forget_sessions(a["name"])
    a.update(session=None, log=[], ctx=0, activity="", error=None,
             status="idle" if a["status"] in ("idle", "paused", "review", "error") else a["status"])
    crew.log(a, "sys", "chat cleared · the next message starts a fresh session")


def respawn(crew, a: dict, p: dict) -> dict | None:
    crew._kill_proc(a["name"])
    if p.get("fresh"):
        a["session"] = None
        crew._forget_sessions(a["name"])
    a.update(status="idle", error=None, activity="")
    crew.log(a, "sys", "respawned" + (" with a fresh session" if p.get("fresh") else ", session kept"))
    crew.event("respawn", f"{a['name']} respawned", a["name"])
