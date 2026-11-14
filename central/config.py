"""Central service settings. Central stands in for the cloud side; here it
is a second local process (uvicorn central.main:app --port 9000)."""
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"


def _default_db_url():
    return f"sqlite:///{(DATA_DIR / 'central.db').resolve().as_posix()}"


# SQLite today; PostgreSQL in production is a change of this URL only.
CENTRAL_DB_URL = os.environ.get("CENTRAL_DB_URL") or _default_db_url()
DEV_HMAC_KEY = "dev-only-snapshot-key-change-me"
SNAPSHOT_HMAC_KEY = os.environ.get("SNAPSHOT_HMAC_KEY", DEV_HMAC_KEY)
SCHEMA_VERSION = 1
FACE_MODEL_ID = "buffalo_sc/w600k_mbf"
EMBEDDING_DIM = 512

# Rule created for a person enrolled at a kiosk, in that kiosk's room:
# every day, 07:00 to 22:00 local time. Change it on the admin page.
DEFAULT_RULE = {"days_mask": 0b1111111, "start_min": 7 * 60, "end_min": 22 * 60}
ROOM_TIMEZONE = os.environ.get("ROOM_TIMEZONE", "Asia/Kolkata")
MAX_BATCH = 500
