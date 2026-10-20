# Crew: work tracker

Legend: `[x]` done and tested · `[~]` in progress · `[ ]` pending · `[-]` dropped

## This batch (requests 3-12)

- [x] **Phone sign-in fix**: the QR link works when opened from a camera/QR app (no redirect, `SameSite=Lax`), friendly "needs a key" page
- [x] **3. Stop, quickly**: per-agent ■ Stop (session kept, task back to backlog *on hold*), header ■ Stop all (agents + workflow runs); explain Kill vs Stop
  - [x] server: `stop` pauses the agent (no new work until resumed) and holds its task; `stop_all`, `resume`, `resume_all`
  - [x] UI: ■ on cards and drawer, ■ Stop all / ▶ Resume in the header, ⏸ on-hold badge, Resume in the card menu
- [x] **4. Clear board**: clear Done, or clear everything (stops running work first)
  - [x] server: `clear` (scope done | all)
  - [x] UI: Clear ▾ menu with confirm
- [x] **5. Mobile, app-like**: bottom navigation (Board · Workflows · Agents · Boss), agents drawer, swipeable columns with tabs, bottom sheets for drawers/modals, + FAB, safe-area insets
- [x] **6. Triggers per agent / workflow**
  - [x] server: a trigger can start a workflow (`task.workflow`)
  - [x] UI: triggers listed + ⚡ Add trigger in agent settings, ⚡ on workflow cards, "Starts: a task / a workflow run" in the editor
- [x] **7. Boss chat**: bottom chat bar; talk to the boss (default) or any agent
  - [x] server: boss session (coordinates, never edits), crew MCP tools
  - [x] UI: chat bar, target picker, conversation panel; real boss test: crew_status + save_workflow worked; boss context cut from 84k to 11k tokens (~$0.09/turn)
- [-] **8. qmd memory**: dropped for now (admin policy blocks execution); qmd works on CPU (`QMD_FORCE_CPU=1`, ~6 s/query) if revisited
- [x] **9. Auto Workflow Builder**: describe a process in the Workflows tab, the boss creates/updates agents, workflows and triggers
- [x] **10. Context compaction**: context meter per agent, Compact (`/compact`) and New session buttons
  - [x] server: context size tracking, `compact`, compact_boundary handling; agents no longer restart after their first turn (kept the live context)
  - [x] UI: context meter, Compact, New session
- [x] **11. Capitalised agent names** (case-insensitive uniqueness; `boss` reserved)
- [x] **12. Auto hand-off as a tool**: agents get a `handoff` tool (teammate name or `auto`), boss can route; the manual hand-off menu stays
  - [x] server: `handoff` action, depth limit, crew MCP server (`crew_mcp.py`)
  - [x] UI: "Auto (boss picks)" in the hand-off menu, ↪ from <agent> on handed-off cards
- [x] Test on the demo + real boss calls, refresh the app, README, push
- [x] Refresh README screenshots for boss chat, slash picker and the phone layout (the demo GIF is unchanged)

## Batch: canvas editor and small asks

- [x] **Workflow canvas** (n8n-style): When (trigger) → one block per agent → Output; drag to reorder, drag agents from the palette or ＋ on a line to insert, ✋/⚡ toggles on lines, inspector panel, Del removes; phone: vertical canvas, Earlier/Later buttons
- [x] **Workflow output**: board (default), markdown report file, webhook (JSON), or start the next workflow; saved with the workflow, boss can set it too
- [x] **Click a board card** to open the agent that handled it
- [x] **Clear chat** (Clear button on the open chat panel): wipes the conversation and context of the boss or the selected agent
- [x] **Reorder agents** by dragging in the rail (saved to crew.json)
- [x] **Star history** chart in the README (Haiku subagent)
- [x] Refresh `assets/wf-edit.png` with the canvas editor

## Plan from the council review (architect · UX · PM · first-time user · AI evangelist)

**Positioning:** the review-gated control room for your Claude Code agents. Tasks go in, isolated branches come out, and you approve diffs instead of babysitting terminals.
**User:** an experienced Claude Code user juggling 2+ terminals. **Job:** hand off well-scoped tasks, walk away, and come back to diffs you can trust and merge in under a minute.
**Metrics (opt-in local counters, no telemetry):** time from install to first merged change (< 15 min) · first-pass approval rate (≥ 70%) and review time (< 60 s) · merged tasks per week and $ per merged task.

### Phase 1 · v0.1.1 safety hotfix (all small, ship first)
- [x] Reuse an agent's branch when its worktree folder is missing. Today `git worktree add -B … HEAD` (server.py:466) resets it and orphans approved, unmerged commits
- [x] Keep synced local Claude files (`.claude/settings.local.json`, `.mcp.json`, `CLAUDE.local.md`) out of commits by listing them in the worktree's `.git/info/exclude`. Approve runs `git add -A` today
- [x] Poll triggers (`shell=True`) can only be created from the UI, after a confirm that shows the command. The boss's `add_trigger` rejects `kind=poll`
- [x] A per-run token on mutating API calls (the page and crew_mcp get it), so other local processes and agents' Bash can't drive `/api/act`
- [x] For `--lan` requests, turn off `/api/browse` and `/api/project` (no browsing the host disk or switching projects from a phone)
- [x] Write `crew.json` and state atomically (temp file and replace, keep a `.bak`). Rename a corrupt state file to `.corrupt` and say so instead of silently starting empty. Guard the webhook `Content-Length`
- [x] Tests: git integration on a temp repo (approve, rename, remove, missing worktree), the auth matrix, bad webhook token

### Phase 2 · v0.2 core loop: from a queued task to a merged change
- [x] **Review as a decision sheet:** a verdict line (files · +/- · tests), Approve & commit pinned as the one primary, diff collapsed per file, Send back secondary, Hand off in ⋮, `A` approves with an undo toast. First check the "duplicated hunk" in review.png; it may just be demo data
- [x] **One-click merge** of approved branches into the base branch (or **Open PR** with `gh` when there is a remote). On a conflict, show the files and offer to send it back to the agent. Done shows merged / not merged
- [x] **First-run card** on an empty board: one example task, one primary action, a line on what the boss is. A clear **demo vs real project** badge
- [x] **Permission inbox:** show tool calls the permission mode refused (stream-json permission denials) with Allow once / Always allow, instead of a silently stuck agent
- [x] **Cost caps:** cost on every card, a budget per task and per day, and an auto-stop with a note when hit
- [x] Move git out of the global lock (approve, unapprove, merge, PR, diff). Record a pidfile per agent and reap orphans at startup; SIGTERM before SIGKILL
  - (still under the lock: worktree create/rename in `_ensure_worktree` and `rename_agent`, and the state write in `touch`)

### Phase 3 · v0.2 polish (do as Phase 2 lands)
- [ ] Header: one running/review summary (queued only if > 0); Phone and the version go into a menu; Stop all appears only while something runs; rename the "worktrees · Auto" chip to **Permissions: Auto**
- [ ] Agent drawer: role, stats and session in a collapsed **Details**; Kill behind ⋮ with a confirm; Compact and New session on the context meter
- [ ] Words: tooltips for boss, hand-off and the ✋/⚡ gates ("Waits for approval" / "Continues automatically"); label the numbers on agent cards and drop the unlabelled slot digit; mark token totals "this session"; give each Clear a specific name
- [ ] Accessibility: a global `:focus-visible` ring, aria-labels on icon buttons, keyboard-operable cards and chips, `prefers-reduced-motion`, and lift `--faint` contrast at small sizes
- [ ] Workflows page: ✕ delete moves into ⋮, the Auto Workflow Builder becomes a one-line input, and the header buttons wrap at ~800 px

### Next · v0.3 bets
- [ ] **Self-review critic** before the human gate: a cheap model reads the diff and test output and returns verdict + issues on the card, with one automatic send-back at most. Measure human rejection rate and review time
- [ ] **Model routing:** suggest a model per task (Haiku for triage, Opus for hard ones) and compare $ per merged task at an equal approval rate
- [ ] **Lessons:** append approve/reject reasons to a per-agent, user-editable `lessons.md` that the agent reads
- [ ] GitHub/ADO in and out: issues with a label become cards, approval opens a PR, CI failures go back to the agent
- [ ] UI system: one SVG icon set (no emoji), neutral tag colours with colour meaning status only, a tag filter dropdown, slimmer agent cards, 44 px phone targets
- [ ] Parallel workflow steps (fan out, then join); `crew eval` on canned tasks

### Parked
- [-] SSH/cloud runners (large and high risk until the local loop is proven), a crew marketplace, voice control, shared memory beyond `lessons.md`

## Later

- [ ] Parallel workflow steps (fan out, then join)
- [ ] Background-job and monitor indicators; hold a task until its jobs finish
- [ ] One-click merge of approved branches
- [ ] Shared crew memory (qmd or plain markdown) once execution is allowed

## Batch: packaging and releases

- [x] **`pip`/`pipx` install**: `pyproject.toml` (hatchling), `crew` command, flat source tree mapped into `claude_crew/` in the wheel
- [x] Installed runs keep config in the current folder, never in site-packages
- [x] **Versioning**: `__version__` in `server.py`, `--version`, version in the header, `CHANGELOG.md`
- [x] **Tests**: stdlib `unittest` for helpers, workflows, config, reorder, clear chat, plus an HTTP smoke test
- [x] **Lint**: ruff (E/F/W, conservative ignores)
- [x] **CI**: lint, tests on Linux/Windows/macOS x Python 3.10-3.13, build and install the wheel
- [x] **Release pipeline**: `v*` tag runs tests, checks the version, builds, creates the GitHub Release
- [x] README: Install and Releasing sections
- [ ] First tagged release (`v0.1.0`)
- [ ] Set up the PyPI trusted publisher, then set repo variable `PUBLISH_PYPI=true`
