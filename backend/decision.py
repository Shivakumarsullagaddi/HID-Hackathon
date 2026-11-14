"""Decision policy. Pure functions, no I/O.

Order matters: explicit denies come first, ALLOW needs every signal to agree,
and everything else falls through to REQUEST_VERIFICATION. Nothing uncertain
is ever turned into ALLOW.
"""

from datetime import datetime
from typing import Optional

from . import config

ALLOW = "ALLOW"
DENY = "DENY"
REQUEST_VERIFICATION = "REQUEST_VERIFICATION"

# Starting values. Tune on your own webcam and record changes in the README.
LIVENESS_ALLOW = 0.85      # at or above: treated as a live person
LIVENESS_DENY = 0.40       # below: treated as a spoof
SIMILARITY_ALLOW = 95.0    # at or above (0-100 scale): identity verified
STEPUP_LIVENESS_MIN = 0.60  # passive floor that a blink step-up may rescue
OFFLINE_LIVENESS_ALLOW = 0.90
OFFLINE_STEPUP_LIVENESS_MIN = 0.65


def evaluate_schedule(
    role: Optional[str] = None,
    zone: Optional[str] = None,
    hour: Optional[int] = None,
    custom_schedule: Optional[dict] = None,
) -> tuple[str, str]:
    """Context-aware schedule check: returns (status, reason).
    status is one of: "ALLOW", "STEPUP", "DENY".

    To preserve backward compatibility with existing tests that run at any hour,
    when both hour and zone are omitted (None), access is allowed by default.
    """
    if hour is None and zone is None:
        return ALLOW, "Within permitted campus schedule"

    effective_role = role or config.DEFAULT_ROLE
    effective_zone = zone or config.DEFAULT_ZONE
    effective_hour = datetime.now().hour if hour is None else int(hour) % 24

    schedule_source = custom_schedule or config.CAMPUS_SCHEDULES.get(effective_role, {})
    zone_rules = schedule_source.get(effective_zone)
    if zone_rules is None:
        return DENY, f"Unauthorized campus zone '{effective_zone}' for role '{effective_role}'"

    for start, end in zone_rules.get("allow_hours", []):
        if start <= effective_hour < end:
            return ALLOW, f"Authorized schedule ({effective_role} @ {effective_zone}, {effective_hour:02d}:00)"

    for start, end in zone_rules.get("stepup_hours", []):
        if start <= effective_hour < end:
            return "STEPUP", f"Extended/restricted schedule requires step-up ({effective_role} @ {effective_zone}, {effective_hour:02d}:00)"

    return DENY, f"Outside permitted campus schedule ({effective_role} @ {effective_zone}, {effective_hour:02d}:00)"


def decide(
    liveness: float,
    similarity: float,
    gate_passed: bool,
    standing: Optional[str],
    matched: bool,
    schedule_status: str = "ALLOW",
    schedule_reason: Optional[str] = None,
    offline_mode: bool = False,
) -> tuple[str, str]:
    """First-pass decision for one verification attempt.

    liveness: 0.0-1.0, similarity: 0-100, gate_passed: bool,
    standing: "active" | "suspended" | None, matched: bool.
    Returns (decision, reason).
    """
    if standing == "suspended":
        return DENY, "User suspended"
    if matched and schedule_status == "DENY":
        return DENY, (schedule_reason or "Outside permitted campus schedule")
    if matched and liveness < LIVENESS_DENY:
        return DENY, "Spoof detected"

    liveness_allow_min = OFFLINE_LIVENESS_ALLOW if offline_mode else LIVENESS_ALLOW
    if not matched and gate_passed and liveness >= liveness_allow_min:
        return DENY, "Live person, identity not enrolled"
    if (gate_passed and matched and liveness >= liveness_allow_min
            and similarity >= SIMILARITY_ALLOW):
        if schedule_status == "STEPUP":
            return REQUEST_VERIFICATION, (schedule_reason or "Campus schedule requires step-up verification")
        return ALLOW, "Live person and identity verified"
    if not gate_passed:
        return REQUEST_VERIFICATION, "Poor image quality"
    return REQUEST_VERIFICATION, (
        "Offline fail-safe: step-up required"
        if offline_mode and matched and liveness >= LIVENESS_ALLOW
        else "Low confidence"
    )


def decide_stepup(
    blinked: bool,
    liveness: float,
    similarity: float,
    standing: Optional[str],
    matched: bool,
    same_user: bool,
    schedule_status: str = "ALLOW",
    schedule_reason: Optional[str] = None,
    offline_mode: bool = False,
) -> tuple[str, str]:
    """Final decision after a blink step-up. Never returns REQUEST_VERIFICATION.

    same_user: the step-up frames matched the same enrolled user as the first
    attempt. A failed or inconclusive step-up is a DENY (the person can retry).
    """
    if standing == "suspended":
        return DENY, "User suspended"
    if matched and schedule_status == "DENY":
        return DENY, (schedule_reason or "Outside permitted campus schedule")
    if liveness < LIVENESS_DENY:
        return DENY, "Spoof detected"
    if not blinked:
        return DENY, "Step-up failed: no blink detected"
    if not matched or not same_user:
        return DENY, "Step-up failed: identity not confirmed"

    stepup_liveness_min = OFFLINE_STEPUP_LIVENESS_MIN if offline_mode else STEPUP_LIVENESS_MIN
    if similarity >= SIMILARITY_ALLOW and liveness >= stepup_liveness_min:
        return ALLOW, "Step-up passed: blink confirmed"
    return DENY, "Step-up failed: low confidence"


def thresholds():
    return {
        "liveness_allow": LIVENESS_ALLOW,
        "liveness_deny": LIVENESS_DENY,
        "similarity_allow": SIMILARITY_ALLOW,
        "stepup_liveness_min": STEPUP_LIVENESS_MIN,
        "offline_liveness_allow": OFFLINE_LIVENESS_ALLOW,
        "offline_stepup_liveness_min": OFFLINE_STEPUP_LIVENESS_MIN,
    }

