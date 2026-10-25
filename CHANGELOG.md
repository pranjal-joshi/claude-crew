# Changelog

All notable changes to this project are documented here. The format follows [Keep a Changelog](https://keepachangelog.com/), and versions follow [Semantic Versioning](https://semver.org/).

## [Unreleased]

## [0.8.0] - 2026-10-25

### Added
- **🧰 Tools per agent.** Every tool description goes out with every turn, so an agent that loads everything you have set up (every built-in tool, MCP server, plugin and claude.ai connector) pays for all of it on each call: about 85k tokens before the first word on a busy setup, against about 8k for the core tools. An agent's settings now have a **Tools** section: *Everything* (as before, the default) or *Picked*: groups of built-in tools (files & shell, subagents, web, background jobs, PowerShell, scheduling, orchestration, artifacts, other), each MCP server or connector, and skills on or off, each with a rough per-turn token cost. **✨ Use recommended** picks a set from the agent's role and tags. Picked tools become `--tools`, unpicked MCP servers go in `--disallowedTools` (which drops their definitions, connectors included), and skills off is `--disable-slash-commands`. The crew tools are always on. Saved in `crew.json` as the agent's `toolkit`. Deferred tool search (`ENABLE_TOOL_SEARCH`) made no difference in `-p` mode, so a toolkit picks instead.
- **⑂ Workflow steps as subagents.** A workflow can *Run steps as subagents* (a tick box in the editor, or `subagents: true` from the boss): an agent that works several steps of one run keeps one session for that run and does each step in a subagent named after the workflow, run and step. Different runs never share a session.
- **Token budget and Claude's limits.** The header's token chip counts tokens since Crew started (it starts at zero after a restart), green, amber, then red against an optional **token budget** (`5M`, `800k`). At 80% a prompt asks you to acknowledge it; at 100% running turns finish and nothing new starts until you raise the budget or restart Crew. Click the chip for the breakdown (in, cache reads, out, top agents), the budget, your dollar budgets, and Claude's own **5-hour session** and **weekly** limits with their reset times, read from the `rate_limit_event` Claude Code streams (no extra calls, no credentials). Saved in `crew.json` as `budget_tokens`.
- **Automatic retries.** A turn that fails for a passing reason (API overloaded or rate-limited, a timeout, a dropped connection, the Claude process dying) goes back to the same agent after 30 s, then 2 min, resuming its session. After that it waits for you with an alert. A lasting error (bad key, not signed in) is not retried.
- **Self-checks.** Every second the dispatcher looks for states nothing should leave behind: an agent "running" with no process, a task In progress that nobody is working on, an unknown status or column, an agent pointing at a deleted task. A state that stays wrong for 20 s is repaired and noted in the feed (`repair`).
- **Event log.** Every feed event is also appended to `~/.crew/events/<project>.jsonl` (rolls over at 5 MB), so you can see what happened after a restart.
- **crew.json history.** Before crew.json changes, the previous version is copied to `~/.crew/config-history/<project>/` (the last 20 are kept). Copy one back to undo a bad change.

### Changed
- **The header puts review first.** A blue **◎ N to review** button opens the next card waiting for you (click again for the one after); running and queued are quiet text; the token chip is one button that opens Budgets, where the dollar-amount toggle now lives; 🧠 Crew memory moved to the ⋮ menu; the permission mode and search sit on the right; the connection dot reads *offline* when the page loses the server.
- **Look before you approve.** On a Review card **Review changes** is the primary button and ✓ Approve a quiet outline beside it. The Review column is outlined while it has cards.
- **Scheduled jobs fold into one line** at the top of the Backlog that says what fires next.
- **Agent settings fill the panel** (the chat comes back with Done), with dividers between rows and a smaller icon picker. A picked tool list folds into a one-line summary.
- **Repeated trigger firings merge.** A trigger that fires again while its earlier task (or workflow run) with the same title and details is still waiting, unstarted, adds nothing new. The waiting task counts the extra firings.
- **The daily budget counts running agents.** Cost is only known when a turn ends, so agents started together could each overshoot the daily budget. Each running agent now counts at the recent average turn cost, and another starts only while that still fits. When nothing is running, work always starts.

### Fixed
- **`az` arguments with `( ) , ; =` on Windows.** `az` is a `.cmd` file, and cmd.exe broke on a `--query` filter without spaces; each argument is now quoted there, for triggers and watches alike.
- **A corrupt state file falls back to its `.bak`** instead of starting an empty board.
- **The state file has a schema number.** Upgrades step through migrations. A file saved by a newer Crew is copied aside (`.schema-N`) before this version writes over it.
- **An agent whose process died mid-task** no longer leaves the task In progress: it is retried or goes back to the backlog.

## [0.7.1] - 2026-10-24

### Fixed
- **Open PR pointed at an old, merged PR.** A task's brief often cites your own earlier PRs as background ("follow-up flagged in the review of PR #2"), and every PR of yours it named joined the card, so Open PR, Address review and Ready to merge could act on a PR the task never touched. A PR that only the brief names now joins the card only while it is open; PRs the agent names in its own output count as before. Links like that already on a card leave it when Crew restarts.

## [0.7.0] - 2026-10-24

### Added
- **🔘 Card buttons:** your own one-click actions on task cards, next to Open PR, Address review and Fix pipeline. Add them from the header ⋮ menu (*Card buttons…*) or ask the boss, which gets two new crew tools, `set_card_button` and `delete_card_button`. A button shows on cards that match it (columns, an agent or its tags, has a PR, no PR, or follows a pipeline run) and does one of three things: asks the card's agent to run a skill or an instruction in the card's own session (`/rebase {pr}`; the card is In progress until the turn ends), starts a workflow on the card, or hands the card to another agent as a new task. Placeholders: `{id}`, `{title}`, `{pr}`, `{pipeline}`, `{branch}`, `{agent}`, `{summary}`. Buttons never run shell commands themselves, and only you press them. Saved in `crew.json` under `card_actions`.
- **✓ Ready to merge:** a card working on a GitHub PR gets a green *Ready to merge* button (and a green outline) as soon as GitHub says the PR can be merged: open, not a draft, required checks passing, reviews done, no conflicts. The header shows how many are ready. The button opens the PR to merge it there; Crew never merges, and the card follows the merge on its own. Crew checks each PR every 2 minutes, read-only through `gh`.
- **🧠 Crew memory:** agents save project facts with the new crew tool `remember` (how a deploy is confirmed, which pipeline steps are non-blocking, who owns what), and every agent gets them at the start of a task, or on its next turn when they changed. Each fact has a key, and saving the same key replaces it, so facts stay current instead of piling up (50 at most). The 🧠 chip in the header lists them; add, edit or forget them there. Kept in the project's state, not in the repo.
- **📎 One card per PR or work item:** when an open card is already about a PR or work item (its links, its run's, AB#n in its title), news about it goes to that card instead of starting new work. A PR trigger (raised or merged) attaches to the owner instead of starting a second run; a pipeline trigger tells the owners of your work items in the run; an alert linking an owned PR reaches its card. A waiting ⟳ owner wakes with the news (a 🚧 blocked one too), and any owner reads it at the start of its next turn; cards show 📎 n news meanwhile.
- **🚧 Blocked, an escape gate for ⟳ steps:** `decide` takes a third verdict, *blocked*, for a goal stuck on something outside the change that its agent can't fix (its pipeline run cancelled or superseded by someone else's, a later run failing for another change, a deploy stage a person must run). The card goes to Review marked 🚧 with what a person must do, the agent is freed for other work, and no more checks run. The free watch keeps going: if it reaches the step's goal after all, the agent is woken to confirm and the step continues. Otherwise approve to pass the step anyway, send it back to retry, or stop the run.
- **Alerts clear themselves:** an alert whose link is a failed pipeline run (Azure Pipelines or GitHub Actions) is resolved once a later run of that pipeline on the same branch succeeds (or partly succeeds), checked every 5 minutes through `az` or `gh`. Open alerts that name the same pipeline go with it, and a second agent reporting the same failed run bumps the first alert instead of adding another.
- **🛠 Fix pipeline:** a deploy watch whose pipeline run failed hands the failure to a *Pipeline fix* workflow run (created from the crew's tags on first use: fix, ship a PR on the same work item, keep it green until it merges), from its card or with the new crew tool `fix_pipeline`. Every step first checks whether someone else already fixed it, and reuses or stops. The watch waits without checks (the time doesn't count against it), then wakes with the fix run's result to follow the first run that carries the fix; a stopped, failed or deleted fix run wakes it too, with why. Several watches blocked by one failure share one fix run (*join*). Before it hands on, the fixer runs locally only the tests its fix touches: the one that failed, any it added, and those covering its diff; the full suite is left to CI on the PR. **PR watches use it too:** a PR CI step whose check fails on master as well hands it to *Pipeline fix* (or joins the running one) instead of waiting forever on a master its watch can't see; it wakes once when the fix merges, merges master into its branch and re-runs its checks. The 🛠 button and chip show on PR cards as well.

### Changed
- **Deploy cards open the pipeline, not the PR.** A card whose agent is tagged `deploy` or `cd` (a CD watcher) shows **Open pipeline #N** for the run it follows instead of Open PR and Address review. Crew picks the run from the agent's output (Azure Pipelines and GitHub Actions links) and from its watch command (`az pipelines runs show --id N`, `az pipelines build show --id N`), using the command's organization and project or the ADO project Crew knows. Until a run is known the card links the PR.

### Fixed
- **A card waiting in Review stalled its agent's watches:** an agent with work in Review takes nothing else, so a deploy agent with one card awaiting approval never ran the checks its other ⟳ cards were woken for. When the card in Review changed no files, the agent now runs those checks in between and then goes back to the card; a card with changes still keeps its agent, so nothing touches that worktree.
- **The alerts strip's header didn't fold or unfold:** clicking it focused the header, and the strip never redrew while anything in it had focus, so it also stopped updating. It now redraws unless you are typing in it.
- **A PR Crew opened got a second CI monitor after a restart:** on start-up a card's PR links are checked again in the background, and in that window the *PR raised* trigger took the PR for one of yours and started another run on it. Links being checked count as Crew's, and a link whose owner can't be read (gh offline) now stays on its card.

## [0.6.0] - 2026-10-23

### Added
- **Pull request trigger:** a PR you open outside Crew (by hand, in your IDE, another tool) starts a workflow run, such as a CI monitor, or a task. Only PRs opened after the trigger is created count, each one starts once, and PRs Crew or its agents opened are skipped; drafts wait until they are ready for review unless *Drafts too* is on. GitHub through `gh`, Azure Repos through `az`, read-only. New *Monitor PRs I open* preset, and a *PR CI monitor* workflow template for new crews (a ⟳ step that fixes what the PR broke until it merges). **Fire when: Raised or Merged**: on Merged, a PR of yours that merges after the trigger is created (Crew's included) starts a CD workflow once; new *Deploy what I merge* preset and *PR merged: CD* template (a ⟳ step that follows the deployment and smoke-checks it).
- **Full screen side panel:** ⤢ in the agent and review panel headers gives the panel the whole window for long reviews and runs; ⤡ or Esc goes back. Remembered per browser.
- **↻ Restart a workflow from the board:** the workflow frame's ↻ menu restarts the whole run (with the workflow's current steps) or restarts from any step whose earlier steps finished; those keep their results. The open step stops, and every restarted step is a new task with a fresh session. Works on running, stopped and finished runs.
- **Background jobs show on the board.** A shell an agent starts in the background (or a Monitor) keeps running in its Claude session after its turn ends, where Crew could not see it. Its agent and its card now show a ⚙ chip; the agent drawer lists each job with its command and age, shows its output, and stops it (the agent is asked to stop one; Stop all ends the idle process, keeping the session).

### Changed
- **Triggers page, grouped:** triggers sit in sections by what they react to (Work intake, Code & CI/CD, Schedules, Project & crew events), each with a count, a fold (remembered) and its own ＋ Add. A search box and On / Off / Needs attention filters sit on top; each trigger shows a status pill, errors stand out in a red box, and the text is easier to read. Presets are grouped the same way, folded away once you have triggers.
- **↻ Address review moves the card to In progress** while the agent works through the PR's comments, and back to Review or Done when its turn ends, also when it fails, is stopped, or Crew restarts. Pressing it in the review sheet opens the agent's panel so you can follow along.

### Fixed
- **A card could get stuck In progress after a restart.** Crew came back with every agent idle, also one whose work still waited in Review, so it took new work; approving the older card then cleared the agent's current task, and that task's finished turn was never recorded. Agents now stay on their Review card across a restart, approving a card leaves the agent's other work alone, and sending a card back waits while its agent is on another one.
- **Auto-gated workflow steps sometimes waited for approval.** When another agent was busy at the same moment, its thread could take the step's auto-approve while the card's diff was still being collected, and the approve was dropped. One thread now runs that queue in order, a card you are diffing waits instead, and a failed auto-approve shows in the feed.
- **A workflow step got earlier steps' summaries cut short:** only the step right before was passed in full; older ones were cut at 4,000 characters without saying so. A block drafted in step 2 (an ADO comment) reached step 4 cut mid-sentence, and the agent, rightly, refused to post it. Every earlier step now comes in full; only when they pass 60,000 characters together are the oldest shortened, with a note saying how much is missing and which task has the full text.
- **Teams replies came without their thread:** a reply like "please look into this" was passed on alone, so the agent guessed which thread it meant (and picked the wrong one). The check now finds the reply's thread and passes its post and earlier replies, and links the reply itself. `{where}` is the channel's name instead of the pasted link, which pointed at some other message.
- **A new task could continue another task's conversation:** an agent's session survived a restart but the task it belonged to didn't, so its next task (a plain one after a workflow step, say) resumed that step's context. Every task now starts on a session of its own (send-back still resumes its own), and on start-up a session found on several tasks stays only with the one that made it.
- **Other people's PRs on your cards:** every PR an agent mentioned (an earlier change, a colleague's related draft) was linked to its card, so ↻ Address review could run on someone else's PR and the PR trigger took it for Crew's. A mentioned PR is now linked only when `gh` or `az` says you opened it or it comes from an agent's branch; existing cards are checked again on start-up.
- **Workflow steps handed work off on the side:** a step's agent could call the handoff tool, starting a duplicate of the next step outside the run (a second plan, a second build) that sat in Review with nothing to do with the run. Hand-offs from a workflow step are now refused; the agent puts what comes next in its summary, which the next step reads.
- **Clearing Done emptied running workflows:** Clear board → Done also removed the finished steps of a workflow still running, and its frame then showed those steps as *not started*. Their cards now stay until the run ends, and a step whose card is gone but whose result the run kept shows as *done · card cleared*.
- **ADO board, assigned to me: turning it back on started old work:** items assigned to you while the trigger was off started the moment it came back on. Turning it on now takes a new baseline, like creating it does.
- **⟳ PR watches missed CI failures and Copilot reviews:** the watch command refused any `|`, even inside a quoted `--jq` filter, so Crew's own example (which lists failed checks) was rejected and agents fell back to `state,reviewDecision`, which never changes when CI fails or Copilot comments (its reviews are COMMENTED, not CHANGES_REQUESTED). Only pipes and redirects outside quotes are refused now. The example also lists merge conflicts, Azure Pipelines statuses and every new review (your own replies left out), and what already fails when the watch is set is its baseline, so only something new wakes the agent. An agent can also set its watch from a turn you start on the waiting step (a message, ↻ Address review).
- **ADO comments timed out:** without a project, the Azure DevOps MCP comment tools ask for one, which a headless agent never answers, so each call hung for a minute and failed. Tasks about a work item now tell the agent its organization and project and to pass `project` on every call.

## [0.5.0] - 2026-10-23

### Added
- **Parallel workflow steps:** steps can run at the same time, e.g. triage and telemetry together, then a planner. On the canvas, ＋ on a line offers *At the same time as …*, or a step's *When it starts* switches to *∥ With step N*. Parallel steps stack in one column and start together; the next step waits until every one of them is done, then gets all their summaries and merges all their branches. Each keeps its own gate (✋ / ⚡ / ⟳). On the board, the run's frame shows every live parallel task as a card and their step dots stacked; the chain on the Workflows page brackets them. Saved as `"par": true` on a step (in `crew.json`, exported files and the boss's `save_workflow`).
- **Watches fix failed CI:** the crew tool `watch` takes a `fail` regex (e.g. `FAILED|CHANGES_REQUESTED`). When the output matches with a new failure, Crew wakes the same agent with the output (the failed checks and their pipeline links) and asks it to fix the cause on the same branch, push and re-run what failed. The same failure doesn't wake it twice. With `fail` set, progress alone wakes nobody. The ⟳ step brief shows a `gh pr view` command that lists failed checks with their links.
- **⏹ Stop workflow on board cards:** a workflow card in In progress or Review has a Stop workflow button. It ends the run and its open step: a working agent is stopped, a waiting ⟳ or 👁 step stops checking, and the card moves to Done marked ⏹ stopped. It also ends the step of a run that was already stopped but still checking.

### Changed
- **Phone design pass:** 44px touch targets (an invisible touch area on small icons and switches), 8-point spacing, rounder cards, and empty columns that say what to do next, with a New task button in an empty Backlog. Desktop is unchanged.
- **"ADO tag" trigger is now "ADO board"**, since it also picks up items assigned to you.
- **Watches run every 2 minutes**, on their own clock instead of the step's interval (30 min by default, which still applies to an agent rechecking without a watch). Watch runs no longer count as the step's checks; a watch lasts as long as the step's checks would (checks × interval), then the agent gets a last look.
- **Waiting steps show in In progress:** a ⟳ step waiting for its next check, or a 👁 step watching a command, is shown in In progress instead of Backlog.

### Fixed
- **Phone board: stacked workflow cards overflowed the column** in In progress, Review and Done when a run's title had a long link (an ADO work item URL), pushing the card off the screen. The column now keeps the screen's width and long titles wrap or shorten.
- **＋ on a workflow canvas line closed its menu straight away**, so adding a step there did nothing; it opens the menu again.
- **Phone layout:** the + button floats above the bottom bar so the five tabs share it evenly; Clear board moved to the ⋮ menu; workflow and chat headers fit on one line; card buttons share a row; the ADO trigger form is one column; menus dim the page behind them; toasts line up; a column keeps its scroll position when the board redraws (it jumped to the top whenever an agent reported).
- **ADO "assigned to me" started old items after an edit:** widening the trigger (say, adding Product Backlog Item to Bug) kept the old baseline, so items already assigned to you that the new filter matched started as if new. Any change to what it matches (types, project, organization, mode, or skip/start) now takes a new baseline on the next check.

## [0.4.0] - 2026-10-22

### Added
- **ADO trigger: assigned to me.** The ADO trigger can pick up items newly assigned to your `az` user instead of tagged ones (*Pick up items: assigned to me*). Items already assigned when it starts are a baseline and skipped, unless you choose *start them too*. No tags are written; the comment is optional as before.
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
[0.4.0]: https://github.com/pranjal-joshi/claude-crew/compare/v0.3.0...v0.4.0
[0.5.0]: https://github.com/pranjal-joshi/claude-crew/compare/v0.4.0...v0.5.0
[0.6.0]: https://github.com/pranjal-joshi/claude-crew/compare/v0.5.0...v0.6.0
[0.7.0]: https://github.com/pranjal-joshi/claude-crew/compare/v0.6.0...v0.7.0
[0.7.1]: https://github.com/pranjal-joshi/claude-crew/compare/v0.7.0...v0.7.1
[Unreleased]: https://github.com/pranjal-joshi/claude-crew/compare/v0.7.1...HEAD
