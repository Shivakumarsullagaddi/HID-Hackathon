"""Seed the central database. Idempotent: run it again and nothing changes.

    alembic -c alembic_central.ini upgrade head
    python tools/seed_central.py --write-env data/kiosk.env

Creates, if missing:
  - rooms lab_a (snapshot TTL 600 s) and hostel_gate (3600 s)
  - the people enrolled on this kiosk (data/gallery.json), each with a rule
    for lab_a, every day 07:00-22:00 local, and a random 4-digit PIN
  - one suspended user (placeholder, no face template)
  - one exam window for lab_a (tomorrow 10:00-13:00 local)
  - kiosk kiosk_lab_a_1 in lab_a with a new API key

Secrets (API key, PINs) are printed ONCE, here, and never stored in clear:
central keeps only hashes. --write-env also saves the kiosk settings to a
git-ignored file the kiosk reads at startup. --rotate-key issues a new key.
"""
import argparse
import json
import secrets
import sys
from datetime import datetime, time, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from sqlalchemy import select  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402

from backend.db import make_engine, utcnow  # noqa: E402
from central import config, service  # noqa: E402
from central.main import schema_at_head  # noqa: E402
from central.models import ExamWindow, Kiosk, User  # noqa: E402

KIOSK_ID, KIOSK_ROOM = "kiosk_lab_a_1", "lab_a"
LAB_RULE = {"room_id": "lab_a", "days_mask": 0b1111111, "start_min": 7 * 60, "end_min": 22 * 60}


def seed(db_url, gallery_path, rotate_key=False, write_env=None, out=print):
    engine = make_engine(db_url)
    current, head = schema_at_head(engine)
    if current != head:
        raise SystemExit(f"central database is at {current}, expected {head}. Run first:\n"
                         "  alembic -c alembic_central.ini upgrade head")
    Session = sessionmaker(engine, expire_on_commit=False)
    now = utcnow()
    secrets_out = {}
    with Session() as s, s.begin():
        service.upsert_room(s, "lab_a", "Lab A", "high", 600)
        service.upsert_room(s, "hostel_gate", "Hostel gate", "low", 3600)
        s.flush()

        people = []
        if gallery_path and Path(gallery_path).exists():
            people = json.loads(Path(gallery_path).read_text(encoding="utf-8")).get("users", [])
        for p in people:
            if s.get(User, p["id"]) is not None:
                continue
            pin = f"{secrets.randbelow(10_000):04d}"
            service.create_user(s, p["name"], now, embedding=p["embedding"], pin=pin,
                                standing=p.get("standing", "active"), rules=[LAB_RULE],
                                user_id=p["id"])
            secrets_out.setdefault("pins", []).append((p["id"], p["name"], pin))

        if s.scalars(select(User.id).where(User.standing == "suspended")).first() is None:
            uid = service.create_user(s, "Carol (seed, suspended)", now, standing="suspended",
                                      rules=[LAB_RULE])
            out(f"created suspended placeholder user {uid} (no face template)")

        if s.scalars(select(ExamWindow.id).where(ExamWindow.room_id == "lab_a")).first() is None:
            tz = ZoneInfo(config.ROOM_TIMEZONE)
            day = (datetime.now(tz) + timedelta(days=1)).date()
            start = datetime.combine(day, time(10, 0), tz).astimezone(timezone.utc)
            service.add_exam_window(s, "lab_a", start, start + timedelta(hours=3))
            out(f"created exam window for lab_a on {day} 10:00-13:00 {config.ROOM_TIMEZONE}")

        kiosk = s.get(Kiosk, KIOSK_ID)
        if kiosk is None:
            secrets_out["api_key"] = service.create_kiosk(s, KIOSK_ID, KIOSK_ROOM)
        elif rotate_key:
            secrets_out["api_key"] = service.rotate_kiosk_key(s, KIOSK_ID)
        version = service.current_version(s)
    engine.dispose()

    out(f"rooms: lab_a, hostel_gate | snapshot version {version}")
    for uid, name, pin in secrets_out.get("pins", []):
        out(f"user {uid} {name}: PIN {pin}   <- shown once; central stores only a hash")
    if "api_key" in secrets_out:
        out(f"kiosk {KIOSK_ID} API key: {secrets_out['api_key']}   <- shown once")
        if write_env:
            Path(write_env).write_text(
                f"KIOSK_ID={KIOSK_ID}\nKIOSK_ROOM_ID={KIOSK_ROOM}\n"
                f"KIOSK_API_KEY={secrets_out['api_key']}\n", encoding="utf-8")
            out(f"wrote kiosk settings to {write_env} (git-ignored)")
    else:
        out(f"kiosk {KIOSK_ID} already exists; key unchanged (use --rotate-key for a new one)")
    return secrets_out


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--db-url", default=config.CENTRAL_DB_URL)
    ap.add_argument("--gallery", default=str(ROOT / "data" / "gallery.json"))
    ap.add_argument("--rotate-key", action="store_true")
    ap.add_argument("--write-env", default=None)
    a = ap.parse_args()
    seed(a.db_url, a.gallery, a.rotate_key, a.write_env)
