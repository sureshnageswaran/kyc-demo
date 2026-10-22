"""Versioned business-timer profiles (spec section 14).

The profile *name* travels in workflow input; the constants live here and must
never change for an existing name. Add `normal-v2` rather than editing
`normal-v1`, or in-flight workflows will diverge on replay.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class TimerProfile:
    name: str
    review_reminder_s: int
    review_escalation_s: int
    review_expiry_s: int
    recovery_expiry_s: int
    activation_budget_s: int
    document_concurrency: int = 4


PROFILES: dict[str, TimerProfile] = {
    "normal-v1": TimerProfile(
        name="normal-v1",
        review_reminder_s=2 * 3600,
        review_escalation_s=8 * 3600,
        review_expiry_s=24 * 3600,
        recovery_expiry_s=7 * 86400,
        activation_budget_s=3600,
    ),
    "demo-v1": TimerProfile(
        name="demo-v1",
        review_reminder_s=15,
        review_escalation_s=30,
        review_expiry_s=60,
        recovery_expiry_s=120,
        activation_budget_s=60,
    ),
}

DEFAULT_PROFILE = "normal-v1"

# Escalation target for overdue reviews (queue indicator only in v1).
SENIOR_QUEUE = "senior-compliance"
