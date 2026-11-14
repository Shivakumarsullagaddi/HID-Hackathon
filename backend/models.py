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
RoleType = Literal["student", "faculty", "contractor", "security_admin"]
ZoneType = Literal["main_gate", "research_lab", "server_room"]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class VerifyRequest(StrictModel):
    frames: list[Frame] = Field(min_length=3, max_length=5)
    user_id: Optional[str] = Field(None, max_length=32)      # optional 1:1 claim
    clip_label: Optional[str] = Field(None, max_length=120)  # free-text tag, logged
    zone: Optional[ZoneType] = None
    access_hour: Optional[int] = Field(default=None, ge=0, le=23)


class VerifyResponse(BaseModel):
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
    role: Optional[str] = None
    zone: str = "main_gate"
    access_hour: Optional[int] = None
    schedule_status: str = "ALLOW"
    schedule_reason: Optional[str] = None
    offline_mode: bool = False
    offline_queue_size: int = 0


class EnrolRequest(StrictModel):
    name: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=60)]
    frames: list[Frame] = Field(min_length=5, max_length=5)
    role: Optional[RoleType] = None


class EnrolResponse(BaseModel):
    user_id: str
    status: str


class UserOut(BaseModel):
    id: str
    name: str
    standing: Standing
    role: Optional[str] = None


class StandingUpdate(StrictModel):
    standing: Optional[Standing] = None
    role: Optional[RoleType] = None


class ConnectivityUpdate(StrictModel):
    online: bool


class BlinkRequest(StrictModel):
    frames: list[Frame] = Field(min_length=config.BLINK_MIN_FRAMES,
                                max_length=config.BLINK_MAX_FRAMES)
    request_id: Optional[str] = Field(None, max_length=64)  # links to the first decision
    zone: Optional[ZoneType] = None
    access_hour: Optional[int] = Field(default=None, ge=0, le=23)


class BlinkResponse(BaseModel):
    blinked: bool
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
    role: Optional[str] = None
    zone: str = "main_gate"
    access_hour: Optional[int] = None
    schedule_status: str = "ALLOW"
    schedule_reason: Optional[str] = None
    offline_mode: bool = False
    offline_queue_size: int = 0



class ClipCreate(StrictModel):
    type: ClipType
    person: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=40)]
    enrolled: bool
    lighting: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=40)]
    expected: Optional[Decision] = None   # defaulted from type/enrolled when omitted
    notes: Optional[str] = Field(None, max_length=200)
    frames: list[Frame] = Field(min_length=3, max_length=5)
