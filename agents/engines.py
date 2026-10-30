"""Engines: what runs an agent. Claude Code by default; OpenCode as an alternative, with its free models (opencode/*) or
the models of your GitHub Copilot seat (github-copilot/*), through opencode_bridge.py, which speaks Claude Code's
stream-json so the rest of Crew can't tell them apart. The boss always runs on Claude Code.

What OpenCode does not have, Crew does without: no permission inbox (its rules are an allow-list written into the agent's
OpenCode config, and anything else is denied), no effort levels, no /compact (OpenCode compacts on its own), and its
cost is what OpenCode reports ($0 on free models). Functions take the Crew first; lock held unless they say otherwise."""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

ENGINES = ("claude", "opencode", "copilot")  # copilot: OpenCode with your Copilot seat's models
FALLBACKS = ("claude",)  # when an OpenCode turn fails: the retry runs on Claude Code
FALLBACK_MODEL = "sonnet"
PROVIDER = {"opencode": "opencode/", "copilot": "github-copilot/"}
MODEL_RE = re.compile(r"[\w.-]+/[\w.:@-]+")  # provider/model, as `opencode models` lists them
SESSION_PREFIX = "ses_"  # OpenCode session ids; Claude Code's are UUIDs
APP_DIR = Path(__file__).resolve().parent

_found: dict = {"checked": 0}  # what probe() last read: {path, version, models, error, checked}
_probing = threading.Lock()


def of(a: dict) -> str:
    """The engine an agent is set to."""
    return a.get("engine") if a.get("engine") in ENGINES else "claude"


def effective(a: dict) -> str:
    """The engine its next start uses: Claude Code while it is on its fallback."""
    return "claude" if a.get("on_fallback") or a.get("kind") == "boss" else of(a)


def model_ok(engine: str, model: str | None, claude_models: list[str]) -> bool:
    if not model:
        return True
    if engine == "claude":
        return model in claude_models
    return bool(MODEL_RE.fullmatch(model)) and model.startswith(PROVIDER[engine])


def session_ok(engine: str, sid: str | None) -> bool:
    """Can this engine resume this session id? Switching engine starts a fresh conversation."""
    return not sid or sid.startswith(SESSION_PREFIX) == (engine != "claude")


def probe(force: bool = False) -> dict:
    """Is opencode installed, and which models does it offer? Runs the CLI: never with the lock held. Cached 10 minutes."""
    if not force and time.time() - _found["checked"] < 600:
        return dict(_found)
    with _probing:
        out: dict = {"path": shutil.which("opencode"), "version": "", "models": [], "error": None, "checked": time.time()}
        if not out["path"]:
            out["error"] = "opencode is not installed (npm i -g opencode-ai)"
        else:
            try:
                v = subprocess.run([out["path"], "--version"], capture_output=True, text=True, encoding="utf-8",
                                   errors="replace", timeout=30, stdin=subprocess.DEVNULL)
                out["version"] = v.stdout.strip().splitlines()[0] if v.stdout.strip() else ""
                m = subprocess.run([out["path"], "models"], capture_output=True, text=True, encoding="utf-8",
                                   errors="replace", timeout=90, stdin=subprocess.DEVNULL)
                out["models"] = [x.strip() for x in m.stdout.splitlines() if MODEL_RE.fullmatch(x.strip())][:300]
                if m.returncode and not out["models"]:
                    out["error"] = (m.stderr or "opencode models failed").strip()[:200]
            except (subprocess.TimeoutExpired, OSError) as e:
                out["error"] = f"opencode did not answer: {e}"[:200]
        _found.clear()
        _found.update(out)
        return dict(out)


def view() -> dict:
    """What the UI shows about each engine: installed, version, and the models it can pick."""
    f = _found
    models = f.get("models") or []
    base = {"ok": bool(f.get("path")) and not f.get("error"), "version": f.get("version") or "", "error": f.get("error"),
            "checked": bool(f.get("checked"))}
    return {"opencode": {**base, "models": [m for m in models if m.startswith(PROVIDER["opencode"])]},
            "copilot": {**base, "models": [m for m in models if m.startswith(PROVIDER["copilot"])]}}


def _user_mcp_servers() -> dict:
    """User-scope MCP servers (`claude mcp add -s user`): Claude Code loads them by itself, OpenCode needs them listed."""
    try:
        return dict(json.loads((Path.home() / ".claude.json").read_text(encoding="utf-8")).get("mcpServers") or {})
    except (OSError, ValueError):
        return {}


def _mcp(servers: dict) -> dict:
    """Claude Code MCP server specs as OpenCode's."""
    out = {}
    for name, s in servers.items():
        if not isinstance(s, dict):
            continue
        if s.get("url"):
            out[name] = {"type": "remote", "url": s["url"], "enabled": True, **({"headers": s["headers"]} if s.get("headers") else {})}
        elif s.get("command"):
            out[name] = {"type": "local", "command": [s["command"], *(s.get("args") or [])], "enabled": True,
                         **({"environment": s["env"]} if s.get("env") else {})}
    return out


def bash_rule(rule: str) -> str | None:
    """A Claude Code allow rule for Bash as an OpenCode bash pattern: Bash(git log:*) -> "git log*", Bash -> "*"."""
    if rule == "Bash":
        return "*"
    m = re.fullmatch(r"Bash\((.+)\)", rule)
    if not m:
        return None
    body = m.group(1)
    return body[:-2] + "*" if body.endswith(":*") else body


def permissions(mode: str, allowed: list[str]) -> dict:
    """Crew's permission mode and allow-list as OpenCode's permission config. A run has nobody to ask, so whatever
    isn't allowed is denied (the agent sees the refusal and says so in its summary)."""
    if mode == "bypassPermissions":
        return {"edit": "allow", "bash": "allow", "webfetch": "allow"}
    bash = {"*": "deny"}
    for r in allowed:
        if (pat := bash_rule(r)) is not None:
            bash[pat] = "allow"
    web = "allow" if {"WebFetch", "WebSearch"} & set(allowed) else "deny"
    return {"edit": "deny" if mode == "plan" else "allow", "bash": bash, "webfetch": web}


def write_config(crew, a: dict, role: str, mode: str, allowed: list[str], servers: dict) -> Path:
    """The agent's OpenCode config: its role as instructions, the crew tool and your MCP servers, its permissions."""
    d = crew.work_root / "mcp"
    d.mkdir(parents=True, exist_ok=True)
    brief = d / f"{crew.key}-{a['key']}.opencode.md"
    brief.write_text(role, encoding="utf-8")
    # snapshot off: OpenCode would copy the whole repo into its own git store on every start (a minute on a big repo),
    # for an undo that the agent's worktree and Crew's review already give
    cfg = {"$schema": "https://opencode.ai/config.json", "snapshot": False, "instructions": [str(brief)],
           "mcp": _mcp({**_user_mcp_servers(), **servers}), "permission": permissions(mode, allowed)}
    f = d / f"{crew.key}-{a['key']}.opencode.json"
    f.write_text(json.dumps(cfg, indent=1), encoding="utf-8")
    return f


def command(crew, a: dict, role: str, mode: str, allowed: list[str], servers: dict) -> list[str] | str:
    """The bridge's command line for this agent, or why it can't start."""
    f = _found
    if not f.get("path"):
        return f.get("error") or "opencode is not installed (npm i -g opencode-ai)"
    cfg = write_config(crew, a, role, mode, allowed, servers)
    cmd = [sys.executable, str(APP_DIR / "opencode_bridge.py"), "--opencode", f["path"], "--config", str(cfg), "--mode", mode]
    if a.get("model"):
        cmd += ["--model", a["model"]]
    if session_ok("opencode", a.get("session")) and a.get("session"):
        cmd += ["--session", a["session"]]
    for name in _mcp({**_user_mcp_servers(), **servers}):
        cmd += ["--mcp", name]
    return cmd
