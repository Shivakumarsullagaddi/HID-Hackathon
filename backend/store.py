"""Kiosk storage.

Product data lives in SQLite (data/kiosk.db, see kiosk_models.py):
users and templates (snapshot cache), schedule, snapshot metadata, the
decision outbox and the local decision log. Lab clips, expected.json and
metrics.json stay as files: they are test tooling, not product data.

The public methods of the old JSON store are unchanged (users, get_user,
add_user, set_standing, delete_user, append_decision, read_decisions, and
the lab/metrics methods). Users and templates are cached in memory and
reloaded whenever the tables change, so matching does not touch SQLite.
"""
import json
import os
import re
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
from sqlalchemy import delete, func, select
from sqlalchemy.orm import sessionmaker

from . import config
from .db import make_engine, sqlite_url, upgrade
from .kiosk_models import DecisionLog, DecisionOutbox, SnapshotMeta, Template, UserCache


def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def slug(text):
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-") or "x"


def embedding_to_blob(embedding):
    arr = np.asarray(embedding, dtype=np.float32).reshape(-1)
    if arr.size != config.EMBEDDING_DIM:
        raise ValueError(f"embedding has {arr.size} values, expected {config.EMBEDDING_DIM}")
    return arr.tobytes()


def blob_to_embedding(blob):
    return np.frombuffer(blob, dtype=np.float32).copy()


class Store:
    def __init__(self, data_dir=None, migrate=True, db_path=None):
        self.dir = Path(data_dir or config.DATA_DIR)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.lab_dir = self.dir / "lab"
        self.lab_dir.mkdir(parents=True, exist_ok=True)
        self.expected_path = self.dir / "expected.json"
        self.metrics_path = self.dir / "metrics.json"
        # Legacy JSON files, read only by tools/import_json.py.
        self.gallery_path = self.dir / "gallery.json"
        self.decisions_path = self.dir / "decisions.jsonl"

        if db_path is None:
            db_path = config.KIOSK_DB_PATH if data_dir is None else self.dir / "kiosk.db"
        self.db_path = Path(db_path)
        self.db_url = sqlite_url(self.db_path)
        if migrate:
            upgrade("kiosk", self.db_url)
        self.engine = make_engine(self.db_url)
        self.Session = sessionmaker(self.engine, expire_on_commit=False)
        self._lock = threading.RLock()
        self._users = None   # in-memory cache of users + templates
        self.reload_cache()

    # ---- sessions ------------------------------------------------------
    @contextmanager
    def transaction(self):
        """One write transaction; rolls back on any exception."""
        with self._lock, self.Session() as s:
            with s.begin():
                yield s

    @contextmanager
    def session(self):
        with self.Session() as s:
            yield s

    def _write_json(self, path, obj):
        tmp = path.with_suffix(path.suffix + ".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(obj, f, indent=1)
        os.replace(tmp, path)

    def _read_json(self, path, default):
        if not path.exists():
            return default
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)

    # ---- users and templates -------------------------------------------
    def reload_cache(self):
        """Reload users + templates into memory (after any change)."""
        with self._lock, self.Session() as s:
            rows = s.execute(
                select(UserCache, Template.embedding)
                .outerjoin(Template, (Template.user_id == UserCache.user_id)
                           & (Template.model_id == config.FACE_MODEL_ID))
                .order_by(UserCache.user_id)).all()
            users = []
            for u, emb in rows:
                users.append({
                    "id": u.user_id,
                    "name": u.name,
                    "standing": u.standing,
                    "has_pin": bool(u.pin_hash),
                    "embedding": blob_to_embedding(emb) if emb is not None else None,
                })
            self._users = users

    def users(self):
        """Users with a usable template (what matching needs) plus those without."""
        return list(self._users)

    def matchable_users(self):
        return [u for u in self._users if u["embedding"] is not None]

    def get_user(self, user_id):
        return next((u for u in self._users if u["id"] == user_id), None)

    def get_pin(self, user_id):
        with self.session() as s:
            u = s.get(UserCache, user_id)
            return (u.pin_salt, u.pin_hash) if u else (None, None)

    def add_user(self, name, embedding, standing="active", pin_salt=None, pin_hash=None,
                 user_id=None):
        """Local add (tests and local-only mode). With a central service the
        kiosk never adds users itself: they arrive in a signed snapshot."""
        blob = embedding_to_blob(embedding)
        with self.transaction() as s:
            if user_id is None:
                ids = s.scalars(select(UserCache.user_id)).all()
                n = max([int(i.split("_")[1]) for i in ids if re.fullmatch(r"u_\d+", i)] or [0]) + 1
                user_id = f"u_{n:03d}"
            s.add(UserCache(user_id=user_id, name=name, standing=standing,
                            pin_salt=pin_salt, pin_hash=pin_hash))
            s.flush()
            s.add(Template(user_id=user_id, embedding=blob, model_id=config.FACE_MODEL_ID))
        self.reload_cache()
        return self.get_user(user_id)

    def set_standing(self, user_id, standing):
        with self.transaction() as s:
            u = s.get(UserCache, user_id)
            if u is None:
                return None
            u.standing = standing
        self.reload_cache()
        return self.get_user(user_id)

    def delete_user(self, user_id):
        with self.transaction() as s:
            u = s.get(UserCache, user_id)
            if u is None:
                return False
            s.execute(delete(Template).where(Template.user_id == user_id))
            s.delete(u)
        self.reload_cache()
        return True

    # ---- local decision log --------------------------------------------
    @staticmethod
    def _log_row(record):
        return DecisionLog(
            request_id=record["request_id"], stage=record.get("stage", "verify"),
            ts=record.get("ts") or now_iso(), source=record.get("source", "kiosk"),
            decision=record.get("decision", ""), payload=json.dumps(record))

    def append_decision(self, record):
        with self.transaction() as s:
            s.add(self._log_row(record))

    def append_access_record(self, record, outbox_row):
        """Decision log entry and outbox row in ONE transaction. The outbox
        row's hash chain is computed inside the same transaction."""
        from .outbox import chain_row
        with self.transaction() as s:
            s.add(self._log_row(record))
            if outbox_row is not None:
                chain_row(s, outbox_row)
                s.add(outbox_row)

    def read_decisions(self):
        with self.session() as s:
            return [json.loads(p) for p in
                    s.scalars(select(DecisionLog.payload).order_by(DecisionLog.id))]

    def snapshot_meta(self):
        with self.session() as s:
            return s.get(SnapshotMeta, 1)

    def outbox_counts(self):
        with self.session() as s:
            total = s.scalar(select(func.count()).select_from(DecisionOutbox))
            unsynced = s.scalar(select(func.count()).select_from(DecisionOutbox)
                                .where(DecisionOutbox.synced_at.is_(None)))
            return {"total": total, "unsynced": unsynced}

    # ---- lab clips (files) ---------------------------------------------
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

    # ---- harness output (file) -----------------------------------------
    def write_metrics(self, metrics):
        with self._lock:
            self._write_json(self.metrics_path, metrics)

    def read_metrics(self):
        return self._read_json(self.metrics_path, None)
