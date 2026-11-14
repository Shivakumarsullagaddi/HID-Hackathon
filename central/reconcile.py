"""Reconciler: runs on every NEW decision central receives (duplicates are
ignored before this point, so each record is reconciled exactly once).

offline_allow_after_suspension: the kiosk was not ONLINE, the final decision
was ALLOW (first pass or a passed step-up), and standing_events show the
user's standing at that moment was already 'suspended'. The kiosk could not
have known: its snapshot predates the suspension. The alert is for a human
to follow up.

repeated_failed_attempts: the kiosk reported 3 failed attempts in 2 minutes.

outbox_tampered: the record breaks the kiosk's hash chain (service.check_chain).
"""
from sqlalchemy import select

from .models import StandingEvent
from .service import add_alert, check_chain


def standing_at(s, user_id, ts):
    """The user's standing at time ts, from standing_events (None if unknown)."""
    ev = s.scalars(select(StandingEvent)
                   .where(StandingEvent.user_id == user_id, StandingEvent.at <= ts)
                   .order_by(StandingEvent.at.desc(), StandingEvent.id.desc()).limit(1)).first()
    return ev.new_standing if ev else None


def on_new_record(s, kiosk, row):
    check_chain(s, kiosk, row)          # outbox_tampered on a hash-chain break
    if (row.user_id and row.kiosk_state != "ONLINE" and row.final_decision == "ALLOW"
            and standing_at(s, row.user_id, row.ts) == "suspended"):
        add_alert(s, "offline_allow_after_suspension", row.received_at,
                  record_id=row.record_id, user_id=row.user_id, kiosk_id=kiosk.id,
                  detail=(f"{row.user_id} was allowed into {row.room_id} at {row.ts.isoformat()} "
                          f"while the kiosk was {row.kiosk_state} (snapshot version "
                          f"{row.snapshot_version}); the user was already suspended."))
    if row.reason == "Repeated failed attempts":
        add_alert(s, "repeated_failed_attempts", row.received_at, record_id=row.record_id,
                  kiosk_id=kiosk.id,
                  detail=f"3 failed attempts within 2 minutes at {row.room_id}, {row.ts.isoformat()}")
