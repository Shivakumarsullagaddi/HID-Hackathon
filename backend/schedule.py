"""Campus access schedule, evaluated in the room's local time.

A rule allows a user into a room on the days in days_mask (bit 0 = Monday
... bit 6 = Sunday) between start_min and end_min (minutes since local
midnight). end_min < start_min crosses midnight: the part after midnight
belongs to the day the rule started. start_min == end_min means all day.
"""
from zoneinfo import ZoneInfo

from sqlalchemy import select

from . import config
from .decision import IN_WINDOW, NO_RULE, OUTSIDE_WINDOW
from .kiosk_models import ExamWindow, ScheduleRule

ALL_DAYS = 0b1111111
WEEKDAYS = 0b0011111


def room_tz(tz=None):
    return ZoneInfo(tz or config.ROOM_TIMEZONE)


def rule_allows(days_mask, start_min, end_min, local_dt):
    minute = local_dt.hour * 60 + local_dt.minute
    today = local_dt.weekday()

    def day_on(d):
        return bool((days_mask >> d) & 1)

    if start_min == end_min:
        return day_on(today)
    if start_min < end_min:
        return day_on(today) and start_min <= minute < end_min
    # Crosses midnight: evening part today, early-morning part from yesterday's rule.
    return (day_on(today) and minute >= start_min) or (
        day_on((today - 1) % 7) and minute < end_min)


def evaluate(rules, now_utc, tz=None):
    """rules: iterable of (days_mask, start_min, end_min) for one user and room."""
    rules = list(rules)
    if not rules:
        return NO_RULE
    local = now_utc.astimezone(room_tz(tz))
    return IN_WINDOW if any(rule_allows(m, s, e, local) for m, s, e in rules) else OUTSIDE_WINDOW


def check_schedule(session, user_id, room_id, now_utc, tz=None):
    rows = session.execute(
        select(ScheduleRule.days_mask, ScheduleRule.start_min, ScheduleRule.end_min)
        .where(ScheduleRule.user_id == user_id, ScheduleRule.room_id == room_id)).all()
    return evaluate([tuple(r) for r in rows], now_utc, tz)


def in_exam_window(session, room_id, now_utc):
    hit = session.scalars(
        select(ExamWindow.id).where(ExamWindow.room_id == room_id,
                                    ExamWindow.starts_at <= now_utc,
                                    ExamWindow.ends_at > now_utc).limit(1)).first()
    return hit is not None
