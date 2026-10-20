# Changelog

All notable changes to this project are documented here. The format follows [Keep a Changelog](https://keepachangelog.com/), and versions follow [Semantic Versioning](https://semver.org/).

## [Unreleased]

### Added
- Agent icons (24 dev line icons) and background colours; the glyph turns black or white for contrast.
- Keep-awake while agents work (`--awake busy|always|off`, default `busy`), like the Claude desktop app: a locked screen never paused Crew, but idle sleep did. ☕ in the header while it holds.
- Case-only agent renames (designer → Designer) carry the worktree and branch along.
- ⋮ menu on every agent: rename (moves its worktree and branch, updates tasks, triggers and workflows), settings, stop, kill, delete.

### Changed
- Cards get a forward button (hand off to an agent) next to an always-visible ⋮ (move, mark done, delete); hand-offs show a forward arrow.

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
