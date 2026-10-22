"""Poll trigger: run a shell command every few minutes and fire on a change, a failure or any output."""
from __future__ import annotations

import hashlib
import subprocess

from .base import Trigger, now, register

POLL_WHEN = ["change", "fail", "output"]


@register
class Poll(Trigger):
    kind, order = "poll", 40
    ui_only = "poll triggers run shell commands: create them in Crew's Triggers panel, not through the boss or an agent"
    polled = True

    def validate(self, sp, old=None):
        cmd = str(sp.get("command") or "").strip()
        if not cmd:
            return "the command to run is missing", {}
        return None, {"command": cmd[:1000], "every": max(1, int(sp.get("every") or 5)),
                      "when": sp.get("when") if sp.get("when") in POLL_WHEN else "change"}

    def check(self, crew, tr):
        sp = tr["spec"]
        try:
            r = subprocess.run(sp["command"], shell=True, cwd=crew.project, capture_output=True, text=True,
                               encoding="utf-8", errors="replace", timeout=120)
            out, code = (r.stdout + r.stderr).strip()[-4000:], r.returncode
        except subprocess.TimeoutExpired:
            out, code = "timed out after 120 s", -1
        with crew.lock:
            tr["_running"] = False
            if tr not in crew.triggers:
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
                crew._fire(tr, {"output": out, "exit": code}, f"exit {code}")
        crew.touch()
