"""File storage: gallery (JSON), decision log (JSON Lines), lab clips and
expected decisions. No database in Phase 1."""
import json
import os
import re
import threading
from datetime import datetime, timezone
from pathlib import Path

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
            return self._read_json(self.gallery_path, {"users": []})["users"]

    def get_user(self, user_id):
        return next((u for u in self.users() if u["id"] == user_id), None)

    def _save_aws_metadata(self, user):
        """Optionally syncs enrolled user metadata to AWS DynamoDB / S3 if configured."""
        table_name = os.environ.get("DYNAMODB_USERS_TABLE")
        s3_bucket = os.environ.get("S3_METADATA_BUCKET")
        if not (table_name or s3_bucket):
            return
        try:
            import boto3
            region = os.environ.get("AWS_REGION", "us-east-1")
            if table_name:
                dynamodb = boto3.resource("dynamodb", region_name=region)
                table = dynamodb.Table(table_name)
                table.put_item(Item={
                    "user_id": user["id"],
                    "name": user["name"],
                    "email": user.get("email") or "",
                    "standing": user["standing"],
                    "enrolled_at": user["enrolled_at"],
                })
            if s3_bucket:
                s3 = boto3.client("s3", region_name=region)
                s3.put_object(
                    Bucket=s3_bucket,
                    Key=f"users/{user['id']}.json",
                    Body=json.dumps(user),
                    ContentType="application/json"
                )
        except Exception:
            pass

    def add_user(self, name, embedding, standing="active", email=None):
        with self._lock:
            users = self.users()
            n = max([int(u["id"].split("_")[1]) for u in users] or [0]) + 1
            user = {
                "id": f"u_{n:03d}",
                "name": name,
                "email": email,
                "standing": standing,
                "enrolled_at": now_iso(),
                "embedding": [round(float(x), 6) for x in embedding],
            }
            users.append(user)
            self._write_json(self.gallery_path, {"users": users})
            self._save_aws_metadata(user)
            return user

    def set_standing(self, user_id, standing):
        with self._lock:
            users = self.users()
            for u in users:
                if u["id"] == user_id:
                    u["standing"] = standing
                    self._write_json(self.gallery_path, {"users": users})
                    return u
            return None

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
