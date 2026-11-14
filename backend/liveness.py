"""Liveness: MiniFASNet ensemble + heuristics, and the blink check.

Passive score: MiniFASNetV2 (crop scale 2.7) and MiniFASNetV1SE (scale 4.0)
run on 80x80 BGR crops in 0-255; their softmax outputs are averaged; class 1
is "real"; the median over frames is the passive score. Cropping follows
upstream src/generate_patches.py.

Heuristics (moire, glare, static frames) can only pull the passive score
down. If the MiniFASNet weights fail to load, the error is logged and the
module runs in heuristic-only mode, which is labelled in every response and
log record and is capped below the ALLOW threshold.
"""
import logging

import cv2
import numpy as np

from . import config
from .identity import align_face

log = logging.getLogger("spoof.liveness")


def _ramp(x, low, high):
    return float(np.clip((x - low) / (high - low), 0.0, 1.0))


def crop_for_fas(img, bbox, scale, size=config.FAS_INPUT_SIZE):
    """Upstream CropImage._get_new_box + resize. bbox is [x1, y1, x2, y2]."""
    src_h, src_w = img.shape[:2]
    x, y = bbox[0], bbox[1]
    box_w, box_h = max(bbox[2] - bbox[0], 1.0), max(bbox[3] - bbox[1], 1.0)
    scale = min((src_h - 1) / box_h, min((src_w - 1) / box_w, scale))
    new_w, new_h = box_w * scale, box_h * scale
    cx, cy = box_w / 2 + x, box_h / 2 + y
    l, t, r, b = cx - new_w / 2, cy - new_h / 2, cx + new_w / 2, cy + new_h / 2
    if l < 0:
        r -= l
        l = 0
    if t < 0:
        b -= t
        t = 0
    if r > src_w - 1:
        l -= r - src_w + 1
        r = src_w - 1
    if b > src_h - 1:
        t -= b - src_h + 1
        b = src_h - 1
    l, t, r, b = max(int(l), 0), max(int(t), 0), int(r), int(b)
    return cv2.resize(img[t:b + 1, l:r + 1], (size, size))


def face_crop(img, bbox, margin=0.1):
    h, w = img.shape[:2]
    bw, bh = bbox[2] - bbox[0], bbox[3] - bbox[1]
    x1, y1 = int(max(bbox[0] - margin * bw, 0)), int(max(bbox[1] - margin * bh, 0))
    x2, y2 = int(min(bbox[2] + margin * bw, w)), int(min(bbox[3] + margin * bh, h))
    return img[y1:y2, x1:x2]


# ---- heuristics ----------------------------------------------------------

_FFT_N = 128
_yy, _xx = np.mgrid[:_FFT_N, :_FFT_N]
_RADIUS = np.hypot(_yy - _FFT_N // 2, _xx - _FFT_N // 2).astype(np.int32)
_WINDOW = np.outer(np.hanning(_FFT_N), np.hanning(_FFT_N)).astype(np.float32)
_BAND = (_RADIUS >= _FFT_N // 8) & (_RADIUS < _FFT_N // 2)


def moire_peak_ratio(crop):
    """Strongest isolated spectral peak relative to its frequency ring.

    Screens re-photographed by a camera add periodic patterns that show up as
    sharp peaks in the mid/high band; natural skin has a smooth spectrum.
    """
    gray = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY).astype(np.float32)
    gray = cv2.resize(gray, (_FFT_N, _FFT_N), interpolation=cv2.INTER_AREA)
    mag = np.abs(np.fft.fftshift(np.fft.fft2((gray - gray.mean()) * _WINDOW)))
    mag = cv2.GaussianBlur(mag, (3, 3), 0)
    ring_sum = np.bincount(_RADIUS.ravel(), mag.ravel())
    ring_n = np.bincount(_RADIUS.ravel())
    ring_mean = (ring_sum / np.maximum(ring_n, 1))[_RADIUS]
    return float((mag / (ring_mean + 1e-6))[_BAND].max())


def glare_fraction(crop):
    """Fraction of blown-out pixels (all channels near 255)."""
    return float((crop.min(axis=2) >= 250).mean())


def motion_level(frames, bbox):
    """Mean absolute grey-level difference between consecutive face crops."""
    if len(frames) < 2:
        return None
    crops = [cv2.resize(cv2.cvtColor(face_crop(f, bbox), cv2.COLOR_BGR2GRAY), (64, 64))
             .astype(np.float32) for f in frames]
    return float(np.mean([np.abs(a - b).mean() for a, b in zip(crops, crops[1:])]))


class LivenessEngine:
    def __init__(self, models_dir=None):
        import onnxruntime as ort
        self.sessions = []
        self.error = None
        try:
            opts = ort.SessionOptions()
            opts.log_severity_level = 3
            for name, scale in config.FAS_MODELS:
                path = (models_dir or config.MODELS_DIR) / name
                sess = ort.InferenceSession(str(path), opts, providers=["CPUExecutionProvider"])
                inp = sess.get_inputs()[0]
                out = sess.run(None, {inp.name: np.zeros((1, 3, 80, 80), np.float32)})[0]
                if out.shape != (1, 3):
                    raise RuntimeError(f"{name}: output shape {out.shape}, expected (1, 3)")
                self.sessions.append((sess, inp.name, scale))
        except Exception as e:  # noqa: BLE001 - reported, then labelled fallback
            self.error = f"{type(e).__name__}: {e}"
            self.sessions = []
            log.error("MiniFASNet failed to load (%s). Running in HEURISTIC-ONLY "
                      "liveness mode.", self.error)

    @property
    def mode(self):
        return "ensemble" if self.sessions else "heuristic"

    def passive_frame(self, img, bbox):
        """Averaged softmax of both models for one frame -> P(real)."""
        probs = np.zeros(3, dtype=np.float64)
        for sess, inp, scale in self.sessions:
            crop = crop_for_fas(img, bbox, scale)
            blob = crop.astype(np.float32).transpose(2, 0, 1)[None]
            logits = sess.run(None, {inp: blob})[0][0].astype(np.float64)
            e = np.exp(logits - logits.max())
            probs += e / e.sum()
        probs /= len(self.sessions)
        return float(probs[config.FAS_REAL_INDEX])

    def score(self, frames, faces):
        """frames: BGR images; faces: largest face per frame or None.

        Returns (liveness 0..1, signals dict). No face in any frame -> 0.0.
        """
        pairs = [(f, face) for f, face in zip(frames, faces) if face is not None]
        signals = {"passive": None, "passive_per_frame": [], "moire_ratio": None,
                   "glare_fraction": None, "motion": None, "penalty": 0.0}
        if not pairs:
            return 0.0, signals

        moire = float(np.median([moire_peak_ratio(face_crop(f, fc["bbox"])) for f, fc in pairs]))
        glare = float(np.median([glare_fraction(face_crop(f, fc["bbox"])) for f, fc in pairs]))
        motion = motion_level([f for f, _ in pairs], pairs[0][1]["bbox"])
        s_moire = _ramp(moire, config.MOIRE_RATIO_LOW, config.MOIRE_RATIO_HIGH)
        s_glare = _ramp(glare, config.GLARE_FRAC_LOW, config.GLARE_FRAC_HIGH)
        # Near-identical frames are not what a live camera feed looks like.
        s_static = 0.0 if motion is None else 1.0 - _ramp(
            motion, config.MOTION_STATIC_LOW, config.MOTION_STATIC_HIGH)
        penalty = min(config.HEURISTIC_PENALTY_CAP,
                      config.W_MOIRE * s_moire + config.W_GLARE * s_glare
                      + config.W_STATIC * s_static)
        signals.update(moire_ratio=round(moire, 2), glare_fraction=round(glare, 4),
                       motion=None if motion is None else round(motion, 3),
                       penalty=round(penalty, 3))

        if self.sessions:
            per_frame = [self.passive_frame(f, fc["bbox"]) for f, fc in pairs]
            passive = float(np.median(per_frame))
            signals["passive"] = round(passive, 4)
            signals["passive_per_frame"] = [round(p, 4) for p in per_frame]
            liveness = passive * (1.0 - penalty)
        else:
            liveness = min(config.HEURISTIC_ONLY_CAP,
                           1.0 - min(1.0, penalty / config.HEURISTIC_PENALTY_CAP))
        return float(np.clip(liveness, 0.0, 1.0)), signals


# ---- blink step-up -------------------------------------------------------

# Regions in the 112x112 ArcFace-aligned face (eyes sit at about x=38/74, y=52).
_EYES = [(slice(45, 59), slice(26, 51)), (slice(45, 59), slice(61, 86))]
_BROWS = [(slice(31, 43), slice(24, 51)), (slice(31, 43), slice(61, 88))]
_NOSE = (slice(60, 80), slice(42, 70))
_REG = (slice(28, 84), slice(20, 92))  # region used to register frames


def detect_blink(frames, faces):
    """Did the eyes close and reopen during the clip?

    Every frame is aligned on its 5 face keypoints and registered to the
    median frame. A blink is a short-lived change in BOTH eye regions that is
    clearly larger than the change in the eyebrows and nose at the same
    moment (which absorbs head motion, lighting change and alignment jitter).
    A static print has no such change. NOTE: a replayed video of a blinking
    person does have it; passive liveness has to catch that case.

    Returns (blinked, signals).
    """
    aligned = []
    for f, face in zip(frames, faces):
        if face is None or face.get("kps") is None:
            continue
        g = cv2.cvtColor(align_face(f, face["kps"]), cv2.COLOR_BGR2GRAY).astype(np.float32)
        aligned.append(g * (110.0 / (g[28:100, 20:92].mean() + 1e-6)))  # exposure-normalise
    signals = {"frames_used": len(aligned), "eye_diff": None, "control_diff": None,
               "blink_frames": 0}
    if len(aligned) < config.BLINK_MIN_FRAMES:
        return False, signals

    stack = np.stack(aligned)
    ref = np.median(stack, axis=0)
    eye_mask = np.ones_like(ref, dtype=bool)
    for r in _EYES:
        eye_mask[r] = False
    reg_mask = np.zeros_like(ref, dtype=bool)
    reg_mask[_REG] = True
    reg_mask &= eye_mask

    def register(img):
        best, best_err = img, np.inf
        for dy in range(-3, 4):
            for dx in range(-3, 4):
                shifted = np.roll(img, (dy, dx), axis=(0, 1))
                err = np.abs(shifted - ref)[reg_mask].mean()
                if err < best_err:
                    best, best_err = shifted, err
        return best

    stack = np.stack([register(a) for a in stack])
    ref = np.median(stack, axis=0)
    diff = np.abs(stack - ref)
    eye = np.minimum(*[diff[(slice(None),) + r].mean(axis=(1, 2)) for r in _EYES])
    control = np.maximum.reduce([diff[(slice(None),) + r].mean(axis=(1, 2))
                                 for r in _BROWS + [_NOSE]])
    hit = (eye >= config.BLINK_EYE_DIFF_MIN) & (
        eye >= config.BLINK_EYE_TO_CONTROL * (control + 1.0))
    peak = int(np.argmax(eye - config.BLINK_EYE_TO_CONTROL * control))
    n_hit = int(hit.sum())
    # Transient: some frames closed, but most of the clip is the open-eye baseline.
    blinked = 1 <= n_hit <= len(stack) // 2
    signals.update(eye_diff=round(float(eye[peak]), 2),
                   control_diff=round(float(control[peak]), 2), blink_frames=n_hit)
    return bool(blinked), signals
