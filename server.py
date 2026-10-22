"""Crew: a local web console for a team of Claude Code agents.

Each agent is a real `claude -p` process speaking stream-json, running in its own
git worktree of the target project. Standard library only.

    python server.py --project /path/to/repo     # then open http://127.0.0.1:8770
    python server.py --demo                      # simulated agents, no Claude CLI or tokens
"""
from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import re
import os
import secrets
import shlex
import socket
import shutil
import subprocess
import sys
import threading
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, urlsplit

try:  # installed as claude_crew; from a clone the folders sit next to this file
    from . import agents, triggers, workflows
    from .triggers import azure
except ImportError:
    import agents
    import triggers
    import workflows
    from triggers import azure

__version__ = "0.2.0"

APP_DIR = Path(__file__).resolve().parent
# A clone has pyproject.toml next to server.py; an installed wheel does not. When installed, the
# default crew.json lives in the current directory (never inside site-packages).
IS_CLONE = (APP_DIR / "pyproject.toml").exists()
WORK_ROOT = Path.home() / ".crew"  # short path: Windows git chokes on long worktree paths
IS_WIN = os.name == "nt"

# One agent per stage of the delivery loop (agents/presets/): plan -> research -> design/build -> test -> review
# -> evaluate -> ship/observe -> report. Tags route unassigned tasks; each tag belongs to one agent.
DEFAULT_CONFIG = {
    "permission_mode": "acceptEdits",
    "context": "fresh",
    "allowed_tools": ["Bash(git status:*)", "Bash(git diff:*)", "Bash(git log:*)", "Bash(git show:*)", "Bash(git merge:*)",
                      "Bash(ls:*)", "Bash(npm test:*)", "Bash(pytest:*)", "Bash(python -m pytest:*)", "WebSearch", "WebFetch"],
    "agents": agents.presets.load(),
}

DEFAULT_WORKFLOWS = workflows.templates.load()  # workflows/templates/

TAGS = ["plan", "research", "ui", "feature", "bug", "api", "test", "review", "eval", "ops", "incident", "docs", "report", "chore"]
# acceptEdits: edits auto-approved, other tools only via allowed_tools.
# auto: Claude Code's classifier approves safe actions. bypassPermissions: nothing is checked.
MODES = ["acceptEdits", "auto", "bypassPermissions"]
MODELS, EFFORTS, CONTEXTS = agents.MODELS, agents.EFFORTS, agents.CONTEXTS
git, is_git, write_atomic, command_catalog = agents.util.git, agents.util.is_git, agents.util.write_atomic, agents.files.command_catalog
fill = workflows.fill  # shared with the trigger templates
SEVERITIES = ("critical", "warning", "info")  # most severe first


def now() -> float:
    return time.time()


def load_json_file(path: Path) -> dict:
    """A JSON object from path. Raises ValueError when it is unreadable, invalid or not an object."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        raise ValueError(str(e)) from e
    if not isinstance(data, dict):
        raise ValueError("not a JSON object")
    return data


# ---- triggers: cron, templates ---------------------------------------------------
TRIGGER_KINDS = triggers.kinds()  # one module per kind in triggers/: see triggers/README.md
PR_URL = re.compile(r"https://(?:github\.com/[\w.-]+/[\w.-]+/pull/\d+|dev\.azure\.com/[\w%.-]+/[\w%.-]+/_git/[\w%.-]+/pullrequest/\d+)")
MAX_CHAIN = triggers.MAX_CHAIN


# ---- permission denials ------------------------------------------------------------
# A stream-json `result` event lists the tool calls the permission mode refused:
#   "permission_denials": [{"tool_name": "Bash", "tool_use_id": "toolu_..", "tool_input": {"command": "npm test"}}]
_SEGMENT = re.compile(r"\s*(?:&&|\|\||[;|&\n])\s*")
_RULE_RE = re.compile(r"[A-Za-z][\w.\-]*(\([^\r\n]*\))?")


def bash_prefix(command: str) -> str:
    """The leading words of a shell command that make a good allow rule: `npm test --silent` -> `npm test`."""
    segs = [s for s in _SEGMENT.split(str(command or "").strip()) if s.strip()]
    seg = next((s for s in segs if not re.match(r"\s*cd(\s|$)", s)), segs[0] if segs else "")
    words = seg.split()
    while words and re.fullmatch(r"[A-Za-z_]\w*=\S*", words[0]):
        words.pop(0)  # FOO=1 cmd
    if not words:
        return ""
    n = 1
    if words[0] in ("python", "python3", "py") and len(words) > 2 and words[1] == "-m":
        n = 3  # python -m pytest
    elif words[0] in ("npm", "pnpm", "yarn") and len(words) > 2 and words[1] == "run" and re.fullmatch(r"[\w:-]+", words[2]):
        n = 3  # npm run build
    elif len(words) > 1 and re.fullmatch(r"[A-Za-z][\w-]*", words[1]):
        n = 2  # git status, npm test, cargo build
    return " ".join(words[:n])


def allow_rule(tool: str, inp: dict | None) -> str:
    """A permission rule for a refused tool call, e.g. `Bash(npm test:*)`, `WebFetch(domain:example.com)`, `Edit`."""
    inp = inp if isinstance(inp, dict) else {}
    if tool == "Bash":
        prefix = bash_prefix(inp.get("command") or "")
        return f"Bash({prefix}:*)" if prefix else "Bash"
    if tool == "WebFetch" and (m := re.match(r"https?://([^/:?#\s]+)", str(inp.get("url") or ""))):
        return f"WebFetch(domain:{m.group(1)})"
    return tool


def clean_rule(rule) -> tuple[str | None, str]:
    """Validate a rule typed by the user: `Tool` or `Tool(specifier)`, one line."""
    rule = str(rule or "").strip()
    if not rule or len(rule) > 300 or not _RULE_RE.fullmatch(rule):
        return "a rule looks like Bash(npm test:*), WebFetch(domain:example.com) or a tool name", rule
    return None, rule


def parse_denials(raw) -> list[dict]:
    """Normalise a result event's permission_denials into {tool, cmd, rule, use_id}, dropping anything malformed."""
    out = []
    for d in raw if isinstance(raw, list) else []:
        if not isinstance(d, dict) or not isinstance(d.get("tool_name"), str) or not d["tool_name"]:
            continue
        tool, inp = d["tool_name"], d.get("tool_input") if isinstance(d.get("tool_input"), dict) else {}
        cmd = inp.get("command") or inp.get("file_path") or inp.get("url") or inp.get("pattern") or inp.get("path") or (json.dumps(inp)[:200] if inp else "")
        out.append({"tool": tool[:100], "cmd": str(cmd)[:300], "rule": allow_rule(tool, inp), "use_id": str(d.get("tool_use_id") or "")[:80]})
    return out


def today() -> str:
    return time.strftime("%Y-%m-%d")


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
        self.demo = False
        self.key = f"{project.name[:24]}-{hashlib.sha1(str(project).lower().encode()).hexdigest()[:6]}"
        self.state_file = WORK_ROOT / "state" / f"{self.key}.json"
        self.state_file.parent.mkdir(parents=True, exist_ok=True)
        self.procs: dict[str, subprocess.Popen] = {}
        self.writers: dict[str, agents.procs.StdinWriter] = {}
        self.pids: dict[int, str] = {}   # live agent processes this run started: pid -> agent name (mirrored to the pidfile)
        self.pid_lock = threading.Lock()
        self.pid_file = WORK_ROOT / "pids" / f"{self.key}.json"
        self._busy: dict[int, str] = {}  # task id -> the git action running outside the lock
        self._jobs: list[tuple] = []     # git work queued by event handlers, run outside the lock (see _run_jobs)
        self.git_mutex = threading.Lock()  # one merge / push at a time in the project folder
        self.next_denial = 1
        self.day, self.day_spend, self.day_hit = today(), 0.0, None  # today's spend across agents and the boss
        self.mcp_allow: set[str] = set()   # MCP server prefixes seen in agents' tool lists
        self.triggers: list[dict] = []
        self.next_trig = 1
        self.runs: list[dict] = []
        self.next_run = 1
        self.alerts: list[dict] = []  # raised by agents (crew tool raise_alert) or by Crew; shown on the board until resolved
        self.next_alert = 1
        self.agents: dict[str, dict] = {}
        for spec in config["agents"]:
            self.agents[spec["name"]] = agents.record.new_agent(self, spec)
        self.boss = agents.record.new_agent(self, {"name": "boss", "role": "Runs the crew", "tags": [],
                                     "model": (config.get("boss") or {}).get("model") or "opus",
                                     "effort": (config.get("boss") or {}).get("effort"), "context": "keep"})
        self.boss["kind"] = "boss"
        self._load()
        for a in self.agents.values():  # re-attach worktrees made in earlier runs
            agents.record.attach_worktree(self, a)

    def _save_config(self) -> None:
        """Write the agent roster and mode back to crew.json, keeping any other keys."""
        self.config["agents"] = agents.record.roster(self)
        if not self.config_path:
            return
        try:
            saved = load_json_file(self.config_path) if self.config_path.exists() else {}
        except ValueError:
            saved = {}
        saved["agents"] = self.config["agents"]
        saved["permission_mode"] = self.config["permission_mode"]
        saved["workflows"] = self.config.get("workflows", [])
        if self.config.get("boss"):
            saved["boss"] = self.config["boss"]
        for k in ("budget_task_usd", "budget_day_usd"):
            if k in self.config:
                saved[k] = self.config[k]
        if "allowed_tools" in saved or self.config.get("allowed_tools") != DEFAULT_CONFIG["allowed_tools"]:
            saved["allowed_tools"] = self.config.get("allowed_tools") or []  # rules added from the permission inbox
        write_atomic(self.config_path, json.dumps(saved, indent=2))

    def _launch_key(self, a: dict) -> tuple:
        return agents.launch.launch_key(self, a)

    def _once_rules(self, a: dict) -> list[str]:
        return agents.launch.once_rules(self, a)

    # ---- persistence & change notification ---------------------------------
    def _load(self) -> None:
        if not self.state_file.exists():
            return
        try:
            data = load_json_file(self.state_file)
        except ValueError as e:
            bad = self.state_file.with_name(f"{self.state_file.name}.corrupt-{time.strftime('%Y%m%d-%H%M%S')}")
            try:
                os.replace(self.state_file, bad)
            except OSError:
                pass
            print(f"crew: state file {self.state_file} is unreadable ({e}); moved to {bad.name} and starting empty", file=sys.stderr)
            self.event("error", f"state file was corrupt; moved to {bad.name} and started empty")
            return
        if data.get("project") != str(self.project):
            return
        self.mcp_allow = set(data.get("mcp_allow", []))
        self.triggers = data.get("triggers", [])
        self.runs = data.get("runs", [])[-50:]
        self.alerts = data.get("alerts", [])[-200:]
        self.next_alert = data.get("next_alert", max([x["id"] for x in self.alerts], default=0) + 1)
        self.next_run = data.get("next_run", len(self.runs) + 1)
        self.next_trig = data.get("next_trig", len(self.triggers) + 1)
        for tr in self.triggers:
            tr["next"] = trig.schedule_next(tr) if (trig := triggers.get(tr["kind"])) else None
        if data.get("day") == today():  # the day's spend survives a restart; a new day starts at zero
            self.day, self.day_spend, self.day_hit = data["day"], float(data.get("day_spend") or 0), data.get("day_hit")
        self.tasks = data.get("tasks", [])
        for tk in self.tasks:  # PR links in work that finished before Crew read them
            self._note_prs(tk, tk.get("details"), tk.get("summary"))
        self.next_id = data.get("next_id", len(self.tasks) + 1)
        for t in self.tasks:  # nothing is running after a restart
            if (t.get("diff") or {}).get("pending"):
                t.pop("diff")  # its collection never finished: the review sheet's refresh button collects it again
            if t["col"] == "doing":
                t["col"] = "backlog"
            if t["col"] == "backlog" and t.get("who") and t["who"] not in self.agents:
                t["who"] = t["pinned"] = None  # its agent left the roster; let the tags route it
        if b := data.get("boss"):
            self.boss.update({k: b[k] for k in ("session", "cost", "log", "tokens", "ctx") if k in b})
        for name, saved in data.get("agents", {}).items():
            if name in self.agents:
                self.agents[name].update({k: saved[k] for k in ("session", "cost", "log", "tokens", "mcp", "skills", "skill_names", "plugins", "commands") if k in saved})
                if self.agents[name]["status"] != "idle":
                    self.agents[name]["status"] = "idle"

    def _save(self) -> None:
        data = {"project": str(self.project), "tasks": self.tasks, "next_id": self.next_id,
                "mcp_allow": sorted(self.mcp_allow), "triggers": self._public_triggers(), "next_trig": self.next_trig,
                "runs": self.runs[-50:], "next_run": self.next_run,
                "alerts": self.alerts[-200:], "next_alert": self.next_alert,
                "day": self.day, "day_spend": round(self.day_spend, 6), "day_hit": self.day_hit,
                "agents": {n: {"session": a["session"], "cost": a["cost"], "tokens": a["tokens"], "log": a["log"][-200:],
                           "mcp": a["mcp"], "skills": a["skills"], "skill_names": a.get("skill_names", []), "plugins": a.get("plugins", []), "commands": a.get("commands", [])}
                       for n, a in self.agents.items()},
                "boss": {k: self.boss[k] for k in ("session", "cost", "tokens", "ctx")} | {"log": self.boss["log"][-200:]}}
        write_atomic(self.state_file, json.dumps(data, indent=1), bak_every=60)

    def touch(self) -> None:
        with self.lock:
            self.version += 1
            try:
                self._save()
            except OSError as e:  # a locked or full disk must not kill the thread that called us
                print(f"crew: could not save state: {e}", file=sys.stderr)
            self.changed.notify_all()

    def event(self, kind: str, text: str, agent: str | None = None) -> None:
        with self.lock:
            self.feed.append({"t": now(), "kind": kind, "text": text, "agent": agent})
            self.feed = self.feed[-60:]

    def log(self, a: dict, kind: str, text: str, **extra) -> None:
        a["log"].append({"t": now(), "kind": kind, "text": text, **extra})
        a["log"] = a["log"][-400:]

    # ---- alerts ----------------------------------------------------------------------
    def raise_alert(self, title: str, severity: str = "warning", details: str = "", agent: str | None = None,
                    link: str = "", key: str = "") -> dict:
        """Open an alert, or bump the open one with the same key (lock held). Returns the alert."""
        title = " ".join(str(title or "").split())[:160]
        severity = severity if severity in SEVERITIES else "warning"
        key = key or f"{agent or 'crew'}:{title.lower()}"
        al = next((x for x in self.alerts if x["key"] == key and x["status"] != "resolved"), None)
        if al:  # the same problem again: one alert with a count, never a pile of copies
            al["count"] += 1
            al["last"] = now()
            al["details"] = str(details or al["details"])[:4000]
            if SEVERITIES.index(severity) < SEVERITIES.index(al["severity"]):
                al["severity"], al["status"] = severity, "open"  # it got worse: needs eyes again
            return al
        al = {"id": self.next_alert, "key": key, "title": title, "severity": severity, "details": str(details or "")[:4000],
              "agent": agent, "link": str(link or "")[:500], "created": now(), "last": now(), "count": 1,
              "status": "open", "task": None}
        self.next_alert += 1
        self.alerts.append(al)
        self.alerts = [x for x in self.alerts if x["status"] != "resolved"][-150:] + \
            [x for x in self.alerts if x["status"] == "resolved"][-50:]
        self.alerts.sort(key=lambda x: x["id"])
        self.event("alert", ("🔴 " if severity == "critical" else "⚠ " if severity == "warning" else "ℹ ")
                   + (f"{agent}: " if agent else "") + title, agent)
        return al

    def resolve_alert(self, key: str, note: str = "") -> bool:
        """Resolve the open alert with this key, e.g. when a failing trigger works again (lock held)."""
        for al in self.alerts:
            if al["key"] == key and al["status"] != "resolved":
                al["status"], al["resolved"] = "resolved", now()
                if note:
                    al["note"] = note[:300]
                return True
        return False

    def _public_alerts(self) -> list[dict]:
        live = [x for x in self.alerts if x["status"] != "resolved"]
        return live + [x for x in self.alerts if x["status"] == "resolved"][-10:]

    def _alert_action(self, action: str, p: dict, via: str) -> dict:
        """alert (raise), alert_ack, alert_resolve, alert_task (lock held)."""
        if action == "alert":
            title = str(p.get("title") or "").strip()
            if not title:
                return {"error": "title required"}
            sev = p.get("severity") or "warning"
            if sev not in SEVERITIES:
                return {"error": "severity: critical, warning or info"}
            who = p.get("from") if p.get("from") in self.agents or p.get("from") == "boss" else None
            al = self.raise_alert(title, sev, p.get("details") or "", who, p.get("link") or "")
            return {"ok": True, "id": al["id"], "count": al["count"]}
        al = None
        if p.get("id") is not None:
            al = next((x for x in self.alerts if str(x["id"]) == str(p["id"])), None)
        elif p.get("title") and via == "mcp":  # an agent resolving its own alert by title once things recover
            key = f"{p.get('from') or 'crew'}:{' '.join(str(p['title']).split()).lower()}"
            al = next((x for x in self.alerts if x["key"] == key and x["status"] != "resolved"), None)
        if not al:
            return {"error": "no such alert"}
        if action == "alert_ack":
            if al["status"] == "open":
                al["status"] = "ack"
        elif action == "alert_resolve":
            if via == "mcp" and al["agent"] != p.get("from"):
                return {"error": "agents resolve only their own alerts"}
            al["status"], al["resolved"] = "resolved", now()
            if p.get("note"):
                al["note"] = str(p["note"])[:300]
            self.event("alert", f"✓ resolved: {al['title']}", al["agent"])
        elif action == "alert_task":
            if al["task"] and self._task(al["task"]):
                return {"error": f"already task #{al['task']}"}
            who = p.get("agent") or None
            if who and who not in self.agents:
                return {"error": f"no agent {who}"}
            t = {"id": self.next_id, "title": f"Alert: {al['title']}"[:200],
                 "details": (f"Raised by {al['agent'] or 'Crew'} ({al['severity']}"
                             + (f", seen {al['count']} times" if al["count"] > 1 else "") + ").\n\n" + al["details"]
                             + (f"\n\nLink: {al['link']}" if al["link"] else "")).strip(),
                 "tag": "incident" if "incident" in self._all_tags() else "bug", "col": "backlog",
                 "who": who, "pinned": who, "created": now(), "alert": al["id"]}
            self.next_id += 1
            self.tasks.append(t)
            al["task"] = t["id"]
            if al["status"] == "open":
                al["status"] = "ack"
            self.event("add", f"#{t['id']} {t['title']}" + (f" → {who}" if who else ""))
            return {"ok": True, "task": t["id"]}
        return {"ok": True}

    def _all_tags(self) -> list[str]:
        tags = list(TAGS)
        for a in self.agents.values():
            tags += [t for t in a["tags"] if t not in tags]
        return tags

    def busy(self) -> bool:
        """Work in flight: an agent or the boss mid-turn, or a workflow run going."""
        return (any(a["status"] == "running" for a in self.agents.values()) or self.boss["status"] == "running"
                or any(r["status"] == "running" for r in self.runs))

    def snapshot(self) -> dict:
        with self.lock:
            roster = []
            for a in self.agents.values():
                roster.append({k: v for k, v in a.items() if k != "log"} | {"log": a["log"][-120:], "alive": self._alive(a["name"])})
            boss = {k: v for k, v in self.boss.items() if k != "log"} | {"log": self.boss["log"][-160:], "alive": self._alive("boss")}
            self._roll_day()
            tasks = [t | {"busy": self._busy[t["id"]]} if t["id"] in self._busy else t for t in self.tasks]
            return {"version": self.version, "project": str(self.project), "git": self.git, "boss": boss,
                    "agents": roster, "tasks": tasks, "feed": self.feed[-30:],
                    "budget": {"task_usd": self._budget("budget_task_usd"), "day_usd": self._budget("budget_day_usd"),
                               "day_spent": round(self.day_spend, 4), "blocked": self._day_blocked()},
                    "permission_mode": self.config["permission_mode"], "modes": MODES,
                    "models": MODELS, "efforts": EFFORTS, "tags": self._all_tags(),
                    "project_name": self.project.name, "app_version": __version__, "mcp_allow": sorted(self.mcp_allow),
                    "allow_mcp": self.config.get("allow_mcp", True), "triggers": self._public_triggers(), "ado_base": self._ado_base(),
                    "trigger_kinds": TRIGGER_KINDS, "workflows": self.config.get("workflows", []),
                    "runs": self.runs[-30:], "contexts": CONTEXTS, "alerts": self._public_alerts(),
                    "awake": getattr(self, "awake", False), "demo": self.demo,
                    "scratch": self.project.parent.resolve() == WORK_ROOT.resolve()}

    # ---- worktrees -----------------------------------------------------------
    def _ensure_worktree(self, a: dict) -> None:
        agents.launch.ensure_worktree(self, a)

    def _stage(self, cwd: Path) -> None:
        """Stage everything except the local Claude files synced in from the project."""
        git(cwd, "add", "-A")
        if paths := agents.files.local_claude_files(self.project):
            git(cwd, "reset", "-q", "--", *paths)

    def diff(self, a: dict) -> dict:
        """Stage and diff an agent's worktree. Runs git: never call it while holding self.lock."""
        return self.diff_at(Path(a["cwd"]))

    def diff_at(self, cwd: Path) -> dict:
        if not self.git:
            return {"stat": "", "patch": "", "note": "Project is not a git repo, so no diff is available."}
        self._stage(cwd)
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
        return agents.launch.start(self, a)

    def _send(self, a: dict, text: str) -> bool:
        return agents.launch.send(self, a, text)

    def _check_writers(self) -> bool:
        return agents.launch.check_writers(self)

    def _fail(self, a: dict, msg: str) -> None:
        agents.launch.fail(self, a, msg)

    def _pid_flush(self) -> None:
        agents.launch.pid_flush(self)

    def _pid_add(self, pid: int, name: str) -> None:
        agents.launch.pid_add(self, pid, name)

    def _pid_drop(self, pid: int) -> None:
        agents.launch.pid_drop(self, pid)

    def reap_orphans(self) -> int:
        return agents.launch.reap_orphans(self)

    def _agent(self, name) -> dict | None:
        return self.boss if name == "boss" else self.agents.get(name or "")

    def _kill_proc(self, name: str, wait: bool = False) -> None:
        agents.launch.kill_proc(self, name, wait)

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
                    p.stdout.close()
                    return  # killed and replaced
                self._on_event(a, d)
            self._run_jobs()  # git work the event queued (diff, auto-approve), outside the lock
            self.touch()
        self._pid_drop(p.pid)
        p.stdout.close()
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
            if (tid := a["task"] or a.get("addressing")) and (tk := self._task(tid)):  # going back to this task resumes this conversation
                tk.setdefault("sessions", {})[a["name"]] = a["session"]
        if t == "system" and st == "compact_boundary":
            meta = d.get("compact_metadata") or {}
            self.log(a, "sys", f"context compacted ({meta.get('trigger', 'manual')}, was {int(meta.get('pre_tokens') or a['ctx']) // 1000}k tokens)")
            a["ctx"] = 0
        if t == "system" and st == "init":
            a["mcp"] = [{"name": m.get("name"), "status": m.get("status")} for m in d.get("mcp_servers") or []]
            a["skills"] = len(d.get("skills") or [])
            a["skill_names"] = sorted(str(x if isinstance(x, str) else x.get("name", "")) for x in d.get("skills") or [])[:200]
            a["plugins"] = sorted(str(x if isinstance(x, str) else x.get("name", "")) for x in d.get("plugins") or [])[:100]
            a["commands"] = sorted({str(x if isinstance(x, str) else x.get("name", "")).lstrip("/") for x in d.get("slash_commands") or []} - {""})[:300]
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
                if not d.get("parent_tool_use_id"):  # the main thread's window, not a subagent's
                    a["ctx"] = sum(int(u.get(k) or 0) for k in ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens"))
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
            tid = a["task"]
            self._on_result(a, d, st)
            self._check_budgets(a, tid)

    def _on_result(self, a: dict, d: dict, st) -> None:
        """A turn ended: cost, refused tool calls, then the task moves to review (or back, on an error)."""
        a["subs"] = {}
        if wins := [int(m.get("contextWindow") or 0) for m in (d.get("modelUsage") or {}).values() if isinstance(m, dict)]:
            a["ctx_max"] = max(wins)  # the window Claude Code reports for this model (1M today), for the context bar
        total = float(d.get("total_cost_usd") or 0)
        delta = max(0.0, total - a["proc_cost"])
        a["cost"] += delta
        a["proc_cost"] = total
        task = self._task(a["task"]) if a["task"] else None
        if task:
            self._note_prs(task, d.get("result"))
        if (rt := a.pop("addressing", None)) and (tk := self._task(rt)):
            tk.pop("reviewing", None)  # the /address-review turn is over
        self._charge(task, delta)
        self._note_denials(a, d.get("permission_denials"), a["task"])
        if a.get("compacting"):  # a /compact turn: no task changes
            a.update(status=a["compacting"], compacting=None, activity="")
            self.log(a, "done", "compaction finished")
            return
        if d.get("is_error") or st != "success":
            msg = d.get("result") or st or "error"
            a["status"], a["error"] = "error", str(msg)[:400]
            if not (a["log"] and a["log"][-1]["kind"] == "text" and a["log"][-1]["text"].strip() == a["error"].strip()):
                self.log(a, "error", a["error"])
            else:
                a["log"][-1]["kind"] = "error"
            self.event("error", f"{a['name']}: {a['error'][:120]}", a["name"])
            if task:
                self._task_event(task, "error", summary=a["error"])
                task["col"], task["who"] = "backlog", task.get("pinned")
                a["task"] = None
            return
        if task and (dec := task.get("decision")):  # a gate (crew tool decide): no review; continue or stop here
            tk = a["tokens"]
            task.update(tokens=tk["in"] + tk["cache"] + tk["out"] - task.get("tok0", 0), col="done", finished=now(),
                        summary=(d.get("result") or "").strip() or dec["reason"])
            a.update(status="idle", task=None)
            if dec["verdict"] == "continue":
                self._task_event(task, "done")  # the workflow's next step starts, done triggers fire
        elif task:
            tk = a["tokens"]
            task["tokens"] = tk["in"] + tk["cache"] + tk["out"] - task.get("tok0", 0)
            task["summary"] = d.get("result", "")
            task["col"] = "review"
            self._queue_diff(task)
            task["finished"] = now()
            a["status"] = "review"
            self.event("review", f"{a['name']} finished #{task['id']} · {task['title']}", a["name"])
            self._task_event(task, "review")
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

    # ---- permission inbox ----------------------------------------------------------------------
    def _note_denials(self, a: dict, raw, tid) -> None:
        """Tool calls the permission mode refused this turn (the result event's permission_denials) go to the agent's
        inbox, one entry per tool + rule per task. Nothing is allowed automatically."""
        found = parse_denials(raw)
        if not found:
            return
        if a.get("kind") == "boss":  # the boss is read-only by design: note it, don't offer to widen it
            self.log(a, "sys", "blocked: " + "; ".join(f"{f['tool']} {f['cmd']}"[:80] for f in found[:3]))
            return
        new = []
        for f in found:
            have = next((x for x in a["denials"] if x["task"] == tid and x["tool"] == f["tool"] and x["rule"] == f["rule"]), None)
            if have:
                have.update(n=have["n"] + 1, cmd=f["cmd"], t=now())
                continue
            a["denials"].append({"id": self.next_denial, "tool": f["tool"], "cmd": f["cmd"], "rule": f["rule"], "task": tid, "n": 1, "t": now()})
            self.next_denial += 1
            new.append(f)
        a["denials"] = a["denials"][-30:]
        if new:
            f = new[0]
            self.log(a, "sys", f"blocked: {f['tool']} {f['cmd']}"[:200] + (f" (+{len(new) - 1} more)" if len(new) > 1 else "") + " · see the Blocked list")
            self.event("blocked", f"{a['name']} was blocked from {f['tool']} {f['cmd'][:60]}" + (f" (+{len(new) - 1} more)" if len(new) > 1 else ""), a["name"])

    def _drop_denials(self, tid: int) -> None:
        for ag in self.agents.values():
            if any(x["task"] == tid for x in ag["denials"]):
                ag["denials"] = [x for x in ag["denials"] if x["task"] != tid]

    def _end_task_rules(self, t: dict, a: dict) -> None:
        """A task ended: its allow-once rules go, and an idle process started with them restarts without them."""
        if t.pop("allow_once", None) and a["status"] != "running" and self._alive(a["name"]):
            self._kill_proc(a["name"])

    def _denial_allow(self, a: dict, p: dict) -> dict:
        """Allow a refused call (always, or just until its task ends), then restart the agent with the new flags and ask
        it to retry. The session is kept, so it carries on where it stopped."""
        d = next((x for x in a["denials"] if x["id"] == p.get("id")), None)
        if not d:
            return {"error": "that blocked call is no longer in the inbox"}
        err, rule = clean_rule(p.get("rule") or d["rule"])
        if err:
            return {"error": err}
        once = bool(p.get("once"))
        if once:
            t = self._task(d["task"])
            if not t or a["task"] != t["id"]:
                return {"error": "this call isn't tied to the agent's current task: use Allow & retry instead"}
            if rule not in t.setdefault("allow_once", []):
                t["allow_once"].append(rule)
        else:
            cur = list(self.config.get("allowed_tools") or [])
            if rule not in cur:
                self.config["allowed_tools"] = [*cur, rule]  # a new list: DEFAULT_CONFIG's must stay as it is
            self._save_config()
        a["denials"] = [x for x in a["denials"] if x["id"] != d["id"] and x["rule"] != rule]
        self.event("config", f"allowed {rule}" + (" until this task ends" if once else "") + f" · {a['name']}", a["name"])
        for ag in self.agents.values():  # idle processes pick the rule up on their next start
            if ag["status"] != "running" and self._alive(ag["name"]) and tuple(ag.get("launched") or ()) != self._launch_key(ag):
                self._kill_proc(ag["name"])
        return self._retry(a, d)

    def _retry(self, a: dict, d: dict) -> dict:
        if a["status"] == "running":
            return {"ok": True, "note": f"{a['name']} is working: the rule applies from its next start"}
        if a["status"] in ("killed", "paused"):
            return {"ok": True, "note": f"{a['name']} is {a['status']}: the rule is saved, nothing was retried"}
        if self._day_blocked():
            return {"ok": True, "note": "the daily budget is reached: the rule is saved, nothing was retried"}
        self._kill_proc(a["name"])  # restarts with the rule in its flags; the session is kept
        if not self._start(a):
            return {"error": a["error"]}
        msg = f"The command you needed is now allowed; retry it.\n({d['tool']}: {d['cmd'][:200]})"
        t = self._task(d["task"])
        if t and t["col"] == "review" and t.get("who") == a["name"] and a["task"] == t["id"]:
            t.update(col="doing", started=now())  # back at work on it, like a send-back
        a.update(status="running", since=now(), activity="retrying…", error=None)
        self.log(a, "you", msg)
        if not self._send(a, msg):
            a.update(status="error", activity="")
            if t and t["col"] == "doing":
                t["col"] = "review"
            return {"error": a["error"]}
        return {"ok": True}

    # ---- budgets -----------------------------------------------------------------------------------
    # stream-json only reports cost when a turn ends (the result event's total_cost_usd), so budgets are checked then.
    def _budget(self, key: str) -> float:
        try:
            v = float(self.config.get(key) or 0)
        except (TypeError, ValueError):
            return 0.0
        return v if v > 0 else 0.0

    def _roll_day(self) -> bool:
        """New local day: today's spend starts again from zero and work held by yesterday's budget resumes."""
        if self.day == today():
            return False
        self.day, self.day_spend, self.day_hit = today(), 0.0, None
        self.event("budget", "new day: today's spend is back at $0")
        self._budget_refresh()
        return True

    def _day_blocked(self) -> bool:
        self._roll_day()
        bd = self._budget("budget_day_usd")
        return bool(bd) and self.day_spend >= bd

    def _over_task(self, t: dict) -> bool:
        bt = self._budget("budget_task_usd")
        return bool(bt) and (t.get("cost") or 0) >= bt

    def _charge(self, t: dict | None, delta: float) -> None:
        if delta <= 0:
            return
        self._roll_day()
        self.day_spend += delta
        if t is not None:
            t["cost"] = round((t.get("cost") or 0) + delta, 6)

    def _check_budgets(self, a: dict, tid) -> None:
        """After a turn: stop work that crossed its task budget, or everyone when the day's budget is crossed."""
        t = self._task(tid) if tid else None
        if t and self._over_task(t) and not t.get("budget_hit"):
            t["budget_hit"] = True
            msg = f"budget reached (${t['cost']:.2f} of ${self._budget('budget_task_usd'):.2f})"
            if t["col"] == "backlog":  # it errored back into the queue: keep it from being picked up again
                t.update(hold=True, budget_hold=True)
            if a["status"] == "running":
                self._stop_agent(a, f"#{t['id']} {msg}")
            elif self._alive(a["name"]):
                self._kill_proc(a["name"])  # the finished work stays in review; a send-back is refused until the budget is raised
            self.log(a, "sys", f"#{t['id']} {msg}")
            self.event("budget", f"#{t['id']} {msg}", a["name"])
        bd = self._budget("budget_day_usd")
        if bd and self.day_spend >= bd and self.day_hit != self.day:
            self.day_hit = self.day
            msg = f"budget reached (${self.day_spend:.2f} of ${bd:.2f} today)"
            n = 0
            for ag in [*self.agents.values(), self.boss]:
                if ag["status"] == "running":
                    tk = self._task(ag["task"]) if ag["task"] else None
                    self._stop_agent(ag, msg)
                    ag["budget_paused"] = True
                    if tk and tk.get("hold"):
                        tk["budget_hold"] = True
                    n += 1
            self.event("budget", f"{msg}: " + (f"stopped {n} agent{'s' if n != 1 else ''}; " if n else "") + "no new work until tomorrow or you raise the budget")
            self.raise_alert("Daily budget reached: agents paused", "warning",
                             f"{msg}. No new work starts until tomorrow or until you raise the budget (header → Budgets).", key=f"budget:{self.day}")

    def _budget_refresh(self) -> None:
        """A new day or changed budgets: clear flags that no longer apply and resume what only the budget was holding."""
        bd = self._budget("budget_day_usd")
        blocked = bool(bd) and self.day_spend >= bd
        resumed = False
        if not blocked:
            self.day_hit = None
            for ag in [*self.agents.values(), self.boss]:
                if ag.get("budget_paused"):
                    ag["budget_paused"] = False
                    if ag["status"] == "paused":
                        ag["status"], resumed = "idle", True
        for t in self.tasks:
            over = self._over_task(t)
            if t.get("budget_hit") and not over:
                t.pop("budget_hit")
            if t.get("budget_hold") and not blocked and not over:
                t.pop("budget_hold")
                t.pop("hold", None)
                resumed = True
        if resumed:
            self.event("budget", "budget cleared: held work resumes")
            self.resolve_alert(f"budget:{self.day}", "budget raised or cleared")

    # ---- tasks -----------------------------------------------------------------
    def _task(self, tid) -> dict | None:
        return next((t for t in self.tasks if t["id"] == tid), None)

    def _begin(self, a: dict, t: dict) -> bool:
        if self._day_blocked():
            return False  # the daily budget is spent: the task waits in the backlog
        if self._over_task(t):
            self.event("budget", f"#{t['id']} is over its task budget (${t.get('cost') or 0:.2f} of ${self._budget('budget_task_usd'):.2f}): "
                                 "raise the budget to run it again", a["name"])
            return False
        if self._git_busy(a["name"]):
            return False
        self._task_session(a, t)
        self._note_prs(t, t.get("details"))
        if not self._start(a):
            return False
        a["last_task"] = t["id"]
        a["denials"] = [x for x in a["denials"] if x["task"] == t["id"]]  # the inbox is per task
        tk = a["tokens"]
        t.update(col="doing", who=a["name"], started=now(), tok0=tk["in"] + tk["cache"] + tk["out"] - t.get("tokens", 0))
        a.update(status="running", task=t["id"], since=now(), activity="starting…")
        body = f"Task #{t['id']}: {t['title']}"
        if t.get("details"):
            body += "\n\n" + t["details"]
        self.log(a, "you", body)
        if not self._send(a, body):
            t.update(col="backlog", who=t.get("pinned"))
            a.update(status="error", task=None, activity="", since=None)
            return False
        self.event("start", f"{a['name']} picked #{t['id']} · {t['title']}", a["name"])
        return True

    def _task_session(self, a: dict, t: dict) -> None:
        agents.launch.task_session(self, a, t)

    @staticmethod
    def _note_prs(t: dict, *texts: str | None) -> None:
        """PRs an agent opened or works on (it runs gh itself), picked out of its task text and summary."""
        found = [m for x in texts if x for m in PR_URL.findall(x)]
        if found:
            t["prs"] = list(dict.fromkeys([*(t.get("prs") or []), *found]))[-5:]

    _ado_dflt: dict | None = None  # az devops defaults, read once in the background when nothing else names the board

    def _ado_base(self) -> str:
        """Where an AB#123 on a card links: '<org>/<project>/_workitems/edit/' from an item Crew fetched, an ADO
        trigger, or `az devops configure` defaults. Empty until one is known. Lock held."""
        for tk in reversed(self.tasks):
            u = (tk.get("ado") or (tk.get("source") or {}).get("ado") or {}).get("url") or ""
            if m := re.match(r"(https://\S+?/_workitems/edit/)\d+$", u):
                return m.group(1)
        for tr in self.triggers:
            sp = tr.get("spec") or {}
            if tr.get("kind") == "ado" and sp.get("org") and sp.get("project"):
                return f"{str(sp['org']).rstrip('/')}/{quote(sp['project'])}/_workitems/edit/"
        d = Crew._ado_dflt
        if d is None:
            Crew._ado_dflt = {}
            threading.Thread(target=Crew._read_ado_dflt, daemon=True).start()
        elif d.get("org") and d.get("project"):
            return f"{d['org'].rstrip('/')}/{quote(d['project'])}/_workitems/edit/"
        return ""

    @staticmethod
    def _read_ado_dflt() -> None:
        try:
            Crew._ado_dflt = azure.ado_defaults()
        except (azure.AzError, OSError):
            pass

    def _forget_sessions(self, name: str) -> None:
        agents.launch.forget_sessions(self, name)

    def dispatch(self) -> None:
        with self.lock:
            moved = self._roll_day() | self._check_writers()
            if not self._day_blocked():  # a spent daily budget dispatches nothing until tomorrow or a higher budget
                for a in self.agents.values():
                    if a["status"] != "idle" or a.get("off") or self._git_busy(a["name"]):
                        continue
                    t = next((t for t in self.tasks if t["col"] == "backlog" and not t.get("hold") and not self._over_task(t) and
                              (t.get("who") == a["name"] or (not t.get("who") and t["tag"] in a["tags"]))), None)
                    if t:
                        self._begin(a, t)
                        moved = True
        if moved:
            self.touch()

    def _git_busy(self, name: str) -> bool:
        """Is a git action (approve, undo, merge, PR, diff) running on one of this agent's tasks?"""
        return any((tk := self._task(i)) and tk.get("who") == name for i in self._busy)

    def _release(self, t: dict) -> None:
        """Detach a task from whoever holds it, from any column. A running owner is stopped (session kept)."""
        owner = self.agents.get(t.get("who") or "")
        if owner and owner["task"] == t["id"]:
            if t["col"] == "doing":
                self._kill_proc(owner["name"])
                self.log(owner, "sys", f"stopped: #{t['id']} was moved")
            owner.update(status="idle", task=None, activity="", since=None)
            self._end_task_rules(t, owner)
        self._drop_denials(t["id"])
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

    # ---- triggers -------------------------------------------------------------------
    # Assigning a task is the manual trigger. These add the automatic ones: each firing adds a
    # backlog task from the trigger's template, and the dispatcher hands it out like any other.
    def _public_triggers(self) -> list[dict]:
        out = [{k: v for k, v in tr.items() if not k.startswith("_")} for tr in self.triggers]
        for pub, tr in zip(out, self.triggers):
            if (trig := triggers.get(tr["kind"])) and trig.polled:  # same rule as tick: never checked means due now
                pub["next"] = trig.next_check(tr)
                pub["checking"] = bool(tr.get("_running"))
        return out

    # what the modules in agents/, workflows/ and triggers/ read from the server's module state (the tests redirect WORK_ROOT)
    MAX_CHAIN = MAX_CHAIN

    @property
    def work_root(self) -> Path:
        return WORK_ROOT  # where agent worktrees and pid files live, and where trigger checks that run `claude` work

    @property
    def port(self) -> int:
        return PORT

    @property
    def mcp_token(self) -> str:
        return MCP_TOKEN

    def _trigger(self, tid) -> dict | None:
        return next((tr for tr in self.triggers if tr["id"] == tid), None)

    def _fire(self, tr: dict, ctx: dict, note: str = "") -> dict:
        """Turn one firing into a backlog task. Caller holds the lock."""
        lt = time.localtime()
        ctx = {"date": time.strftime("%Y-%m-%d", lt), "time": time.strftime("%H:%M", lt),
               "weekday": time.strftime("%A", lt), "trigger": tr["name"]} | ctx
        spec = tr["task"]
        title = fill(spec.get("title") or tr["name"], ctx).strip()[:200] or tr["name"]
        if (w := self._workflow(spec.get("workflow"))) is not None:
            run = self._start_run(w, title, fill(spec.get("details") or "", ctx), source={"trigger": tr["id"], "name": tr["name"]})
            tr["fired"] = tr.get("fired", 0) + 1
            tr["last"], tr["note"] = now(), note[:200]
            self.event("trigger", f"⚡ {tr['name']} → ⛓ {w['name']}: {title}")
            return self._task(run["tasks"][0]) or {}
        who = spec.get("agent") if spec.get("agent") in self.agents else None
        t = {"id": self.next_id, "title": title, "details": fill(spec.get("details") or "", ctx).strip()[:8000],
             "tag": spec.get("tag") if spec.get("tag") in self._all_tags() else "feature", "col": "backlog",
             "who": who, "pinned": who, "created": now(), "depth": ctx.get("depth", 0),
             "source": {"trigger": tr["id"], "name": tr["name"], "kind": tr["kind"]}}
        self.next_id += 1
        self.tasks.append(t)
        tr["fired"] = tr.get("fired", 0) + 1
        tr["last"], tr["note"] = now(), note[:200]
        self.event("trigger", f"⚡ {tr['name']} → #{t['id']} {title}" + (f" → {who}" if who else ""), who)
        return t

    # ---- git actions, run outside the lock ----------------------------------------------------
    # approve, unapprove, merge, open_pr and diff shell out to git, which can take seconds. Holding self.lock that long
    # would freeze the SSE stream and every other request, so each is split in three: _x_plan (under the lock: validate
    # and capture what git needs), _x_git (no lock) and _x_apply (under the lock again, writing the result back). While
    # it runs the task is marked busy: a second click or a competing action on it is refused.
    def _git_action(self, action: str, p: dict) -> dict:
        with self.lock:
            t = self._task(p.get("task"))
            if not t:
                return {"error": f"bad action {action}"}
            if t["id"] in self._busy:
                return {"error": f"#{t['id']} is busy ({self._busy[t['id']]}): one moment"}
            plan = getattr(self, f"_{action}_plan")(t, p)
            if "error" in plan:
                return plan
            self._busy[t["id"]] = action
        self.touch()  # the task shows as busy while git runs
        try:
            res = getattr(self, f"_{action}_git")(plan)
        except Exception as e:  # never leave the task marked busy
            res = {"error": f"{action} failed: {e}"}
        with self.lock:
            self._busy.pop(t["id"], None)
            if t not in self.tasks:
                out = {"error": f"#{t['id']} was removed while {action} ran"}
            else:
                out = getattr(self, f"_{action}_apply")(t, plan, res)
        self.touch()
        return out

    def _approve(self, t: dict, by: str = "you") -> dict:
        """Commit the owner's worktree and move the task to Done. Don't call it holding the lock: git runs in between."""
        return self._git_action("approve", {"task": t["id"], "by": by})

    def _approve_plan(self, t: dict, p: dict) -> dict:
        if t["col"] != "review":
            return {"error": "bad action approve"}
        w = self.agents.get(t.get("who") or "")
        return {"w": w["name"] if w else None, "cwd": Path(w["cwd"]) if w and self.git else None, "by": p.get("by", "you"),
                "msg": f"#{t['id']} {t['title']}\n\n{(t.get('summary') or '')[:2000]}"}

    def _approve_git(self, plan: dict) -> dict:
        cwd = plan["cwd"]
        if not cwd:
            return {}
        self._stage(cwd)
        r = git(cwd, "-c", "user.name=crew", "-c", "user.email=crew@localhost", "commit", "-m", plan["msg"])
        if r.returncode != 0:
            return {"commit": None, "full": None}
        return {"commit": git(cwd, "rev-parse", "--short", "HEAD").stdout.strip(), "full": git(cwd, "rev-parse", "HEAD").stdout.strip()}

    def _approve_apply(self, t: dict, plan: dict, res: dict) -> dict:
        w = self.agents.get(plan["w"] or "")
        if plan["cwd"]:
            t["commit"], t["commit_full"] = res.get("commit"), res.get("full")
            for k in ("merged", "merge_error", "pr"):
                t.pop(k, None)
        t["col"] = "done"
        if (t.get("diff") or {}).get("pending"):
            t.pop("diff")
        if w:
            w.update(status="idle", task=None)
            self._end_task_rules(t, w)
        self._drop_denials(t["id"])
        self.event("approve", f"{'auto-' if plan['by'] != 'you' else ''}approved #{t['id']}" + (f" · commit {t.get('commit')} on {w['branch']}" if t.get("commit") and w else ""))
        self._task_event(t, "done")
        return {"ok": True, "undo": bool(t.get("commit_full"))}

    def _unapprove_plan(self, t: dict, p: dict) -> dict:
        """Undo an approval: soft-reset the agent's worktree and move the task back to Review."""
        w = self.agents.get(t.get("who") or "")
        if t["col"] != "done" or not t.get("commit_full") or not w or not self.git:
            return {"error": "nothing to undo"}
        if t.get("merged") or t.get("pr"):
            return {"error": "already merged or in a PR: can't undo"}
        if w["status"] in ("running", "review") or w.get("task"):
            return {"error": f"{w['name']} has moved on to other work: can't undo"}
        return {"w": w["name"], "cwd": Path(w["cwd"]), "sha": t["commit_full"]}

    def _unapprove_git(self, plan: dict) -> dict:
        cwd = plan["cwd"]
        if git(cwd, "rev-parse", "HEAD").stdout.strip() != plan["sha"]:
            return {"error": "the branch has new commits since this approval: can't undo"}
        r = git(cwd, "reset", "--soft", "HEAD~1")
        if r.returncode != 0:
            return {"error": "undo failed: " + r.stderr.strip()[:200]}
        return {"diff": self.diff_at(cwd)}

    def _unapprove_apply(self, t: dict, plan: dict, res: dict) -> dict:
        if "error" in res:
            return res
        for k in ("commit", "commit_full"):
            t.pop(k, None)
        t["col"] = "review"
        if w := self.agents.get(plan["w"]):
            w.update(status="review", task=t["id"])
        t["diff"] = res["diff"]
        self.event("reject", f"undid approval of #{t['id']}", plan["w"])
        return {"ok": True}

    def _merge_plan(self, t: dict, p: dict) -> dict:
        """Merge an approved task's branch into the project's current branch."""
        w = self.agents.get(t.get("who") or "")
        if not self.git or t["col"] != "done" or not t.get("commit") or not w or not w.get("branch"):
            return {"error": "only an approved task with a commit can be merged"}
        if t.get("merged"):
            return {"error": "already merged"}
        return {"br": w["branch"], "w": w["name"], "id": t["id"], "title": t["title"]}

    def _merge_git(self, plan: dict) -> dict:
        br, proj = plan["br"], self.project
        with self.git_mutex:  # one merge or push at a time in the project folder
            base = git(proj, "symbolic-ref", "--short", "-q", "HEAD").stdout.strip()
            if not base:
                return {"error": "the project folder is on a detached HEAD: check out a branch first"}
            if git(proj, "status", "--porcelain", "--untracked-files=no").stdout.strip():
                return {"error": f"{proj.name} has uncommitted changes to tracked files: commit or stash them, then merge"}
            head = lambda: git(proj, "rev-parse", "--short", "HEAD").stdout.strip()
            if git(proj, "merge-base", "--is-ancestor", br, "HEAD").returncode == 0:
                return {"merged": {"commit": head(), "at": now(), "into": base}, "already": True}
            r = git(proj, "-c", "user.name=crew", "-c", "user.email=crew@localhost", "merge", "--no-ff", "-m", f"Merge {br}: #{plan['id']} {plan['title']}", br)
            if r.returncode != 0:
                files = git(proj, "diff", "--name-only", "--diff-filter=U").stdout.split()
                git(proj, "merge", "--abort")
                msg = (r.stderr.strip() or r.stdout.strip())[:300]
                return {"error": f"merge into {base} conflicts in: {', '.join(files)}" if files else f"merge failed: {msg}", "conflicts": files,
                        "merge_error": {"files": files, "base": base, "at": now(), "msg": msg}}
            return {"merged": {"commit": head(), "at": now(), "into": base}}

    def _merge_apply(self, t: dict, plan: dict, res: dict) -> dict:
        if "merged" in res:
            t["merged"] = res["merged"]
            t.pop("merge_error", None)
            if not res.get("already"):
                self.event("approve", f"merged #{t['id']} into {t['merged']['into']} · {t['merged']['commit']}", plan["w"])
            return {"ok": True, "merged": t["merged"]}
        if me := res.get("merge_error"):
            t["merge_error"] = me
            self.event("error", f"merging #{t['id']} into {me['base']} " + (f"conflicts in {len(me['files'])} file(s)" if me["files"] else "failed"), plan["w"])
            return {"error": res["error"], "conflicts": res["conflicts"]}
        return {"error": res["error"]}

    def _open_pr_plan(self, t: dict, p: dict) -> dict:
        """Push the task's branch (never forced) and open a PR with gh."""
        w = self.agents.get(t.get("who") or "")
        if not self.git or t["col"] != "done" or not t.get("commit") or not w or not w.get("branch"):
            return {"error": "only an approved task with a commit can open a PR"}
        if t.get("pr"):
            return {"error": "a PR is already open: " + t["pr"]}
        return {"br": w["branch"], "w": w["name"], "title": f"#{t['id']} {t['title']}"[:200],
                "body": (t.get("summary") or f"Task #{t['id']} from Crew.").strip()[:4000]}

    def _open_pr_git(self, plan: dict) -> dict:
        if not shutil.which("gh"):
            return {"error": "the GitHub CLI (gh) isn't installed or isn't on PATH"}
        if git(self.project, "remote", "get-url", "origin").returncode != 0:
            return {"error": "this repo has no 'origin' remote to push to"}
        env = os.environ | {"GIT_TERMINAL_PROMPT": "0"}
        run = lambda *cmd: subprocess.run(cmd, cwd=self.project, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=90, env=env)
        try:
            with self.git_mutex:
                r = run("git", "push", "-u", "origin", plan["br"])
                if r.returncode != 0:
                    return {"error": "push failed: " + (r.stderr.strip() or r.stdout.strip())[-300:]}
                r = run("gh", "pr", "create", "--head", plan["br"], "--title", plan["title"], "--body", plan["body"])
        except (subprocess.TimeoutExpired, OSError) as e:
            return {"error": f"PR failed: {e}"}
        m = re.search(r"https?://\S+", r.stdout + "\n" + r.stderr)
        if not m:
            return {"error": "gh pr create failed: " + (r.stderr.strip() or r.stdout.strip())[-300:]}
        return {"pr": m.group(0)}  # also covers "a pull request already exists: <url>"

    def _open_pr_apply(self, t: dict, plan: dict, res: dict) -> dict:
        if "pr" not in res:
            return res
        t["pr"] = res["pr"]
        self.event("approve", f"opened a PR for #{t['id']}: {t['pr']}", plan["w"])
        return {"ok": True, "pr": t["pr"]}

    def _diff_plan(self, t: dict, p: dict) -> dict:
        w = self.agents.get(t.get("who") or "")
        return {"cwd": Path(w["cwd"])} if w else {"error": "bad action diff"}

    def _diff_git(self, plan: dict) -> dict:
        return {"diff": self.diff_at(plan["cwd"])}

    def _diff_apply(self, t: dict, plan: dict, res: dict) -> dict:
        t["diff"] = res["diff"]
        return {"ok": True}

    def _queue_diff(self, t: dict) -> None:
        """A task reached review: its diff is collected right after the event handler returns, outside the lock."""
        if not self.git:
            t["diff"] = self.diff_at(Path(self.project))
            return
        t["diff"] = {"stat": "", "patch": "", "note": "Collecting the diff…", "pending": True}
        self._jobs.append(("diff", t["id"]))

    def _run_jobs(self) -> None:
        """Run git work queued by event handlers (diffs, workflow auto-approvals). Call without holding the lock."""
        while True:
            with self.lock:
                if not self._jobs:
                    return
                job = self._jobs.pop(0)
                t = self._task(job[1])
                if not t or t["col"] != "review":
                    continue
            try:
                self._git_action(job[0], {"task": t["id"], **({"by": job[2]} if len(job) > 2 else {})})
            except Exception as e:  # keep the reader thread alive
                print("crew: background git job failed:", e, file=sys.stderr)

    def _merge_rebase(self, t: dict) -> dict:
        """After a conflict: ask the owner to merge the base into its branch and resolve. Caller holds the lock."""
        me, w = t.get("merge_error") or {}, self.agents.get(t.get("who") or "")
        if not me.get("files") or not w or w["status"] == "killed":
            return {"error": "no conflict to send back, or the agent is unavailable"}
        files = "\n".join(f"- {f}" for f in me["files"])
        nt = {"id": self.next_id, "title": f"Resolve merge conflicts for #{t['id']} against {me['base']}"[:200], "tag": t["tag"], "col": "backlog",
              "details": (f"Merging {w['branch']} into {me['base']} conflicts in:\n{files}\n\nIn your worktree run `git merge {me['base']} --no-commit --no-ff`, "
                          "resolve the conflicts keeping both sides' intent, and run the tests. Do not commit: leave the merge staged and Crew commits it when approved."),
              "who": w["name"], "pinned": w["name"], "created": now(), "depth": t.get("depth", 0) + 1,
              "handoff": {"from": "you", "task": t["id"]}}
        self.next_id += 1
        self.tasks.append(nt)
        me["sent"] = nt["id"]
        self.event("handoff", f"↪ #{t['id']} merge conflicts → {w['name']}: #{nt['id']}", w["name"])
        return {"ok": True, "task": nt["id"]}

    def _task_event(self, t: dict, col: str, summary: str | None = None) -> None:
        """Event triggers and workflow runs: a task reached review or done, or its agent hit an error."""
        self._wf_event(t, col, summary)
        for tr in self.triggers:
            if tr["enabled"] and (trig := triggers.get(tr["kind"])):
                trig.on_task_event(self, tr, t, col, summary)

    def tick_triggers(self) -> None:
        """Schedules, file watches and the polled kinds. Called every second by the dispatcher thread."""
        fired = False
        with self.lock:
            minute = int(now() // 60)
            lt = time.localtime()
            for tr in self.triggers:
                if tr["enabled"] and not self._target_off(tr) and (trig := triggers.get(tr["kind"])):
                    fired |= trig.tick(self, tr, minute, lt)
        if fired:
            self.touch()

    def hook(self, tid: str, token: str, body: str) -> dict:
        with self.lock:
            tr = self._trigger(tid)
            if not tr or tr["kind"] != "webhook" or not hmac.compare_digest(tr["spec"]["token"], token or ""):
                return {"error": "unknown hook or bad token"}
            if not tr["enabled"]:
                return {"error": "trigger is off"}
            if self._target_off(tr):
                return {"error": "the workflow this trigger starts is turned off"}
            t = self._fire(tr, {"payload": body[:6000]}, "webhook")
        self.touch()
        return {"ok": True, "task": t["id"]}

    def _clean_trigger(self, p: dict, old: dict | None) -> tuple[str | None, dict]:
        if not (trig := triggers.get(p.get("kind"))):
            return "pick a trigger type", {}
        return trig.clean(self, p, old)

    def _target_off(self, tr: dict) -> bool:
        """Does this trigger start a workflow that is turned off? Then it is not checked, so nothing is lost: items wait
        until the workflow is back on."""
        wid = (tr.get("spec") or {}).get("workflow") or (tr.get("task") or {}).get("workflow")
        return bool(wid and (w := self._workflow(wid)) and w.get("off"))

    # ---- workflows: see workflows/ ----------------------------------------------------
    def _workflow(self, wid) -> dict | None:
        return workflows.runs.find(self, wid)

    def _run(self, rid) -> dict | None:
        return workflows.runs.find_run(self, rid)

    def _start_run(self, w: dict, title: str, details: str = "", source: dict | None = None) -> dict:
        return workflows.runs.start_run(self, w, title, details, source)

    def _wf_event(self, t: dict, col: str, summary: str | None) -> None:
        workflows.runs.on_task_event(self, t, col, summary)

    def _clean_workflow(self, p: dict) -> tuple[str | None, dict]:
        return workflows.definitions.clean(self, p)

    def _stop_agent(self, a: dict, why: str = "stopped by you") -> None:
        agents.launch.stop_agent(self, a, why)

    # ---- actions from the UI ---------------------------------------------------
    def act(self, action: str, p: dict, via: str = "ui") -> dict:
        if via == "mcp" and action in ("add", "handoff", "wf_run") and (why := title_problem(p.get("title"))):
            return {"error": why}
        if action in GIT_ACTIONS:  # git runs outside the lock
            return self._git_action(action, p)
        with self.lock:
            a = self.agents.get(p.get("agent", ""))
            t = self._task(p.get("task"))
            if t and t["id"] in self._busy:
                return {"error": f"#{t['id']} is busy ({self._busy[t['id']]}): one moment"}
            if action in ("tell", "stop", "compact", "respawn", "resume", "clear_chat") and p.get("agent") == "boss":
                a = self.boss
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
                if a and a["status"] == "paused":
                    a["status"] = "idle"
                if t["col"] == "doing" and a and t.get("who") == a["name"]:
                    return {"error": f"{a['name']} is already on it"}
                was = t["col"]
                if was != "backlog":
                    self._release(t)
                t["col"] = "backlog"
                t["who"] = t["pinned"] = a["name"] if a else None
                t.pop("commit", None)
                t.pop("hold", None)
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
                            self._end_task_rules(t, owner)
                    t["col"] = p["col"]
                    t.pop("hold", None)
                    if p["col"] == "done":
                        self._task_event(t, "done")
            elif action == "mode" and p.get("mode") in MODES:
                self.config["permission_mode"] = p["mode"]
                self._save_config()
                for ag in self.agents.values():  # idle processes restart in the new mode; busy ones after their turn
                    if ag["status"] != "running" and self._alive(ag["name"]):
                        self._kill_proc(ag["name"])
                self.event("mode", f"permission mode → {p['mode']}")
            elif action == "configure" and a:
                if res := agents.record.configure(self, a, p):
                    return res
            elif action == "add_agent":
                if res := agents.record.add(self, p):
                    return res
            elif action in ("alert", "alert_ack", "alert_resolve", "alert_task"):
                res = self._alert_action(action, p, via)
                if "error" not in res:
                    self.touch()
                return res
            elif action == "import_agents":
                res = agents.record.import_agents(self, p.get("agents"), update=p.get("existing") == "update")
                if "error" not in res:
                    self.touch()
                return res
            elif action == "rename_agent" and a:
                if res := agents.record.rename(self, a, p):
                    return res
            elif action == "remove_agent" and a:
                if res := agents.record.remove(self, a):
                    return res
            elif action == "delete" and t:
                if t["col"] == "doing":
                    return {"error": "task is running"}
                self.tasks.remove(t)
                self._drop_denials(t["id"])
                if (run := self._run((t.get("run") or {}).get("id"))) and run["status"] == "running":
                    run.update(status="stopped", ended=now(), note=f"step {t['run']['step'] + 1}'s task was deleted")
            elif action == "tell" and a:
                text = (p.get("text") or "").strip()
                if not text:
                    return {"error": "empty message"}
                if a["status"] == "killed":
                    return {"error": "agent is killed"}
                if not self._start(a):
                    return {"error": a["error"]}
                self.log(a, "you", text, **({"by": p["from"]} if p.get("from") else {}))
                if not self._send(a, text):
                    a.update(status="error", activity="")
                    return {"error": a["error"]}
                if a["status"] in ("idle", "error", "paused", "review"):
                    a.update(status="running", since=now(), activity="thinking…", error=None)
                # a message about the task waiting for review is more work on it: back to In progress until the
                # agent's turn ends, which puts it in Review again with the new summary and diff
                rt = self._task(a["task"]) if a.get("task") else None
                if rt and rt["col"] == "review" and rt.get("who") == a["name"] and not rt.get("commit"):
                    rt.update(col="doing", started=now())
                    a["activity"] = "working on your message…"
                self.event("tell", f"→ {a['name']}: {text[:80]}", a["name"])
            elif action == "stop" and a:
                if a["status"] != "running" and not self._alive(a["name"]):
                    return {"error": f"{a['name']} is not running"}
                self._stop_agent(a)
                self.event("kill", f"■ {a['name']} stopped", a["name"])
            elif action == "stop_all":
                n = 0
                for ag in [*self.agents.values(), self.boss]:
                    if ag["status"] == "running":
                        self._stop_agent(ag, "stopped (stop all)")
                        n += 1
                runs = [r for r in self.runs if r["status"] == "running"]
                for r in runs:
                    r.update(status="stopped", ended=now(), note="stop all")
                self.event("kill", f"■ stopped {n} agent{'s' if n != 1 else ''}" + (f" and {len(runs)} workflow run{'s' if len(runs) != 1 else ''}" if runs else ""))
            elif action == "clear":
                scope = p.get("scope") if p.get("scope") in ("done", "all") else "done"
                if scope == "all":
                    for ag in self.agents.values():
                        if ag["status"] == "running":
                            self._stop_agent(ag, "stopped: board cleared")
                        ag.update(task=None, status="idle" if ag["status"] in ("review", "running") else ag["status"])
                    for r in self.runs:
                        if r["status"] == "running":
                            r.update(status="stopped", ended=now(), note="board cleared")
                    gone = len(self.tasks)
                    self.tasks = []
                else:
                    gone = sum(1 for x in self.tasks if x["col"] == "done")
                    self.tasks = [x for x in self.tasks if x["col"] != "done"]
                self.event("clear", f"cleared {gone} task{'s' if gone != 1 else ''}" + (" (whole board)" if scope == "all" else " from Done"))
            elif action == "compact" and a:
                if a["status"] == "running":
                    return {"error": f"{a['name']} is busy; compact after this turn"}
                if not self._alive(a["name"]):
                    return {"error": "no live session to compact; its next task starts with a fresh context anyway"}
                a.update(compacting=a["status"], status="running", since=now(), activity="compacting context…")
                self.log(a, "you", "/compact")
                if not self._send(a, "/compact"):
                    a.update(compacting=None, status="error", activity="")
                    return {"error": a["error"]}
                self.event("tell", f"{a['name']}: compacting context", a["name"])
            elif action == "handoff":
                src = self.agents.get(p.get("from") or "")
                to = (p.get("to") or "").strip()
                title = (p.get("title") or "").strip()
                if not title:
                    return {"error": "handoff needs a title"}
                if to.lower() != "auto" and to not in self.agents:
                    return {"error": f"no agent called {to}; use one of {', '.join(self.agents)} or 'auto'"}
                if to == (src or {}).get("name"):
                    return {"error": "hand off to someone else"}
                if to in self.agents and self.agents[to].get("off"):
                    return {"error": f"{to} is turned off; hand off to someone else or 'auto'"}
                parent = self._task(src["task"]) if src and src.get("task") else None
                depth = (parent or {}).get("depth", 0) + 1
                if depth > MAX_CHAIN:
                    return {"error": f"hand-off chain is already {MAX_CHAIN} deep; finish and summarise instead"}
                who = None if to.lower() == "auto" else to
                tag = p.get("tag") if p.get("tag") in self._all_tags() else (self.agents[who]["tags"] or ["feature"])[0] if who else "feature"
                details = (p.get("details") or "").strip()
                if src and src.get("branch"):
                    details += f"\n\n---\nHanded off by {src['name']} from #{(parent or {}).get('id', '?')} ({src['branch']}). Merge that branch first if you need its changes: `git merge {src['branch']}`."
                nt = {"id": self.next_id, "title": title[:200], "details": details[:8000], "tag": tag, "col": "backlog",
                      "who": who, "pinned": who, "created": now(), "depth": depth,
                      "handoff": {"from": (src or {}).get("name", p.get("from") or "boss"), "task": (parent or {}).get("id")}}
                self.next_id += 1
                self.tasks.append(nt)
                self.event("handoff", f"↪ {nt['handoff']['from']} → {who or 'auto (' + tag + ')'}: #{nt['id']} {nt['title']}", who)
                self.touch()
                return {"ok": True, "task": nt["id"]}
            elif action == "decide":  # an agent gates its own running task: continue or stop the work here
                return workflows.runs.decide(self, p)
            elif action == "kill" and a:
                agents.launch.kill(self, a, p)
            elif action == "release" and a:
                if res := agents.launch.release(self, a, p):
                    return res
            elif action == "clear_chat" and a:
                if res := agents.launch.clear_chat(self, a, p):
                    return res
            elif action == "reorder_agents":
                agents.record.reorder(self, p)
            elif action == "respawn" and a:
                agents.launch.respawn(self, a, p)
            elif action in UI_TASK_ACTIONS and t:  # UI only: not in MCP_ACTIONS
                res = getattr(self, UI_TASK_ACTIONS[action])(t)
                self.touch()
                return res
            elif action == "address_review" and t:  # UI only: the PR's agent runs /address-review in the task's session
                w = self.agents.get(t.get("who") or "")
                pr = t.get("pr") or (t.get("prs") or [None])[-1]
                if not pr:
                    return {"error": f"#{t['id']} has no PR"}
                if not w:
                    return {"error": "the agent that opened this PR is gone"}
                if w["status"] == "running":
                    return {"error": f"{w['name']} is working: try again when it's done"}
                if w["status"] == "killed":
                    return {"error": f"{w['name']} is killed; respawn it first"}
                self._task_session(w, t)
                if not self._start(w):
                    return {"error": w["error"]}
                text = f"/address-review {pr}"
                self.log(w, "you", text)
                if not self._send(w, text):
                    w.update(status="error", activity="")
                    return {"error": w["error"]}
                w.update(status="running", since=now(), activity="addressing PR review…", error=None, addressing=t["id"])
                t["reviewing"] = now()
                self.event("tell", f"→ {w['name']}: /address-review on #{t['id']}", w["name"])
            elif action == "reject" and t and t["col"] == "review":
                w = self.agents.get(t["who"])
                fb = (p.get("text") or "Please revise.").strip()
                if self._over_task(t):
                    return {"error": f"#{t['id']} reached its budget (${t.get('cost') or 0:.2f} of ${self._budget('budget_task_usd'):.2f}): raise the task budget to send it back"}
                if w and w["status"] != "killed":
                    self._task_session(w, t)
                if not w or w["status"] == "killed" or not self._start(w):
                    t["col"] = "backlog"
                    return {"error": "agent unavailable; task returned to backlog"}
                t["col"] = "doing"
                t["started"] = now()
                w.update(status="running", task=t["id"], since=now(), activity="revising…")
                msg = f"Review feedback on task #{t['id']}: {fb}\nPlease address it, then summarise again."
                self.log(w, "you", msg)
                if not self._send(w, msg):
                    t["col"] = "review"
                    w.update(status="error", task=None, activity="")
                    return {"error": w["error"]}
                self.event("reject", f"#{t['id']} sent back to {w['name']}", w["name"])
            elif action in ("trigger_add", "trigger_update"):
                if (trig := triggers.get(p.get("kind"))) and trig.ui_only and via != "ui":
                    return {"error": trig.ui_only}
                tr = self._trigger(p.get("id")) if action == "trigger_update" else None
                if action == "trigger_update" and not tr:
                    return {"error": "unknown trigger"}
                if tr and set(p) <= {"id", "enabled"}:  # on/off switch
                    tr["enabled"] = bool(p.get("enabled"))
                else:
                    err, clean = self._clean_trigger(p, tr)
                    if err:
                        return {"error": err}
                    if tr:
                        for k in [k for k in tr if k.startswith("_")] + ["minute", "checked"] + ([] if (trig := triggers.get(tr["kind"])) and trig.keeps_seen else ["seen"]):
                            tr.pop(k, None)  # new settings, new baseline
                        tr.update(clean)
                    else:
                        tr = clean | {"id": f"t{self.next_trig}", "fired": 0, "last": None, "created": now()}
                        self.next_trig += 1
                        self.triggers.append(tr)
                tr["next"] = trig.schedule_next(tr) if tr["enabled"] and (trig := triggers.get(tr["kind"])) else None
                self.event("trigger", f"trigger {tr['name']} {'on' if tr['enabled'] else 'off'}")
            elif action == "trigger_delete" and (tr := self._trigger(p.get("id"))):
                self.triggers.remove(tr)
            elif action == "trigger_run" and (tr := self._trigger(p.get("id"))) and (trig := triggers.get(tr["kind"])) and trig.check_on_run:
                tr["_checked"] = 0  # "Run" on an ADO, Teams or pipeline trigger means: check now
            elif action == "trigger_run" and (tr := self._trigger(p.get("id"))):
                self._fire(tr, {"payload": "(run by hand)", "output": "(run by hand)", "files": "(run by hand)", "exit": 0},
                           "run by you")
            elif action == "wf_save":
                return workflows.definitions.save(self, p)
            elif action == "wf_toggle" and (w := self._workflow(p.get("id"))):
                w["off"] = bool(p.get("off"))
                if not w["off"]:
                    w.pop("off")
                self._save_config()
                self.event("config", f"⛓ {w['name']} turned {'off: no new runs' if p.get('off') else 'on'}")
            elif action == "wf_delete" and self._workflow(p.get("id")):
                workflows.definitions.delete(self, p)
            elif action == "wf_run" and (w := self._workflow(p.get("id"))):
                if res := workflows.definitions.run(self, p):
                    return res
            elif action == "run_stop" and (run := self._run(p.get("id"))) and run["status"] == "running":
                workflows.runs.stop(self, run)
            elif action == "run_delete" and (run := self._run(p.get("id"))) and run["status"] != "running":
                workflows.runs.delete(self, run)
            elif action == "resume" and t and t.get("hold"):
                t.pop("hold")
            elif action == "resume" and a and a["status"] == "paused":
                a["status"] = "idle"
                self.log(a, "sys", "resumed")
            elif action == "resume_all":
                for ag in self.agents.values():
                    if ag["status"] == "paused":
                        ag["status"] = "idle"
            elif action == "boss_model":
                if p.get("model") and p["model"] not in MODELS:
                    return {"error": "unknown model"}
                self.boss["model"] = p.get("model") or None
                self.config["boss"] = {"model": self.boss["model"], "effort": self.boss["effort"]}
                self._save_config()
                if self.boss["status"] != "running":
                    self._kill_proc("boss")
            elif action == "denial_dismiss" and a:  # UI only
                before = len(a["denials"])
                a["denials"] = [x for x in a["denials"] if x["id"] != p.get("id")]
                if len(a["denials"]) == before:
                    return {"error": "that blocked call is no longer in the inbox"}
            elif action == "denial_allow" and a:  # UI only
                res = self._denial_allow(a, p)
                if "error" in res:
                    return res
                self.touch()
                return res
            elif action == "budgets":  # UI only
                for k, f in (("task_usd", "budget_task_usd"), ("day_usd", "budget_day_usd")):
                    if k in p:
                        try:
                            v = float(p[k] or 0)
                        except (TypeError, ValueError):
                            return {"error": "budgets are dollar amounts; 0 turns one off"}
                        if not 0 <= v <= 1_000_000:
                            return {"error": "budgets are dollar amounts; 0 turns one off"}
                        self.config[f] = round(v, 2)
                self._save_config()
                self._budget_refresh()
                self.event("config", "budgets · task " + (f"${self._budget('budget_task_usd'):.2f}" if self._budget("budget_task_usd") else "off")
                           + " · day " + (f"${self._budget('budget_day_usd'):.2f}" if self._budget("budget_day_usd") else "off"))
            else:
                return {"error": f"bad action {action}"}
        self.touch()
        return {"ok": True}

    def shutdown(self) -> None:
        for n in list(self.procs):  # includes the boss
            self._kill_proc(n, wait=True)


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
        new.reap_orphans()
        new.version = old.version + 1
        CREW = new
        remember_project(target)
        with old.lock:  # wake streams parked on the old crew so they move to the new one
            old.version += 1
            old.changed.notify_all()
        new.touch()
    return {"ok": True}


def title_problem(title) -> str | None:
    """Titles from agents and the boss: short enough to read on a card at a glance."""
    t = " ".join(str(title or "").split())
    if t and (len(t.split(" ")) > 9 or len(t) > 80 or re.search(r"https?://", t)):
        return ("title too long: use about 6 plain words saying what to do, no links (AB#123 is fine), "
                "and put the rest in details")
    return None


PORT = 8770
FORM_ROUTES = {"/api/ado": "ado", "/api/teams": "teams"}  # the paths index.html posts to; /api/trigger/<kind> is the general form
# Per-run secrets for POST /api/act. The page gets UI_TOKEN; only crew_mcp's environment gets MCP_TOKEN, and that
# tier may call just the actions crew_mcp.py uses (nothing that runs shell commands or changes mode, project or MCP trust).
UI_TOKEN = secrets.token_urlsafe(24)
MCP_TOKEN = secrets.token_urlsafe(24)
GIT_ACTIONS = ("approve", "unapprove", "merge", "open_pr", "diff")  # UI only; each runs git outside the lock (_git_action)
UI_TASK_ACTIONS = {"merge_rebase": "_merge_rebase"}
MCP_ACTIONS = {"handoff", "add", "assign", "tell", "stop", "wf_run", "add_agent", "configure", "wf_save", "trigger_add",
               "alert", "alert_resolve", "decide"}
LAN_KEY: str | None = None   # set when serving beyond this computer; every non-local request must carry it
LOCAL_HOSTS = ("127.0.0.1", "localhost", "[::1]")
MAX_BODY = 1_000_000


def lan_ips() -> list[str]:
    ips = []
    try:  # the address of the default route; no packet is sent
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sk:
            sk.connect(("10.255.255.255", 1))
            ips.append(sk.getsockname()[0])
    except OSError:
        pass
    try:
        ips += [ip for ip in socket.gethostbyname_ex(socket.gethostname())[2] if ip not in ips]
    except OSError:
        pass
    return [ip for ip in ips if not ip.startswith("127.")]


def lan_key() -> str:
    """One key per machine, kept across restarts so a phone stays signed in."""
    f = WORK_ROOT / "lan-key"
    try:
        if len(k := f.read_text(encoding="utf-8").strip()) >= 20:
            return k
    except OSError:
        pass
    k = secrets.token_urlsafe(24)
    WORK_ROOT.mkdir(parents=True, exist_ok=True)
    f.write_text(k, encoding="utf-8")
    return k


class Handler(BaseHTTPRequestHandler):
    server_version = "crew/1"

    def log_message(self, *args):  # quiet
        pass

    def _local(self) -> bool:
        host = (self.headers.get("Host") or "").rsplit(":", 1)[0]
        return host in LOCAL_HOSTS and self.client_address[0] in ("127.0.0.1", "::1")

    def _host_ok(self) -> bool:
        """Local requests always pass. LAN requests need a bare-IP Host (no DNS rebinding) and the key cookie."""
        if self._local():
            return True
        if not LAN_KEY:
            return False
        host = (self.headers.get("Host") or "").rsplit(":", 1)[0]
        if not re.fullmatch(r"\d{1,3}(\.\d{1,3}){3}", host) and host.lower() not in (socket.gethostname().lower(), socket.gethostname().lower() + ".local"):
            return False
        cookie = next((c.split("=", 1)[1] for c in (self.headers.get("Cookie") or "").split("; ") if c.startswith("crew_key=")), "")
        return hmac.compare_digest(cookie, LAN_KEY)

    def _login(self) -> bool:
        """/?key=<key> from the LAN: set the cookie and serve the app in the same response.
        No redirect: a link opened from a camera or QR app is a cross-site navigation, and browsers
        drop cookies on the redirect that follows it. Lax (not Strict) for the same reason."""
        u = urlsplit(self.path)
        key = (parse_qs(u.query).get("key") or [""])[0]
        if not (LAN_KEY and key and hmac.compare_digest(key, LAN_KEY)):
            return False
        self._page(cookie=f"crew_key={LAN_KEY}; Path=/; Max-Age=31536000; HttpOnly; SameSite=Lax")
        return True

    def _tier(self) -> str | None:
        """Who is calling, by the per-run token header: 'ui' (the served page), 'mcp' (crew_mcp), or None."""
        tok = (self.headers.get("X-Crew-Auth") or "").encode("utf-8", "replace")
        if hmac.compare_digest(tok, UI_TOKEN.encode()):
            return "ui"
        return "mcp" if hmac.compare_digest(tok, MCP_TOKEN.encode()) else None

    def _read_body(self, required: bool = False) -> bytes | None:
        """The request body, or None after answering 400 (bad Content-Length) or 413 (over MAX_BODY)."""
        raw = self.headers.get("Content-Length")
        if raw is None and not required:
            return b""
        if raw is None or not raw.isdigit():
            self._json({"error": "bad or missing Content-Length"}, 400)
            return None
        if int(raw) > MAX_BODY:
            self._json({"error": f"body too large (max {MAX_BODY} bytes)"}, 413)
            return None
        return self.rfile.read(int(raw))

    def _page(self, cookie: str | None = None):
        body = (APP_DIR / "index.html").read_bytes().replace(b"__CREW_TOKEN__", UI_TOKEN.encode()).replace(
            b"__CREW_LAN__", b"0" if self._local() else b"1")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Referrer-Policy", "no-referrer")  # never leak ?key= to other sites
        if cookie:
            self.send_header("Set-Cookie", cookie)
        self.end_headers()
        self.wfile.write(body)

    def _denied(self):
        body = ("<!doctype html><meta name=viewport content='width=device-width,initial-scale=1'><title>Crew</title>"
                "<body style='background:#15161b;color:#ececf1;font:16px system-ui;padding:32px;line-height:1.5'>"
                "<h2 style='margin:0 0 12px'>Crew needs a key</h2><p style='color:#a4a5b2'>On your computer, click <b>Phone</b> "
                "in Crew's header and scan the QR code, or open the full link with <code>?key=</code>. "
                "It signs this browser in for a year.</p></body>").encode()
        self.send_response(403)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj, code=200):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if urlsplit(self.path).path == "/" and "key=" in self.path and self._login():
            return
        if not self._host_ok():
            return self._denied() if urlsplit(self.path).path in ("/", "/index.html") else self.send_error(HTTPStatus.FORBIDDEN)
        if self.path == "/api/lan":  # only the computer itself may read the key
            if not self._local():
                return self.send_error(HTTPStatus.FORBIDDEN)
            port = self.server.server_address[1]
            return self._json({"enabled": bool(LAN_KEY), "urls": [f"http://{ip}:{port}/?key={LAN_KEY}" for ip in lan_ips()] if LAN_KEY else []})
        if urlsplit(self.path).path in ("/", "/index.html"):
            self._page()
        elif self.path == "/api/state":
            self._json(CREW.snapshot())
        elif urlsplit(self.path).path == "/api/commands":  # the / picker: what this agent's session can run
            who = (parse_qs(urlsplit(self.path).query).get("agent") or [""])[0]
            c = CREW
            with c.lock:
                a = c.boss if who == "boss" else c.agents.get(who)
                if not a:
                    return self._json({"error": "no such agent"}, 404)
                names, skills = list(a.get("commands") or []), list(a.get("skill_names") or [])
            boss = who == "boss"  # the boss runs with slash commands off: only the crew's own
            self._json({"boss": boss, "live": bool(names), "commands": [] if boss else names,
                        "skills": [] if boss else skills, "catalog": {} if boss else command_catalog(c.project)})
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

    def _drain(self) -> None:
        """Read an unwanted body before refusing: closing on unread data resets the client's connection (Windows)."""
        try:
            n = int(self.headers.get("Content-Length") or 0)
            if 0 < n <= MAX_BODY:
                self.rfile.read(n)
        except (ValueError, OSError):
            pass

    def do_POST(self):
        if self.path.startswith("/api/hook/"):  # webhook triggers: a per-trigger token, no browser header needed
            u = urlsplit(self.path)
            token = self.headers.get("X-Crew-Token") or (parse_qs(u.query).get("token") or [""])[0]
            if (raw := self._read_body(required=True)) is None:
                return
            body = raw.decode("utf-8", "replace")
            res = CREW.hook(u.path.rsplit("/", 1)[1], token, body)
            return self._json(res, 403 if "error" in res else 200)
        # Custom header forces a CORS preflight, which this server never answers: other sites can't post here.
        if not self._host_ok() or self.headers.get("X-Crew") != "1":
            self._drain()
            return self.send_error(HTTPStatus.FORBIDDEN)
        tier = self._tier()
        if not tier:
            self._drain()
            return self._json({"error": "missing or bad X-Crew-Auth token: reload the page"}, 403)
        if (raw := self._read_body()) is None:
            return
        try:
            payload = json.loads(raw or b"{}")
        except json.JSONDecodeError:
            return self._json({"error": "bad json"}, 400)
        if not isinstance(payload, dict):
            return self._json({"error": "bad json"}, 400)
        if kind := FORM_ROUTES.get(self.path) or (self.path.removeprefix("/api/trigger/") if self.path.startswith("/api/trigger/") else ""):
            # a trigger form's helper calls (defaults, the Test button): UI only, read-only, starts nothing
            if tier != "ui":
                return self._json({"error": "not allowed for this caller"}, 403)
            if (res := trig.form(CREW, payload) if (trig := triggers.get(kind)) else None) is None:
                return self.send_error(HTTPStatus.NOT_FOUND)
        elif self.path in ("/api/browse", "/api/project"):
            if tier != "ui":
                return self._json({"error": "not allowed for this caller"}, 403)
            if not self._local():
                return self._json({"error": "browsing folders and switching projects works only on the computer running Crew"}, 403)
            res = browse(payload.get("path") or "") if self.path == "/api/browse" else switch_project(payload.get("path") or "")
        elif self.path.startswith("/api/act/"):
            action = self.path.rsplit("/", 1)[1]
            if tier == "mcp" and action not in MCP_ACTIONS:
                return self._json({"error": f"{action} is not available to agents or the boss"}, 403)
            res = CREW.act(action, payload, via=tier)
        else:
            return self.send_error(HTTPStatus.NOT_FOUND)
        self._json(res, 400 if "error" in res else 200)


DEMO_TASKS = [
    ("Add rate limiting to the login endpoint", "api"),
    ("Plan the move from session cookies to JWT", "plan"),
    ("Compare background job queues for email sending", "research"),
    ("Review the auth module for security issues", "review"),
    ("Build a dark settings page with a theme toggle", "ui"),
    ("Evaluate Haiku vs Sonnet on the support-ticket classifier", "eval"),
    ("Check last night's failed deploy", "incident"),
    ("Document the new login limits in the README", "docs"),
    ("Fix flaky date parsing on the invoices page", "bug"),
]


DEMO_TRIGGERS = [
    {"name": "Daily standup", "kind": "schedule", "spec": {"cron": "0 11 * * 1-5"},
     "task": {"title": "Standup report for {date}", "tag": "report", "agent": "scribe",
              "details": "Summarise yesterday's merged PRs, what is in review, and anything blocked."}},
    {"name": "Test what ships", "kind": "event", "spec": {"on": "done", "tag": "api"},
     "task": {"title": "Write tests for #{id}: {title}", "tag": "test", "agent": "tester",
              "details": "{who} finished this on {branch} (commit {commit}). Run `{merge}` in your worktree first.\n\nTheir summary:\n{summary}"}},
    {"name": "Docs changed", "kind": "watch", "spec": {"glob": "**/*.md", "every": 10, "cooldown": 120},
     "task": {"title": "Proofread changed docs", "tag": "docs", "agent": "scribe", "details": "Changed files:\n{files}"}},
]


def init_repo(project: Path, files: dict[str, str]) -> None:
    project.mkdir(parents=True)
    for rel, body in files.items():
        (project / rel).parent.mkdir(parents=True, exist_ok=True)
        (project / rel).write_text(body, encoding="utf-8")
    git(project, "init", "-q")
    git(project, "add", "-A")
    git(project, "-c", "user.name=crew", "-c", "user.email=crew@localhost", "commit", "-qm", "init")


class KeepAwake:
    """Keep the computer from idle-sleeping while the crew has work in flight, like the Claude desktop app.
    A locked screen never pauses Crew; sleep does. The display may still turn off, and a closed lid still sleeps.
    Windows: SetThreadExecutionState (always called from the dispatcher thread). macOS: caffeinate. Linux: systemd-inhibit."""

    def __init__(self, mode: str):
        self.mode, self.on, self.proc = mode, False, None

    def set(self, want: bool) -> None:
        want = want and self.mode != "off"
        if want == self.on:
            return
        self.on = want
        try:
            if IS_WIN:
                import ctypes
                es_continuous, es_system_required = 0x80000000, 0x00000001
                ctypes.windll.kernel32.SetThreadExecutionState(es_continuous | (es_system_required if want else 0))
            elif want:
                cmd = (["caffeinate", "-i", "-w", str(os.getpid())] if sys.platform == "darwin" else
                       ["systemd-inhibit", "--what=idle:sleep", "--who=crew", "--why=agents are working", "sleep", "infinity"])
                if shutil.which(cmd[0]):
                    self.proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            elif self.proc:
                self.proc.terminate()
                self.proc = None
        except Exception as e:  # never let power management take the dispatcher down
            print("keep-awake:", e, file=sys.stderr)


def main() -> None:
    global CREW
    ap = argparse.ArgumentParser(description="Local web console for a crew of Claude Code agents")
    ap.add_argument("--project", help="repo the agents work on (default: a demo git repo)")
    ap.add_argument("--port", type=int, default=8770)
    ap.add_argument("--config", help="crew config (default: crew.json next to server.py, or in the current folder when installed)")
    ap.add_argument("--demo", action="store_true", help="simulated agents with sample tasks: no Claude CLI, no tokens")
    ap.add_argument("--lan", action="store_true", help="also serve other devices on your network (phone, tablet), behind a key")
    ap.add_argument("--awake", choices=("busy", "always", "off"), default="busy",
                    help="keep the computer from sleeping: while agents work (default), always (so schedules fire), or off")
    ap.add_argument("--version", action="version", version=f"crew {__version__}")
    args = ap.parse_args()

    if args.demo:
        project = Path(args.project or WORK_ROOT / "demo-sim").resolve()
        cfg_path = Path(args.config or WORK_ROOT / "demo-crew.json")
        claude = [sys.executable, str(APP_DIR / "demo" / "fake_claude.py")]
    else:
        project = Path(args.project or WORK_ROOT / "demo-project").resolve()
        cfg_path = Path(args.config or (APP_DIR if IS_CLONE else Path.cwd()) / "crew.json")
        # CREW_CLAUDE overrides the binary, e.g. "python demo/fake_claude.py"
        claude = shlex.split(os.environ["CREW_CLAUDE"], posix=False) if os.environ.get("CREW_CLAUDE") else [shutil.which("claude") or ""]
        if not claude[0]:
            sys.exit("claude CLI not found on PATH. Install Claude Code, or try `python server.py --demo`.")

    if not cfg_path.exists():
        cfg = json.loads(json.dumps(DEFAULT_CONFIG))
        cfg_path.parent.mkdir(parents=True, exist_ok=True)
        write_atomic(cfg_path, json.dumps(cfg, indent=2))
    try:
        saved = load_json_file(cfg_path)
    except ValueError as e:
        bak = cfg_path.with_name(cfg_path.name + ".bak")
        try:
            saved = load_json_file(bak)
        except ValueError:
            sys.exit(f"config file {cfg_path} is not valid JSON ({e}). Fix it, or delete it to start from the defaults.")
        print(f"crew: {cfg_path} is not valid JSON ({e}); using {bak.name}", file=sys.stderr)
    config = DEFAULT_CONFIG | {"workflows": DEFAULT_WORKFLOWS} | saved
    config.setdefault("workflows", DEFAULT_WORKFLOWS)

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
    CREW.demo = args.demo
    CREW.reap_orphans()
    remember_project(project)
    if args.demo and not CREW.tasks:
        for title, tag in DEMO_TASKS:
            CREW.act("add", {"title": title, "tag": tag})
        for tr in DEMO_TRIGGERS:
            CREW.act("trigger_add", tr)

    awake = KeepAwake(args.awake)

    def loop():
        while True:
            try:
                CREW.tick_triggers()
                CREW.dispatch()
                busy = args.awake == "always" or CREW.busy()
                if busy != awake.on:
                    awake.set(busy)
                    CREW.awake = awake.on
                    CREW.touch()
            except Exception as e:  # keep the dispatcher alive
                print("dispatch error:", e, file=sys.stderr)
            time.sleep(1)

    threading.Thread(target=loop, daemon=True).start()
    global LAN_KEY, PORT
    PORT = args.port
    if args.lan:
        LAN_KEY = lan_key()
    srv = ThreadingHTTPServer(("0.0.0.0" if args.lan else "127.0.0.1", args.port), Handler)
    srv.daemon_threads = True
    print(f"crew{' (demo)' if args.demo else ''} · project {project} · http://127.0.0.1:{args.port}", flush=True)
    for ip in lan_ips() if args.lan else []:
        print(f"  on your network: http://{ip}:{args.port}/?key={LAN_KEY}", flush=True)
    try:
        srv.serve_forever()
    finally:
        CREW.shutdown()
        awake.set(False)


if __name__ == "__main__":
    main()
