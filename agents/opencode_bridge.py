"""OpenCode engine: speaks Claude Code's stream-json on stdin/stdout, so Crew drives an OpenCode agent like a Claude one.

Crew starts this script instead of `claude -p --input-format stream-json`. Each user message on stdin becomes one
`opencode run --format json` (the message goes in on stdin: Windows caps a command line at 32k characters), continuing
the agent's OpenCode session. Its JSON events come out as the stream-json events Crew's reader expects: an init, assistant
text and tool_use blocks, tool_result blocks, token usage per step, and a result with the cumulative cost when the turn
ends. Run as a script (no imports from Crew): `python opencode_bridge.py --opencode PATH --model provider/model ...`.
"""
from __future__ import annotations

import argparse
import json
import os
import queue
import subprocess
import sys
import threading

IS_WIN = os.name == "nt"
TOOL_NAMES = {"bash": "Bash", "read": "Read", "edit": "Edit", "write": "Write", "glob": "Glob", "grep": "Grep", "list": "LS",
              "webfetch": "WebFetch", "websearch": "WebSearch", "todowrite": "TodoWrite", "todoread": "TodoRead",
              "patch": "Edit", "multiedit": "MultiEdit", "task": "Agent"}
INPUT_KEYS = {"filePath": "file_path", "path": "file_path"}
RESULT_MAX = 4000  # tool output kept on a tool_result: Crew logs errors only


def emit(d: dict) -> None:
    sys.stdout.write(json.dumps(d) + "\n")
    sys.stdout.flush()


def tool_name(t: str) -> str:
    """bash -> Bash; an MCP tool (crew_decide) -> mcp__crew__decide, the name Claude Code would give it."""
    if t in TOOL_NAMES:
        return TOOL_NAMES[t]
    server, _, name = t.partition("_")
    return f"mcp__{server}__{name}" if name else t


def message_text(msg: dict) -> str:
    """The text of a stream-json user message. Images can't go through `opencode run`: say so instead of dropping them."""
    c = (msg.get("message") or {}).get("content")
    if isinstance(c, str):
        return c
    parts, images = [], 0
    for b in c or []:
        if isinstance(b, dict) and b.get("type") == "text":
            parts.append(b.get("text") or "")
        elif isinstance(b, dict) and b.get("type") == "image":
            images += 1
    if images:
        parts.append(f"({images} image{'s' if images > 1 else ''} attached in Crew; this engine can't read images)")
    return "\n".join(parts)


class Bridge:
    def __init__(self, args):
        self.a = args
        self.session = args.session or None
        self.cost = 0.0  # cumulative for this process, like Claude Code's total_cost_usd
        self.inited = False

    def ev(self, d: dict) -> None:
        if self.session:
            d["session_id"] = self.session
        emit(d)

    def init(self) -> None:
        if self.inited:
            return
        self.inited = True
        self.ev({"type": "system", "subtype": "init", "model": self.a.model or "opencode default", "permissionMode": self.a.mode,
                 "mcp_servers": [{"name": n, "status": "connected"} for n in self.a.mcp], "skills": [], "plugins": [],
                 "slash_commands": [], "tools": [], "engine": "opencode"})

    def result(self, ok: bool, text: str) -> None:
        self.ev({"type": "result", "subtype": "success" if ok else "error_during_execution", "is_error": not ok,
                 "result": text, "total_cost_usd": round(self.cost, 6), "modelUsage": {}, "permission_denials": []})

    def turn(self, text: str) -> None:
        if text.strip() == "/compact":  # OpenCode compacts on its own; Crew's /compact turn ends at once
            self.result(True, "This engine compacts its context on its own.")
            return
        cmd = [self.a.opencode, *self.a.opencode_arg, "run", "--format", "json"]
        if self.a.model:
            cmd += ["-m", self.a.model]
        if self.session:
            cmd += ["-s", self.session]
        if self.a.variant:
            cmd += ["--variant", self.a.variant]
        env = {**os.environ, **({"OPENCODE_CONFIG": self.a.config} if self.a.config else {})}
        try:
            p = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                                 encoding="utf-8", errors="replace", bufsize=1, env=env)
        except OSError as e:
            self.init()
            self.result(False, f"could not start opencode: {e}")
            return
        err: list[str] = []
        threading.Thread(target=lambda: err.extend(p.stderr), daemon=True).start()
        try:
            p.stdin.write(text)
            p.stdin.close()
        except OSError:
            pass
        texts: list[str] = []  # text since the last tool call: the turn's answer
        failed = None
        for line in p.stdout:
            try:
                d = json.loads(line)
            except json.JSONDecodeError:
                continue
            if d.get("sessionID") and not self.session:
                self.session = d["sessionID"]
            self.init()
            part, kind = d.get("part") or {}, d.get("type")
            mid = part.get("messageID") or part.get("id") or ""
            if kind == "text" and (part.get("text") or "").strip():
                texts.append(part["text"])
                self.ev({"type": "assistant", "message": {"id": mid, "content": [{"type": "text", "text": part["text"]}]}})
            elif kind == "tool_use":
                st, cid = part.get("state") or {}, part.get("callID") or part.get("id") or ""
                inp = {INPUT_KEYS.get(k, k): v for k, v in (st.get("input") or {}).items()}
                texts = []
                self.ev({"type": "assistant", "message": {"id": mid, "content": [
                    {"type": "tool_use", "id": cid, "name": tool_name(part.get("tool") or "tool"), "input": inp}]}})
                bad = st.get("status") == "error"
                out = st.get("error") if bad else st.get("output")
                self.ev({"type": "user", "message": {"content": [
                    {"type": "tool_result", "tool_use_id": cid, "is_error": bad, "content": str(out or "")[:RESULT_MAX]}]}})
            elif kind == "step_finish":
                tk = part.get("tokens") or {}
                cache = tk.get("cache") or {}
                self.cost += float(part.get("cost") or 0)
                self.ev({"type": "assistant", "message": {"id": part.get("id") or mid, "model": self.a.model or "opencode default", "content": [], "usage": {
                    "input_tokens": int(tk.get("input") or 0), "cache_read_input_tokens": int(cache.get("read") or 0),
                    "cache_creation_input_tokens": int(cache.get("write") or 0),
                    "output_tokens": int(tk.get("output") or 0) + int(tk.get("reasoning") or 0)}}})
            elif kind == "error":
                e = d.get("error") or {}
                failed = str((e.get("data") or {}).get("message") or e.get("message") or e.get("name") or e)[:400]
        rc = p.wait()
        self.init()
        if failed or rc:
            tail = "".join(err).strip().splitlines()[-3:]
            self.result(False, failed or f"opencode exited with {rc}" + (": " + " ".join(tail)[:300] if tail else ""))
        else:
            self.result(True, "\n".join(texts).strip())


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--opencode", required=True)
    ap.add_argument("--opencode-arg", action="append", default=[])  # e.g. a script, when --opencode is an interpreter
    ap.add_argument("--model", default="")
    ap.add_argument("--session", default="")
    ap.add_argument("--config", default="")
    ap.add_argument("--variant", default="")
    ap.add_argument("--mode", default="")
    ap.add_argument("--mcp", action="append", default=[])
    b = Bridge(ap.parse_args())
    if IS_WIN:
        sys.stdout.reconfigure(encoding="utf-8")
    sys.stdin.reconfigure(encoding="utf-8")
    q: queue.Queue = queue.Queue()

    def reader():  # always drain stdin, so Crew never sees a writer stuck behind a long turn
        for line in sys.stdin:
            q.put(line)
        q.put(None)

    threading.Thread(target=reader, daemon=True).start()
    b.init()
    while (line := q.get()) is not None:
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue
        if msg.get("type") == "user":
            b.turn(message_text(msg))


if __name__ == "__main__":
    main()
