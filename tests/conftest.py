import os
import tempfile

# Configure the app for headless, hardware-free testing BEFORE it is imported.
# settings is a frozen dataclass evaluated at import time, so env must be set first.
_db_fd, _db_path = tempfile.mkstemp(suffix=".db")
os.close(_db_fd)
os.environ.setdefault("INCUBATOR_DB_URL", f"sqlite:///{_db_path}")
os.environ.setdefault("GPIO_MOCK", "true")
os.environ.setdefault("CAMERA_BACKEND", "mock")
os.environ.setdefault("VISION_BACKEND", "mock")
os.environ.setdefault("INCUBATOR_AUTO_HOTSPOT", "false")
os.environ.setdefault("INCUBATOR_BUTTON_MOCK_FILE", os.path.join(tempfile.gettempdir(), "btn_mock"))
# Keep capture/preview writes inside the temp dir, never the repo or SD card.
os.environ.setdefault("CAMERA_IMAGE_DIR", os.path.join(tempfile.gettempdir(), "incubator_test_captures"))
os.environ.setdefault("CAMERA_FRAME_DIR", os.path.join(tempfile.gettempdir(), "incubator_test_frames"))
# Vision model paths under temp so the upload/install tests never write to the
# repo or an unwritable system path.
os.environ.setdefault("VISION_TFLITE_MODEL", os.path.join(tempfile.gettempdir(), "incubator_test_models", "model.tflite"))
os.environ.setdefault("VISION_STAGE_MODEL", os.path.join(tempfile.gettempdir(), "incubator_test_models", "stage.tflite"))
# The suite must never reach a real NetworkManager: with nmcli on PATH the
# HTTP-level flows would block on 90 s joins and could rewrite the host's
# Wi-Fi profiles. Point nmcli at a path that does not exist; the WiFiService
# then takes its explicit mock path (GPIO_MOCK=true above).
os.environ.setdefault("INCUBATOR_NMCLI_BIN", "/nonexistent/nmcli")
# Setup-mode state file private to this test process (never the shared
# /tmp/incubator_setup_state.json a production or sibling run might use).
os.environ.setdefault(
    "INCUBATOR_SETUP_STATE_PATH",
    os.path.join(tempfile.mkdtemp(prefix="incubator_test_"), "setup-state.json"),
)

import pytest
from fastapi.testclient import TestClient


@pytest.fixture(scope="session")
def client():
    # Imported lazily so the env above is applied first.
    from app.main import app

    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture(autouse=True)
def _isolate(client):
    """Each test starts from a clean database, no cookies and no network state."""
    from app.database import Base, engine
    import app.models  # noqa: F401  (registers all tables on Base.metadata)
    from app.services import wifi_service as _wifi_mod

    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    client.cookies.clear()
    # The NetworkManager probe is cached for 60 s; tests swap nmcli fakes in
    # and out, so every test starts with a fresh probe.
    _wifi_mod.reset_nm_probe_cache()
    yield
    _wifi_mod.reset_nm_probe_cache()
    # Leave the shared app services as a fresh device would be: no AP up, no
    # fallback cycle, setup mode off — whatever a test did over HTTP.
    try:
        from app import main as _main

        _main.onboarding_service._hotspot_active = False
        _main.onboarding_service._fallback_active = False
        _main.onboarding_service._ap_retry_source = None
        _main.onboarding_service._manual_session_started_at = None
        _main.setup_mode_service.exit_setup_mode()
    except Exception:  # noqa: BLE001 — isolation is best effort
        pass
