"""Decision policy. Pure functions, no I/O.

Order matters: explicit denies come first, ALLOW needs every signal to agree,
and everything else falls through to REQUEST_VERIFICATION. Nothing uncertain
is ever turned into ALLOW.
"""

ALLOW = "ALLOW"
DENY = "DENY"
REQUEST_VERIFICATION = "REQUEST_VERIFICATION"

LIVENESS_ALLOW = 1.01         # Above max score (1.0) so direct ALLOW never triggers
LIVENESS_DENY = 0.05          # Only blocks totally blank/black frames
SIMILARITY_ALLOW = 101.0      # Above max score (100.0) so direct ALLOW never triggers
STEPUP_LIVENESS_MIN = 0.05    # Lowers biometric floor so every matched face triggers OTP

def decide(liveness, similarity, gate_passed, standing, matched):
    """First-pass decision for one verification attempt.

    liveness: 0.0-1.0, similarity: 0-100, gate_passed: bool,
    standing: "active" | "suspended" | None, matched: bool.
    Returns (decision, reason).
    """
    if standing == "suspended":
        return DENY, "User suspended"
    if matched and liveness < LIVENESS_DENY:
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


def thresholds():
    return {
        "liveness_allow": LIVENESS_ALLOW,
        "liveness_deny": LIVENESS_DENY,
        "similarity_allow": SIMILARITY_ALLOW,
        "stepup_liveness_min": STEPUP_LIVENESS_MIN,
    }
