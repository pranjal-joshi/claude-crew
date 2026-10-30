"""Small helpers shared by the agent modules and server.py: git, and atomic file writes."""
from __future__ import annotations

import os
import shutil
import subprocess
import threading
import time
from pathlib import Path


def git(cwd: Path, *args: str, check: bool = False) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-c", "core.longpaths=true", *args], cwd=cwd, capture_output=True,
                          text=True, encoding="utf-8", errors="replace", check=check)


def is_git(path: Path) -> bool:
    return git(path, "rev-parse", "--is-inside-work-tree").stdout.strip() == "true"


def write_atomic(path: Path, text: str, bak_every: float = 0) -> None:
    """Write via a temp file in the same folder and os.replace, keeping the previous file as <name>.bak
    (refreshed at most every bak_every seconds). Each thread writes its own temp file, so two saves at once
    don't rename one file twice."""
    tmp = path.with_name(f"{path.name}.{os.getpid()}-{threading.get_ident()}.tmp")
    bak = path.with_name(path.name + ".bak")
    tmp.write_text(text, encoding="utf-8")
    try:
        if path.exists() and (not bak.exists() or time.time() - bak.stat().st_mtime >= bak_every):
            shutil.copyfile(path, bak)
    except OSError:
        pass
    for i in range(5):
        try:
            os.replace(tmp, path)
            return
        except PermissionError:  # Windows: another save is replacing the same file right now
            if i == 4:
                raise
            time.sleep(0.05 * (i + 1))
