"""Keep every test away from the real data/ folder: the app's startup
(backup + migrate data/kiosk.db) runs against a temp directory instead."""
import pytest

from backend import config


@pytest.fixture(autouse=True, scope="session")
def _isolated_kiosk_db(tmp_path_factory):
    d = tmp_path_factory.mktemp("kiosk_home")
    old = config.KIOSK_DB_PATH
    config.KIOSK_DB_PATH = d / "kiosk.db"
    yield
    config.KIOSK_DB_PATH = old
