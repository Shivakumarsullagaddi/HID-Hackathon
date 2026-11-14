"""Keep every test away from real data and the real central service:
- the app's startup (backup + migrate data/kiosk.db) runs against a temp dir
- no background polling, and no kiosk API key, so a test run can never
  push records to a central service that happens to be running locally."""
import pytest

from backend import config


@pytest.fixture(autouse=True, scope="session")
def _isolated_kiosk(tmp_path_factory):
    d = tmp_path_factory.mktemp("kiosk_home")
    saved = (config.KIOSK_DB_PATH, config.BACKGROUND_POLL, config.KIOSK_API_KEY,
             config.CENTRAL_URL)
    config.KIOSK_DB_PATH = d / "kiosk.db"
    config.BACKGROUND_POLL = False
    config.KIOSK_API_KEY = None
    config.CENTRAL_URL = "http://127.0.0.1:9"   # discard port: never a real service
    yield
    (config.KIOSK_DB_PATH, config.BACKGROUND_POLL, config.KIOSK_API_KEY,
     config.CENTRAL_URL) = saved
