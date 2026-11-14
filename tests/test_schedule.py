from datetime import datetime, timedelta, timezone

import pytest

from backend.decision import IN_WINDOW, NO_RULE, OUTSIDE_WINDOW
from backend.kiosk_models import ExamWindow, ScheduleRule
from backend.schedule import ALL_DAYS, WEEKDAYS, check_schedule, evaluate, in_exam_window
from backend.store import Store

IST = 5.5  # Asia/Kolkata, UTC+05:30, no DST


def ist(y, mo, d, h, mi):
    """A UTC datetime for the given India local time."""
    return datetime(y, mo, d, h, mi, tzinfo=timezone.utc) - timedelta(hours=IST)


# 2026-11-16 is a Monday.
MON_10 = ist(2026, 11, 16, 10, 0)
NINE_TO_FIVE = (WEEKDAYS, 9 * 60, 17 * 60)


def test_in_window():
    assert evaluate([NINE_TO_FIVE], MON_10) == IN_WINDOW


def test_outside_window():
    assert evaluate([NINE_TO_FIVE], ist(2026, 11, 16, 18, 0)) == OUTSIDE_WINDOW
    assert evaluate([NINE_TO_FIVE], ist(2026, 11, 16, 17, 0)) == OUTSIDE_WINDOW  # end exclusive
    assert evaluate([NINE_TO_FIVE], ist(2026, 11, 16, 9, 0)) == IN_WINDOW        # start inclusive


def test_no_rule():
    assert evaluate([], MON_10) == NO_RULE


def test_day_mask():
    sat_10 = ist(2026, 11, 21, 10, 0)
    assert evaluate([NINE_TO_FIVE], sat_10) == OUTSIDE_WINDOW
    assert evaluate([(1 << 5, 9 * 60, 17 * 60)], sat_10) == IN_WINDOW   # Saturday only
    assert evaluate([(ALL_DAYS, 0, 0)], sat_10) == IN_WINDOW            # start == end: all day


def test_midnight_crossing():
    night = (1 << 0, 22 * 60, 6 * 60)  # Monday 22:00 to Tuesday 06:00
    assert evaluate([night], ist(2026, 11, 16, 23, 30)) == IN_WINDOW    # Monday night
    assert evaluate([night], ist(2026, 11, 17, 5, 59)) == IN_WINDOW     # Tuesday early
    assert evaluate([night], ist(2026, 11, 17, 6, 0)) == OUTSIDE_WINDOW
    assert evaluate([night], ist(2026, 11, 17, 23, 0)) == OUTSIDE_WINDOW  # Tuesday night: no
    assert evaluate([night], ist(2026, 11, 16, 3, 0)) == OUTSIDE_WINDOW   # Monday early: from Sunday, off


def test_timezone_conversion():
    # 04:00 UTC Monday is 09:30 in India: inside 09:00-17:00 there, outside in UTC terms.
    t = datetime(2026, 11, 16, 4, 0, tzinfo=timezone.utc)
    assert evaluate([NINE_TO_FIVE], t) == IN_WINDOW
    assert evaluate([NINE_TO_FIVE], t, tz="UTC") == OUTSIDE_WINDOW
    # 20:00 UTC Sunday is 01:30 Monday in India: the day changes with the timezone.
    sun_late = datetime(2026, 11, 15, 20, 0, tzinfo=timezone.utc)
    assert evaluate([(1 << 0, 0, 2 * 60)], sun_late) == IN_WINDOW


@pytest.fixture
def store(tmp_path):
    return Store(tmp_path)


def test_check_schedule_from_database(store):
    with store.transaction() as s:
        s.add(ScheduleRule(user_id="u_001", room_id="lab_a", days_mask=WEEKDAYS,
                           start_min=9 * 60, end_min=17 * 60))
    with store.session() as s:
        assert check_schedule(s, "u_001", "lab_a", MON_10) == IN_WINDOW
        assert check_schedule(s, "u_001", "lab_a", ist(2026, 11, 16, 20, 0)) == OUTSIDE_WINDOW
        assert check_schedule(s, "u_001", "hostel_gate", MON_10) == NO_RULE
        assert check_schedule(s, "u_999", "lab_a", MON_10) == NO_RULE


def test_exam_window(store):
    with store.transaction() as s:
        s.add(ExamWindow(room_id="lab_a", starts_at=MON_10, ends_at=MON_10 + timedelta(hours=3)))
    with store.session() as s:
        assert in_exam_window(s, "lab_a", MON_10 + timedelta(minutes=1))
        assert in_exam_window(s, "lab_a", MON_10)                         # start inclusive
        assert not in_exam_window(s, "lab_a", MON_10 + timedelta(hours=3))  # end exclusive
        assert not in_exam_window(s, "lab_a", MON_10 - timedelta(minutes=1))
        assert not in_exam_window(s, "hostel_gate", MON_10 + timedelta(minutes=1))
