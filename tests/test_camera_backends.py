"""CameraService backends with fakes: auto resolution, picamera2 preview rate,
OpenCV device scan, and an MJPEG stream that never pins a thread."""

import io
import sys
import threading
import time
import types


from PIL import Image

from app.services.camera_service import (
    PREVIEW_BUFFER_COUNT,
    CameraService,
    frame_duration_us,
)


def _cam(tmp_path, **kw):
    kw.setdefault("image_dir", str(tmp_path / "captures"))
    kw.setdefault("frame_dir", str(tmp_path / "frames"))
    kw.setdefault("resolution", (320, 240))
    kw.setdefault("preview_resolution", (160, 120))
    return CameraService(**kw)


# ── auto resolution ──────────────────────────────────────────────────────────

def test_auto_prefers_picamera2(tmp_path):
    cam = _cam(tmp_path, backend="auto", importable=lambda n: n == "picamera2")
    assert cam.backend_resolved == "picamera2" and cam.backend_requested == "auto"


def test_auto_uses_opencv_only_with_a_video_node(tmp_path):
    dev = tmp_path / "video0"
    cam = _cam(tmp_path, backend="auto", importable=lambda n: n == "cv2", video_devices=[str(dev)])
    assert cam.backend_resolved == "mock"                   # cv2 present, no device
    dev.write_bytes(b"")
    cam = _cam(tmp_path, backend="auto", importable=lambda n: n == "cv2", video_devices=[str(dev)])
    assert cam.backend_resolved == "opencv"


def test_auto_falls_back_to_mock(tmp_path):
    cam = _cam(tmp_path, backend="auto", importable=lambda n: False, video_devices=[])
    assert cam.backend_resolved == "mock"
    assert cam.capture()["backend"] == "mock"


def test_explicit_backend_is_kept_and_unknown_becomes_auto(tmp_path):
    assert _cam(tmp_path, backend="mock", importable=lambda n: True).backend_resolved == "mock"
    cam = _cam(tmp_path, backend="webcam", importable=lambda n: False, video_devices=[])
    assert cam.backend_resolved == "mock" and cam.backend_requested == "webcam"


def test_main_camera_service_resolves_mock_in_tests():
    from app import main

    assert main.camera_service.backend_resolved == "mock"


# ── picamera2 preview configuration ──────────────────────────────────────────

def test_frame_duration_us():
    assert frame_duration_us(1.0) == 1_000_000
    assert frame_duration_us(5.0) == 200_000
    assert frame_duration_us(100.0) == 33_333           # clamped to 30 fps
    assert frame_duration_us(0.1) == 2_000_000          # clamped to 0.5 fps


def _fake_picamera2(record):
    mod = types.ModuleType("picamera2")

    class Picamera2:
        def __init__(self):
            self.started = False

        def create_video_configuration(self, main, controls=None, buffer_count=6):
            record["video"] = {"main": main, "controls": controls, "buffer_count": buffer_count}
            return {"kind": "video"}

        def create_still_configuration(self, main):
            record["still"] = main
            return {"kind": "still"}

        def configure(self, cfg):
            record["configured"] = cfg

        def start(self):
            self.started = True
            record["started"] = True

        def capture_array(self, name):
            return Image.new("RGB", (160, 120))

        def switch_mode_and_capture_array(self, cfg, name):
            record["switched"] = cfg
            return Image.new("RGB", (320, 240))

        def stop(self):
            record["stopped"] = True

        def close(self):
            record["closed"] = True

    mod.Picamera2 = Picamera2
    return mod


def test_picamera2_preview_requests_preview_fps_and_two_buffers(tmp_path, monkeypatch):
    record = {}
    monkeypatch.setitem(sys.modules, "picamera2", _fake_picamera2(record))
    cam = _cam(tmp_path, backend="picamera2", preview_fps=2.0)
    data = cam.preview_jpeg()
    assert Image.open(io.BytesIO(data)).size == (160, 120)
    assert record["video"]["controls"] == {"FrameDurationLimits": (500_000, 500_000)}
    assert record["video"]["buffer_count"] == PREVIEW_BUFFER_COUNT
    assert record["video"]["main"]["size"] == (160, 120)
    assert record["started"] is True
    snap = cam.snapshot_jpeg()
    assert Image.open(io.BytesIO(snap)).size == (320, 240) and record["switched"] == {"kind": "still"}
    cam.cleanup()
    assert record["stopped"] and record["closed"]


def test_picamera2_falls_back_when_controls_rejected(tmp_path, monkeypatch):
    record = {}
    mod = _fake_picamera2(record)
    orig = mod.Picamera2.create_video_configuration

    def strict(self, main, **kw):
        if kw:
            raise TypeError("unexpected keyword")
        return orig(self, main)

    mod.Picamera2.create_video_configuration = strict
    monkeypatch.setitem(sys.modules, "picamera2", mod)
    cam = _cam(tmp_path, backend="picamera2")
    assert cam.preview_jpeg()
    assert record["video"]["controls"] is None


# ── OpenCV device scan ───────────────────────────────────────────────────────

def _fake_cv2(good_devices, record):
    mod = types.ModuleType("cv2")
    mod.CAP_PROP_FRAME_WIDTH, mod.CAP_PROP_FRAME_HEIGHT, mod.COLOR_BGR2RGB = 3, 4, 4

    class VideoCapture:
        def __init__(self, device):
            self.device = device
            record.setdefault("opened", []).append(device)
            self._open = device in good_devices or device in ("decoder",)

        def isOpened(self):
            return self._open or self.device == "decoder-node"

        def read(self):
            if self.device in good_devices:
                return True, Image.new("RGB", (320, 240))
            return False, None

        def set(self, prop, value):
            record.setdefault("props", []).append((self.device, prop, value))

        def release(self):
            record.setdefault("released", []).append(self.device)

    mod.VideoCapture = VideoCapture
    mod.cvtColor = lambda frame, code: frame
    return mod


def test_opencv_picks_first_device_that_delivers_frames(tmp_path, monkeypatch):
    devs = [tmp_path / f"video{i}" for i in range(4)]
    for d in devs:
        d.write_bytes(b"")
    record = {}
    # video0 is a VPU/decoder node: opens but never returns a frame.
    monkeypatch.setitem(sys.modules, "cv2", _fake_cv2({str(devs[2])}, record))
    cam = _cam(tmp_path, backend="opencv", video_devices=[str(d) for d in devs])
    data = cam.preview_jpeg()
    assert Image.open(io.BytesIO(data)).size == (160, 120)
    assert cam._cv_device == str(devs[2])
    assert record["opened"] == [str(devs[0]), str(devs[1]), str(devs[2])]
    assert str(devs[0]) in record["released"] and str(devs[1]) in record["released"]
    assert cam.describe()["device"] == str(devs[2])


def test_opencv_reports_no_camera_when_nothing_delivers(tmp_path, monkeypatch):
    dev = tmp_path / "video0"
    dev.write_bytes(b"")
    monkeypatch.setitem(sys.modules, "cv2", _fake_cv2(set(), {}))
    cam = _cam(tmp_path, backend="opencv", video_devices=[str(dev)])
    r = cam.capture()
    assert r["ok"] is False and "No USB camera" in r["error"]


# ── MJPEG stream: always yields, always releasable ───────────────────────────

def _first_frame(part: bytes) -> bytes:
    head, _, body = part.partition(b"\r\n\r\n")
    assert head.startswith(b"--frame")
    return body[: -len(b"\r\n")]


def test_stream_on_camera_less_host_yields_mock_frames_at_preview_fps(tmp_path, monkeypatch):
    monkeypatch.setitem(sys.modules, "picamera2", None)        # import fails
    slept = []
    cam = _cam(tmp_path, backend="picamera2", preview_fps=4.0)
    gen = cam.mjpeg_stream(sleep=slept.append)
    frames = [next(gen) for _ in range(3)]
    for part in frames:
        Image.open(io.BytesIO(_first_frame(part))).verify()
    assert slept == [0.25, 0.25]                                # first frame immediate, then one wait per frame
    assert cam.active_streams == 1
    gen.close()                                                 # client disconnected
    assert cam.active_streams == 0


def test_stream_stops_when_asked_and_releases_thread(tmp_path):
    cam = _cam(tmp_path, backend="mock", preview_fps=5.0)
    stop = threading.Event()
    got = []

    def consume():
        for part in cam.mjpeg_stream(should_stop=stop.is_set, sleep=lambda s: time.sleep(0.01)):
            got.append(len(part))

    before = threading.active_count()
    t = threading.Thread(target=consume, daemon=True)
    t.start()
    time.sleep(0.1)
    stop.set()
    t.join(2.0)
    assert not t.is_alive() and len(got) >= 2
    assert threading.active_count() == before
    assert cam.active_streams == 0


def test_stream_generator_close_runs_finally_without_spinning(tmp_path, monkeypatch):
    monkeypatch.setitem(sys.modules, "picamera2", None)
    cam = _cam(tmp_path, backend="picamera2", preview_fps=5.0)
    calls = {"n": 0}

    def sleep(_s):
        calls["n"] += 1

    gen = cam.mjpeg_stream(sleep=sleep)
    next(gen)
    next(gen)
    gen.close()
    assert calls["n"] == 1                                      # one wait between the two frames, nothing extra


def test_available_probe(tmp_path, monkeypatch):
    assert _cam(tmp_path, backend="mock").available() is True
    monkeypatch.setitem(sys.modules, "picamera2", None)
    assert _cam(tmp_path, backend="picamera2").available() is False
