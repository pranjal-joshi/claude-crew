# Changelog

All notable changes to this project are documented here. The format follows [Keep a Changelog](https://keepachangelog.com/), and versions follow [Semantic Versioning](https://semver.org/).

## [Unreleased]

### Added
- **Delete a whole workflow run from the board:** a stacked run card's menu has *Delete the whole run · N cards*, which stops a running step and removes every step's card at once; *Delete just this step* keeps the old behaviour.
- **Watches on ⟳ steps:** the step's agent gives Crew one read-only command (crew tool `watch`: `gh pr view/checks`, `gh run`, `gh api` GET, `az pipelines runs/build`, `az repos pr`, `az boards work-item show`, `git log/ls-remote/rev-parse/merge-base`) and an optional `done` regex. Crew runs it on the step's schedule without the model and wakes the agent, with the new output, only when it changes or matches. A long wait costs a few turns instead of one per interval. No shell: pipes, redirects and `gh api` writes are refused.
- **⟳ Check-until-done gate for workflow steps:** for goals that take hours or days (a PR merging, a deployment landing). The step's agent works, then Crew asks it to check again every N minutes (30 by default) in the same session, with a short message instead of the whole brief, until it calls `decide`: continue passes the step, stop ends the run. After the set number of checks (96 by default) it goes to Review. Set it on a step in the workflow editor (the gate badge now cycles ✋ → ⚡ → ⟳), from the boss's `save_workflow`, or in an exported file. A waiting card shows ⟳ with its next check.
- **PRs named by number become links:** "PR #123" in an agent's brief or summary links to the project's GitHub or Azure Repos PR (read from the `origin` remote), so the card gets its PR and ↻ Address review buttons even when the agent never pasted a URL.
- **Approve from the board:** a review card has a solid ✓ Approve button (with undo) above Review changes.
- **Mermaid diagrams:** a ```` ```mermaid ```` block in a chat reply, an agent log or a review summary is drawn as a diagram, in the current theme. The Mermaid library loads from jsDelivr the first time a diagram appears; if it can't load, or the diagram is invalid, the source stays visible.

### Fixed
- **Review summaries render Markdown:** the agent summary in the approval panel showed raw Markdown; it now renders like the chat (headings, lists, tables, code, links).
- **☕ disappeared after switching projects** although the computer was still being kept awake; the new project's crew now picks up the keep-awake state.

### Changed
- **Toasts sit at the bottom right**, just above the chat bar's Send button, instead of under the header.
- **Keep-awake is on by default:** `--awake` now defaults to `always`, so the computer doesn't idle-sleep while Crew runs and scheduled triggers fire on time, with ☕ in the header the whole time. `--awake busy` brings back the old behavior (only while agents work); `--awake off` turns it off.

## [0.3.0] - 2026-10-22

### Added
- **Export and import workflows and triggers** (header ⋮ menu, and **Export… / Import…** on the Workflows and Triggers tabs), next to the agent ones: one dialog, three kinds. Files are `{"crew": "workflows" | "triggers", "version": 1, "items": [...]}`. Trigger files never hold a webhook token (a new one is made on import) or what a trigger fired, and imported triggers arrive **turned off**. A workflow step that names an agent you do not have is flagged; a trigger aimed at a missing workflow or agent is skipped. Existing names are skipped or updated, as for agents.
- **Pipeline triggers fire on several results:** pick any of succeeds, partially succeeds, fails, is canceled, or finishes; a run with any of them fires it. Triggers saved with one result keep working.
- **Scheduled jobs on the board:** every trigger (cron, event, webhook, pipeline, Teams, ADO, poll, watch) shows at the top of the Backlog, tagged *scheduled* and its kind, soonest first, with its next time. Click one to edit it, ▶ to fire it now, or drag it to In progress to run it: it stays there while its check, workflow run or task is in progress, then goes back to the Backlog.
- **Workflow runs grouped on the board:** each run is one frame in the column of its current step, with a stepper; its other steps show as one-line rows inside it, and ▾ collapses it. Tasks outside a workflow are unchanged.
- **Screenshots in chat:** paste, drop or attach up to 4 images (PNG, JPEG, GIF, WebP, 5 MB each) with a message to the boss or an agent.
- **Turn agents and workflows on and off:** a switch on every agent card and workflow card, also in their ⋮ menus. An agent that is off takes no new work: a task it is running finishes, tasks for it wait, and hand-offs to it are refused. A workflow that is off can't be run, and triggers that start it pause, so nothing they would pick up is lost. Agent cards no longer show a status pill; the avatar ring shows the status.
- **Ticketer agent and Request to ticket workflow:** a new built-in agent turns a request from anywhere (a Teams message or mention, an alert, a failed pipeline, a webhook, an earlier step) into a Bug or PBI, or links an open duplicate, then shares the link where the request came from. Point a Teams trigger at it, or use it as a step in any workflow.
- **Formatted replies:** the boss chat and agent logs render Markdown (headings, lists, tables, code blocks, links) plus a few plain HTML tags. Anything else, such as scripts or attributes, stays as text.
- **Agent status at a glance**: every agent avatar carries a status ring (none when idle, a spinning arc while running, blue to review, amber paused, red on error) and a small time tag: how long it has been running, or since it was last active. The agent panel's header is one row: avatar, name, a status line ("To review · #14 is done, needs you", "waiting for work · last active 4 min ago"), Stop/Resume/Respawn when they apply, an Edit | delete toolbar, ⋮ and close.
- **Many triggers, one workflow**: a workflow's ⚡ button, its editor and agent cards now list ADO tag and Pipeline triggers too, and a trigger opened from a workflow keeps it as the target when you switch to ADO or Pipeline. Several triggers (for example one per pipeline) can start the same workflow.
- **Pipeline trigger**: when an Azure Pipelines run of the named pipelines finishes (succeeds, partially succeeds, fails, or any of them), Crew starts an agent or a workflow, once per run. The task carries the run link, branch, commit, who it ran for and its linked work items, with yours first. **Only runs for me** keeps runs requested for you or carrying a work item assigned to you. Uses your az sign-in, read-only; **Test** lists recent matching runs.
- **Teams trigger**: when someone @mentions you, or posts, in a Teams channel or chat, Crew starts a task or a workflow run. It reads Teams through the Microsoft 365 connector in your Claude Code (a short, read-only Haiku run per check, allowed only those tools), so Crew holds no Microsoft tokens. Each message starts work once, with a short title, the sender, text and a link; **Test** lists the last 24 hours. A failing check raises an alert.
- **Gates in workflows and hand-offs**: a new crew tool, `decide`, lets an agent rule on the task it is running. *Continue* closes it without review and starts the next workflow step with its summary; *stop* closes it marked ⏹ stopped and ends the workflow run. Cards show ⏹ stopped or → passed with the reason, and the review panel shows who decided and why. Meant for a gate agent like **Picker** as the first step of an ADO-triggered workflow.
- **PR buttons on cards**: Crew now notices PRs that agents open or work on themselves (Shipper, CI-Review), from the task's brief and the agent's summary, GitHub and Azure DevOps. Any task with a PR gets **Open PR #n ↗** (opens it in a new tab) and **↻ Address review**: one click has that card's agent run the `/address-review` skill on the PR, in the task's own session, to fetch the review comments, fix them, reply and resolve. Address review shows only for agents that have the skill. When a PR already exists, the review panel's button to create one (now called **Create PR**) is hidden.
- **ADO tag trigger**: tag a work item on your Azure DevOps board with a tag you choose, and Crew starts a workflow run (or one agent's task) for it, once per item. It then adds `<tag>-picked`, removes the tag or leaves tags alone, and can post a comment. Uses your `az` login. A **Test** button lists matching items without starting anything. Cards and runs show an `AB#123` chip linking to the item. UI-only, like poll triggers.
- **Light theme** built from the Book Cloth, Kraft and Manilla palette, with a **System / Dark / Light** switch in the header ⋮ menu. System follows the OS live; the choice is remembered and applied before first paint.
- **Next check time** on ADO and poll triggers ("next check 11:42 (in 3 min)", "checking now…", "off"), computed by the server with the same rule the scheduler uses.
- **Alerts on the board**: agents call the new `raise_alert` crew tool when something is failing that you must see (a failed deployment, a red main-branch pipeline, an outage, expired access), and `resolve_alert` once it recovers. Open alerts sit in a strip above the board, worst first, with the agent, age, count, details and link; **Make task** turns one into an incident task in Backlog, **Ack** dims it, **Resolve** clears it. Repeats of the same alert bump a count instead of stacking, and a worse severity re-opens it. Crew raises its own alerts for a trigger whose check fails (closed by its next good check) and for the daily budget.
- **Continue in Claude Code** (card and agent ⋮ menus): the exact `cd <worktree>; claude --resume <session>` command to pick an agent's session up in your own terminal, with a button to close Crew's process first, plus a self-contained hand-off brief to copy or download for a fresh session. For work Crew can't finish alone: sign-ins, MFA, browsers, local devices, prod approvals.
- Forwarding or moving a card shows **Sending to X · starting its Claude session…** on the card right away, instead of nothing until the new session is up.
- **Export and import agents** (header ⋮ menu): download all or selected agents as JSON (settings only), and import a file with a preview, per-agent selection, and skip-or-update for names that already exist. Values from the file are validated on both sides; import is UI-only.
- **Resizable side panel**: drag the left edge of the agent / review panel (or focus it and use the arrow keys) to make it wider or narrower; double-click resets. The width is remembered.

### Fixed
- **Pipeline "Only runs for me" matched almost every run:** ADO links a build to every work item since the last fully successful build, so on a pipeline that keeps partly succeeding the same assigned items rode along on every run. A run now counts only the work items the previous run on its branch didn't have, and reads up to 500 linked items instead of ADO's default 50.

### Changed
- **Triggers is a tab:** it sits next to Board and Workflows (with a count of the ones that are on) instead of a chip in the top bar that opened a dialog. The list, presets and the new/edit form are the same, on a full page. <kbd>T</kbd> opens it (like <kbd>W</kbd> and <kbd>B</kbd>), <kbd>Esc</kbd> in the form goes back to the list, and on a phone it is in the bottom bar. The palette, scheduled cards on the Board, agent and workflow ⚡ buttons and `#tab=triggers` all land there.
- **Quieter agent cards:** an idle agent's card no longer shows a "waiting for work" line.
- **Teams triggers take a link:** paste a link to the channel, chat or any message in it. Crew finds messages with message search filtered to that conversation, which works without the Team.ReadBasic.All permission that looking a channel up by name needs.
- **Workflow steps start fresh:** every workflow step runs on a new Claude session, even for agents set to keep one session, so no run inherits another's context. Such an agent goes back to its own session afterwards, and a sent-back step resumes that step's session.
- **Steps get the whole previous summary:** a step's summary was cut to 3,000 characters before the next step saw it, and earlier steps to 400, so comment blocks an agent had to post verbatim arrived cut off. The previous step now passes on up to 20,000 characters and earlier steps up to 4,000.
- **✋ gates always wait for you:** an agent that ends a workflow step with the crew tool `decide` (continue) no longer skips that step's ✋ approval. The task goes to review with the agent's verdict shown, and the next step starts when you approve. On a ⚡ step, continue still goes straight on, and stop still ends the run.
- **Context bar uses the real window:** the agent panel's context bar reads the window size Claude Code reports (1M tokens today) instead of assuming 200k. Agent card buttons (Stop, Respawn, Resume) sit next to the on/off switch instead of over it.
- **Agents and workflows are modular**: agent records, process launch and sessions, and the roster actions moved to the new `agents/` package, and workflow definitions, runs, gates and delivery to `workflows/`. The built-in crew and workflow templates are one drop-in module each (`agents/presets/`, `workflows/templates/`). `server.py` keeps the HTTP layer and the Crew core and delegates. No change in behavior, the HTTP/MCP API or the `crew.json` format.
- **Triggers are modular**: each kind (schedule, event, watch, poll, webhook, ado, teams, pipeline) now lives in its own module in the new `triggers/` package, found through a registry, so a new kind is one dropped-in file (see `triggers/README.md`). `server.py` no longer lists kinds or branches on them. A general `/api/trigger/<kind>` route serves the form helpers; `/api/ado` and `/api/teams` stay as aliases. No change in behavior or in the `crew.json` format.
- **Triggers dialog**: the trigger type is a dropdown (same icons), and the dialog fits the window and scrolls instead of squashing its fields. Switching away from Teams no longer leaves its task text behind.
- **Short card titles**: tasks, hand-offs and runs created by agents or the boss must have short titles (about 6 words, no links), so cards read at a glance; Crew refuses longer ones and asks for the rest in details. ADO-triggered tasks show `AB#id` plus the first 6 words of the item's title, with the full title at the top of the details. Titles you type are never changed.
- **Delete agent** (was Remove): the dialog now opens with Cancel focused, so a stray Enter doesn't delete. The same goes for every other delete or stop dialog.
- **Agent panel**: the Details fold-out is gone; its branch, model, tokens, skills/MCP and session line now sits at the top of the **✎ Edit** panel (was ⚙ Model & role). A bin icon next to Edit deletes the agent (Remove agent left the panel). The Edit panel scrolls on its own, so the message box below it stays visible.
- **Board**: the row of tag filters is gone, and **＋ New task** moved from the header into the Backlog column (<kbd>N</kbd> still works). Any card that mentions `AB#123` links to the work item in a new tab, once Crew knows your board URL (from an ADO trigger, an item it fetched, or `az devops configure` defaults).
- **Boss chat**: the ▴/▾ toggle is gone. The conversation opens when you focus the input and closes with **Hide**, <kbd>Esc</kbd> or a click elsewhere; while it is closed, a one-line peek shows a new reply.
- **Workflows page**: **Workflows** and **Runs** tabs. Runs lists every run; select one to see each step's agent, task, state, decision and summary. **＋ New workflow** uses the accent colour. The Auto Workflow Builder box is removed: ask the boss in chat instead.
- **Each task keeps its own Claude session** (agents on "Fresh per task"). Messages, **Send back** and re-running a task the agent already worked on continue that task's conversation, even after the agent has done other tasks since. Only a new task (from you, the boss or a trigger) starts a fresh session. Clear chat, a fresh respawn and renaming an agent forget its saved sessions.
- **Messaging an agent whose task waits in Review moves the card back to In progress** until its turn ends, then it returns to Review with the new summary and diff. Approved tasks stay where they are.
- Deleting a trigger uses a red bin icon instead of ✕ (same confirm).
- Switching projects shows an "Opening <project>…" overlay at once instead of looking frozen while the old crew shuts down.
- The agent **Role** box (settings and Add agent) grows to fit its text and can be resized, instead of a two-line box you had to scroll.
- Notifications no longer repeat: an identical toast already on screen is refreshed instead of stacked (an action's own toast and the server's event for it show once), at most 4 show at a time, and editing an agent's role, tags, icon or colour posts one event naming what changed instead of repeating its model line, with none when nothing changed.
- Agent roles can be up to 4000 characters (was 500, and longer roles were cut off silently).
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
[0.3.0]: https://github.com/pranjal-joshi/claude-crew/compare/v0.2.0...v0.3.0
