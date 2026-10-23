# Trigger kinds

Each kind of trigger (schedule, event, watch, poll, webhook, ado, pr, teams, pipeline) is one module in this folder. `server.py` knows none of them by name: it asks the registry in `base.py` for the kind a trigger has, and calls the methods below.

| File | Kind |
|---|---|
| `base.py` | `Trigger` base class, `@register`, `get(kind)`, `kinds()`, shared `now()`, `short_title()`, `MAX_CHAIN` |
| `azure.py` | `az` runner shared by `ado` and `pipeline` (argument list, never a shell), `AzError`, `ado_defaults()`, `az_me()` |
| `schedule.py` `event.py` `watch.py` `poll.py` `webhook.py` | the simple kinds |
| `ado.py` `pipeline.py` `pr.py` `teams.py` | kinds that start work on their own (their own task or workflow run per item) |

## Add a kind

Drop in one file, `triggers/mykind.py`. Modules here are imported automatically (anything not starting with `_`), so there is nothing to register by hand and nothing to change in `server.py`.

```python
from .base import Trigger, now, register


@register
class Ping(Trigger):
    kind = "ping"
    order = 60            # position in the type list; omit to go last
    polled = True         # check() runs in a thread every spec["every"] minutes

    def validate(self, sp, old=None):          # (error, clean spec); the spec is stored in crew.json as it is returned
        url = str(sp.get("url") or "").strip()
        if not url.startswith("https://"):
            return "the URL must start with https://", {}
        return None, {"url": url, "every": max(1, int(sp.get("every") or 5))}   # bad numbers: raise ValueError/TypeError

    def check(self, crew, tr):                 # not under the lock: take crew.lock only around crew state
        err = None
        ...                                    # slow work here (network, subprocesses)
        with crew.lock:
            crew._fire(tr, {"status": "200"}, "ping answered")   # makes a task or a workflow run from the trigger's task template
        self.finish(crew, tr, err, {"found": 1})                 # clears tr["_running"], records the check, raises or resolves an alert
```

The trigger's task template (title, details, tag, agent, workflow) is validated for you; `{status}` above is a placeholder available in it, next to `{date}`, `{time}`, `{weekday}` and `{trigger}`.

What a kind can set or override:

| Member | Meaning |
|---|---|
| `kind`, `order` | The name stored in `crew.json` (never change it once shipped), and its place in the type list. |
| `validate(sp, old)` | Required. `old` is the trigger being edited (webhook keeps its token this way). |
| `ui_only` | A refusal message. When set, only the UI may create or edit this kind; the boss and agents get the message. Set it for anything that runs commands or touches an external account. |
| `polled`, `check(crew, tr)` | A background check every `spec["every"]` minutes. `tr["_running"]` stops overlapping checks; keys starting with `_` are never saved or sent to the UI. |
| `check_on_run` | The Run button means "check now" instead of firing once by hand. |
| `keeps_seen` | Editing the spec keeps `tr["seen"]` (what was already handled). Without it, an edit starts from a new baseline. |
| `own_task`, `default_name(spec)`, `task(spec)` | The kind starts its own work (workflow or agent chosen in the spec) instead of using a task template; the name becomes optional. See `ado.py`. |
| `tick(crew, tr, minute, lt)` | Replaces the polling rule for kinds that run on the clock or on short intervals (`schedule.py`, `watch.py`). Runs every second under the lock, so keep it quick. Return `True` when something fired. |
| `schedule_next(tr)` | When a clock-driven kind fires next, for the UI. |
| `on_task_event(crew, tr, t, col, summary)` | React to a task reaching review, done or error (`event.py`). |
| `form(crew, p)` | The UI form's helper endpoint, `POST /api/trigger/<kind>` with `{op, spec}`: defaults, a Test button. UI only, no lock held, return a dict (`{"error": ...}` on failure). `/api/ado` and `/api/teams` stay as aliases for the existing form. |

Rules that hold for every kind: standard library only; never run a shell with text from a spec unless the kind is `ui_only`; anything slow runs outside `crew.lock`; `crew.json` keeps the same shape (`{id, name, kind, enabled, spec, task, ...}`). The Triggers dialog in `index.html` has its own form per kind, so a new kind needs one there to be creatable from the UI; the server side is complete without it.

Tests patch the module that owns a function: `mock.patch.object(triggers.azure, "az", fake)`, not `server`. `triggers.get("ado").check(crew, tr)` runs one check synchronously.
