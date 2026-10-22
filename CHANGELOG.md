# Changelog

All notable changes to this project are documented here. The format follows [Keep a Changelog](https://keepachangelog.com/), and versions follow [Semantic Versioning](https://semver.org/).

## [Unreleased]

### Added
- **ADO tag trigger**: tag a work item on your Azure DevOps board with a tag you choose, and Crew starts a workflow run (or one agent's task) for it, once per item. It then adds `<tag>-picked`, removes the tag or leaves tags alone, and can post a comment. Uses your `az` login. A **Test** button lists matching items without starting anything. Cards and runs show an `AB#123` chip linking to the item. UI-only, like poll triggers.
- **Light theme** built from the Book Cloth, Kraft and Manilla palette, with a **System / Dark / Light** switch in the header ⋮ menu. System follows the OS live; the choice is remembered and applied before first paint.
- **Next check time** on ADO and poll triggers ("next check 11:42 (in 3 min)", "checking now…", "off"), computed by the server with the same rule the scheduler uses.
- **Resizable side panel**: drag the left edge of the agent / review panel (or focus it and use the arrow keys) to make it wider or narrower; double-click resets. The width is remembered.

### Changed
- Deleting a trigger uses a red bin icon instead of ✕ (same confirm).
- Switching projects shows an "Opening <project>…" overlay at once instead of looking frozen while the old crew shuts down.
- Board card footers wrap and long agent names are shortened with an ellipsis instead of running off the card.

## [0.2.0] - 2026-10-21

The review-to-merge loop, plus safety and polish.

### Added
- Review decision sheet: a verdict line (files, +/-, tests), a pinned **Approve & commit**, a per-file diff, and `A` to approve with an undo toast.
- One-click **Merge** of an approved branch into the base branch, or **Open PR** with `gh` when there is a remote. On a conflict it lists the files and offers to send the task back to the agent. Done cards show merged or not merged.
- First-run card on an empty board and a **Demo** badge that tells demo and real projects apart.
- Blocked inbox: tool calls the permission mode refused show as a "N blocked" chip, a list and a drawer section. **Allow & retry** adds an editable rule (such as `Bash(npm test:*)`) to `allowed_tools` and restarts the agent with the session kept; **Allow once** keeps it until the task ends; **Dismiss** clears it.
- Cost on every task card, and budgets per task and per day (`budget_task_usd`, `budget_day_usd`). A task at its budget is flagged and can't be sent back; a spent daily budget stops running agents until the next local day or a higher budget.
- Header summary pill, a ⋮ header menu (Phone, version and more) and a **Permissions: Auto** chip. Agent drawer **Details**, Kill behind a confirm, tooltips and a gate legend for ✋ / ⚡.
- Agent ⋮ menu (rename, settings, stop, kill, delete). Rename moves the worktree and branch and updates tasks, triggers and workflows; case-only renames work.
- Agent icons (24 dev line icons) and background colours.
- Card forward button (hand off to an agent), next to an always-visible ⋮.
- Keep-awake while agents work (`--awake busy|always|off`, default `busy`), with ☕ in the header.
- Workflows page: ⋮ menu with delete, and a one-line Auto Workflow Builder.
- Accessibility: focus ring, aria-labels, keyboard-operable cards and chips, reduced-motion support, better small-text contrast.
- Agent processes are recorded in `~/.crew/pids/<project>.json`; leftovers from a crashed run are ended at startup.
- Tests for git integration, the auth matrix and bad webhook tokens.

### Changed
- Clear buttons have specific names: **Clear board** and **Clear chat**.
- Token totals are labelled "this session"; agent card numbers are labelled.
- The header shows one running/review summary; Stop all appears only while something runs.
- Clicking a board card opens the agent that handled it.

### Fixed
- Approve, undo, merge, Open PR and diff no longer run git under the global lock, so the live stream stays responsive. The task shows as busy and a second click is refused.
- A hung agent stdin can no longer block Crew; a process that stops reading is ended with its session kept.
- Stopping an agent sends SIGTERM to its process group and SIGKILL after 3 s on macOS and Linux; on Windows a failed `taskkill` is logged.
- An agent's branch is reused when its worktree folder is missing, instead of being reset and orphaning approved commits.
- A corrupt state file is renamed to `.corrupt` and reported, instead of silently starting empty.

### Security
- Synced local Claude files (`.claude/settings.local.json`, `.mcp.json`, `CLAUDE.local.md`) are kept out of commits.
- Poll triggers (shell commands) can only be created from the UI, after a confirm that shows the command. The boss can't create them.
- Per-run caller tokens (UI and MCP) on `POST /api/act/*`; the MCP token is limited to an allowlist of crew actions.
- With `--lan`, clients can't browse folders or switch projects.
- `crew.json` and state are written atomically with a `.bak`; the webhook `Content-Length` is guarded.

## [0.1.0] - 2026-10-20

First packaged release. `pipx install claude-crew` gives a `crew` command; `python server.py` from a clone still works.

### Added
- A local web console for a crew of Claude Code agents (`claude -p`, stream-json), each in its own git worktree and branch.
- An SDLC crew by default: planner, researcher, designer, builder, tester, reviewer, evaluator, ops and scribe. Add, remove, re-role and reorder agents from the UI; model and effort per agent.
- Kanban board with a review gate (summary and colored diff, approve and commit, send back, or hand off), drag-to-hand-off, and live subagent indicators.
- Workflows: chain agents in order with an approval or auto gate per step, a canvas editor, and outputs (board, markdown report, webhook, or the next workflow).
- Triggers: schedules, task events, file watches, command polls and webhooks, with presets.
- Boss chat: a chat bar that runs the crew (tasks, workflows, agents, triggers) from plain requests; agents get a `handoff` tool through the bundled `crew_mcp.py` MCP server.
- Stop, kill and clear controls; per-task fresh context or one kept session; context meter with Compact and New session; Clear chat.
- A `/` picker in every chat box: Crew's `/clear` `/compact` `/stop` plus the agent's Claude Code commands and skills with descriptions.
- Collapsible agents pane (avatars and status dots), and a Clear button on the open chat panel.
- Permission modes (Accept edits, Auto, Bypass), token and cost tracking, multiple projects.
- `--lan` mode with a key and QR code for phones and tablets, with an app-like mobile layout.
- `--demo` mode with simulated agents: no Claude CLI, no tokens.
- `crew` console script, `--version` flag, and the version shown in the header.
- Unit and smoke tests, ruff lint config, CI on Linux, Windows and macOS (Python 3.10 to 3.13), and a tag-to-release pipeline.

### Changed
- When installed from a package, the default `crew.json` is read from (and created in) the current folder instead of next to `server.py`. From a clone, behavior is unchanged.

[0.1.0]: https://github.com/pranjal-joshi/claude-crew/releases/tag/v0.1.0
[0.2.0]: https://github.com/pranjal-joshi/claude-crew/compare/v0.1.0...v0.2.0
