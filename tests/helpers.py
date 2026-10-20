"""Shared setup: import server.py from the repo root and keep all state in a temp dir."""
import copy
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import server  # noqa: E402


class TempCrewCase(unittest.TestCase):
    """A Crew built on a temp (non-git) project. WORK_ROOT is redirected so ~/.crew is never touched.
    The claude binary is a placeholder that is never launched."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        tmp = Path(self._tmp.name)
        self._old_root = server.WORK_ROOT
        server.WORK_ROOT = tmp / "work"
        self.addCleanup(setattr, server, "WORK_ROOT", self._old_root)
        self.project = tmp / "project"
        self.project.mkdir()
        self.cfg_path = tmp / "crew.json"
        cfg = copy.deepcopy(server.DEFAULT_CONFIG) | {"workflows": copy.deepcopy(server.DEFAULT_WORKFLOWS)}
        self.cfg_path.write_text(json.dumps({"permission_mode": "acceptEdits"}), encoding="utf-8")
        self.crew = server.Crew(self.project, cfg, ["claude-not-used"])
        self.crew.config_path = self.cfg_path
