"""Decision policy. Pure functions, no I/O.

Order matters: explicit denies come first, ALLOW needs every signal to agree,
and everything else falls through to REQUEST_VERIFICATION. Nothing uncertain
is ever turned into ALLOW.
"""

ALLOW = "ALLOW"
DENY = "DENY"
REQUEST_VERIFICATION = "REQUEST_VERIFICATION"

# Starting values. Tune on your own webcam and record changes in the README.
LIVENESS_ALLOW = 0.85      # at or above: treated as a live person
LIVENESS_DENY = 0.40       # below: treated as a spoof
SIMILARITY_ALLOW = 95.0    # at or above (0-100 scale): identity verified
STEPUP_LIVENESS_MIN = 0.60  # passive floor that a blink step-up may rescue


def decide(liveness, similarity, gate_passed, standing, matched):
    """First-pass decision for one verification attempt.

    liveness: 0.0-1.0, similarity: 0-100, gate_passed: bool,
    standing: "active" | "suspended" | None, matched: bool.
    Returns (decision, reason).
    """
    if standing == "suspended":
        return DENY, "User suspended"
    # Low score is a spoof when the face matched, or when the image was good
    # enough to judge (gate passed). A failed gate stays a retry.
    if liveness < LIVENESS_DENY and (matched or gate_passed):
        return DENY, "Spoof detected"
    if not matched and gate_passed and liveness >= LIVENESS_ALLOW:
        return DENY, "Live person, identity not enrolled"
    if (gate_passed and matched and liveness >= LIVENESS_ALLOW
            and similarity >= SIMILARITY_ALLOW):
        return ALLOW, "Live person and identity verified"
    if not gate_passed:
        return REQUEST_VERIFICATION, "Poor image quality"
    return REQUEST_VERIFICATION, "Low confidence"


def decide_stepup(blinked, liveness, similarity, standing, matched, same_user):
    """Final decision after a blink step-up. Never returns REQUEST_VERIFICATION.

    same_user: the step-up frames matched the same enrolled user as the first
    attempt. A failed or inconclusive step-up is a DENY (the person can retry).
    """
    if standing == "suspended":
        return DENY, "User suspended"
    if liveness < LIVENESS_DENY:
        return DENY, "Spoof detected"
    if not blinked:
        return DENY, "Step-up failed: no blink detected"
    if not matched or not same_user:
        return DENY, "Step-up failed: identity not confirmed"
    if similarity >= SIMILARITY_ALLOW and liveness >= STEPUP_LIVENESS_MIN:
        return ALLOW, "Step-up passed: blink confirmed"
    return DENY, "Step-up failed: low confidence"


def decide_stepup_offline(blinked, liveness, similarity, standing, matched, same_user,
                          pin_ok, pin_locked):
    """DEGRADED step-up: the online rules AND a correct PIN for the matched user."""
    dec, reason = decide_stepup(blinked, liveness, similarity, standing, matched, same_user)
    if dec != ALLOW:
        return dec, reason
    if pin_locked:
        return DENY, "PIN entry locked, see the guard"
    if not pin_ok:
        return DENY, "Step-up failed: wrong or missing PIN"
    return ALLOW, "Step-up passed: blink and PIN confirmed"


# ---- access policy ---------------------------------------------------------
# Final outcomes shown to people. REQUEST_VERIFICATION above is only the
# internal biometric label.

STEP_UP = "STEP_UP_VERIFICATION"
ONLINE, DEGRADED, LOCKED = "ONLINE", "DEGRADED", "LOCKED"
IN_WINDOW, OUTSIDE_WINDOW, NO_RULE = "in_window", "outside_window", "no_rule"


def decide_access(kiosk_state, bio_decision, bio_reason, standing, schedule_status, in_exam):
    """Combine the biometric result with standing, schedule and kiosk state.

    Pure function, first match wins. schedule_status is None when no
    enrolled user matched (there is nobody to check a schedule for).
    Anything not covered below is DENY: the kiosk never fails open.
    """
    if kiosk_state == LOCKED:
        return DENY, "Kiosk locked, see the guard"
    if bio_decision == DENY:
        return DENY, bio_reason
    if standing == "suspended":
        return DENY, "User suspended"
    if schedule_status == NO_RULE:
        return DENY, "No access rule for this room"
    if schedule_status == OUTSIDE_WINDOW:
        if in_exam:
            return STEP_UP, "Exam window, verify"
        return DENY, "Outside permitted schedule"
    if bio_decision == REQUEST_VERIFICATION:
        return STEP_UP, bio_reason
    if bio_decision == ALLOW and schedule_status == IN_WINDOW and standing == "active":
        if kiosk_state == ONLINE:
            return ALLOW, "Live person and identity verified"
        if kiosk_state == DEGRADED:
            return STEP_UP, "Offline: blink and PIN required"
    return DENY, "Access could not be confirmed"


def decide_access_stepup(kiosk_state, stepup_decision, stepup_reason, standing,
                         schedule_status, in_exam):
    """Final decision after a step-up (STEP_UP_VERIFICATION -> ALLOW or DENY).

    stepup_decision comes from decide_stepup (ONLINE) or
    decide_stepup_offline (DEGRADED, needs the PIN). The schedule is checked
    again: a passed step-up only opens doors inside the window, or outside it
    during an exam window.
    """
    if kiosk_state == LOCKED:
        return DENY, "Kiosk locked, see the guard"
    if stepup_decision != ALLOW:
        return DENY, stepup_reason
    if standing == "suspended":
        return DENY, "User suspended"
    if schedule_status == NO_RULE:
        return DENY, "No access rule for this room"
    if schedule_status == OUTSIDE_WINDOW and not in_exam:
        return DENY, "Outside permitted schedule"
    if schedule_status in (IN_WINDOW, OUTSIDE_WINDOW) and standing == "active":
        return ALLOW, stepup_reason
    return DENY, "Access could not be confirmed"


def thresholds():
    return {
        "liveness_allow": LIVENESS_ALLOW,
        "liveness_deny": LIVENESS_DENY,
        "similarity_allow": SIMILARITY_ALLOW,
        "stepup_liveness_min": STEPUP_LIVENESS_MIN,
    }
