"""Crew: a local web console for a team of Claude Code agents.

Each agent is a real `claude -p` process speaking stream-json, running in its own
git worktree of the target project. Standard library only.

    python server.py --project /path/to/repo     # then open http://127.0.0.1:8770
    python server.py --demo                      # simulated agents, no Claude CLI or tokens
"""
from __future__ import annotations

import argparse
import functools
import hashlib
import hmac
import json
import re
import os
import secrets
import shlex
import socket
import shutil
import signal
import subprocess
import sys
import threading
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

APP_DIR = Path(__file__).resolve().parent
WORK_ROOT = Path.home() / ".crew"  # short path: Windows git chokes on long worktree paths
IS_WIN = os.name == "nt"

# One agent per stage of the delivery loop: plan -> research -> design/build -> test -> review
# -> evaluate -> ship/observe -> report. Tags route unassigned tasks; each tag belongs to one agent.
DEFAULT_CONFIG = {
    "permission_mode": "acceptEdits",
    "context": "fresh",
    "allowed_tools": ["Bash(git status:*)", "Bash(git diff:*)", "Bash(git log:*)", "Bash(git show:*)", "Bash(git merge:*)",
                      "Bash(ls:*)", "Bash(npm test:*)", "Bash(pytest:*)", "Bash(python -m pytest:*)", "WebSearch", "WebFetch"],
    "agents": [
        {"name": "planner", "tags": ["plan", "triage"], "model": "opus", "effort": "high",
         "role": "Turns requests and work items into small, testable tasks with acceptance criteria. Reads the code first. "
                 "Writes the plan to plans/<slug>.md and lists the follow-up tasks it recommends, each with a tag. Does not implement."},
        {"name": "researcher", "tags": ["research", "spike"], "model": "fable", "effort": "high",
         "role": "Investigates tools, libraries and approaches. Compares options with evidence, cost and risk, and ends with one "
                 "clear recommendation. Writes findings to notes/<slug>.md."},
        {"name": "designer", "tags": ["ui", "design"], "model": "opus", "effort": "high",
         "role": "Designs and builds UI: prototypes, components, dark-theme polish, accessibility and responsive layout. "
                 "Ships working HTML/CSS/JS, checks it at phone width, and never leaves placeholder styling."},
        {"name": "builder", "tags": ["feature", "bug", "api", "refactor"], "model": "opus", "effort": "high",
         "role": "Implements features and fixes in application code. Makes the smallest change that meets the acceptance criteria, "
                 "matches the surrounding code, and adds tests for new behaviour. Does not commit; Crew commits on approval."},
        {"name": "tester", "tags": ["test", "qa"], "model": "sonnet", "effort": "medium",
         "role": "Writes and runs tests. Reproduces a bug with a failing test before anyone fixes it. Reports failures with the exact "
                 "command, output and file:line. When a task names another agent's branch, merges it into this worktree first."},
        {"name": "reviewer", "tags": ["review", "security"], "model": "opus", "effort": "xhigh",
         "role": "Reviews changes like a senior engineer: correctness, security, data and domain correctness, and clarity. "
                 "Fixes only clear defects; reports everything else as findings ranked by severity, each with file:line."},
        {"name": "evaluator", "tags": ["eval", "data", "ml"], "model": "opus", "effort": "high",
         "role": "Runs model and prompt evaluations: datasets, A/B and shadow comparisons, regression checks. Reports numbers with "
                 "sample sizes and spread, never anecdotes, and says plainly when the data is too thin to decide."},
        {"name": "ops", "tags": ["ops", "deploy", "incident", "chore"], "model": "sonnet", "effort": "high",
         "role": "Watches delivery and production: CI/CD runs, deployments, telemetry, cost and incidents, plus repo hygiene "
                 "(stale branches, worktrees, dependencies). Diagnoses from logs and queries first; changes infrastructure "
                 "only when the task says so."},
        {"name": "scribe", "tags": ["docs", "report"], "model": "sonnet", "effort": "low",
         "role": "Writes for people: daily standup reports, PR descriptions, release notes, work item comments and docs. "
                 "Short, factual, linked to the PR or work item, no filler."},
    ],
}

DEFAULT_WORKFLOWS = [
    {"id": "w1", "name": "Feature delivery", "description": "Plan it, build it, test it, review it, write it up.",
     "steps": [{"agent": "planner", "title": "Plan: {input}", "details": "", "gate": "approve"},
               {"agent": "builder", "title": "Build: {input}", "details": "Implement the plan from step 1.", "gate": "approve"},
               {"agent": "tester", "title": "Test: {input}", "details": "Cover the new behaviour and its edge cases.", "gate": "auto"},
               {"agent": "reviewer", "title": "Review: {input}", "details": "Review the builder's and tester's commits.", "gate": "approve"},
               {"agent": "scribe", "title": "Write up: {input}", "details": "PR description and release note.", "gate": "auto"}]},
    {"id": "w2", "name": "Bug fix", "description": "Reproduce with a failing test, fix, review.",
     "steps": [{"agent": "tester", "title": "Reproduce: {input}", "details": "Write a failing test that shows the bug. Do not fix it.", "gate": "auto"},
               {"agent": "builder", "title": "Fix: {input}", "details": "Make the failing test from step 1 pass.", "gate": "approve"},
               {"agent": "reviewer", "title": "Review fix: {input}", "details": "", "gate": "approve"}]},
    {"id": "w3", "name": "Spike to plan", "description": "Research the options, then turn the pick into a plan.",
     "steps": [{"agent": "researcher", "title": "Research: {input}", "details": "", "gate": "approve"},
               {"agent": "planner", "title": "Plan: {input}", "details": "Plan around the recommendation from step 1.", "gate": "approve"}]},
    {"id": "w4", "name": "Model change", "description": "Baseline eval, change, re-eval against the baseline.",
     "steps": [{"agent": "evaluator", "title": "Baseline: {input}", "details": "Measure the current behaviour first.", "gate": "auto"},
               {"agent": "builder", "title": "Change: {input}", "details": "", "gate": "approve"},
               {"agent": "evaluator", "title": "Re-evaluate: {input}", "details": "Same dataset and settings as the baseline; report the delta.", "gate": "approve"}]},
    {"id": "w5", "name": "Incident", "description": "Diagnose, fix, verify, post-mortem.",
     "steps": [{"agent": "ops", "title": "Diagnose: {input}", "details": "Find the root cause. Change nothing yet.", "gate": "approve"},
               {"agent": "builder", "title": "Fix: {input}", "details": "", "gate": "approve"},
               {"agent": "tester", "title": "Verify fix: {input}", "details": "", "gate": "auto"},
               {"agent": "scribe", "title": "Post-mortem: {input}", "details": "What broke, why, the fix, and how we'd catch it sooner.", "gate": "auto"}]},
]

TAGS = ["plan", "research", "ui", "feature", "bug", "api", "test", "review", "eval", "ops", "incident", "docs", "report", "chore"]
# acceptEdits: edits auto-approved, other tools only via allowed_tools.
# auto: Claude Code's classifier approves safe actions. bypassPermissions: nothing is checked.
MODES = ["acceptEdits", "auto", "bypassPermissions"]
CONTEXTS = ["fresh", "keep"]
MODELS = ["fable", "opus", "sonnet", "haiku"]          # aliases the CLI resolves to the latest of each family
EFFORTS = ["low", "medium", "high", "xhigh", "max"]
NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9-]{0,23}")
RESERVED = {"boss", "you", "anyone", "auto"}


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


# Claude Code files that git doesn't carry into a worktree (ignored or never committed) but that
# shape a session: local settings, project skills/agents/commands, project MCP servers, local memory.
CLAUDE_LOCAL = [".claude/settings.local.json", ".claude/skills", ".claude/agents", ".claude/commands",
                ".claude/rules", ".mcp.json", "CLAUDE.local.md"]


def sync_claude_files(src: Path, dst: Path) -> list[str]:
    """Copy the project's untracked Claude Code files into an agent's worktree. Tracked files come with git."""
    tracked = set(git(src, "ls-files", "--", *CLAUDE_LOCAL).stdout.split("\n"))
    copied = []
    for rel in CLAUDE_LOCAL:
        s_path = src / rel
        files = [s_path] if s_path.is_file() else [f for f in s_path.rglob("*") if f.is_file()] if s_path.is_dir() else []
        for f in files:
            r = f.relative_to(src).as_posix()
            if r in tracked:
                continue
            d = dst / r
            try:
                if d.exists() and d.stat().st_mtime >= f.stat().st_mtime and d.stat().st_size == f.stat().st_size:
                    continue
                d.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(f, d)
                copied.append(r)
            except OSError:
                pass
    return copied


def local_mcp_servers(project: Path) -> dict:
    """MCP servers added with `claude mcp add` (local scope) for the project or a folder above it.
    Claude Code keys these by the folder it was started in, so an agent in a worktree would miss them."""
    try:
        cfg = json.loads((Path.home() / ".claude.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    want = [project, *project.parents]
    servers: dict = {}
    for key, entry in (cfg.get("projects") or {}).items():
        try:
            k = Path(key).resolve()
        except OSError:
            continue
        if k in want:
            for name, spec in (entry.get("mcpServers") or {}).items():
                servers.setdefault(name, spec)  # nearest folder wins: `want` starts at the project
    mcp = project / ".mcp.json"
    if mcp.is_file():  # project-scope servers, whether or not .mcp.json is committed
        try:
            for name, spec in (json.loads(mcp.read_text(encoding="utf-8")).get("mcpServers") or {}).items():
                servers.setdefault(name, spec)
        except ValueError:
            pass
    return servers


# ---- triggers: cron, templates ---------------------------------------------------
TRIGGER_KINDS = ["schedule", "event", "watch", "poll", "webhook"]
EVENT_ON = ["review", "done", "error"]
POLL_WHEN = ["change", "fail", "output"]
MAX_CHAIN = 5  # an event trigger never fires on a task that is already this many triggers deep


def _cron_field(field: str, lo: int, hi: int) -> frozenset:
    vals = set()
    for part in field.split(","):
        rng, _, st = part.partition("/")
        step = int(st) if st else 1
        if rng == "*":
            a, b = lo, hi
        elif "-" in rng:
            a, b = (int(x) for x in rng.split("-", 1))
        else:
            a = int(rng)
            b = hi if st else a
        if step < 1 or a < lo or b > hi or a > b:
            raise ValueError(f"'{part}' is out of range {lo}-{hi}")
        vals.update(range(a, b + 1, step))
    return frozenset(vals)


@functools.lru_cache(maxsize=256)
def parse_cron(expr: str) -> tuple:
    f = expr.split()
    if len(f) != 5:
        raise ValueError("use 5 fields: minute hour day-of-month month weekday")
    dow = frozenset(d % 7 for d in _cron_field(f[4], 0, 7))
    return (_cron_field(f[0], 0, 59), _cron_field(f[1], 0, 23), _cron_field(f[2], 1, 31), _cron_field(f[3], 1, 12), dow,
            not f[2].startswith("*"), not f[4].startswith("*"))


def cron_error(expr: str) -> str | None:
    try:
        parse_cron(expr)
        return None
    except ValueError as e:
        return f"bad cron: {e}"


def cron_match(expr: str, lt: time.struct_time) -> bool:
    mi, hr, dom, mon, dow, dom_set, dow_set = parse_cron(expr)
    if lt.tm_min not in mi or lt.tm_hour not in hr or lt.tm_mon not in mon:
        return False
    d_ok, w_ok = lt.tm_mday in dom, (lt.tm_wday + 1) % 7 in dow
    return (d_ok or w_ok) if (dom_set and dow_set) else (d_ok and w_ok)  # standard cron: either day field when both are set


def cron_next(expr: str, after: float) -> float | None:
    t = (int(after) // 60 + 1) * 60
    for _ in range(60 * 24 * 8):  # look a week ahead; rarer schedules show as "later"
        if cron_match(expr, time.localtime(t)):
            return float(t)
        t += 60
    return None


def fill(template: str, ctx: dict) -> str:
    """{name} placeholders from ctx; unknown ones are left as they are."""
    return re.sub(r"\{(\w+)\}", lambda m: str(ctx[m.group(1)]) if m.group(1) in ctx else m.group(0), template)


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
        self.triggers: list[dict] = []
        self.next_trig = 1
        self.runs: list[dict] = []
        self.next_run = 1
        self.agents: dict[str, dict] = {}
        for spec in config["agents"]:
            self.agents[spec["name"]] = self._new_agent(spec)
        self.boss = self._new_agent({"name": "boss", "role": "Runs the crew", "tags": [],
                                     "model": (config.get("boss") or {}).get("model") or "opus",
                                     "effort": (config.get("boss") or {}).get("effort"), "context": "keep"})
        self.boss["kind"] = "boss"
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
            # fresh: a new Claude session per task (send-back keeps it). keep: one session across tasks.
            "context": spec.get("context") if spec.get("context") in CONTEXTS else self.config.get("context", "fresh"),
            "last_task": None,
            "ctx": 0,            # tokens in the context window at the last model call
            "compacting": None,  # status to return to after /compact
        }

    def _attach_worktree(self, a: dict) -> None:
        wt = WORK_ROOT / "wt" / self.key / a["name"]
        if self.git and (wt / ".git").exists():
            a["cwd"], a["branch"] = str(wt), f"crew/{a['name']}"

    def _save_config(self) -> None:
        """Write the agent roster and mode back to crew.json, keeping any other keys."""
        self.config["agents"] = [{"name": a["name"], "role": a["role"], "tags": a["tags"], "model": a["model"],
                                  "effort": a["effort"], "context": a["context"]} for a in self.agents.values()]
        if not self.config_path:
            return
        saved = json.loads(self.config_path.read_text(encoding="utf-8")) if self.config_path.exists() else {}
        saved["agents"] = self.config["agents"]
        saved["permission_mode"] = self.config["permission_mode"]
        saved["workflows"] = self.config.get("workflows", [])
        if self.config.get("boss"):
            saved["boss"] = self.config["boss"]
        self.config_path.write_text(json.dumps(saved, indent=2), encoding="utf-8")

    def _launch_key(self, a: dict) -> tuple:
        # MCP servers found later are allowed from the agent's next start; no restart (that would drop the live context)
        return (self.config["permission_mode"], a["model"], a["effort"])

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
        self.triggers = data.get("triggers", [])
        self.runs = data.get("runs", [])[-50:]
        self.next_run = data.get("next_run", len(self.runs) + 1)
        self.next_trig = data.get("next_trig", len(self.triggers) + 1)
        for tr in self.triggers:
            tr["next"] = cron_next(tr["spec"]["cron"], now()) if tr["kind"] == "schedule" else None
        self.tasks = data.get("tasks", [])
        self.next_id = data.get("next_id", len(self.tasks) + 1)
        for t in self.tasks:  # nothing is running after a restart
            if t["col"] == "doing":
                t["col"] = "backlog"
            if t["col"] == "backlog" and t.get("who") and t["who"] not in self.agents:
                t["who"] = t["pinned"] = None  # its agent left the roster; let the tags route it
        if b := data.get("boss"):
            self.boss.update({k: b[k] for k in ("session", "cost", "log", "tokens", "ctx") if k in b})
        for name, saved in data.get("agents", {}).items():
            if name in self.agents:
                self.agents[name].update({k: saved[k] for k in ("session", "cost", "log", "tokens", "mcp", "skills", "skill_names", "plugins") if k in saved})
                if self.agents[name]["status"] != "idle":
                    self.agents[name]["status"] = "idle"

    def _save(self) -> None:
        data = {"project": str(self.project), "tasks": self.tasks, "next_id": self.next_id,
                "mcp_allow": sorted(self.mcp_allow), "triggers": self._public_triggers(), "next_trig": self.next_trig,
                "runs": self.runs[-50:], "next_run": self.next_run,
                "agents": {n: {"session": a["session"], "cost": a["cost"], "tokens": a["tokens"], "log": a["log"][-200:],
                           "mcp": a["mcp"], "skills": a["skills"], "skill_names": a.get("skill_names", []), "plugins": a.get("plugins", [])}
                       for n, a in self.agents.items()},
                "boss": {k: self.boss[k] for k in ("session", "cost", "tokens", "ctx")} | {"log": self.boss["log"][-200:]}}
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
            boss = {k: v for k, v in self.boss.items() if k != "log"} | {"log": self.boss["log"][-160:], "alive": self._alive("boss")}
            return {"version": self.version, "project": str(self.project), "git": self.git, "boss": boss,
                    "agents": agents, "tasks": self.tasks, "feed": self.feed[-30:],
                    "permission_mode": self.config["permission_mode"], "modes": MODES,
                    "models": MODELS, "efforts": EFFORTS, "tags": self._all_tags(),
                    "project_name": self.project.name, "mcp_allow": sorted(self.mcp_allow),
                    "allow_mcp": self.config.get("allow_mcp", True), "triggers": self._public_triggers(),
                    "trigger_kinds": TRIGGER_KINDS, "workflows": self.config.get("workflows", []),
                    "runs": self.runs[-30:], "contexts": CONTEXTS}

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
        boss = a.get("kind") == "boss"
        if not boss:
            self._ensure_worktree(a)
        team = ", ".join(f"{x['name']} ({', '.join(x['tags']) or 'no tags'})" for x in self.agents.values())
        if boss:
            role = (f"You are the boss of Crew, a team of Claude Code agents working on the project {self.project}. "
                    f"The crew: {team}. You do not write code yourself: you plan, delegate and coordinate through the crew "
                    "tools (crew_status, create_task, assign_task, message_agent, stop_agent, run_workflow, add_agent, "
                    "update_agent, save_workflow, add_trigger). Call crew_status before acting. Prefer existing agents and "
                    "workflows; add or change them only when the request needs it. Keep replies short: what you did, "
                    "task numbers, and what the user should approve next. Read files only to understand the project.")
        else:
            role = (f"You are '{a['name']}', one of {len(self.agents)} Claude Code agents working in parallel on this project. "
                    f"Your role: {a['role']} Teammates: {team}. "
                    f"Your working folder is {a['cwd']}" + (f", a git worktree of the project {self.project}" if a['branch'] else "") + ". "
                    "Stay inside it: do not read, create or change files outside this folder unless the task explicitly names a path outside it. "
                    "Never touch other agents' worktrees. "
                    + ("When your finished work clearly needs another specialist next, call the crew handoff tool once "
                       "(to a teammate's name, or 'auto' with a tag); say what you did and where. " if self.config.get("auto_handoff", True) else "")
                    + "When you finish a task, end with a short summary: what you changed, files touched, and anything left open.")
        mode = "acceptEdits" if boss else self.config["permission_mode"]
        cmd = [*self.claude, "-p", "--input-format", "stream-json", "--output-format", "stream-json", "--verbose",
               "--permission-mode", mode, "--append-system-prompt", role]
        if boss:  # coordinates, never edits: crew tools and read-only tools only
            cmd += ["--allowedTools", "mcp__crew", "Read", "Grep", "Glob", "WebSearch", "WebFetch", "Bash(git log:*)", "Bash(git status:*)",
                    "--disallowedTools", "Edit", "Write", "MultiEdit", "NotebookEdit"]
        elif mode == "bypassPermissions":
            cmd += ["--dangerously-skip-permissions"]
        else:
            allowed = list(self.config.get("allowed_tools") or []) + ["mcp__crew"]
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
        # Same setup as `claude` in a terminal: user-level skills, plugins, MCP servers and claude.ai
        # connectors load on their own; these add what is tied to the project folder.
        if a["branch"]:
            if copied := sync_claude_files(self.project, Path(a["cwd"])):
                self.log(a, "sys", f"synced {len(copied)} local Claude file{'s' if len(copied) != 1 else ''} into the worktree")
        servers = {} if boss else local_mcp_servers(self.project)
        servers["crew"] = {"type": "stdio", "command": sys.executable, "args": [str(APP_DIR / "crew_mcp.py")],
                           "env": {"CREW_URL": f"http://127.0.0.1:{PORT}", "CREW_AGENT": a["name"], "CREW_ROLE": "boss" if boss else "agent"}}
        f = WORK_ROOT / "mcp" / f"{self.key}-{a['name']}.json"
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

    def _agent(self, name) -> dict | None:
        return self.boss if name == "boss" else self.agents.get(name or "")

    def _kill_proc(self, name: str) -> None:
        if a := self._agent(name):
            a["subs"] = {}  # the tree kill ends its subagents too
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
        if t == "system" and st == "compact_boundary":
            meta = d.get("compact_metadata") or {}
            self.log(a, "sys", f"context compacted ({meta.get('trigger', 'manual')}, was {int(meta.get('pre_tokens') or a['ctx']) // 1000}k tokens)")
            a["ctx"] = 0
        if t == "system" and st == "init":
            a["mcp"] = [{"name": m.get("name"), "status": m.get("status")} for m in d.get("mcp_servers") or []]
            a["skills"] = len(d.get("skills") or [])
            a["skill_names"] = sorted(str(x if isinstance(x, str) else x.get("name", "")) for x in d.get("skills") or [])[:200]
            a["plugins"] = sorted(str(x if isinstance(x, str) else x.get("name", "")) for x in d.get("plugins") or [])[:100]
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
            a["subs"] = {}
            total = float(d.get("total_cost_usd") or 0)
            a["cost"] += max(0.0, total - a["proc_cost"])
            a["proc_cost"] = total
            if a.get("compacting"):  # a /compact turn: no task changes
                a.update(status=a["compacting"], compacting=None, activity="")
                self.log(a, "done", "compaction finished")
                return
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
                    self._task_event(task, "error", summary=a["error"])
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

    # ---- tasks -----------------------------------------------------------------
    def _task(self, tid) -> dict | None:
        return next((t for t in self.tasks if t["id"] == tid), None)

    def _begin(self, a: dict, t: dict) -> None:
        if a["context"] == "fresh" and a.get("last_task") not in (None, t["id"]) and (a["session"] or self._alive(a["name"])):
            self._kill_proc(a["name"])  # new task, new session: no paying to re-read old work
            a["session"] = None
            self.log(a, "sys", f"fresh context for #{t['id']}")
        if not self._start(a):
            return
        a["last_task"] = t["id"]
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
                t = next((t for t in self.tasks if t["col"] == "backlog" and not t.get("hold") and
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

    # ---- triggers -------------------------------------------------------------------
    # Assigning a task is the manual trigger. These add the automatic ones: each firing adds a
    # backlog task from the trigger's template, and the dispatcher hands it out like any other.
    def _public_triggers(self) -> list[dict]:
        return [{k: v for k, v in tr.items() if not k.startswith("_")} for tr in self.triggers]

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

    def _approve(self, t: dict, by: str = "you") -> None:
        """Commit the owner's worktree and move the task to Done. Caller holds the lock."""
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
        self.event("approve", f"{'auto-' if by != 'you' else ''}approved #{t['id']}" + (f" · commit {t.get('commit')} on {w['branch']}" if t.get("commit") else ""))
        self._task_event(t, "done")

    def _task_event(self, t: dict, col: str, summary: str | None = None) -> None:
        """Event triggers and workflow runs: a task reached review or done, or its agent hit an error."""
        self._wf_event(t, col, summary)
        w = self.agents.get(t.get("who") or "")
        for tr in self.triggers:
            sp = tr["spec"]
            if not tr["enabled"] or tr["kind"] != "event" or sp.get("on") != col:
                continue
            if (sp.get("tag") and sp["tag"] != t["tag"]) or (sp.get("agent") and sp["agent"] != t.get("who")):
                continue
            if (t.get("source") or {}).get("trigger") == tr["id"] or t.get("depth", 0) >= MAX_CHAIN:
                continue  # no self-loops, no runaway chains
            branch = w["branch"] if w and w.get("branch") else ""
            self._fire(tr, {"id": t["id"], "title": t["title"], "tag": t["tag"], "who": t.get("who") or "",
                            "summary": (summary if summary is not None else t.get("summary") or "")[:3000],
                            "branch": branch, "commit": t.get("commit") or "",
                            "merge": f"git merge {branch}" if branch and t.get("commit") else "",
                            "depth": t.get("depth", 0) + 1}, f"#{t['id']} reached {col}")

    def tick_triggers(self) -> None:
        """Schedules, file watches and command polls. Called every second by the dispatcher thread."""
        fired = False
        with self.lock:
            minute = int(now() // 60)
            lt = time.localtime()
            for tr in self.triggers:
                if not tr["enabled"]:
                    continue
                kind, sp = tr["kind"], tr["spec"]
                if kind == "schedule":
                    if tr.get("minute") != minute and cron_match(sp["cron"], lt):
                        tr["minute"] = minute
                        self._fire(tr, {}, "on schedule")
                        tr["next"] = cron_next(sp["cron"], now())
                        fired = True
                    elif tr.get("next") and tr["next"] < now() - 60:
                        tr["next"] = cron_next(sp["cron"], now())
                elif kind == "watch" and now() - tr.get("_checked", 0) >= sp["every"]:
                    tr["_checked"] = now()
                    fired |= self._check_watch(tr)
                elif kind == "poll" and not tr.get("_running") and now() - tr.get("_checked", 0) >= sp["every"] * 60:
                    tr["_checked"], tr["_running"] = now(), True
                    threading.Thread(target=self._run_poll, args=(tr,), daemon=True).start()
        if fired:
            self.touch()

    def _check_watch(self, tr: dict) -> bool:
        sig: dict[str, float] = {}
        for pat in (x.strip() for x in tr["spec"]["glob"].split(",")):
            if not pat:
                continue
            for f in self.project.glob(pat):
                if len(sig) >= 5000:
                    break
                if ".git" in f.parts or not f.is_file():
                    continue
                try:
                    sig[f.relative_to(self.project).as_posix()] = f.stat().st_mtime
                except OSError:
                    pass
        old = tr.get("_sig")
        if old is None:  # first look is the baseline
            tr["_sig"] = sig
            return False
        changed = sorted(k for k in sig.keys() | old.keys() if sig.get(k) != old.get(k))
        if not changed or now() - (tr.get("last") or 0) < tr["spec"]["cooldown"]:
            return False  # keep the old baseline so changes during the cooldown fire afterwards
        tr["_sig"] = sig
        self._fire(tr, {"files": "\n".join(changed[:40]) + ("\n…" if len(changed) > 40 else "")},
                   f"{len(changed)} file{'s' if len(changed) != 1 else ''} changed")
        return True

    def _run_poll(self, tr: dict) -> None:
        sp = tr["spec"]
        try:
            r = subprocess.run(sp["command"], shell=True, cwd=self.project, capture_output=True, text=True,
                               encoding="utf-8", errors="replace", timeout=120)
            out, code = (r.stdout + r.stderr).strip()[-4000:], r.returncode
        except subprocess.TimeoutExpired:
            out, code = "timed out after 120 s", -1
        with self.lock:
            tr["_running"] = False
            if tr not in self.triggers:
                return
            prev, prev_code = tr.get("_out"), tr.get("_code")
            tr["_out"], tr["_code"] = out, code
            tr["checked"] = {"t": now(), "exit": code}
            digest = hashlib.sha1(out.encode()).hexdigest()[:12]
            fire = {"change": prev is not None and out != prev,
                    "fail": code != 0 and prev_code in (0, None),
                    # a command that errors, or prints an empty JSON list/object, has nothing new to say
                    "output": code == 0 and out not in ("", "[]", "{}", "null") and digest != tr.get("seen")}[sp["when"]]
            if fire:
                tr["seen"] = digest
                self._fire(tr, {"output": out, "exit": code}, f"exit {code}")
        self.touch()

    def hook(self, tid: str, token: str, body: str) -> dict:
        with self.lock:
            tr = self._trigger(tid)
            if not tr or tr["kind"] != "webhook" or not hmac.compare_digest(tr["spec"]["token"], token or ""):
                return {"error": "unknown hook or bad token"}
            if not tr["enabled"]:
                return {"error": "trigger is off"}
            t = self._fire(tr, {"payload": body[:6000]}, "webhook")
        self.touch()
        return {"ok": True, "task": t["id"]}

    def _clean_trigger(self, p: dict, old: dict | None) -> tuple[str | None, dict]:
        kind = p.get("kind")
        if kind not in TRIGGER_KINDS:
            return "pick a trigger type", {}
        name = str(p.get("name") or "").strip()[:60]
        task = p.get("task") or {}
        sp = p.get("spec") or {}
        if not name:
            return "name the trigger", {}
        if not str(task.get("title") or "").strip():
            return "the task needs a title", {}
        if task.get("agent") and task["agent"] not in self.agents:
            return f"no agent called {task['agent']}", {}
        if task.get("workflow") and not self._workflow(task["workflow"]):
            return f"no workflow {task['workflow']}", {}
        try:
            if kind == "schedule":
                cron = " ".join(str(sp.get("cron") or "").split())
                if err := cron_error(cron):
                    return err, {}
                spec = {"cron": cron}
            elif kind == "event":
                if sp.get("on") not in EVENT_ON:
                    return "pick when: review, done or error", {}
                spec = {"on": sp["on"], "tag": sp.get("tag") or "", "agent": sp.get("agent") or ""}
            elif kind == "watch":
                glob = str(sp.get("glob") or "").strip()
                if not glob or ".." in glob or glob.startswith(("/", "\\")) or ":" in glob:
                    return "watch a pattern inside the project, e.g. src/**/*.py", {}
                spec = {"glob": glob[:200], "every": max(5, int(sp.get("every") or 10)),
                        "cooldown": max(0, int(sp.get("cooldown") if sp.get("cooldown") not in (None, "") else 60))}
            elif kind == "poll":
                cmd = str(sp.get("command") or "").strip()
                if not cmd:
                    return "the command to run is missing", {}
                spec = {"command": cmd[:1000], "every": max(1, int(sp.get("every") or 5)),
                        "when": sp.get("when") if sp.get("when") in POLL_WHEN else "change"}
            else:
                spec = {"token": (old or {}).get("spec", {}).get("token") or secrets.token_urlsafe(18)}
        except (TypeError, ValueError):
            return "numbers only for intervals", {}
        return None, {"name": name, "kind": kind, "enabled": bool(p.get("enabled", True)), "spec": spec,
                      "task": {"title": str(task["title"]).strip()[:200], "details": str(task.get("details") or "")[:4000],
                               "tag": task.get("tag") or "", "agent": task.get("agent") or "", "workflow": task.get("workflow") or ""}}

    # ---- workflows --------------------------------------------------------------------
    # A workflow is an ordered list of steps, each run by one agent. Running it creates step 1's task;
    # when a step is done (approved by you, or automatically if its gate is "auto") the next step's task
    # is created with the previous step's summary and branch, so agents build on each other's work.
    def _workflow(self, wid) -> dict | None:
        return next((w for w in self.config.get("workflows", []) if w["id"] == wid), None)

    def _run(self, rid) -> dict | None:
        return next((r for r in self.runs if r["id"] == rid), None)

    def _start_run(self, w: dict, title: str, details: str = "", source: dict | None = None) -> dict:
        n = len(w["steps"])
        run = {"id": f"r{self.next_run}", "wf": w["id"], "name": w["name"], "title": title[:200],
               "details": (details or "").strip()[:4000], "steps": json.loads(json.dumps(w["steps"])),
               "status": "running", "step": 0, "tasks": [None] * n, "results": [None] * n, "started": now(), "note": "",
               "source": source}
        self.next_run += 1
        self.runs.append(run)
        self._wf_step(run, 0)
        return run

    def _stop_agent(self, a: dict, why: str = "stopped by you") -> None:
        """End the current run now. The session is kept; the task waits in the backlog until you move it."""
        self._kill_proc(a["name"])
        if t := self._task(a["task"]):
            if t["col"] == "doing":
                t.update(col="backlog", who=t.get("pinned") or a["name"], hold=True)
        a.update(status="paused" if a.get("kind") != "boss" else "idle", task=None, activity="", since=None, compacting=None)
        self.log(a, "sys", why + (" · paused until you resume it or give it work" if a.get("kind") != "boss" else ""))

    def _wf_step(self, run: dict, i: int) -> None:
        step, n = run["steps"][i], len(run["steps"])
        a = self.agents.get(step["agent"])
        if not a:
            run.update(status="failed", note=f"step {i + 1}: no agent called {step['agent']}", ended=now())
            self.event("error", f"workflow {run['name']} stopped: {step['agent']} is not in the crew")
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
        t = {"id": self.next_id, "title": title, "details": ((body + "\n\n---\n") if body else "") + "\n\n".join(brief),
             "tag": (a["tags"] or ["feature"])[0], "col": "backlog", "who": a["name"], "pinned": a["name"], "created": now(),
             "run": {"id": run["id"], "step": i, "name": run["name"], "n": n}}
        self.next_id += 1
        self.tasks.append(t)
        run["step"], run["tasks"][i] = i, t["id"]
        self.event("workflow", f"⛓ {run['name']} {i + 1}/{n} → {a['name']}: {title}", a["name"])

    def _wf_event(self, t: dict, col: str, summary: str | None) -> None:
        ref = t.get("run") or {}
        run = self._run(ref.get("id"))
        if not run or run["status"] != "running" or run["tasks"][ref["step"]] != t["id"]:
            return
        i, step = ref["step"], run["steps"][ref["step"]]
        if col == "error":
            run["note"] = f"step {i + 1} hit an error; it is back in the backlog and the run continues when it finishes"
        elif col == "review" and step.get("gate") == "auto":
            self._approve(t, by="workflow")  # fires "done", which advances below
        elif col == "done":
            w = self.agents.get(t.get("who") or "")
            run["results"][i] = {"who": t.get("who") or "", "summary": (t.get("summary") or "")[:3000],
                                 "branch": w["branch"] if w and w.get("branch") else "", "commit": t.get("commit") or ""}
            run["note"] = ""
            if i + 1 < len(run["steps"]):
                self._wf_step(run, i + 1)
            else:
                run.update(status="done", ended=now())
                self.event("workflow", f"⛓ {run['name']} finished: {run['title']}")

    def _clean_workflow(self, p: dict) -> tuple[str | None, dict]:
        name = str(p.get("name") or "").strip()[:60]
        if not name:
            return "name the workflow", {}
        steps = []
        for k, st in enumerate(p.get("steps") or []):
            if st.get("agent") not in self.agents:
                return f"step {k + 1}: pick an agent", {}
            steps.append({"agent": st["agent"], "title": str(st.get("title") or "").strip()[:200],
                          "details": str(st.get("details") or "")[:3000], "gate": "auto" if st.get("gate") == "auto" else "approve"})
        if not steps:
            return "add at least one step", {}
        return None, {"name": name, "description": str(p.get("description") or "").strip()[:300], "steps": steps[:12]}

    # ---- actions from the UI ---------------------------------------------------
    def act(self, action: str, p: dict) -> dict:
        with self.lock:
            a = self.agents.get(p.get("agent", ""))
            t = self._task(p.get("task"))
            if action in ("tell", "stop", "compact", "respawn", "resume") and p.get("agent") == "boss":
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
                if p.get("context") in CONTEXTS:
                    a["context"] = p["context"]
                self._save_config()
                if a["status"] != "running" and self._alive(a["name"]) and tuple(a.get("launched") or ()) != self._launch_key(a):
                    self._kill_proc(a["name"])  # restarts with the new settings on its next task (session kept)
                self.event("config", f"{a['name']} · {a['model'] or 'default'} · {a['effort'] or 'default'} effort", a["name"])
            elif action == "add_agent":
                name = (p.get("name") or "").strip()
                if not NAME_RE.fullmatch(name):
                    return {"error": "name: 1-24 letters, digits or dashes"}
                if name.lower() in RESERVED:
                    return {"error": f"{name} is reserved"}
                if name.lower() in {n.lower() for n in self.agents}:  # worktree folders are case-insensitive on Windows
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
                self._send(a, text)
                if a["status"] in ("idle", "error", "paused"):
                    a.update(status="running", since=now(), activity="thinking…", error=None)
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
                self._send(a, "/compact")
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
                self._approve(t)
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
            elif action in ("trigger_add", "trigger_update"):
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
                        for k in [k for k in tr if k.startswith("_")] + ["minute", "seen", "checked"]:
                            tr.pop(k, None)  # new settings, new baseline
                        tr.update(clean)
                    else:
                        tr = clean | {"id": f"t{self.next_trig}", "fired": 0, "last": None, "created": now()}
                        self.next_trig += 1
                        self.triggers.append(tr)
                tr["next"] = cron_next(tr["spec"]["cron"], now()) if tr["kind"] == "schedule" and tr["enabled"] else None
                self.event("trigger", f"trigger {tr['name']} {'on' if tr['enabled'] else 'off'}")
            elif action == "trigger_delete" and (tr := self._trigger(p.get("id"))):
                self.triggers.remove(tr)
            elif action == "trigger_run" and (tr := self._trigger(p.get("id"))):
                self._fire(tr, {"payload": "(run by hand)", "output": "(run by hand)", "files": "(run by hand)", "exit": 0},
                           "run by you")
            elif action == "wf_save":
                err, clean = self._clean_workflow(p)
                if err:
                    return {"error": err}
                flows = self.config.setdefault("workflows", [])
                if w := self._workflow(p.get("id")):
                    w.update(clean)
                else:
                    ids = [int(x["id"][1:]) for x in flows if x["id"][1:].isdigit()]
                    flows.append(clean | {"id": f"w{max(ids, default=0) + 1}"})
                self._save_config()
            elif action == "wf_delete" and self._workflow(p.get("id")):
                self.config["workflows"] = [w for w in self.config["workflows"] if w["id"] != p["id"]]
                self._save_config()
            elif action == "wf_run" and (w := self._workflow(p.get("id"))):
                title = (p.get("title") or "").strip()
                if not title:
                    return {"error": "what should this run work on?"}
                self._start_run(w, title, p.get("details") or "")
            elif action == "run_stop" and (run := self._run(p.get("id"))) and run["status"] == "running":
                run.update(status="stopped", ended=now())
                self.event("workflow", f"⛓ {run['name']} stopped")
            elif action == "run_delete" and (run := self._run(p.get("id"))) and run["status"] != "running":
                self.runs.remove(run)
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
            elif action == "diff" and t and t.get("who") in self.agents:
                t["diff"] = self.diff(self.agents[t["who"]])
            else:
                return {"error": f"bad action {action}"}
        self.touch()
        return {"ok": True}

    def shutdown(self) -> None:
        for n in list(self.procs):  # includes the boss
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


PORT = 8770
LAN_KEY: str | None = None   # set when serving beyond this computer; every non-local request must carry it
LOCAL_HOSTS = ("127.0.0.1", "localhost", "[::1]")


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

    def _page(self, cookie: str | None = None):
        body = (APP_DIR / "index.html").read_bytes()
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
        if self.path.startswith("/api/hook/"):  # webhook triggers: a per-trigger token, no browser header needed
            u = urlsplit(self.path)
            token = self.headers.get("X-Crew-Token") or (parse_qs(u.query).get("token") or [""])[0]
            body = self.rfile.read(min(int(self.headers.get("Content-Length") or 0), 64_000)).decode("utf-8", "replace")
            res = CREW.hook(u.path.rsplit("/", 1)[1], token, body)
            return self._json(res, 403 if "error" in res else 200)
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


def main() -> None:
    global CREW
    ap = argparse.ArgumentParser(description="Local web console for a crew of Claude Code agents")
    ap.add_argument("--project", help="repo the agents work on (default: a demo git repo)")
    ap.add_argument("--port", type=int, default=8770)
    ap.add_argument("--config", help="crew config (default: crew.json next to server.py)")
    ap.add_argument("--demo", action="store_true", help="simulated agents with sample tasks: no Claude CLI, no tokens")
    ap.add_argument("--lan", action="store_true", help="also serve other devices on your network (phone, tablet), behind a key")
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
        cfg_path.parent.mkdir(parents=True, exist_ok=True)
        cfg_path.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
    config = DEFAULT_CONFIG | {"workflows": DEFAULT_WORKFLOWS} | json.loads(cfg_path.read_text(encoding="utf-8"))
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
    remember_project(project)
    if args.demo and not CREW.tasks:
        for title, tag in DEMO_TASKS:
            CREW.act("add", {"title": title, "tag": tag})
        for tr in DEMO_TRIGGERS:
            CREW.act("trigger_add", tr)

    def loop():
        while True:
            try:
                CREW.tick_triggers()
                CREW.dispatch()
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


if __name__ == "__main__":
    main()
