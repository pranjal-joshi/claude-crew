"""Teams trigger: a message in a Teams channel or chat starts one task or workflow run, once. Read-only, through the
Microsoft 365 connector of your Claude Code, so Crew holds no Microsoft tokens."""
from __future__ import annotations

import json
import re
import subprocess
import time
from pathlib import Path
from urllib.parse import unquote

from .base import Trigger, now, register, short_title

# ---- Teams trigger -----------------------------------------------------------------------------
# Crew has no Graph token of its own. Each check is a short headless `claude -p` run on Haiku that may use only the
# Microsoft 365 connector from your Claude Code (claude.ai connectors load in every session), reads the channel or chat,
# and answers in JSON. Read-only: it never posts. Every new message starts one task or workflow run.
TEAMS_TOOLS = "mcp__claude_ai_Microsoft_365"
TEAMS_MODES = ("mention", "all")
TEAMS_MAX = 10  # messages that start work per check
TEAMS_SEEN_MAX = 500
# A Teams link (to a channel, a chat, or any message in one) names its conversation by thread id. Searching messages
# and keeping that thread id needs only the message-read permissions most tenants grant; looking a channel up by name
# needs Team.ReadBasic.All, which many organizations don't grant the connector.
THREAD_RE = re.compile(r"19(?::|%3[Aa])[A-Za-z0-9_.\-]+(?:@|%40)thread\.(?:skype|tacv2|v2)")


def thread_id(where: str) -> str:
    m = THREAD_RE.search(where or "")
    return m.group(0).replace("%3A", ":").replace("%3a", ":").replace("%40", "@") if m else ""


def team_id(where: str) -> str:
    """The team (group) id a channel link carries. Listing a channel's posts and their replies needs it."""
    m = re.search(r"[?&]groupId=([0-9A-Fa-f-]{36})", where or "")
    return m.group(1) if m else ""

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
    name = " ".join(str(sp.get("name") or "").split())[:100]
    if not name and (m := re.search(r"[?&]channelName=([^&]+)", where)):  # a channel link names its channel
        name = unquote(m.group(1).replace("+", " "))[:100]
    return None, {"where": where[:600], "thread": thread_id(where), "name": name,
                  "mode": sp.get("mode") if sp.get("mode") in TEAMS_MODES else "mention",
                  "about": " ".join(str(sp.get("about") or "").split())[:200], "every": every}


def teams_prompt(sp: dict, since_iso: str) -> str:
    which = ("only messages that @mention me (the signed-in user), or reply to me in a thread" if sp["mode"] == "mention"
             else "every new message and reply")
    team = team_id(sp["where"])
    if sp.get("thread"):
        where = (f"Where: the Teams conversation with thread id {sp['thread']}"
                 + (f" ({sp['name']})" if sp.get("name") else "") + ". Find its messages with the message search tool "
                 f"(chat_message_search, query '*', afterDateTime {since_iso}, every page) and keep only results whose "
                 f"chatId is {sp['thread']}. Don't list teams or channels: that permission is not granted.")
        if team:
            # A reply alone ("please look into this") says nothing; the search doesn't say which thread it is in, and
            # read_resource can't open a reply by its id. Listing the channel's posts and their replies can.
            where += (f"\nThreads: the search doesn't tell which post a reply belongs to, so find it for every message you "
                      f"keep. List the channel's top-level posts with teams_list_channel_messages (teamId {team}, channelId "
                      f"{sp['thread']}); they come newest activity first. Then list the replies of each post from the top "
                      "(the same tool with parentMessageId) until you find the message's id. Never guess the thread from "
                      "the sender or the time. Take the link from the webUrl these list results give.")
    else:
        where = (f"Where: {sp['where']} (a team and channel, or a chat). Find it with the message search tool "
                 "(chat_message_search) and the chat list; don't list teams or channels, that permission is usually "
                 "not granted. If you can't tell which conversation it is, reply {\"error\": \"paste a Teams link to "
                 "the channel or to any message in it\"}.")
    return (
        "You check Microsoft Teams for Crew, which turns messages into work for an AI team. Use only the Microsoft 365 "
        "tools, read-only: never send, post or react.\n"
        f"{where}\n"
        f"Which messages: {which}, posted after {since_iso} (UTC). Skip messages I sent myself. An @mention shows as my "
        "name in the message text; call get_me for my name. Use read_resource on a message for its full text when the "
        "search summary is cut short; if that fails, use the summary."
        + (f"\nOnly messages about: {sp['about']}." if sp.get("about") else "")
        + "\nReply with JSON only, no prose, in this shape:\n"
        '{"messages": [{"id": "the message id", "from": "sender name", "time": "ISO time", "text": "the message, up to 1500 '
        'characters", "link": "a URL to the message, or empty", "title": "what is being asked, 3 to 6 plain words, '
        'taken from the thread when the message only points at it", "thread": null or, when the message is a reply, '
        '{"from": "who started the thread", "subject": "its subject", "text": "the top-level post, up to 1500 characters", '
        '"replies": "the earlier replies in order, one line each as name: text, up to 800 characters"}}]}\n'
        'Use {"messages": []} when there is nothing new. If you cannot reach Microsoft 365 (not signed in, no access, '
        'channel not found), reply {"error": "short reason"}.')


def thread_text(th) -> str:
    """The thread a reply was posted in, for {text}: the request is usually the post, not the reply that points at it."""
    if not isinstance(th, dict) or not str(th.get("text") or "").strip():
        return ""
    subject = str(th.get("subject") or "").strip()[:200]
    out = (f"\n\nThis is a reply in the thread {f'"{subject}" ' if subject else ''}started by {str(th.get('from') or 'someone')[:100]}:\n"
           + str(th["text"]).strip()[:2000])
    if replies := str(th.get("replies") or "").strip():
        out += "\n\nEarlier replies:\n" + replies[:1000]
    return out


def teams_check(claude: list[str], sp: dict, since: float, cwd: Path) -> tuple[list[dict], float]:
    """One read of Teams. Returns (messages, cost in USD). Raises TeamsError with a reason a person can act on."""
    since_iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(since))
    cmd = [*claude, "-p", teams_prompt(sp, since_iso), "--output-format", "json", "--model", "haiku",
           "--max-turns", "50", "--allowedTools", TEAMS_TOOLS]  # finding a reply's thread takes a few list calls
    cwd.mkdir(parents=True, exist_ok=True)
    try:
        r = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, encoding="utf-8", errors="replace",
                           timeout=360, stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired:
        raise TeamsError("the check took over 6 minutes and was stopped") from None
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
                         "text": str(x.get("text") or "")[:2000] + thread_text(x.get("thread")),
                         "link": link[:600] if link.startswith(("https://", "http://")) else "",
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
                                    # the channel's name, not the pasted link: that link points at some other message
                                    "when": m["time"], "where": sp.get("name") or sp["where"]}, f"message from {m['from']}")
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
