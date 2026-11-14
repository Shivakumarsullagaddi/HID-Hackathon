"""Paths and tunable constants (everything except the decision thresholds,
which live in decision.py so the policy stays self-contained)."""
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = Path(os.environ.get("SPOOF_DATA_DIR", ROOT / "data"))
MODELS_DIR = Path(os.environ.get("SPOOF_MODELS_DIR", ROOT / "models"))
FRONTEND_DIR = ROOT / "frontend"
STATIC_DIR = ROOT / "static"

MODE = os.environ.get("MODE", "local")  # "local" | "aws" (Phase 2, not implemented)

# Quality gate (computed on the frame, downscaled to GATE_WIDTH for stability).
GATE_WIDTH = 640
BRIGHTNESS_MIN = 60.0
BRIGHTNESS_MAX = 200.0
SHARPNESS_MIN = 100.0   # variance of the Laplacian

# MiniFASNet, values read from the upstream source (see README, "Models").
FAS_MODELS = [
    # (file name, crop scale around the face box)
    ("minifasnet_v2.onnx", 2.7),
    ("minifasnet_v1se.onnx", 4.0),
]
FAS_INPUT_SIZE = 80     # 80x80, BGR, float32 in 0-255 (no normalisation)
FAS_REAL_INDEX = 1      # class 1 = real face

# Heuristics: how strongly each one can pull the passive score down.
MOIRE_RATIO_LOW, MOIRE_RATIO_HIGH = 8.0, 20.0     # FFT peak ratio -> 0..1
GLARE_FRAC_LOW, GLARE_FRAC_HIGH = 0.02, 0.10      # saturated pixel fraction -> 0..1
MOTION_STATIC_LOW, MOTION_STATIC_HIGH = 0.15, 0.6  # mean abs frame diff (grey levels)
W_MOIRE, W_GLARE, W_STATIC = 0.5, 0.3, 0.2
HEURISTIC_PENALTY_CAP = 0.6
HEURISTIC_ONLY_CAP = 0.80   # heuristic-only mode can never reach LIVENESS_ALLOW

# Identity. Cosine similarity is mapped to 0-100 through these anchor points.
COSINE_ANCHORS = [0.0, 0.30, 0.50, 1.0]
SIMILARITY_ANCHORS = [0.0, 80.0, 95.0, 100.0]
MATCH_MIN_SIMILARITY = 80.0   # below this the person counts as not enrolled

# Blink step-up.
BLINK_MIN_FRAMES, BLINK_MAX_FRAMES = 8, 30
BLINK_EYE_DIFF_MIN = 6.0     # grey levels, both eyes
BLINK_EYE_TO_CONTROL = 2.0   # eye change must exceed control-region change by this factor
STEPUP_TTL_S = 120

MAX_FRAME_B64_CHARS = 4_000_000

# ---- kiosk database, snapshot and sync (Phase 1b: local-first) -----------

def _env_file(path):
    """Minimal KEY=VALUE reader for data/kiosk.env (written by the seed script)."""
    out = {}
    try:
        for line in Path(path).read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                out[k.strip()] = v.strip()
    except OSError:
        pass
    return out


_KIOSK_ENV = _env_file(DATA_DIR / "kiosk.env")


def setting(name, default=None):
    return os.environ.get(name, _KIOSK_ENV.get(name, default))


KIOSK_DB_PATH = Path(setting("KIOSK_DB_PATH", DATA_DIR / "kiosk.db"))
FACE_MODEL_ID = "buffalo_sc/w600k_mbf"   # templates from any other model are refused
EMBEDDING_DIM = 512

KIOSK_ID = setting("KIOSK_ID", "kiosk_lab_a_1")
KIOSK_ROOM_ID = setting("KIOSK_ROOM_ID", "lab_a")
ROOM_CHOICES = [r.strip() for r in setting("ROOM_CHOICES", "lab_a,hostel_gate").split(",") if r.strip()]
KIOSK_API_KEY = setting("KIOSK_API_KEY")          # printed once by tools/seed_central.py
CENTRAL_URL = setting("CENTRAL_URL", "http://127.0.0.1:9000")
DEV_HMAC_KEY = "dev-only-snapshot-key-change-me"
SNAPSHOT_HMAC_KEY = setting("SNAPSHOT_HMAC_KEY", DEV_HMAC_KEY)
SUPPORTED_SCHEMA_VERSIONS = {1}
ROOM_TIMEZONE = setting("ROOM_TIMEZONE", "Asia/Kolkata")

FORCE_OFFLINE = setting("FORCE_OFFLINE", "0") == "1"
_ttl_override = setting("SNAPSHOT_TTL_OVERRIDE_S")
SNAPSHOT_TTL_OVERRIDE_S = int(_ttl_override) if _ttl_override else None  # can only shorten

BACKGROUND_POLL = setting("BACKGROUND_POLL", "1") == "1"   # tests turn it off
POLL_INTERVAL_S = 5.0
POLL_TIMEOUT_S = 1.0
FAILURES_TO_DEGRADED = 3
CLOCK_BACKWARDS_MAX_S = 300

OUTBOX_BATCH = 100
OUTBOX_SYNCED_RETENTION_S = 24 * 3600
OUTBOX_MAX_ROWS = 50_000

PIN_MAX_FAILURES = 3
PIN_FAILURE_WINDOW_S = 600
PIN_LOCKOUT_S = 600
SPOOF_TYPES = ["print", "screen", "video", "mask"]
CLIP_TYPES = ["live"] + SPOOF_TYPES + ["poor_quality"]
