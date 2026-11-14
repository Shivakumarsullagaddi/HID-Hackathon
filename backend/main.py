"""FastAPI app: pages, API routes, timing.

    uvicorn backend.main:app --host 127.0.0.1 --port 8000
"""
import asyncio
import base64
import logging
import tempfile
from pathlib import Path

from typing import Optional

import httpx
from pydantic import BaseModel, ConfigDict, Field
from collections import Counter
from contextlib import asynccontextmanager

import numpy as np
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from . import config, decision, evaluate
from .models import (BlinkRequest, BlinkResponse, ClipCreate, EnrolRequest, EnrolResponse,
                     StandingUpdate, UserOut, VerifyRequest, VerifyResponse)
from .db import kiosk_startup
from .pipeline import FrameError, Pipeline, decode_frames
from .state import StateManager
from .store import Store
from .sync import SyncAgent, SyncError

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("spoof.main")

state = {}


@asynccontextmanager
async def lifespan(app):
    if config.MODE != "local":
        log.warning("MODE=%s requested but only local mode is implemented; using local.",
                    config.MODE)
    # Back up data/kiosk.db, then upgrade it. On failure the backup is restored
    # and the kiosk starts LOCKED (see state.py).
    db_error = kiosk_startup(config.KIOSK_DB_PATH)
    try:
        store = Store(migrate=False)
    except Exception as e:  # noqa: BLE001 - labelled fallback, kiosk stays LOCKED
        db_error = db_error or f"{type(e).__name__}: {e}"
        fallback = Path(tempfile.mkdtemp(prefix="kiosk-locked-"))
        log.error("kiosk database unusable (%s); running LOCKED on an empty "
                  "temporary database at %s", db_error, fallback)
        store = Store(fallback)
    http = httpx.Client(timeout=config.POLL_TIMEOUT_S)
    agent = make_sync_agent(store, http)
    manager = StateManager(store, health=lambda: central_healthy(http), db_error=db_error,
                           sync=agent.sync if agent else None)
    if agent:
        agent.on_applied = manager.snapshot_applied
        manager.warnings = agent.warnings
    state.update(db_error=db_error, http=http, agent=agent, manager=manager)
    state["pipeline"] = Pipeline(store, state=manager)
    log.info("pipeline ready: %s", state["pipeline"].status())
    task = asyncio.create_task(poll_forever(manager)) if config.BACKGROUND_POLL else None
    try:
        yield
    finally:
        if task:
            task.cancel()
        http.close()


def central_healthy(http):
    r = http.get(f"{config.CENTRAL_URL}/health")
    return r.status_code == 200 and r.json().get("ok") is True


def make_sync_agent(store, http):
    """Sync agent (push outbox, pull snapshot). Without a kiosk API key the
    kiosk cannot sync: it stays LOCKED (no snapshot) and says why."""
    if not config.KIOSK_API_KEY:
        log.error("KIOSK_API_KEY is not set: this kiosk cannot reach central and will stay "
                  "LOCKED. Run: python tools/seed_central.py --write-env data/kiosk.env")
        return None
    return SyncAgent(store, http, config.CENTRAL_URL, config.KIOSK_ID, config.KIOSK_API_KEY,
                     config.SNAPSHOT_HMAC_KEY, ttl_override_s=config.SNAPSHOT_TTL_OVERRIDE_S)


async def poll_forever(manager):
    """Poll central every 5 s (1 s timeout). Runs the blocking poll in a
    thread so verification requests are never held up by the network."""
    while True:
        try:
            await asyncio.to_thread(manager.poll_once)
        except Exception:  # noqa: BLE001 - keep polling whatever happens
            log.exception("poll failed")
        await asyncio.sleep(config.POLL_INTERVAL_S)


app = FastAPI(title="Spot-a-Spoof", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=config.STATIC_DIR), name="static")


def pipe() -> Pipeline:
    return state["pipeline"]


def frames_or_400(frames_b64):
    try:
        return decode_frames(frames_b64)
    except FrameError as e:
        raise HTTPException(400, str(e))


# ---- pages -------------------------------------------------------------

def _page(name):
    return FileResponse(config.FRONTEND_DIR / name, headers={"Cache-Control": "no-store"})


@app.get("/", include_in_schema=False)
def kiosk_page():
    return _page("index.html")


@app.get("/enrol", include_in_schema=False)
def enrol_page():
    return _page("enrol.html")


@app.get("/lab", include_in_schema=False)
def lab_page():
    return _page("lab.html")


@app.get("/metrics", include_in_schema=False)
def metrics_page():
    return _page("metrics.html")


# ---- verify and step-up ------------------------------------------------

@app.get("/api/status")
def status():
    return {**pipe().status(), "central_configured": state.get("agent") is not None,
            "central_url": config.CENTRAL_URL}


@app.get("/api/state")
def kiosk_state():
    """State, snapshot version and age, time left before LOCKED, outbox size."""
    return pipe().state.info()


class SimulateIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    outage: bool


@app.post("/api/state/simulate")
def simulate_outage(body: SimulateIn):
    """Demo control, labelled SIMULATED in the UI: polls are skipped and count
    as failures, exactly as if central were unreachable."""
    manager = state.get("manager")
    if manager is None:
        raise HTTPException(409, "no state manager")
    return manager.set_simulated_outage(body.outage)


@app.post("/api/verify", response_model=VerifyResponse)
def verify(req: VerifyRequest):
    if req.user_id and not pipe().store.get_user(req.user_id):
        raise HTTPException(404, f"unknown user_id {req.user_id}")
    if req.room_id and req.room_id not in config.ROOM_CHOICES:
        raise HTTPException(400, f"unknown room_id {req.room_id}")
    return pipe().verify(frames_or_400(req.frames), req.user_id, req.clip_label,
                         room_id=req.room_id)


@app.post("/api/stepup/blink", response_model=BlinkResponse)
def stepup_blink(req: BlinkRequest):
    return pipe().stepup_blink(frames_or_400(req.frames), req.request_id, pin=req.pin)


# ---- users -------------------------------------------------------------

CENTRAL_OWNS_USERS = ("Users are managed by the central service: change standing on the "
                      "central admin page (port 9000).")


def agent():
    """The sync agent, or None when the kiosk runs without a central service
    (tests, local-only development)."""
    return state.get("agent")


@app.post("/api/enrol", response_model=EnrolResponse)
def enrol(req: EnrolRequest):
    frames = frames_or_400(req.frames)
    a = agent()
    if a is None:   # no central configured: local-only enrolment (tests, development)
        try:
            user = pipe().enrol(req.name, frames)
        except FrameError as e:
            raise HTTPException(400, str(e))
        log.warning("enrolled %s locally only: no central service configured", user["id"])
        return {"user_id": user["id"], "status": "enrolled"}
    if not req.pin:
        raise HTTPException(422, "a 4 to 8 digit PIN is required")
    try:
        embedding = pipe().embed_for_enrol(frames)
    except FrameError as e:
        raise HTTPException(400, str(e))
    try:
        body = a.enrol(req.name, embedding, req.pin)
    except (httpx.HTTPError, SyncError) as e:
        log.error("enrolment failed: %s", e)
        raise HTTPException(503, "Enrolment needs the central service, which is not reachable.")
    return {"user_id": body["user_id"], "status": "enrolled"}


@app.get("/api/users", response_model=list[UserOut])
def users():
    return pipe().store.users()  # response_model strips the embeddings


@app.patch("/api/users/{user_id}", response_model=UserOut)
def set_standing(user_id: str, body: StandingUpdate):
    if agent() is not None:
        raise HTTPException(409, CENTRAL_OWNS_USERS)
    user = pipe().store.set_standing(user_id, body.standing)
    if not user:
        raise HTTPException(404, "unknown user")
    return user


@app.delete("/api/users/{user_id}")
def delete_user(user_id: str):
    if agent() is not None:
        raise HTTPException(409, CENTRAL_OWNS_USERS)
    if not pipe().store.delete_user(user_id):
        raise HTTPException(404, "unknown user")
    return {"deleted": user_id}


@app.post("/api/sync")
def sync_now():
    """Run one poll now (health, push, pull) instead of waiting up to 5 s."""
    manager = state.get("manager")
    if manager is None:
        raise HTTPException(409, "no state manager")
    manager.poll_once()
    return manager.info()


class AttemptsIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    room_id: Optional[str] = Field(None, pattern=r"^[a-z0-9_]{1,32}$")


@app.post("/api/attempts/failed")
def failed_attempts(body: AttemptsIn):
    """Audit record after 3 failed attempts within 2 minutes (kiosk page).
    Report only: it never opens anything."""
    rec = pipe().report_failed_attempts(body.room_id)
    return {"record_id": rec["record_id"], "final_decision": rec["final_decision"]}


# ---- spoof lab ---------------------------------------------------------

@app.get("/api/lab/clips")
def lab_clips():
    store = pipe().store
    return [{**c, "frames": len(store.clip_frame_paths(c["name"]))}
            for c in store.expected()["clips"]]


@app.post("/api/lab/clips")
def lab_record(req: ClipCreate):
    frames_or_400(req.frames)  # validate before anything is written
    raw = [base64.b64decode(f.split(",", 1)[-1]) for f in req.frames]
    meta = req.model_dump(exclude={"frames"})
    meta["expected"] = meta["expected"] or evaluate.default_expected(req.type, req.enrolled)
    return pipe().store.add_clip(meta, raw)


@app.delete("/api/lab/clips/{name}")
def lab_delete(name: str):
    if not pipe().store.delete_clip(name):
        raise HTTPException(404, "unknown clip")
    return {"deleted": name}


@app.post("/api/lab/clips/{name}/run")
def lab_run(name: str):
    store = pipe().store
    clip = next((c for c in store.expected()["clips"] if c["name"] == name), None)
    if not clip:
        raise HTTPException(404, "unknown clip")
    frames = evaluate.load_clip(store, name)
    if len(frames) < 3:
        raise HTTPException(409, f"clip has {len(frames)} readable frames, need 3")
    rec = pipe().verify(frames, clip_label=name, source="lab")
    return {"expected": clip["expected"], "actual": rec["decision"],
            "correct": rec["decision"] == clip["expected"], "result": rec}


# ---- metrics -----------------------------------------------------------

@app.get("/api/metrics")
def metrics():
    store = pipe().store
    kiosk = [r for r in store.read_decisions() if r.get("source") == "kiosk"]
    first = [r for r in kiosk if r.get("stage") == "verify"]
    lat = [r["latency_ms"] for r in first]
    return {
        "status": pipe().status(),
        "thresholds": {
            **decision.thresholds(),
            "match_min_similarity": config.MATCH_MIN_SIMILARITY,
            "brightness_min": config.BRIGHTNESS_MIN,
            "brightness_max": config.BRIGHTNESS_MAX,
            "sharpness_min": config.SHARPNESS_MIN,
        },
        "kiosk_log": {
            "n": len(first),
            "decisions": dict(Counter(r["decision"] for r in first)),
            "final_decisions": dict(Counter(r["final_decision"] for r in first
                                            if r.get("final_decision"))),
            "stepups": dict(Counter(r["decision"] for r in kiosk if r.get("stage") == "stepup")),
            "liveness_modes": dict(Counter(r["liveness_mode"] for r in first)),
            "p50_ms": round(float(np.percentile(lat, 50)), 1) if lat else None,
            "p95_ms": round(float(np.percentile(lat, 95)), 1) if lat else None,
        },
        "harness": store.read_metrics(),
    }


@app.post("/api/evaluate")
def run_evaluate():
    return evaluate.run_harness(pipe())
