"""Trigger kinds: one module per kind, found and registered here. See README.md in this folder to add one."""
from __future__ import annotations

import importlib
import pkgutil

from .base import MAX_CHAIN, REGISTRY, Trigger, get, kinds, now, register, short_title  # noqa: F401

for _m in pkgutil.iter_modules(__path__):  # dropping a module in this folder is all it takes
    if not _m.name.startswith("_") and _m.name != "base":
        importlib.import_module(f"{__name__}.{_m.name}")
