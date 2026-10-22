"""Webhook trigger: POST /api/hook/<id> with the trigger's token."""
from __future__ import annotations

import secrets

from .base import Trigger, register


@register
class Webhook(Trigger):
    kind, order = "webhook", 50

    def validate(self, sp, old=None):
        return None, {"token": (old or {}).get("spec", {}).get("token") or secrets.token_urlsafe(18)}
