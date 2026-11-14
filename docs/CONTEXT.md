# Spot-a-Spoof: full context

Handoff document for the HID hackathon build. It covers the goal, what was built, every decision and deviation from the plan, what has been measured, what went wrong, and what is still open. Written so someone new can pick the project up without the original conversation.

Last updated: 2026-11-14.

---

## 1. Goal

A local prototype of an entry kiosk. A webcam and browser decide **ALLOW**, **REQUEST_VERIFICATION** or **DENY** for a person at a door. The decision appears within 2 seconds. It must reject printed photos, screen replays, video replays and masks, and it must measure its own accuracy, false acceptance rate (FAR), false rejection rate (FRR) and latency.

Hard rules from the brief:
- Never claim the passive model catches all spoofs. Show failure cases and how the decision engine handles them.
- Every uncertain case becomes REQUEST_VERIFICATION, never ALLOW.
- Every number is reported with its sample size and conditions (people, lighting, spoof types).

Phase 1 is fully local. Phase 2 (AWS Rekognition) is optional and only starts after Phase 1 passes its acceptance tests.

---

## 2. Use case

An entry kiosk for a secured door or turnstile. The person looks at a camera. The system answers two questions together:
1. Is a real person standing here (liveness), not a photo, phone screen, video replay or mask?
2. Is it someone we know, and are they still allowed in (identity and standing)?

Outcomes:
- **ALLOW**: live, recognised, in good standing.
- **DENY**: spoof, unknown person, or suspended user.
- **REQUEST_VERIFICATION**: unsure. A blink step-up is offered instead of guessing.

Typical settings: office access, visitor check-in, exam or attendance check-in.

Pages:

| Page | User | Purpose |
|---|---|---|
| `/` Kiosk | person at the door | the product |
| `/enrol` | admin | register people, suspend, reinstate, delete |
| `/lab` | project team | record labelled clips (live and spoof) and run each one |
| `/metrics` | judges, team | accuracy, FAR, FRR, latency, thresholds, kiosk log |

---

## 3. The source build plan (summary)

The brief was a pasted plan titled "Build Plan: Spot-a-Spoof". Key requirements:

- **Flow:** browser runs MediaPipe Face Detection for a face gate and guide oval; on Verify captures 3 to 5 JPEG frames over about 0.5 s and POSTs base64 to `/api/verify`.
- **Backend order:** decode, quality gate (at least one face, brightness 60 to 200, Laplacian variance above 100), liveness (MiniFASNetV2 and V1SE averaged softmax, median over frames, plus moiré, glare and motion heuristics), identity (InsightFace `buffalo_sc`, ArcFace, cosine mapped to 0 to 100), standing lookup in `data/gallery.json`, `decide()`, append to `data/decisions.jsonl`, return `VerifyResponse` with `latency_ms`.
- **Step-up:** blink check for REQUEST_VERIFICATION only; result replaces the first decision and is logged with a shared `request_id`.
- **Decision policy** (`backend/decision.py`, pure function, order matters):
  1. suspended -> DENY "User suspended"
  2. matched and liveness < 0.4 -> DENY "Spoof detected"
  3. not matched, gate passed, liveness >= 0.85 -> DENY "Live person, identity not enrolled"
  4. gate passed, matched, liveness >= 0.85, similarity >= 95 -> ALLOW
  5. gate failed -> REQUEST_VERIFICATION "Poor image quality"
  6. otherwise -> REQUEST_VERIFICATION "Low confidence"
- **Required unit tests:** six named cases (suspended live; matched liveness 0.2; unmatched live; matched 0.9 / 97 / gate passed; gate failed; matched liveness 0.6).
- **Stack:** Python 3.11, FastAPI, uvicorn, OpenCV headless, numpy, onnxruntime, insightface, pytest, boto3 (Phase 2 only). Vanilla JS frontend, MediaPipe and Chart.js from jsDelivr with pinned versions.
- **Build steps 1 to 13** with acceptance checks (environment, decision engine, store, liveness, identity, verify endpoint, kiosk page, enrol and lab pages, metrics and harness, test data, acceptance, README and demo, optional AWS).
- **Time plan:** 8 hours, freeze features at hour 6.
- **Deliverables:** working app, passing pytest, harness table with sample sizes, README with hashes and thresholds and limits, demo script, Phase 2 results only if they exist, an Eraser architecture diagram (a separate "Untitled" file, not yet reviewed in this session).

---

## 4. What was built

Repository: https://github.com/Shivakumarsullagaddi/HID-Hackathon (branch `main`). Local path: `d:\HID-Hackathon`.

### 4.1 Commits on `main`

| Commit | Message |
|---|---|
| `02ceb31` | Initial commit: face verification backend |
| `f51f632` | Add frontend, MiniFASNet models, and steadier blink detection |
| `7214496` | Add README and demo walkthrough |

All three are authored and committed as `U Shivakumar <146023010+Shivakumarsullagaddi@users.noreply.github.com>`. Local repo config is pinned to that account (see section 7.2).

### 4.2 Layout

```
HID-Hackathon/
  backend/
    __init__.py
    config.py        paths, MODE, gate, MiniFASNet settings, heuristic ramps, identity anchors, blink params
    decision.py      decide() and decide_stepup(), pure, thresholds
    identity.py      FaceEngine (SCRFD det_500m + ArcFace w600k_mbf via onnxruntime), match, scale_similarity, RekognitionIdentity stub
    liveness.py      LivenessEngine (MiniFASNet ensemble), heuristics, detect_blink
    models.py        Pydantic request/response models (strict, frame count 3 to 5)
    pipeline.py      Pipeline: verify(), stepup_blink(), enrol(), quality_gate(), decode_frames()
    store.py         Store: gallery.json, decisions.jsonl, lab clips, expected.json, metrics.json
    evaluate.py      harness: run_harness(), summarise(), format_table()
    main.py          FastAPI app, pages, API routes
  frontend/
    index.html, enrol.html, lab.html, metrics.html
  static/
    style.css, camera.js (shared), app.js, enrol.js, lab.js, metrics.js
  tools/
    convert_minifasnet.py   PyTorch .pth to ONNX (upstream commit b6d5f04)
    fetch_models.py         downloads buffalo_sc, verifies SHA256, optional --samples
  tests/
    test_decision.py, test_store.py, test_pipeline.py
    fixtures/  (git-ignored, downloaded)
  models/
    minifasnet_v2.onnx, minifasnet_v1se.onnx   (committed)
    buffalo_sc/det_500m.onnx, w600k_mbf.onnx    (git-ignored, downloaded)
  data/
    gallery.json (git-ignored), decisions.jsonl (git-ignored), metrics.json (git-ignored)
    expected.json (tracked, currently modified: user's recorded clips)
    lab/ (clip folders, git-ignored)
  docs/            this folder
  requirements.txt, requirements.lock, README.md, DEMO.md, NOTICE, .gitignore
```

### 4.3 Pages

- **Kiosk (`/`)**: webcam, MediaPipe face gate (green oval when one centred face is at a usable size), Verify button, result card (verdict, reason, identity, liveness, similarity, image quality, blink, server time, click-to-result time, signal line with request id). On REQUEST_VERIFICATION a blink panel appears: 3-2-1, then 24 frames over about 2.2 s, then the result replaces the first decision.
- **Enrol (`/enrol`)**: name field, capture 5 frames over 2 s, enrol. Table of users with Suspend or Reinstate and Delete.
- **Spoof lab (`/lab`)**: record a 5-frame clip with type (live, print, screen, video, mask, poor_quality), person, enrolled yes or no, lighting, expected decision (auto if blank). Table of clips with Run and Delete; each run shows expected versus actual.
- **Metrics (`/metrics`)**: Run harness button, stat tiles (accuracy, FAR, FRR, p50, p95), by-type table, stacked bar chart (Chart.js), misses table, thresholds, kiosk log summary, and a "not tested" banner for spoof types with no clips.

### 4.4 API

| Method | Path | Notes |
|---|---|---|
| GET | `/`, `/enrol`, `/lab`, `/metrics` | pages |
| GET | `/api/status` | mode, liveness mode and error, identity availability and error |
| POST | `/api/verify` | 3 to 5 frames; optional `user_id`, `clip_label` |
| POST | `/api/stepup/blink` | 8 to 30 frames; optional `request_id` |
| POST | `/api/enrol` | name and exactly 5 frames |
| GET | `/api/users` | no embeddings |
| PATCH | `/api/users/{id}` | `{standing}` |
| DELETE | `/api/users/{id}` | |
| GET | `/api/lab/clips` | |
| POST | `/api/lab/clips` | records a clip |
| DELETE | `/api/lab/clips/{name}` | |
| POST | `/api/lab/clips/{name}/run` | one clip through the pipeline |
| GET | `/api/metrics` | kiosk log summary plus last harness result |
| POST | `/api/evaluate` | runs the harness on all clips |

Extra endpoints beyond the plan: status, PATCH and DELETE for users, and the clip create, delete and run routes.

### 4.5 Harness

- `python -m backend.evaluate` or the Run harness button.
- FAR = spoof clips decided ALLOW / spoof clips.
- FRR = live clips expected ALLOW but decided DENY / live clips expected ALLOW. A live enrolled clip that gets REQUEST_VERIFICATION counts as a step-up, not a false reject.
- Accuracy = decision equals expected decision.
- p50 and p95 latency per clip type. The first clip is a warm-up and is excluded from latency.
- Writes `data/metrics.json`. Records conditions (people, lighting, clip types, missing spoof types).

### 4.6 Tests

`pytest tests/` : **34 passed** at last full run.
- `test_decision.py`: the six required cases, plus extra cases including an exhaustive grid check that ALLOW only happens when every signal agrees, and step-up policy cases.
- `test_store.py`: decision log round trip, gallery operations, clip round trip, frame-count validation (3 to 5, 422), strict fields, enrol needs exactly 5 frames.
- `test_pipeline.py`: integration on public sample photos (skipped if models or samples are missing). Covers passive separation, live ALLOW under 2 s, spoof denied, others not allowed, dark, blurry and faceless gate outcomes, identical frames not allowed, suspended denied, logging, blink detection on synthetic blink clips, step-up replacing the first decision with a shared request_id and being single use, and an API end-to-end test.

---

## 5. Models

| File | SHA256 | Source |
|---|---|---|
| `models/minifasnet_v2.onnx` | `7503fa5ce1494f152a15ca276b23b03b9e21f3a3e4d1f247bcc4c2c0ce4e1db8` | converted from `2.7_80x80_MiniFASNetV2.pth` (source sha `a5eb02e1…99bec0`) |
| `models/minifasnet_v1se.onnx` | `a9230a34c7df3de8dceaa518a2da33511a6f5679c20bb63f801dca2ce43741fd` | converted from `4_0_0_80x80_MiniFASNetV1SE.pth` (source sha `84ee1d37…2f5990`) |
| `models/buffalo_sc/det_500m.onnx` | `5e4447f50245bbd7966bd6c0fa52938c61474a04ec7def48753668a9d8b4ea3a` | InsightFace buffalo_sc.zip, release v0.7 |
| `models/buffalo_sc/w600k_mbf.onnx` | `9cc6e4a75f0e2bf0b1aed94578f144d15175f357bdc05e815e5c4a02b319eb4f` | same |

Verified against upstream source, not memory:
- MiniFASNet input 80x80, BGR, float32 0 to 255, no normalisation.
- Crop scale 2.7 for V2 and 4.0 for V1SE (from file names and `src/utility.py`).
- Class index 1 is real (`test.py`).
- ONNX versus PyTorch outputs agree to about 3e-6 on random input; 3 classes out.
- buffalo_sc: detector takes `input.1` 1x3xHxW with 9 outputs; recogniser takes 1x3x112x112 and gives 512-dim.

Licence: MiniFASNet is Apache 2.0 (upstream). InsightFace pretrained models are **non-commercial research only**. Fine for a prototype; a product needs a different face model or an InsightFace licence. Recorded in `NOTICE`.

Fallbacks: if MiniFASNet fails to load, liveness runs in heuristic-only mode, capped at 0.80 so nobody is allowed without a step-up, labelled in the header, every response and every log record. If buffalo_sc fails to load, detection falls back to OpenCV Haar and identity is off, so nobody can be allowed. Both were tested by pointing `SPOOF_MODELS_DIR` at an empty folder.

---

## 6. Decisions and deviations from the plan

1. **`insightface` not installed.** Version 0.7.3 has no Windows wheel and needs MSVC build tools, which this machine lacks (`vswhere` not found). `backend/identity.py` reimplements the SCRFD decode, NMS, ArcFace alignment (Umeyama) and embedding with onnxruntime. dlib (the plan's fallback) needs the same compiler, so it was not used.
2. **`requirements.txt` changed.** `insightface==0.7.3` removed (with a comment explaining why). `httpx==0.27.*` added for FastAPI's TestClient. `scikit-image` dropped as unused. `boto3` kept (Phase 2 only). `requirements.lock` generated with `pip freeze` (44 lines). Versions in use: opencv-python-headless 4.10.0, onnxruntime 1.18.1, fastapi 0.115.14, numpy 1.26.4, Python 3.11.9.
3. **MiniFASNet weights converted to ONNX and committed**, rather than kept as PyTorch. Conversion needed `torch` (CPU, 2.3.1) and `onnx` 1.16.1 in a throwaway venv in the scratchpad. onnx's wheel contains test data with paths too long for Windows, so it was extracted with those paths skipped. Conversion script committed as `tools/convert_minifasnet.py`.
4. **Blink detector changed during testing.** First version aligned each frame on its own keypoints. Eye keypoints move when the eyes close, so the whole face smeared on exactly the frames that mattered and no blink was detected. Fixed by aligning all frames with one shared transform from the median keypoints, and widening the registration search from ±3 to ±6 px.
5. **Lab test changed.** The API test originally tried to record a "stranger" live clip expected DENY. The policy correctly returns REQUEST_VERIFICATION for an unenrolled face at low-to-mid liveness, so the test was changed to a `poor_quality` clip (dark frame expected REQUEST_VERIFICATION). Policy was not changed.
6. **Gallery and runtime data are git-ignored** (`data/gallery.json`, `data/decisions.jsonl`, `data/metrics.json`, `data/lab/*`) because the gallery holds face embeddings. `data/expected.json` is tracked.
7. **Frontend choices:** no framework, no build step, as specified. CDN scripts pinned: `@mediapipe/face_detection@0.4.1646425229` and `chart.js@4.4.3`. All three CDN URLs were checked with HTTP 200.
8. **Similarity mapping:** ArcFace cosine is mapped piecewise linearly, cosine 0.30 to 80, 0.50 to 95, 1.0 to 100. So "similarity >= 95" means cosine >= 0.50. Raw cosine is logged. This mapping is a design choice, not from the plan.
9. **Step-up thresholds added:** blink requires both eye regions to change by at least 6 grey levels and at least 2x the brow and nose change. Step-up ALLOW needs blink, same user matched again, similarity >= 95, liveness >= 0.60, not suspended. These are my values, not the brief's, and are untuned.
10. **Heuristic formula:** `liveness = passive x (1 - penalty)`, where penalty = 0.5 moiré + 0.3 glare + 0.2 static, capped at 0.6. Ramps: moiré 8 to 20, glare 0.02 to 0.10, motion 0.15 to 0.6 grey levels. These are my values, uncalibrated.

---

## 7. Verification performed

### 7.1 Unit and integration

- `pytest tests/`: 34 passed (last full run after the blink fix).
- JS syntax checked with `node --check` on all `static/*.js`.

### 7.2 Browser run (headless Chrome, fake webcam)

A synthetic clip built from upstream `image_T1.jpg` (480x640 region, shifts and noise) was played as the fake camera. Results:
- Enrol: enrolled as `u_001`. Gate ready after about 5.1 s.
- Kiosk: three Verify presses, all ALLOW. Press-to-card 784, 882, 886 ms. Server 239 to 292 ms.
- Lab: clip saved, run, "as expected".
- Metrics: harness ran, chart drawn. Console showed only the favicon 404 and MediaPipe WebGL info lines.

Screenshots were taken and checked (kiosk result card and metrics page rendered correctly).

### 7.3 Missing-model fallback

Pointing `SPOOF_MODELS_DIR` at an empty folder: both models logged their errors, status showed `liveness_mode: heuristic` and identity off, and a live photo returned REQUEST_VERIFICATION "Low confidence" with liveness 0.80. Tests still passed (34).

### 7.4 Sample-photo measurements

These are the numbers in the README. All use public still images with synthetic noise, not live capture.

| Check | Result | Sample |
|---|---|---|
| Passive, known-real photo | 0.9998 | 1 image |
| Passive, known-fake photos | 0.2925 and 0.0012 | 2 images |
| Same face, altered copies | cosine 0.93 to 0.98, similarity 99.3 to 99.8 | 4 copies of 1 image |
| Different people | max cosine 0.225 (similarity 59.9), mean 0.063 | 15 pairs from 1 group photo |
| Live enrolled face to ALLOW | press-to-card 784 to 886 ms | 3 runs, fake webcam |
| Spoof of enrolled face | DENY "Spoof detected", liveness 0.002, similarity 94.6 | 1 image |
| Suspended user, live | DENY | 1 image |
| Dark (brightness 33) and blurred (sharpness 6) | REQUEST_VERIFICATION | 1 each, synthetic |
| Identical frames | REQUEST_VERIFICATION, liveness 0.80 | 1 image repeated |
| Unenrolled fake photo (liveness 0.23) | REQUEST_VERIFICATION (not DENY) | 1 image |
| Phone screen, video replay, mask | **not tested** | 0 |

Observation: rule 2 of the policy denies low liveness only when the face matches an enrolled user. An unenrolled spoof gets REQUEST_VERIFICATION. It cannot become ALLOW, so this is safe, but it is worth noting for the demo.

---

## 8. Real-world run by the user (this session)

The user ran the app on their own machine with a real webcam.

### 8.1 What the screenshots showed

1. **Metrics page:** 6 clips, people "shiva", lighting "daylight", liveness mode ensemble, run at 11:28:32. All six live clips expected ALLOW got REQUEST_VERIFICATION. Accuracy 0 of 6. FRR 0 of 6 (none decided DENY). Step-ups 6. p50 254 ms, p95 359 ms.
2. **Spoof lab page:** showed "Starting camera…" with a black video area. Most likely the camera was held by another tab or app. Not confirmed.
3. **Enrol page:** the face was detected but the oval was dashed (not green) because the face was off-centre. "Nobody is enrolled yet." The Capture button was disabled.
4. **Kiosk page:** oval green, "Ready", Verify enabled, with the user in a cap. Result card still at the default text, so no Verify had run yet at that point.

### 8.2 Why the six live clips were REQUEST_VERIFICATION

Read from `data/metrics.json` and `data/decisions.jsonl`:

| Clip | Liveness | Similarity | Match | Gate | Sharpness |
|---|---|---|---|---|---|
| daylight_01 | 0.71 | 0.0 | none | blurry | about 95 |
| daylight_02 | 0.77 | 0.0 | none | blurry | about 90 |
| daylight_03 | 0.81 | 0.0 | none | blurry | about 95 |
| daylight_04 | 0.71 | 0.0 | none | blurry | about 95 |
| daylight_05 | 0.74 | 0.0 | none | blurry | about 89 |
| daylight_06 | 0.81 | 0.0 | none | blurry | about 94 |

Three causes:
1. **Nobody was enrolled.** No `data/gallery.json` existed at the time, so similarity is 0 and nobody matches. ALLOW is impossible without enrolment.
2. **Sharpness gate just under the threshold.** The gate uses the median Laplacian variance of the frame at 640 px wide. Values 89 to 95 are below the 100 threshold, so "Poor image quality" applies. The webcam is softer than the sample photos used for testing.
3. **Liveness just under 0.85.** Values 0.71 to 0.81. A guess is that the cap or lighting is the cause; not tested.

No false reject or false accept happened: the system was unsure and asked for a second check, as designed. The 0% accuracy is a consequence of the setup, not of the decision logic.

Recommended fixes, in order:
1. Enrol on `/enrol` until the oval is solid green and the Capture button enables.
2. Remove the cap and improve lighting facing the camera. Re-record clips.
3. If sharpness still sits just under 100, lower `SHARPNESS_MIN` in `backend/config.py` (suggested about 60). First record a deliberately blurry clip to confirm it still fails at that value.

### 8.3 Lab clip data on disk

`data/lab/` contains clips for two people:
- `live_shiva_daylight_01` to `_06` (shown on the metrics page)
- `live_shivu-k-c_daylight_01` to `_06` (second person, not in the screenshot)

`data/expected.json` is modified in the working tree because the user's clips were added through the lab. This was deliberate and is left as is.

---

## 9. Server and environment issues

- **Port 8000 conflict.** An assistant-started `uvicorn` failed with `[Errno 10048]` because the user already had a server on port 8000 (started from their own terminal about 40 seconds earlier). The page checks were answered by the user's server. The user's server was left running and not touched.
- **Python.** Created the venv with `py -3.11` (Python 3.11.9). The base Python 3.13 on the machine was not used for the project.
- **Chrome headless** was used for the browser run (`--remote-debugging-port`, fake media stream flags).
- **Long paths.** onnx's test data on Windows exceeds path limits; extraction skipped it.
- **No ffmpeg** on the machine; not needed.
- **No `gh` account switch.** See 10.2.

---

## 10. GitHub and account handling

### 10.1 Push problem

First push was refused with HTTP 403 ("Permission to Shivakumarsullagaddi/HID-Hackathon.git denied to shivakumaru-darukaa"). The machine's active `gh` account is `shivakumaru-darukaa`, which is the user's work account.

### 10.2 Fix

The user asked to use `Shivakumarsullagaddi`. Applied as repo-local config only:
- `credential.helper` pulls `gh auth token --user Shivakumarsullagaddi`.
- `user.name` set to `U Shivakumar`, `user.email` to the account's noreply address.
- The global `gh` login was not changed.

After this, `git push` succeeded and all commits show `Shivakumarsullagaddi` as author and committer.

A memory was saved: `hid-hackathon-github-account.md`, indexed in `MEMORY.md`. It notes the account preference and that this may also apply to the user's other personal repos (not yet confirmed).

### 10.3 Commit attribution

Commit messages should end with the `Co-Authored-By` line from the system reminder. The most recent reminder specified "Claude Haiku 5.5". Earlier commits used "Claude Fable 5.2". Check the reminder in force at the time of each commit.

---

## 11. Not done / open items

1. **Rekognition CLI checks not run.** The AWS CLI is not installed. No sandbox credentials. Phase 2 is a stub (`RekognitionIdentity` raises NotImplementedError). Phase 2 is only meant to start after Phase 1 passes acceptance.
2. **Real test set not recorded.** Needed: about 20 clips per type (live, print, screen, video, mask), at least 2 people (one not enrolled), at least 2 lighting conditions. Currently only daylight live clips for two people.
3. **Spoof types not tested:** print, screen, video, mask. The README states this.
4. **Thresholds not tuned** on the user's webcam. Current values are the plan's starting values, plus my values for step-up, heuristics, and similarity mapping.
5. **Acceptance checks (step 11)** not run with real inputs. Need: live enrolled ALLOW under 2000 ms; printed photo not ALLOW; phone replay not ALLOW; suspended DENY; blurry or dark REQUEST_VERIFICATION; card within 2 s. Only the sample-photo versions have been run.
6. **Blink detector** has only been tested on synthetic blinks.
7. **Video replay with a blink** can pass the step-up. Only the passive score (>= 0.60) stands between it and ALLOW. Phase 2 Rekognition Face Liveness is the planned fix.
8. **Eraser architecture diagram** from the plan: not created or reviewed in this session.
9. **README "Open items"** lists the same tasks.
10. **Demo rehearsal** not done. `DEMO.md` written but not rehearsed.
11. **Heuristic thresholds** uncalibrated. Moiré on sample photos was 2.7 to 7.3 for non-screen images; the penalty starts at 8.
12. **Crop mismatch:** SCRFD boxes differ from the detector MiniFASNet was trained with. Not measured.
13. **No authentication** on any endpoint. Bind to 127.0.0.1 only.
14. **Enrolment does not check liveness.** Intended for operator-supervised use.
15. **Kiosk frames are never written to disk.** Lab clips are written, by design.
16. **`data/expected.json` modified** in the working tree and not committed.
17. **`docs/` contents** described in section 12. Not committed.

---

## 12. Documents in this folder

| File | What it is |
|---|---|
| `CONTEXT.md` | this file |
| `DEMO.md` | three-minute demo script with fallbacks (copy of the root file) |
| `NOTICE` | third-party model licences and attribution (copy of the root file) |
| `README-project.md` | the project README (copy of the root README, which stays in place) |

The root `README.md` and `DEMO.md` remain where they were. The copies here are for sharing.

---

## 13. How to run

```
cd d:\HID-Hackathon
py -3.11 -m venv .venv
.venv\Scripts\activate
pip install -r requirements.lock
python tools/fetch_models.py --samples
uvicorn backend.main:app --host 127.0.0.1 --port 8000
```

Open http://127.0.0.1:8000. Start on `/enrol`. Then record clips on `/lab` and run the harness on `/metrics`.

Tests: `pytest tests/`. Harness from the command line: `python -m backend.evaluate`.

---

## 14. Glossary

- **Passive liveness:** a single-image (or short burst) decision that the face is live, without user action.
- **Step-up:** a second, active check (blink) requested when the first decision is uncertain.
- **FAR:** false acceptance rate, spoofs allowed / spoofs presented.
- **FRR:** false rejection rate, genuine people denied / genuine people presented.
- **Gate:** the quality check (face present, brightness, sharpness) that must pass before a decision.
- **Embedding:** the 512-number face vector from ArcFace. Only embeddings are stored, never photos.
- **Standing:** whether an enrolled user is `active` or `suspended`.
