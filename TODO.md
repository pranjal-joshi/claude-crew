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
- [ ] Refresh README screenshots/GIF for boss chat and the phone layout

## Batch: canvas editor and small asks

- [x] **Workflow canvas** (n8n-style): When (trigger) → one block per agent → Output; drag to reorder, drag agents from the palette or ＋ on a line to insert, ✋/⚡ toggles on lines, inspector panel, Del removes; phone: vertical canvas, Earlier/Later buttons
- [x] **Workflow output**: board (default), markdown report file, webhook (JSON), or start the next workflow; saved with the workflow, boss can set it too
- [x] **Click a board card** to open the agent that handled it
- [x] **Clear chat** (⌫ in the chat bar): wipes the conversation and context of the boss or the selected agent
- [x] **Reorder agents** by dragging in the rail (saved to crew.json)
- [x] **Star history** chart in the README (Haiku subagent)
- [ ] Refresh `assets/wf-edit.png` with the canvas editor

## Later

- [ ] Parallel workflow steps (fan out, then join)
- [ ] Background-job and monitor indicators; hold a task until its jobs finish
- [ ] One-click merge of approved branches
- [ ] Shared crew memory (qmd or plain markdown) once execution is allowed
