# Spot-a-Spoof

Face liveness and identity kiosk prototype for the HID hackathon. A webcam and a
browser decide **ALLOW**, **REQUEST_VERIFICATION** or **DENY** for a person at an
entry point. Everything runs locally (Phase 1).

The passive liveness model does **not** catch every spoof. Anything the system is
not sure about becomes REQUEST_VERIFICATION, never ALLOW, and the metrics page
shows the misses instead of hiding them.

## Setup

Needs Python 3.11 and Chrome or Edge. Tested on Windows 11, CPU only.

```
py -3.11 -m venv .venv
.venv\Scripts\activate
pip install -r requirements.lock
python tools/fetch_models.py --samples
```

`fetch_models.py` downloads the InsightFace `buffalo_sc` pack (16 MB) into
`models/buffalo_sc/`, checks every model file against the SHA256 values below,
and with `--samples` fetches four public photos used by the tests. Run it once
while online. `requirements.txt` holds the loose pins, `requirements.lock` the
exact versions that were tested.

## Run

```
uvicorn backend.main:app --host 127.0.0.1 --port 8000
```

Open http://127.0.0.1:8000 (use this address so the webcam works without HTTPS).

| Page | What it is for |
|---|---|
| `/` | Kiosk: face guide, Verify, result card, blink step-up |
| `/enrol` | Enrol a person (5 frames), suspend, reinstate, delete |
| `/lab` | Record labelled test clips (live and spoof) and run each one |
| `/metrics` | Harness table and chart, thresholds, kiosk log |

The pages load MediaPipe Face Detection and Chart.js from jsDelivr, so the
browser needs internet the first time. Without MediaPipe the kiosk still works:
the guide oval is disabled and the server-side quality gate decides.

## Tests and harness

```
pytest tests/                 # 34 tests
python -m backend.evaluate    # replays data/lab/ and prints the metrics table
```

The harness is also the **Run harness** button on `/metrics`
(`POST /api/evaluate`). It writes `data/metrics.json`.

## How a decision is made

1. **Quality gate.** A face in at least one frame, median brightness 60 to 200,
   median Laplacian variance above 100 (frame downscaled to 640 px wide).
2. **Liveness.** MiniFASNetV2 and MiniFASNetV1SE run on face crops, softmax
   outputs are averaged, class 1 is "real", and the median over frames is the
   passive score. Three heuristics can only lower it: moiré (FFT peak ratio),
   glare (saturated pixel fraction) and static frames (near-zero frame
   difference). `liveness = passive x (1 - penalty)`, penalty capped at 0.6.
3. **Identity.** ArcFace embedding per frame, averaged, compared to each enrolled
   user by cosine similarity, then mapped to 0-100 (see below).
4. **Policy** (`backend/decision.py`, pure, unit-tested), first match wins:

| # | Condition | Decision |
|---|---|---|
| 1 | matched user is suspended | DENY, "User suspended" |
| 2 | matched and liveness < 0.40 | DENY, "Spoof detected" |
| 3 | not matched, gate passed, liveness >= 0.85 | DENY, "Live person, identity not enrolled" |
| 4 | gate passed, matched, liveness >= 0.85, similarity >= 95 | ALLOW |
| 5 | gate failed | REQUEST_VERIFICATION, "Poor image quality" |
| 6 | anything else | REQUEST_VERIFICATION, "Low confidence" |

**Step-up.** After REQUEST_VERIFICATION the kiosk asks for a blink and sends 24
frames to `/api/stepup/blink`. The server re-checks liveness and identity on those
frames and looks for a short change in both eye regions that is larger than the
change in the eyebrows and nose. ALLOW needs: blink seen, same user matched
again, similarity >= 95, liveness >= 0.60, not suspended. Otherwise DENY. The
result is logged as a second record with the same `request_id`.

### Thresholds

These are the plan's starting values. **None has been tuned on a real webcam
yet.** When you tune them, record old and new values here.

| Threshold | Value | Where |
|---|---|---|
| Liveness to allow | >= 0.85 | `decision.py` |
| Liveness treated as spoof | < 0.40 | `decision.py` |
| Similarity to allow | >= 95 | `decision.py` |
| Liveness floor after blink step-up | >= 0.60 | `decision.py` |
| Similarity to count as enrolled | >= 80 | `config.py` |
| Brightness, sharpness | 60 to 200, > 100 | `config.py` |
| Moiré ratio, glare fraction, static motion ramps | 8 to 20, 0.02 to 0.10, 0.15 to 0.6 | `config.py` |

**Similarity scale.** ArcFace cosine similarity for the same person is usually
0.5 to 0.9, so it is mapped piecewise-linearly: cosine 0.30 -> 80, 0.50 -> 95,
1.0 -> 100. "Similarity >= 95" therefore means cosine >= 0.50. The raw cosine is
in every log record (`signals.cosine`).

## Models

| File | SHA256 | Source |
|---|---|---|
| `models/minifasnet_v2.onnx` | `7503fa5ce1494f152a15ca276b23b03b9e21f3a3e4d1f247bcc4c2c0ce4e1db8` | converted from `2.7_80x80_MiniFASNetV2.pth` |
| `models/minifasnet_v1se.onnx` | `a9230a34c7df3de8dceaa518a2da33511a6f5679c20bb63f801dca2ce43741fd` | converted from `4_0_0_80x80_MiniFASNetV1SE.pth` |
| `models/buffalo_sc/det_500m.onnx` | `5e4447f50245bbd7966bd6c0fa52938c61474a04ec7def48753668a9d8b4ea3a` | InsightFace `buffalo_sc.zip`, release v0.7 |
| `models/buffalo_sc/w600k_mbf.onnx` | `9cc6e4a75f0e2bf0b1aed94578f144d15175f357bdc05e815e5c4a02b319eb4f` | same |

MiniFASNet comes from
[Silent-Face-Anti-Spoofing](https://github.com/minivision-ai/Silent-Face-Anti-Spoofing)
(Apache 2.0) at commit `b6d5f04ad78778917853b25c778acef6d5626d15`. Upstream ships
PyTorch weights only, so they were converted with `tools/convert_minifasnet.py`.
Source weight hashes: V2 `a5eb02e1…99bec0`, V1SE `84ee1d37…2f5990`.

Checked against the upstream source code, not from memory:

- Input 80x80, BGR, float32 in 0-255 with no normalisation (`src/data_io/functional.py`).
- Crop scale 2.7 for V2 and 4.0 for V1SE (from the weight file names, `src/utility.py`).
- Class index 1 is "real" (`test.py`).
- ONNX and PyTorch outputs agree to 3e-6 on random input; both output 3 classes.

**Licence note.** The InsightFace pretrained models are for non-commercial
research use only. Fine for a hackathon prototype, not for a product. See `NOTICE`.

**Fallbacks.** If MiniFASNet fails to load, the exact error is logged and
liveness runs in heuristic-only mode, shown in the page header, in every response
(`liveness_mode: "heuristic"`) and every log record. Heuristic-only liveness is
capped at 0.80, so nobody is allowed without a step-up. If `buffalo_sc` fails to
load, detection falls back to OpenCV's Haar cascade and identity is off, so
nobody can be allowed at all.

## What has been measured so far

All numbers below come from **public still photos with synthetic camera noise**,
on this laptop's CPU, by `pytest` and one headless-Chrome run with a fake webcam.
**No real webcam capture, real printed photo or real phone screen has been tested
yet.** They show the pipeline is wired correctly; they are not accuracy figures.

| Check | Result | Sample |
|---|---|---|
| Passive score, known-real photo | 0.9998 | 1 image (upstream `image_T1`) |
| Passive score, known-fake photos | 0.2925 and 0.0012 | 2 images (upstream `image_F1`, `image_F2`) |
| Same face, altered copy (flip, darker, blur, half size) | cosine 0.93 to 0.98, similarity 99.3 to 99.8 | 4 copies of 1 image |
| Different people | max cosine 0.225 (similarity 59.9), mean 0.063 | 15 pairs from 1 group photo |
| Live enrolled face -> ALLOW, Verify press to result card | 784, 882, 886 ms (server 239 to 292 ms) | 3 runs, fake webcam replaying 1 photo |
| Spoof image of the enrolled face -> DENY "Spoof detected" | liveness 0.002, similarity 94.6 | 1 image (`image_F2`) |
| Suspended user, live -> DENY | yes | 1 image |
| Dark (brightness 33) and blurred (sharpness 6) -> REQUEST_VERIFICATION | yes | 1 image each, synthetic |
| Identical frames -> REQUEST_VERIFICATION | liveness 0.80 | 1 image repeated |
| Blink detector | detects painted-over eyes; no blink on a static clip | synthetic only |
| **Phone screen replay, video replay, mask** | **not tested** | 0 |

Two things worth knowing from these runs:

- One unenrolled fake photo (`image_F1`, liveness 0.23) got REQUEST_VERIFICATION
  rather than DENY. That is the policy as written: rule 2 only denies a low
  liveness score when the face matches an enrolled user. It can never become
  ALLOW (the step-up requires a match).
- Faces in the group photo scored 0.61 to 0.997 passive, so small or distant real
  faces can fall below 0.85 and get a step-up.

## Building the real test set

Use `/lab`: choose the type (live, print, screen, video, mask, poor_quality),
name the person and lighting, hold the subject in front of the camera, press
**Record clip**. The clip (5 JPEG frames) goes to `data/lab/<name>/` and its
expected decision to `data/expected.json`. Aim for about 20 clips per type, at
least 2 people (one not enrolled) and 2 lighting conditions. Spoof types with no
clips are listed as "not tested" on the metrics page. Do not fill them in by hand.

### Metrics

- **FAR** = spoof clips decided ALLOW / all spoof clips.
- **FRR** = live clips expected ALLOW but decided DENY / live clips expected ALLOW.
  (A live enrolled clip that gets REQUEST_VERIFICATION is not a false reject; it
  is counted in the "step-ups" column.)
- **Accuracy** = clips whose decision equals the expected decision / all clips.
- **p50 / p95** server latency per clip type.

The harness scores the first-pass decision only. Step-up outcomes are in the
kiosk log section of the metrics page.

## Known limits

- **Not evaluated on real attacks yet.** See the table above.
- **Video replay with a blink passes the blink check.** The blink step-up stops
  still images, not replays of a blinking person. Only the passive score
  (>= 0.60) stands in the way. Phase 2 (Rekognition Face Liveness) is the real fix.
- **The blink detector has only seen synthetic blinks.** Test it on your own
  webcam before relying on it in the demo.
- **Heuristic thresholds are uncalibrated.** On the sample photos the moiré ratio
  was 2.7 to 7.3 for non-screen images; the penalty starts at 8.
- **Face boxes come from SCRFD**, while MiniFASNet was trained with a different
  detector's boxes. Crops differ slightly from upstream.
- **Enrolment does not check liveness.** It is meant to be operator-supervised.
- **No authentication on any endpoint.** Bind to 127.0.0.1 only.
- Decision log and gallery are plain files in `data/` and are git-ignored
  (the gallery holds face embeddings). Kiosk frames are never written to disk.

## Changes from the build plan

- **`insightface` is not installed.** Version 0.7.3 has no Windows wheel and needs
  MSVC build tools, which this machine does not have. `backend/identity.py` runs
  the same `buffalo_sc` ONNX models directly with onnxruntime. The plan's dlib
  fallback has the same compiler problem.
- **`scikit-image` dropped** (unused). **`httpx` added** for the API tests.
- **Added files:** `backend/config.py`, `backend/pipeline.py` (shared by API and
  harness), `static/camera.js`, `tools/`, `tests/test_store.py`,
  `tests/test_pipeline.py`.
- **Added endpoints:** `GET /api/status`, `PATCH` and `DELETE /api/users/{id}`,
  `POST /api/lab/clips`, `DELETE /api/lab/clips/{name}`,
  `POST /api/lab/clips/{name}/run`.
- `/api/stepup/blink` accepts an optional `request_id` so the result can replace
  the first decision.
- `data/gallery.json` is git-ignored rather than committed.

## Open items

1. **Rekognition CLI checks: not run.** The AWS CLI is not installed here and no
   sandbox credentials were available. Phase 2 has not started;
   `RekognitionIdentity` is a stub.
2. Record the real test set, run the harness, and paste the table here.
3. Tune thresholds on that data and record old and new values.
4. Run the acceptance checks with a real printed photo and a phone replay.
5. Rehearse `DEMO.md`.
