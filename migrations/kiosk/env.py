"""Alembic environment for the kiosk database. render_as_batch=True because
SQLite cannot alter columns in place."""
import sys
from pathlib import Path

from alembic import context

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend import config as kiosk_config  # noqa: E402
from backend.db import make_engine, sqlite_url  # noqa: E402
from backend.kiosk_models import KioskBase  # noqa: E402

cfg = context.config
url = (cfg.attributes.get("url") or context.get_x_argument(as_dictionary=True).get("url")
       or sqlite_url(kiosk_config.KIOSK_DB_PATH))

if context.is_offline_mode():
    context.configure(url=url, target_metadata=KioskBase.metadata, render_as_batch=True,
                      literal_binds=True)
    with context.begin_transaction():
        context.run_migrations()
else:
    engine = make_engine(url)
    try:
        with engine.connect() as conn:
            context.configure(connection=conn, target_metadata=KioskBase.metadata,
                              render_as_batch=True, compare_type=True)
            with context.begin_transaction():
                context.run_migrations()
    finally:
        engine.dispose()
