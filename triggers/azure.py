"""Shared Azure CLI helpers for the ado and pipeline triggers: `az` is always run with an argument list, never a shell."""
from __future__ import annotations

import json
import re
import shutil
import subprocess

AZ_UNSAFE = re.compile(r'[%^&|<>"!`\x00-\x1f]')  # az is a .cmd file on Windows: cmd.exe would read these as syntax


class AzError(Exception):
    pass


def az_hint(err: str) -> str:
    low = err.lower()
    if "az login" in low or "aadsts" in low or "tf400813" in low or "not authorized" in low or "authentication" in low:
        return "not logged in: run `az login`"
    if "is not in the 'az' command group" in low or "'boards' is misspelled" in low or ("azure-devops" in low and "extension" in low):
        return "azure-devops extension missing: `az extension add --name azure-devops`"
    if "--organization must be specified" in low:
        return "no organization: fill in the Organization URL"
    return (err.strip().splitlines() or ["az failed"])[-1][:300]


def az(*args: str, raw: bool = False, timeout: int = 90):
    """Run `az` with an argument list (never a shell) and return its parsed JSON, or its text with raw=True.
    An empty result prints nothing, which comes back as None."""
    exe = shutil.which("az")
    if not exe:
        raise AzError("az not found: install the Azure CLI (https://aka.ms/installazurecli)")
    for a in args:
        if AZ_UNSAFE.search(a):
            raise AzError(f"unsupported character in {a[:40]!r}")
    try:
        r = subprocess.run([exe, *args], capture_output=True, text=True, encoding="utf-8", errors="replace",
                           timeout=timeout, stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired:
        raise AzError(f"az timed out after {timeout} s") from None
    except OSError as e:
        raise AzError(f"az could not start: {e}") from None
    if r.returncode:
        raise AzError(az_hint(r.stderr or r.stdout))
    if raw:
        return r.stdout
    try:
        return json.loads(r.stdout) if r.stdout.strip() else None
    except ValueError:
        raise AzError("az printed something that is not JSON") from None


def ado_defaults() -> dict:
    """Organization and project from `az devops configure --list` (lines like `Organization = https://...`)."""
    out: dict = {}
    for line in az("devops", "configure", "--list", raw=True).splitlines():
        k, _, v = line.partition("=")
        k = k.strip().lower()
        if k in ("organization", "project") and v.strip():
            out["org" if k == "organization" else "project"] = v.strip()
    return out


_me: str | None = None


def az_me() -> str:
    """The az sign-in's user name, lower case (compared locally with who a run was requested for, never sent anywhere)."""
    global _me
    if _me is None:
        _me = str(az("account", "show", "--query", "user.name", "--output", "tsv", raw=True) or "").strip().lower()
    return _me
