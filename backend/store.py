"""File storage: gallery (JSON), decision log (JSON Lines), lab clips and
expected decisions. No database in Phase 1."""
import hashlib
import json
import os
import re
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from . import config


def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def slug(text):
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-") or "x"


class Store:
    def __init__(self, data_dir=None):
        self.dir = Path(data_dir or config.DATA_DIR)
        self.lab_dir = self.dir / "lab"
        self.lab_dir.mkdir(parents=True, exist_ok=True)
        self.gallery_path = self.dir / "gallery.json"
        self.decisions_path = self.dir / "decisions.jsonl"
        self.expected_path = self.dir / "expected.json"
        self.metrics_path = self.dir / "metrics.json"
        self.offline_queue_path = self.dir / "offline_queue.jsonl"
        self._lock = threading.RLock()

    # ---- helpers -------------------------------------------------------
    def _read_json(self, path, default):
        if not path.exists():
            return default
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)

    def _write_json(self, path, obj):
        tmp = path.with_suffix(path.suffix + ".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(obj, f, indent=1)
        os.replace(tmp, path)

    # ---- gallery -------------------------------------------------------
    def users(self):
        with self._lock:
            users = self._read_json(self.gallery_path, {"users": []})["users"]
            for u in users:
                if "role" not in u:
                    u["role"] = None
            return users

    def get_user(self, user_id):
        return next((u for u in self.users() if u["id"] == user_id), None)

    def add_user(self, name, embedding, standing="active", role=None):
        with self._lock:
            users = self.users()
            n = max([int(u["id"].split("_")[1]) for u in users] or [0]) + 1
            user = {
                "id": f"u_{n:03d}",
                "name": name,
                "standing": standing,
                "role": role,
                "enrolled_at": now_iso(),
                "embedding": [round(float(x), 6) for x in embedding],
            }
            users.append(user)
            self._write_json(self.gallery_path, {"users": users})
            return user

    def update_user(self, user_id, standing=None, role=None):
        with self._lock:
            users = self.users()
            for u in users:
                if u["id"] == user_id:
                    if standing is not None:
                        u["standing"] = standing
                    if role is not None:
                        u["role"] = role
                    self._write_json(self.gallery_path, {"users": users})
                    return u
            return None

    def set_standing(self, user_id, standing):
        return self.update_user(user_id, standing=standing)

    def delete_user(self, user_id):
        with self._lock:
            users = self.users()
            kept = [u for u in users if u["id"] != user_id]
            if len(kept) == len(users):
                return False
            self._write_json(self.gallery_path, {"users": kept})
            return True

    # ---- decision log --------------------------------------------------
    def append_decision(self, record):
        with self._lock:
            with open(self.decisions_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(record) + "\n")

    def read_decisions(self):
        if not self.decisions_path.exists():
            return []
        out = []
        with open(self.decisions_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    out.append(json.loads(line))
        return out

    # ---- offline fail-safe queue & biometrics scrubbing ----------------
    @staticmethod
    def scrub_biometrics(record: dict) -> dict:
        raw_key_material = f"{record.get('ts')}|{record.get('request_id')}|{record.get('matched_user')}|{record.get('decision')}|{record.get('stage')}"
        integrity_hash = hashlib.sha256(raw_key_material.encode("utf-8")).hexdigest()
        return {
            "event_id": uuid.uuid4().hex,
            "ts": record.get("ts"),
            "request_id": record.get("request_id"),
            "stage": record.get("stage", "verify"),
            "source": record.get("source", "kiosk"),
            "decision": record.get("decision"),
            "reason": record.get("reason"),
            "matched_user": record.get("matched_user"),
            "standing": record.get("standing"),
            "role": record.get("role", config.DEFAULT_ROLE),
            "zone": record.get("zone", config.DEFAULT_ZONE),
            "access_hour": record.get("access_hour"),
            "schedule_status": record.get("schedule_status", "ALLOW"),
            "gate_passed": record.get("gate_passed"),
            "offline_mode": True,
            "liveness_mode": record.get("liveness_mode", "offline_scrubbed"),
            "biometrics_scrubbed": True,
            "integrity_hash": integrity_hash,
            "latency_ms": record.get("latency_ms"),
        }

    def append_offline_decision(self, record: dict) -> dict:
        scrubbed = self.scrub_biometrics(record)
        with self._lock:
            with open(self.offline_queue_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(scrubbed) + "\n")
        return scrubbed

    def read_offline_queue(self) -> list[dict]:
        with self._lock:
            if not self.offline_queue_path.exists():
                return []
            out = []
            with open(self.offline_queue_path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line:
                        out.append(json.loads(line))
            return out

    def sync_offline_to_central(self) -> dict:
        with self._lock:
            queued = self.read_offline_queue()
            if not queued:
                return {"synced_count": 0, "remaining_queue": 0, "synced_at": None}
            synced_ts = now_iso()
            with open(self.decisions_path, "a", encoding="utf-8") as f:
                for item in queued:
                    entry = {**item, "synced_from_offline": True, "synced_at": synced_ts}
                    f.write(json.dumps(entry) + "\n")
            tmp = self.offline_queue_path.with_suffix(".tmp")
            with open(tmp, "w", encoding="utf-8") as f:
                pass
            os.replace(tmp, self.offline_queue_path)
            return {"synced_count": len(queued), "remaining_queue": 0, "synced_at": synced_ts}


    # ---- lab clips -----------------------------------------------------
    def expected(self):
        with self._lock:
            data = self._read_json(self.expected_path, {})
            data.setdefault("conditions", {})
            data.setdefault("clips", [])
            return data

    def clip_frame_paths(self, name):
        d = self.lab_dir / name
        if not d.is_dir():
            return []
        return sorted(p for p in d.iterdir()
                      if p.suffix.lower() in (".jpg", ".jpeg", ".png"))

    def add_clip(self, meta, jpeg_frames):
        """meta: type, person, enrolled, lighting, expected, notes."""
        with self._lock:
            data = self.expected()
            base = f"{meta['type']}_{slug(meta['person'])}_{slug(meta['lighting'])}"
            existing = {c["name"] for c in data["clips"]}
            n = 1
            while f"{base}_{n:02d}" in existing or (self.lab_dir / f"{base}_{n:02d}").exists():
                n += 1
            name = f"{base}_{n:02d}"
            d = self.lab_dir / name
            d.mkdir(parents=True)
            for i, raw in enumerate(jpeg_frames):
                (d / f"frame_{i}.jpg").write_bytes(raw)
            clip = {"name": name, **meta, "recorded_at": now_iso()}
            data["clips"].append(clip)
            self._write_json(self.expected_path, data)
            return clip

    def delete_clip(self, name):
        with self._lock:
            data = self.expected()
            kept = [c for c in data["clips"] if c["name"] != name]
            if len(kept) == len(data["clips"]):
                return False
            data["clips"] = kept
            self._write_json(self.expected_path, data)
            for p in self.clip_frame_paths(name):
                p.unlink()
            d = self.lab_dir / name
            if d.is_dir() and not any(d.iterdir()):
                d.rmdir()
            return True

    # ---- harness output ------------------------------------------------
    def write_metrics(self, metrics):
        with self._lock:
            self._write_json(self.metrics_path, metrics)

    def read_metrics(self):
        return self._read_json(self.metrics_path, None)
