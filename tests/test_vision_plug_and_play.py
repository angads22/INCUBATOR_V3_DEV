"""Plug-and-play vision: auto-detect, lazy on-command load, status/results/install."""

from app.services.vision_service import VisionService


# ── unit: backend resolution + lazy loading (no hardware, no tflite_runtime) ──

def test_auto_uses_dropped_in_model(tmp_path):
    model = tmp_path / "model.tflite"
    model.write_bytes(b"\x00" * 1024)  # presence is enough for detection
    vs = VisionService(backend="auto", tflite_model_path=str(model))
    vs.setup()
    assert vs.backend == "tflite"               # auto-detected the dropped-in model
    assert vs._tflite_ready is False            # but NOT loaded at launch (lazy)
    st = vs.status()
    assert st["classifier"]["available"] is True
    assert st["on_command_only"] is True


def test_auto_falls_back_to_mock_without_model(tmp_path):
    vs = VisionService(backend="auto", tflite_model_path=str(tmp_path / "absent.tflite"))
    vs.setup()
    assert vs.backend == "mock"


def test_explicit_mock_is_forced_even_with_model(tmp_path):
    model = tmp_path / "model.tflite"
    model.write_bytes(b"\x00" * 1024)
    vs = VisionService(backend="mock", tflite_model_path=str(model))
    vs.setup()
    assert vs.backend == "mock"                  # explicit mock wins over detection


def test_inference_is_lazy_and_safe_without_runtime(tmp_path):
    model = tmp_path / "model.tflite"
    model.write_bytes(b"\x00" * 1024)
    vs = VisionService(backend="auto", tflite_model_path=str(model))
    vs.setup()
    # tflite_runtime isn't installed in CI → load fails gracefully → mock result,
    # and it doesn't retry the load on every call.
    out = vs.analyze_egg_image(str(model))
    assert out["ok"] is True
    assert vs._tflite_attempted is True


# ── API endpoints ────────────────────────────────────────────────────────────

def test_vision_status_and_results(client):
    st = client.get("/api/vision/status").json()
    assert st["ok"] is True and st["on_command_only"] is True
    assert client.get("/api/vision/results").json()["ok"] is True


def test_install_model_endpoint(client):
    files = {"model": ("stage.tflite", b"\x00" * 2048, "application/octet-stream")}
    r = client.post("/api/vision/model", data={"kind": "stage"}, files=files)
    assert r.status_code == 200
    body = r.json()
    assert body["installed"] == "stage"
    assert body["stage"]["available"] is True     # picked up immediately, no restart


def test_install_model_rejects_non_tflite(client):
    files = {"model": ("notes.txt", b"hello", "text/plain")}
    r = client.post("/api/vision/model", data={"kind": "classifier"}, files=files)
    assert r.status_code == 400
