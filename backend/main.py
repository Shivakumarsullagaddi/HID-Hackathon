"""FastAPI app: pages, API routes, timing.

    uvicorn backend.main:app --host 127.0.0.1 --port 8000
"""
import base64
import logging
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
from .store import Store

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
    state["db_error"] = kiosk_startup(config.KIOSK_DB_PATH)
    state["pipeline"] = Pipeline(Store(migrate=False))
    log.info("pipeline ready: %s", state["pipeline"].status())
    yield


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
    return pipe().status()


@app.post("/api/verify", response_model=VerifyResponse)
def verify(req: VerifyRequest):
    if req.user_id and not pipe().store.get_user(req.user_id):
        raise HTTPException(404, f"unknown user_id {req.user_id}")
    return pipe().verify(frames_or_400(req.frames), req.user_id, req.clip_label)


@app.post("/api/stepup/blink", response_model=BlinkResponse)
def stepup_blink(req: BlinkRequest):
    return pipe().stepup_blink(frames_or_400(req.frames), req.request_id)


# ---- users -------------------------------------------------------------

@app.post("/api/enrol", response_model=EnrolResponse)
def enrol(req: EnrolRequest):
    try:
        user = pipe().enrol(req.name, frames_or_400(req.frames))
    except FrameError as e:
        raise HTTPException(400, str(e))
    return {"user_id": user["id"], "status": "enrolled"}


@app.get("/api/users", response_model=list[UserOut])
def users():
    return pipe().store.users()  # response_model strips the embeddings


@app.patch("/api/users/{user_id}", response_model=UserOut)
def set_standing(user_id: str, body: StandingUpdate):
    user = pipe().store.set_standing(user_id, body.standing)
    if not user:
        raise HTTPException(404, "unknown user")
    return user


@app.delete("/api/users/{user_id}")
def delete_user(user_id: str):
    if not pipe().store.delete_user(user_id):
        raise HTTPException(404, "unknown user")
    return {"deleted": user_id}


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
