"""Schedule trigger: a 5-field cron expression in local time."""
from __future__ import annotations

import functools
import time

from .base import Trigger, now, register


def _cron_field(field: str, lo: int, hi: int) -> frozenset:
    vals = set()
    for part in field.split(","):
        rng, _, st = part.partition("/")
        step = int(st) if st else 1
        if rng == "*":
            a, b = lo, hi
        elif "-" in rng:
            a, b = (int(x) for x in rng.split("-", 1))
        else:
            a = int(rng)
            b = hi if st else a
        if step < 1 or a < lo or b > hi or a > b:
            raise ValueError(f"'{part}' is out of range {lo}-{hi}")
        vals.update(range(a, b + 1, step))
    return frozenset(vals)


@functools.lru_cache(maxsize=256)
def parse_cron(expr: str) -> tuple:
    f = expr.split()
    if len(f) != 5:
        raise ValueError("use 5 fields: minute hour day-of-month month weekday")
    dow = frozenset(d % 7 for d in _cron_field(f[4], 0, 7))
    return (_cron_field(f[0], 0, 59), _cron_field(f[1], 0, 23), _cron_field(f[2], 1, 31), _cron_field(f[3], 1, 12), dow,
            not f[2].startswith("*"), not f[4].startswith("*"))


def cron_error(expr: str) -> str | None:
    try:
        parse_cron(expr)
        return None
    except ValueError as e:
        return f"bad cron: {e}"


def cron_match(expr: str, lt: time.struct_time) -> bool:
    mi, hr, dom, mon, dow, dom_set, dow_set = parse_cron(expr)
    if lt.tm_min not in mi or lt.tm_hour not in hr or lt.tm_mon not in mon:
        return False
    d_ok, w_ok = lt.tm_mday in dom, (lt.tm_wday + 1) % 7 in dow
    return (d_ok or w_ok) if (dom_set and dow_set) else (d_ok and w_ok)  # standard cron: either day field when both are set


def cron_next(expr: str, after: float) -> float | None:
    t = (int(after) // 60 + 1) * 60
    for _ in range(60 * 24 * 8):  # look a week ahead; rarer schedules show as "later"
        if cron_match(expr, time.localtime(t)):
            return float(t)
        t += 60
    return None



@register
class Schedule(Trigger):
    kind, order = "schedule", 10

    def validate(self, sp, old=None):
        cron = " ".join(str(sp.get("cron") or "").split())
        if err := cron_error(cron):
            return err, {}
        return None, {"cron": cron}

    def schedule_next(self, tr):
        return cron_next(tr["spec"]["cron"], now())

    def tick(self, crew, tr, minute, lt):
        sp = tr["spec"]
        if tr.get("minute") != minute and cron_match(sp["cron"], lt):
            tr["minute"] = minute
            crew._fire(tr, {}, "on schedule")
            tr["next"] = cron_next(sp["cron"], now())
            return True
        if tr.get("next") and tr["next"] < now() - 60:
            tr["next"] = cron_next(sp["cron"], now())
        return False
