"""Git integration on a real temp repo: worktrees, approve, rename, remove. No claude binary is ever started."""
import contextlib
import copy
import io
import json
import shutil
import unittest
from pathlib import Path

from tests.helpers import TempCrewCase, server

G = ["-c", "user.name=t", "-c", "user.email=t@localhost"]


def run(cwd, *args):
    r = server.git(Path(cwd), *args)
    assert r.returncode == 0, (args, r.stderr)
    return r.stdout.strip()


class GitCrewCase(TempCrewCase):
    def setUp(self):
        super().setUp()
        (self.project / "README.md").write_text("hi\n", encoding="utf-8")
        run(self.project, "init", "-q")
        run(self.project, "add", "-A")
        run(self.project, *G, "commit", "-qm", "init")
        cfg = copy.deepcopy(server.DEFAULT_CONFIG) | {"workflows": copy.deepcopy(server.DEFAULT_WORKFLOWS)}
        self.crew = server.Crew(self.project, cfg, ["claude-not-used"])  # rebuilt now that the project is a repo
        self.crew.config_path = self.cfg_path
        self.assertTrue(self.crew.git)
        self.agent = self.crew.agents["builder"]
        self.wt = server.WORK_ROOT / "wt" / self.crew.key / "builder"

    def commit_in_wt(self, name="work.txt", text="x\n"):
        (self.wt / name).write_text(text, encoding="utf-8")
        run(self.wt, "add", "-A")
        run(self.wt, *G, "commit", "-qm", "work")
        return run(self.wt, "rev-parse", "HEAD")

    def branch_exists(self, name):
        return server.git(self.project, "rev-parse", "--verify", "-q", f"refs/heads/{name}").returncode == 0


class Worktrees(GitCrewCase):
    def test_creates_branch_and_worktree(self):
        self.crew._ensure_worktree(self.agent)
        self.assertEqual(self.agent["branch"], "crew/builder")
        self.assertEqual(Path(self.agent["cwd"]), self.wt)
        self.assertTrue((self.wt / ".git").exists())
        self.assertTrue(self.branch_exists("crew/builder"))

    def test_missing_folder_keeps_approved_commits(self):
        self.crew._ensure_worktree(self.agent)
        sha = self.commit_in_wt()
        shutil.rmtree(self.wt)  # the folder vanishes, the registration and branch stay
        self.agent.update(branch=None, cwd=str(self.project))  # as after a restart
        self.crew._ensure_worktree(self.agent)
        self.assertEqual(self.agent["branch"], "crew/builder")
        self.assertEqual(run(self.project, "rev-parse", "crew/builder"), sha)
        self.assertTrue((self.wt / "work.txt").exists())

    def test_missing_folder_after_prune(self):
        self.crew._ensure_worktree(self.agent)
        sha = self.commit_in_wt()
        shutil.rmtree(self.wt)
        run(self.project, "worktree", "prune")
        self.agent["branch"] = None
        self.crew._ensure_worktree(self.agent)
        self.assertEqual(run(self.wt, "rev-parse", "HEAD"), sha)


class SyncedFiles(GitCrewCase):
    def setUp(self):
        super().setUp()
        (self.project / ".claude").mkdir()
        (self.project / ".claude" / "settings.local.json").write_text("{}", encoding="utf-8")
        (self.project / ".mcp.json").write_text('{"mcpServers": {}}', encoding="utf-8")
        self.crew._ensure_worktree(self.agent)

    def exclude_lines(self):
        out = run(self.wt, "rev-parse", "--git-path", "info/exclude")
        return (self.wt / out).read_text(encoding="utf-8").splitlines()

    def test_synced_paths_are_excluded_once(self):
        copied = server.sync_claude_files(self.project, self.wt)
        self.assertEqual(sorted(copied), [".claude/settings.local.json", ".mcp.json"])
        server.sync_claude_files(self.project, self.wt)  # idempotent, even when nothing is copied again
        lines = self.exclude_lines()
        for want in ("/.claude/settings.local.json", "/.mcp.json"):
            self.assertEqual(lines.count(want), 1)
        self.assertEqual(run(self.wt, "status", "--porcelain"), "")

    def approve(self):
        t = {"id": 1, "title": "Task", "details": "", "tag": "feature", "col": "review", "who": "builder", "summary": "done"}
        self.crew.tasks.append(t)
        with self.crew.lock:
            self.crew._approve(t)
        return t

    def test_approve_does_not_commit_synced_files(self):
        server.sync_claude_files(self.project, self.wt)
        (self.wt / "feature.py").write_text("print(1)\n", encoding="utf-8")
        t = self.approve()
        self.assertTrue(t["commit"])
        files = run(self.wt, "show", "--name-only", "--format=", "HEAD").splitlines()
        self.assertEqual(files, ["feature.py"])

    def test_approve_unstages_synced_files_already_staged(self):
        server.sync_claude_files(self.project, self.wt)
        (self.wt / "feature.py").write_text("print(1)\n", encoding="utf-8")
        run(self.wt, "add", "-f", ".mcp.json")  # even if one slipped into the index
        self.approve()
        files = run(self.wt, "show", "--name-only", "--format=", "HEAD").splitlines()
        self.assertEqual(files, ["feature.py"])


class RenameRemove(GitCrewCase):
    def test_rename_moves_worktree_and_branch(self):
        self.crew._ensure_worktree(self.agent)
        sha = self.commit_in_wt()
        self.assertEqual(self.crew.act("rename_agent", {"agent": "builder", "name": "coder"}), {"ok": True})
        a = self.crew.agents["coder"]
        self.assertEqual(a["branch"], "crew/coder")
        self.assertTrue((server.WORK_ROOT / "wt" / self.crew.key / "coder" / "work.txt").exists())
        self.assertEqual(run(self.project, "rev-parse", "crew/coder"), sha)
        self.assertFalse(self.branch_exists("crew/builder"))

    def test_remove_keeps_the_branch(self):
        self.crew._ensure_worktree(self.agent)
        sha = self.commit_in_wt()
        self.assertEqual(self.crew.act("remove_agent", {"agent": "builder"}), {"ok": True})
        self.assertNotIn("builder", self.crew.agents)
        self.assertEqual(run(self.project, "rev-parse", "crew/builder"), sha)
        saved = json.loads(self.cfg_path.read_text(encoding="utf-8"))
        self.assertNotIn("builder", [a["name"] for a in saved["agents"]])


class Persistence(TempCrewCase):
    def test_corrupt_state_is_moved_aside_and_crew_starts_empty(self):
        self.crew.act("add", {"title": "keep me"})
        self.crew.state_file.write_text("{not json", encoding="utf-8")
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            c = server.Crew(self.project, json.loads(json.dumps(server.DEFAULT_CONFIG)), ["claude-not-used"])
        self.assertEqual(c.tasks, [])
        self.assertFalse(c.state_file.exists())
        moved = list(c.state_file.parent.glob(c.state_file.name + ".corrupt-*"))
        self.assertEqual(len(moved), 1)
        self.assertEqual(moved[0].read_text(encoding="utf-8"), "{not json")
        self.assertIn("unreadable", err.getvalue())
        self.assertTrue(any(e["kind"] == "error" and "corrupt" in e["text"] for e in c.feed))

    def test_state_and_config_writes_keep_a_backup(self):
        self.crew.act("add", {"title": "one"})
        self.crew.act("configure", {"agent": "builder", "role": "first"})
        self.crew.act("configure", {"agent": "builder", "role": "second"})
        bak = self.cfg_path.with_name(self.cfg_path.name + ".bak")
        self.assertEqual(next(a for a in json.loads(bak.read_text(encoding="utf-8"))["agents"] if a["name"] == "builder")["role"], "first")
        self.assertEqual(server.load_json_file(self.crew.state_file)["tasks"][0]["title"], "one")
        self.assertFalse(self.cfg_path.with_name(self.cfg_path.name + ".tmp").exists())


if __name__ == "__main__":
    unittest.main()
