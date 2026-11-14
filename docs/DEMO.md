# Demo script (3 minutes)

## Before you start

- Server running: `uvicorn backend.main:app --host 127.0.0.1 --port 8000`.
- Header badge reads "liveness: ensemble". If it reads "heuristic", say so.
- Enrolled on `/enrol`: the presenter (active) and one teammate marked **suspended**.
- On the table: a printed photo of the presenter, and a phone showing a photo or
  video of the presenter.
- Harness already run on `/metrics`, so the table is filled.
- Do one warm-up Verify so the first on-stage result is fast.

## Script

| Time | Do | Say |
|---|---|---|
| 0:00 | Open `/`. | "A kiosk has two questions: is this a real person, and is it someone we know? We answer ALLOW, DENY, or 'not sure, check again'. It never guesses ALLOW." |
| 0:20 | Presenter faces the camera, oval turns green, press **Verify**. Expect ALLOW. | "Live and enrolled: ALLOW. The card shows the liveness score, the match score and the time taken." Read the "press to result" number aloud. |
| 0:50 | Hold up the **printed photo**. Verify. Expect DENY "Spoof detected". | "Same face, on paper. Identity matches, liveness does not: DENY." |
| 1:15 | Hold up the **phone**. Verify. Expect DENY or VERIFICATION NEEDED. | "Screens are harder. If the model is unsure, we do not allow. We ask for a second check." If the step-up panel appears, run it and show the outcome. |
| 1:45 | Suspended teammate steps in. Verify. Expect DENY "User suspended". | "A real, enrolled person whose access is suspended is still denied. Policy beats biometrics." |
| 2:05 | Cover the lens partly or defocus (or dim the light). Verify. Expect VERIFICATION NEEDED "Poor image quality". | "Bad image: we do not decide on bad evidence." |
| 2:25 | Open `/metrics`. | "We measured it. This is N clips, from P people, in L lighting conditions." Read FAR, FRR and p95 with their counts. Point at the "clips that did not match" table: "These are the ones we got wrong, and what the system did with them." |
| 2:50 | Stay on metrics. | "Not tested: [read the 'Not tested' banner]. Known gap: a video replay of someone blinking can pass the blink check. The next step is Rekognition Face Liveness for the step-up." |

## If something goes wrong

- **Phone replay returns ALLOW.** Do not hide it. "This is a miss, and it is what
  our FAR number counts." Show the same clip in the metrics miss table.
- **Live face gets VERIFICATION NEEDED.** Run the blink step-up; that is what it
  is for. Usual cause: face too far from the camera or uneven light.
- **Camera does not start.** Check the address is `http://127.0.0.1:8000` and
  camera permission is allowed.
- **Oval never turns green.** MediaPipe did not load (no internet). Verify still
  works; the server gate decides.
