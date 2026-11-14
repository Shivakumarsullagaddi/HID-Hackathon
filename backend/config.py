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
SHARPNESS_MIN = 35.0   # variance of the Laplacian (calibrated for ambient indoor webcams)

# MiniFASNet, values read from the upstream source (see README, "Models").
FAS_MODELS = [
    # (file name, crop scale around the face box)
    ("minifasnet_v2.onnx", 2.7),
    ("minifasnet_v1se.onnx", 4.0),
]
FAS_INPUT_SIZE = 80     # 80x80, BGR, float32 in 0-255 (no normalisation)
FAS_REAL_INDEX = 1      # class 1 = real face

# Heuristics: how strongly each one can pull the passive score down.
# Calibrated for indoor webcam lighting: ambient ceiling textures ~10-18, screen pixel grids >25
MOIRE_RATIO_LOW, MOIRE_RATIO_HIGH = 18.0, 35.0     # FFT peak ratio -> 0..1
GLARE_FRAC_LOW, GLARE_FRAC_HIGH = 0.02, 0.10      # saturated pixel fraction -> 0..1
MOTION_STATIC_LOW, MOTION_STATIC_HIGH = 0.15, 0.6  # mean abs frame diff (grey levels)
W_MOIRE, W_GLARE, W_STATIC = 0.5, 0.3, 0.2
HEURISTIC_PENALTY_CAP = 0.25                       # Cap heuristic penalty at 25% max
PASSIVE_WEIGHT = 0.75                              # 75% primary weight on passive anti-spoof model
HEURISTIC_WEIGHT = 0.25                            # 25% weight on heuristic consistency
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
SPOOF_TYPES = ["print", "screen", "video", "mask"]
CLIP_TYPES = ["live"] + SPOOF_TYPES + ["poor_quality"]
