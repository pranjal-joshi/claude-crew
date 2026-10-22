"""Agent processes: the stdin writer thread, process trees, and the command-line checks that keep Crew from touching
a process that is not its own."""
from __future__ import annotations

import os
import queue
import re
import signal
import subprocess
import threading
import time

IS_WIN = os.name == "nt"

STDIN_LIMIT = 15.0  # a write to an agent's stdin that blocks longer than this means the process is hung


def proc_cmdlines(pids) -> dict[int, str]:
    """Command lines of the live processes among pids ({} when they can't be read). Windows: Get-CimInstance
    (wmic is gone from current Windows); elsewhere: ps."""
    pids = sorted({int(p) for p in pids if int(p) > 0})
    if not pids:
        return {}
    try:
        if IS_WIN:
            flt = " -or ".join(f"$_.ProcessId -eq {p}" for p in pids)
            ps = "Get-CimInstance Win32_Process | Where-Object { " + flt + ' } | ForEach-Object { "$($_.ProcessId)`t$($_.CommandLine)" }'
            r = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", ps], capture_output=True, text=True,
                               encoding="utf-8", errors="replace", timeout=25)
        else:
            # every process, full width, filtered here: macOS ps rejects a -p list holding a dead pid and truncates commands
            r = subprocess.run(["ps", "-A", "-ww", "-o", "pid=,command="], capture_output=True, text=True,
                               encoding="utf-8", errors="replace", timeout=10)
    except (OSError, subprocess.SubprocessError):
        return {}
    out = {}
    for line in r.stdout.splitlines():
        m = re.match(r"\s*(\d+)[\t ]+(.*)$", line)
        if m and int(m.group(1)) in pids:
            out[int(m.group(1))] = m.group(2).strip()
    return out


def is_agent_cmdline(cmd: str, key: str = "") -> bool:
    """Does this command line look like an agent process Crew started: a claude / node claude-code / demo fake_claude
    speaking stream-json, whose --mcp-config file carries this project's key? Guards against a reused pid."""
    c = (cmd or "").lower()
    return "stream-json" in c and "claude" in c and (not key or key.lower() in c)


CREW_SERVER_RE = re.compile(r"server\.py|claude_crew|[\\/ ]crew(\.exe)?(\s|$)")


def orphans_to_kill(entries: list[dict], cmdlines: dict[int, str], key: str = "") -> list[int]:
    """Recorded pids that are still alive (present in cmdlines) and still look like our agent processes."""
    out = []
    for e in entries:
        pid = e.get("pid") if isinstance(e, dict) else None
        if isinstance(pid, int) and pid > 0 and pid != os.getpid() and is_agent_cmdline(cmdlines.get(pid, ""), key):
            out.append(pid)
    return out


def pid_alive(pid: int, proc: subprocess.Popen | None = None) -> bool:
    if proc is not None:
        return proc.poll() is None
    if IS_WIN:
        try:
            r = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/NH", "/FO", "CSV"], capture_output=True, text=True, timeout=15)
            return f'"{pid}"' in r.stdout
        except (OSError, subprocess.SubprocessError):
            return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except OSError:
        return True
    return True


def signal_tree(pid: int, sig: int) -> None:
    try:
        os.killpg(pid, sig)  # agents start their own session, so the pid is the group id
    except ProcessLookupError:
        pass
    except OSError:
        try:
            os.kill(pid, sig)
        except OSError:
            pass


def terminate_tree(pid: int, proc: subprocess.Popen | None = None, grace: float = 3.0) -> str | None:
    """Stop a process and its children. POSIX: SIGTERM to the group, wait up to `grace` s, then SIGKILL.
    Windows: taskkill /T /F. Returns None on success, else why it failed."""
    if IS_WIN:
        try:
            r = subprocess.run(["taskkill", "/T", "/F", "/PID", str(pid)], capture_output=True, text=True, timeout=30)
        except (OSError, subprocess.SubprocessError) as e:
            return f"taskkill could not run: {e}"
        if r.returncode != 0 and pid_alive(pid, proc):
            return f"taskkill exited {r.returncode}: {(r.stderr or r.stdout).strip()[:200]}"
        return None
    signal_tree(pid, signal.SIGTERM)
    end = time.time() + grace
    while time.time() < end:
        if proc is not None:
            try:
                proc.wait(timeout=0.1)
                return None
            except subprocess.TimeoutExpired:
                continue
        if not pid_alive(pid):
            return None
        time.sleep(0.1)
    signal_tree(pid, signal.SIGKILL)
    if proc is not None:
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            return "still running after SIGKILL"
    return None


class StdinWriter:
    """Feeds one process's stdin from its own thread so a hung pipe can never block the server or its lock."""

    def __init__(self, p: subprocess.Popen):
        self.p, self.q, self.since, self.error = p, queue.Queue(), None, None
        threading.Thread(target=self._run, daemon=True).start()

    def put(self, line: str) -> None:
        self.q.put(line)

    def close(self) -> None:
        self.q.put(None)

    def stuck(self, limit: float | None = None) -> bool:
        return self.since is not None and time.time() - self.since > (STDIN_LIMIT if limit is None else limit)

    def _run(self) -> None:
        while (line := self.q.get()) is not None:
            self.since = time.time()
            try:
                self.p.stdin.write(line)
                self.p.stdin.flush()
            except (OSError, ValueError) as e:  # broken pipe, or the process was killed mid-write
                self.error, self.since = str(e) or type(e).__name__, None
                return
            self.since = None
