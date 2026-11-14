"""decide_access: every rule, every outcome, precedence. Plus the low-score
change in decide() and the step-up access rules."""
import itertools

import pytest

from backend.decision import (ALLOW, DEGRADED, DENY, IN_WINDOW, LOCKED, NO_RULE, ONLINE,
                              OUTSIDE_WINDOW, REQUEST_VERIFICATION, STEP_UP, decide,
                              decide_access, decide_access_stepup, decide_stepup_offline)

RV = REQUEST_VERIFICATION


def test_rule1_locked_denies():
    assert decide_access(LOCKED, ALLOW, "ok", "active", IN_WINDOW, False) == (
        DENY, "Kiosk locked, see the guard")


def test_rule1_locked_beats_everything():
    for bio, standing, sched, exam in itertools.product(
            (ALLOW, RV, DENY), ("active", "suspended", None),
            (IN_WINDOW, OUTSIDE_WINDOW, NO_RULE, None), (True, False)):
        assert decide_access(LOCKED, bio, "x", standing, sched, exam) == (
            DENY, "Kiosk locked, see the guard")


def test_rule2_bio_deny_keeps_reason():
    assert decide_access(ONLINE, DENY, "Spoof detected", "active", IN_WINDOW, False) == (
        DENY, "Spoof detected")


def test_rule3_suspended_denies():
    assert decide_access(ONLINE, ALLOW, "ok", "suspended", IN_WINDOW, False) == (
        DENY, "User suspended")


def test_rule3_suspended_beats_schedule_and_exam():
    for sched, exam in itertools.product((IN_WINDOW, OUTSIDE_WINDOW, NO_RULE), (True, False)):
        assert decide_access(ONLINE, ALLOW, "ok", "suspended", sched, exam) == (
            DENY, "User suspended")


def test_rule4_no_rule_denies():
    assert decide_access(ONLINE, ALLOW, "ok", "active", NO_RULE, True) == (
        DENY, "No access rule for this room")


def test_rule5_outside_window_denies():
    assert decide_access(ONLINE, ALLOW, "ok", "active", OUTSIDE_WINDOW, False) == (
        DENY, "Outside permitted schedule")


def test_rule5_outside_window_during_exam_steps_up():
    assert decide_access(ONLINE, ALLOW, "ok", "active", OUTSIDE_WINDOW, True) == (
        STEP_UP, "Exam window, verify")


def test_rule6_bio_request_verification_steps_up_with_bio_reason():
    assert decide_access(ONLINE, RV, "Low confidence", "active", IN_WINDOW, False) == (
        STEP_UP, "Low confidence")
    # nobody matched (no schedule to check), e.g. a blurry frame
    assert decide_access(ONLINE, RV, "Poor image quality", None, None, False) == (
        STEP_UP, "Poor image quality")


def test_rule7_online_in_window_allows():
    assert decide_access(ONLINE, ALLOW, "ok", "active", IN_WINDOW, False)[0] == ALLOW


def test_rule8_degraded_in_window_steps_up():
    assert decide_access(DEGRADED, ALLOW, "ok", "active", IN_WINDOW, False) == (
        STEP_UP, "Offline: blink and PIN required")


def test_allow_only_when_online_in_window_bio_allow_and_active():
    states = (ONLINE, DEGRADED, LOCKED, "garbage")
    for k, bio, standing, sched, exam in itertools.product(
            states, (ALLOW, RV, DENY), ("active", "suspended", None),
            (IN_WINDOW, OUTSIDE_WINDOW, NO_RULE, None), (True, False)):
        final, _ = decide_access(k, bio, "x", standing, sched, exam)
        assert final in (ALLOW, STEP_UP, DENY)
        if final == ALLOW:
            assert (k, bio, standing, sched) == (ONLINE, ALLOW, "active", IN_WINDOW)


def test_stepup_allow_only_for_active_users():
    for k, sd, standing, sched, exam in itertools.product(
            (ONLINE, DEGRADED, LOCKED), (ALLOW, DENY), ("active", "suspended", None),
            (IN_WINDOW, OUTSIDE_WINDOW, NO_RULE, None), (True, False)):
        final, _ = decide_access_stepup(k, sd, "x", standing, sched, exam)
        assert final in (ALLOW, DENY)
        if final == ALLOW:
            assert k != LOCKED and sd == ALLOW and standing == "active"
            assert sched == IN_WINDOW or (sched == OUTSIDE_WINDOW and exam)


def test_unknown_state_or_inputs_fail_closed():
    assert decide_access("garbage", ALLOW, "ok", "active", IN_WINDOW, False)[0] == DENY
    assert decide_access(ONLINE, ALLOW, "ok", "active", None, False)[0] == DENY


# ---- low-score handling in the biometric policy ---------------------------

def test_unmatched_low_liveness_with_good_image_is_spoof():
    assert decide(0.2, 10.0, True, None, False) == (DENY, "Spoof detected")


def test_unmatched_low_liveness_with_failed_gate_is_a_retry():
    assert decide(0.2, 10.0, False, None, False) == (RV, "Poor image quality")


# ---- after a step-up -------------------------------------------------------

@pytest.mark.parametrize("sched,exam,want", [
    (IN_WINDOW, False, ALLOW), (OUTSIDE_WINDOW, True, ALLOW),
    (OUTSIDE_WINDOW, False, DENY), (NO_RULE, True, DENY), (None, False, DENY)])
def test_stepup_access_rechecks_schedule(sched, exam, want):
    assert decide_access_stepup(ONLINE, ALLOW, "passed", "active", sched, exam)[0] == want


def test_stepup_access_locked_suspended_and_failed_stepup_deny():
    assert decide_access_stepup(LOCKED, ALLOW, "p", "active", IN_WINDOW, False)[0] == DENY
    assert decide_access_stepup(ONLINE, ALLOW, "p", "suspended", IN_WINDOW, False)[0] == DENY
    assert decide_access_stepup(ONLINE, DENY, "no blink", "active", IN_WINDOW, False) == (
        DENY, "no blink")


def test_offline_stepup_needs_blink_and_pin():
    ok = dict(blinked=True, liveness=0.9, similarity=97.0, standing="active",
              matched=True, same_user=True)
    assert decide_stepup_offline(**ok, pin_ok=True, pin_locked=False)[0] == ALLOW
    assert decide_stepup_offline(**ok, pin_ok=False, pin_locked=False)[0] == DENY
    assert decide_stepup_offline(**ok, pin_ok=True, pin_locked=True)[0] == DENY
    assert decide_stepup_offline(**{**ok, "blinked": False}, pin_ok=True, pin_locked=False)[0] == DENY
    assert decide_stepup_offline(**{**ok, "standing": "suspended"}, pin_ok=True,
                                 pin_locked=False)[0] == DENY
