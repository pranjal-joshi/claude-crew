"""Small helpers shared by the agent modules and server.py: git, and atomic file writes."""
from __future__ import annotations

import os
import shutil
import subprocess
import time
from pathlib import Path


def git(cwd: Path, *args: str, check: bool = False) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-c", "core.longpaths=true", *args], cwd=cwd, capture_output=True,
                          text=True, encoding="utf-8", errors="replace", check=check)


def is_git(path: Path) -> bool:
    return git(path, "rev-parse", "--is-inside-work-tree").stdout.strip() == "true"


def write_atomic(path: Path, text: str, bak_every: float = 0) -> None:
    """Write via a temp file in the same folder and os.replace, keeping the previous file as <name>.bak
    (refreshed at most every bak_every seconds)."""
    tmp, bak = path.with_name(path.name + ".tmp"), path.with_name(path.name + ".bak")
    tmp.write_text(text, encoding="utf-8")
    try:
        if path.exists() and (not bak.exists() or time.time() - bak.stat().st_mtime >= bak_every):
            shutil.copyfile(path, bak)
    except OSError:
        pass
    os.replace(tmp, path)
