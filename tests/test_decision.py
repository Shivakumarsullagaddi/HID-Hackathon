from backend.decision import decide, decide_stepup


def test_suspended_user_live_face_is_denied():
    assert decide(0.95, 98.0, True, "suspended", True) == ("DENY", "User suspended")


def test_matched_user_low_liveness_is_denied():
    assert decide(0.2, 97.0, True, "active", True) == ("DENY", "Spoof detected")


def test_unmatched_live_person_is_denied():
    decision, reason = decide(0.95, 10.0, True, None, False)
    assert decision == "DENY"
    assert "not enrolled" in reason


def test_matched_live_high_similarity_is_allowed():
    assert decide(0.9, 97.0, True, "active", True)[0] == "ALLOW"


def test_gate_failed_requests_verification():
    assert decide(0.9, 97.0, False, "active", True) == (
        "REQUEST_VERIFICATION", "Poor image quality")


def test_matched_mid_liveness_requests_verification():
    assert decide(0.6, 97.0, True, "active", True) == (
        "REQUEST_VERIFICATION", "Low confidence")


# Extra cases beyond the required six.

def test_matched_live_but_similarity_below_threshold_is_not_allowed():
    assert decide(0.95, 94.9, True, "active", True)[0] == "REQUEST_VERIFICATION"


def test_suspended_wins_over_spoof_and_gate():
    assert decide(0.1, 99.0, False, "suspended", True)[0] == "DENY"


def test_unmatched_spoof_is_never_allowed():
    assert decide(0.1, 0.0, True, None, False)[0] != "ALLOW"


def test_exhaustive_grid_only_allows_when_every_signal_agrees():
    for liveness in (0.0, 0.39, 0.4, 0.6, 0.84, 0.85, 1.0):
        for similarity in (0.0, 80.0, 94.9, 95.0, 100.0):
            for gate in (True, False):
                for standing in ("active", "suspended", None):
                    for matched in (True, False):
                        decision, _ = decide(liveness, similarity, gate, standing, matched)
                        if decision == "ALLOW":
                            assert gate and matched and standing != "suspended"
                            assert liveness >= 0.85 and similarity >= 95


def test_stepup_blink_confirms_borderline_live_match():
    assert decide_stepup(True, 0.7, 97.0, "active", True, True)[0] == "ALLOW"


def test_stepup_without_blink_is_denied():
    assert decide_stepup(False, 0.95, 99.0, "active", True, True)[0] == "DENY"


def test_stepup_never_rescues_spoof_suspended_or_identity_swap():
    assert decide_stepup(True, 0.3, 99.0, "active", True, True)[0] == "DENY"
    assert decide_stepup(True, 0.9, 99.0, "suspended", True, True)[0] == "DENY"
    assert decide_stepup(True, 0.9, 99.0, "active", True, False)[0] == "DENY"
    assert decide_stepup(True, 0.9, 99.0, None, False, False)[0] == "DENY"
    assert decide_stepup(True, 0.9, 90.0, "active", True, True)[0] == "DENY"
