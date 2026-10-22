"""The agent's Claude Code environment: local files git leaves out of a worktree, local MCP servers, the slash-command catalog."""
from __future__ import annotations

import json
import re
import shutil
from pathlib import Path

from .util import git

# Claude Code files that git doesn't carry into a worktree (ignored or never committed) but that
# shape a session: local settings, project skills/agents/commands, project MCP servers, local memory.
CLAUDE_LOCAL = [".claude/settings.local.json", ".claude/skills", ".claude/agents", ".claude/commands",
                ".claude/rules", ".mcp.json", "CLAUDE.local.md"]


def local_claude_files(src: Path) -> list[str]:
    """The project's untracked Claude Code files (posix paths relative to src). Tracked ones come with git."""
    tracked = set(git(src, "ls-files", "--", *CLAUDE_LOCAL).stdout.split("\n"))
    out = []
    for rel in CLAUDE_LOCAL:
        s_path = src / rel
        files = [s_path] if s_path.is_file() else [f for f in s_path.rglob("*") if f.is_file()] if s_path.is_dir() else []
        out += [r for f in files if (r := f.relative_to(src).as_posix()) not in tracked]
    return out


def git_exclude(wt: Path, paths: list[str]) -> None:
    """List paths in the worktree's info/exclude so `git add -A` never picks them up. Idempotent.
    Git keeps a single info/exclude per repository, so all worktrees (and the project itself) share it."""
    if not paths:
        return
    out = git(wt, "rev-parse", "--git-path", "info/exclude").stdout.strip()
    if not out:
        return
    f = wt / out  # absolute when git reports it that way
    try:
        text = f.read_text(encoding="utf-8") if f.exists() else ""
        have = set(text.splitlines())
        lines = ["/" + re.sub(r"([\*?\[\]!#])", r"\\1", r) for r in paths]
        new = [ln for ln in dict.fromkeys(lines) if ln not in have]
        if new:
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_text(text + ("" if not text or text.endswith("\n") else "\n") + "\n".join(new) + "\n", encoding="utf-8")
    except OSError:
        pass


def sync_claude_files(src: Path, dst: Path) -> list[str]:
    """Copy the project's untracked Claude Code files into an agent's worktree, and keep them out of its commits."""
    paths = local_claude_files(src)
    copied = []
    for r in paths:
        f, d = src / r, dst / r
        try:
            if d.exists() and d.stat().st_mtime >= f.stat().st_mtime and d.stat().st_size == f.stat().st_size:
                continue
            d.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(f, d)
            copied.append(r)
        except OSError:
            pass
    git_exclude(dst, paths)
    return copied


def _md_description(f: Path) -> str:
    """The description from a command/skill file's front matter, else its first line of text."""
    try:
        txt = f.read_text(encoding="utf-8", errors="replace")[:6000]
    except OSError:
        return ""
    body = txt
    if txt.startswith("---"):
        head, _, body = txt[3:].partition("\n---")
        lines = head.splitlines()
        for i, line in enumerate(lines):
            if line.lower().startswith("description:"):
                v = line.split(":", 1)[1].strip()
                if v in ("", ">", "|", ">-", "|-"):  # folded / block scalar: the indented lines below
                    v = " ".join(x.strip() for x in lines[i + 1:] if x.startswith((" ", "\t")))
                return v.strip("\"'")[:220]
    return next((x.strip("# ").strip() for x in body.splitlines() if x.strip() and not x.startswith("---")), "")[:220]


def command_catalog(project: Path) -> dict:
    """Slash commands and skills on disk (user, then project), with descriptions for the / picker."""
    out = {}
    for base, scope in ((Path.home() / ".claude", "user"), (project / ".claude", "project")):
        cmds = base / "commands"
        if cmds.is_dir():
            for f in sorted(cmds.rglob("*.md"))[:300]:
                out[":".join(f.relative_to(cmds).with_suffix("").parts)] = {"kind": "command", "scope": scope, "desc": _md_description(f)}
        skills = base / "skills"
        if skills.is_dir():
            for f in sorted(skills.glob("*/SKILL.md"))[:300]:
                out[f.parent.name] = {"kind": "skill", "scope": scope, "desc": _md_description(f)}
    return out


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
