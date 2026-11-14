"""Central service (stands in for the cloud side).

    alembic -c alembic_central.ini upgrade head     # by hand, never at startup
    python tools/seed_central.py                    # rooms, people, kiosk key
    uvicorn central.main:app --host 127.0.0.1 --port 9000

Central is never in the kiosk's decision path. It owns users, standing and
schedule, hands each kiosk a signed snapshot, and receives the kiosk's
decisions (the audit trail). Stopping this process is how the outage is shown.
"""
import logging
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from typing import Literal, Optional

from alembic.migration import MigrationContext
from alembic.script import ScriptDirectory
from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request, Response
from fastapi.responses import FileResponse
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, StringConstraints
from sqlalchemy import select
from sqlalchemy.orm import sessionmaker
from typing_extensions import Annotated

from backend.db import alembic_config, make_engine, utcnow

from . import config, service
from .models import Alert, Decision, ExamWindow, Kiosk, Room, ScheduleRule, User
from .reconcile import on_new_record

log = logging.getLogger("spoof.central")
ADMIN_PAGE = config.ROOT / "central" / "admin.html"


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


Id32 = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9_\-]{1,32}$")]
Decision3 = Literal["ALLOW", "STEP_UP_VERIFICATION", "DENY"]


class DecisionIn(Strict):
    record_id: Annotated[str, StringConstraints(min_length=8, max_length=36)]
    seq: int = Field(ge=1)
    request_id: Annotated[str, StringConstraints(min_length=1, max_length=64)]
    stepup_of: Optional[str] = Field(None, max_length=36)
    ts: AwareDatetime
    room_id: Id32
    user_id: Optional[Id32] = None
    bio_decision: Literal["ALLOW", "REQUEST_VERIFICATION", "DENY"]
    final_decision: Decision3
    reason: str = Field(max_length=120)
    kiosk_state: Literal["ONLINE", "DEGRADED", "LOCKED"]
    snapshot_version: Optional[int] = None
    liveness: Optional[float] = None
    similarity: Optional[float] = None
    latency_ms: Optional[float] = None
    liveness_mode: Optional[str] = Field(None, max_length=12)
    prev_hash: Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]
    record_hash: Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]


class DecisionBatch(Strict):
    records: list[DecisionIn] = Field(max_length=config.MAX_BATCH)


class RuleIn(Strict):
    room_id: Id32
    days_mask: int = Field(ge=0, le=127)
    start_min: int = Field(ge=0, lt=1440)
    end_min: int = Field(ge=0, le=1440)


class UserIn(Strict):
    name: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=60)]
    embedding: list[float] = Field(min_length=config.EMBEDDING_DIM, max_length=config.EMBEDDING_DIM)
    model_id: str = Field(config.FACE_MODEL_ID, max_length=64)
    pin: Annotated[str, StringConstraints(pattern=r"^\d{4,8}$")]
    rules: Optional[list[RuleIn]] = None   # default: config.DEFAULT_RULE in the kiosk's room


class StandingIn(Strict):
    standing: Literal["active", "suspended"]


class UserRuleIn(RuleIn):
    user_id: Id32


class ExamWindowIn(Strict):
    room_id: Id32
    starts_at: Optional[AwareDatetime] = None   # default: now
    minutes: int = Field(60, ge=1, le=24 * 60)


def schema_at_head(engine):
    script = ScriptDirectory.from_config(alembic_config("central", str(engine.url)))
    with engine.connect() as conn:
        current = MigrationContext.configure(conn).get_current_revision()
    return current, script.get_current_head()


def create_app(db_url=None, hmac_key=None, clock=None, check_schema=True):
    engine = make_engine(db_url or config.CENTRAL_DB_URL)
    Session = sessionmaker(engine, expire_on_commit=False)
    key = hmac_key or config.SNAPSHOT_HMAC_KEY
    now = clock or utcnow

    @asynccontextmanager
    async def lifespan(app):
        if check_schema:
            current, head = schema_at_head(engine)
            if current != head:
                raise RuntimeError(
                    f"central database is at revision {current}, code expects {head}. "
                    "Run: alembic -c alembic_central.ini upgrade head")
        if key == config.DEV_HMAC_KEY:
            log.warning("SNAPSHOT_HMAC_KEY is the built-in development key. Set your own.")
        yield
        engine.dispose()

    app = FastAPI(title="Spot-a-Spoof central", lifespan=lifespan)
    app.state.Session, app.state.engine = Session, engine

    def db():
        with Session() as s:
            yield s

    def kiosk_auth(x_kiosk_key: Optional[str] = Header(None), s=Depends(db)):
        kiosk = service.kiosk_for_key(s, x_kiosk_key)
        if kiosk is None:
            raise HTTPException(401, "unknown or missing X-Kiosk-Key")
        return kiosk.id

    def commit(s):
        try:
            s.commit()
        except Exception:
            s.rollback()
            raise

    # ---- kiosk-facing ----------------------------------------------------
    @app.get("/health")
    def health():
        return {"ok": True, "time": now().isoformat()}

    @app.get("/snapshot")
    def snapshot(kiosk_id: str, since: Optional[int] = None,
                 kid: str = Depends(kiosk_auth), s=Depends(db)):
        if kiosk_id != kid:
            raise HTTPException(403, "this key belongs to another kiosk")
        kiosk = s.get(Kiosk, kid)
        kiosk.last_seen_at = now()
        commit(s)
        if since is not None and since == service.current_version(s):
            return Response(status_code=304)
        return service.build_snapshot(s, kiosk, now(), key)

    @app.post("/decisions")
    def decisions(batch: DecisionBatch, kid: str = Depends(kiosk_auth), s=Depends(db)):
        kiosk = s.get(Kiosk, kid)
        records = [r.model_dump() for r in batch.records]
        accepted, dups = service.ingest(s, kiosk, records, now(), on_new=app.state.on_new)
        commit(s)
        return {"accepted": accepted, "duplicates": len(dups), "duplicate_ids": dups}

    @app.post("/users")
    def create_user(body: UserIn, kid: str = Depends(kiosk_auth), s=Depends(db)):
        kiosk = s.get(Kiosk, kid)
        rules = ([r.model_dump() for r in body.rules] if body.rules is not None
                 else [{"room_id": kiosk.room_id, **config.DEFAULT_RULE}])
        for r in rules:
            if s.get(Room, r["room_id"]) is None:
                raise HTTPException(400, f"unknown room {r['room_id']}")
        user_id = service.create_user(s, body.name, now(), embedding=body.embedding,
                                      pin=body.pin, model_id=body.model_id, rules=rules)
        commit(s)
        return {"user_id": user_id, "status": "enrolled",
                "snapshot_version": service.current_version(s)}

    # ---- admin (no authentication: bind central to 127.0.0.1) -------------
    @app.patch("/users/{user_id}")
    def patch_user(user_id: str, body: StandingIn, s=Depends(db)):
        try:
            u = service.set_standing(s, user_id, body.standing, now())
        except service.NotFound:
            raise HTTPException(404, "unknown user")
        commit(s)
        return {"id": u.id, "name": u.name, "standing": u.standing,
                "snapshot_version": service.current_version(s)}

    @app.get("/users")
    def users(s=Depends(db)):
        rules = s.scalars(select(ScheduleRule).order_by(ScheduleRule.id)).all()
        by_user = {}
        for r in rules:
            by_user.setdefault(r.user_id, []).append(
                {"id": r.id, "room_id": r.room_id, "days_mask": r.days_mask,
                 "start_min": r.start_min, "end_min": r.end_min})
        return [{"id": u.id, "name": u.name, "standing": u.standing,
                 "has_pin": bool(u.pin_hash), "rules": by_user.get(u.id, [])}
                for u in s.scalars(select(User).order_by(User.id))]

    @app.post("/rules")
    def add_rule(body: UserRuleIn, s=Depends(db)):
        try:
            rule = service.add_rule(s, body.user_id, body.room_id, body.days_mask,
                                    body.start_min, body.end_min)
        except service.NotFound:
            raise HTTPException(404, "unknown user or room")
        commit(s)
        return {"id": rule.id}

    @app.delete("/rules/{rule_id}")
    def delete_rule(rule_id: int, s=Depends(db)):
        try:
            service.delete_rule(s, rule_id)
        except service.NotFound:
            raise HTTPException(404, "unknown rule")
        commit(s)
        return {"deleted": rule_id}

    @app.get("/rooms")
    def rooms(s=Depends(db)):
        return [{"id": r.id, "name": r.name, "risk_level": r.risk_level,
                 "snapshot_ttl_s": r.snapshot_ttl_s} for r in s.scalars(select(Room))]

    @app.get("/exam_windows")
    def exam_windows(s=Depends(db)):
        return [{"id": w.id, "room_id": w.room_id, "starts_at": w.starts_at.isoformat(),
                 "ends_at": w.ends_at.isoformat()}
                for w in s.scalars(select(ExamWindow).order_by(ExamWindow.starts_at))]

    @app.post("/exam_windows")
    def add_exam_window(body: ExamWindowIn, s=Depends(db)):
        start = body.starts_at or now()
        try:
            w = service.add_exam_window(s, body.room_id, start,
                                        start + timedelta(minutes=body.minutes))
        except service.NotFound:
            raise HTTPException(404, "unknown room")
        commit(s)
        return {"id": w.id}

    @app.delete("/exam_windows/{window_id}")
    def delete_exam_window(window_id: int, s=Depends(db)):
        try:
            service.delete_exam_window(s, window_id)
        except service.NotFound:
            raise HTTPException(404, "unknown exam window")
        commit(s)
        return {"deleted": window_id}

    @app.get("/kiosks")
    def kiosks(s=Depends(db)):
        return [{"id": k.id, "room_id": k.room_id, "last_seq": k.last_seq,
                 "last_seen_at": k.last_seen_at.isoformat() if k.last_seen_at else None}
                for k in s.scalars(select(Kiosk))]

    @app.get("/alerts")
    def alerts(limit: int = Query(50, ge=1, le=500), s=Depends(db)):
        return [{"id": a.id, "type": a.type, "record_id": a.record_id, "user_id": a.user_id,
                 "kiosk_id": a.kiosk_id, "detail": a.detail,
                 "created_at": a.created_at.isoformat()}
                for a in s.scalars(select(Alert).order_by(Alert.id.desc()).limit(limit))]

    @app.get("/decisions")
    def list_decisions(limit: int = Query(50, ge=1, le=500), s=Depends(db)):
        rows = s.scalars(select(Decision).order_by(Decision.id.desc()).limit(limit)).all()
        return [{k: (v.isoformat() if isinstance(v, datetime) else v)
                 for k, v in ((c, getattr(d, c)) for c in
                              ("kiosk_id", "record_id", "seq", "request_id", "stepup_of",
                               "ts", "room_id", "user_id", "bio_decision", "final_decision",
                               "reason", "kiosk_state", "snapshot_version", "liveness",
                               "similarity", "latency_ms", "liveness_mode", "received_at"))}
                for d in rows]

    @app.get("/summary")
    def summary(s=Depends(db)):
        return service.counts(s)

    @app.get("/", include_in_schema=False)
    def admin_page(request: Request):
        return FileResponse(ADMIN_PAGE, headers={"Cache-Control": "no-store"})

    # Per-record hook run during intake for each NEW record.
    app.state.on_new = on_new_record
    return app


app = create_app()
