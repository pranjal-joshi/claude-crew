"""Built-in agent presets: one module per agent, each with AGENT (a crew.json agent entry) and ORDER (its place in the
default crew). Drop a module in this folder to add a preset; modules whose names start with "_" are skipped."""
from __future__ import annotations

import copy
import importlib
import pkgutil


def load() -> list[dict]:
    """The preset agents, in ORDER."""
    mods = [importlib.import_module(f"{__name__}.{m.name}") for m in pkgutil.iter_modules(__path__) if not m.name.startswith("_")]
    return [copy.deepcopy(m.AGENT) for m in sorted(mods, key=lambda m: (m.ORDER, m.AGENT["name"]))]
