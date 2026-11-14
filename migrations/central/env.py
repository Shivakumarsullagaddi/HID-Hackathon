"""Alembic environment for the central database (SQLite now, PostgreSQL later).
render_as_batch is on for SQLite only; PostgreSQL alters columns in place."""
import sys
from pathlib import Path

from alembic import context

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.db import make_engine  # noqa: E402
from central import config as central_config  # noqa: E402
from central.models import CentralBase  # noqa: E402

cfg = context.config
url = (cfg.attributes.get("url") or context.get_x_argument(as_dictionary=True).get("url")
       or central_config.CENTRAL_DB_URL)
batch = url.startswith("sqlite")

if context.is_offline_mode():
    context.configure(url=url, target_metadata=CentralBase.metadata, render_as_batch=batch,
                      literal_binds=True)
    with context.begin_transaction():
        context.run_migrations()
else:
    engine = make_engine(url)
    try:
        with engine.connect() as conn:
            context.configure(connection=conn, target_metadata=CentralBase.metadata,
                              render_as_batch=batch, compare_type=True)
            with context.begin_transaction():
                context.run_migrations()
    finally:
        engine.dispose()
