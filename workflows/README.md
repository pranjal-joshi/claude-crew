# Workflows

A workflow is an ordered list of steps, each run by one agent. Running it creates step 1's task; when a step is done (approved by you, or automatically when its gate is `auto`) the next step's task is created with the previous step's summary and branch. A step with `"par": true` runs at the same time as the step before it: `groups()` splits the steps into groups that start together, and the next group starts once every step in the group is done, with all their summaries and a `git merge` of all their branches. `run["step"]` is the current group's first step. Workflows live in `crew.json` under `"workflows"`; a run is a dict in the Crew's `runs` list, with the workflow's id in its `"wf"` key.

| File | What it holds |
|---|---|
| `runs.py` | `start_run`, `start_step`, `on_task_event` (advances a run as tasks reach review, done or error), delivering the result (`output`: board, markdown report, webhook, or the next workflow), gates (`decide`), stop and delete, and `fill` for `{placeholders}`. |
| `definitions.py` | Validation (`clean`) and the save, delete and run-now actions. |
| `templates/` | The built-in workflows, one module per workflow. |

The Crew keeps `crew._workflow`, `crew._run`, `crew._start_run`, `crew._wf_event` and `crew._clean_workflow` as one-line methods that call these functions; triggers use them to start runs. Functions take the Crew first and run with its lock held.

## Add a template

Drop `workflows/templates/<name>.py`; nothing else changes. Modules whose names start with `_` are skipped.

```python
"""The release workflow template."""

ORDER = 60   # templates sort by ORDER, then id
WORKFLOW = {"id": "w6", "name": "Release", "description": "Test, review, write the notes.",
            "steps": [{"agent": "tester", "title": "Regression: {input}", "details": "", "gate": "auto"},
                      {"agent": "scribe", "title": "Release notes: {input}", "details": "", "gate": "approve"}]}
```

`WORKFLOW` has the shape of a `crew.json` workflow. The `id` is fixed, unique and never reused (runs and triggers refer to it). Step titles and details can use `{input}`, `{details}`, `{step}`, `{steps}`, `{workflow}`, `{prev_summary}`, `{prev_who}`, `{prev_branch}` and `{prev_commit}`. After a parallel group, `{prev_summary}` holds every summary (each under its agent's name), `{prev_who}` lists the agents, and `{prev_branch}` / `{prev_commit}` are empty. Templates are only the starting set: a `crew.json` that already has workflows is never overwritten. Every `agent` must exist in the crew (the built-in templates use the preset agents).
