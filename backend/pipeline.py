"""The verification pipeline shared by the kiosk, the lab and the harness:
quality gate -> liveness -> identity -> biometric decision -> schedule ->
access decision -> log (and outbox for kiosk decisions).

There is one decision path, online or offline: the kiosk always decides
locally from its signed snapshot. Kiosk state only changes how much it
trusts that data."""
import base64
import binascii
import threading
import time
import uuid

import cv2
import numpy as np

from . import config, decision
from .identity import FaceEngine, match, mean_embedding
from .db import utcnow
from .liveness import LivenessEngine, detect_blink
from .pin import PinLockout, verify_pin
from .schedule import check_schedule, in_exam_window
from .state import StaticState
from .store import Store, now_iso


class FrameError(ValueError):
    pass


def decode_frames(frames_b64):
    """base64 (optionally a data: URL) -> list of BGR images."""
    out = []
    for i, s in enumerate(frames_b64):
        if s.startswith("data:"):
            s = s.split(",", 1)[-1]
        try:
            raw = base64.b64decode(s, validate=True)
        except (binascii.Error, ValueError):
            raise FrameError(f"frame {i} is not valid base64")
        img = cv2.imdecode(np.frombuffer(raw, np.uint8), cv2.IMREAD_COLOR)
        if img is None or min(img.shape[:2]) < 64:
            raise FrameError(f"frame {i} is not a decodable image of at least 64x64")
        out.append(img)
    return out


def quality_gate(frames, faces):
    """At least one face, mean brightness in range, Laplacian variance high enough."""
    brightness, sharpness = [], []
    for f in frames:
        h, w = f.shape[:2]
        if w > config.GATE_WIDTH:
            f = cv2.resize(f, (config.GATE_WIDTH, int(h * config.GATE_WIDTH / w)),
                           interpolation=cv2.INTER_AREA)
        gray = cv2.cvtColor(f, cv2.COLOR_BGR2GRAY)
        brightness.append(float(gray.mean()))
        sharpness.append(float(cv2.Laplacian(gray, cv2.CV_64F).var()))
    b, s = float(np.median(brightness)), float(np.median(sharpness))
    detail = {"brightness": round(b, 1), "sharpness": round(s, 1),
              "frames_with_face": sum(f is not None for f in faces)}
    if not any(f is not None for f in faces):
        return False, "no_face", detail
    if b < config.BRIGHTNESS_MIN:
        return False, "too_dark", detail
    if b > config.BRIGHTNESS_MAX:
        return False, "too_bright", detail
    if s < config.SHARPNESS_MIN:
        return False, "blurry", detail
    return True, "ok", detail


class Pipeline:
    def __init__(self, store=None, state=None, clock=None):
        self.store = store or Store()
        self.state = state or StaticState()   # LOCKED unless a state manager is given
        self.clock = clock or utcnow
        self.faces = FaceEngine()
        self.liveness = LivenessEngine()
        self.pin_lockout = PinLockout()
        self._pending = {}           # request_id -> first record awaiting step-up
        self._lock = threading.Lock()

    def _access_context(self, user, room_id, now):
        """Kiosk state and schedule, read on the kiosk. Never taken from the client."""
        kiosk_state, snapshot_version = self.state.current(now)
        with self.store.session() as s:
            schedule_status = check_schedule(s, user["id"], room_id, now) if user else None
            in_exam = in_exam_window(s, room_id, now)
        return kiosk_state, snapshot_version, schedule_status, in_exam

    def status(self):
        return {
            "mode": config.MODE,
            "liveness_mode": self.liveness.mode,
            "liveness_error": self.liveness.error,
            "identity_available": self.faces.identity_available,
            "identity_error": self.faces.error,
            "kiosk_state": self.state.current()[0],
            "kiosk_id": config.KIOSK_ID,
            "room_id": config.KIOSK_ROOM_ID,
        }

    def _analyse(self, frames, user_id=None, identity_frames=None):
        faces = []
        for f in frames:
            found = self.faces.detect(f)
            faces.append(found[0] if found else None)
        live, signals = self.liveness.score(frames, faces)
        idx = identity_frames if identity_frames is not None else range(len(frames))
        emb = mean_embedding([self.faces.embed(frames[i], faces[i])
                              for i in idx if faces[i] is not None])
        user, similarity, cosine = match(emb, self.store.matchable_users(), user_id)
        signals["cosine"] = round(cosine, 4)
        return faces, live, signals, user, similarity

    # ---- first pass ----------------------------------------------------
    def verify(self, frames, user_id=None, clip_label=None, source="kiosk", room_id=None):
        """`decision`/`reason` stay the biometric result (ALLOW,
        REQUEST_VERIFICATION, DENY) so the harness and old clients keep
        working. `final_decision`/`final_reason` are what the door does."""
        t0 = time.perf_counter()
        room_id = room_id or config.KIOSK_ROOM_ID
        faces, live, signals, user, similarity = self._analyse(frames, user_id)
        gate_passed, gate_reason, gate_detail = quality_gate(frames, faces)
        signals.update(gate_detail)
        standing = user["standing"] if user else None
        dec, reason = decision.decide(live, similarity, gate_passed, standing, user is not None)
        now = self.clock()
        kiosk_state, snapshot_version, schedule_status, in_exam = self._access_context(
            user, room_id, now)
        final, final_reason = decision.decide_access(
            kiosk_state, dec, reason, standing, schedule_status, in_exam)
        record = {
            "ts": now_iso(),
            "request_id": uuid.uuid4().hex,
            "record_id": str(uuid.uuid4()),
            "stage": "verify",
            "source": source,
            "room_id": room_id,
            "final_decision": final,
            "final_reason": final_reason,
            "bio_decision": dec,
            "kiosk_state": kiosk_state,
            "snapshot_version": snapshot_version,
            "schedule_status": schedule_status,
            "in_exam_window": in_exam,
            "decision": dec,
            "reason": reason,
            "liveness": round(live, 4),
            "liveness_mode": self.liveness.mode,
            "similarity": round(similarity, 2),
            "matched_user": user["id"] if user else None,
            "matched_name": user["name"] if user else None,
            "standing": standing,
            "claimed_user": user_id,
            "gate_passed": gate_passed,
            "gate_reason": gate_reason,
            "clip_label": clip_label,
            "frames": len(frames),
            "signals": signals,
        }
        record["latency_ms"] = round((time.perf_counter() - t0) * 1000, 1)
        self._persist(record, now)
        if dec == decision.REQUEST_VERIFICATION or final == decision.STEP_UP:
            with self._lock:
                now = time.time()
                self._pending = {k: v for k, v in self._pending.items()
                                 if now - v[0] < config.STEPUP_TTL_S}
                self._pending[record["request_id"]] = (now, record)
        return record

    # ---- blink step-up -------------------------------------------------
    def _persist(self, record, now):
        """Local log always; kiosk decisions also go to the outbox, in the
        same transaction. Lab and harness runs are test tooling: no outbox."""
        self.store.append_decision(record)

    def stepup_blink(self, frames, request_id=None, pin=None):
        """Blink check. With a pending request_id the result replaces the first
        decision and is logged as a second record with the same request_id.
        In DEGRADED state the matched user's PIN is also required."""
        t0 = time.perf_counter()
        n = len(frames)
        sample = sorted({0, n // 4, n // 2, (3 * n) // 4, n - 1})
        first = None
        if request_id:
            with self._lock:
                entry = self._pending.pop(request_id, None)
            if entry and time.time() - entry[0] < config.STEPUP_TTL_S:
                first = entry[1]

        faces = []
        for f in frames:
            found = self.faces.detect(f)
            faces.append(found[0] if found else None)
        blinked, blink_signals = detect_blink(frames, faces)
        sub = [i for i in sample if faces[i] is not None]
        live, signals = self.liveness.score([frames[i] for i in sub], [faces[i] for i in sub])
        signals.update(blink_signals)
        out = {"blinked": blinked, "liveness": round(live, 4), "signals": signals,
               "liveness_mode": self.liveness.mode}
        if first is None:
            out["latency_ms"] = round((time.perf_counter() - t0) * 1000, 1)
            return out

        emb = mean_embedding([self.faces.embed(frames[i], faces[i]) for i in sub])
        user, similarity, cosine = match(emb, self.store.matchable_users(), first.get("claimed_user"))
        signals["cosine"] = round(cosine, 4)
        standing = user["standing"] if user else None
        same_user = user is not None and user["id"] == first.get("matched_user")
        now = self.clock()
        room_id = first.get("room_id") or config.KIOSK_ROOM_ID
        kiosk_state, snapshot_version, schedule_status, in_exam = self._access_context(
            user, room_id, now)
        pin_checked = None
        if kiosk_state == decision.DEGRADED:
            locked = bool(user) and self.pin_lockout.is_locked(user["id"], time.time())
            pin_ok = False
            if user and not locked and pin:
                salt, digest = self.store.get_pin(user["id"])
                pin_ok = verify_pin(pin, salt, digest)
                if pin_ok:
                    self.pin_lockout.record_success(user["id"])
                else:
                    self.pin_lockout.record_failure(user["id"], time.time())
            pin_checked = pin_ok
            dec, reason = decision.decide_stepup_offline(
                blinked, live, similarity, standing, user is not None, same_user,
                pin_ok, locked)
        else:
            dec, reason = decision.decide_stepup(blinked, live, similarity, standing,
                                                 user is not None, same_user)
        final, final_reason = decision.decide_access_stepup(
            kiosk_state, dec, reason, standing, schedule_status, in_exam)
        record = {
            "ts": now_iso(),
            "request_id": first["request_id"],
            "record_id": str(uuid.uuid4()),
            "stepup_of": first.get("record_id"),
            "stage": "stepup",
            "source": first["source"],
            "room_id": room_id,
            "final_decision": final,
            "final_reason": final_reason,
            "bio_decision": dec,
            "kiosk_state": kiosk_state,
            "snapshot_version": snapshot_version,
            "schedule_status": schedule_status,
            "in_exam_window": in_exam,
            "pin_checked": pin_checked,   # True/False/None, never the PIN itself
            "decision": dec,
            "reason": reason,
            "liveness": round(live, 4),
            "liveness_mode": self.liveness.mode,
            "similarity": round(similarity, 2),
            "matched_user": user["id"] if user else None,
            "matched_name": user["name"] if user else None,
            "standing": standing,
            "blinked": blinked,
            "first_decision": first["decision"],
            "first_reason": first["reason"],
            "clip_label": first.get("clip_label"),
            "frames": n,
            "signals": signals,
        }
        record["latency_ms"] = round((time.perf_counter() - t0) * 1000, 1)
        self._persist(record, now)
        out.update({k: record[k] for k in (
            "decision", "reason", "request_id", "record_id", "similarity", "matched_user",
            "matched_name", "latency_ms", "final_decision", "final_reason", "bio_decision",
            "kiosk_state", "snapshot_version", "schedule_status", "room_id", "pin_checked")})
        return out

    # ---- enrolment -----------------------------------------------------
    def enrol(self, name, frames):
        """Mean embedding over 5 frames; every frame must show a usable face."""
        if not self.faces.identity_available:
            raise FrameError(f"identity model unavailable: {self.faces.error}")
        faces = []
        for i, f in enumerate(frames):
            found = self.faces.detect(f)
            if not found:
                raise FrameError(f"no face found in frame {i}")
            faces.append(found[0])
        gate_passed, gate_reason, _ = quality_gate(frames, faces)
        if not gate_passed:
            raise FrameError(f"image quality too low to enrol: {gate_reason}")
        emb = mean_embedding([self.faces.embed(f, fc) for f, fc in zip(frames, faces)])
        return self.store.add_user(name, emb.tolist())
