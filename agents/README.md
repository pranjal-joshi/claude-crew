# Agents

What lives here: everything about an agent that is not the board. `server.py` keeps the HTTP layer and the Crew core (lock, tasks, board, events, alerts, snapshot, saving); the Crew keeps one-line methods (`crew._start`, `crew._send`, `crew._kill_proc`, ...) that call these modules, so the rest of the server and the tests are unchanged. Functions take the Crew as their first argument and run with its lock held unless noted.

| File | What it holds |
|---|---|
| `record.py` | An agent's dict (`new_agent`), the allowed models, efforts and contexts, validation (`NAME_RE`, `ROLE_MAX`, `clean_tags`), and the roster actions: `configure`, `add`, `rename`, `remove`, `reorder`, `import_agents`. |
| `launch.py` | Worktrees, building the `claude` command line and starting the process, `send`, `kill_proc`, the pidfile and orphan reaping, per-task sessions (`task_session`, `forget_sessions`), `stop_agent`, and the kill / release / clear chat / respawn actions. |
| `procs.py` | Process helpers with no Crew in them: the stdin writer thread, process-tree termination, command-line checks. Tests patch `agents.procs.terminate_tree` and friends. |
| `files.py` | The agent's Claude Code environment: untracked `.claude` files synced into a worktree, local MCP servers, the slash-command catalog. |
| `lanes.py` | Parallel lanes: an agent runs up to `max_parallel` tasks at once, one Claude process, worktree and branch per lane. Lane 1 is the agent's own record; lane n is a record of the same shape keyed `<name>.<n>` (`find`, `of`, `holder`, `new`, `ensure`, `free`, `room`, `for_user`, and `view` for the board). A card in Review holds its lane only while its uncommitted changes are in that worktree. |
| `health.py` | Self-checks the dispatcher runs every second (`check`: repairs states nothing should leave behind, once they stay wrong for `REPAIR_AFTER` seconds) and automatic retries of turns that failed for a passing reason (`schedule_retry`, `tick_retries`, `succeeded`). |
| `util.py` | `git()`, `is_git()` and `write_atomic()`. |
| `presets/` | The built-in crew, one module per agent. |

Processes, pids and worktrees are keyed by a lane's `key` (the agent's name for lane 1), never by its `name`, which all lanes of an agent share. A task records the lane it ran on (`lane`, `cwd`, `branch`): approve, diff, undo, merge and PR read them from the task.

Still in `server.py`: the stream-json reader (`_read`, `_on_event`, `_on_result`), the permission inbox and the budgets. They update tasks, costs and alerts together, so they stay with the board.

## Add a preset

Drop `agents/presets/<name>.py`; nothing else changes. Modules whose names start with `_` are skipped.

```python
"""The security preset."""

ORDER = 55   # where it sits in the default crew; presets sort by ORDER, then name
AGENT = {"name": "security", "tags": ["security", "audit"], "model": "opus", "effort": "high",
         "role": "Reviews changes for vulnerabilities. Reports findings ranked by severity, each with file:line."}
```

`AGENT` has the same shape as an entry in `crew.json`'s `"agents"` list (`name`, `role`, `tags`, `model`, `effort`, and optionally `icon`, `color`, `context`). It is only the starting crew: a `crew.json` that already has agents is never overwritten. Workflow templates refer to preset agents by name, so keep a template's agents in the preset list (a test checks this).
