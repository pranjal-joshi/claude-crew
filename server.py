"""Crew: a local web console for a team of Claude Code agents.

Each agent is a real `claude -p` process speaking stream-json, running in its own
git worktree of the target project. Standard library only.

    python server.py --project /path/to/repo     # then open http://127.0.0.1:8770
    python server.py --demo                      # simulated agents, no Claude CLI or tokens
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import os
import shlex
import shutil
import signal
import subprocess
import sys
import threading
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent
WORK_ROOT = Path.home() / ".crew"  # short path: Windows git chokes on long worktree paths
IS_WIN = os.name == "nt"

DEFAULT_CONFIG = {
    "permission_mode": "acceptEdits",
    "allowed_tools": ["Bash(git status:*)", "Bash(git diff:*)", "Bash(ls:*)", "Bash(npm test:*)", "Bash(pytest:*)", "Bash(python -m pytest:*)", "WebSearch", "WebFetch"],
    "agents": [
        {"name": "builder", "role": "Implements features and fixes in application code.", "tags": ["api", "ui", "feature", "bug"], "model": None, "effort": None},
        {"name": "tester", "role": "Writes and runs tests; reports failures precisely.", "tags": ["test"], "model": None, "effort": None},
        {"name": "reviewer", "role": "Reviews code for bugs, security and clarity. Edits only to fix clear defects.", "tags": ["review"], "model": None, "effort": None},
        {"name": "researcher", "role": "Investigates questions, compares options, writes findings to notes/*.md.", "tags": ["research"], "model": None, "effort": None},
        {"name": "docs", "role": "Writes and updates documentation and READMEs.", "tags": ["docs"], "model": None, "effort": None},
    ],
}

TAGS = ["feature", "bug", "api", "ui", "test", "review", "research", "docs"]
# acceptEdits: edits auto-approved, other tools only via allowed_tools.
# auto: Claude Code's classifier approves safe actions. bypassPermissions: nothing is checked.
MODES = ["acceptEdits", "auto", "bypassPermissions"]
MODELS = ["fable", "opus", "sonnet", "haiku"]          # aliases the CLI resolves to the latest of each family
EFFORTS = ["low", "medium", "high", "xhigh", "max"]
NAME_RE = re.compile(r"[a-z0-9][a-z0-9-]{0,23}")


def clean_tags(tags) -> list[str]:
    if isinstance(tags, str):
        tags = re.split(r"[,\s]+", tags)
    out = []
    for t in tags or []:
        t = re.sub(r"[^a-z0-9-]", "", str(t).lower())[:20]
        if t and t not in out:
            out.append(t)
    return out[:8]


def now() -> float:
    return time.time()


def git(cwd: Path, *args: str, check: bool = False) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-c", "core.longpaths=true", *args], cwd=cwd, capture_output=True,
                          text=True, encoding="utf-8", errors="replace", check=check)


def is_git(path: Path) -> bool:
    return git(path, "rev-parse", "--is-inside-work-tree").stdout.strip() == "true"


class Crew:
    def __init__(self, project: Path, config: dict, claude: list[str]):
        self.project = project
        self.config = config
        self.config_path: Path | None = None
        self.claude = claude
        self.lock = threading.RLock()
        self.changed = threading.Condition(self.lock)
        self.version = 0
        self.tasks: list[dict] = []
        self.feed: list[dict] = []
        self.next_id = 1
        self.git = is_git(project)
        self.key = f"{project.name[:24]}-{hashlib.sha1(str(project).lower().encode()).hexdigest()[:6]}"
        self.state_file = WORK_ROOT / "state" / f"{self.key}.json"
        self.state_file.parent.mkdir(parents=True, exist_ok=True)
        self.procs: dict[str, subprocess.Popen] = {}
        self.mcp_allow: set[str] = set()   # MCP server prefixes seen in agents' tool lists
        self.agents: dict[str, dict] = {}
        for spec in config["agents"]:
            self.agents[spec["name"]] = self._new_agent(spec)
        self._load()
        for a in self.agents.values():  # re-attach worktrees made in earlier runs
            self._attach_worktree(a)

    def _new_agent(self, spec: dict) -> dict:
        return {
            "name": spec["name"], "role": spec.get("role", ""), "tags": list(spec.get("tags", [])),
            "model": spec.get("model") or None, "effort": spec.get("effort") or None,
            "status": "idle", "task": None, "session": None,
            "cost": 0.0, "proc_cost": 0.0, "log": [], "activity": "", "since": None,
            "cwd": str(self.project), "branch": None, "error": None, "launched": None,
            "tokens": {"in": 0, "out": 0, "cache": 0}, "last_msg": None, "mcp": [], "skills": 0,
            "subs": {},  # live subagents: tool_use id -> {label, type, since}
        }

    def _attach_worktree(self, a: dict) -> None:
        wt = WORK_ROOT / "wt" / self.key / a["name"]
        if self.git and (wt / ".git").exists():
            a["cwd"], a["branch"] = str(wt), f"crew/{a['name']}"

    def _save_config(self) -> None:
        """Write the agent roster and mode back to crew.json, keeping any other keys."""
        self.config["agents"] = [{"name": a["name"], "role": a["role"], "tags": a["tags"],
                                  "model": a["model"], "effort": a["effort"]} for a in self.agents.values()]
        if not self.config_path:
            return
        saved = json.loads(self.config_path.read_text(encoding="utf-8")) if self.config_path.exists() else {}
        saved["agents"] = self.config["agents"]
        saved["permission_mode"] = self.config["permission_mode"]
        self.config_path.write_text(json.dumps(saved, indent=2), encoding="utf-8")

    def _launch_key(self, a: dict) -> tuple:
        mcp = tuple(sorted(self.mcp_allow)) if self.config.get("allow_mcp", True) else ()
        return (self.config["permission_mode"], a["model"], a["effort"], mcp)

    # ---- persistence & change notification ---------------------------------
    def _load(self) -> None:
        if not self.state_file.exists():
            return
        try:
            data = json.loads(self.state_file.read_text(encoding="utf-8"))
        except Exception:
            return
        if data.get("project") != str(self.project):
            return
        self.mcp_allow = set(data.get("mcp_allow", []))
        self.tasks = data.get("tasks", [])
        self.next_id = data.get("next_id", len(self.tasks) + 1)
        for t in self.tasks:  # nothing is running after a restart
            if t["col"] == "doing":
                t["col"] = "backlog"
        for name, saved in data.get("agents", {}).items():
            if name in self.agents:
                self.agents[name].update({k: saved[k] for k in ("session", "cost", "log", "tokens") if k in saved})
                if self.agents[name]["status"] != "idle":
                    self.agents[name]["status"] = "idle"

    def _save(self) -> None:
        data = {"project": str(self.project), "tasks": self.tasks, "next_id": self.next_id,
                "mcp_allow": sorted(self.mcp_allow),
                "agents": {n: {"session": a["session"], "cost": a["cost"], "tokens": a["tokens"], "log": a["log"][-200:]} for n, a in self.agents.items()}}
        tmp = self.state_file.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=1), encoding="utf-8")
        tmp.replace(self.state_file)

    def touch(self) -> None:
        with self.lock:
            self.version += 1
            self._save()
            self.changed.notify_all()

    def event(self, kind: str, text: str, agent: str | None = None) -> None:
        with self.lock:
            self.feed.append({"t": now(), "kind": kind, "text": text, "agent": agent})
            self.feed = self.feed[-60:]

    def log(self, a: dict, kind: str, text: str, **extra) -> None:
        a["log"].append({"t": now(), "kind": kind, "text": text, **extra})
        a["log"] = a["log"][-400:]

    def _all_tags(self) -> list[str]:
        tags = list(TAGS)
        for a in self.agents.values():
            tags += [t for t in a["tags"] if t not in tags]
        return tags

    def snapshot(self) -> dict:
        with self.lock:
            agents = []
            for a in self.agents.values():
                agents.append({k: v for k, v in a.items() if k != "log"} | {"log": a["log"][-120:], "alive": self._alive(a["name"])})
            return {"version": self.version, "project": str(self.project), "git": self.git,
                    "agents": agents, "tasks": self.tasks, "feed": self.feed[-30:],
                    "permission_mode": self.config["permission_mode"], "modes": MODES,
                    "models": MODELS, "efforts": EFFORTS, "tags": self._all_tags(),
                    "project_name": self.project.name, "mcp_allow": sorted(self.mcp_allow),
                    "allow_mcp": self.config.get("allow_mcp", True)}

    # ---- worktrees -----------------------------------------------------------
    def _ensure_worktree(self, a: dict) -> None:
        if not self.git or a["branch"]:
            return
        branch = f"crew/{a['name']}"
        path = WORK_ROOT / "wt" / self.key / a["name"]
        if not (path / ".git").exists():
            path.parent.mkdir(parents=True, exist_ok=True)
            r = git(self.project, "worktree", "add", "-B", branch, str(path), "HEAD")
            if r.returncode != 0:
                self.log(a, "error", "worktree failed: " + r.stderr.strip())
                return
        a["cwd"], a["branch"] = str(path), branch

    def diff(self, a: dict) -> dict:
        if not self.git:
            return {"stat": "", "patch": "", "note": "Project is not a git repo, so no diff is available."}
        cwd = Path(a["cwd"])
        git(cwd, "add", "-A")
        stat = git(cwd, "diff", "--cached", "--stat").stdout
        patch = git(cwd, "diff", "--cached").stdout
        if len(patch) > 120_000:
            patch = patch[:120_000] + "\n… diff truncated …"
        return {"stat": stat, "patch": patch, "note": ""}

    # ---- processes -----------------------------------------------------------
    def _alive(self, name: str) -> bool:
        p = self.procs.get(name)
        return bool(p and p.poll() is None)

    def _start(self, a: dict) -> bool:
        if self._alive(a["name"]):
            return True
        self._ensure_worktree(a)
        role = (f"You are '{a['name']}', one of {len(self.agents)} Claude Code agents working in parallel on this project. "
                f"Your role: {a['role']} "
                f"Your working folder is {a['cwd']}" + (f", a git worktree of the project {self.project}" if a['branch'] else "") + ". "
                "Stay inside it: do not read, create or change files outside this folder unless the task explicitly names a path outside it. "
                "Never touch other agents' worktrees. "
                "When you finish a task, end with a short summary: what you changed, files touched, and anything left open.")
        mode = self.config["permission_mode"]
        cmd = [*self.claude, "-p", "--input-format", "stream-json", "--output-format", "stream-json", "--verbose",
               "--permission-mode", mode, "--append-system-prompt", role]
        if mode == "bypassPermissions":
            cmd += ["--dangerously-skip-permissions"]
        else:
            allowed = list(self.config.get("allowed_tools") or [])
            if self.config.get("allow_mcp", True):
                allowed += sorted(self.mcp_allow)  # e.g. mcp__azure-devops: every tool on that server
            if allowed:
                cmd += ["--allowedTools", *allowed]
        if a["effort"]:
            cmd += ["--effort", a["effort"]]
        a["launched"] = self._launch_key(a)
        if a["model"]:
            cmd += ["--model", a["model"]]
        if a["session"]:
            cmd += ["--resume", a["session"]]
        flags = subprocess.CREATE_NEW_PROCESS_GROUP if IS_WIN else 0
        try:
            p = subprocess.Popen(cmd, cwd=a["cwd"], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                 text=True, encoding="utf-8", errors="replace", bufsize=1, creationflags=flags,
                                 start_new_session=not IS_WIN)
        except OSError as e:
            a["status"], a["error"] = "error", f"could not start claude: {e}"
            self.log(a, "error", a["error"])
            return False
        self.procs[a["name"]] = p
        a["proc_cost"] = 0.0
        a["error"] = None
        threading.Thread(target=self._read, args=(a, p), daemon=True).start()
        return True

    def _send(self, a: dict, text: str) -> None:
        p = self.procs[a["name"]]
        msg = {"type": "user", "message": {"role": "user", "content": text}}
        p.stdin.write(json.dumps(msg) + "\n")
        p.stdin.flush()

    def _kill_proc(self, name: str) -> None:
        if name in self.agents:
            self.agents[name]["subs"] = {}  # the tree kill ends its subagents too
        p = self.procs.pop(name, None)
        if not p or p.poll() is not None:
            return
        try:
            if IS_WIN:
                subprocess.run(["taskkill", "/T", "/F", "/PID", str(p.pid)], capture_output=True)
            else:
                os.killpg(p.pid, signal.SIGKILL)
        except Exception:
            p.kill()

    def _read(self, a: dict, p: subprocess.Popen) -> None:
        for line in p.stdout:
            line = line.strip()
            if not line:
                continue
            try:
                d = json.loads(line)
            except json.JSONDecodeError:
                with self.lock:
                    self.log(a, "sys", line[:500])
                self.touch()
                continue
            with self.lock:
                if self.procs.get(a["name"]) is not p:
                    return  # killed and replaced
                self._on_event(a, d)
            self.touch()
        with self.lock:
            if self.procs.get(a["name"]) is p:
                self.procs.pop(a["name"], None)
                if a["status"] in ("running",):
                    a["status"], a["error"] = "error", "claude exited unexpectedly"
                    self.log(a, "error", a["error"])
                    self.event("error", f"{a['name']} exited unexpectedly", a["name"])
        self.touch()

    def _on_event(self, a: dict, d: dict) -> None:
        t, st = d.get("type"), d.get("subtype")
        if d.get("session_id"):
            a["session"] = d["session_id"]
        if t == "system" and st == "init":
            a["mcp"] = [{"name": m.get("name"), "status": m.get("status")} for m in d.get("mcp_servers") or []]
            a["skills"] = len(d.get("skills") or [])
            found = {"__".join(tool.split("__")[:2]) for tool in d.get("tools") or [] if tool.startswith("mcp__")}
            if found - self.mcp_allow:
                self.mcp_allow |= found  # other agents pick these up when they next (re)start
            ok = sum(1 for m in a["mcp"] if m["status"] == "connected")
            self.log(a, "sys", f"session ready · {d.get('model', '')} · {d.get('permissionMode', '')} · {a['skills']} skills · {ok} MCP servers")
        elif t == "assistant":
            m = d.get("message", {})
            sub = self._sub_label(a, d)
            u = m.get("usage") or {}
            if u and m.get("id") != a["last_msg"]:  # one API response can arrive as several events
                a["last_msg"] = m.get("id")
                tk = a["tokens"]
                tk["in"] += int(u.get("input_tokens") or 0) + int(u.get("cache_creation_input_tokens") or 0)
                tk["cache"] += int(u.get("cache_read_input_tokens") or 0)
                tk["out"] += int(u.get("output_tokens") or 0)
            for b in m.get("content", []):
                if b.get("type") == "text" and b.get("text", "").strip():
                    self.log(a, "text", b["text"], sub=sub)
                    a["activity"] = (f"↳ {sub}: " if sub else "") + b["text"].strip().splitlines()[0][:120]
                elif b.get("type") == "tool_use":
                    inp = b.get("input") or {}
                    hint = inp.get("file_path") or inp.get("command") or inp.get("pattern") or inp.get("url") or inp.get("description") or ""
                    hint = str(hint).replace(str(a["cwd"]), ".")[:160]
                    if b.get("name") in ("Agent", "Task"):  # a subagent starts; its tool_result ends it
                        label = str(inp.get("description") or inp.get("subagent_type") or "subagent")[:80]
                        a["subs"][b.get("id") or label] = {"label": label, "type": inp.get("subagent_type") or "", "since": now()}
                        task = self._task(a["task"]) if a["task"] else None
                        if task:
                            task["subagents"] = task.get("subagents", 0) + 1
                        self.log(a, "subagent", label, detail=inp.get("subagent_type") or "", sub=sub)
                    else:
                        self.log(a, "tool", b.get("name", "tool"), detail=hint, sub=sub)
                    a["activity"] = (f"↳ {sub}: " if sub else "") + f"{b.get('name')} {hint}".strip()[:120]
        elif t == "user":
            for b in (d.get("message", {}).get("content") or []):
                if isinstance(b, dict) and b.get("type") == "tool_result" and b.get("tool_use_id") in a["subs"]:
                    done = a["subs"].pop(b["tool_use_id"])
                    self.log(a, "subdone", done["label"], detail=f"{int(now() - done['since'])}s")
                if isinstance(b, dict) and b.get("type") == "tool_result" and b.get("is_error"):
                    c = b.get("content")
                    txt = c if isinstance(c, str) else json.dumps(c)[:300]
                    self.log(a, "toolerr", txt[:300])
        elif t == "result":
            a["subs"] = {}
            total = float(d.get("total_cost_usd") or 0)
            a["cost"] += max(0.0, total - a["proc_cost"])
            a["proc_cost"] = total
            task = self._task(a["task"]) if a["task"] else None
            if d.get("is_error") or st != "success":
                msg = d.get("result") or st or "error"
                a["status"], a["error"] = "error", str(msg)[:400]
                if not (a["log"] and a["log"][-1]["kind"] == "text" and a["log"][-1]["text"].strip() == a["error"].strip()):
                    self.log(a, "error", a["error"])
                else:
                    a["log"][-1]["kind"] = "error"
                self.event("error", f"{a['name']}: {a['error'][:120]}", a["name"])
                if task:
                    task["col"], task["who"] = "backlog", task.get("pinned")
                    a["task"] = None
                return
            if task:
                tk = a["tokens"]
                task["tokens"] = tk["in"] + tk["cache"] + tk["out"] - task.get("tok0", 0)
                task["summary"] = d.get("result", "")
                task["col"] = "review"
                task["diff"] = self.diff(a)
                task["finished"] = now()
                a["status"] = "review"
                self.event("review", f"{a['name']} finished #{task['id']} · {task['title']}", a["name"])
            else:
                a["status"] = "idle"
            a["activity"] = ""
            self.log(a, "done", f"turn finished · ${total:.3f} this session")
            if a.get("launched") and tuple(a["launched"]) != self._launch_key(a):
                self._kill_proc(a["name"])  # session is kept; next start uses the new settings
                self.log(a, "sys", "settings changed · restarts on next task")

    @staticmethod
    def _sub_label(a: dict, d: dict) -> str | None:
        """Events from inside a subagent carry the id of the Agent call that started it."""
        par = d.get("parent_tool_use_id")
        return (a["subs"].get(par) or {}).get("label", "subagent") if par else None

    # ---- tasks -----------------------------------------------------------------
    def _task(self, tid) -> dict | None:
        return next((t for t in self.tasks if t["id"] == tid), None)

    def _begin(self, a: dict, t: dict) -> None:
        if not self._start(a):
            return
        tk = a["tokens"]
        t.update(col="doing", who=a["name"], started=now(), tok0=tk["in"] + tk["cache"] + tk["out"] - t.get("tokens", 0))
        a.update(status="running", task=t["id"], since=now(), activity="starting…")
        body = f"Task #{t['id']}: {t['title']}"
        if t.get("details"):
            body += "\n\n" + t["details"]
        self.log(a, "you", body)
        self._send(a, body)
        self.event("start", f"{a['name']} picked #{t['id']} · {t['title']}", a["name"])

    def dispatch(self) -> None:
        with self.lock:
            moved = False
            for a in self.agents.values():
                if a["status"] != "idle":
                    continue
                t = next((t for t in self.tasks if t["col"] == "backlog" and
                          (t.get("who") == a["name"] or (not t.get("who") and t["tag"] in a["tags"]))), None)
                if t:
                    self._begin(a, t)
                    moved = True
        if moved:
            self.touch()

    def _release(self, t: dict) -> None:
        """Detach a task from whoever holds it, from any column. A running owner is stopped (session kept)."""
        owner = self.agents.get(t.get("who") or "")
        if owner and owner["task"] == t["id"]:
            if t["col"] == "doing":
                self._kill_proc(owner["name"])
                self.log(owner, "sys", f"stopped: #{t['id']} was moved")
            owner.update(status="idle", task=None, activity="", since=None)
        if t.get("summary"):  # hand the previous attempt to whoever picks it up next
            prev = owner["name"] if owner else t.get("who")
            where = f"branch {owner['branch']}" if owner and owner.get("branch") else "the shared folder"
            if t.get("commit"):
                where += f", commit {t['commit']}"
            t.setdefault("history", []).append({"who": prev, "summary": t["summary"], "commit": t.get("commit"), "t": now()})
            t["details"] = (t.get("details") or "") + (
                f"\n\n---\nEarlier work on this task by {prev} ({where}):\n{t['summary'][:1500]}")
            t["summary"] = ""
        t.pop("diff", None)

    # ---- actions from the UI ---------------------------------------------------
    def act(self, action: str, p: dict) -> dict:
        with self.lock:
            a = self.agents.get(p.get("agent", ""))
            t = self._task(p.get("task"))
            if action == "add":
                title = (p.get("title") or "").strip()
                if not title:
                    return {"error": "title required"}
                who = p.get("agent") or None
                t = {"id": self.next_id, "title": title, "details": (p.get("details") or "").strip(),
                     "tag": p.get("tag") if p.get("tag") in self._all_tags() else "feature", "col": "backlog",
                     "who": who, "pinned": who, "created": now()}
                self.next_id += 1
                self.tasks.append(t)
                self.event("add", f"#{t['id']} {title}" + (f" → {who}" if who else ""))
            elif action == "assign" and t:
                # Any column: the task becomes a to-do for `agent` (or anyone when agent is empty).
                if a and a["status"] in ("killed", "error"):
                    return {"error": f"{a['name']} is {a['status']}; respawn it first"}
                if t["col"] == "doing" and a and t.get("who") == a["name"]:
                    return {"error": f"{a['name']} is already on it"}
                was = t["col"]
                if was != "backlog":
                    self._release(t)
                t["col"] = "backlog"
                t["who"] = t["pinned"] = a["name"] if a else None
                t.pop("commit", None)
                if a and a["status"] == "idle":
                    self._begin(a, t)
                else:
                    self.event("queue", f"#{t['id']} → {a['name'] if a else 'anyone'}" + (f" (from {was})" if was != "backlog" else ""),
                               a["name"] if a else None)
            elif action == "move" and t and p.get("col") in ("backlog", "done"):
                if t["col"] != p["col"]:
                    if p["col"] == "backlog":
                        self._release(t)
                        t["who"] = t["pinned"] = None
                        t.pop("commit", None)
                    else:
                        owner = self.agents.get(t.get("who") or "")
                        if owner and owner["task"] == t["id"]:
                            if t["col"] == "doing":
                                self._kill_proc(owner["name"])
                            owner.update(status="idle", task=None, activity="", since=None)
                    t["col"] = p["col"]
            elif action == "mode" and p.get("mode") in MODES:
                self.config["permission_mode"] = p["mode"]
                self._save_config()
                for ag in self.agents.values():  # idle processes restart in the new mode; busy ones after their turn
                    if ag["status"] != "running" and self._alive(ag["name"]):
                        self._kill_proc(ag["name"])
                self.event("mode", f"permission mode → {p['mode']}")
            elif action == "configure" and a:
                if "model" in p:
                    if p["model"] and p["model"] not in MODELS:
                        return {"error": f"unknown model {p['model']}"}
                    a["model"] = p["model"] or None
                if "effort" in p:
                    if p["effort"] and p["effort"] not in EFFORTS:
                        return {"error": f"unknown effort {p['effort']}"}
                    a["effort"] = p["effort"] or None
                if "role" in p:
                    a["role"] = str(p["role"]).strip()[:500]
                if "tags" in p:
                    a["tags"] = clean_tags(p["tags"])
                self._save_config()
                if a["status"] != "running" and self._alive(a["name"]) and tuple(a.get("launched") or ()) != self._launch_key(a):
                    self._kill_proc(a["name"])  # restarts with the new settings on its next task (session kept)
                self.event("config", f"{a['name']} · {a['model'] or 'default'} · {a['effort'] or 'default'} effort", a["name"])
            elif action == "add_agent":
                name = (p.get("name") or "").strip().lower()
                if not NAME_RE.fullmatch(name):
                    return {"error": "name: 1-24 lowercase letters, digits or dashes"}
                if name in self.agents:
                    return {"error": f"{name} already exists"}
                if p.get("model") and p["model"] not in MODELS or p.get("effort") and p["effort"] not in EFFORTS:
                    return {"error": "unknown model or effort"}
                self.agents[name] = self._new_agent({"name": name, "role": (p.get("role") or "").strip()[:500],
                                                     "tags": clean_tags(p.get("tags") or []),
                                                     "model": p.get("model"), "effort": p.get("effort")})
                self._attach_worktree(self.agents[name])
                self._save_config()
                self.event("agent", f"+ {name} joined the crew", name)
            elif action == "remove_agent" and a:
                if len(self.agents) <= 1:
                    return {"error": "keep at least one agent"}
                self._kill_proc(a["name"])
                for tk in self.tasks:
                    if tk.get("who") == a["name"] or tk.get("pinned") == a["name"]:
                        if tk["col"] == "doing":
                            tk["col"] = "backlog"
                        if tk["col"] == "backlog":
                            tk["who"] = tk["pinned"] = None
                del self.agents[a["name"]]
                self._save_config()
                self.event("agent", f"− {a['name']} removed" + (f"; its work stays on {a['branch']}" if a.get("branch") else ""))
            elif action == "delete" and t:
                if t["col"] == "doing":
                    return {"error": "task is running"}
                self.tasks.remove(t)
            elif action == "tell" and a:
                text = (p.get("text") or "").strip()
                if not text:
                    return {"error": "empty message"}
                if a["status"] == "killed":
                    return {"error": "agent is killed"}
                if not self._start(a):
                    return {"error": a["error"]}
                self.log(a, "you", text)
                self._send(a, text)
                if a["status"] in ("idle", "error"):
                    a.update(status="running", since=now(), activity="thinking…", error=None)
                self.event("tell", f"→ {a['name']}: {text[:80]}", a["name"])
            elif action == "kill" and a:
                self._kill_proc(a["name"])
                if t := self._task(a["task"]):
                    t["col"], t["who"] = "backlog", t.get("pinned")
                a.update(status="killed", task=None, activity="", since=None)
                self.log(a, "error", "killed by you")
                self.event("kill", f"{a['name']} killed", a["name"])
            elif action == "respawn" and a:
                self._kill_proc(a["name"])
                if p.get("fresh"):
                    a["session"] = None
                a.update(status="idle", error=None, activity="")
                self.log(a, "sys", "respawned" + (" with a fresh session" if p.get("fresh") else ", session kept"))
                self.event("respawn", f"{a['name']} respawned", a["name"])
            elif action == "approve" and t and t["col"] == "review":
                w = self.agents.get(t["who"])
                if w and self.git:
                    git(Path(w["cwd"]), "add", "-A")
                    r = git(Path(w["cwd"]), "-c", "user.name=crew", "-c", "user.email=crew@localhost",
                            "commit", "-m", f"#{t['id']} {t['title']}\n\n{(t.get('summary') or '')[:2000]}")
                    t["commit"] = git(Path(w["cwd"]), "rev-parse", "--short", "HEAD").stdout.strip() if r.returncode == 0 else None
                    t["merge_hint"] = f"git merge {w['branch']}"
                t["col"] = "done"
                if w:
                    w.update(status="idle", task=None)
                self.event("approve", f"approved #{t['id']}" + (f" · commit {t.get('commit')} on {w['branch']}" if t.get("commit") else ""))
            elif action == "reject" and t and t["col"] == "review":
                w = self.agents.get(t["who"])
                fb = (p.get("text") or "Please revise.").strip()
                if not w or w["status"] == "killed" or not self._start(w):
                    t["col"] = "backlog"
                    return {"error": "agent unavailable; task returned to backlog"}
                t["col"] = "doing"
                t["started"] = now()
                w.update(status="running", task=t["id"], since=now(), activity="revising…")
                msg = f"Review feedback on task #{t['id']}: {fb}\nPlease address it, then summarise again."
                self.log(w, "you", msg)
                self._send(w, msg)
                self.event("reject", f"#{t['id']} sent back to {w['name']}", w["name"])
            elif action == "diff" and t and t.get("who") in self.agents:
                t["diff"] = self.diff(self.agents[t["who"]])
            else:
                return {"error": f"bad action {action}"}
        self.touch()
        return {"ok": True}

    def shutdown(self) -> None:
        for n in list(self.procs):
            self._kill_proc(n)


CREW: Crew
SWITCH = threading.Lock()


def recent_projects() -> list[str]:
    f = WORK_ROOT / "recent.json"
    try:
        return [p for p in json.loads(f.read_text(encoding="utf-8")) if Path(p).is_dir()][:12]
    except Exception:
        return []


def remember_project(path: Path) -> None:
    items = [str(path)] + [p for p in recent_projects() if p.lower() != str(path).lower()]
    WORK_ROOT.mkdir(parents=True, exist_ok=True)
    (WORK_ROOT / "recent.json").write_text(json.dumps(items[:12], indent=1), encoding="utf-8")


def browse(path: str) -> dict:
    base = Path(path).expanduser() if path else Path.home()
    if not base.is_dir():
        return {"error": f"not a folder: {base}"}
    base = base.resolve()
    dirs = []
    try:
        for c in sorted(base.iterdir(), key=lambda x: x.name.lower()):
            if c.is_dir() and not c.name.startswith((".", "$")) and c.name not in ("node_modules", "__pycache__"):
                dirs.append({"name": c.name, "path": str(c), "git": (c / ".git").exists()})
    except PermissionError:
        pass
    parent = str(base.parent) if base.parent != base else None
    drives = [f"{d}:\\" for d in "CDEFGH" if Path(f"{d}:/").exists()] if IS_WIN else []
    return {"path": str(base), "parent": parent, "git": (base / ".git").exists() or is_git(base), "dirs": dirs[:400],
            "drives": drives, "home": str(Path.home()), "recent": recent_projects()}


def switch_project(path: str) -> dict:
    global CREW
    if not path or not path.strip():
        return {"error": "choose a folder"}
    target = Path(path.strip()).expanduser()
    if not target.is_absolute():
        return {"error": r"use a full path, like C:\code\my-repo"}
    if not target.is_dir():
        return {"error": f"not a folder: {target}"}
    target = target.resolve()
    with SWITCH:
        old = CREW
        if target == old.project:
            return {"ok": True}
        old.shutdown()
        new = Crew(target, old.config, old.claude)
        new.config_path = old.config_path
        new.version = old.version + 1
        CREW = new
        remember_project(target)
        with old.lock:  # wake streams parked on the old crew so they move to the new one
            old.version += 1
            old.changed.notify_all()
        new.touch()
    return {"ok": True}


class Handler(BaseHTTPRequestHandler):
    server_version = "crew/1"

    def log_message(self, *args):  # quiet
        pass

    def _host_ok(self) -> bool:
        host = (self.headers.get("Host") or "").split(":")[0]
        return host in ("127.0.0.1", "localhost")

    def _json(self, obj, code=200):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if not self._host_ok():
            return self.send_error(HTTPStatus.FORBIDDEN)
        if self.path in ("/", "/index.html"):
            body = (APP_DIR / "index.html").read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)
        elif self.path == "/api/state":
            self._json(CREW.snapshot())
        elif self.path == "/api/stream":
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            seen = -1
            try:
                crew = None
                while True:
                    c = CREW
                    if c is not crew:  # project switched: force a fresh snapshot
                        crew, seen = c, -1
                    with c.lock:
                        if c.version == seen:
                            c.changed.wait(timeout=15)
                        v = c.version
                    if CREW is not c:
                        continue
                    if v == seen:
                        self.wfile.write(b": ping\n\n")
                    else:
                        seen = v
                        self.wfile.write(b"data: " + json.dumps(c.snapshot()).encode() + b"\n\n")
                    self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                return
        else:
            self.send_error(HTTPStatus.NOT_FOUND)

    def do_POST(self):
        # Custom header forces a CORS preflight, which this server never answers: other sites can't post here.
        if not self._host_ok() or self.headers.get("X-Crew") != "1":
            return self.send_error(HTTPStatus.FORBIDDEN)
        n = int(self.headers.get("Content-Length") or 0)
        try:
            payload = json.loads(self.rfile.read(n) or b"{}")
        except json.JSONDecodeError:
            return self._json({"error": "bad json"}, 400)
        if self.path == "/api/browse":
            res = browse(payload.get("path") or "")
        elif self.path == "/api/project":
            res = switch_project(payload.get("path") or "")
        elif self.path.startswith("/api/act/"):
            res = CREW.act(self.path.rsplit("/", 1)[1], payload)
        else:
            return self.send_error(HTTPStatus.NOT_FOUND)
        self._json(res, 400 if "error" in res else 200)


DEMO_TASKS = [
    ("Add rate limiting to the login endpoint", "api"),
    ("Write tests for the login rate limiter", "test"),
    ("Compare background job queues for email sending", "research"),
    ("Review the auth module for security issues", "review"),
    ("Document the new login limits in the README", "docs"),
    ("Add CSV export to the reports page", "feature"),
    ("Fix flaky date parsing on the invoices page", "bug"),
]


DEMO_MODELS = {"builder": ("opus", "high"), "tester": ("sonnet", "medium"), "reviewer": ("opus", "xhigh"),
               "researcher": ("fable", "high"), "docs": ("haiku", "low")}


def init_repo(project: Path, files: dict[str, str]) -> None:
    project.mkdir(parents=True)
    for rel, body in files.items():
        (project / rel).parent.mkdir(parents=True, exist_ok=True)
        (project / rel).write_text(body, encoding="utf-8")
    git(project, "init", "-q")
    git(project, "add", "-A")
    git(project, "-c", "user.name=crew", "-c", "user.email=crew@localhost", "commit", "-qm", "init")


def main() -> None:
    global CREW
    ap = argparse.ArgumentParser(description="Local web console for a crew of Claude Code agents")
    ap.add_argument("--project", help="repo the agents work on (default: a demo git repo)")
    ap.add_argument("--port", type=int, default=8770)
    ap.add_argument("--config", help="crew config (default: crew.json next to server.py)")
    ap.add_argument("--demo", action="store_true", help="simulated agents with sample tasks: no Claude CLI, no tokens")
    args = ap.parse_args()

    if args.demo:
        project = Path(args.project or WORK_ROOT / "demo-sim").resolve()
        cfg_path = Path(args.config or WORK_ROOT / "demo-crew.json")
        claude = [sys.executable, str(APP_DIR / "demo" / "fake_claude.py")]
    else:
        project = Path(args.project or WORK_ROOT / "demo-project").resolve()
        cfg_path = Path(args.config or APP_DIR / "crew.json")
        # CREW_CLAUDE overrides the binary, e.g. "python demo/fake_claude.py"
        claude = shlex.split(os.environ["CREW_CLAUDE"], posix=False) if os.environ.get("CREW_CLAUDE") else [shutil.which("claude") or ""]
        if not claude[0]:
            sys.exit("claude CLI not found on PATH. Install Claude Code, or try `python server.py --demo`.")

    if not cfg_path.exists():
        cfg = json.loads(json.dumps(DEFAULT_CONFIG))
        if args.demo:  # show off per-agent models in the demo
            for a in cfg["agents"]:
                a["model"], a["effort"] = DEMO_MODELS.get(a["name"], (None, None))
        cfg_path.parent.mkdir(parents=True, exist_ok=True)
        cfg_path.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
    config = DEFAULT_CONFIG | json.loads(cfg_path.read_text(encoding="utf-8"))

    if not project.exists() and project.parent == WORK_ROOT.resolve():
        init_repo(project, {
            "README.md": "# Demo project\n\nA scratch repo for trying Crew.\n",
            "app/__init__.py": "",
            "app/auth.py": "class TooManyAttempts(Exception):\n    def __init__(self, retry_after: int):\n        self.retry_after = retry_after\n\n\n"
                           "def login(user: str, password: str) -> bool:\n    ...\n",
        })
    if not project.is_dir():
        sys.exit(f"project folder not found: {project}")

    CREW = Crew(project, config, claude)
    CREW.config_path = cfg_path
    remember_project(project)
    if args.demo and not CREW.tasks:
        for title, tag in DEMO_TASKS:
            CREW.act("add", {"title": title, "tag": tag})

    def loop():
        while True:
            try:
                CREW.dispatch()
            except Exception as e:  # keep the dispatcher alive
                print("dispatch error:", e, file=sys.stderr)
            time.sleep(1)

    threading.Thread(target=loop, daemon=True).start()
    srv = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    srv.daemon_threads = True
    print(f"crew{' (demo)' if args.demo else ''} · project {project} · http://127.0.0.1:{args.port}", flush=True)
    try:
        srv.serve_forever()
    finally:
        CREW.shutdown()


if __name__ == "__main__":
    main()
