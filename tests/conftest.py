import os
import tempfile

# Configure the app for headless, hardware-free testing BEFORE it is imported.
# settings is a frozen dataclass evaluated at import time, so env must be set first.
_db_fd, _db_path = tempfile.mkstemp(suffix=".db")
os.close(_db_fd)
os.environ.setdefault("INCUBATOR_DB_URL", f"sqlite:///{_db_path}")
os.environ.setdefault("GPIO_MOCK", "true")
os.environ.setdefault("CAMERA_BACKEND", "mock")
os.environ.setdefault("CAMERA_IMAGE_DIR", os.path.join(tempfile.gettempdir(), "incubator_test_captures"))
os.environ.setdefault("VISION_BACKEND", "mock")
os.environ.setdefault("INCUBATOR_AUTO_HOTSPOT", "false")
os.environ.setdefault("INCUBATOR_BUTTON_MOCK_FILE", os.path.join(tempfile.gettempdir(), "btn_mock"))

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
    """Each test starts from a clean database and no cookies."""
    from app.database import Base, engine
    import app.models  # noqa: F401  (registers all tables on Base.metadata)

    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    client.cookies.clear()
    yield
