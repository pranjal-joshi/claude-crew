"""Built-in workflow templates: one module per workflow, each with WORKFLOW (a crew.json workflow entry, with a fixed
"id") and ORDER. Drop a module in this folder to add a template; modules whose names start with "_" are skipped."""
from __future__ import annotations

import copy
import importlib
import pkgutil


def load() -> list[dict]:
    """The template workflows, in ORDER."""
    mods = [importlib.import_module(f"{__name__}.{m.name}") for m in pkgutil.iter_modules(__path__) if not m.name.startswith("_")]
    return [copy.deepcopy(m.WORKFLOW) for m in sorted(mods, key=lambda m: (m.ORDER, m.WORKFLOW["id"]))]
