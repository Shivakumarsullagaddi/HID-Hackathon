"""FastAPI app: pages, API routes, timing.

    uvicorn backend.main:app --host 127.0.0.1 --port 8000
"""
from dotenv import load_dotenv
from pathlib import Path

# Load environment variables from .env
load_dotenv()
_backend_env = Path(__file__).resolve().parent / ".env"
if _backend_env.exists():
    load_dotenv(_backend_env)
_root_env = Path(__file__).resolve().parent.parent / ".env"
if _root_env.exists():
    load_dotenv(_root_env)

import base64
import logging
import os
from collections import Counter
from contextlib import asynccontextmanager
from typing import Optional

import numpy as np
from pydantic import BaseModel
from fastapi import BackgroundTasks, FastAPI, HTTPException, Response
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from . import config, decision, evaluate
from .models import (BlinkRequest, BlinkResponse, ClipCreate, EnrolRequest, EnrolResponse,
                     StandingUpdate, UserOut, VerifyRequest, VerifyResponse)
from .otp_security import (HardenedVerifyResponse, VerifyOtpRequest, challenge_manager,
                           mask_email, send_otp_email)
from .pipeline import FrameError, Pipeline, decode_frames

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("spoof.main")

state = {}


@asynccontextmanager
async def lifespan(app):
    if config.MODE != "local":
        log.warning("MODE=%s requested but only local mode is implemented; using local.",
                    config.MODE)
    if "pipeline" not in state or state["pipeline"] is None:
        state["pipeline"] = Pipeline()
    status_str = state["pipeline"].status() if hasattr(state["pipeline"], "status") else "ready"
    log.info("pipeline ready: %s", status_str)
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


class ResendOtpRequest(BaseModel):
    challenge_id: str


@app.post("/api/verify", response_model=HardenedVerifyResponse)
@app.post("/verify", response_model=HardenedVerifyResponse)
def verify(req: VerifyRequest, background_tasks: BackgroundTasks = BackgroundTasks()):
    if req.user_id and not pipe().store.get_user(req.user_id):
        raise HTTPException(404, f"unknown user_id {req.user_id}")

    # Anti-replay frame check: reject duplicate frames within 10s
    if req.frames and challenge_manager.is_replay(req.frames):
        return {
            "decision": "DENY",
            "reason": "Replay attack detected: duplicate frame within 10s",
            "liveness": 0.0,
            "similarity": 0.0,
            "matched_user": req.user_id,
            "latency_ms": 0.0,
            "gate_passed": False,
            "liveness_mode": getattr(pipe().liveness, "mode", "ensemble"),
            "request_id": "replay-blocked",
            "gate_reason": "replay_detected",
            "signals": {"replay_detected": True},
        }

    # Rate limiting: 3-strike lockout per user
    if req.user_id and challenge_manager.is_rate_limited(req.user_id):
        return {
            "decision": "DENY",
            "reason": f"User {req.user_id} locked out: 3-strike rate limit exceeded",
            "liveness": 0.0,
            "similarity": 0.0,
            "matched_user": req.user_id,
            "latency_ms": 0.0,
            "gate_passed": False,
            "liveness_mode": getattr(pipe().liveness, "mode", "ensemble"),
            "request_id": "rate-limit-blocked",
            "gate_reason": "rate_limited",
            "signals": {"rate_limited": True},
        }

    result = pipe().verify(frames_or_400(req.frames), req.user_id, req.clip_label)

    # When /verify decides REQUEST VERIFICATION, create an OTP challenge
    if result.get("decision") == "REQUEST_VERIFICATION":
        user_for_challenge = result.get("matched_user") or req.user_id or "demo_user"
        user_record = pipe().store.get_user(user_for_challenge) if user_for_challenge else None
        recipient_email = (user_record.get("email") if user_record else None) or os.environ.get("DEMO_RECIPIENT_EMAIL") or "shivakumarsullagaddi@gmail.com"

        live_val = float(result.get("liveness", 0.0) or 0.0)
        sim_val = float(result.get("similarity", 0.0) or 0.0)

        # Whenever decision is REQUEST_VERIFICATION and meets hard biometric floor
        if live_val >= challenge_manager.LIVENESS_FLOOR and sim_val >= challenge_manager.SIMILARITY_FLOOR:
            try:
                challenge = challenge_manager.create_challenge(
                    user_id=user_for_challenge,
                    liveness_score=live_val,
                    similarity_score=sim_val,
                    email=recipient_email,
                )
                result["challenge_id"] = challenge["challenge_id"]
                masked = mask_email(recipient_email)
                result["masked_email"] = masked

                # Attempt resilient email delivery via SMTP (587 STARTTLS -> 465 SSL)
                email_sent = False
                if recipient_email:
                    email_sent = send_otp_email(recipient_email, challenge["otp_code"], user_for_challenge)

                if email_sent:
                    result["email_delivery_status"] = "sent"
                    result["otp_sent"] = True
                    result["demo_otp"] = None
                    result["fallback_reason"] = None
                else:
                    # Venue Wi-Fi blocked SMTP or credentials failed -> provide backup code
                    result["email_delivery_status"] = "smtp_blocked_fallback"
                    result["otp_sent"] = False
                    result["demo_otp"] = challenge["otp_code"]
                    result["fallback_reason"] = f"Venue Wi-Fi blocked SMTP — Backup Code: {challenge['otp_code']}"
            except Exception as e:
                log.warning("Challenge creation declined: %s", e)
                result["otp_sent"] = False

    return result


@app.post("/api/resend-otp")
@app.post("/resend-otp")
def resend_otp(req: ResendOtpRequest):
    """Re-sends OTP email for an active, unburned challenge."""
    active = challenge_manager.get_active_challenge(req.challenge_id)
    if not active:
        raise HTTPException(400, "Active challenge not found or expired. Please re-verify.")
    user_record = pipe().store.get_user(active["user_id"])
    recipient_email = active.get("email") or (user_record.get("email") if user_record else None) or os.getenv("DEMO_RECIPIENT_EMAIL") or "shivakumarsullagaddi@gmail.com"
    masked = mask_email(recipient_email)
    sent = send_otp_email(recipient_email, active["otp_code"], active["user_id"])
    if sent:
        return {
            "success": True,
            "message": f"6-digit OTP resent to {masked}",
            "masked_email": masked,
            "email_delivery_status": "sent",
        }
    else:
        return {
            "success": True,
            "message": f"Venue Wi-Fi blocked SMTP — Backup Code: {active['otp_code']} (sent to {masked})",
            "masked_email": masked,
            "email_delivery_status": "smtp_blocked_fallback",
            "demo_otp": active["otp_code"],
        }


@app.post("/api/verify-otp")
@app.post("/verify-otp")
def verify_otp_endpoint(req: VerifyOtpRequest, response: Response):
    """Verifies single-use OTP challenge and returns signed ALLOW verdict."""
    result = challenge_manager.verify_otp(req.effective_challenge_id, req.effective_otp)
    if "signature" in result:
        response.headers["X-Verdict-Signature"] = result["signature"]
    if not result.get("verified", False):
        return JSONResponse(status_code=400, content=result)
    return result


@app.get("/api/security/audit-log")
def get_security_audit_log():
    """Audit log trail inspection endpoint."""
    return {
        "valid": challenge_manager.verify_audit_chain(),
        "entries": challenge_manager.get_audit_log(),
    }


@app.post("/api/stepup/blink", response_model=BlinkResponse)
def stepup_blink(req: BlinkRequest):
    return pipe().stepup_blink(frames_or_400(req.frames), req.request_id)


# ---- users -------------------------------------------------------------

@app.post("/api/enrol", response_model=EnrolResponse)
@app.post("/api/enroll", response_model=EnrolResponse)
@app.post("/enroll", response_model=EnrolResponse)
def enrol(req: EnrolRequest):
    try:
        user = pipe().enrol(req.name, frames_or_400(req.frames), email=req.email)
    except FrameError as e:
        raise HTTPException(400, str(e))
    return {"user_id": user["id"], "status": "enrolled", "email": req.email}


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
