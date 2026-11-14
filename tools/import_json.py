"""One-time import of the Phase 1 JSON files into the kiosk database.

    python tools/import_json.py            # data/gallery.json + data/decisions.jsonl -> data/kiosk.db

Idempotent: users already in users_cache and log lines already in
decision_log are skipped, so running it twice changes nothing. The JSON
files are left in place.

Note: once a central snapshot is applied, users_cache is replaced by the
snapshot (central owns users). Use tools/seed_central.py to load these
users into central.
"""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from sqlalchemy import select  # noqa: E402

from backend import config  # noqa: E402
from backend.kiosk_models import DecisionLog, Template, UserCache  # noqa: E402
from backend.store import Store, embedding_to_blob  # noqa: E402


def import_json(store):
    users_new = users_seen = log_new = log_seen = 0
    if store.gallery_path.exists():
        gallery = json.loads(store.gallery_path.read_text(encoding="utf-8"))
        with store.transaction() as s:
            for u in gallery.get("users", []):
                if s.get(UserCache, u["id"]) is not None:
                    users_seen += 1
                    continue
                s.add(UserCache(user_id=u["id"], name=u["name"], standing=u["standing"]))
                s.flush()
                s.add(Template(user_id=u["id"], embedding=embedding_to_blob(u["embedding"]),
                               model_id=config.FACE_MODEL_ID))
                users_new += 1
    if store.decisions_path.exists():
        with store.transaction() as s:
            have = set(s.execute(select(DecisionLog.request_id, DecisionLog.stage,
                                        DecisionLog.ts)).all())
            for line in store.decisions_path.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                rec = json.loads(line)
                key = (rec["request_id"], rec.get("stage", "verify"), rec["ts"])
                if key in have:
                    log_seen += 1
                    continue
                have.add(key)
                s.add(Store._log_row(rec))
                log_new += 1
    store.reload_cache()
    return {"users_imported": users_new, "users_skipped": users_seen,
            "log_imported": log_new, "log_skipped": log_seen}


if __name__ == "__main__":
    print(import_json(Store()))
