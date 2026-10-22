"""Watch trigger: files matching a glob in the project changed."""
from __future__ import annotations

from .base import Trigger, now, register


@register
class Watch(Trigger):
    kind, order = "watch", 30

    def validate(self, sp, old=None):
        glob = str(sp.get("glob") or "").strip()
        if not glob or ".." in glob or glob.startswith(("/", "\\")) or ":" in glob:
            return "watch a pattern inside the project, e.g. src/**/*.py", {}
        return None, {"glob": glob[:200], "every": max(5, int(sp.get("every") or 10)),
                      "cooldown": max(0, int(sp.get("cooldown") if sp.get("cooldown") not in (None, "") else 60))}

    def tick(self, crew, tr, minute, lt):
        if now() - tr.get("_checked", 0) < tr["spec"]["every"]:
            return False
        tr["_checked"] = now()
        sig: dict[str, float] = {}
        for pat in (x.strip() for x in tr["spec"]["glob"].split(",")):
            if not pat:
                continue
            for f in crew.project.glob(pat):
                if len(sig) >= 5000:
                    break
                if ".git" in f.parts or not f.is_file():
                    continue
                try:
                    sig[f.relative_to(crew.project).as_posix()] = f.stat().st_mtime
                except OSError:
                    pass
        old = tr.get("_sig")
        if old is None:  # first look is the baseline
            tr["_sig"] = sig
            return False
        changed = sorted(k for k in sig.keys() | old.keys() if sig.get(k) != old.get(k))
        if not changed or now() - (tr.get("last") or 0) < tr["spec"]["cooldown"]:
            return False  # keep the old baseline so changes during the cooldown fire afterwards
        tr["_sig"] = sig
        crew._fire(tr, {"files": "\n".join(changed[:40]) + ("\n…" if len(changed) > 40 else "")},
                   f"{len(changed)} file{'s' if len(changed) != 1 else ''} changed")
        return True
