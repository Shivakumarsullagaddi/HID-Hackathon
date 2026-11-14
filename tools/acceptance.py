"""Acceptance run (build plan section 12) against two REAL processes:
central on :9100 and kiosk on :8100, every database in a temp work folder
(your data/ folder and any server on :8000/:9000 are not touched).

Simulated parts, stated plainly: the face is a public sample photo with
synthetic camera noise (tests/fixtures, from tools/fetch_models.py --samples),
the blink is synthetic (eyes painted over), and check 5 uses the kiosk's
simulated-outage switch so central can take the suspension while the kiosk
cannot see it. Check 4 stops the central process for real.

    python tools/acceptance.py <work folder>     # about 4 minutes
"""
import base64
import json
import os
import signal
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
WORK = Path(sys.argv[1])
sys.path[:0] = [str(REPO), str(REPO / "tests")]
os.chdir(REPO)

import cv2  # noqa: E402
import httpx  # noqa: E402

from test_pipeline import blink_clip, burst  # noqa: E402

PY = str(REPO / ".venv" / "Scripts" / "python.exe")
KEY = "acceptance-hmac-key"
C_URL, K_URL = "http://127.0.0.1:9100", "http://127.0.0.1:8100"
WORK.mkdir(parents=True, exist_ok=True)
CENTRAL_DB = f"sqlite:///{(WORK / 'central.db').as_posix()}"
results = {}
log = open(WORK / "acceptance.log", "w", encoding="utf-8")


def note(key, **kw):
    results[key] = kw
    print(f"[{key}] " + json.dumps(kw, default=str))
    log.write(f"[{key}] {json.dumps(kw, default=str)}\n")
    log.flush()


def b64(frames):
    return [base64.b64encode(cv2.imencode(".jpg", f, [cv2.IMWRITE_JPEG_QUALITY, 92])[1]).decode()
            for f in frames]


def start(name, args, env):
    out = open(WORK / f"{name}.log", "a", encoding="utf-8")
    return subprocess.Popen(args, env={**os.environ, **env}, stdout=out, stderr=subprocess.STDOUT,
                            creationflags=subprocess.CREATE_NEW_PROCESS_GROUP)


def stop(proc):
    proc.send_signal(signal.CTRL_BREAK_EVENT)
    try:
        proc.wait(10)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(10)


def wait_http(url, timeout=90):
    t = time.time()
    while time.time() - t < timeout:
        try:
            if httpx.get(url, timeout=2).status_code == 200:
                return
        except httpx.HTTPError:
            pass
        time.sleep(0.5)
    raise RuntimeError(f"{url} did not come up")


def kstate():
    return httpx.get(f"{K_URL}/api/state", timeout=5).json()


def wait_state(want, timeout):
    t = time.time()
    while time.time() - t < timeout:
        st = kstate()
        if st["state"] == want:
            return round(time.time() - t, 1), st
        time.sleep(0.5)
    raise RuntimeError(f"kiosk never reached {want}: {kstate()}")


def verify(frames, room=None):
    body = {"frames": b64(frames)}
    if room:
        body["room_id"] = room
    t = time.perf_counter()
    r = httpx.post(f"{K_URL}/api/verify", json=body, timeout=30).json()
    r["_round_trip_ms"] = round((time.perf_counter() - t) * 1000, 1)
    return r


def stepup(frames, request_id, pin=None):
    body = {"frames": b64(frames), "request_id": request_id}
    if pin:
        body["pin"] = pin
    return httpx.post(f"{K_URL}/api/stepup/blink", json=body, timeout=60).json()


def brief(r):
    return {k: r.get(k) for k in ("final_decision", "final_reason", "bio_decision", "reason",
                                  "schedule_status", "kiosk_state", "snapshot_version",
                                  "latency_ms", "_round_trip_ms", "pin_checked", "blinked")}


# ---- setup -------------------------------------------------------------
c_env = {"CENTRAL_DB_URL": CENTRAL_DB, "SNAPSHOT_HMAC_KEY": KEY}
subprocess.run([PY, "-m", "alembic", "-c", "alembic_central.ini", "upgrade", "head"],
               env={**os.environ, **c_env}, check=True, capture_output=True)
empty = WORK / "empty_gallery.json"
empty.write_text('{"users": []}')
seed = subprocess.run([PY, "tools/seed_central.py", "--db-url", CENTRAL_DB, "--gallery", str(empty),
                       "--write-env", str(WORK / "kiosk.env")],
                      env={**os.environ, **c_env}, check=True, capture_output=True, text=True)
note("seed", output=[ln for ln in seed.stdout.splitlines() if "API key" not in ln])
api_key = [ln.split("=", 1)[1] for ln in (WORK / "kiosk.env").read_text().splitlines()
           if ln.startswith("KIOSK_API_KEY=")][0]
k_env = {"SPOOF_DATA_DIR": str(WORK), "KIOSK_DB_PATH": str(WORK / "kiosk.db"),
         "CENTRAL_URL": C_URL, "SNAPSHOT_HMAC_KEY": KEY, "SNAPSHOT_TTL_OVERRIDE_S": "120"}
central = start("central", [PY, "-m", "uvicorn", "central.main:app", "--host", "127.0.0.1",
                            "--port", "9100"], c_env)
wait_http(f"{C_URL}/health")
kiosk = start("kiosk", [PY, "-m", "uvicorn", "backend.main:app", "--host", "127.0.0.1",
                        "--port", "8100"], k_env)
wait_http(f"{K_URL}/api/state")
secs, st = wait_state("ONLINE", 30)
note("startup", reached_online_s=secs, snapshot_version=st["snapshot_version"],
     ttl_s=st["ttl_s"], reason=st["reason"])

img = cv2.imread(str(REPO / "tests" / "fixtures" / "image_T1.jpg"))


class P:  # blink_clip only needs .faces.detect
    from backend.identity import FaceEngine
    faces = FaceEngine()


BLINK = blink_clip(P, img, closed=(8, 9))
NO_BLINK = blink_clip(P, img)

r = httpx.post(f"{K_URL}/api/enrol", json={"name": "Sample person", "pin": "4321",
                                           "frames": b64(burst(img, seed=1))}, timeout=60)
uid = r.json()["user_id"]
note("enrol", status=r.status_code, body=r.json())
verify(burst(img, seed=2))  # warm-up

# ---- 1. ONLINE allow ----------------------------------------------------------
runs = [verify(burst(img, seed=10 + i)) for i in range(5)]
note("1_online_allow", runs=[brief(x) for x in runs],
     server_ms=[x["latency_ms"] for x in runs], round_trip_ms=[x["_round_trip_ms"] for x in runs])

# ---- 2. room with no rule -----------------------------------------------------
note("2_no_rule_room", **brief(verify(burst(img, seed=20), room="hostel_gate")))

# ---- 3. outside window, then exam window -------------------------------------
users = httpx.get(f"{C_URL}/users").json()
rule = [u for u in users if u["id"] == uid][0]["rules"][0]
httpx.delete(f"{C_URL}/rules/{rule['id']}")
# a 1-minute window at 03:00 local: certainly not now during the run
httpx.post(f"{C_URL}/rules", json={"user_id": uid, "room_id": "lab_a", "days_mask": 127,
                                   "start_min": 180, "end_min": 181})
httpx.post(f"{K_URL}/api/sync", timeout=30)
outside = verify(burst(img, seed=30))
ex = httpx.post(f"{C_URL}/exam_windows", json={"room_id": "lab_a", "minutes": 30}).json()
httpx.post(f"{K_URL}/api/sync", timeout=30)
exam = verify(burst(img, seed=31))
exam_step = stepup(BLINK, exam["request_id"])
note("3_outside_window", **brief(outside))
note("3_exam_window", first=brief(exam), stepup=brief(exam_step))
httpx.delete(f"{C_URL}/exam_windows/{ex['id']}")
cur = [u for u in httpx.get(f"{C_URL}/users").json() if u["id"] == uid][0]["rules"][0]
httpx.delete(f"{C_URL}/rules/{cur['id']}")
httpx.post(f"{C_URL}/rules", json={"user_id": uid, "room_id": "lab_a", "days_mask": 127,
                                   "start_min": 0, "end_min": 0})   # all day, for the rest
httpx.post(f"{K_URL}/api/sync", timeout=30)

# ---- 4. real outage: stop the central process --------------------------------
stop(central)
t_stop = time.time()
secs, st = wait_state("DEGRADED", 40)
deg = verify(burst(img, seed=40))
wrong = stepup(BLINK, deg["request_id"], pin="0000")
deg2 = verify(burst(img, seed=41))
noblink = stepup(NO_BLINK, deg2["request_id"], pin="4321")
deg3 = verify(burst(img, seed=42))
right = stepup(BLINK, deg3["request_id"], pin="4321")
note("4_outage", degraded_after_s=secs, reason=st["reason"], first=brief(deg),
     wrong_pin=brief(wrong), right_pin_no_blink=brief(noblink), right_pin_and_blink=brief(right))

# ---- 5. suspension while the kiosk is offline, then LOCKED -------------------
central = start("central", [PY, "-m", "uvicorn", "central.main:app", "--host", "127.0.0.1",
                            "--port", "9100"], c_env)
wait_http(f"{C_URL}/health")
wait_state("ONLINE", 30)
httpx.post(f"{K_URL}/api/state/simulate", json={"outage": True})
secs, _ = wait_state("DEGRADED", 40)
t_suspend = time.time()
susp = httpx.patch(f"{C_URL}/users/{uid}", json={"standing": "suspended"}).json()
time.sleep(1.5)
off = verify(burst(img, seed=50))
off_step = stepup(BLINK, off["request_id"], pin="4321")
st = kstate()
note("5_offline_after_suspension", simulated_degraded_after_s=secs, central_says=susp,
     kiosk_still_thinks=[u for u in httpx.get(f"{K_URL}/api/users").json() if u["id"] == uid],
     first=brief(off), stepup=brief(off_step), time_left_s=st["time_left_s"])
secs, st = wait_state("LOCKED", 150)
locked = verify(burst(img, seed=51))
note("5_locked", locked_after_more_s=secs, reason=st["reason"], snapshot_age_s=st["snapshot_age_s"],
     ttl_s=st["ttl_s"], attempt=brief(locked))

# ---- 6. reconnect: sync, one alert, new snapshot, user suspended -------------
before = kstate()["outbox"]
httpx.post(f"{K_URL}/api/state/simulate", json={"outage": False})
secs, st = wait_state("ONLINE", 30)
alerts = [a for a in httpx.get(f"{C_URL}/alerts").json()
          if a["type"] == "offline_allow_after_suspension"]
after_sync = verify(burst(img, seed=60))
note("6_reconnect", online_after_s=secs, outbox_before=before, outbox_after=st["outbox"],
     last_sync_push=st["last_sync"]["push"], snapshot_version=st["snapshot_version"],
     alerts_offline_allow_after_suspension=len(alerts),
     alert_detail=[a["detail"] for a in alerts], attempt_now=brief(after_sync))

# ---- 7. re-send the whole outbox: all duplicates ----------------------------
con = sqlite3.connect(WORK / "kiosk.db")
con.row_factory = sqlite3.Row
from backend.outbox import WIRE_FIELDS  # noqa: E402
rows = [dict(r) for r in con.execute(f"select {','.join(WIRE_FIELDS)} from decision_outbox "
                                     "where synced_at is not null order by seq")]
for r_ in rows:   # sqlite returns naive UTC text; send as aware ISO like the agent does
    r_["ts"] = r_["ts"].replace(" ", "T") + "+00:00"
resend = httpx.post(f"{C_URL}/decisions", json={"records": rows},
                    headers={"X-Kiosk-Key": api_key}, timeout=30).json()
note("7_resend", sent=len(rows), accepted=len(resend["accepted"]), duplicates=resend["duplicates"])

# ---- 8. no biometrics in outbox / central decisions ---------------------------
k_cols = [(r[1], r[2]) for r in con.execute("pragma table_info(decision_outbox)")]
sample_k = dict(con.execute("select * from decision_outbox order by seq desc limit 1").fetchone())
con.close()
c = sqlite3.connect(WORK / "central.db")
c.row_factory = sqlite3.Row
c_cols = [(r[1], r[2]) for r in c.execute("pragma table_info(decisions)")]
sample_c = dict(c.execute("select * from decisions order by id desc limit 1").fetchone())
c.close()
longest = max(len(str(v)) for v in list(sample_k.values()) + list(sample_c.values()))
note("8_privacy", kiosk_outbox_columns=k_cols, central_decision_columns=c_cols,
     blob_columns=[n for n, t in k_cols + c_cols if "BLOB" in t.upper()],
     longest_value_chars=longest, sample_kiosk_row=sample_k, sample_central_row=sample_c,
     image_files_in_workdir=[str(p.relative_to(WORK)) for p in WORK.rglob("*")
                             if p.suffix.lower() in (".jpg", ".jpeg", ".png", ".bmp")])

# ---- 9. corrupted signature is rejected, old kept ----------------------------
from backend.snapshot import apply  # noqa: E402
from backend.store import Store  # noqa: E402
snap = httpx.get(f"{C_URL}/snapshot", params={"kiosk_id": "kiosk_lab_a_1"},
                 headers={"X-Kiosk-Key": api_key}).json()
snap["signature"] = ("0" if snap["signature"][0] != "0" else "1") + snap["signature"][1:]
side = Store(WORK, migrate=False, db_path=WORK / "kiosk.db")
v_before = side.snapshot_meta().version
from backend.db import utcnow  # noqa: E402
res = apply(side, snap, KEY, "kiosk_lab_a_1", utcnow())
note("9_bad_signature", result=res, version_before=v_before,
     version_after=side.snapshot_meta().version)
side.engine.dispose()

# ---- 10. kill the kiosk during a snapshot apply -------------------------------
stop(kiosk)
httpx.patch(f"{C_URL}/users/{uid}", json={"standing": "active"})   # new version on central
fresh = httpx.get(f"{C_URL}/snapshot", params={"kiosk_id": "kiosk_lab_a_1"},
                  headers={"X-Kiosk-Key": api_key}).json()
(WORK / "fresh.json").write_text(json.dumps(fresh))
code = ("import json,sys; from backend.store import Store; from backend.snapshot import apply;"
        "from backend.db import utcnow; s=Store(sys.argv[1], migrate=False, db_path=sys.argv[2]);"
        f"apply(s, json.load(open(sys.argv[3])), '{KEY}', 'kiosk_lab_a_1', utcnow())")
proc = subprocess.run([PY, "-c", code, str(WORK), str(WORK / "kiosk.db"), str(WORK / "fresh.json")],
                      env={**os.environ, "SPOOF_FAULT_APPLY": "crash"}, capture_output=True, text=True)
check = Store(WORK, migrate=False, db_path=WORK / "kiosk.db")
v_after_crash = check.snapshot_meta().version
standing_after_crash = check.get_user(uid)["standing"]
check.engine.dispose()
kiosk = start("kiosk", [PY, "-m", "uvicorn", "backend.main:app", "--host", "127.0.0.1",
                        "--port", "8100"], k_env)
wait_http(f"{K_URL}/api/state")
secs, st = wait_state("ONLINE", 30)
note("10_kill_during_apply", crash_exit_code=proc.returncode, central_version=fresh["body"]["version"],
     kiosk_version_after_crash=v_after_crash, user_standing_after_crash=standing_after_crash,
     after_restart_version=st["snapshot_version"], backups=sorted(p.name for p in (WORK / "backup").glob("*.db")))

stop(kiosk)
stop(central)

# ---- 11. tests and migrations up/down/up on both histories (alembic CLI) ------
mig = {}
for which in ("kiosk", "central"):
    url = f"sqlite:///{(WORK / f'mig_{which}.db').as_posix()}"
    steps = []
    for cmd in (["upgrade", "head"], ["downgrade", "base"], ["upgrade", "head"]):
        p = subprocess.run([PY, "-m", "alembic", "-c", f"alembic_{which}.ini", "-x", f"url={url}", *cmd],
                           capture_output=True, text=True)
        steps.append(f"{' '.join(cmd)}: exit {p.returncode}")
    cur = subprocess.run([PY, "-m", "alembic", "-c", f"alembic_{which}.ini", "-x", f"url={url}", "current"],
                         capture_output=True, text=True).stdout.strip().splitlines()
    mig[which] = {"steps": steps, "current": cur[-1] if cur else None}
tests = subprocess.run([PY, "-m", "pytest", "tests", "-q", "-p", "no:cacheprovider"],
                       capture_output=True, text=True)
note("11_tests_and_migrations", migrations=mig,
     pytest=tests.stdout.strip().splitlines()[-1] if tests.stdout else tests.stderr[-300:])

(WORK / "results.json").write_text(json.dumps(results, indent=1, default=str))
print("DONE")
