"""Teams trigger: a message in a Teams channel or chat starts one task or workflow run, once. Read-only, through the
Microsoft 365 connector of your Claude Code, so Crew holds no Microsoft tokens."""
from __future__ import annotations

import json
import re
import subprocess
import time
from pathlib import Path

from .base import Trigger, now, register, short_title

# ---- Teams trigger -----------------------------------------------------------------------------
# Crew has no Graph token of its own. Each check is a short headless `claude -p` run on Haiku that may use only the
# Microsoft 365 connector from your Claude Code (claude.ai connectors load in every session), reads the channel or chat,
# and answers in JSON. Read-only: it never posts. Every new message starts one task or workflow run.
TEAMS_TOOLS = "mcp__claude_ai_Microsoft_365"
TEAMS_MODES = ("mention", "all")
TEAMS_MAX = 10  # messages that start work per check
TEAMS_SEEN_MAX = 500

class TeamsError(Exception):
    pass


def teams_spec(sp: dict) -> tuple[str | None, dict]:
    where = " ".join(str(sp.get("where") or "").split())
    if not where:
        return "which channel or chat? e.g. Platform > Incidents", {}
    try:
        every = max(5, int(sp.get("every") or 10))
    except (TypeError, ValueError):
        return "numbers only for intervals", {}
    return None, {"where": where[:200], "mode": sp.get("mode") if sp.get("mode") in TEAMS_MODES else "mention",
                  "about": " ".join(str(sp.get("about") or "").split())[:200], "every": every}


def teams_prompt(sp: dict, since_iso: str) -> str:
    which = ("only messages that @mention me (the signed-in user), or reply to me in a thread" if sp["mode"] == "mention"
             else "every new message and reply")
    return (
        "You check Microsoft Teams for Crew, which turns messages into work for an AI team. Use only the Microsoft 365 "
        "tools, read-only: never send, post or react.\n"
        f"Where: {sp['where']} (a team and channel, or a chat).\n"
        f"Which messages: {which}, posted after {since_iso} (UTC). Skip messages I sent myself."
        + (f"\nOnly messages about: {sp['about']}." if sp.get("about") else "")
        + "\nReply with JSON only, no prose, in this shape:\n"
        '{"messages": [{"id": "the message id", "from": "sender name", "time": "ISO time", "text": "the message, up to 1500 '
        'characters", "link": "a URL to the message, or empty", "title": "what is being asked, 3 to 6 plain words"}]}\n'
        'Use {"messages": []} when there is nothing new. If you cannot reach Microsoft 365 (not signed in, no access, '
        'channel not found), reply {"error": "short reason"}.')


def teams_check(claude: list[str], sp: dict, since: float, cwd: Path) -> tuple[list[dict], float]:
    """One read of Teams. Returns (messages, cost in USD). Raises TeamsError with a reason a person can act on."""
    since_iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(since))
    cmd = [*claude, "-p", teams_prompt(sp, since_iso), "--output-format", "json", "--model", "haiku",
           "--max-turns", "15", "--allowedTools", TEAMS_TOOLS]
    cwd.mkdir(parents=True, exist_ok=True)
    try:
        r = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, encoding="utf-8", errors="replace",
                           timeout=240, stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired:
        raise TeamsError("the check took over 4 minutes and was stopped") from None
    except OSError as e:
        raise TeamsError(f"could not start claude: {e}") from None
    try:
        d = json.loads(r.stdout)
    except json.JSONDecodeError:
        raise TeamsError("claude failed: " + ((r.stdout + r.stderr).strip()[-300:] or f"exit {r.returncode}")) from None
    cost = float(d.get("total_cost_usd") or 0)
    text = str(d.get("result") or "")
    if d.get("is_error"):
        raise TeamsError(text[:300] or "claude reported an error")
    m = re.search(r"\{.*\}", text, re.S)
    try:
        out = json.loads(m.group(0)) if m else None
    except json.JSONDecodeError:
        out = None
    if not isinstance(out, dict):
        raise TeamsError("could not read the answer: " + text[:200])
    if out.get("error"):
        why = str(out["error"])[:300]
        if re.search(r"not available|sign|auth|connect|login|log in", why, re.I):
            why += ". Sign in the Microsoft 365 connector once: run `claude` in a terminal, type /mcp, pick claude.ai Microsoft 365"
        raise TeamsError(why)
    msgs = []
    for x in out.get("messages") or []:
        if isinstance(x, dict) and x.get("id"):
            link = str(x.get("link") or "")
            msgs.append({"id": str(x["id"])[:200], "from": str(x.get("from") or "someone")[:100], "time": str(x.get("time") or "")[:40],
                         "text": str(x.get("text") or "")[:2000], "link": link[:600] if link.startswith(("https://", "http://")) else "",
                         "title": short_title(x.get("title") or x.get("text")) or "Teams message"})
    return msgs, cost


@register
class Teams(Trigger):
    kind, order = "teams", 70
    ui_only = "teams triggers read your Teams with your Microsoft 365 sign-in: create them in Crew's Triggers panel"
    polled = check_on_run = keeps_seen = True

    def validate(self, sp, old=None):
        return teams_spec(sp)

    def form(self, crew, p):
        """POST /api/trigger/teams {op: "test", spec}: one read of the last 24 hours. Starts nothing; it is a short paid Haiku run."""
        if p.get("op") != "test":
            return {"error": "unknown op"}
        err, sp = teams_spec(p.get("spec") or {})
        if err:
            return {"error": err}
        try:
            msgs, cost = teams_check(crew.claude, sp, now() - 86400, crew.work_root)
        except TeamsError as e:
            return {"error": str(e)}
        with crew.lock:
            crew._charge(None, cost)
        return {"ok": True, "total": len(msgs), "items": msgs[:25], "cost": round(cost, 4)}

    def check(self, crew, tr):
        """One check. Runs in a thread; claude never runs while the lock is held."""
        sp, started, err, found, picked, cost = tr["spec"], now(), None, 0, 0, 0.0
        since = tr.get("since") or started - sp["every"] * 60
        try:
            msgs, cost = teams_check(crew.claude, sp, since - 120, crew.work_root)  # a little overlap: ids keep it from firing twice
            found = len(msgs)
            with crew.lock:
                if tr not in crew.triggers:
                    return
                seen = list(tr.get("seen") or [])
                for m in [m for m in msgs if m["id"] not in seen][:TEAMS_MAX]:
                    crew._fire(tr, {"title": m["title"], "from": m["from"], "text": m["text"], "link": m["link"],
                                    "when": m["time"], "where": sp["where"]}, f"message from {m['from']}")
                    seen.append(m["id"])
                    picked += 1
                tr["seen"], tr["since"] = seen[-TEAMS_SEEN_MAX:], started
                tr["picked"] = tr.get("picked", 0) + picked
        except TeamsError as e:
            err = str(e)
        except Exception as e:  # a bug here must not leave the trigger stuck as running
            err = f"unexpected: {e}"
        with crew.lock:
            crew._charge(None, cost)
        self.finish(crew, tr, err, {"found": found, "picked": picked, "cost": round(cost, 4)})
