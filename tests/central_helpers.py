"""Test helpers: a migrated central database, a seeded kiosk, and a
TestClient for the central app (an httpx.Client, so the kiosk's sync agent
can use it as its transport)."""
from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient
from sqlalchemy.orm import sessionmaker

from backend.db import make_engine, sqlite_url, upgrade
from central import service
from central.main import create_app

HMAC_KEY = "test-hmac-key"
T0 = datetime(2026, 11, 16, 4, 30, tzinfo=timezone.utc)   # Monday 10:00 India time


class Clock:
    def __init__(self, t=T0):
        self.t = t

    def __call__(self):
        return self.t

    def advance(self, **kw):
        self.t = self.t + timedelta(**kw)


class Central:
    def __init__(self, tmp_path, clock=None, reconcile=True):
        self.url = sqlite_url(tmp_path / "central.db")
        upgrade("central", self.url)
        self.clock = clock or Clock()
        self.app = create_app(self.url, HMAC_KEY, clock=self.clock)
        if reconcile:
            try:
                from central.reconcile import on_new_record
                self.app.state.on_new = on_new_record
            except ImportError:
                pass
        self.engine = make_engine(self.url)
        self.Session = sessionmaker(self.engine, expire_on_commit=False)
        with self.Session() as s, s.begin():
            service.upsert_room(s, "lab_a", "Lab A", "high", 600)
            service.upsert_room(s, "hostel_gate", "Hostel gate", "low", 3600)
            s.flush()
            self.api_key = service.create_kiosk(s, "kiosk_lab_a_1", "lab_a")
        self.client = TestClient(self.app)
        self.client.__enter__()

    def close(self):
        self.client.__exit__(None, None, None)
        self.engine.dispose()

    def headers(self):
        return {"X-Kiosk-Key": self.api_key}

    def add_user(self, name, embedding=None, pin="4321", standing="active", rules=None):
        rules = [{"room_id": "lab_a", "days_mask": 127, "start_min": 9 * 60,
                  "end_min": 17 * 60}] if rules is None else rules
        with self.Session() as s, s.begin():
            return service.create_user(s, name, self.clock(), embedding=embedding, pin=pin,
                                       standing=standing, rules=rules)
