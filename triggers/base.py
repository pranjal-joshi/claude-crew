"""The Trigger base class and the registry. A trigger kind is one module in this package that subclasses Trigger and
registers itself with @register; see README.md in this folder."""
from __future__ import annotations

import re
import threading
import time

MAX_CHAIN = 5  # an event trigger never fires on a task that is already this many triggers deep
REGISTRY: dict[str, "Trigger"] = {}


def now() -> float:
    return time.time()


def short_title(s, words: int = 6) -> str:
    """A card title people can scan: no links, at most `words` words."""
    w = " ".join(re.sub(r"https?://\S+", "", str(s or "")).split()).split(" ")
    return (" ".join(w[:words]).rstrip(",.;:") + ("…" if len(w) > words else "")).strip()


class Trigger:
    """One trigger kind. Instances are stateless singletons: per-trigger state lives in the trigger dict `tr`
    (persisted keys: id, name, kind, enabled, spec, task, fired, last, note, seen, since; keys starting with "_" are
    runtime-only and never saved or sent to the UI). `crew` is the Crew; methods run under its lock unless noted."""
    kind = ""
    order = 100            # position in the type list; kinds without a better idea go last
    ui_only = ""           # non-empty: only the UI may create or edit it, and this is the refusal shown to agents and the boss
    own_task = False       # starts its own work from the spec (workflow or agent): no task template, the name is optional
    polled = False         # check(crew, tr) runs in a thread every spec["every"] minutes
    check_on_run = False   # the Run button means "check now" instead of firing once by hand
    keeps_seen = False     # editing the spec keeps tr["seen"] (the baseline of what was already handled)

    def validate(self, sp: dict, old: dict | None = None) -> tuple[str | None, dict]:
        """(error, clean spec) from the spec the UI sent. `old` is the trigger being edited, if any. May raise
        TypeError/ValueError for bad numbers: that becomes "numbers only for intervals"."""
        raise NotImplementedError

    def default_name(self, spec: dict) -> str:
        return self.kind

    def task(self, spec: dict) -> dict:
        """own_task kinds: the task template stored with the trigger."""
        return {"title": "", "details": "", "tag": "", "agent": spec.get("agent", ""), "workflow": spec.get("workflow", "")}

    def schedule_next(self, tr: dict) -> float | None:
        """When a clock-driven trigger fires next (shown by the UI); None for the rest."""
        return None

    def next_check(self, tr: dict) -> float | None:
        """Polled kinds: when the next check is due, by the same rule as tick."""
        return tr.get("_checked", 0) + tr["spec"]["every"] * 60 if tr["enabled"] else None

    def tick(self, crew, tr: dict, minute: int, lt: time.struct_time) -> bool:
        """Called every second for each enabled trigger, under the lock. Return True when something fired."""
        if self.polled and not tr.get("_running") and now() - tr.get("_checked", 0) >= tr["spec"]["every"] * 60:
            tr["_checked"], tr["_running"] = now(), True
            threading.Thread(target=self.check, args=(crew, tr), daemon=True).start()
        return False

    def check(self, crew, tr: dict) -> None:
        """Polled kinds: one check, in a thread and NOT under the lock (take crew.lock for the short bits). It must
        clear tr["_running"] when done; finish() does."""
        raise NotImplementedError

    def finish(self, crew, tr: dict, err: str | None, checked: dict) -> None:
        """The end of a check: clear _running, record the result, raise or resolve the trigger's alert. Takes the lock."""
        with crew.lock:
            tr["_running"] = False
            if tr not in crew.triggers:
                return
            tr["error"] = err
            tr["checked"] = {"t": now()} | checked
            if err:  # one alert per failing trigger, closed by its next good check
                crew.raise_alert(f"Trigger {tr['name']} check failed", "warning", err, key=f"trigger:{tr['id']}")
            else:
                crew.resolve_alert(f"trigger:{tr['id']}", "the next check worked")
        crew.touch()

    def on_task_event(self, crew, tr: dict, t: dict, col: str, summary: str | None) -> None:
        """A task reached review or done, or its agent hit an error. Enabled triggers only."""

    def form(self, crew, p: dict) -> dict | None:
        """The UI form's helper endpoint (POST /api/trigger/<kind>, body {op, spec}): defaults, a Test button. Runs
        without the lock. Return a dict ({"error": ...} on failure); None means this kind has no endpoint."""
        return None

    def clean(self, crew, p: dict, old: dict | None) -> tuple[str | None, dict]:
        """(error, trigger record) from a trigger_add / trigger_update request."""
        name = str(p.get("name") or "").strip()[:60]
        task, sp, enabled = p.get("task") or {}, p.get("spec") or {}, bool(p.get("enabled", True))
        if self.own_task:
            err, spec = self.validate(sp, old)
            if err:
                return err, {}
            if sp.get("workflow"):
                if not crew._workflow(sp["workflow"]):
                    return f"no workflow {sp['workflow']}", {}
            elif sp.get("agent") not in crew.agents:
                return "pick a workflow or an agent to start", {}
            spec |= {"workflow": sp.get("workflow") or "", "agent": "" if sp.get("workflow") else sp["agent"]}
            return None, {"name": name or self.default_name(spec)[:60], "kind": self.kind, "enabled": enabled, "spec": spec, "task": self.task(spec)}
        if not name:
            return "name the trigger", {}
        if not str(task.get("title") or "").strip():
            return "the task needs a title", {}
        if task.get("agent") and task["agent"] not in crew.agents:
            return f"no agent called {task['agent']}", {}
        if task.get("workflow") and not crew._workflow(task["workflow"]):
            return f"no workflow {task['workflow']}", {}
        try:
            err, spec = self.validate(sp, old)
        except (TypeError, ValueError):
            return "numbers only for intervals", {}
        if err:
            return err, {}
        return None, {"name": name, "kind": self.kind, "enabled": enabled, "spec": spec,
                      "task": {"title": str(task["title"]).strip()[:200], "details": str(task.get("details") or "")[:4000],
                               "tag": task.get("tag") or "", "agent": task.get("agent") or "", "workflow": task.get("workflow") or ""}}


def register(cls: type[Trigger]) -> type[Trigger]:
    """Class decorator: adds the kind to the registry (one instance)."""
    REGISTRY[cls.kind] = cls()
    return cls


def get(kind) -> Trigger | None:
    return REGISTRY.get(kind) if isinstance(kind, str) else None


def kinds() -> list[str]:
    return [t.kind for t in sorted(REGISTRY.values(), key=lambda t: (t.order, t.kind))]
