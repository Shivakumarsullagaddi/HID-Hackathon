"""Pydantic request/response models. Requests are strict: unknown fields and
frame lists of the wrong length are rejected with HTTP 422."""
from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, StringConstraints
from typing_extensions import Annotated

from . import config

Frame = Annotated[str, StringConstraints(min_length=16, max_length=config.MAX_FRAME_B64_CHARS)]
Decision = Literal["ALLOW", "REQUEST_VERIFICATION", "DENY"]
Standing = Literal["active", "suspended"]
ClipType = Literal["live", "print", "screen", "video", "mask", "poor_quality"]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


FinalDecision = Literal["ALLOW", "STEP_UP_VERIFICATION", "DENY"]
KioskState = Literal["ONLINE", "DEGRADED", "LOCKED"]
RoomId = Annotated[str, StringConstraints(pattern=r"^[a-z0-9_]{1,32}$")]
Pin = Annotated[str, StringConstraints(pattern=r"^\d{4,8}$")]


class VerifyRequest(StrictModel):
    # extra="forbid": a client can never supply kiosk_state or schedule_status.
    frames: list[Frame] = Field(min_length=3, max_length=5)
    user_id: Optional[str] = Field(None, max_length=32)      # optional 1:1 claim
    clip_label: Optional[str] = Field(None, max_length=120)  # free-text tag, logged
    room_id: Optional[RoomId] = None                         # default: the kiosk's room


class AccessFields(BaseModel):
    final_decision: Optional[FinalDecision] = None
    final_reason: Optional[str] = None
    bio_decision: Optional[Decision] = None
    schedule_status: Optional[Literal["in_window", "outside_window", "no_rule"]] = None
    kiosk_state: Optional[KioskState] = None
    snapshot_version: Optional[int] = None
    record_id: Optional[str] = None
    room_id: Optional[str] = None


class VerifyResponse(AccessFields):
    # decision/reason: the biometric result, kept for compatibility (harness).
    decision: Decision
    reason: str
    liveness: float
    similarity: float
    matched_user: Optional[str]
    latency_ms: float
    gate_passed: bool
    liveness_mode: Literal["ensemble", "heuristic"]
    request_id: str
    # Extras for the UI and the log; not part of the decision contract.
    matched_name: Optional[str] = None
    gate_reason: str = "ok"
    signals: dict = {}


class EnrolRequest(StrictModel):
    name: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=60)]
    frames: list[Frame] = Field(min_length=5, max_length=5)


class EnrolResponse(BaseModel):
    user_id: str
    status: str


class UserOut(BaseModel):
    id: str
    name: str
    standing: Standing


class StandingUpdate(StrictModel):
    standing: Standing


class BlinkRequest(StrictModel):
    frames: list[Frame] = Field(min_length=config.BLINK_MIN_FRAMES,
                                max_length=config.BLINK_MAX_FRAMES)
    request_id: Optional[str] = Field(None, max_length=64)  # links to the first decision
    pin: Optional[Pin] = None   # required when the kiosk is DEGRADED; never logged


class BlinkResponse(AccessFields):
    blinked: bool
    pin_checked: Optional[bool] = None
    liveness: float
    # Present when request_id pointed at a pending REQUEST_VERIFICATION.
    decision: Optional[Decision] = None
    reason: Optional[str] = None
    request_id: Optional[str] = None
    similarity: Optional[float] = None
    matched_user: Optional[str] = None
    matched_name: Optional[str] = None
    latency_ms: Optional[float] = None
    liveness_mode: Optional[str] = None
    signals: dict = {}


class ClipCreate(StrictModel):
    type: ClipType
    person: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=40)]
    enrolled: bool
    lighting: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=40)]
    expected: Optional[Decision] = None   # defaulted from type/enrolled when omitted
    notes: Optional[str] = Field(None, max_length=200)
    frames: list[Frame] = Field(min_length=3, max_length=5)
